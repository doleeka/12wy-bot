"""Сводка сообщества для админа: по понедельникам за прошлую неделю и по /stats. Только количество."""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.config import Settings, local_today
from bot.notify import safe_send
from bot.models import Cohort
from bot.services import payments, scorecard
from bot.services.stats import WeekStats, week_stats

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def period(week: date) -> str:
    end = week + timedelta(days=6)
    if week.month == end.month:
        return f"{week.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{week.day} {MONTHS[week.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


def digest_text(stats: WeekStats, settings: Settings | None, running: bool = False, cohort: Cohort | None = None) -> str:
    start = cohort.start if cohort else (settings.cycle_start if settings else None)
    n = scorecard.week_number(start, stats.week) if start else None
    if n is None:
        title = texts.ADMIN_DIGEST_TITLE_PLAIN
    else:
        title = (texts.ADMIN_DIGEST_TITLE_RUNNING if running else texts.ADMIN_DIGEST_TITLE_DONE).format(n=n)
    if cohort is not None:
        title = f"Поток {cohort.number} · {title}"
    if not stats.participants:
        return texts.ADMIN_DIGEST_EMPTY.format(title=title, period=period(stats.week))
    teams = "\n".join(
        texts.ADMIN_DIGEST_TEAM.format(team=escape(t.name), checked=t.checked, total=t.total) for t in stats.teams
    ) or texts.ADMIN_DIGEST_NO_TEAMS
    if stats.solo:
        teams += texts.ADMIN_DIGEST_SOLO.format(checked=stats.solo.checked, total=stats.solo.total)
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


async def _per_cohort(session: AsyncSession, settings: Settings | None, week: date, today: date,
                      running: bool = False, skip_empty: bool = False) -> list[str]:
    """Сводка недели — отдельно по каждому потоку (у каждого своя неделя цикла); без потоков — одна общая.
    Кто на паузе до оплаты, в подсчёт не входит."""
    admin_ids = settings.admin_ids if settings else []

    async def paused(user) -> bool:  # noqa: ANN001
        return await payments.locked(session, user, today, admin_ids)

    cohorts = await payments.cohorts(session) or [None]
    out = []
    for cohort in cohorts:
        stats = await week_stats(session, week, cohort.id if cohort else None, paused)
        if skip_empty and not stats.participants:
            continue
        out.append(digest_text(stats, settings, running, cohort if len(cohorts) > 1 else None))
    return out


async def stats_message(session: AsyncSession, settings: Settings | None, today: date) -> str:
    """/stats: итоги прошлой недели + как идёт текущая — по каждому потоку, где идёт цикл."""
    this_week = scorecard.week_start(today)
    prev = await _per_cohort(session, settings, this_week - timedelta(days=7), today, skip_empty=True)
    current = await _per_cohort(session, settings, this_week, today, running=True, skip_empty=True)
    if not prev and not current:  # ни у кого не идёт цикл — покажем пустую сводку, как раньше
        prev = await _per_cohort(session, settings, this_week - timedelta(days=7), today)
        current = await _per_cohort(session, settings, this_week, today, running=True)
    return "\n\n— — —\n\n".join(prev + current)


async def send_admin_digest(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> int:
    """Понедельник: итоги закрывшейся недели — лично каждому админу, по каждому потоку. Цикл ни у кого не шёл — молчим."""
    today = local_today(settings)
    week = scorecard.week_start(today) - timedelta(days=7)
    async with sessionmaker() as session:
        parts = await _per_cohort(session, settings, week, today, skip_empty=True)
    if not parts:
        return 0
    text = "\n\n— — —\n\n".join(parts)
    sent = 0
    for admin_id in settings.admin_ids:
        if await safe_send(bot, admin_id, text):
            sent += 1
    return sent


async def send_common_weekly(bot: Bot, sessionmaker: async_sessionmaker, settings: Settings) -> bool:
    """Понедельник: итоги прошлой недели в общий чат сообщества — только числа, все потоки вместе,
    участницы общего чата («Базовый» и «Фокус-группа»); кто на паузе до оплаты — не считается."""
    from bot.team_notify import common_chat_id, common_thread_id, in_common_chat  # noqa: PLC0415 — без цикла

    today = local_today(settings)
    week = scorecard.week_start(today) - timedelta(days=7)
    async with sessionmaker() as session:
        chat_id = await common_chat_id(session)
        if chat_id is None:
            return False
        thread_id = await common_thread_id(session)

        async def skip(user) -> bool:  # noqa: ANN001
            return not in_common_chat(user) or await payments.locked(session, user, today, settings.admin_ids)

        stats = await week_stats(session, week, None, skip)
    if not stats.participants:
        return False
    return await safe_send(bot, chat_id, texts.COMMON_WEEKLY.format(
        period=period(week), checked=stats.checked, participants=stats.participants,
        good=stats.bucket("good"), avg=f"{stats.average}%" if stats.average is not None else "—"), thread_id=thread_id)
