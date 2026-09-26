"""Вкладка «Сегодня»: действия дня, отметки «сделано сегодня», прогресс текущей недели."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Checkin, DailyMark, User, WeeklyTactic


def is_daily(tactic: WeeklyTactic) -> bool:
    """Еженедельное действие с днями недели — отмечается по дням."""
    return tactic.weeks is None and bool(tactic.days)


def scheduled_on(tactics: list[WeeklyTactic], day: date) -> list[WeeklyTactic]:
    return [t for t in tactics if is_daily(t) and day.weekday() in t.days]


async def daily_marks(session: AsyncSession, user: User, week: date) -> dict[int, set[date]]:
    rows = await session.scalars(
        select(DailyMark).where(DailyMark.user_id == user.id, DailyMark.day >= week, DailyMark.day < week + timedelta(days=7))
    )
    marks: dict[int, set[date]] = {}
    for row in rows:
        marks.setdefault(row.tactic_id, set()).add(row.day)
    return marks


def suggested_done(tactic: WeeklyTactic, week: date, daily: dict[int, set[date]]) -> bool:
    """Все запланированные дни недели отмечены «сделано» — значит, действие выполнено за неделю."""
    if not is_daily(tactic):
        return False
    planned = {week + timedelta(days=d) for d in tactic.days}
    return planned <= daily.get(tactic.id, set())


def week_state(tactic: WeeklyTactic, week: date, checkin: dict[int, bool], daily: dict[int, set[date]]) -> bool | None:
    """Выполнено ли за неделю: отметка чек-ина важнее; иначе — по дневным отметкам."""
    if tactic.id in checkin:
        return checkin[tactic.id]
    if is_daily(tactic):
        return suggested_done(tactic, week, daily)
    return None


async def set_daily(session: AsyncSession, user: User, tactic: WeeklyTactic, day: date, done: bool) -> None:
    existing = await session.scalar(select(DailyMark).where(DailyMark.tactic_id == tactic.id, DailyMark.day == day))
    if done and existing is None:
        session.add(DailyMark(user_id=user.id, tactic_id=tactic.id, day=day))
    elif not done and existing is not None:
        await session.delete(existing)
    await session.flush()


async def clear_week_mark(session: AsyncSession, user: User, tactic_id: int, week: date) -> None:
    await session.execute(
        delete(Checkin).where(Checkin.user_id == user.id, Checkin.tactic_id == tactic_id, Checkin.week_start == week)
    )
