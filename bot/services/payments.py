"""Оплата через бот: перевод на Kaspi → чек в бот → админ подтверждает. Цена и реквизиты — в настройках,
админ задаёт их сам (/payment text …). Пока оплата выключена, новые участницы получают доступ сразу."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import AppSetting, Payment, PaymentStatus, User

REQUIRED = "payment_required"
TEXT = "payment_text"
TEAM, SOLO = "team", "solo"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def is_required(session: AsyncSession) -> bool:
    return await get_setting(session, REQUIRED) == "1"


async def instructions(session: AsyncSession) -> str | None:
    return await get_setting(session, TEXT)


async def needs_payment(session: AsyncSession, user: User | None, admin_ids: list[int]) -> bool:
    """Доступ закрыт до оплаты: оплата включена, а у неё оплаты нет. Админов не касается."""
    if user is None or user.paid_at is not None or user.telegram_id in admin_ids:
        return False
    return await is_required(session)


async def add_receipt(session: AsyncSession, user: User, file_id: str, kind: str) -> Payment:
    payment = Payment(user_id=user.id, file_id=file_id, file_kind=kind, status=PaymentStatus.PENDING)
    session.add(payment)
    await session.flush()
    return payment


def mark_paid(user: User, tariff: str | None = None) -> None:
    user.paid_at = user.paid_at or _now()
    if tariff == SOLO:
        user.no_team = True
    elif tariff == TEAM:
        user.no_team = False


def approve(payment: Payment, user: User, tariff: str, admin_id: int) -> None:
    payment.status, payment.tariff, payment.decided_at, payment.decided_by = PaymentStatus.APPROVED, tariff, _now(), admin_id
    mark_paid(user, tariff)


def reject(payment: Payment, admin_id: int) -> None:
    payment.status, payment.decided_at, payment.decided_by = PaymentStatus.REJECTED, _now(), admin_id


async def pending(session: AsyncSession) -> list[tuple[Payment, User]]:
    rows = await session.execute(
        select(Payment, User).join(User, User.id == Payment.user_id)
        .where(Payment.status == PaymentStatus.PENDING).order_by(Payment.id)
    )
    return [(p, u) for p, u in rows]


async def unpaid_count(session: AsyncSession, admin_ids: list[int]) -> int:
    rows = await session.scalars(select(User).where(User.paid_at.is_(None), User.excluded_at.is_(None)))
    return sum(1 for u in rows if u.telegram_id not in admin_ids)
