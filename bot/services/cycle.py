"""Завершение 12-недельного цикла: итоги (13-я неделя) и старт нового цикла."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import ArchivedCheckin, ArchivedDailyMark, Checkin, DailyMark, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import checkins, scorecard, wheel

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
    tactics = await checkins.active_tactics(session, user)
    tactic_ids = {t.id for t in tactics}
    weeks: list[int | None] = [None] * scorecard.CYCLE_WEEKS
    if user.cycle_start is None or not tactics:
        return CycleStats(weeks)
    rows = await session.scalars(
        select(Checkin).where(
            Checkin.user_id == user.id,
            Checkin.week_start >= user.cycle_start,
            Checkin.week_start < cycle_end(user),
        )
    )
    done_by_week: dict[int, int] = {}
    marked_weeks: set[int] = set()
    for row in rows:
        if row.tactic_id not in tactic_ids:
            continue
        n = (row.week_start - user.cycle_start).days // 7
        marked_weeks.add(n)
        done_by_week[n] = done_by_week.get(n, 0) + int(row.done)
    for n in marked_weeks:
        planned = sum(1 for t in tactics if t.in_week(n + 1))
        weeks[n] = scorecard.percent(done_by_week.get(n, 0), planned)
    return CycleStats(weeks)


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
    await session.flush()


async def wheel_comparison(session: AsyncSession, user: User) -> list[tuple[wheel.Sphere, int, int]]:
    """(сфера, было, стало) для сфер, оценённых и в прошлом, и в текущем цикле."""
    if user.cycle <= 1:
        return []
    now = await wheel.get_scores(session, user)
    before = await wheel.get_scores(session, user, cycle=user.cycle - 1)
    return [(s, before[s.key], v) for s, v in wheel.ordered_scores(now) if s.key in before]


# План (цели, «зачем», действия, расписание) можно менять до старта и в первые 3 календарных дня цикла
# по местному времени: старт 05.10 → правки до 07.10 включительно, с 08.10 план закрыт. Видение — всегда.
PLAN_EDIT_GRACE_DAYS = 3


def plan_edit_until(user: User) -> date | None:
    """Последний день, когда подтверждённый план ещё можно менять (включительно)."""
    if user.onboarding_step != OnboardingStep.DONE or user.cycle_start is None:
        return None
    return user.cycle_start + timedelta(days=PLAN_EDIT_GRACE_DAYS - 1)


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
