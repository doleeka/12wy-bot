"""Колесо баланса: сферы, порядок вопросов, визуализация."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import User, WheelOfBalance


@dataclass(frozen=True)
class Sphere:
    key: str
    title: str
    emoji: str


CORE_SPHERES: list[Sphere] = [
    Sphere("career", "Карьера", "💼"),
    Sphere("health", "Здоровье", "💪"),
    Sphere("relationships", "Отношения", "❤️"),
    Sphere("finance", "Финансы", "💰"),
    Sphere("growth", "Саморазвитие", "📚"),
    Sphere("rest", "Отдых", "🌴"),
]

EXTRA_SPHERES: list[Sphere] = [
    Sphere("friends", "Окружение", "👭"),
    Sphere("creativity", "Творчество", "🎨"),
    Sphere("spirit", "Духовность", "🕊"),
    Sphere("home", "Дом и быт", "🏡"),
]

MAX_EXTRA = 2
MIN_SCORE, MAX_SCORE = 1, 10
# Сфера считается просадкой, если оценка не выше этого порога
LOW_THRESHOLD = 5

SPHERES: dict[str, Sphere] = {s.key: s for s in CORE_SPHERES + EXTRA_SPHERES}
_CORE_KEYS = {s.key for s in CORE_SPHERES}
_EXTRA_KEYS = {s.key for s in EXTRA_SPHERES}


async def get_scores(session: AsyncSession, user: User, cycle: int | None = None) -> dict[str, int]:
    """Оценки колеса за цикл (по умолчанию — текущий цикл участницы)."""
    rows = await session.scalars(
        select(WheelOfBalance).where(
            WheelOfBalance.user_id == user.id, WheelOfBalance.cycle == (cycle or user.cycle)
        )
    )
    return {row.sphere: row.score for row in rows}


async def save_score(session: AsyncSession, user: User, sphere: str, score: int) -> None:
    if sphere not in SPHERES:
        raise ValueError(f"Неизвестная сфера: {sphere}")
    if not MIN_SCORE <= score <= MAX_SCORE:
        raise ValueError(f"Оценка должна быть от {MIN_SCORE} до {MAX_SCORE}")
    row = await session.scalar(
        select(WheelOfBalance).where(
            WheelOfBalance.user_id == user.id,
            WheelOfBalance.cycle == user.cycle,
            WheelOfBalance.sphere == sphere,
        )
    )
    if row is None:
        session.add(WheelOfBalance(user_id=user.id, cycle=user.cycle, sphere=sphere, score=score))
    else:
        row.score = score
    await session.flush()


def next_core_sphere(scores: dict[str, int]) -> Sphere | None:
    return next((s for s in CORE_SPHERES if s.key not in scores), None)


def rated_extra_count(scores: dict[str, int]) -> int:
    return sum(1 for key in scores if key in _EXTRA_KEYS)


def available_extras(scores: dict[str, int]) -> list[Sphere]:
    """Дополнительные сферы, которые ещё можно добавить."""
    if rated_extra_count(scores) >= MAX_EXTRA:
        return []
    return [s for s in EXTRA_SPHERES if s.key not in scores]


def can_rate(scores: dict[str, int], sphere: str) -> bool:
    if sphere in _CORE_KEYS:
        return True
    if sphere in _EXTRA_KEYS:
        return sphere in scores or (next_core_sphere(scores) is None and rated_extra_count(scores) < MAX_EXTRA)
    return False


def ordered_scores(scores: dict[str, int]) -> list[tuple[Sphere, int]]:
    order = CORE_SPHERES + EXTRA_SPHERES
    return [(s, scores[s.key]) for s in order if s.key in scores]


def render_chart(scores: dict[str, int]) -> str:
    """Текстовая «диаграмма» для <pre>-блока."""
    items = ordered_scores(scores)
    width = max(len(s.title) for s, _ in items)
    lines = []
    for sphere, score in items:
        bar = "█" * score + "░" * (MAX_SCORE - score)
        mark = " ⚠" if score <= LOW_THRESHOLD else ""
        lines.append(f"{sphere.title.ljust(width)} {bar} {score:>2}{mark}")
    return "\n".join(lines)


def low_spheres(scores: dict[str, int]) -> list[tuple[Sphere, int]]:
    """Просадки: всё, что ≤ порога; если таких нет — самые низкие сферы."""
    items = ordered_scores(scores)
    if not items:
        return []
    lows = [(s, v) for s, v in items if v <= LOW_THRESHOLD]
    if not lows:
        min_score = min(v for _, v in items)
        lows = [(s, v) for s, v in items if v == min_score]
    return sorted(lows, key=lambda item: item[1])


def average(scores: dict[str, int]) -> float:
    return round(sum(scores.values()) / len(scores), 1) if scores else 0.0
