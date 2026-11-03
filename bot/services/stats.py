"""Сводка сообщества за неделю для админа — только количество, без имён и без целей."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import OnboardingStep, User
from bot.services import checkins, scorecard, teams


@dataclass
class TeamCount:
    name: str
    checked: int  # сделали чек-ин недели
    total: int    # участниц, у которых эта неделя — в цикле и есть действия


@dataclass
class WeekStats:
    week: date
    participants: int = 0              # у кого эта неделя в цикле и есть действия по плану
    checked: int = 0                   # отметили неделю целиком
    percents: list[int] = field(default_factory=list)
    buffer: int = 0                    # неделя без действий по плану — чек-ин не нужен
    no_gaps: int = 0                   # отмечают все свои недели без пропусков
    teams: list[TeamCount] = field(default_factory=list)
    solo: TeamCount | None = None      # самостоятельно — только приложение, без тройки

    @property
    def missing(self) -> int:
        return self.participants - self.checked

    @property
    def average(self) -> int | None:
        return round(sum(self.percents) / len(self.percents)) if self.percents else None

    def bucket(self, level: str) -> int:
        """good / warning / critical; «critical» здесь — 1–69%: 0% считаем отдельно (zero)."""
        return sum(1 for p in self.percents if p > 0 and scorecard.level(p) == level)

    @property
    def zero(self) -> int:
        """Отметили неделю, но ни одно действие не получилось (0%)."""
        return sum(1 for p in self.percents if p == 0)


async def _week_state(session: AsyncSession, user: User, week: date) -> str:
    """'out' — неделя не в её цикле; 'buffer' — действий нет; 'done' / 'missing' — чек-ин сделан или нет."""
    if checkins.checkin_week_number(user, week) is None:
        return "out"
    if not await checkins.tactics_for_week(session, user, week):
        return "buffer"
    return "done" if await scorecard.week_percent(session, user, week) is not None else "missing"


async def week_stats(session: AsyncSession, week: date, cohort_id: int | None = None,
                     skip: Callable[[User], Awaitable[bool]] | None = None) -> WeekStats:
    """cohort_id — только этот поток; skip — кого не считать (например, на паузе до оплаты)."""
    stats = WeekStats(week=week)
    query = select(User).where(
        User.onboarding_step.in_([OnboardingStep.DONE, OnboardingStep.FINISHED]), User.excluded_at.is_(None))
    if cohort_id is not None:
        query = query.where(User.cohort_id == cohort_id)
    users = [u for u in await session.scalars(query.order_by(User.id)) if skip is None or not await skip(u)]
    state: dict[int, str] = {}
    for user in users:
        s = state[user.id] = await _week_state(session, user, week)
        if s == "buffer":
            stats.buffer += 1
        if s not in ("done", "missing"):
            continue
        stats.participants += 1
        if s == "done":
            stats.checked += 1
            stats.percents.append(await scorecard.week_percent(session, user, week))
            # без пропусков: все прошлые недели её цикла с действиями тоже отмечены
            prev = [user.cycle_start + timedelta(weeks=i) for i in range(checkins.checkin_week_number(user, week) - 1)]
            if all([await _week_state(session, user, w) != "missing" for w in prev]):
                stats.no_gaps += 1
    for team, members in await teams.list_teams(session):
        counted = [m for m in members if state.get(m.id) in ("done", "missing")]
        if counted:
            stats.teams.append(TeamCount(teams.team_name(team), sum(state[m.id] == "done" for m in counted), len(counted)))
    solo = [u for u in users if u.no_team and state[u.id] in ("done", "missing")]
    if solo:
        stats.solo = TeamCount("Самостоятельно", sum(state[u.id] == "done" for u in solo), len(solo))
    return stats
