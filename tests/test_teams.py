import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from bot.config import Settings
from bot.filters import IsAdmin
from bot.handlers.teams import cmd_moveteam, cmd_team, cmd_teams, join_team
from bot.models import Checkin, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import scorecard, teams
from tests.helpers import make_bot

ADMIN_ID = 999


def settings():
    return Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])


async def make_users(sessionmaker, n, start=1):
    async with sessionmaker() as session:
        users = [
            User(telegram_id=start + i, first_name=f"U{start + i}", onboarding_step=OnboardingStep.DONE, is_ready=True)
            for i in range(n)
        ]
        session.add_all(users)
        await session.commit()
        return users


async def assign(sessionmaker, user):
    async with sessionmaker() as session:
        user = await session.get(User, user.id)
        team, created = await teams.assign_to_team(session, user)
        return team.id, created


async def team_sizes(sessionmaker):
    async with sessionmaker() as session:
        return [len(m) for _, m in await teams.list_teams(session)]


# ---------- сервис ----------

async def test_continuous_filling(sessionmaker):
    users = await make_users(sessionmaker, 7)
    results = [await assign(sessionmaker, u) for u in users]
    assert [r[0] for r in results] == [1, 1, 1, 2, 2, 2, 3]
    assert [r[1] for r in results] == [True, False, False, True, False, False, True]
    assert await assign(sessionmaker, users[0]) == (1, False)  # повторно — та же команда
    assert await team_sizes(sessionmaker) == [3, 3, 1]


async def test_free_slot_in_older_team_is_filled_first(sessionmaker):
    users = await make_users(sessionmaker, 6)
    for u in users:
        await assign(sessionmaker, u)
    async with sessionmaker() as session:
        await teams.move_to_team(session, await session.get(User, users[0].id), None)  # из команды 1 в новую 3
    newcomer = (await make_users(sessionmaker, 1, start=100))[0]
    assert (await assign(sessionmaker, newcomer))[0] == 1


async def test_concurrent_assignment_never_overfills(sessionmaker):
    users = await make_users(sessionmaker, 10)
    await asyncio.gather(*(assign(sessionmaker, u) for u in users))
    sizes = await team_sizes(sessionmaker)
    assert sum(sizes) == 10 and max(sizes) <= 3 and len(sizes) == 4


async def test_move_errors(sessionmaker):
    users = await make_users(sessionmaker, 4)
    for u in users:
        await assign(sessionmaker, u)
    async with sessionmaker() as session:
        u4 = await session.get(User, users[3].id)
        with pytest.raises(teams.TeamFull):
            await teams.move_to_team(session, u4, 1)
        with pytest.raises(teams.TeamNotFound):
            await teams.move_to_team(session, u4, 77)
        old, new = await teams.move_to_team(session, u4, 2)  # в свою же — без изменений
        assert old.id == new.id == 2


def test_week_helpers():
    start = date(2026, 9, 28)
    assert scorecard.week_start(date(2026, 10, 1)) == start
    assert scorecard.week_number(start, date(2026, 9, 27)) is None
    assert scorecard.week_number(start, start) == 1
    assert scorecard.week_number(start, start + timedelta(days=7 * 11 + 6)) == 12
    assert scorecard.week_number(start, start + timedelta(days=7 * 12)) is None
    assert scorecard.percent(3, 4) == 75 and scorecard.percent(0, 0) is None
    assert scorecard.rating(85) == "отлично" and scorecard.rating(70).startswith("хорошо")
    assert scorecard.rating(69) == "сбой в исполнении"


# ---------- хендлеры ----------

def message(tg_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=tg_id, username=None, first_name=f"U{tg_id}"), answer=AsyncMock())


async def test_join_team_notifies_mates_and_admin(sessionmaker):
    users = await make_users(sessionmaker, 2)
    await assign(sessionmaker, users[0])
    bot, msg = make_bot(), message(2)
    async with sessionmaker() as session:
        await join_team(msg, bot, session, await session.get(User, users[1].id), settings())
    assert "Команда №1" in msg.answer.call_args.args[0] and "U1" in msg.answer.call_args.args[0]
    recipients = [c.args[0] for c in bot.send_message.call_args_list]
    assert recipients == [1, ADMIN_ID]


async def test_team_view_shows_only_percent(sessionmaker):
    users = await make_users(sessionmaker, 2)
    week = scorecard.week_start(date.today())
    async with sessionmaker() as session:
        for u in users:
            u = await session.get(User, u.id)
            u.cycle_start = week
            p = Priority(user_id=u.id, position=1, title=f"СЕКРЕТНАЯ ЦЕЛЬ {u.id}")
            session.add(p)
            await session.flush()
            tactics = [WeeklyTactic(priority_id=p.id, user_id=u.id, text=f"секретная тактика {i}") for i in range(4)]
            session.add_all(tactics)
            await session.flush()
            if u.telegram_id == 1:  # U1 отметила 3 из 4, U2 — ничего
                for i, t in enumerate(tactics):
                    session.add(Checkin(user_id=u.id, tactic_id=t.id, week_start=week, week_number=1, done=i < 3))
        await session.commit()
    for u in users:
        await assign(sessionmaker, u)

    msg = message(2)
    async with sessionmaker() as session:
        await cmd_team(msg, session, make_bot(), settings=None)
    text = msg.answer.call_args.args[0]
    assert "Неделя 1 из 12" in text
    assert "🟡 U1 — 75%" in text and "U2 (ты) — ещё не отмечала" in text
    assert "СЕКРЕТН" not in text and "тактика" not in text


async def test_team_before_onboarding(sessionmaker):
    async with sessionmaker() as session:
        session.add(User(telegram_id=5))
        await session.commit()
    msg = message(5)
    async with sessionmaker() as session:
        await cmd_team(msg, session, make_bot())
    assert "онбординг" in msg.answer.call_args.args[0]


async def test_moveteam_command(sessionmaker):
    users = await make_users(sessionmaker, 4)
    for u in users:
        await assign(sessionmaker, u)

    async def run(args):
        msg, bot = message(ADMIN_ID), make_bot()
        async with sessionmaker() as session:
            await cmd_moveteam(msg, CommandObject(command="moveteam", args=args), session, bot)
        return msg.answer.call_args.args[0], bot

    assert "Использование" in (await run("abc"))[0]
    assert "не найдена" in (await run("555 1"))[0]
    assert "уже 3" in (await run("4 1"))[0]
    assert "нет" in (await run("4 9"))[0]

    text, bot = await run("1 2")  # U1: команда 1 → команда 2
    assert "U1 → Команда №2" in text
    recipients = [c.args[0] for c in bot.send_message.call_args_list]
    assert recipients == [1, 4, 2, 3]  # сама, новая сокомандница, старые сокомандницы
    assert await team_sizes(sessionmaker) == [2, 2]

    text, _ = await run("1 new")
    assert "Команда №3" in text

    msg = message(ADMIN_ID)
    async with sessionmaker() as session:
        await cmd_teams(msg, session)
    listing = msg.answer.call_args.args[0]
    assert "<b>Команда №3</b> (id 3, 1/3)" in listing and "<code>1</code>" in listing


async def test_admin_filter():
    f = IsAdmin()
    assert await f(message(ADMIN_ID), settings=settings())
    assert not await f(message(1), settings=settings())
    assert not await f(message(ADMIN_ID))
