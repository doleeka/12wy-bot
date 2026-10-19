from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import Message

from bot.config import Settings


class IsAdmin(Filter):
    async def __call__(self, message: Message, settings: Settings | None = None) -> bool:
        return settings is not None and message.from_user is not None and message.from_user.id in settings.admin_ids
