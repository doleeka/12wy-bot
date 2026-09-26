"""Scorecard: % = выполненные тактики / запланированные × 100."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Checkin, User, WeeklyTactic

CYCLE_WEEKS = 12
EXCELLENT, GOOD = 85, 70


def week_start(day: date) -> date:
    """Понедельник недели, в которую входит day."""
    return day - timedelta(days=day.weekday())


def week_number(cycle_start: date | None, day: date) -> int | None:
    """Номер недели цикла (1..12) или None, если цикл ещё не начался / уже закончился."""
    if cycle_start is None or day < cycle_start:
        return None
    n = (day - cycle_start).days // 7 + 1
    return n if n <= CYCLE_WEEKS else None


def percent(done: int, planned: int) -> int | None:
    return round(done / planned * 100) if planned else None


def rating(value: int) -> str:
    if value >= EXCELLENT:
        return "отлично"
    if value >= GOOD:
        return "хорошо, есть что подтянуть"
    return "сбой в исполнении"


def rating_emoji(value: int | None) -> str:
    if value is None:
        return "▫️"
    if value >= EXCELLENT:
        return "🟢"
    if value >= GOOD:
        return "🟡"
    return "🔴"


async def week_percent(session: AsyncSession, user: User, week: date) -> int | None:
    """% за неделю или None, если чек-ин за эту неделю ещё не заполнен целиком."""
    done, marked = (
        await session.execute(
            select(func.coalesce(func.sum(cast(Checkin.done, Integer)), 0), func.count(Checkin.id)).where(
                Checkin.user_id == user.id, Checkin.week_start == week
            )
        )
    ).one()
    planned = await session.scalar(
        select(func.count(WeeklyTactic.id)).where(WeeklyTactic.user_id == user.id, WeeklyTactic.is_active)
    )
    if not marked or marked < planned:
        return None
    return percent(done, marked)
