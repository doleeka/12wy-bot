"""Расписание тактик по неделям цикла и подписи к нему."""
from __future__ import annotations

from collections.abc import Iterable

from bot.models import WeeklyTactic
from bot.services.scorecard import CYCLE_WEEKS

ALL_WEEKS = list(range(1, CYCLE_WEEKS + 1))
# В приложении по брифу обычно 3–8 тактик на приоритет; жёстко ограничиваем сверху
MAX_TACTICS_PER_PRIORITY = 8
RECOMMENDED_MIN, RECOMMENDED_MAX = 3, 8
MAX_TACTIC_LEN = 200
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]  # 0 = понедельник


def normalize_weeks(weeks: Iterable[int] | None) -> list[int] | None:
    """None или все 12 недель → None («каждую неделю»); иначе отсортированный список без повторов."""
    if weeks is None:
        return None
    result = sorted(set(weeks))
    if not result or any(not 1 <= w <= CYCLE_WEEKS for w in result):
        raise ValueError("Недели — от 1 до 12, хотя бы одна")
    return None if result == ALL_WEEKS else result


def normalize_days(days: Iterable[int] | None) -> list[int] | None:
    """Дни недели еженедельной тактики: отсортированный список 0–6 без повторов, хотя бы один."""
    if days is None:
        return None
    result = sorted(set(days))
    if not result or any(not 0 <= d <= 6 for d in result):
        raise ValueError("Дни недели — от пн до вс, хотя бы один")
    return result


def days_label(days: list[int] | None) -> str:
    if not days:
        return ""
    if len(days) == 7:
        return "каждый день"
    return ", ".join(WEEKDAYS[d] for d in days)


def _ranges(weeks: list[int]) -> list[str]:
    parts, start = [], weeks[0]
    for prev, cur in zip(weeks, weeks[1:] + [None]):
        if cur != prev + 1:
            parts.append(str(start) if start == prev else f"{start}–{prev}")
            start = cur
    return parts


def weeks_label(weeks: list[int] | None, days: list[int] | None = None) -> str:
    if weeks is None:
        if days and len(days) == 7:
            return "каждый день"
        return "каждую неделю" + (f" · {days_label(days)}" if days else "")
    if len(weeks) == 1:
        return f"неделя {weeks[0]}"
    return "недели " + ", ".join(_ranges(weeks))


def for_week(tactics: Iterable[WeeklyTactic], week_number: int) -> list[WeeklyTactic]:
    return [t for t in tactics if t.in_week(week_number)]


def load_per_week(tactics: Iterable[WeeklyTactic]) -> list[int]:
    """Сколько тактик приходится на каждую из 12 недель — для подсказки про буфер."""
    tactics = list(tactics)
    return [len(for_week(tactics, n)) for n in ALL_WEEKS]
