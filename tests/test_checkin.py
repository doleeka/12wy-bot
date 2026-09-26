from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import CommandObject

from bot.config import Settings
from bot.handlers.checkin import cmd_checkin, cmd_report, default_checkin_week, on_checkin_callback
from bot.models import OnboardingStep, Priority, ReportTarget, User, WeeklyTactic
from bot.scheduler import send_week_planning, send_weekly_checkins, setup_scheduler
from bot.services import checkins, scorecard, teams
from tests.helpers import make_bot

ADMIN_ID = 999
MONDAY = date(2026, 9, 28)


def settings():
    return Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])


def tg(tg_id):
    return SimpleNamespace(id=tg_id, username=None, first_name=f"U{tg_id}")


def message(tg_id):
    return SimpleNamespace(from_user=tg(tg_id), answer=AsyncMock(), edit_text=AsyncMock())


async def make_user(sessionmaker, tg_id, n_tactics=4, cycle_start=None, report=ReportTarget.TEAM):
    async with sessionmaker() as session:
        user = User(
            telegram_id=tg_id,
            first_name=f"U{tg_id}",
            onboarding_step=OnboardingStep.DONE,
            is_ready=True,
            cycle_start=cycle_start or scorecard.week_start(date.today()),
            send_report=report,
        )
        session.add(user)
        await session.flush()
        for pos in (1, 2):
            p = Priority(user_id=user.id, position=pos, title=f"Приоритет {pos} <x>")
            session.add(p)
            await session.flush()
            for i in range(n_tactics // 2):
                session.add(WeeklyTactic(priority_id=p.id, user_id=user.id, text=f"тактика {pos}.{i}"))
        await session.commit()
        await teams.assign_to_team(session, user)
        return user


class Flow:
    def __init__(self, sessionmaker, tg_id):
        self.sm, self.tg_id, self.bot = sessionmaker, tg_id, make_bot()
        self.msg = message(tg_id)

    async def open(self):
        async with self.sm() as session:
            await cmd_checkin(self.msg, session)
            await session.commit()
        return self.msg.answer.call_args

    async def press(self, data):
        cb = SimpleNamespace(from_user=tg(self.tg_id), data=data, message=self.msg, answer=AsyncMock())
        async with self.sm() as session:
            await on_checkin_callback(cb, session, self.bot, settings=settings())
            await session.commit()
        return cb


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_full_checkin_and_team_report(sessionmaker):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2)  # сокомандница
    flow = Flow(sessionmaker, 1)
    call = await flow.open()
    text, markup = call.args[0], call.kwargs["reply_markup"]
    assert "неделя 1 из 12" in text and "Приоритет 1 &lt;x&gt;" in text and text.count("▫️") == 4
    cbs = buttons(markup)
    assert len(cbs) == 9 and cbs[-1].startswith("ci:done:")

    # нельзя посчитать, пока не всё отмечено
    cb = await flow.press(cbs[-1])
    assert "осталось 4" in cb.answer.call_args.args[0]

    marks = [cbs[0], cbs[2], cbs[4], cbs[7]]  # ✅ ✅ ✅ ❌
    for data in marks[:2]:
        await flow.press(data)
    async with sessionmaker() as session:  # пока отмечено не всё — сокомандницы процент не видят
        user = await teams.get_user_by_telegram_id(session, 1)
        assert await scorecard.week_percent(session, user, scorecard.week_start(date.today())) is None
    for data in marks[2:]:
        await flow.press(data)
    assert flow.msg.edit_text.call_args.args[0].count("— ✅") == 3
    await flow.press(cbs[-1])
    result = flow.msg.edit_text.call_args.args[0]
    assert "75%" in result and "хорошо" in result and "Выполнено 3 из 4" in result
    report = flow.bot.send_message.call_args_list
    assert [c.args[0] for c in report] == [2]
    assert "U1" in report[0].args[1] and "75%" in report[0].args[1] and "тактика" not in report[0].args[1]

    # исправила отметку и пересчитала — результат новый, повторного отчёта нет
    await flow.press(cbs[6])
    await flow.press(cbs[-1])
    assert "100%" in flow.msg.edit_text.call_args.args[0]
    assert flow.bot.send_message.call_count == 1

    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 1)
        assert await scorecard.week_percent(session, user, scorecard.week_start(date.today())) == 100


async def test_low_score_advice_and_admin_report(sessionmaker):
    await make_user(sessionmaker, 1, n_tactics=2, report=ReportTarget.ADMIN)
    flow = Flow(sessionmaker, 1)
    cbs = buttons((await flow.open()).kwargs["reply_markup"])
    await flow.press(cbs[0])  # ✅
    await flow.press(cbs[3])  # ❌
    await flow.press(cbs[-1])
    result = flow.msg.edit_text.call_args.args[0]
    assert "50%" in result and "сбой" in result and "Что помешало" in result and "буфер" in result
    assert [c.args[0] for c in flow.bot.send_message.call_args_list] == [ADMIN_ID]


async def test_report_none(sessionmaker):
    await make_user(sessionmaker, 1, n_tactics=2, report=ReportTarget.NONE)
    await make_user(sessionmaker, 2)
    flow = Flow(sessionmaker, 1)
    cbs = buttons((await flow.open()).kwargs["reply_markup"])
    await flow.press(cbs[0])
    await flow.press(cbs[2])
    await flow.press(cbs[-1])
    assert flow.bot.send_message.call_count == 0


async def test_cannot_mark_foreign_or_future(sessionmaker):
    await make_user(sessionmaker, 1)
    other = await make_user(sessionmaker, 2)
    flow = Flow(sessionmaker, 1)
    week = scorecard.week_start(date.today())
    async with sessionmaker() as session:
        foreign = (await checkins.active_tactics(session, await session.get(User, other.id)))[0]
    await flow.press(f"ci:m:{foreign.id}:{week.toordinal()}:1")
    async with sessionmaker() as session:
        assert await checkins.get_marks(session, await session.get(User, other.id), week) == {}

    future = week + timedelta(days=7)
    cb = await flow.press(f"ci:m:{foreign.id}:{future.toordinal()}:1")
    assert cb.answer.call_args.kwargs.get("show_alert")
    cb = await flow.press("ci:m:garbage")
    assert cb.answer.called


async def test_checkin_before_cycle_and_before_onboarding(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=scorecard.week_start(date.today()) + timedelta(days=7))
    flow = Flow(sessionmaker, 1)
    assert "начинаются в понедельник" in (await flow.open()).args[0]

    async with sessionmaker() as session:
        session.add(User(telegram_id=5))
        await session.commit()
    assert "после онбординга" in (await Flow(sessionmaker, 5).open()).args[0]


def test_default_week_on_monday_is_previous_if_not_reported():
    user = User(cycle_start=MONDAY - timedelta(days=7), onboarding_step=OnboardingStep.DONE)
    assert default_checkin_week(user, MONDAY) == MONDAY - timedelta(days=7)
    user.last_reported_week = MONDAY - timedelta(days=7)
    assert default_checkin_week(user, MONDAY) == MONDAY
    assert default_checkin_week(user, MONDAY + timedelta(days=3)) == MONDAY
    fresh = User(cycle_start=MONDAY, onboarding_step=OnboardingStep.DONE)  # первая неделя ещё не прошла
    assert default_checkin_week(fresh, MONDAY) == MONDAY


async def test_scheduled_jobs(sessionmaker):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2, cycle_start=scorecard.week_start(date.today()) + timedelta(days=7))
    bot = make_bot()
    assert await send_weekly_checkins(bot, sessionmaker, settings()) == 1
    assert bot.send_message.call_args.args[0] == 1
    assert bot.send_message.call_args.kwargs["reply_markup"] is not None

    bot = make_bot()
    assert await send_week_planning(bot, sessionmaker, settings()) == 1
    text = bot.send_message.call_args.args[1]
    assert "Неделя 1 из 12" in text and "буфер" in text and "однозначное" in text

    # после отчёта за неделю воскресный чек-ин повторно не приходит
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        u.last_reported_week = scorecard.week_start(date.today())
        await session.commit()
    assert await send_weekly_checkins(make_bot(), sessionmaker, settings()) == 0


def test_scheduler_jobs_configured():
    scheduler = setup_scheduler(make_bot(), None, settings())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"weekly_checkin", "week_planning", "nightly_backup"}
    assert "hour='3'" in str(jobs["nightly_backup"].trigger)
    assert "day_of_week='sun'" in str(jobs["weekly_checkin"].trigger)
    assert "hour='18'" in str(jobs["weekly_checkin"].trigger)


async def test_report_admin_command(sessionmaker):
    await make_user(sessionmaker, 1)

    async def run(args):
        msg = message(ADMIN_ID)
        async with sessionmaker() as session:
            await cmd_report(msg, CommandObject(command="report", args=args), session)
            await session.commit()
        return msg.answer.call_args.args[0]

    assert "Использование" in await run("")
    assert "Использование" in await run("1 everyone")
    assert "не найдена" in await run("77 team")
    assert "в команду" in await run("1")
    assert "админу" in await run("1 admin")
    assert "админу" in await run("1")
    assert "никому" in await run("1 NONE")
