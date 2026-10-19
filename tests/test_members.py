"""Исключение из сообщества (/remove) и возврат (/restore): тихо, обратимо, данные целы."""
from datetime import date, datetime
from itertools import count

import httpx
import pytest
from aiogram.methods import BanChatMember, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

from bot.config import Settings
from bot.handlers.broadcast import recipients
from bot.main import build_dispatcher
from bot.scheduler import send_week_planning
from bot.services import checkins, scorecard, teams
from bot.services.stats import week_stats
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.webapp_helpers import TOKEN, auth
from webapp.app import create_app

_ids = count(50_000)
WEEK = scorecard.week_start(date.today())


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def press(tg, data, user_id=ADMIN_ID):
    dp, bot, _ = tg
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"), text="p")
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=TgUser(id=user_id, is_bot=False, first_name="A"), chat_instance="x", message=msg, data=data,
    )))


def last_buttons(session, chat_id):
    m = [c for c in session.of_type(SendMessage) if c.chat_id == chat_id and c.reply_markup][-1]
    return [b.callback_data for b in m.reply_markup.inline_keyboard[0]]


async def community(sessionmaker):
    await make_user(sessionmaker, ADMIN_ID)
    for tg_id, name, nick in ((1, "Арман", "arman"), (2, "Дана", None), (3, "Айгерим", None)):
        await make_user(sessionmaker, tg_id)
        async with sessionmaker() as session:
            u = await teams.get_user_by_telegram_id(session, tg_id)
            u.first_name, u.username = name, nick
            await session.commit()


async def test_remove_is_silent_and_complete(sessionmaker, tg):
    await community(sessionmaker)
    async with sessionmaker() as session:  # у команды есть групповой чат — бот уберёт оттуда
        team = await teams.get_team_of(session, await teams.get_user_by_telegram_id(session, 1))
        team.chat_id = -100500
        await session.commit()
    tg[2].calls.clear()
    await send(tg, "/remove @arman", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "Исключить из сообщества?" in preview and "Арман" in preview and "никому ничего не придёт" in preview
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 1)).excluded_at is None  # предпросмотр ничего не меняет
    yes, _ = last_buttons(tg[2], ADMIN_ID)
    await press(tg, yes)
    assert "исключён(а)" in tg[2].sent(ADMIN_ID)[-1]
    # никому, кроме админа, ничего не ушло; из чата команды — убран
    assert {c.chat_id for c in tg[2].of_type(SendMessage)} == {ADMIN_ID}
    assert [(b.chat_id, b.user_id) for b in tg[2].of_type(BanChatMember)] == [(-100500, 1)]

    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        assert u.excluded_at is not None and await teams.get_team_of(session, u) is None
        assert await checkins.get_marks(session, u, WEEK) == {}  # данные никуда не делись (план на месте)
        assert len(await checkins.active_tactics(session, u)) == 4
        # выпал(а) отовсюду
        assert 1 not in [x.telegram_id for x in await checkins.users_for_checkin(session, WEEK)]
        assert 1 not in await recipients(session, ADMIN_ID)
        assert (await week_stats(session, WEEK)).participants == 3  # админ + 2 девочки
        assert await teams.assign_to_team(session, u) == (None, False)
    bot2, tg2 = make_fake_bot()
    await send_week_planning(bot2, sessionmaker, Settings(bot_token="x", database_path=None, admin_ids=[]))
    assert not tg2.sent(1) and tg2.sent(2)


async def test_excluded_gets_access_closed_in_bot_and_app(sessionmaker, tg):
    await community(sessionmaker)
    await send(tg, "/remove 1", ADMIN_ID, "private", ADMIN_ID)
    await press(tg, last_buttons(tg[2], ADMIN_ID)[0])
    for cmd in ("/start", "/checkin", "/team"):
        await send(tg, cmd, 1, "private", 1)
        assert tg[2].sent(1)[-1] == "Доступ закрыт. Если это ошибка — напиши организатору."
    bot, _ = make_fake_bot()
    app = create_app(sessionmaker, Settings(bot_token=TOKEN, database_path=None, admin_ids=[ADMIN_ID]), bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        r = await api.get("/api/me", headers=auth(user_id=1))
        assert r.status_code == 403 and r.json()["detail"] == "excluded"
        assert (await api.get("/api/me", headers=auth(user_id=2))).status_code == 200


async def test_restore_brings_back(sessionmaker, tg):
    await community(sessionmaker)
    await send(tg, "/remove Арман", ADMIN_ID, "private", ADMIN_ID)
    await press(tg, last_buttons(tg[2], ADMIN_ID)[0])
    await send(tg, "/restore", ADMIN_ID, "private", ADMIN_ID)
    assert "Арман" in tg[2].sent(ADMIN_ID)[-1] and "id <code>1</code>" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/restore 1", ADMIN_ID, "private", ADMIN_ID)
    assert "снова в сообществе" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        assert u.excluded_at is None
        assert 1 in [x.telegram_id for x in await checkins.users_for_checkin(session, WEEK)]
    await send(tg, "/start", 1, "private", 1)
    assert "Доступ закрыт" not in tg[2].sent(1)[-1]
    await send(tg, "/restore", ADMIN_ID, "private", ADMIN_ID)
    assert "Исключённых нет" in tg[2].sent(ADMIN_ID)[-1]


async def test_cancel_guards_and_admin_only(sessionmaker, tg):
    await community(sessionmaker)
    await send(tg, "/remove 2", ADMIN_ID, "private", ADMIN_ID)
    await press(tg, last_buttons(tg[2], ADMIN_ID)[1])
    assert "Отменено" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 2)).excluded_at is None
    await send(tg, "/remove", ADMIN_ID, "private", ADMIN_ID)
    assert "Исключить из сообщества" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/remove Нурлан", ADMIN_ID, "private", ADMIN_ID)
    assert "Не нашла" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, f"/remove {ADMIN_ID}", ADMIN_ID, "private", ADMIN_ID)
    assert "нельзя" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/remove 3", 2, "private", 2)
    assert "только для админа" in tg[2].sent(2)[-1]
    # чужое нажатие на кнопку подтверждения не работает
    await send(tg, "/remove 3", ADMIN_ID, "private", ADMIN_ID)
    yes, _ = last_buttons(tg[2], ADMIN_ID)
    await press(tg, yes, user_id=2)
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 3)).excluded_at is None
