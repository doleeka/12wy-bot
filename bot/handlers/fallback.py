"""Подключается последним: /help и неизвестные команды. on_error регистрируется на диспетчере (bot.main):
ошибки поднимаются от роутера к родителю, и соседний роутер их бы не увидел."""
from __future__ import annotations

import logging
import traceback
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import ErrorEvent, Message

from bot import texts
from bot.config import Settings
from bot.notify import safe_send

log = logging.getLogger(__name__)

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


def help_text(user_id: int, settings: Settings | None) -> str:
    is_admin = settings is not None and user_id in settings.admin_ids
    return texts.HELP + (texts.HELP_ADMIN if is_admin else "")


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings | None = None) -> None:
    await message.answer(help_text(message.from_user.id, settings))


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, settings: Settings | None = None) -> None:
    """Любая нераспознанная команда в личке — вместо тишины подсказка."""
    await message.answer("Не знаю такой команды 🙂\n\n" + help_text(message.from_user.id, settings))


async def on_error(event: ErrorEvent, bot: Bot, settings: Settings | None = None) -> bool:
    """Необработанная ошибка: участнице — вежливое сообщение, админам — суть ошибки, в лог — трейсбек."""
    log.error("Ошибка при обработке апдейта:\n%s", "".join(traceback.format_exception(event.exception)))
    update = event.update
    user = None
    chat_id = None
    if update.message:
        user, chat_id = update.message.from_user, update.message.chat.id
    elif update.callback_query:
        user = update.callback_query.from_user
        chat_id = user.id
        try:
            await update.callback_query.answer()
        except Exception:  # noqa: BLE001
            pass
    if chat_id is not None:
        await safe_send(bot, chat_id, texts.ERROR_USER)
    who = f"{escape(user.first_name or '')} ({user.id})" if user else "бота"
    error = escape(f"{type(event.exception).__name__}: {event.exception}")[:500]
    for admin_id in settings.admin_ids if settings else []:
        if admin_id != chat_id:
            await safe_send(bot, admin_id, texts.ERROR_ADMIN.format(who=who, error=error))
    return True
