"""Оплата по месяцам цикла: перевод на Kaspi → чек в бот → админ подтверждает.

Цикл — 3 месяца по 4 недели (1–4, 5–8, 9–12). Оплата открывает месяцы «по месяц N»: за 1 месяц или сразу за 3.
Окно оплаты следующего месяца — с пятницы перед ним по его первый понедельник включительно; со вторника без
оплаты доступ закрыт (данные целы, после оплаты всё как было). Цены — у каждого потока свои, по тарифам
(приложение / базовый / фокус-группа); личная цена участницы перекрывает цену потока. 0 ₸ — бесплатно.
Реквизиты Kaspi — в настройках (/payment text), включает оплату админ (/payment on)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import AppSetting, Cohort, CohortPrice, OnboardingStep, Payment, PaymentStatus, PriceOverride, User

REQUIRED = "payment_required"
TEXT = "payment_text"

APP, BASIC, FOCUS = "app", "basic", "focus"
TARIFFS = (APP, BASIC, FOCUS)
TARIFF_NAMES = {APP: "Приложение", BASIC: "Базовый", FOCUS: "Фокус-группа"}
TARIFF_ALIASES = {
    "app": APP, "приложение": APP, "прил": APP,
    "basic": BASIC, "базовый": BASIC, "база": BASIC,
    "focus": FOCUS, "фокус": FOCUS, "фокус-группа": FOCUS, "фокусгруппа": FOCUS,
}
CYCLE_MONTHS = 3
WEEKS_PER_MONTH = 4
WINDOW_DAYS = 3  # окно оплаты открывается за 3 дня до первого понедельника месяца


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(amount: int) -> str:
    return "бесплатно" if amount == 0 else f"{amount:,}".replace(",", " ") + " ₸"


def months_word(months: int) -> str:
    return "1 месяц" if months == 1 else f"{months} месяца"


# ---------- настройки ----------

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


# ---------- потоки ----------

async def cohorts(session: AsyncSession) -> list[Cohort]:
    return list(await session.scalars(select(Cohort).order_by(Cohort.number)))


async def cohort_by_number(session: AsyncSession, number: int) -> Cohort | None:
    return await session.scalar(select(Cohort).where(Cohort.number == number))


async def enrolling_cohort(session: AsyncSession) -> Cohort | None:
    return await session.scalar(select(Cohort).where(Cohort.enrolling.is_(True)).order_by(Cohort.number.desc()))


async def cohort_of(session: AsyncSession, user: User) -> Cohort | None:
    return await session.get(Cohort, user.cohort_id) if user.cohort_id else None


async def cohort_start(session: AsyncSession, user: User, settings) -> date | None:  # noqa: ANN001
    """Общий старт её потока (неделя 1); без потока — общая дата из настроек (COHORT_START)."""
    cohort = await cohort_of(session, user)
    if cohort is not None:
        return cohort.start
    return settings.cycle_start if settings else None


async def enroll(session: AsyncSession, user: User) -> None:
    """Новенькая (или новый цикл) — в поток, на который сейчас идёт набор."""
    cohort = await enrolling_cohort(session)
    user.cohort_id = cohort.id if cohort else None


async def create_cohort(session: AsyncSession, start: date) -> Cohort:
    last = await session.scalar(select(Cohort.number).order_by(Cohort.number.desc()).limit(1))
    cohort = Cohort(number=(last or 0) + 1, start=start, enrolling=False)
    session.add(cohort)
    await session.flush()
    return cohort


async def set_enrolling(session: AsyncSession, cohort: Cohort) -> None:
    for c in await cohorts(session):
        c.enrolling = c.id == cohort.id
    await session.flush()


# ---------- месяцы цикла ----------

def _start(user: User, cohort: Cohort | None) -> date | None:
    return user.cycle_start or (cohort.start if cohort else None)


def month_start(start: date, month: int) -> date:
    return start + timedelta(weeks=WEEKS_PER_MONTH * (month - 1))


def cycle_month(user: User, cohort: Cohort | None, today: date) -> int | None:
    """Какой сейчас месяц её цикла (1–3); до старта — 1; после 12-й недели — None (платить не за что)."""
    start = _start(user, cohort)
    if start is None or today < start:
        return 1
    week = (today - start).days // 7 + 1
    if week > CYCLE_MONTHS * WEEKS_PER_MONTH:
        return None
    return (week - 1) // WEEKS_PER_MONTH + 1


def needed_month(user: User, cohort: Cohort | None, today: date) -> int:
    """По какой месяц должно быть оплачено сегодня. В первый понедельник 2-го и 3-го месяца ещё можно
    доплатить — в этот день хватает оплаты по прошлый месяц."""
    month = cycle_month(user, cohort, today)
    if month is None:
        return 0
    start = _start(user, cohort)
    if month > 1 and start and today == month_start(start, month):
        return month - 1
    return month


# ---------- цены ----------

async def price(session: AsyncSession, user: User, tariff: str, months: int) -> int | None:
    """Личная цена, иначе цена её потока; None — такого тарифа/срока нет."""
    personal = await session.scalar(select(PriceOverride.amount).where(
        PriceOverride.user_id == user.id, PriceOverride.tariff == tariff, PriceOverride.months == months))
    if personal is not None:
        return personal
    if not user.cohort_id:
        return None
    return await session.scalar(select(CohortPrice.amount).where(
        CohortPrice.cohort_id == user.cohort_id, CohortPrice.tariff == tariff, CohortPrice.months == months))


@dataclass
class Option:
    tariff: str
    months: int
    amount: int

    @property
    def code(self) -> str:
        return f"{self.tariff}:{self.months}"

    @property
    def label(self) -> str:
        return f"{TARIFF_NAMES[self.tariff]} · {months_word(self.months)} — {money(self.amount)}"


async def options(session: AsyncSession, user: User, month_from: int) -> list[Option]:
    """Что можно оплатить с месяца month_from: каждый тариф за 1 месяц; сразу за 3 — только с 1-го месяца."""
    out = []
    for tariff in TARIFFS:
        for months in (1, 3) if month_from == 1 else (1,):
            amount = await price(session, user, tariff, months)
            if amount is not None:
                out.append(Option(tariff, months, amount))
    return out


async def option(session: AsyncSession, user: User, code: str, month_from: int) -> Option | None:
    return next((o for o in await options(session, user, month_from) if o.code == code), None)


async def set_cohort_price(session: AsyncSession, cohort: Cohort, tariff: str, months: int, amount: int | None) -> None:
    row = await session.scalar(select(CohortPrice).where(
        CohortPrice.cohort_id == cohort.id, CohortPrice.tariff == tariff, CohortPrice.months == months))
    if amount is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(CohortPrice(cohort_id=cohort.id, tariff=tariff, months=months, amount=amount))
    else:
        row.amount = amount
    await session.flush()


async def set_personal_price(session: AsyncSession, user: User, tariff: str, months: int, amount: int | None) -> None:
    row = await session.scalar(select(PriceOverride).where(
        PriceOverride.user_id == user.id, PriceOverride.tariff == tariff, PriceOverride.months == months))
    if amount is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(PriceOverride(user_id=user.id, tariff=tariff, months=months, amount=amount))
    else:
        row.amount = amount
    await session.flush()


async def cohort_prices(session: AsyncSession, cohort: Cohort) -> dict[tuple[str, int], int]:
    rows = await session.scalars(select(CohortPrice).where(CohortPrice.cohort_id == cohort.id))
    return {(r.tariff, r.months): r.amount for r in rows}


async def personal_prices(session: AsyncSession) -> list[tuple[User, PriceOverride]]:
    rows = await session.execute(select(User, PriceOverride).join(PriceOverride, PriceOverride.user_id == User.id)
                                 .order_by(User.id, PriceOverride.tariff, PriceOverride.months))
    return [(u, p) for u, p in rows]


# ---------- доступ ----------

async def is_free(session: AsyncSession, user: User) -> bool:
    """Её тариф в её потоке бесплатный (например, «Приложение» в 1-м потоке) — доступ не закрываем."""
    return user.tariff is not None and await price(session, user, user.tariff, 1) == 0


async def due_month(session: AsyncSession, user: User | None, today: date, admin_ids: list[int],
                    assume_on: bool = False) -> int | None:
    """Месяц, который нужно оплатить, чтобы был доступ сегодня; None — доступ есть.
    assume_on — посчитать так, будто оплата уже включена (предпросмотр перед /payment on)."""
    if user is None or user.telegram_id in admin_ids or user.excluded_at is not None:
        return None
    if not assume_on and not await is_required(session):
        return None
    need = needed_month(user, await cohort_of(session, user), today)
    if need == 0 or user.paid_through >= need or await is_free(session, user):
        return None
    return max(need, user.paid_through + 1)


async def locked(session: AsyncSession, user: User | None, today: date, admin_ids: list[int]) -> bool:
    return await due_month(session, user, today, admin_ids) is not None


async def window(session: AsyncSession, user: User, today: date) -> tuple[int, date] | None:
    """Окно оплаты следующего месяца: (месяц, его первый понедельник), если сегодня пт–пн перед ним."""
    if user.onboarding_step != OnboardingStep.DONE or user.excluded_at is not None or await is_free(session, user):
        return None
    nxt = user.paid_through + 1
    start = _start(user, await cohort_of(session, user))
    if start is None or not 2 <= nxt <= CYCLE_MONTHS:
        return None
    monday = month_start(start, nxt)
    return (nxt, monday) if monday - timedelta(days=WINDOW_DAYS) <= today <= monday else None


# ---------- чеки и решения ----------

async def add_receipt(session: AsyncSession, user: User, opt: Option, month_from: int, file_id: str, kind: str) -> Payment:
    payment = Payment(user_id=user.id, cohort_id=user.cohort_id, tariff=opt.tariff, months=opt.months,
                      month_from=month_from, amount=opt.amount, file_id=file_id, file_kind=kind,
                      status=PaymentStatus.PENDING)
    session.add(payment)
    await session.flush()
    return payment


def approve(payment: Payment, user: User, admin_id: int | None) -> None:
    """Подтвердить: оплачено по месяц (month_from + months − 1), тариф — из оплаты."""
    payment.status, payment.decided_at, payment.decided_by = PaymentStatus.APPROVED, _now(), admin_id
    user.paid_through = min(CYCLE_MONTHS, max(user.paid_through, payment.month_from + payment.months - 1))
    user.tariff = payment.tariff
    user.no_team = payment.tariff == APP
    user.pay_choice = None


def reject(payment: Payment, admin_id: int) -> None:
    payment.status, payment.decided_at, payment.decided_by = PaymentStatus.REJECTED, _now(), admin_id


async def grant(session: AsyncSession, user: User, opt: Option, month_from: int, admin_id: int | None) -> Payment:
    """Без чека: бесплатный тариф или админ отметила оплату сама (/paid)."""
    payment = Payment(user_id=user.id, cohort_id=user.cohort_id, tariff=opt.tariff, months=opt.months,
                      month_from=month_from, amount=opt.amount, status=PaymentStatus.PENDING)
    session.add(payment)
    approve(payment, user, admin_id)
    await session.flush()
    return payment


async def pending(session: AsyncSession) -> list[tuple[Payment, User]]:
    rows = await session.execute(
        select(Payment, User).join(User, User.id == Payment.user_id)
        .where(Payment.status == PaymentStatus.PENDING).order_by(Payment.id)
    )
    return [(p, u) for p, u in rows]


async def pending_of(session: AsyncSession, user: User) -> list[Payment]:
    return [p for p, u in await pending(session) if u.id == user.id]


async def would_lock(session: AsyncSession, today: date, admin_ids: list[int]) -> list[tuple[User, int]]:
    """Кого закроет включение оплаты сегодня: (участница, месяц к оплате)."""
    users = await session.scalars(select(User).where(User.excluded_at.is_(None)).order_by(User.id))
    out = []
    for u in users:
        month = await due_month(session, u, today, admin_ids, assume_on=True)
        if month is not None:
            out.append((u, month))
    return out
