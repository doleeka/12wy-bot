"""Общий чат с темами: каждый вид сообщений — в свою тему (/topic), приветствие новеньких, пост «Откройте бота»,
объявления с предпросмотром, встречи с напоминаниями, окно оплаты."""
from datetime import date, datetime
from itertools import count

from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, Update, User as TgUser

from bot import scheduler
from bot.models import Meeting
from bot.services import meetings
from tests.test_group_chat import ADMIN_ID, send
from tests.test_payments import SETTINGS, clock, last_buttons, member, press, tg, turn_on  # noqa: F401

COMMON = -100900
_ids = count(95_000)


async def in_topic(tg, text, thread, user_id=ADMIN_ID, chat_id=COMMON):
    dp, bot = tg[0], tg[1]
    cmd = text.split()[0]
    await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=chat_id, type="supergroup", title="С", is_forum=True),
        from_user=TgUser(id=user_id, is_bot=False, first_name="A"), text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(cmd))],
        message_thread_id=thread, is_topic_message=thread is not None)))


def posts(tg, chat_id=COMMON):
    return [c for c in tg[2].of_type(SendMessage) if c.chat_id == chat_id]


async def setup_topics(tg):
    await in_topic(tg, "/linkcommon", 7)        # «Статистика» — итоги
    await in_topic(tg, "/topic встречи", 11)
    await in_topic(tg, "/topic приветствие", 12)
    await in_topic(tg, "/topic объявления", 13)
    await in_topic(tg, "/topic бот", 14)
    await in_topic(tg, "/topic оплата", 15)


async def test_topics_are_set_per_kind(sessionmaker, tg):
    await in_topic(tg, "/topic встречи", 11)  # общий чат ещё не привязан
    assert "/linkcommon" in tg[2].sent(COMMON)[-1]
    await setup_topics(tg)
    assert tg[2].sent(COMMON)[-1] == "Готово 🌿 Сюда: оплата."
    await in_topic(tg, "/topic", 11)  # без вида — список, админу в личку
    listing = tg[2].sent(ADMIN_ID)[-1]
    assert "встречи: тема #11" in listing and "итоги недели: тема #7" in listing
    await in_topic(tg, "/topic встречи", 11, user_id=5)
    assert "только для админа" in tg[2].sent(COMMON)[-1]


async def test_welcome_new_members_in_their_topic(sessionmaker, tg):
    await setup_topics(tg)
    dp, bot = tg[0], tg[1]
    await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=COMMON, type="supergroup", title="С"),
        from_user=TgUser(id=900, is_bot=False, first_name="Аня"),
        new_chat_members=[TgUser(id=900, is_bot=False, first_name="Аня"), TgUser(id=901, is_bot=True, first_name="Bot")])))
    post = posts(tg)[-1]
    assert post.message_thread_id == 12 and "Добро пожаловать, Аня!" in post.text and "Bot" not in post.text
    assert post.reply_markup.inline_keyboard[0][0].url == "https://t.me/twelve_wy_bot"
    # в другом чате (например, тройки) не приветствуем
    before = len(tg[2].of_type(SendMessage))
    await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=-555, type="supergroup", title="Т"),
        from_user=TgUser(id=902, is_bot=False, first_name="Б"), new_chat_members=[TgUser(id=902, is_bot=False, first_name="Б")])))
    assert len(tg[2].of_type(SendMessage)) == before


async def test_open_bot_post_and_announce(sessionmaker, tg):
    await send(tg, "/postbot", ADMIN_ID, "private", ADMIN_ID)
    assert "не привязан" in tg[2].sent(ADMIN_ID)[-1]
    await setup_topics(tg)
    await send(tg, "/postbot", ADMIN_ID, "private", ADMIN_ID)
    post = posts(tg)[-1]
    assert post.message_thread_id == 14 and "всё самое важное — в боте" in post.text and post.reply_markup
    assert "тема «пост «Откройте бота»»" in tg[2].sent(ADMIN_ID)[-1]

    await send(tg, "/announce Встреча <в среду> переносится", ADMIN_ID, "private", ADMIN_ID)
    assert "Так будет в общем чате" in tg[2].sent(ADMIN_ID)[-1]
    n = len(posts(tg))
    yes = [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("ann:yes")][0]
    await press(tg, yes)
    assert len(posts(tg)) == n + 1
    post = posts(tg)[-1]
    assert post.message_thread_id == 13 and post.text == "Встреча &lt;в среду&gt; переносится"
    await press(tg, yes)  # повторное нажатие — не публикует второй раз
    assert len(posts(tg)) == n + 1
    await send(tg, "/announce текст", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]


# ---------- встречи ----------

def test_parse_meeting():
    now = datetime(2026, 11, 3, 10, 0)  # вторник
    p = meetings.parse("12.11 19:00 Разбор недели https://meet", now)
    assert (p.starts_at, p.weekly, p.details) == (datetime(2026, 11, 12, 19, 0), False, "Разбор недели https://meet")
    p = meetings.parse("каждую среду 19:00 Созвон", now)
    assert (p.starts_at, p.weekly) == (datetime(2026, 11, 4, 19, 0), True)
    assert meetings.parse("каждый вторник 09:00 Утро", now).starts_at == datetime(2026, 11, 10, 9, 0)  # сегодня уже прошло
    assert meetings.parse("05.01 19:00 Новый год", now).starts_at == datetime(2027, 1, 5, 19, 0)
    for bad in ("12.11 Разбор", "35.11 19:00 X", "каждую среду Созвон", "12.11 25:00 X", "02.11.2026 10:00 прошла"):
        assert meetings.parse(bad, now) is None


async def test_one_off_meeting_announce_and_reminders(sessionmaker, tg, clock, monkeypatch):
    import bot.handlers.meetings as mh
    await setup_topics(tg)
    monkeypatch.setattr(mh, "local_now", lambda _s=None: datetime(2026, 11, 3, 10, 0))
    await send(tg, "/meeting 12.11 19:00 Разбор недели https://meet", ADMIN_ID, "private", ADMIN_ID)
    assert "12 ноября, 19:00" in tg[2].sent(ADMIN_ID)[-1]
    await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("mt:yes")][0])
    post = posts(tg)[-1]
    assert post.message_thread_id == 11 and "📅 <b>Встреча</b> · 12 ноября, 19:00" in post.text
    assert "встреча №1" in tg[2].sent(ADMIN_ID)[-1]

    async def at(y, m, d, h, mi):
        return await scheduler.send_meeting_reminders(tg[1], sessionmaker, SETTINGS, now=datetime(y, m, d, h, mi))
    assert await at(2026, 11, 11, 18, 55) == 0
    assert await at(2026, 11, 11, 19, 0) == 1 and "Завтра встреча" in posts(tg)[-1].text
    assert await at(2026, 11, 11, 19, 5) == 0
    assert await at(2026, 11, 12, 18, 0) == 1 and "Через час встреча" in posts(tg)[-1].text
    assert posts(tg)[-1].message_thread_id == 11
    assert await at(2026, 11, 12, 18, 30) == 0
    assert await at(2026, 11, 13, 10, 0) == 0
    async with sessionmaker() as s:
        assert (await s.get(Meeting, 1)).active is False  # прошла — выключена


async def test_weekly_meeting_and_off(sessionmaker, tg, monkeypatch):
    import bot.handlers.meetings as mh
    await setup_topics(tg)
    monkeypatch.setattr(mh, "local_now", lambda _s=None: datetime(2026, 11, 3, 10, 0))
    await send(tg, "/meeting каждую среду 19:00 Общий созвон", ADMIN_ID, "private", ADMIN_ID)
    await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("mt:yes")][0])
    assert "каждую среду, 19:00" in posts(tg)[-1].text

    async def at(d, h, mi=0):
        return await scheduler.send_meeting_reminders(tg[1], sessionmaker, SETTINGS, now=datetime(2026, 11, d, h, mi))
    assert await at(3, 19) == 1 and await at(4, 18) == 1  # за день и за час
    assert await at(10, 19) == 1 and await at(11, 18) == 1  # и через неделю снова
    await send(tg, "/meetings", ADMIN_ID, "private", ADMIN_ID)
    assert "№1 — каждую среду" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/meeting off 1", ADMIN_ID, "private", ADMIN_ID)
    assert "выключена" in tg[2].sent(ADMIN_ID)[-1]
    assert await at(17, 19) == 0
    await send(tg, "/meeting off 1", ADMIN_ID, "private", ADMIN_ID)
    assert "Встречи №1 нет" in tg[2].sent(ADMIN_ID)[-1]


# ---------- окно оплаты в общем чате ----------

async def test_payment_window_posts_in_payment_topic(sessionmaker, tg, clock):
    await setup_topics(tg)
    await member(sessionmaker, 1, "basic", paid_through=2)  # 3-й месяц ещё не оплачен
    await member(sessionmaker, 2, "app")
    await turn_on(tg)
    n = len(posts(tg))
    clock["today"] = date(2026, 11, 26)
    await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS)
    assert len(posts(tg)) == n
    clock["today"] = date(2026, 11, 27)  # пятница перед 9-й неделей
    await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS)
    post = posts(tg)[-1]
    assert post.message_thread_id == 15 and "Поток 1: открыта оплата 3-го месяца" in post.text and "30.11" in post.text
    assert post.reply_markup.inline_keyboard[0][0].url.endswith("?start=pay")
    assert "U1" not in post.text and "₸" not in post.text  # без имён и сумм
    clock["today"] = date(2026, 11, 30)
    await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS)
    assert "последний день оплаты 3-го месяца" in posts(tg)[-1].text

    # кнопка «Оплатить в боте»: в окне — выбор тарифа; вне окна — «ничего не нужно»
    clock["today"] = date(2026, 11, 28)
    await send(tg, "/start pay", 1, "private", 1)
    assert "3-му месяцу" in tg[2].sent(1)[-1]
    await send(tg, "/start pay", 2, "private", 2)
    assert "ничего не нужно" in tg[2].sent(2)[-1]


async def test_returning_members_are_not_welcomed_again(sessionmaker, tg):
    """Вернулась в общий чат после паузы / смены тарифа — без «добро пожаловать, начни с бота»."""
    from bot.models import OnboardingStep, User
    await setup_topics(tg)
    await member(sessionmaker, 1, "basic")  # уже участница
    async with sessionmaker() as s:
        s.add(User(telegram_id=2, first_name="Дана", onboarding_step=OnboardingStep.EXPLORE, cohort_id=1))
        await s.commit()
    dp, bot = tg[0], tg[1]

    async def joins(tg_id, name):
        await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
            message_id=next(_ids), date=datetime.now(), chat=Chat(id=COMMON, type="supergroup", title="С"),
            from_user=TgUser(id=tg_id, is_bot=False, first_name=name),
            new_chat_members=[TgUser(id=tg_id, is_bot=False, first_name=name)])))
    n = len(posts(tg))
    await joins(1, "U1")
    assert len(posts(tg)) == n  # своя — молчим
    await joins(2, "Дана")
    assert "Продолжи в боте" in posts(tg)[-1].text  # не закончила онбординг — мягко
    await joins(3, "Новая")
    assert "Добро пожаловать, Новая!" in posts(tg)[-1].text and "нажми «Старт»" in posts(tg)[-1].text
