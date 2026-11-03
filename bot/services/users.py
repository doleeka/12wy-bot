from __future__ import annotations

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import User
from bot.services import payments


async def get_or_create_user(session: AsyncSession, tg_user: TgUser) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    if user is None:
        user = User(telegram_id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name)
        session.add(user)
        await payments.enroll(session, user)  # в поток, на который сейчас идёт набор
        await session.flush()
    else:
        # держим имя/username актуальными
        user.username = tg_user.username
        user.first_name = tg_user.first_name
    return user
