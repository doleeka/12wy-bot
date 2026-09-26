"""Онбординг после колеса: Explore → Eliminate → Essential intent → Тактики."""
from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models import EssentialIntent, ExploreItem, OnboardingStep, Priority, User, WeeklyTactic

PRIORITIES_COUNT = 3
MIN_EXPLORE_ITEMS = PRIORITIES_COUNT
MAX_EXPLORE_ITEMS = 40
MAX_ITEM_LEN = 200
MIN_INTENT_LEN = 15
MAX_TACTICS_PER_PRIORITY = 2

_BULLET_RE = re.compile(r"^\s*(?:[-–—•*·]+|\d+[.)])\s*")
_MEASURABLE_RE = re.compile(
    r"\d|раз|кажд|ежедн|ежеднев|минут|час|страниц|шаг|км|километр|тренировк|сесси|урок",
    re.IGNORECASE,
)


# ---------- Explore ----------

def parse_explore_text(text: str) -> list[str]:
    """Каждая непустая строка — отдельный пункт; маркеры списка убираем."""
    items = []
    for line in text.splitlines():
        line = _BULLET_RE.sub("", line).strip()
        if line:
            items.append(line[:MAX_ITEM_LEN])
    return items


async def get_explore_items(session: AsyncSession, user: User) -> list[ExploreItem]:
    rows = await session.scalars(
        select(ExploreItem)
        .where(ExploreItem.user_id == user.id, ExploreItem.cycle == user.cycle)
        .order_by(ExploreItem.id)
    )
    return list(rows)


async def add_explore_items(session: AsyncSession, user: User, texts: list[str]) -> int:
    """Добавляет пункты (без дублей и не больше лимита). Возвращает, сколько добавлено."""
    existing = await get_explore_items(session, user)
    seen = {item.text.casefold() for item in existing}
    added = 0
    for text in texts:
        if len(existing) + added >= MAX_EXPLORE_ITEMS:
            break
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        session.add(ExploreItem(user_id=user.id, cycle=user.cycle, text=text))
        added += 1
    await session.flush()
    return added


async def delete_explore_item(session: AsyncSession, user: User, item_id: int) -> bool:
    result = await session.execute(
        delete(ExploreItem).where(
            ExploreItem.id == item_id, ExploreItem.user_id == user.id, ExploreItem.cycle == user.cycle
        )
    )
    return result.rowcount > 0


# ---------- Eliminate ----------

class TooManySelected(Exception):
    pass


async def toggle_selection(session: AsyncSession, user: User, item_id: int) -> ExploreItem | None:
    items = await get_explore_items(session, user)
    item = next((i for i in items if i.id == item_id), None)
    if item is None:
        return None
    if not item.selected and sum(i.selected for i in items) >= PRIORITIES_COUNT:
        raise TooManySelected
    item.selected = not item.selected
    await session.flush()
    return item


async def confirm_priorities(session: AsyncSession, user: User) -> list[Priority]:
    """Превращает ровно 3 отмеченных пункта в приоритеты и переводит на шаг Intent."""
    selected = [i for i in await get_explore_items(session, user) if i.selected]
    if len(selected) != PRIORITIES_COUNT:
        raise ValueError(f"Нужно выбрать ровно {PRIORITIES_COUNT}, выбрано {len(selected)}")
    await session.execute(delete(Priority).where(Priority.user_id == user.id, Priority.cycle == user.cycle))
    priorities = [
        Priority(user_id=user.id, cycle=user.cycle, position=pos, title=item.text, explore_item_id=item.id)
        for pos, item in enumerate(selected, start=1)
    ]
    session.add_all(priorities)
    user.onboarding_step = OnboardingStep.INTENT
    await session.flush()
    return priorities


# ---------- Priorities / Intent / Tactics ----------

async def get_priorities(session: AsyncSession, user: User) -> list[Priority]:
    rows = await session.scalars(
        select(Priority)
        .where(Priority.user_id == user.id, Priority.cycle == user.cycle)
        .options(selectinload(Priority.intent), selectinload(Priority.tactics))
        .order_by(Priority.position)
        .execution_options(populate_existing=True)
    )
    return list(rows)


def next_priority_without_intent(priorities: list[Priority]) -> Priority | None:
    return next((p for p in priorities if p.intent is None), None)


async def save_intent(session: AsyncSession, user: User, text: str) -> Priority | None:
    """Сохраняет «зачем» для первого приоритета без него. Возвращает этот приоритет."""
    priorities = await get_priorities(session, user)
    priority = next_priority_without_intent(priorities)
    if priority is None:
        return None
    session.add(EssentialIntent(priority_id=priority.id, text=text.strip()))
    await session.flush()
    await session.refresh(priority, ["intent"])
    if next_priority_without_intent(priorities) is None:
        user.onboarding_step = OnboardingStep.TACTICS
        user.onboarding_position = 1
    return priority


def is_measurable(text: str) -> bool:
    return bool(_MEASURABLE_RE.search(text))


async def current_tactic_priority(session: AsyncSession, user: User) -> Priority | None:
    if user.onboarding_step != OnboardingStep.TACTICS or user.onboarding_position is None:
        return None
    priorities = await get_priorities(session, user)
    return next((p for p in priorities if p.position == user.onboarding_position), None)


async def add_tactic(session: AsyncSession, user: User, priority: Priority, text: str) -> WeeklyTactic:
    if len(priority.tactics) >= MAX_TACTICS_PER_PRIORITY:
        raise ValueError("У приоритета уже максимум тактик")
    tactic = WeeklyTactic(priority_id=priority.id, user_id=user.id, text=text.strip()[:MAX_ITEM_LEN])
    session.add(tactic)
    await session.flush()
    await session.refresh(priority, ["tactics"])
    return tactic


def cycle_start_for(today: date) -> date:
    """Неделя 1 стартует с ближайшего понедельника (сегодня, если сегодня понедельник)."""
    return today + timedelta(days=(7 - today.weekday()) % 7)


def advance_tactics(user: User, today: date) -> bool:
    """Переходит к следующему приоритету. True — онбординг завершён."""
    position = (user.onboarding_position or 0) + 1
    if position > PRIORITIES_COUNT:
        user.onboarding_position = None
        user.onboarding_step = OnboardingStep.DONE
        user.is_ready = True
        user.cycle_start = cycle_start_for(today)
        return True
    user.onboarding_position = position
    return False

