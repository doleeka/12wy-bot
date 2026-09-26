"""Чек-ин, процент и итоги цикла считают только тактики, запланированные на эту неделю."""
from datetime import date, timedelta

from bot.handlers.checkin import build_checkin
from bot.models import Checkin, OnboardingStep, Priority, User, WeeklyTactic
from bot.scheduler import send_week_planning
from bot.services import checkins, cycle, scorecard
from tests.helpers import make_bot
from tests.test_checkin import settings

THIS_WEEK = scorecard.week_start(date.today())


async def make_planned_user(sessionmaker, cycle_start):
    """Каждую неделю — «Бег»; недели 1, 5, 12 — «Забег»; неделя 2 — «Тест»."""
    async with sessionmaker() as session:
        user = User(telegram_id=1, first_name="U", onboarding_step=OnboardingStep.DONE, is_ready=True, cycle_start=cycle_start)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add_all([
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Бег", weeks=None),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Забег", weeks=[1, 5, 12]),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Тест", weeks=[2]),
        ])
        await session.commit()
        return user.id


async def test_checkin_shows_only_this_weeks_tactics(sessionmaker):
    uid = await make_planned_user(sessionmaker, THIS_WEEK)  # сегодня — неделя 1
    async with sessionmaker() as session:
        user = await session.get(User, uid)
        week1 = [t.text for t in await checkins.tactics_for_week(session, user, THIS_WEEK)]
        week2 = [t.text for t in await checkins.tactics_for_week(session, user, THIS_WEEK + timedelta(days=7))]
        text, markup = await build_checkin(session, user, THIS_WEEK)
    assert week1 == ["Бег", "Забег"] and week2 == ["Бег", "Тест"]
    assert "Бег" in text and "Забег" in text and "Тест" not in text
    assert len([b for row in markup.inline_keyboard for b in row]) == 2 * 2 + 1


async def test_percent_uses_planned_for_that_week(sessionmaker):
    uid = await make_planned_user(sessionmaker, THIS_WEEK)
    async with sessionmaker() as session:
        user = await session.get(User, uid)
        bег, забег = await checkins.tactics_for_week(session, user, THIS_WEEK)
        session.add(Checkin(user_id=uid, tactic_id=bег.id, week_start=THIS_WEEK, week_number=1, done=True))
        await session.commit()
        assert await scorecard.week_percent(session, user, THIS_WEEK) is None  # «Забег» ещё не отмечен
        session.add(Checkin(user_id=uid, tactic_id=забег.id, week_start=THIS_WEEK, week_number=1, done=False))
        await session.commit()
        # 1 из 2 запланированных на неделю 1 (а не из 3 тактик вообще)
        assert await scorecard.week_percent(session, user, THIS_WEEK) == 50


async def test_cycle_stats_per_week_planned(sessionmaker):
    start = THIS_WEEK - timedelta(days=84)
    uid = await make_planned_user(sessionmaker, start)
    async with sessionmaker() as session:
        user = await session.get(User, uid)
        tactics = {t.text: t for t in await checkins.active_tactics(session, user)}
        week2 = start + timedelta(days=7)
        # неделя 2: запланированы «Бег» и «Тест», выполнено оба → 100%
        for name in ("Бег", "Тест"):
            session.add(Checkin(user_id=uid, tactic_id=tactics[name].id, week_start=week2, week_number=2, done=True))
        # неделя 3: запланирован только «Бег», не выполнен → 0%
        session.add(Checkin(user_id=uid, tactic_id=tactics["Бег"].id, week_start=start + timedelta(days=14), week_number=3, done=False))
        await session.commit()
        stats = await cycle.cycle_stats(session, user)
    assert stats.weeks[1] == 100 and stats.weeks[2] == 0


async def test_week_without_planned_tactics_is_buffer(sessionmaker):
    async with sessionmaker() as session:
        user = User(telegram_id=2, onboarding_step=OnboardingStep.DONE, cycle_start=THIS_WEEK)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="X")
        session.add(p)
        await session.flush()
        session.add(WeeklyTactic(priority_id=p.id, user_id=user.id, text="Только 12-я", weeks=[12]))
        await session.commit()
        text, markup = await build_checkin(session, user, THIS_WEEK)
    assert markup is None and "буфер" in text
    # в понедельник напоминание не шлём — на эту неделю ничего не запланировано
    assert await send_week_planning(make_bot(), sessionmaker, settings()) == 0


async def test_planning_message_shows_weekdays(sessionmaker):
    async with sessionmaker() as session:
        user = User(telegram_id=3, onboarding_step=OnboardingStep.DONE, cycle_start=THIS_WEEK)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add_all([
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Тренировка", days=[0, 2, 4]),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Старая тактика из чата"),  # без дней
        ])
        await session.commit()
    bot = make_bot()
    await send_week_planning(bot, sessionmaker, settings())
    text = bot.send_message.call_args.args[1]
    assert "• Тренировка — пн, ср, пт" in text and "• Старая тактика из чата\n" in text + "\n"
