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
from bot.services.cycle import end_test_cycle, find_test_marks, in_test_mode, real_start_for, start_test_cycle
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
    if arg in ("off", "off confirm"):
        # идёт общий цикл — возвращаемся в него (05.10), а не на следующий понедельник
        real_start = real_start_for(today, settings.cycle_start if settings else None)
        if not in_test_mode(user, real_start):
            await message.answer(texts.TEST_CYCLE_NOT_ON.format(start=f"{real_start:%d.%m}"))
            return
        if arg == "off":  # сначала — что именно изменится; без подтверждения ничего не трогаем
            marks = await find_test_marks(session, user, real_start)
            weeks = "\n".join(
                texts.TEST_CYCLE_OFF_WEEK.format(start=f"{d:%d.%m}", n=n, done=done) for d, n, done in marks.by_week()
            ) or texts.TEST_CYCLE_OFF_NO_WEEKS
            await message.answer(texts.TEST_CYCLE_OFF_PREVIEW.format(
                now=f"{user.cycle_start:%d.%m}" if user.cycle_start else "—", start=f"{real_start:%d.%m}",
                weeks=weeks, daily=len(marks.daily),
            ))
            return
        marks = await end_test_cycle(session, user, real_start)
        user.onboarding_step = OnboardingStep.DONE
        await message.answer(texts.TEST_CYCLE_OFF.format(
            start=f"{real_start:%d.%m}", checkins=len(marks.checkins), daily=len(marks.daily)
        ))
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
