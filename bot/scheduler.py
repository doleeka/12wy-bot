"""Расписание: воскресный чек-ин, понедельничное напоминание о неделе (или итоги цикла), ночной бэкап."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import escape

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot import keyboards, texts
from bot.backup_job import send_backup
from bot.digest import send_admin_digest, send_common_weekly
from bot.config import Settings, local_today
from bot.handlers.checkin import build_checkin
from bot.handlers.cycle import summary_text
from bot.models import OnboardingStep, User
from bot.notify import display_name, safe_send
from bot.team_notify import pause_chats
from bot.services import checkins, cycle, payments, scorecard
from bot.services.tactics import days_label

log = logging.getLogger(__name__)

# Пауза между сообщениями, чтобы не упереться в лимиты Telegram при росте комьюнити
SEND_DELAY = 0.05


async def send_weekly_checkins(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    week = scorecard.week_start(local_today(settings))
    sent = 0
    async with sessionmaker() as session:
        for user in await checkins.users_for_checkin(session, week):
            if await payments.locked(session, user, local_today(settings), settings.admin_ids):
                continue  # доступ на паузе до оплаты — чек-ин не шлём
            text, markup = await build_checkin(session, user, week, settings.webapp_url)
            await session.commit()  # отметки, подставленные из галочек приложения
            if markup is not None and await safe_send(bot, user.telegram_id, text, markup):
                sent += 1
            await asyncio.sleep(SEND_DELAY)
    log.info("Чек-ин за неделю %s отправлен: %d", week, sent)
    return sent


async def send_week_planning(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    today = local_today(settings)
    sent = 0
    async with sessionmaker() as session:
        users = await session.scalars(
            select(User).where(User.onboarding_step == OnboardingStep.DONE, User.excluded_at.is_(None))
        )
        for user in list(users):
            if await payments.locked(session, user, today, settings.admin_ids):
                continue  # доступ на паузе до оплаты
            if cycle.maybe_finish_cycle(user, today):
                # понедельник 13-й недели: вместо плана недели — итоги цикла
                text = await summary_text(session, user, today)
                if await safe_send(bot, user.telegram_id, text, keyboards.cycle_finish()):
                    sent += 1
                await session.commit()
                await asyncio.sleep(SEND_DELAY)
                continue
            n = scorecard.week_number(user.cycle_start, today)
            tactics = await checkins.tactics_for_week(session, user, scorecard.week_start(today))
            if n is None or not tactics:
                continue
            lines = "\n".join(f"• {escape(t.text)}" + (f" — {days_label(t.days)}" if t.days else "") for t in tactics)
            if await safe_send(bot, user.telegram_id, texts.WEEK_PLANNING.format(n=n, tactics=lines)):
                sent += 1
            await asyncio.sleep(SEND_DELAY)
    log.info("Напоминание о неделе отправлено: %d", sent)
    return sent


async def send_payment_reminders(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    """Окно оплаты следующего месяца: пятница перед ним — «можно оплатить», его первый понедельник — «сегодня
    последний день», вторник без оплаты — «доступ на паузе» (бот и приложение). Каждый раз — с кнопками тарифов.
    По средам — кто так и не оплатил, тихо убираем из чатов (после оплаты вернём ссылками). Кто уже прислал чек,
    тех не трогаем. Пока оплата выключена — молчим."""
    from bot.handlers.payments import offer, weeks  # noqa: PLC0415 — без циклического импорта

    today = local_today(settings)
    sent = 0
    async with sessionmaker() as session:
        if not await payments.is_required(session):
            return 0
        users = await session.scalars(
            select(User).where(User.onboarding_step == OnboardingStep.DONE, User.excluded_at.is_(None))
        )
        for user in list(users):
            if user.telegram_id in settings.admin_ids or await payments.pending_of(session, user):
                continue
            header = None
            win = await payments.window(session, user, today)
            due = await payments.due_month(session, user, today, settings.admin_ids)
            if due and today.weekday() == 2 and not user.chats_paused:  # среда: на паузе — из чатов
                await pause_chats(bot, session, user)
                await session.commit()
            if win and today == win[1] - timedelta(days=payments.WINDOW_DAYS):
                month = win[0]
                header = texts.PAY_WINDOW.format(month=month, weeks=weeks(month), monday=f"{win[1]:%d.%m}")
            elif win and today == win[1]:
                month = win[0]
                header = texts.PAY_LAST_DAY.format(month=month)
            elif due and user.cycle_start and today == payments.month_start(user.cycle_start, due) + timedelta(days=1):
                month = due
                header = texts.PAY_DUE.format(month=month, weeks=weeks(month)) + texts.PAY_CHATS_WARNING
            if header is None:
                continue
            text, markup = await offer(session, user, month, header)
            if await safe_send(bot, user.telegram_id, text, markup):
                sent += 1
            await asyncio.sleep(SEND_DELAY)
    log.info("Напоминания об оплате: %d", sent)
    return sent


STALE_RECEIPT_HOURS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def remind_stale_receipts(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings,
                                now: datetime | None = None) -> int:
    """Чек ждёт решения 3+ часа — один раз напоминаем админам (с кнопками). Ночью не будим: с 9 до 22.
    now — текущее время UTC (для тестов)."""
    from bot.handlers.payments import decision_buttons  # noqa: PLC0415 — без циклического импорта

    now = now or _utcnow()
    hour = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).hour
    if not 9 <= hour < 22:
        return 0
    border = now - timedelta(hours=STALE_RECEIPT_HOURS)
    sent = 0
    async with sessionmaker() as session:
        for payment, user in await payments.pending(session):
            if payment.reminded_at is not None or payment.created_at > border:
                continue
            text = texts.PAY_ADMIN_STALE.format(
                hours=STALE_RECEIPT_HOURS, name=display_name(user), tg_id=user.telegram_id,
                tariff=payments.TARIFF_NAMES[payment.tariff], amount=payments.money(payment.amount))
            for admin_id in settings.admin_ids:
                sent += await safe_send(bot, admin_id, text, decision_buttons(payment.id))
            payment.reminded_at = now
        await session.commit()
    return sent


NUDGE_AFTER = (timedelta(days=2), timedelta(days=5))  # первое — через 2 дня после «Старт», второе — ещё через 5
NUDGE_ONLY_RECENT = timedelta(days=30)  # давно бросивших не трогаем


async def nudge_unfinished_onboarding(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings,
                                      now: datetime | None = None) -> int:
    """Нажала «Старт», но не дошла до конца: через 2 дня — напоминание, ещё через 5 — второе, мягкое, и всё.
    Кому нужна оплата — не трогаем (у них свой экран). Админу — сколько напомнили. now — UTC (для тестов)."""
    now = now or _utcnow()
    today = local_today(settings)
    sent = 0
    async with sessionmaker() as session:
        users = await session.scalars(select(User).where(
            User.onboarding_step.not_in([OnboardingStep.DONE, OnboardingStep.FINISHED]), User.excluded_at.is_(None),
            User.cycle == 1, User.onboarding_nudges < len(NUDGE_AFTER), User.created_at >= now - NUDGE_ONLY_RECENT))
        for user in list(users):
            if user.telegram_id in settings.admin_ids or await payments.locked(session, user, today, settings.admin_ids):
                continue
            since = user.onboarding_nudged_at or user.created_at
            if now - since < NUDGE_AFTER[user.onboarding_nudges]:
                continue
            name = escape(user.first_name or "")
            text = (texts.NUDGE_1.format(name=name, left=texts.NUDGE_LEFT.get(user.onboarding_step.value, "немного"))
                    if user.onboarding_nudges == 0 else texts.NUDGE_2.format(name=name))
            markup = keyboards.open_app(settings.webapp_url) if settings.webapp_url else None
            if await safe_send(bot, user.telegram_id, text, markup):
                sent += 1
            user.onboarding_nudges += 1
            user.onboarding_nudged_at = now
            await asyncio.sleep(SEND_DELAY)
        await session.commit()
    if sent:
        for admin_id in settings.admin_ids:
            await safe_send(bot, admin_id, texts.NUDGE_ADMIN.format(n=sent))
    return sent


def _hm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def setup_scheduler(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    hour, minute = _hm(settings.checkin_time)
    scheduler.add_job(
        send_weekly_checkins,
        CronTrigger(day_of_week=settings.checkin_day, hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="weekly_checkin",
        misfire_grace_time=3600,
    )
    hour, minute = _hm(settings.planning_time)
    scheduler.add_job(
        send_week_planning,
        CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="week_planning",
        misfire_grace_time=3600,
    )
    hour, minute = _hm(settings.digest_time)
    scheduler.add_job(
        send_admin_digest,
        CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="admin_digest",
        misfire_grace_time=3600,
    )
    hour, minute = _hm(settings.common_weekly_time)
    scheduler.add_job(
        send_common_weekly,
        CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="common_weekly",
        misfire_grace_time=3600,
    )
    hour, minute = _hm(settings.payment_time)
    scheduler.add_job(
        send_payment_reminders,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="payment_reminders",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        remind_stale_receipts,
        CronTrigger(minute=5, timezone=settings.timezone),  # каждый час
        args=[bot, sessionmaker, settings],
        id="stale_receipts",
        misfire_grace_time=1800,
    )
    hour, minute = _hm(settings.nudge_time)
    scheduler.add_job(
        nudge_unfinished_onboarding,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, sessionmaker, settings],
        id="onboarding_nudges",
        misfire_grace_time=3600,
    )
    hour, minute = _hm(settings.backup_time)
    scheduler.add_job(
        send_backup,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, settings],
        id="nightly_backup",
        misfire_grace_time=3600,
    )
    return scheduler
