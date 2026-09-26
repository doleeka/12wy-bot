"""Команды (аналог WAM): до 3 участниц, непрерывное автозаполнение."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Team, TeamMember, User

# Хендлеры aiogram работают конкурентно: без блокировки две участницы, завершившие
# онбординг одновременно, могли бы обе занять последнее место в команде.
# Бот работает в одном процессе, поэтому asyncio.Lock достаточно.
_lock = asyncio.Lock()


class TeamFull(Exception):
    pass


class TeamNotFound(Exception):
    pass


def team_name(team: Team) -> str:
    return team.name or f"Команда №{team.id}"


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_team_of(session: AsyncSession, user: User) -> Team | None:
    return await session.scalar(select(Team).join(TeamMember).where(TeamMember.user_id == user.id))


async def team_members(session: AsyncSession, team_id: int) -> list[User]:
    rows = await session.scalars(
        select(User).join(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.joined_at, TeamMember.id)
    )
    return list(rows)


async def _member_count(session: AsyncSession, team_id: int) -> int:
    return await session.scalar(select(func.count(TeamMember.id)).where(TeamMember.team_id == team_id))


async def _team_with_free_slot(session: AsyncSession) -> Team | None:
    """Самая старая команда, где меньше MAX_MEMBERS участниц."""
    counts = (
        select(TeamMember.team_id, func.count(TeamMember.id).label("n")).group_by(TeamMember.team_id).subquery()
    )
    return await session.scalar(
        select(Team)
        .outerjoin(counts, counts.c.team_id == Team.id)
        .where(func.coalesce(counts.c.n, 0) < Team.MAX_MEMBERS)
        .order_by(Team.id)
        .limit(1)
    )


async def assign_to_team(session: AsyncSession, user: User) -> tuple[Team, bool]:
    """Добавляет участницу в команду со свободным местом или создаёт новую.

    Возвращает (команда, создана_ли_новая). Коммитит сразу, внутри блокировки.
    """
    async with _lock:
        team = await get_team_of(session, user)
        if team is not None:
            return team, False
        team = await _team_with_free_slot(session)
        created = team is None
        if created:
            team = Team()
            session.add(team)
            await session.flush()
        session.add(TeamMember(team_id=team.id, user_id=user.id))
        await session.commit()
        return team, created


async def move_to_team(session: AsyncSession, user: User, team_id: int | None) -> tuple[Team | None, Team]:
    """Переносит участницу в команду team_id (None — в новую). Возвращает (старая, новая)."""
    async with _lock:
        old_team = await get_team_of(session, user)
        if team_id is None:
            new_team = Team()
            session.add(new_team)
            await session.flush()
        else:
            new_team = await session.get(Team, team_id)
            if new_team is None:
                raise TeamNotFound
            if old_team is not None and old_team.id == new_team.id:
                return old_team, new_team
            if await _member_count(session, new_team.id) >= Team.MAX_MEMBERS:
                raise TeamFull
        membership = await session.scalar(select(TeamMember).where(TeamMember.user_id == user.id))
        if membership is None:
            session.add(TeamMember(team_id=new_team.id, user_id=user.id))
        else:
            membership.team_id = new_team.id
        await session.commit()
        return old_team, new_team


async def list_teams(session: AsyncSession) -> list[tuple[Team, list[User]]]:
    teams = await session.scalars(select(Team).order_by(Team.id))
    return [(team, await team_members(session, team.id)) for team in teams]


async def ready_without_team(session: AsyncSession) -> list[User]:
    rows = await session.scalars(
        select(User)
        .outerjoin(TeamMember, TeamMember.user_id == User.id)
        .where(User.is_ready, TeamMember.id.is_(None))
        .order_by(User.id)
    )
    return list(rows)
