from datetime import date, timedelta

from sqlalchemy import func, select

from bot.config import Settings
from bot.models import Checkin, OnboardingStep
from bot.services import checkins, scorecard, teams
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.fake_telegram import make_fake_bot
from bot.main import build_dispatcher

import pytest

THIS_WEEK = scorecard.week_start(date.today())
OCT5 = date(2026, 10, 5)


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID], cycle_start=OCT5)
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def user(sessionmaker, tg_id=ADMIN_ID):
    async with sessionmaker() as session:
        return await teams.get_user_by_telegram_id(session, tg_id)


async def test_testcycle_on_checkin_off(sessionmaker, tg):
    await make_user(sessionmaker, ADMIN_ID, cycle_start=OCT5)

    await send(tg, "/testcycle", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 1 из 12" in tg[2].sent(ADMIN_ID)[-1]
    assert (await user(sessionmaker)).cycle_start == THIS_WEEK

    # чек-ин недели 1 доступен как в настоящем цикле
    await send(tg, "/checkin", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 1 из 12" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        for t in await checkins.tactics_for_week(session, u, THIS_WEEK):
            await checkins.set_mark(session, u, t.id, THIS_WEEK, True)
        await session.commit()

    await send(tg, "/testcycle 3", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 3 из 12" in tg[2].sent(ADMIN_ID)[-1]
    assert (await user(sessionmaker)).cycle_start == THIS_WEEK - timedelta(days=14)

    await send(tg, "/testcycle off", ADMIN_ID, "private", ADMIN_ID)
    assert "05.10" in tg[2].sent(ADMIN_ID)[-1] and "удалено: 4" in tg[2].sent(ADMIN_ID)[-1]
    u = await user(sessionmaker)
    assert u.cycle_start == OCT5 and u.onboarding_step == OnboardingStep.DONE
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Checkin)) == 0


async def test_testcycle_guards(sessionmaker, tg):
    await make_user(sessionmaker, 5)
    await send(tg, "/testcycle", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]
    assert (await user(sessionmaker, 5)).cycle_start == THIS_WEEK  # не тронуто

    await send(tg, "/start", ADMIN_ID, "private", ADMIN_ID)  # админ ещё в онбординге
    await send(tg, "/testcycle", ADMIN_ID, "private", ADMIN_ID)
    assert "подтверди план" in tg[2].sent(ADMIN_ID)[-1]

    await make_user(sessionmaker, 77)
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        u.onboarding_step = OnboardingStep.DONE
        await session.commit()
    await send(tg, "/testcycle 13", ADMIN_ID, "private", ADMIN_ID)
    assert "Режим проверки" in tg[2].sent(ADMIN_ID)[-1]
