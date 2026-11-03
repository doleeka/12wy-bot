"""Завершение 12-недельного цикла: итоги (13-я неделя) и старт нового цикла."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import ArchivedCheckin, ArchivedDailyMark, Checkin, DailyMark, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import checkins, payments, scorecard, wheel

CYCLE_DAYS = scorecard.CYCLE_WEEKS * 7


def cycle_end(user: User) -> date | None:
    """Первый день после 12-й недели (понедельник 13-й недели)."""
    return user.cycle_start + timedelta(days=CYCLE_DAYS) if user.cycle_start else None


def is_cycle_over(user: User, today: date) -> bool:
    end = cycle_end(user)
    return end is not None and today >= end


def maybe_finish_cycle(user: User, today: date) -> bool:
    """Переводит участницу в «итоги», если её 12 недель прошли. True — если перевели сейчас."""
    if user.onboarding_step == OnboardingStep.DONE and is_cycle_over(user, today):
        user.onboarding_step = OnboardingStep.FINISHED
        return True
    return False


@dataclass
class CycleStats:
    weeks: list[int | None]  # % по неделям 1..12, None — неделя не отмечена

    @property
    def marked(self) -> list[int]:
        return [w for w in self.weeks if w is not None]

    @property
    def average(self) -> int | None:
        return round(sum(self.marked) / len(self.marked)) if self.marked else None

    @property
    def excellent_weeks(self) -> int:
        return sum(1 for w in self.marked if w >= scorecard.EXCELLENT)

    @property
    def best(self) -> tuple[int, int] | None:
        """(номер недели, %) лучшей недели; при равенстве — более поздняя."""
        candidates = [(pct, n) for n, pct in enumerate(self.weeks, 1) if pct is not None]
        if not candidates:
            return None
        pct, n = max(candidates)
        return n, pct


async def cycle_stats(session: AsyncSession, user: User) -> CycleStats:
    """% по неделям цикла. Запланировано = тактики текущего цикла (они активны, пока не начат новый)."""
    if not user.cycle_start:
        return CycleStats([None] * scorecard.CYCLE_WEEKS)
    return CycleStats([await scorecard.week_percent(session, user, user.cycle_start + timedelta(weeks=n))
                       for n in range(scorecard.CYCLE_WEEKS)])


async def previous_priorities(session: AsyncSession, user: User) -> list[Priority]:
    if user.cycle <= 1:
        return []
    rows = await session.scalars(
        select(Priority).where(Priority.user_id == user.id, Priority.cycle == user.cycle - 1).order_by(Priority.position)
    )
    return list(rows)


async def start_new_cycle(session: AsyncSession, user: User) -> None:
    """Новый цикл: онбординг заново с колеса баланса. Команда сохраняется."""
    await session.execute(
        update(WeeklyTactic).where(WeeklyTactic.user_id == user.id, WeeklyTactic.is_active).values(is_active=False)
    )
    user.cycle += 1
    user.cycle_start = None
    user.onboarding_step = OnboardingStep.WHEEL
    user.onboarding_position = None
    user.paid_through = 0  # новый цикл — в текущем наборе, оплата заново
    await payments.enroll(session, user)
    await session.flush()


async def wheel_comparison(session: AsyncSession, user: User) -> list[tuple[wheel.Sphere, int, int]]:
    """(сфера, было, стало) для сфер, оценённых и в прошлом, и в текущем цикле."""
    if user.cycle <= 1:
        return []
    now = await wheel.get_scores(session, user)
    before = await wheel.get_scores(session, user, cycle=user.cycle - 1)
    return [(s, before[s.key], v) for s, v in wheel.ordered_scores(now) if s.key in before]


# План можно менять до старта и в течение цикла; прошлые недели защищены снимками.


def plan_edit_until(user: User) -> date | None:
    """Последний день, когда подтверждённый план ещё можно менять (включительно)."""
    if user.onboarding_step != OnboardingStep.DONE or user.cycle_start is None:
        return None
    return user.cycle_start + timedelta(days=CYCLE_DAYS - 1)


def plan_editable(user: User, today: date) -> bool:
    until = plan_edit_until(user)
    return until is not None and today <= until


def start_test_cycle(user: User, today: date, week: int = 1) -> date:
    """Режим проверки для админа: сдвигает старт так, чтобы сегодня шла неделя week (1–12)."""
    if not 1 <= week <= scorecard.CYCLE_WEEKS:
        raise ValueError("Неделя — от 1 до 12")
    user.cycle_start = scorecard.week_start(today) - timedelta(days=7 * (week - 1))
    user.last_reported_week = None
    if user.test_mode_since is None:  # повторный /testcycle N не сдвигает начало режима
        user.test_mode_since = datetime.now(timezone.utc).replace(tzinfo=None)
    return user.cycle_start


def real_start_for(today: date, cohort_start: date | None) -> date:
    """Настоящий старт при выходе из проверки: идёт общий цикл — его дата (а не следующий понедельник)."""
    if cohort_start and cohort_start <= today < cohort_start + timedelta(weeks=scorecard.CYCLE_WEEKS):
        return cohort_start
    nearest = today + timedelta(days=(7 - today.weekday()) % 7)
    return max(nearest, cohort_start) if cohort_start else nearest


def in_test_mode(user: User, real_start: date) -> bool:
    return user.test_mode_since is not None or (user.cycle_start is not None and user.cycle_start != real_start)


@dataclass
class TestMarks:
    checkins: list[Checkin]
    daily: list[DailyMark]

    def by_week(self) -> list[tuple[date, int, int]]:
        """(понедельник, отмечено, из них «сделано») — для предпросмотра."""
        weeks: dict[date, list[int]] = {}
        for c in self.checkins:
            w = weeks.setdefault(c.week_start, [0, 0])
            w[0] += 1
            w[1] += int(c.done)
        return [(d, n, done) for d, (n, done) in sorted(weeks.items())]


async def find_test_marks(session: AsyncSession, user: User, real_start: date) -> TestMarks:
    """Тестовые отметки: сделанные в режиме проверки (по времени создания/правки) — в том числе в неделе,
    совпавшей с настоящей, — и всё до настоящего старта. Если начало режима неизвестно (режим включён
    до появления этой отметки времени), тестовыми считаются все отметки аккаунта."""
    since = user.test_mode_since
    cq = select(Checkin).where(Checkin.user_id == user.id)
    dq = select(DailyMark).where(DailyMark.user_id == user.id)
    if since is not None:
        cq = cq.where(or_(Checkin.week_start < real_start, Checkin.created_at >= since, Checkin.updated_at >= since))
        dq = dq.where(or_(DailyMark.day < real_start, DailyMark.created_at >= since))
    checkins = list(await session.scalars(cq.order_by(Checkin.week_start, Checkin.id)))
    daily = list(await session.scalars(dq.order_by(DailyMark.day, DailyMark.id)))
    return TestMarks(checkins, daily)


async def end_test_cycle(session: AsyncSession, user: User, real_start: date) -> TestMarks:
    """Выход из режима проверки: тестовые отметки переносятся в архив (все поля сохраняются, ничего
    не удаляется безвозвратно), старт — настоящий. Даты других участниц не трогаем."""
    marks = await find_test_marks(session, user, real_start)
    for c in marks.checkins:
        session.add(ArchivedCheckin(
            original_id=c.id, user_id=c.user_id, tactic_id=c.tactic_id, week_start=c.week_start,
            week_number=c.week_number, done=c.done, created_at=c.created_at, updated_at=c.updated_at,
            cycle_start_was=user.cycle_start, reason="testcycle",
        ))
    for d in marks.daily:
        session.add(ArchivedDailyMark(
            original_id=d.id, user_id=d.user_id, tactic_id=d.tactic_id, day=d.day, created_at=d.created_at, reason="testcycle",
        ))
    await session.flush()  # сначала копия в архиве — потом убираем из живых таблиц, в одной транзакции
    for row in marks.checkins + marks.daily:
        await session.delete(row)
    user.cycle_start = real_start
    user.last_reported_week = None
    user.test_mode_since = None
    await session.flush()
    return marks


async def has_marks_before(session: AsyncSession, user: User, day: date) -> bool:
    """Отмечала ли что-то раньше этой даты — то есть правда проходила недели до неё."""
    from bot.models import DailyMark  # noqa: PLC0415

    checkin = await session.scalar(select(Checkin.id).where(Checkin.user_id == user.id, Checkin.week_start < day).limit(1))
    daily = await session.scalar(select(DailyMark.id).where(DailyMark.user_id == user.id, DailyMark.day < day).limit(1))
    return checkin is not None or daily is not None


async def shift_cycle_start(session: AsyncSession, user: User, start: date) -> None:
    """Тот же цикл и тот же план, но неделя 1 — с другой даты (пришла поздно, а неделями до неё не жила).
    Снимки плана тех недель, что она не проходила, к новым неделям не применяем: убираем их номера в минус
    (запись остаётся)."""
    from bot.models import PlanWeekSnapshot  # noqa: PLC0415

    shelved = list(await session.scalars(select(PlanWeekSnapshot).where(
        PlanWeekSnapshot.user_id == user.id, PlanWeekSnapshot.cycle == user.cycle, PlanWeekSnapshot.week_number > 0)))
    used = set(await session.scalars(select(PlanWeekSnapshot.week_number).where(
        PlanWeekSnapshot.user_id == user.id, PlanWeekSnapshot.cycle == user.cycle, PlanWeekSnapshot.week_number < 0)))
    for snap in shelved:
        n = -snap.week_number
        while n in used:  # уже переносили раньше — не сталкиваемся с прежними
            n -= 100
        used.add(n)
        snap.week_number = n
    user.cycle_start = start
    user.last_reported_week = None
    await session.flush()


async def carry_plan_to_new_cycle(session: AsyncSession, user: User, start: date | None = None) -> None:
    """Перевод в другой поток, когда цикл уже шёл: прежний цикл остаётся в истории как есть (отметки, снимки
    недель, колесо), а в новый цикл копируется её план — колесо, желания, приоритеты с «зачем» и действия.
    Онбординг заново не нужен; старт нового цикла задаёт поток. Отметки с даты start и позже (уже недели нового
    потока) переезжают к скопированным действиям — их не теряем."""
    from bot.models import DailyMark, EssentialIntent, ExploreItem, Priority, WheelOfBalance  # noqa: PLC0415

    old, new = user.cycle, user.cycle + 1
    for w in list(await session.scalars(select(WheelOfBalance).where(
            WheelOfBalance.user_id == user.id, WheelOfBalance.cycle == old))):
        session.add(WheelOfBalance(user_id=user.id, cycle=new, sphere=w.sphere, score=w.score))
    explore_ids = {}
    for e in list(await session.scalars(select(ExploreItem).where(
            ExploreItem.user_id == user.id, ExploreItem.cycle == old).order_by(ExploreItem.id))):
        copy = ExploreItem(user_id=user.id, cycle=new, text=e.text, selected=e.selected)
        session.add(copy)
        await session.flush()
        explore_ids[e.id] = copy.id
    for p in list(await session.scalars(select(Priority).where(
            Priority.user_id == user.id, Priority.cycle == old).order_by(Priority.position))):
        copy = Priority(user_id=user.id, cycle=new, position=p.position, title=p.title,
                        explore_item_id=explore_ids.get(p.explore_item_id))
        session.add(copy)
        await session.flush()
        intent = await session.scalar(select(EssentialIntent).where(EssentialIntent.priority_id == p.id))
        if intent is not None:
            session.add(EssentialIntent(priority_id=copy.id, text=intent.text))
        for t in list(await session.scalars(select(WeeklyTactic).where(
                WeeklyTactic.priority_id == p.id, WeeklyTactic.is_active))):
            new_t = WeeklyTactic(priority_id=copy.id, user_id=user.id, text=t.text, weeks=t.weeks, days=t.days)
            session.add(new_t)
            await session.flush()
            t.is_active = False  # прежние действия — история прошлого цикла
            if start is not None:
                await session.execute(update(Checkin).where(Checkin.tactic_id == t.id, Checkin.week_start >= start)
                                      .values(tactic_id=new_t.id))
                await session.execute(update(DailyMark).where(DailyMark.tactic_id == t.id, DailyMark.day >= start)
                                      .values(tactic_id=new_t.id))
    user.cycle = new
    user.last_reported_week = None
    await session.flush()
