"""Отправка сообщений другим пользователям без падения хендлера."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from bot.models import User

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> bool:  # noqa: ANN001
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as e:  # заблокировала бота, удалила аккаунт и т.п.
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


def display_name(user: User) -> str:
    """Имя для показа другим (уже экранированное для HTML)."""
    name = escape(user.first_name or "") or "Участница"
    return f"{name} (@{escape(user.username)})" if user.username else name


async def safe_edit(message, text: str, reply_markup=None) -> None:  # noqa: ANN001
    """edit_text, который не падает на «message is not modified» (повторное нажатие той же кнопки)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
