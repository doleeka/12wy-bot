"""Расписание: воскресный чек-ин и понедельничное напоминание о новой неделе."""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot import texts
from bot.config import Settings, local_today
from bot.handlers.checkin import build_checkin
from bot.models import OnboardingStep, User
from bot.notify import safe_send
from bot.services import checkins, scorecard

log = logging.getLogger(__name__)

# Пауза между сообщениями, чтобы не упереться в лимиты Telegram при росте комьюнити
SEND_DELAY = 0.05


async def send_weekly_checkins(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    week = scorecard.week_start(local_today(settings))
    sent = 0
    async with sessionmaker() as session:
        for user in await checkins.users_for_checkin(session, week):
            text, markup = await build_checkin(session, user, week)
            if markup is not None and await safe_send(bot, user.telegram_id, text, markup):
                sent += 1
            await asyncio.sleep(SEND_DELAY)
    log.info("Чек-ин за неделю %s отправлен: %d", week, sent)
    return sent


async def send_week_planning(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    today = local_today(settings)
    sent = 0
    async with sessionmaker() as session:
        users = await session.scalars(select(User).where(User.onboarding_step == OnboardingStep.DONE))
        for user in users:
            n = scorecard.week_number(user.cycle_start, today)
            tactics = await checkins.active_tactics(session, user)
            if n is None or not tactics:
                continue
            lines = "\n".join(f"• {escape(t.text)}" for t in tactics)
            if await safe_send(bot, user.telegram_id, texts.WEEK_PLANNING.format(n=n, tactics=lines)):
                sent += 1
            await asyncio.sleep(SEND_DELAY)
    log.info("Напоминание о неделе отправлено: %d", sent)
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
    return scheduler
