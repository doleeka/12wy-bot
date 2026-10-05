"""Сводка сообщества для админа: по понедельникам за прошлую неделю и по /stats. Только количество."""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.config import Settings, local_today
from bot.notify import safe_send
from bot.services import scorecard
from bot.services.stats import WeekStats, week_stats

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def period(week: date) -> str:
    end = week + timedelta(days=6)
    if week.month == end.month:
        return f"{week.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{week.day} {MONTHS[week.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


def digest_text(stats: WeekStats, settings: Settings | None, running: bool = False) -> str:
    cohort = settings.cycle_start if settings else None
    n = scorecard.week_number(cohort, stats.week) if cohort else None
    if n is None:
        title = texts.ADMIN_DIGEST_TITLE_PLAIN
    else:
        title = (texts.ADMIN_DIGEST_TITLE_RUNNING if running else texts.ADMIN_DIGEST_TITLE_DONE).format(n=n)
    if not stats.participants:
        return texts.ADMIN_DIGEST_EMPTY.format(title=title, period=period(stats.week))
    teams = "\n".join(
        texts.ADMIN_DIGEST_TEAM.format(team=escape(t.name), checked=t.checked, total=t.total) for t in stats.teams
    ) or texts.ADMIN_DIGEST_NO_TEAMS
    text = texts.ADMIN_DIGEST.format(
        title=title,
        period=period(stats.week),
        checked=stats.checked,
        participants=stats.participants,
        missing=texts.ADMIN_DIGEST_MISSING.format(n=stats.missing),  # все счётчики — и нулевые тоже
        avg=f"{stats.average}%" if stats.average is not None else "—",
        good=stats.bucket("good"),
        warn=stats.bucket("warning"),
        low=stats.bucket("critical"),
        zero=stats.zero,
        no_gaps=stats.no_gaps,
        buffer=texts.ADMIN_DIGEST_BUFFER.format(n=stats.buffer),
        teams=teams,
    )
    return text + (texts.ADMIN_DIGEST_RUNNING_NOTE if running else "")


async def stats_message(session: AsyncSession, settings: Settings | None, today: date) -> str:
    """/stats: итоги прошлой недели + как идёт текущая."""
    this_week = scorecard.week_start(today)
    prev = digest_text(await week_stats(session, this_week - timedelta(days=7)), settings)
    current = digest_text(await week_stats(session, this_week), settings, running=True)
    return prev + "\n\n— — —\n\n" + current


async def send_admin_digest(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    """Понедельник: итоги закрывшейся недели — лично каждому админу. Если цикл ни у кого не шёл — молчим."""
    week = scorecard.week_start(local_today(settings)) - timedelta(days=7)
    async with sessionmaker() as session:
        stats = await week_stats(session, week)
    if not stats.participants:
        return 0
    text = digest_text(stats, settings)
    sent = 0
    for admin_id in settings.admin_ids:
        if await safe_send(bot, admin_id, text):
            sent += 1
    return sent
