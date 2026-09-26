from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.handlers.checkin import cmd_checkin
from bot.handlers.cycle import cmd_newcycle, on_cycle_callback
from bot.handlers.onboarding import cmd_plan, on_eliminate_callback, on_explore_callback, on_tactic_callback, on_text
from bot.handlers.start import cmd_start
from bot.handlers.teams import cmd_team
from bot.handlers.wheel import on_wheel_callback
from bot.models import Checkin, OnboardingStep, User, WeeklyTactic, WheelOfBalance
from bot.scheduler import send_week_planning
from bot.services import checkins, cycle, scorecard, teams, wheel
from tests.helpers import all_texts, make_bot, make_state
from tests.test_checkin import make_user, settings

THIS_WEEK = scorecard.week_start(date.today())
FINISHED_START = THIS_WEEK - timedelta(days=84)  # сегодня — 13-я неделя


def tg(tg_id):
    return SimpleNamespace(id=tg_id, username=None, first_name=f"U{tg_id}")


def message(tg_id, text=None):
    return SimpleNamespace(
        from_user=tg(tg_id), text=text, answer=AsyncMock(), edit_text=AsyncMock(), edit_reply_markup=AsyncMock()
    )


async def add_checkins(sessionmaker, tg_id, done_by_week: dict[int, int]):
    """done_by_week: номер недели (1..12) → сколько из 4 тактик выполнено."""
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, tg_id)
        tactics = await checkins.active_tactics(session, user)
        for n, done in done_by_week.items():
            week = user.cycle_start + timedelta(days=7 * (n - 1))
            for i, t in enumerate(tactics):
                session.add(Checkin(user_id=user.id, tactic_id=t.id, week_start=week, week_number=n, done=i < done))
        await session.commit()


async def add_wheel(sessionmaker, tg_id, cycle_n, scores):
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, tg_id)
        session.add_all(WheelOfBalance(user_id=user.id, cycle=cycle_n, sphere=k, score=v) for k, v in scores.items())
        await session.commit()


# ---------- сервис ----------

def test_cycle_end_and_finish():
    user = User(onboarding_step=OnboardingStep.DONE, cycle_start=date(2026, 6, 1))
    assert cycle.cycle_end(user) == date(2026, 8, 24)  # понедельник 13-й недели
    assert not cycle.maybe_finish_cycle(user, date(2026, 8, 23))  # воскресенье 12-й недели
    assert cycle.maybe_finish_cycle(user, date(2026, 8, 24))
    assert user.onboarding_step == OnboardingStep.FINISHED
    assert not cycle.maybe_finish_cycle(user, date(2026, 8, 25))  # уже завершён


async def test_cycle_stats(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    await add_checkins(sessionmaker, 1, {1: 4, 2: 3, 3: 2, 5: 4, 12: 4})
    async with sessionmaker() as session:
        stats = await cycle.cycle_stats(session, await teams.get_user_by_telegram_id(session, 1))
    assert stats.weeks == [100, 75, 50, None, 100] + [None] * 6 + [100]
    assert stats.average == 85 and stats.excellent_weeks == 3 and stats.best == (12, 100)


async def test_new_cycle_resets_but_keeps_team(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 1)
        team_before = (await teams.get_team_of(session, user)).id
        await cycle.start_new_cycle(session, user)
        await session.commit()
        assert user.cycle == 2 and user.onboarding_step == OnboardingStep.WHEEL and user.cycle_start is None
        assert await checkins.active_tactics(session, user) == []
        assert all(not t.is_active for t in await session.scalars(select(WeeklyTactic)))
        assert (await teams.get_team_of(session, user)).id == team_before


# ---------- сценарий ----------

async def test_summary_pause_and_newcycle(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    await add_checkins(sessionmaker, 1, {n: 4 for n in range(1, 12)})  # 12-я неделя не отмечена

    msg = message(1)
    async with sessionmaker() as session:
        await cmd_start(msg, session)
        await session.commit()
    text = msg.answer.call_args.args[0]
    assert "12 недель позади" in text and "13-я неделя" in text and "Недель на 85%+: <b>11 из 12</b>" in text
    assert "🟢" * 11 + "▫️" in text and "Приоритет 1 &lt;x&gt;" in text
    assert "12-ю неделю" in text and "/checkin" in text  # подсказка: 12-ю ещё можно отметить
    assert msg.answer.call_args.kwargs["reply_markup"] is not None

    # 12-я неделя ещё открыта для отметки, хоть цикл и завершён
    msg = message(1)
    async with sessionmaker() as session:
        await cmd_checkin(msg, session)
    assert "неделя 12 из 12" in msg.answer.call_args.args[0]

    cb = SimpleNamespace(from_user=tg(1), data="cycle:pause", message=message(1), answer=AsyncMock())
    async with sessionmaker() as session:
        await on_cycle_callback(cb, session)
        await session.commit()
    assert "/newcycle" in cb.message.answer.call_args.args[0]

    msg = message(1)
    async with sessionmaker() as session:
        await cmd_newcycle(msg, session)
        await session.commit()
    assert "Новый цикл" in msg.answer.call_args.args[0]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 1)).cycle == 2


async def test_newcycle_mid_cycle_refused(sessionmaker):
    await make_user(sessionmaker, 1)
    msg = message(1)
    async with sessionmaker() as session:
        await cmd_newcycle(msg, session)
    assert "неделя 1 из 12" in msg.answer.call_args.args[0]


async def test_full_second_cycle(sessionmaker):
    """Итоги → новый цикл → колесо со сравнением → Explore с прошлыми приоритетами → тактики → та же команда."""
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    await make_user(sessionmaker, 2)  # сокомандница
    await add_wheel(sessionmaker, 1, 1, {s.key: 5 for s in wheel.CORE_SPHERES})

    bot, state = make_bot(), make_state()
    msg = message(1)
    async with sessionmaker() as session:
        await cmd_start(msg, session)
        await session.commit()

    async def press(handler, data, **kw):
        cb = SimpleNamespace(from_user=tg(1), data=data, message=msg, answer=AsyncMock())
        async with sessionmaker() as session:
            await handler(cb, session, **kw)
            await session.commit()
        return cb

    async def say(text):
        nonlocal msg
        msg = message(1, text)
        async with sessionmaker() as session:
            await on_text(msg, session, state, bot)
            await session.commit()

    await press(on_cycle_callback, "cycle:new")
    assert "колеса баланса" in msg.answer.call_args.args[0]

    scores = {"career": 7, "health": 3, "relationships": 5, "finance": 6, "growth": 8, "rest": 5}
    for key, v in scores.items():
        await press(on_wheel_callback, f"wheel:rate:{key}:{v}")
    await press(on_wheel_callback, "wheel:finish")
    result = msg.edit_text.call_args.args[0]
    assert "Как изменилось за 12 недель" in result
    assert "Карьера: 5 → 7 (+2) 🌱" in result and "Здоровье: 5 → 3 (-2)" in result and "Отдых: 5 → 5 (=)" in result
    assert "Прошлые приоритеты" in all_texts(msg.answer) and "Приоритет 1 &lt;x&gt;" in all_texts(msg.answer)

    await say("Бег\nЯзык\nСон")
    await press(on_explore_callback, "explore:done")
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 1)
        from bot.services import onboarding as svc

        items = await svc.get_explore_items(session, user)
        assert [i.text for i in items] == ["Бег", "Язык", "Сон"]  # список нового цикла — с чистого листа
    for item in items:
        await press(on_eliminate_callback, f"elim:t:{item.id}")
    await press(on_eliminate_callback, "elim:ok")
    for _ in range(3):
        await say("Потому что это делает мою жизнь лучше")
    for pos in (1, 2, 3):
        await say("3 раза в неделю")
        await press(on_tactic_callback, f"tac:next:{pos}", state=state, bot=bot)

    assert "команда остаётся прежней" in all_texts(msg.answer)
    assert bot.send_message.call_count == 0  # сокомандницам не пишем «новая участница»
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 1)
        assert user.onboarding_step == OnboardingStep.DONE and user.cycle == 2 and user.cycle_start is not None
        assert len(await checkins.active_tactics(session, user)) == 3
        assert [len(m) for _, m in await teams.list_teams(session)] == [2]

    plan = message(1)
    async with sessionmaker() as session:
        await cmd_plan(plan, session)
    assert "Бег" in plan.answer.call_args.args[0] and "&lt;x&gt;" not in plan.answer.call_args.args[0]


async def test_team_view_during_new_cycle_planning(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    async with sessionmaker() as session:
        await cycle.start_new_cycle(session, await teams.get_user_by_telegram_id(session, 1))
        await session.commit()
    msg = message(1)
    async with sessionmaker() as session:
        await cmd_team(msg, session, make_bot())
    assert "планирование нового цикла" in msg.answer.call_args.args[0]


async def test_monday_of_week_13_sends_summary_instead_of_plan(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=FINISHED_START)
    await make_user(sessionmaker, 2)  # обычная неделя 1
    bot = make_bot()
    assert await send_week_planning(bot, sessionmaker, settings()) == 2
    by_user = {c.args[0]: c for c in bot.send_message.call_args_list}
    assert "12 недель позади" in by_user[1].args[1] and by_user[1].kwargs["reply_markup"] is not None
    assert "Неделя 1 из 12 началась" in by_user[2].args[1]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 1)).onboarding_step == OnboardingStep.FINISHED
    # на следующий понедельник итоги повторно не приходят
    assert await send_week_planning(make_bot(), sessionmaker, settings()) == 1
