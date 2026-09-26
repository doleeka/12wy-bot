from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.backup_job import send_backup
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.models import OnboardingStep
from bot.services.cycle import end_test_cycle, start_test_cycle
from bot.services.onboarding import cycle_start_for, reset_onboarding
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


@router.message(Command("testcycle"), IsAdmin())
async def cmd_testcycle(
    message: Message, command: CommandObject, session: AsyncSession, settings: Settings | None = None
) -> None:
    user = await get_or_create_user(session, message.from_user)
    if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
        await message.answer(texts.TEST_CYCLE_NOT_READY)
        return
    arg = (command.args or "").strip().lower()
    today = local_today(settings)
    if arg == "off":
        real_start = cycle_start_for(today, settings.cycle_start if settings else None)
        deleted = await end_test_cycle(session, user, real_start)
        user.onboarding_step = OnboardingStep.DONE
        await message.answer(texts.TEST_CYCLE_OFF.format(start=f"{real_start:%d.%m}", deleted=deleted))
        return
    if arg and not (arg.isdigit() and 1 <= int(arg) <= 12):
        await message.answer(texts.TEST_CYCLE_USAGE)
        return
    week = int(arg or 1)
    start = start_test_cycle(user, today, week)
    user.onboarding_step = OnboardingStep.DONE
    await message.answer(texts.TEST_CYCLE_ON.format(start=f"{start:%d.%m}", week=week))


@router.message(Command("backup", "resetme", "testcycle"))
async def backup_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
