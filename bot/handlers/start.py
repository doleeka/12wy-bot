from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers.wheel import send_wheel_result, wheel_question
from bot.models import OnboardingStep
from bot.services import wheel
from bot.services.users import get_or_create_user

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    user = await get_or_create_user(session, message.from_user)
    name = escape(message.from_user.first_name or "")

    if user.onboarding_step == OnboardingStep.WHEEL:
        scores = await wheel.get_scores(session, user)
        if not scores:
            await message.answer(texts.WELCOME.format(name=name), reply_markup=keyboards.wheel_begin())
            return
        await message.answer(texts.WELCOME_BACK_WHEEL.format(name=name))
        text, markup = wheel_question(scores)
        await message.answer(text, reply_markup=markup)
        return

    # Следующие шаги онбординга подключим по мере готовности
    scores = await wheel.get_scores(session, user)
    await message.answer(texts.WHEEL_ALREADY_DONE)
    if scores:
        await send_wheel_result(message, scores)
    await message.answer(texts.EXPLORE_COMING)
