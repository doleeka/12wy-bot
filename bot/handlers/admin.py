from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.backup_job import send_backup
from bot.config import Settings
from bot.filters import IsAdmin
from bot.services.onboarding import reset_onboarding
from bot.services.users import get_or_create_user

router = Router(name="admin")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


@router.message(Command("backup"), IsAdmin())
async def cmd_backup(message: Message, bot: Bot, settings: Settings) -> None:
    await message.answer(texts.BACKUP_STARTED)
    await send_backup(bot, settings)


@router.message(Command("resetme"), IsAdmin())
async def cmd_resetme(message: Message, session: AsyncSession) -> None:
    user = await get_or_create_user(session, message.from_user)
    await reset_onboarding(session, user)
    await message.answer(texts.ADMIN_RESET_DONE)


@router.message(Command("backup", "resetme"))
async def backup_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
