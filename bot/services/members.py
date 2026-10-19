"""Исключение из сообщества (обратимое): ничего не получает, не считается, приложение закрыто, данные целы."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Team, User
from bot.services import teams


async def find_users(session: AsyncSession, query: str) -> list[User]:
    """По @нику, числовому Telegram id или имени (без учёта регистра)."""
    q = query.strip()
    if q.startswith("@"):
        stmt = select(User).where(func.lower(User.username) == q[1:].lower())
    elif q.lstrip("-").isdigit():
        stmt = select(User).where(User.telegram_id == int(q))
    else:
        # lower() в SQLite не понимает кириллицу — сравниваем в Python
        rows = await session.scalars(select(User).order_by(User.id))
        return [u for u in rows if (u.first_name or "").casefold() == q.casefold()]
    return list(await session.scalars(stmt.order_by(User.id)))


async def exclude(session: AsyncSession, user: User) -> Team | None:
    """Исключить: отметка + выход из тройки (место освобождается). Возвращает команду, где был(а)."""
    user.excluded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    team = await teams.leave_team(session, user)
    await session.flush()
    return team


def restore(user: User) -> None:
    user.excluded_at = None


async def list_excluded(session: AsyncSession) -> list[User]:
    return list(await session.scalars(select(User).where(User.excluded_at.is_not(None)).order_by(User.excluded_at)))
