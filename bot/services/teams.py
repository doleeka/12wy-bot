"""Команды (аналог WAM): до 3 участниц, непрерывное автозаполнение."""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select
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


async def _team_with_free_slot(session: AsyncSession, cohort_id: int | None) -> Team | None:
    """Самая старая тройка её потока, где меньше MAX_MEMBERS участниц (фокус-группа — не тройка)."""
    counts = (
        select(TeamMember.team_id, func.count(TeamMember.id).label("n")).group_by(TeamMember.team_id).subquery()
    )
    return await session.scalar(
        select(Team)
        .outerjoin(counts, counts.c.team_id == Team.id)
        .where(func.coalesce(counts.c.n, 0) < Team.MAX_MEMBERS, Team.is_focus.is_(False),
               Team.cohort_id.is_(None) if cohort_id is None else Team.cohort_id == cohort_id)
        .order_by(Team.id)
        .limit(1)
    )


async def focus_team(session: AsyncSession, cohort_id: int | None) -> Team | None:
    return await session.scalar(select(Team).where(
        Team.is_focus.is_(True), Team.cohort_id.is_(None) if cohort_id is None else Team.cohort_id == cohort_id))


def wants_focus(user: User) -> bool:
    return user.tariff == "focus"


async def assign_to_team(session: AsyncSession, user: User) -> tuple[Team | None, bool]:
    """Тройка её потока со свободным местом или новая; тариф «фокус-группа» — в фокус-группу потока
    (одна на поток, без лимита). Возвращает (команда, создана_ли_новая). Коммитит сразу, внутри блокировки.
    Вне команд (no_team) и исключённых не распределяем: (None, False).
    """
    if user.no_team or user.excluded_at is not None:
        return None, False
    async with _lock:
        team = await get_team_of(session, user)
        if team is not None:
            return team, False
        if wants_focus(user):
            team = await focus_team(session, user.cohort_id)
        else:
            team = await _team_with_free_slot(session, user.cohort_id)
        created = team is None
        if created:
            team = Team(cohort_id=user.cohort_id, is_focus=wants_focus(user),
                        name="Фокус-группа" if wants_focus(user) else None)
            session.add(team)
            await session.flush()
        session.add(TeamMember(team_id=team.id, user_id=user.id))
        await session.commit()
        return team, created


async def leave_team(session: AsyncSession, user: User) -> Team | None:
    """Убирает участницу из её команды (запись о членстве). Возвращает команду, где она была."""
    async with _lock:
        team = await get_team_of(session, user)
        if team is not None:
            await session.execute(delete(TeamMember).where(TeamMember.user_id == user.id))
            await session.flush()
        return team


async def move_to_team(session: AsyncSession, user: User, team_id: int | None) -> tuple[Team | None, Team]:
    """Переносит участницу в команду team_id (None — в новую). Возвращает (старая, новая)."""
    async with _lock:
        old_team = await get_team_of(session, user)
        if team_id is None:
            new_team = Team(cohort_id=user.cohort_id)
            session.add(new_team)
            await session.flush()
        else:
            new_team = await session.get(Team, team_id)
            if new_team is None:
                raise TeamNotFound
            if old_team is not None and old_team.id == new_team.id:
                return old_team, new_team
            if not new_team.is_focus and await _member_count(session, new_team.id) >= Team.MAX_MEMBERS:
                raise TeamFull
        membership = await session.scalar(select(TeamMember).where(TeamMember.user_id == user.id))
        if membership is None:
            session.add(TeamMember(team_id=new_team.id, user_id=user.id))
        else:
            membership.team_id = new_team.id
        user.no_team = False
        await session.commit()
        return old_team, new_team


async def list_teams(session: AsyncSession) -> list[tuple[Team, list[User]]]:
    teams = await session.scalars(select(Team).order_by(Team.id))
    return [(team, await team_members(session, team.id)) for team in teams]


async def ready_without_team(session: AsyncSession) -> list[User]:
    """Готовы, но ни в какой команде — и не на самостоятельном прохождении (их показываем отдельно)."""
    rows = await session.scalars(
        select(User)
        .outerjoin(TeamMember, TeamMember.user_id == User.id)
        .where(User.is_ready, TeamMember.id.is_(None), User.excluded_at.is_(None), User.no_team.is_(False))
        .order_by(User.id)
    )
    return list(rows)


async def list_solo(session: AsyncSession) -> list[User]:
    """Проходят самостоятельно — только приложение, без тройки (no_team)."""
    return list(await session.scalars(
        select(User).where(User.no_team.is_(True), User.excluded_at.is_(None)).order_by(User.id)
    ))


# ---------- групповой чат команды ----------

async def get_team_by_chat(session: AsyncSession, chat_id: int) -> Team | None:
    return await session.scalar(select(Team).where(Team.chat_id == chat_id))


async def link_chat(session: AsyncSession, team_id: int, chat_id: int) -> tuple[Team, Team | None]:
    """Привязывает чат к команде. Один чат — одна команда: у прежней команды чат отвязывается.

    Возвращает (команда, команда_у_которой_чат_был_раньше).
    """
    team = await session.get(Team, team_id)
    if team is None:
        raise TeamNotFound
    previous = await get_team_by_chat(session, chat_id)
    if previous is not None and previous.id != team.id:
        previous.chat_id = None
        previous.invite_link = None
        await session.flush()  # иначе уникальность chat_id сработает раньше, чем освободится значение
    if team.chat_id != chat_id:
        team.invite_link = None
    team.chat_id = chat_id
    await session.flush()
    return team, previous if previous is not None and previous.id != team.id else None


def unlink_chat(team: Team) -> None:
    team.chat_id = None
    team.invite_link = None


async def migrate_chat(session: AsyncSession, old_chat_id: int, new_chat_id: int) -> Team | None:
    """Группа стала супергруппой — у неё новый id."""
    team = await get_team_by_chat(session, old_chat_id)
    if team is not None:
        team.chat_id = new_chat_id
        await session.flush()
    return team
