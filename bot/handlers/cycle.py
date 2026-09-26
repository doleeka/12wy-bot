"""Завершение цикла: итоги 13-й недели, пауза и старт нового цикла."""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.models import OnboardingStep, User
from bot.services import checkins, cycle, scorecard
from bot.services import onboarding as svc
from bot.services.users import get_or_create_user

router = Router(name="cycle")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


def _week_squares(weeks: list[int | None]) -> str:
    return "".join(scorecard.rating_emoji(w) for w in weeks)


async def summary_text(session: AsyncSession, user: User, today: date) -> str:
    stats = await cycle.cycle_stats(session, user)
    if stats.average is None:
        stats_text = texts.CYCLE_STATS_EMPTY
    else:
        n, pct = stats.best
        stats_text = texts.CYCLE_STATS.format(
            average=stats.average,
            emoji=scorecard.rating_emoji(stats.average),
            weeks=_week_squares(stats.weeks),
            excellent=stats.excellent_weeks,
            best=texts.CYCLE_BEST.format(n=n, percent=pct),
        )
    priorities = await svc.get_priorities(session, user)
    prev_week = scorecard.week_start(today) - timedelta(days=7)
    week12_open = stats.weeks[-1] is None and checkins.can_check_in(user, prev_week, today)
    return texts.CYCLE_SUMMARY.format(
        stats=stats_text,
        priorities="\n".join(f"{p.position}. {escape(p.title)}" for p in priorities) or "—",
        hint=texts.CYCLE_WEEK12_HINT if week12_open else "",
    )


async def send_summary(message: Message, session: AsyncSession, user: User, today: date) -> None:
    await message.answer(await summary_text(session, user, today), reply_markup=keyboards.cycle_finish())


async def begin_new_cycle(message: Message, session: AsyncSession, user: User) -> None:
    await cycle.start_new_cycle(session, user)
    await message.answer(
        texts.NEW_CYCLE_WELCOME.format(name=escape(user.first_name or "")), reply_markup=keyboards.wheel_begin()
    )


@router.callback_query(F.data.startswith("cycle:"))
async def on_cycle_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(session, callback.from_user)
    if user.onboarding_step != OnboardingStep.FINISHED:
        await callback.answer("Это уже неактуально 🙂")
        return
    action = callback.data.split(":")[1]
    await callback.message.edit_reply_markup(reply_markup=None)
    if action == "new":
        await begin_new_cycle(callback.message, session, user)
    elif action == "pause":
        await callback.message.answer(texts.CYCLE_PAUSED)
    await callback.answer()


@router.message(Command("newcycle"))
async def cmd_newcycle(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    today = local_today(settings)
    cycle.maybe_finish_cycle(user, today)
    if user.onboarding_step == OnboardingStep.FINISHED:
        await begin_new_cycle(message, session, user)
    elif user.onboarding_step == OnboardingStep.DONE:
        n = scorecard.week_number(user.cycle_start, today) or 1
        await message.answer(texts.CYCLE_NOT_FINISHED.format(n=n))
    else:
        await message.answer(texts.CYCLE_NOT_READY)
