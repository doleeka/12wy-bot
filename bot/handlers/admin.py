from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import texts
from bot.backup_job import send_backup
from bot.config import Settings
from bot.filters import IsAdmin

router = Router(name="admin")


@router.message(Command("backup"), IsAdmin())
async def cmd_backup(message: Message, bot: Bot, settings: Settings) -> None:
    await message.answer(texts.BACKUP_STARTED)
    await send_backup(bot, settings)


@router.message(Command("backup"))
async def backup_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
