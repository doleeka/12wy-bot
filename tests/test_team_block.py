"""Блок «Моя команда» в приложении и организатор вне команд (/noteam)."""
from datetime import date

import pytest

from bot.config import Settings
from bot.main import build_dispatcher
from bot.services import checkins, scorecard, teams
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

WEEK = scorecard.week_start(date.today())


async def mark_all(sessionmaker, tg_id, done_flags):
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, tg_id)
        for t, d in zip(await checkins.tactics_for_week(session, u, WEEK), done_flags):
            await checkins.set_mark(session, u, t.id, WEEK, d)
        await session.commit()


async def test_team_block_shows_only_percents(api, sessionmaker):  # noqa: F811
    for tg in (42, 43, 44):
        await make_user(sessionmaker, tg)
    await mark_all(sessionmaker, 43, [True, True, True, False])  # 75%
    d = (await api.get("/api/team", headers=auth())).json()
    assert d["team"] == "Команда №1" and not d["organizer"]
    rows = {m["name"]: m for m in d["members"]}
    me = next(m for m in d["members"] if m["you"])
    assert me["current"] is None and len(d["members"]) == 3  # ещё не отметила
    assert rows["U43"]["current"] == 75 and rows["U43"]["level"] == "warning"
    assert set(rows["U43"]) == {"name", "you", "current", "level", "prev"}  # ни целей, ни действий


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def test_organizer_leaves_team_and_is_not_reassigned(sessionmaker, tg, api):  # noqa: F811
    await make_user(sessionmaker, ADMIN_ID)
    await make_user(sessionmaker, 43)
    sent_before = len(tg[2].sent(43))
    await send(tg, "/noteam", ADMIN_ID, "private", ADMIN_ID)
    assert "вне команд" in tg[2].sent(ADMIN_ID)[-1] and "Команда №1" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert a.no_team and await teams.get_team_of(session, a) is None
        assert [m.telegram_id for m in await teams.team_members(session, 1)] == [43]  # подруга осталась
    assert len(tg[2].sent(43)) == sent_before  # никаких рассылок
    # /team не распределяет заново
    await send(tg, "/team", ADMIN_ID, "private", ADMIN_ID)
    assert "режим организатора" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert await teams.get_team_of(session, a) is None
        assert await teams.assign_to_team(session, a) == (None, False)
    # в приложении блока команды нет
    d = (await api.get("/api/team", headers=auth(user_id=ADMIN_ID))).json()
    assert d == {"team": None, "organizer": True, "members": []}
    # вернуть
    await send(tg, "/noteam off", ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/team", ADMIN_ID, "private", ADMIN_ID)
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert not a.no_team and await teams.get_team_of(session, a) is not None


async def test_noteam_admin_only(sessionmaker, tg):
    await make_user(sessionmaker, 5)
    await send(tg, "/noteam", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 5)
        assert not u.no_team and await teams.get_team_of(session, u) is not None
