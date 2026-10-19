"""/testcycle и /resetme работают только с отдельной тестовой базой (/testapp): личное прохождение админа
и данные участниц не меняются."""
from datetime import date

import pytest
from sqlalchemy import func, select

from bot.config import Settings
from bot.db import create_engine, create_sessionmaker, init_db
from bot.main import build_dispatcher
from bot.models import Checkin, OnboardingStep
from bot.services import checkins, scorecard, teams
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send

THIS_WEEK = scorecard.week_start(date.today())


@pytest.fixture
async def sandbox(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'sandbox.db'}")
    await init_db(engine)
    yield create_sessionmaker(engine)
    await engine.dispose()


def dispatcher(sessionmaker, sandbox=None):
    bot, tg = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID], cycle_start=THIS_WEEK)
    if sandbox is not None:
        dp["sandbox_sessionmaker"] = sandbox
    return dp, bot, tg


@pytest.fixture
async def tg(sessionmaker, sandbox):
    t = dispatcher(sessionmaker, sandbox)
    yield t
    for router in list(t[0].sub_routers):
        router._parent_router = None
    t[0].sub_routers.clear()


async def real_snapshot(sessionmaker, tg_id=ADMIN_ID):
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, tg_id)
        n = await session.scalar(select(func.count()).select_from(Checkin))
        return u.cycle_start, u.onboarding_step, u.cycle, n


async def test_testcycle_moves_only_sandbox_week(sessionmaker, sandbox, tg):
    await make_user(sessionmaker, ADMIN_ID, cycle_start=THIS_WEEK)
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        t = (await checkins.active_tactics(session, u))[0]
        await checkins.set_mark(session, u, t.id, THIS_WEEK, True)
        await session.commit()
    before = await real_snapshot(sessionmaker)
    await make_user(sandbox, ADMIN_ID, cycle_start=THIS_WEEK)  # тестовый профиль — в своей базе

    await send(tg, "/testcycle 3", ADMIN_ID, "private", ADMIN_ID)
    assert "Тестовая неделя 3 из 12" in tg[2].sent(ADMIN_ID)[-1]
    async with sandbox() as session:
        assert (await teams.get_user_by_telegram_id(session, ADMIN_ID)).sandbox_week == 3
    assert await real_snapshot(sessionmaker) == before  # личное прохождение не тронуто


async def test_testcycle_guards(sessionmaker, sandbox, tg):
    await make_user(sessionmaker, 5)
    await send(tg, "/testcycle", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]

    await make_user(sandbox, ADMIN_ID)
    async with sandbox() as session:
        (await teams.get_user_by_telegram_id(session, ADMIN_ID)).onboarding_step = OnboardingStep.TACTICS
        await session.commit()
    await send(tg, "/testcycle", ADMIN_ID, "private", ADMIN_ID)  # тестовый профиль ещё не прошёл онбординг
    assert "подтверди план" in tg[2].sent(ADMIN_ID)[-1]

    async with sandbox() as session:
        (await teams.get_user_by_telegram_id(session, ADMIN_ID)).onboarding_step = OnboardingStep.DONE
        await session.commit()
    await send(tg, "/testcycle 13", ADMIN_ID, "private", ADMIN_ID)
    assert "от 1 до 12" in tg[2].sent(ADMIN_ID)[-1]


async def test_without_sandbox_nothing_changes(sessionmaker):
    dp, bot, tg = t = dispatcher(sessionmaker)
    try:
        await make_user(sessionmaker, ADMIN_ID, cycle_start=THIS_WEEK)
        before = await real_snapshot(sessionmaker)
        await send(t, "/testcycle 3", ADMIN_ID, "private", ADMIN_ID)
        assert "Тестовая база недоступна" in tg.sent(ADMIN_ID)[-1]
        await send(t, "/resetme", ADMIN_ID, "private", ADMIN_ID)
        assert "/testapp" in tg.sent(ADMIN_ID)[-1]
        assert await real_snapshot(sessionmaker) == before
    finally:
        for router in list(dp.sub_routers):
            router._parent_router = None
        dp.sub_routers.clear()
