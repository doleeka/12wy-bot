from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.handlers.onboarding import send_step_prompt
from bot.handlers.wheel import wheel_question
from bot.models import OnboardingStep
from bot.notify import display_name, safe_send
from bot.services import cycle, teams, wheel
from bot.services.users import get_or_create_user

router = Router(name="start")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")

# t.me/<бот>?start=solo — пригласить сразу на самостоятельное прохождение: только приложение, без тройки
SOLO_START = "solo"
PAY_START = "pay"  # кнопка «Оплатить в боте» в общем чате


async def _join_solo_by_link(bot: Bot | None, session: AsyncSession, user, settings: Settings | None) -> None:
    """Пришла по ссылке «без тройки»: если она ещё ни в какой тройке — в распределение не попадёт.
    Кто уже в тройке, того ссылка не двигает (перевести можно /solo)."""
    if user.no_team or (settings and user.telegram_id in settings.admin_ids):
        return
    if await teams.get_team_of(session, user) is not None:
        return
    user.no_team = True
    user.tariff = "app"  # ссылка «без тройки» — тариф «Приложение»
    await session.commit()
    for admin_id in settings.admin_ids if settings and bot else []:
        await safe_send(bot, admin_id, texts.SOLO_LINK_JOINED.format(name=display_name(user), tg_id=user.telegram_id))


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, settings: Settings | None = None,
                    command: CommandObject | None = None, bot: Bot | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    if command is not None and (command.args or "").strip().lower() == SOLO_START:
        await _join_solo_by_link(bot, session, user, settings)
    if command is not None and (command.args or "").strip().lower() == PAY_START:  # «Оплатить в боте» из общего чата
        from bot.handlers.payments import month_to_pay, offer  # noqa: PLC0415 — без циклического импорта
        month = await month_to_pay(session, user, settings)
        if month is None:
            await message.answer(texts.PAY_NOTHING_DUE)
            return
        text, markup = await offer(session, user, month, texts.PAY_DUE.format(
            month=month, weeks=f"{4 * month - 3}–{4 * month}"))
        await message.answer(text, reply_markup=markup)
        return
    name = escape(message.from_user.first_name or "")
    today = local_today(settings)
    cycle.maybe_finish_cycle(user, today)

    if user.onboarding_step == OnboardingStep.WHEEL:
        scores = await wheel.get_scores(session, user)
        if not scores:
            welcome = texts.NEW_CYCLE_WELCOME if user.cycle > 1 else texts.WELCOME
            if user.no_team and not (settings and user.telegram_id in settings.admin_ids):
                welcome = welcome.replace(texts.WELCOME_TEAM_LINE, texts.WELCOME_SOLO_LINE)
            webapp_url = settings.webapp_url if settings else ""
            await message.answer(welcome.format(name=name), reply_markup=keyboards.wheel_begin(webapp_url))
            return
        await message.answer(texts.WELCOME_BACK_WHEEL.format(name=name))
        text, markup = wheel_question(scores)
        await message.answer(text, reply_markup=markup)
        return

    await send_step_prompt(message, session, user, today)
    if settings and settings.webapp_url:
        await message.answer(texts.OPEN_APP, reply_markup=keyboards.open_app(settings.webapp_url))
