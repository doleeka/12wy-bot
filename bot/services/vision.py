"""Видение на 3+ года и рефлексия 12-й недели. Необязательные, личные (не уходят команде)."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import OnboardingStep, User, Vision, VisionReflection
from bot.services import scorecard

FIELDS = ("work", "life", "me", "main")
REFLECTION_FIELDS = ("closer", "changed", "next")
MAX_LEN = 1000  # 1–2 предложения на блок; лимит только от мусора


def clean(texts: dict[str, str | None], fields: tuple[str, ...]) -> dict[str, str]:
    """Пустые блоки допустимы; длинные — ошибка (ValueError), а не молчаливая обрезка."""
    out = {f: (texts.get(f) or "").strip() for f in fields}
    if any(len(v) > MAX_LEN for v in out.values()):
        raise ValueError("too_long")
    return out


async def get_vision(session: AsyncSession, user: User) -> Vision | None:
    return await session.scalar(select(Vision).where(Vision.user_id == user.id))


async def save_vision(session: AsyncSession, user: User, texts: dict[str, str]) -> Vision:
    vision = await get_vision(session, user)
    if vision is None:
        vision = Vision(user_id=user.id)
        session.add(vision)
    for field, value in texts.items():
        setattr(vision, field, value)
    await session.flush()
    await session.refresh(vision)
    return vision


def reflection_open(user: User, today: date) -> bool:
    """Вопрос о видении — на 12-й неделе цикла (и после неё, пока участница не перешла к итогам)."""
    if user.onboarding_step != OnboardingStep.DONE or user.cycle_start is None:
        return False
    n = scorecard.week_number(user.cycle_start, today)
    return n == scorecard.CYCLE_WEEKS or (n is None and today >= user.cycle_start + timedelta(weeks=scorecard.CYCLE_WEEKS))


async def get_reflection(session: AsyncSession, user: User) -> VisionReflection | None:
    return await session.scalar(
        select(VisionReflection).where(VisionReflection.user_id == user.id, VisionReflection.cycle == user.cycle)
    )


async def save_reflection(session: AsyncSession, user: User, texts: dict[str, str]) -> VisionReflection:
    row = await get_reflection(session, user)
    if row is None:
        row = VisionReflection(user_id=user.id, cycle=user.cycle)
        session.add(row)
    for field, value in texts.items():
        setattr(row, field, value)
    await session.flush()
    return row
