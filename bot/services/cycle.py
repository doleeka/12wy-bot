"""Завершение 12-недельного цикла: итоги (13-я неделя) и старт нового цикла."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Checkin, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import checkins, scorecard, wheel

CYCLE_DAYS = scorecard.CYCLE_WEEKS * 7


def cycle_end(user: User) -> date | None:
    """Первый день после 12-й недели (понедельник 13-й недели)."""
    return user.cycle_start + timedelta(days=CYCLE_DAYS) if user.cycle_start else None


def is_cycle_over(user: User, today: date) -> bool:
    end = cycle_end(user)
    return end is not None and today >= end


def maybe_finish_cycle(user: User, today: date) -> bool:
    """Переводит участницу в «итоги», если её 12 недель прошли. True — если перевели сейчас."""
    if user.onboarding_step == OnboardingStep.DONE and is_cycle_over(user, today):
        user.onboarding_step = OnboardingStep.FINISHED
        return True
    return False


@dataclass
class CycleStats:
    weeks: list[int | None]  # % по неделям 1..12, None — неделя не отмечена

    @property
    def marked(self) -> list[int]:
        return [w for w in self.weeks if w is not None]

    @property
    def average(self) -> int | None:
        return round(sum(self.marked) / len(self.marked)) if self.marked else None

    @property
    def excellent_weeks(self) -> int:
        return sum(1 for w in self.marked if w >= scorecard.EXCELLENT)

    @property
    def best(self) -> tuple[int, int] | None:
        """(номер недели, %) лучшей недели; при равенстве — более поздняя."""
        candidates = [(pct, n) for n, pct in enumerate(self.weeks, 1) if pct is not None]
        if not candidates:
            return None
        pct, n = max(candidates)
        return n, pct


async def cycle_stats(session: AsyncSession, user: User) -> CycleStats:
    """% по неделям цикла. Запланировано = тактики текущего цикла (они активны, пока не начат новый)."""
    tactics = await checkins.active_tactics(session, user)
    planned = len(tactics)
    tactic_ids = {t.id for t in tactics}
    weeks: list[int | None] = [None] * scorecard.CYCLE_WEEKS
    if user.cycle_start is None or not planned:
        return CycleStats(weeks)
    rows = await session.scalars(
        select(Checkin).where(
            Checkin.user_id == user.id,
            Checkin.week_start >= user.cycle_start,
            Checkin.week_start < cycle_end(user),
        )
    )
    done_by_week: dict[int, int] = {}
    marked_weeks: set[int] = set()
    for row in rows:
        if row.tactic_id not in tactic_ids:
            continue
        n = (row.week_start - user.cycle_start).days // 7
        marked_weeks.add(n)
        done_by_week[n] = done_by_week.get(n, 0) + int(row.done)
    for n in marked_weeks:
        weeks[n] = scorecard.percent(done_by_week.get(n, 0), planned)
    return CycleStats(weeks)


async def previous_priorities(session: AsyncSession, user: User) -> list[Priority]:
    if user.cycle <= 1:
        return []
    rows = await session.scalars(
        select(Priority).where(Priority.user_id == user.id, Priority.cycle == user.cycle - 1).order_by(Priority.position)
    )
    return list(rows)


async def start_new_cycle(session: AsyncSession, user: User) -> None:
    """Новый цикл: онбординг заново с колеса баланса. Команда сохраняется."""
    await session.execute(
        update(WeeklyTactic).where(WeeklyTactic.user_id == user.id, WeeklyTactic.is_active).values(is_active=False)
    )
    user.cycle += 1
    user.cycle_start = None
    user.onboarding_step = OnboardingStep.WHEEL
    user.onboarding_position = None
    await session.flush()


async def wheel_comparison(session: AsyncSession, user: User) -> list[tuple[wheel.Sphere, int, int]]:
    """(сфера, было, стало) для сфер, оценённых и в прошлом, и в текущем цикле."""
    if user.cycle <= 1:
        return []
    now = await wheel.get_scores(session, user)
    before = await wheel.get_scores(session, user, cycle=user.cycle - 1)
    return [(s, before[s.key], v) for s, v in wheel.ordered_scores(now) if s.key in before]
