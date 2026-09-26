"""Еженедельный чек-ин: отметки по тактикам за неделю."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models import Checkin, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import scorecard


async def active_tactics(session: AsyncSession, user: User) -> list[WeeklyTactic]:
    """Активные тактики текущего цикла в порядке приоритетов."""
    rows = await session.scalars(
        select(WeeklyTactic)
        .join(Priority)
        .where(WeeklyTactic.user_id == user.id, WeeklyTactic.is_active, Priority.cycle == user.cycle)
        .options(selectinload(WeeklyTactic.priority))
        .order_by(Priority.position, WeeklyTactic.id)
    )
    return list(rows)


async def get_marks(session: AsyncSession, user: User, week: date) -> dict[int, bool]:
    rows = await session.scalars(select(Checkin).where(Checkin.user_id == user.id, Checkin.week_start == week))
    return {row.tactic_id: row.done for row in rows}


def checkin_week_number(user: User, week: date) -> int | None:
    """Номер недели цикла для недели, начинающейся с week (понедельник)."""
    return scorecard.week_number(user.cycle_start, week)


def can_check_in(user: User, week: date, today: date) -> bool:
    return (
        user.onboarding_step == OnboardingStep.DONE
        and checkin_week_number(user, week) is not None
        and week <= scorecard.week_start(today)
    )


async def set_mark(session: AsyncSession, user: User, tactic_id: int, week: date, done: bool) -> bool:
    """Отмечает тактику за неделю. False — если тактика не её или неактивна."""
    tactic = await session.get(WeeklyTactic, tactic_id)
    if tactic is None or tactic.user_id != user.id or not tactic.is_active:
        return False
    row = await session.scalar(select(Checkin).where(Checkin.tactic_id == tactic_id, Checkin.week_start == week))
    if row is None:
        session.add(
            Checkin(
                user_id=user.id,
                tactic_id=tactic_id,
                week_start=week,
                week_number=checkin_week_number(user, week),
                done=done,
            )
        )
    else:
        row.done = done
    await session.flush()
    return True


def summarize(tactics: list[WeeklyTactic], marks: dict[int, bool]) -> tuple[int, int, int]:
    """(выполнено, запланировано, не отмечено)."""
    planned = len(tactics)
    done = sum(1 for t in tactics if marks.get(t.id) is True)
    unmarked = sum(1 for t in tactics if t.id not in marks)
    return done, planned, unmarked


async def users_for_checkin(session: AsyncSession, week: date) -> list[User]:
    """Готовые участницы, у которых неделя week входит в цикл и отчёт за неё ещё не отправлен."""
    rows = await session.scalars(select(User).where(User.onboarding_step == OnboardingStep.DONE).order_by(User.id))
    return [
        u
        for u in rows
        if checkin_week_number(u, week) is not None and (u.last_reported_week is None or u.last_reported_week < week)
    ]
