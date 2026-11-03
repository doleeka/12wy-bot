"""Оплата по месяцам: потоки со своими ценами, 3 тарифа × (1 месяц | сразу 3), личные цены, окно оплаты
пт–пн перед 5-й и 9-й неделей, со вторника без оплаты — доступ на паузе; чек → админ подтверждает."""
from datetime import date, datetime
from itertools import count

import httpx
import pytest
from aiogram.methods import CopyMessage, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User as TgUser

import bot.handlers.payments as pay_handlers
import bot.scheduler as scheduler
import webapp.app as appmod
from bot.config import Settings
from bot.digest import stats_message
from bot.main import build_dispatcher
from bot.models import Cohort, Payment, PaymentStatus
from bot.services import payments, teams
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.webapp_helpers import TOKEN, auth

_ids = count(70_000)
KASPI = "Kaspi: +7 700 000 00 00 (Динара Н.)"
OCT5, DEC28 = date(2026, 10, 5), date(2026, 12, 28)        # старт 1-го и 2-го потока
FRI, MON, TUE = date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3)  # окно перед 5-й неделей 1-го потока
NEW = 501
SETTINGS = Settings(bot_token=TOKEN, database_path=None, admin_ids=[ADMIN_ID], webapp_url="https://app.test")


@pytest.fixture
def clock(monkeypatch):
    """«Сегодня» для бота, расписания и приложения; по умолчанию — вторник 5-й недели 1-го потока."""
    now = {"today": TUE}
    for mod in (pay_handlers, scheduler, appmod):
        monkeypatch.setattr(mod, "local_today", lambda _s=None: now["today"])
    return now


@pytest.fixture
async def tg(sessionmaker, clock):
    """Бот с двумя потоками и ценами из ТЗ."""
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = SETTINGS
    async with sessionmaker() as s:
        c1 = Cohort(number=1, start=OCT5, enrolling=True)
        c2 = Cohort(number=2, start=DEC28, enrolling=False)
        s.add_all([c1, c2])
        await s.flush()
        for tariff, months, amount in (("app", 1, 0), ("basic", 1, 1990), ("focus", 1, 9990)):
            await payments.set_cohort_price(s, c1, tariff, months, amount)
        for tariff, months, amount in (("app", 1, 990), ("basic", 1, 2990), ("basic", 3, 7990)):
            await payments.set_cohort_price(s, c2, tariff, months, amount)
        await s.commit()
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def member(sessionmaker, tg_id, tariff="basic", paid_through=1, cohort=1, cycle_start=OCT5):
    """Участница 1-го потока, прошедшая 1-й месяц (как всех переносит миграция 0012)."""
    await make_user(sessionmaker, tg_id, cycle_start=cycle_start)
    async with sessionmaker() as s:
        u = await teams.get_user_by_telegram_id(s, tg_id)
        u.cohort_id, u.tariff, u.paid_through, u.no_team = cohort, tariff, paid_through, tariff == "app"
        await teams.leave_team(s, u)  # make_user распределил до того, как узнал поток, — распределяем заново
        await s.commit()
        await teams.assign_to_team(s, u)


async def user(sessionmaker, tg_id):
    async with sessionmaker() as s:
        return await teams.get_user_by_telegram_id(s, tg_id)


async def receipt(tg, user_id, document=False):
    dp, bot = tg[0], tg[1]
    extra = ({"document": Document(file_id="pdf1", file_unique_id="u1", file_name="kaspi.pdf")} if document
             else {"photo": [PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
                             PhotoSize(file_id="big", file_unique_id="b", width=900, height=900)]})
    await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Новенькая"), **extra)))


async def press(tg, data, user_id=ADMIN_ID):
    dp, bot = tg[0], tg[1]
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"), text="x")
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=TgUser(id=user_id, is_bot=False, first_name="A"), chat_instance="x",
        message=msg, data=data)))


def last_buttons(tg, chat_id):
    m = [c for c in tg[2].calls if isinstance(c, (SendMessage, CopyMessage)) and c.chat_id == chat_id and c.reply_markup][-1]
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


async def turn_on(tg):
    await send(tg, "/payment text " + KASPI, ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/payment on confirm", ADMIN_ID, "private", ADMIN_ID)


async def app_status(sessionmaker, user_id):
    app = appmod.create_app(sessionmaker, SETTINGS, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/me", headers=auth(user_id=user_id))
        return r.status_code, r.json().get("detail")


# ---------- месяцы цикла и окно оплаты ----------

def test_months_grace_monday_and_window():
    from types import SimpleNamespace as NS
    u = NS(cycle_start=OCT5)
    assert payments.cycle_month(u, None, date(2026, 10, 1)) == 1      # до старта
    assert payments.cycle_month(u, None, date(2026, 11, 1)) == 1      # воскресенье 4-й недели
    assert payments.cycle_month(u, None, MON) == 2                    # 5-я неделя
    assert payments.needed_month(u, None, MON) == 1                   # понедельник 5-й — ещё можно оплатить
    assert payments.needed_month(u, None, TUE) == 2                   # со вторника — нужен 2-й месяц
    assert payments.needed_month(u, None, date(2026, 11, 30)) == 2    # понедельник 9-й недели
    assert payments.needed_month(u, None, date(2026, 12, 1)) == 3
    assert payments.needed_month(u, None, date(2026, 12, 28)) == 0    # 13-я неделя — платить не за что


# ---------- 1-й поток: закрыть тех, кто не оплатил ----------

async def test_cohort1_unpaid_are_paused_until_payment(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")   # в тройке, 2-й месяц не оплачен
    await member(sessionmaker, 2, "app")     # «Приложение» в 1-м потоке бесплатно — доступ не закрывается
    await member(sessionmaker, 3, "basic")   # оплатила напрямую — админ отметит до включения
    await send(tg, "/payment text " + KASPI, ADMIN_ID, "private", ADMIN_ID)

    # сначала предпросмотр: у кого закроется; ничего не включено, никому ничего не ушло
    tg[2].calls.clear()
    await send(tg, "/payment on", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "закроется у <b>2</b>" in preview and "U1" in preview and "U3" in preview and "U2" not in preview
    assert "2-й месяц" in preview and "/payment on confirm" in preview
    async with sessionmaker() as s:
        assert not await payments.is_required(s)
    assert {c.chat_id for c in tg[2].of_type(SendMessage)} == {ADMIN_ID}

    await send(tg, "/paid U3", ADMIN_ID, "private", ADMIN_ID)
    assert "оплачено по 2-й месяц" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/payment on confirm", ADMIN_ID, "private", ADMIN_ID)
    assert "отправлено: 1" in tg[2].sent(ADMIN_ID)[-1]

    # ей — выбор тарифа с ценами 1-го потока (только за месяц: середина цикла)
    assert "2-му месяцу" in tg[2].sent(1)[-1] and "недели 5–8" in tg[2].sent(1)[-1]
    labels = [t for t, _ in last_buttons(tg, 1)]
    assert labels == ["Приложение · 1 месяц — бесплатно", "Базовый · 1 месяц — 1 990 ₸", "Фокус-группа · 1 месяц — 9 990 ₸"]
    # пока не оплатит — только выбор тарифа, в боте и в приложении; чек-ин не приходит
    await send(tg, "/plan", 1, "private", 1)
    assert "2-му месяцу" in tg[2].sent(1)[-1]
    assert await app_status(sessionmaker, 1) == (403, "unpaid")
    assert (await app_status(sessionmaker, 2))[0] == 200 and (await app_status(sessionmaker, 3))[0] == 200

    # выбрала «Базовый» → сумма и реквизиты → чек → админ подтверждает
    await press(tg, "pay:pick:basic:1", user_id=1)
    assert "1 990 ₸" in tg[2].sent(1)[-1] and KASPI in tg[2].sent(1)[-1]
    await receipt(tg, 1)
    copy = tg[2].of_type(CopyMessage)[-1]
    assert copy.chat_id == ADMIN_ID and "поток 1" in copy.caption
    assert "Базовый · 1 месяц · 2-й месяц · <b>1 990 ₸</b>" in copy.caption
    await send(tg, "/plan", 1, "private", 1)
    assert "жду подтверждения" in tg[2].sent(1)[-1]
    ok = [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:ok")][0]
    await press(tg, ok)
    assert "доступ открыт по 2-й месяц" in tg[2].sent(1)[-1]
    u = await user(sessionmaker, 1)
    assert u.paid_through == 2 and u.tariff == "basic" and not u.no_team
    assert (await app_status(sessionmaker, 1))[0] == 200
    await press(tg, ok)  # повтор — «уже обработан», ничего не меняется
    assert (await user(sessionmaker, 1)).paid_through == 2


async def test_grace_monday_and_reminders(sessionmaker, tg, clock):
    await member(sessionmaker, 1, "basic")
    await member(sessionmaker, 2, "app")
    await turn_on(tg)
    tg[2].calls.clear()

    clock["today"] = date(2026, 10, 29)  # четверг — ещё рано
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 0
    clock["today"] = FRI
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 1
    assert "Скоро <b>2-й месяц</b>" in tg[2].sent(1)[-1] and "02.11" in tg[2].sent(1)[-1]
    assert not tg[2].sent(2)  # бесплатный тариф — ничего не приходит
    assert (await app_status(sessionmaker, 1))[0] == 200  # окно открыто, доступ ещё есть

    clock["today"] = MON  # понедельник 5-й недели — последний день, доступ ещё есть
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 1
    assert "последний день" in tg[2].sent(1)[-1]
    assert (await app_status(sessionmaker, 1))[0] == 200
    await send(tg, "/plan", 1, "private", 1)
    assert "месяцу" not in tg[2].sent(1)[-1]

    clock["today"] = TUE  # вторник — пауза до оплаты
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 1
    assert "2-му месяцу" in tg[2].sent(1)[-1]
    assert (await app_status(sessionmaker, 1)) == (403, "unpaid")
    clock["today"] = date(2026, 11, 4)  # дальше не надоедаем
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 0

    # можно оплатить заранее, прямо в окне: выбрала в пятницу перед 9-й неделей — чек принимается
    await send(tg, "/paid U1", ADMIN_ID, "private", ADMIN_ID)
    clock["today"] = date(2026, 11, 27)
    await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS)
    assert "Скоро <b>3-й месяц</b>" in tg[2].sent(1)[-1]
    await press(tg, "pay:pick:basic:1", user_id=1)
    await receipt(tg, 1)
    assert "3-й месяц" in tg[2].of_type(CopyMessage)[-1].caption
    assert await scheduler.send_payment_reminders(tg[1], sessionmaker, SETTINGS) == 0  # чек у админа — не пишем


async def test_paused_skip_checkins_and_stats(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")
    await member(sessionmaker, 3, "basic", paid_through=2)
    await turn_on(tg)
    tg[2].calls.clear()
    from bot.scheduler import send_weekly_checkins
    assert await send_weekly_checkins(tg[1], sessionmaker, SETTINGS) == 1
    assert not tg[2].sent(1) and tg[2].sent(3)
    async with sessionmaker() as s:
        text = await stats_message(s, SETTINGS, TUE)
    assert "Неделя 5 — идёт" in text and "из 1" in text  # на паузе не считаются


# ---------- новенькие 2-го потока, личная цена, 3 месяца сразу ----------

async def test_newcomer_cohort2_prices_personal_and_three_months(sessionmaker, tg, clock):
    clock["today"] = date(2026, 12, 10)
    await send(tg, "/cohort enroll 2", ADMIN_ID, "private", ADMIN_ID)
    await turn_on(tg)

    await send(tg, "/start", NEW, "private", NEW)
    assert "открывается после оплаты" in tg[2].sent(NEW)[-1]
    assert [t for t, _ in last_buttons(tg, NEW)] == [
        "Приложение · 1 месяц — 990 ₸", "Базовый · 1 месяц — 2 990 ₸", "Базовый · 3 месяца — 7 990 ₸"]  # фокус-группы нет
    u = await user(sessionmaker, NEW)
    assert u.cohort_id == 2 and u.paid_through == 0

    # личная цена для одной девушки
    await send(tg, f"/price {NEW} приложение 1 690", ADMIN_ID, "private", ADMIN_ID)
    assert "690 ₸" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/start", NEW, "private", NEW)
    assert "Приложение · 1 месяц — 690 ₸" in [t for t, _ in last_buttons(tg, NEW)]

    await press(tg, "pay:pick:basic:3", user_id=NEW)
    assert "7 990 ₸" in tg[2].sent(NEW)[-1]
    await receipt(tg, NEW, document=True)
    copy = tg[2].of_type(CopyMessage)[-1]
    assert "поток 2" in copy.caption and "Базовый · 3 месяца · 1–3-й месяцы · <b>7 990 ₸</b>" in copy.caption
    await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:ok")][0])
    u = await user(sessionmaker, NEW)
    assert u.paid_through == 3 and u.tariff == "basic"
    assert "маленькая команда" in tg[2].sent(NEW)[-1]  # приветствие онбординга
    async with sessionmaker() as s:
        p = await s.get(Payment, 1)
        assert (p.status, p.amount, p.months, p.month_from, p.cohort_id) == (PaymentStatus.APPROVED, 7990, 3, 1, 2)


async def test_rejected_then_app_tariff_goes_solo(sessionmaker, tg, clock):
    clock["today"] = date(2026, 12, 10)
    await send(tg, "/cohort enroll 2", ADMIN_ID, "private", ADMIN_ID)
    await turn_on(tg)
    await receipt(tg, NEW)  # чек без выбранного тарифа — сначала выбор
    assert "Сначала выбери тариф" in tg[2].sent(NEW)[-1] and not tg[2].of_type(CopyMessage)
    await press(tg, "pay:pick:app:1", user_id=NEW)
    await receipt(tg, NEW)
    await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:no")][0])
    assert "не нашла эту оплату" in tg[2].sent(NEW)[-1]
    assert (await user(sessionmaker, NEW)).paid_through == 0
    await receipt(tg, NEW)  # тариф помнится — сразу новый чек
    await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:ok")][0])
    u = await user(sessionmaker, NEW)
    assert u.paid_through == 1 and u.tariff == "app" and u.no_team
    assert "самостоятельно" in tg[2].sent(NEW)[-1]  # приветствие без обещания команды


async def test_free_tariff_needs_no_receipt(sessionmaker, tg):
    await turn_on(tg)
    await send(tg, "/start", NEW, "private", NEW)  # 1-й поток, идёт 5-я неделя → платит со 2-го месяца
    assert "открывается после оплаты" in tg[2].sent(NEW)[-1]  # новенькой — приветствие, а не «план сохранён»
    await press(tg, "pay:pick:app:1", user_id=NEW)
    assert "бесплатный" in tg[2].sent(NEW)[-2] and not tg[2].of_type(CopyMessage)
    u = await user(sessionmaker, NEW)
    assert u.tariff == "app" and u.no_team and u.paid_through == 2


async def test_focus_tariff_joins_focus_group(sessionmaker, tg):
    for tg_id in (1, 2, 3, 4):
        await member(sessionmaker, tg_id, "basic")
    await turn_on(tg)
    for tg_id in (1, 2, 3, 4):  # четверо в фокус-группе — без лимита в 3
        await press(tg, "pay:pick:focus:1", user_id=tg_id)
        await receipt(tg, tg_id)
        await press(tg, [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:ok")][0])
    async with sessionmaker() as s:
        team_ids = {(await teams.get_team_of(s, await teams.get_user_by_telegram_id(s, i))).id for i in (1, 2, 3, 4)}
        focus = await teams.focus_team(s, 1)
    assert team_ids == {focus.id} and focus.name == "Фокус-группа"
    assert "Фокус-группа" in tg[2].sent(4)[-1]


# ---------- админ: цены и потоки ----------

async def test_admin_prices_and_cohorts(sessionmaker, tg):
    await send(tg, "/price", ADMIN_ID, "private", ADMIN_ID)
    text = tg[2].sent(ADMIN_ID)[-1]
    assert "Поток 1" in text and "Базовый: 1 месяц — 1 990 ₸ · сразу 3 — нет" in text and "Поток 2" in text
    await send(tg, "/price 2 фокус 1 нет", ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/price 2 приложение 3 2490", ADMIN_ID, "private", ADMIN_ID)
    assert "Приложение: 1 месяц — 990 ₸ · сразу 3 — 2 490 ₸" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/price 2 базовый один 100", ADMIN_ID, "private", ADMIN_ID)
    assert "Не поняла" in tg[2].sent(ADMIN_ID)[-1]

    await send(tg, "/cohort new 30.03.2027", ADMIN_ID, "private", ADMIN_ID)
    assert "не понедельник" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/cohort new 22.03.2027", ADMIN_ID, "private", ADMIN_ID)
    assert "Создан поток 3" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/cohort enroll 3", ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/cohort", ADMIN_ID, "private", ADMIN_ID)
    rows = tg[2].sent(ADMIN_ID)[-1]
    assert "Поток 3 · старт 22.03.2027 · участниц: 0 · ✅ идёт набор" in rows
    assert "Поток 1 · старт 05.10.2026" in rows and "Поток 1 · старт 05.10.2026 · участниц: 0 · ✅" not in rows

    await member(sessionmaker, 7, "basic")
    await send(tg, "/price", 7, "private", 7)
    assert "только для админа" in tg[2].sent(7)[-1]
    await send(tg, "/start", ADMIN_ID, "private", ADMIN_ID)  # админ платить не должен, даже если оплата включена
    await turn_on(tg)
    await send(tg, "/plan", ADMIN_ID, "private", ADMIN_ID)
    assert "месяцу" not in tg[2].sent(ADMIN_ID)[-1]
    await press(tg, "pay:ok:1", user_id=7)  # кнопки решения — только у админа


async def test_off_by_default_nobody_is_paused(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")
    await send(tg, "/plan", 1, "private", 1)
    assert "месяцу" not in tg[2].sent(1)[-1]
    assert (await app_status(sessionmaker, 1))[0] == 200
    await send(tg, "/payment on", ADMIN_ID, "private", ADMIN_ID)
    assert "Сначала задай реквизиты" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/payment", ADMIN_ID, "private", ADMIN_ID)
    assert "выключена" in tg[2].sent(ADMIN_ID)[-1] and "Набор идёт в поток: 1" in tg[2].sent(ADMIN_ID)[-1]


async def test_cohorts_do_not_mix(sessionmaker, tg):
    """2-й поток: свой старт (28.12), свои тройки; выравнивание при старте бота не тянет его к 05.10."""
    from bot.services.onboarding import align_to_cohort_start
    await member(sessionmaker, 1, "basic")  # одна в тройке 1-го потока — свободные места есть
    await member(sessionmaker, 2, "basic", cohort=2, cycle_start=date(2026, 12, 21))  # «ошибочный» старт
    async with sessionmaker() as s:
        u1, u2 = await teams.get_user_by_telegram_id(s, 1), await teams.get_user_by_telegram_id(s, 2)
        assert await payments.cohort_start(s, u2, SETTINGS) == DEC28
        t1, t2 = await teams.get_team_of(s, u1), await teams.get_team_of(s, u2)
        assert t1.id != t2.id and t1.cohort_id == 1 and t2.cohort_id == 2
        assert await align_to_cohort_start(s, OCT5, date(2026, 11, 3), 1) == 0  # 1-й поток не трогает 2-й
        assert await align_to_cohort_start(s, DEC28, date(2026, 11, 3), 2) == 1
        await s.commit()
    assert (await user(sessionmaker, 2)).cycle_start == DEC28


async def test_who_and_bulk_paid(sessionmaker, tg):
    """Многие оплатили напрямую: /who показывает, кто нет; /paid отмечает сразу многих — тихо, пока доступ открыт."""
    for tg_id in (101, 102, 103, 104):
        await member(sessionmaker, tg_id, "basic")
    await member(sessionmaker, 105, "app")
    async with sessionmaker() as s:
        (await teams.get_user_by_telegram_id(s, 102)).username = "dana"
        await s.commit()
    await send(tg, "/who", ADMIN_ID, "private", ADMIN_ID)
    who = tg[2].sent(ADMIN_ID)[-1]
    assert "Не оплачено (4)" in who and "Бесплатный тариф (1)" in who and "<code>101</code>" in who
    assert "нужен 2-й месяц" in who

    tg[2].calls.clear()
    await send(tg, "/paid 101, @dana 103 нетакой", ADMIN_ID, "private", ADMIN_ID)
    report = tg[2].sent(ADMIN_ID)[-1]
    assert "Отмечено: 3" in report and "по 2-й месяц, Базовый" in report and "Не нашла: нетакой" in report
    assert not any(tg[2].sent(i) for i in (101, 102, 103))  # оплата выключена, доступ не закрывался — тихо
    for tg_id in (101, 102, 103):
        assert (await user(sessionmaker, tg_id)).paid_through == 2

    await send(tg, "/paid фокус 104", ADMIN_ID, "private", ADMIN_ID)  # с тарифом
    assert (await user(sessionmaker, 104)).tariff == "focus"
    await send(tg, "/who", ADMIN_ID, "private", ADMIN_ID)
    assert "Не оплачено (0)" in tg[2].sent(ADMIN_ID)[-1] and "Оплачено (4)" in tg[2].sent(ADMIN_ID)[-1]

    # после включения: у кого доступ закрыт, тому /paid пишет, что всё открыто
    await member(sessionmaker, 106, "basic")
    await turn_on(tg)
    await send(tg, "/paid 106", ADMIN_ID, "private", ADMIN_ID)
    assert "доступ открыт по 2-й месяц" in tg[2].sent(106)[-1]


async def test_cohort_move_keeps_plan(sessionmaker, tg):
    """Перевод во 2-й поток: план сохраняется; если цикл шёл — прежний остаётся в истории, план — в новом цикле."""
    from aiogram.methods import BanChatMember
    from sqlalchemy import func, select
    from bot.models import Checkin, OnboardingStep, Priority, User, WeeklyTactic
    from bot.services import checkins

    await member(sessionmaker, 201, "basic")  # цикл идёт: 5-я неделя 1-го потока, есть отметки
    await member(sessionmaker, 203, "basic")  # сокомандница — остаётся в 1-м потоке
    async with sessionmaker() as s:
        u = await teams.get_user_by_telegram_id(s, 201)
        for t in await checkins.tactics_for_week(s, u, OCT5):
            await checkins.set_mark(s, u, t.id, OCT5, True)
        (await teams.get_team_of(s, u)).chat_id = -100777
        s.add(User(telegram_id=202, first_name="U202", onboarding_step=OnboardingStep.EXPLORE, cohort_id=1))
        await s.commit()

    tg[2].calls.clear()
    await send(tg, "/cohort move 2 201 202 нетакой", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "проходила прежние недели" in preview and "цикл ещё не начался" in preview and "не нашла" in preview
    assert "/cohort move 2 201 202 confirm" in preview
    assert (await user(sessionmaker, 201)).cohort_id == 1 and not tg[2].sent(201)  # предпросмотр ничего не меняет

    await send(tg, "/cohort move 2 201 202 confirm", ADMIN_ID, "private", ADMIN_ID)
    assert "переведено 2" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as s:
        u = await teams.get_user_by_telegram_id(s, 201)
        assert (u.cohort_id, u.cycle, u.cycle_start, u.paid_through) == (2, 2, DEC28, 0)
        assert u.onboarding_step == OnboardingStep.DONE
        new_p = list(await s.scalars(select(Priority).where(Priority.user_id == u.id, Priority.cycle == 2)))
        old_p = list(await s.scalars(select(Priority).where(Priority.user_id == u.id, Priority.cycle == 1)))
        assert [p.title for p in new_p] == [p.title for p in old_p] and len(new_p) == 2
        assert len(await checkins.active_tactics(s, u)) == 4  # действия переехали
        old_active = await s.scalar(select(func.count(WeeklyTactic.id)).where(
            WeeklyTactic.priority_id.in_([p.id for p in old_p]), WeeklyTactic.is_active))
        assert old_active == 0
        assert await s.scalar(select(func.count(Checkin.id)).where(Checkin.user_id == u.id)) == 4  # история цела
        team = await teams.get_team_of(s, u)
        assert team is not None and team.cohort_id == 2
        mate_team = await teams.get_team_of(s, await teams.get_user_by_telegram_id(s, 203))
        assert mate_team.cohort_id == 1 and mate_team.id != team.id
        v = await teams.get_user_by_telegram_id(s, 202)
        assert (v.cohort_id, v.cycle, v.cycle_start) == (2, 1, None)  # ещё в онбординге — просто другой поток
    assert [(b.chat_id, b.user_id) for b in tg[2].of_type(BanChatMember)] == [(-100777, 201)]
    assert "поток 2" in tg[2].sent(201)[-1] and "Отметки прошлых недель" in tg[2].sent(201)[-1]
    assert "поток 2" in tg[2].sent(202)[-1]

    await send(tg, "/cohort move 2 201", ADMIN_ID, "private", ADMIN_ID)
    assert "уже в потоке 2" in tg[2].sent(ADMIN_ID)[-1]


async def test_cohort_that_started_at_week5_late_joiner_and_veteran(sessionmaker, tg, clock):
    """2-й поток стартует 02.11 (5-я неделя 1-го). Пришла поздно — тот же план, отметки этой недели = её неделя 1;
    проходила 1-й поток — прежние недели в историю, отметки с 02.11 переезжают к плану нового цикла."""
    from sqlalchemy import select
    from bot.models import Checkin, PlanWeekSnapshot
    from bot.services import checkins, scorecard

    await member(sessionmaker, 301, "basic")  # поздняя: старт выровняли на 05.10, отмечать начала только 02.11
    await member(sessionmaker, 302, "basic")  # проходила с 05.10
    async with sessionmaker() as s:
        late, vet = await teams.get_user_by_telegram_id(s, 301), await teams.get_user_by_telegram_id(s, 302)
        for t in await checkins.tactics_for_week(s, late, MON):
            await checkins.set_mark(s, late, t.id, MON, True)
        for week in (OCT5, MON):
            for t in await checkins.tactics_for_week(s, vet, week):
                await checkins.set_mark(s, vet, t.id, week, True)
        s.add(PlanWeekSnapshot(user_id=late.id, cycle=1, week_number=1, plan=[]))  # снимок недели, которой у неё не было
        await s.commit()

    await send(tg, "/cohort new 02.11.2026", ADMIN_ID, "private", ADMIN_ID)
    assert "Создан поток 3" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/cohort move 3 301 302", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "301</code> — до старта потока ничего не отмечала" in preview
    assert "302</code> — проходила прежние недели" in preview
    await send(tg, "/cohort move 3 301 302 confirm", ADMIN_ID, "private", ADMIN_ID)

    async with sessionmaker() as s:
        late, vet = await teams.get_user_by_telegram_id(s, 301), await teams.get_user_by_telegram_id(s, 302)
        assert (late.cycle, late.cycle_start, late.cohort_id) == (1, MON, 3)  # тот же цикл и те же действия
        assert checkins.checkin_week_number(late, MON) == 1 and await scorecard.week_percent(s, late, MON) == 100
        snaps = list(await s.scalars(select(PlanWeekSnapshot.week_number).where(PlanWeekSnapshot.user_id == late.id)))
        assert snaps == [-1]  # чужая неделя 1 к её неделе 1 не применяется, запись цела
        assert (vet.cycle, vet.cycle_start) == (2, MON)
        assert await scorecard.week_percent(s, vet, MON) == 100  # отметки 02.11 переехали к новому плану
        all_marks = list(await s.scalars(select(Checkin).where(Checkin.user_id == vet.id)))
        assert len(all_marks) == 8  # история 05.10 цела
        assert (await teams.get_team_of(s, late)).cohort_id == 3


async def test_admin_changes_tariff_without_payment(sessionmaker, tg):
    from bot.team_notify import COMMON_CHAT
    await member(sessionmaker, 401, "app")    # «без комьюнити»
    await member(sessionmaker, 402, "app")
    await member(sessionmaker, 403, "basic")
    async with sessionmaker() as s:
        await payments.set_setting(s, COMMON_CHAT, "-100900")
        await s.commit()
    await send(tg, "/tariff базовый 401 402 нетакой", ADMIN_ID, "private", ADMIN_ID)
    report = tg[2].sent(ADMIN_ID)[-1]
    assert "Тариф «Базовый»: 2" in report and "Не нашла: нетакой" in report
    async with sessionmaker() as s:
        u = await teams.get_user_by_telegram_id(s, 401)
        assert u.tariff == "basic" and not u.no_team and u.paid_through == 1  # оплата не тронута
        assert (await teams.get_team_of(s, u)) is not None  # сразу в тройку
    assert any("сменила твой тариф на «Базовый»" in t for t in tg[2].sent(401))
    assert any("Общий чат" in t for t in tg[2].sent(401))  # и ссылка в общий чат

    await send(tg, "/tariff фокус 403", ADMIN_ID, "private", ADMIN_ID)
    async with sessionmaker() as s:
        team = await teams.get_team_of(s, await teams.get_user_by_telegram_id(s, 403))
        assert team.is_focus
    await send(tg, "/tariff приложение 402", ADMIN_ID, "private", ADMIN_ID)
    async with sessionmaker() as s:
        u = await teams.get_user_by_telegram_id(s, 402)
        assert u.tariff == "app" and u.no_team and await teams.get_team_of(s, u) is None
    await send(tg, "/tariff приложение 402", ADMIN_ID, "private", ADMIN_ID)
    assert "Уже на этом тарифе" in tg[2].sent(ADMIN_ID)[-1]

    await send(tg, "/solo 401", ADMIN_ID, "private", ADMIN_ID)  # /solo — это тоже «Приложение»
    assert (await user(sessionmaker, 401)).tariff == "app"
    await send(tg, "/unsolo 401", ADMIN_ID, "private", ADMIN_ID)
    assert (await user(sessionmaker, 401)).tariff == "basic"
