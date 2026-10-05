"""Сводка сообщества за неделю для админа — только количество, без имён и без целей."""
from __future__ import annotations

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

    @property
    def missing(self) -> int:
        return self.participants - self.checked

    @property
    def average(self) -> int | None:
        return round(sum(self.percents) / len(self.percents)) if self.percents else None

    def bucket(self, level: str) -> int:
        return sum(1 for p in self.percents if scorecard.level(p) == level)


async def _week_state(session: AsyncSession, user: User, week: date) -> str:
    """'out' — неделя не в её цикле; 'buffer' — действий нет; 'done' / 'missing' — чек-ин сделан или нет."""
    if checkins.checkin_week_number(user, week) is None:
        return "out"
    if not await checkins.tactics_for_week(session, user, week):
        return "buffer"
    return "done" if await scorecard.week_percent(session, user, week) is not None else "missing"


async def week_stats(session: AsyncSession, week: date) -> WeekStats:
    stats = WeekStats(week=week)
    users = list(await session.scalars(
        select(User).where(User.onboarding_step.in_([OnboardingStep.DONE, OnboardingStep.FINISHED])).order_by(User.id)
    ))
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
    return stats
