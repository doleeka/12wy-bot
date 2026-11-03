"""Встречи сообщества: разовые (/meeting 12.11 19:00 …) и регулярные (/meeting каждую среду 19:00 …).
Время — местное (часовой пояс бота). Напоминания: за день и за час до каждой встречи."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Meeting

WEEKDAY_STEMS = (("пон", 0), ("вт", 1), ("ср", 2), ("чет", 3), ("пят", 4), ("суб", 5), ("вос", 6))
WEEKDAY_EVERY = ("каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу",
                 "каждую субботу", "каждое воскресенье")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")
DAY_BEFORE, HOUR_BEFORE = timedelta(days=1), timedelta(hours=1)


@dataclass
class Parsed:
    starts_at: datetime
    weekly: bool
    details: str


def _time(raw: str) -> time | None:
    try:
        hour, minute = raw.replace(".", ":").split(":")
        return time(int(hour), int(minute))
    except ValueError:
        return None


def parse(args: str, now: datetime) -> Parsed | None:
    """«12.11 19:00 Текст», «12.11.2026 19:00 Текст» или «каждую среду 19:00 Текст». None — не разобрали."""
    tokens = args.split(maxsplit=3)
    if len(tokens) >= 3 and tokens[0].lower() in {"каждый", "каждую", "каждое", "по"}:
        weekday = next((n for stem, n in WEEKDAY_STEMS if tokens[1].lower().startswith(stem)), None)
        at = _time(tokens[2])
        details = tokens[3].strip() if len(tokens) > 3 else ""
        if weekday is None or at is None or not details:
            return None
        first = datetime.combine(now.date() + timedelta(days=(weekday - now.weekday()) % 7), at)
        if first <= now:
            first += timedelta(weeks=1)
        return Parsed(first, True, details)
    tokens = args.split(maxsplit=2)
    if len(tokens) < 3:
        return None
    at = _time(tokens[1])
    parts = tokens[0].split(".")
    try:
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) > 2 and parts[2] else now.year
        when = datetime.combine(date(year, month, day), at) if at else None
    except (ValueError, IndexError):
        return None
    if when is None:
        return None
    if len(parts) < 3 and when < now:  # «12.01» в декабре — это следующий год
        when = when.replace(year=now.year + 1)
    return Parsed(when, False, tokens[2].strip()) if when > now else None


def next_occurrence(meeting: Meeting, now: datetime) -> datetime | None:
    """Ближайшая встреча не раньше now; разовая прошла — None."""
    if not meeting.weekly:
        return meeting.starts_at if meeting.starts_at >= now else None
    occ = meeting.starts_at
    if occ < now:
        occ += timedelta(weeks=((now - occ).days // 7))
        while occ < now:
            occ += timedelta(weeks=1)
    return occ


def when_text(meeting: Meeting, occ: datetime) -> str:
    day = f"{occ.day} {MONTHS_GEN[occ.month - 1]}, {occ:%H:%M}"
    return f"{WEEKDAY_EVERY[occ.weekday()]}, {occ:%H:%M} · ближайшая — {occ.day} {MONTHS_GEN[occ.month - 1]}" \
        if meeting.weekly else day


async def create(session: AsyncSession, parsed: Parsed, admin_id: int, now: datetime) -> Meeting:
    meeting = Meeting(details=parsed.details, starts_at=parsed.starts_at, weekly=parsed.weekly, created_by=admin_id)
    # объявили меньше чем за день / за час — эти напоминания уже не нужны
    if parsed.starts_at - now <= DAY_BEFORE:
        meeting.day_reminded_for = parsed.starts_at
    if parsed.starts_at - now <= HOUR_BEFORE:
        meeting.hour_reminded_for = parsed.starts_at
    session.add(meeting)
    await session.flush()
    return meeting


async def active(session: AsyncSession) -> list[Meeting]:
    return list(await session.scalars(select(Meeting).where(Meeting.active.is_(True)).order_by(Meeting.starts_at)))


async def due_reminders(session: AsyncSession, now: datetime) -> list[tuple[Meeting, datetime, str]]:
    """(встреча, когда, 'day' | 'hour') — что пора напомнить сейчас. Прошедшие разовые — выключаем."""
    out = []
    for m in await active(session):
        occ = next_occurrence(m, now)
        if occ is None:
            m.active = False
            continue
        if occ - now <= HOUR_BEFORE and m.hour_reminded_for != occ:
            m.hour_reminded_for = occ
            m.day_reminded_for = occ  # за час уже напоминаем — «завтра» не шлём
            out.append((m, occ, "hour"))
        elif occ - now <= DAY_BEFORE and m.day_reminded_for != occ:
            m.day_reminded_for = occ
            out.append((m, occ, "day"))
    await session.flush()
    return out
