from datetime import date

from sqlalchemy import func, select

from bot.config import Settings
from bot.handlers.start import cmd_start
from bot.models import Checkin, EssentialIntent, OnboardingStep, WeeklyTactic
from bot.services import checkins, scorecard, teams
from tests.helpers import make_message
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send, tg  # noqa: F401 — фикстура tg


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def test_resetme_wipes_cycle_but_keeps_team(sessionmaker, tg):  # noqa: F811
    await make_user(sessionmaker, ADMIN_ID)
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        tactic = (await checkins.active_tactics(session, user))[0]
        await checkins.set_mark(session, user, tactic.id, scorecard.week_start(date.today()), True)
        await session.commit()

    await send(tg, "/resetme", ADMIN_ID, "private", ADMIN_ID)
    assert "сброшен" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert user.onboarding_step == OnboardingStep.WHEEL and user.cycle_start is None
        assert await count(session, WeeklyTactic) == 0 and await count(session, Checkin) == 0
        assert await count(session, EssentialIntent) == 0
        assert await teams.get_team_of(session, user) is not None  # команда осталась


async def test_resetme_admin_only(sessionmaker, tg):  # noqa: F811
    await make_user(sessionmaker, 5)
    await send(tg, "/resetme", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 5)).onboarding_step == OnboardingStep.DONE


async def test_start_shows_app_button_on_any_step(sessionmaker):
    await make_user(sessionmaker, 42)  # онбординг пройден
    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session, settings=Settings(bot_token="x", database_path=None, webapp_url="https://a.b"))
    last = msg.answer.call_args
    assert "Приложение" in last.args[0]
    assert last.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == "https://a.b"
