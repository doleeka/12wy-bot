"""Еженедельный чек-ин, scorecard и отчёт по флагу send_report."""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.handlers.cycle import send_summary
from bot.models import OnboardingStep, ReportTarget, User
from bot.notify import display_name, safe_edit, safe_send
from bot.team_notify import notify_team
from bot.services import checkins, cycle, scorecard, teams
from bot.services import today as today_svc
from bot.services.users import get_or_create_user

router = Router(name="checkin")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")

_MARK = {True: "✅", False: "❌", None: "▫️"}


default_checkin_week = checkins.default_week  # общая логика с Mini App


async def build_checkin(
    session: AsyncSession, user: User, week: date, webapp_url: str = ""
) -> tuple[str, InlineKeyboardMarkup | None]:
    tactics = await checkins.tactics_for_week(session, user, week)
    if not tactics:
        return texts.CHECKIN_NO_TACTICS, None
    marks = await checkins.get_marks(session, user, week)
    # Галочки из приложения: все дни действия отмечены → в чате сразу «сделано» (можно поменять).
    # Подставляем только туда, где отметки ещё нет — ручной ответ (в т.ч. «❌») не перетираем.
    daily = await today_svc.daily_marks(session, user, week)
    prefilled = [t for t in tactics if t.id not in marks and today_svc.suggested_done(t, week, daily)]
    for t in prefilled:
        await checkins.set_mark(session, user, t.id, week, True)
    if prefilled:
        marks = await checkins.get_marks(session, user, week)
    period = f"{week:%d.%m}–{week + timedelta(days=6):%d.%m}"
    lines = [texts.CHECKIN_HEADER.format(n=checkins.checkin_week_number(user, week), period=period)]
    current_priority = None
    for n, tactic in enumerate(tactics, 1):
        if tactic.priority_id != current_priority:
            current_priority = tactic.priority_id
            lines.append(texts.CHECKIN_PRIORITY.format(title=escape(tactic.priority.title)))
        lines.append(texts.CHECKIN_TACTIC.format(n=n, text=escape(tactic.text), mark=_MARK[marks.get(tactic.id)]))
    footer = (texts.CHECKIN_PREFILLED if prefilled else "") + texts.CHECKIN_FOOTER + (texts.CHECKIN_APP_HINT if webapp_url else "")
    return "\n".join(lines) + footer, keyboards.checkin(tactics, week.toordinal(), webapp_url)


def advice_for(value: int) -> str:
    return {
        "good": texts.SCORE_ADVICE_EXCELLENT,
        "warning": texts.SCORE_ADVICE_GOOD,
        "critical": texts.SCORE_ADVICE_LOW,
    }[scorecard.level(value)]


def result_text(n: int, done: int, planned: int) -> str:
    value = scorecard.percent(done, planned) or 0
    advice = advice_for(value)
    return texts.SCORE_RESULT.format(
        n=n,
        percent=value,
        emoji=scorecard.rating_emoji(value),
        rating=scorecard.rating(value),
        done=done,
        planned=planned,
        advice=advice,
    )


async def send_report(
    bot: Bot, session: AsyncSession, user: User, week: date, value: int, settings: Settings | None
) -> None:
    """Отчёт по флагу send_report — только процент, без целей и тактик. Один раз за неделю."""
    if user.last_reported_week is not None and user.last_reported_week >= week:
        return
    user.last_reported_week = week
    text = texts.SCORE_REPORT.format(
        name=display_name(user),
        n=checkins.checkin_week_number(user, week),
        percent=value,
        emoji=scorecard.rating_emoji(value),
    )
    if user.send_report == ReportTarget.TEAM:
        team = await teams.get_team_of(session, user)
        if team is not None:
            await notify_team(bot, session, team, text, settings, exclude=user)
    elif user.send_report == ReportTarget.ADMIN:
        for admin_id in settings.admin_ids if settings else []:
            await safe_send(bot, admin_id, text)


# ---------- участница ----------

@router.message(Command("checkin"))
async def cmd_checkin(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
        await message.answer(texts.CHECKIN_NOT_READY)
        return
    today = local_today(settings)
    week = default_checkin_week(user, today)
    if checkins.checkin_week_number(user, week) is None:
        if user.cycle_start and today < user.cycle_start:
            await message.answer(texts.CHECKIN_NOT_STARTED.format(start=f"{user.cycle_start:%d.%m}"))
        else:
            cycle.maybe_finish_cycle(user, today)
            await send_summary(message, session, user, today)
        return
    text, markup = await build_checkin(session, user, week, settings.webapp_url if settings else "")
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("ci:"))
async def on_checkin_callback(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None
) -> None:
    user = await get_or_create_user(session, callback.from_user)
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        week = date.fromordinal(int(parts[3] if action == "m" else parts[2]))
    except (IndexError, ValueError):
        await callback.answer()
        return
    if not checkins.can_check_in(user, week, local_today(settings)):
        await callback.answer(texts.CHECKIN_STALE, show_alert=True)
        return

    if action == "m" and len(parts) == 5 and parts[2].isdigit() and parts[4] in ("0", "1"):
        if await checkins.set_mark(session, user, int(parts[2]), week, parts[4] == "1"):
            text, markup = await build_checkin(session, user, week, settings.webapp_url if settings else "")
            await safe_edit(callback.message, text, markup)
    elif action == "edit":
        text, markup = await build_checkin(session, user, week, settings.webapp_url if settings else "")
        await safe_edit(callback.message, text, markup)
    elif action == "done":
        tactics = await checkins.tactics_for_week(session, user, week)
        marks = await checkins.get_marks(session, user, week)
        done, planned, unmarked = checkins.summarize(tactics, marks)
        if not planned:
            await callback.answer(texts.CHECKIN_NO_TACTICS, show_alert=True)
            return
        if unmarked:
            await callback.answer(texts.CHECKIN_UNMARKED.format(left=unmarked), show_alert=True)
            return
        n = checkins.checkin_week_number(user, week)
        await safe_edit(callback.message, result_text(n, done, planned), keyboards.checkin_edit(week.toordinal()))
        await send_report(bot, session, user, week, scorecard.percent(done, planned), settings)
    await callback.answer()


# ---------- админ ----------

@router.message(Command("report"), IsAdmin())
async def cmd_report(message: Message, command: CommandObject, session: AsyncSession) -> None:
    args = (command.args or "").split()
    targets = {t.value: t for t in ReportTarget}
    if not args or not args[0].lstrip("-").isdigit() or len(args) > 2 or (len(args) == 2 and args[1].lower() not in targets):
        await message.answer(texts.ADMIN_REPORT_USAGE)
        return
    user = await teams.get_user_by_telegram_id(session, int(args[0]))
    if user is None:
        await message.answer(texts.ADMIN_USER_NOT_FOUND.format(tg_id=args[0]))
        return
    if len(args) == 1:
        title = texts.REPORT_TARGET_TITLES[user.send_report.value]
        await message.answer(texts.ADMIN_REPORT_CURRENT.format(name=display_name(user), target=title))
        return
    user.send_report = targets[args[1].lower()]
    title = texts.REPORT_TARGET_TITLES[user.send_report.value]
    await message.answer(texts.ADMIN_REPORT_SET.format(name=display_name(user), target=title))


@router.message(Command("report"))
async def report_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
