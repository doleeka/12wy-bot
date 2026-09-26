from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.handlers.onboarding import send_step_prompt
from bot.handlers.wheel import wheel_question
from bot.models import OnboardingStep
from bot.services import cycle, wheel
from bot.services.users import get_or_create_user

router = Router(name="start")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    name = escape(message.from_user.first_name or "")
    today = local_today(settings)
    cycle.maybe_finish_cycle(user, today)

    if user.onboarding_step == OnboardingStep.WHEEL:
        scores = await wheel.get_scores(session, user)
        if not scores:
            welcome = texts.NEW_CYCLE_WELCOME if user.cycle > 1 else texts.WELCOME
            await message.answer(welcome.format(name=name), reply_markup=keyboards.wheel_begin())
            return
        await message.answer(texts.WELCOME_BACK_WHEEL.format(name=name))
        text, markup = wheel_question(scores)
        await message.answer(text, reply_markup=markup)
        return

    await send_step_prompt(message, session, user, today)
