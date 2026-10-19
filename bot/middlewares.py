from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию на каждый апдейт и коммитит её, если хендлер отработал без ошибок."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            result = await handler(event, data)
            await session.commit()
            return result


class ExcludedMiddleware(BaseMiddleware):
    """Исключённым из сообщества (/remove) бот не отвечает по существу: только «доступ закрыт» в личке.
    Админов не касается. Стоит после DbSessionMiddleware — берёт его сессию."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from bot import texts  # noqa: PLC0415 — без циклического импорта при старте
        from bot.services.teams import get_user_by_telegram_id  # noqa: PLC0415

        tg_user = data.get("event_from_user")
        settings = data.get("settings")
        if tg_user is None or (settings and tg_user.id in settings.admin_ids):
            return await handler(event, data)
        user = await get_user_by_telegram_id(data["session"], tg_user.id)
        if user is None or user.excluded_at is None:
            return await handler(event, data)
        message = getattr(event, "message", None)
        if message is not None and message.chat.type == "private":
            await message.answer(texts.EXCLUDED)
        elif getattr(event, "callback_query", None) is not None:
            await event.callback_query.answer(texts.EXCLUDED, show_alert=True)
        return None
