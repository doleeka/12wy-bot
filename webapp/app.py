"""FastAPI: API для Mini App и статика фронтенда. Работает в одном процессе с ботом (см. bot.main)."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import WebAppInitData
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings, local_today
from bot.models import Checkin, EssentialIntent, ExploreItem, OnboardingStep, Priority, User, WeeklyTactic, WheelOfBalance
from bot.services import checkins, cycle, scorecard, tactics, teams, wheel
from bot.services import today as today_svc
from bot.services import onboarding as svc
from bot.services.users import get_or_create_user
from bot.handlers.checkin import advice_for, send_report
from webapp.auth import telegram_user

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# До шага Eliminate колесо ещё можно переоценить
WHEEL_EDITABLE_STEPS = {OnboardingStep.WHEEL, OnboardingStep.EXPLORE}
# Список Explore можно дополнять и на шаге выбора (вернулась «дописать»)
EXPLORE_EDITABLE_STEPS = {OnboardingStep.EXPLORE, OnboardingStep.ELIMINATE}


class WheelIn(BaseModel):
    scores: dict[str, int]


class ExploreIn(BaseModel):
    text: str


class EliminateIn(BaseModel):
    selected: list[int]


class IntentIn(BaseModel):
    intents: dict[int, str]


class DailyIn(BaseModel):
    tactic_id: int
    done: bool


class WeekMarkIn(BaseModel):
    tactic_id: int
    done: bool | None  # None — снять отметку


class CheckinIn(BaseModel):
    week_start: str
    marks: dict[int, bool]


class PriorityIn(BaseModel):
    title: str
    intent: str


class TacticIn(BaseModel):
    text: str
    weeks: list[int] | None = None  # None — каждую неделю
    days: list[int] | None = None  # для еженедельных: 0 = пн … 6 = вс
    priority_id: int | None = None  # нужен при добавлении


class ChatTarget:
    """Позволяет переиспользовать чат-хендлеры (message.answer) для отправки из API."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id

    async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup)


def create_app(sessionmaker: async_sessionmaker, settings: Settings, bot: Bot | None = None) -> FastAPI:
    app = FastAPI(title="12 Week Year", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessionmaker = sessionmaker
    app.state.settings = settings
    app.state.bot = bot

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session
            await session.commit()

    async def current_user(
        tg: WebAppInitData = Depends(telegram_user), session: AsyncSession = Depends(db)
    ) -> User:
        return await get_or_create_user(session, tg.user)

    # ---------- профиль ----------

    @app.get("/api/me")
    async def me(user: User = Depends(current_user)) -> dict:
        return {
            "first_name": user.first_name,
            "step": user.onboarding_step.value,
            "cycle": user.cycle,
            "is_admin": user.telegram_id in settings.admin_ids,
        }

    # ---------- колесо баланса ----------

    def wheel_payload(scores: dict[str, int], comparison) -> dict:  # noqa: ANN001
        return {
            "spheres": [
                {"key": s.key, "title": s.title, "emoji": s.emoji, "core": s in wheel.CORE_SPHERES}
                for s in wheel.CORE_SPHERES + wheel.EXTRA_SPHERES
            ],
            "max_extra": wheel.MAX_EXTRA,
            "low_threshold": wheel.LOW_THRESHOLD,
            "scores": scores,
            "average": wheel.average(scores) if scores else None,
            "lows": [s.key for s, _ in wheel.low_spheres(scores)],
            "comparison": [{"key": s.key, "before": b, "after": a} for s, b, a in comparison],
        }

    @app.get("/api/wheel")
    async def get_wheel(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        scores = await wheel.get_scores(session, user)
        return wheel_payload(scores, await cycle.wheel_comparison(session, user))

    @app.put("/api/wheel")
    async def save_wheel(
        body: WheelIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        if user.onboarding_step not in WHEEL_EDITABLE_STEPS:
            raise HTTPException(status_code=409, detail="wheel_locked")
        scores = body.scores
        unknown = set(scores) - set(wheel.SPHERES)
        missing = {s.key for s in wheel.CORE_SPHERES} - set(scores)
        extras = [k for k in scores if k not in {s.key for s in wheel.CORE_SPHERES}]
        if unknown or missing or len(extras) > wheel.MAX_EXTRA:
            raise HTTPException(status_code=422, detail="bad_spheres")
        if any(not wheel.MIN_SCORE <= v <= wheel.MAX_SCORE for v in scores.values()):
            raise HTTPException(status_code=422, detail="bad_score")

        # сферы, которые убрали из колеса (снятая доп. сфера), удаляем
        await session.execute(
            delete(WheelOfBalance).where(
                WheelOfBalance.user_id == user.id,
                WheelOfBalance.cycle == user.cycle,
                WheelOfBalance.sphere.not_in(list(scores)),
            )
        )
        for key, value in scores.items():
            await wheel.save_score(session, user, key, value)

        user.onboarding_step = OnboardingStep.EXPLORE
        return wheel_payload(scores, await cycle.wheel_comparison(session, user))

    # ---------- Explore / Eliminate ----------

    async def explore_payload(session: AsyncSession, user: User) -> dict:
        items = await svc.get_explore_items(session, user)
        scores = await wheel.get_scores(session, user)
        return {
            "items": [{"id": i.id, "text": i.text, "selected": i.selected} for i in items],
            "min": svc.MIN_EXPLORE_ITEMS,
            "max": svc.MAX_EXPLORE_ITEMS,
            "pick": svc.PRIORITIES_COUNT,
            # подсказки: просадки колеса и прошлые приоритеты (в новом цикле)
            "lows": [f"{s.emoji} {s.title}" for s, _ in wheel.low_spheres(scores)],
            "previous": [p.title for p in await cycle.previous_priorities(session, user)],
        }

    def require_step(user: User, *steps: OnboardingStep) -> None:
        if user.onboarding_step not in steps:
            raise HTTPException(status_code=409, detail="wrong_step")

    def before_start(user: User) -> bool:
        """Подтвердила план, но цикл ещё не начался — план можно менять."""
        return (
            user.onboarding_step == OnboardingStep.DONE
            and user.cycle_start is not None
            and local_today(settings) < user.cycle_start
        )

    def require_plan_editable(user: User, *steps: OnboardingStep) -> None:
        if user.onboarding_step not in steps and not before_start(user):
            raise HTTPException(status_code=409, detail="plan_locked")

    @app.get("/api/explore")
    async def get_explore(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        return await explore_payload(session, user)

    @app.post("/api/explore")
    async def add_explore(
        body: ExploreIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_step(user, *EXPLORE_EDITABLE_STEPS)
        texts = svc.parse_explore_text(body.text)
        if not texts:
            raise HTTPException(status_code=422, detail="empty")
        if len(await svc.get_explore_items(session, user)) >= svc.MAX_EXPLORE_ITEMS:
            raise HTTPException(status_code=422, detail="limit")
        added = await svc.add_explore_items(session, user, texts)
        return await explore_payload(session, user) | {"added": added}

    @app.put("/api/explore/{item_id}")
    async def edit_explore(
        item_id: int, body: ExploreIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        """Исправить пункт прямо в карточке (до выбора трёх приоритетов)."""
        require_step(user, *EXPLORE_EDITABLE_STEPS)
        text = body.text.strip()[: svc.MAX_ITEM_LEN]
        if not text:
            raise HTTPException(status_code=422, detail="empty")
        item = await session.get(ExploreItem, item_id)
        if item is None or item.user_id != user.id or item.cycle != user.cycle:
            raise HTTPException(status_code=404, detail="no_item")
        item.text = text
        await session.flush()
        return await explore_payload(session, user)

    @app.delete("/api/explore/{item_id}")
    async def delete_explore(item_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *EXPLORE_EDITABLE_STEPS)
        await svc.delete_explore_item(session, user, item_id)
        return await explore_payload(session, user)

    @app.post("/api/explore/done")
    async def explore_done(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *EXPLORE_EDITABLE_STEPS)
        if len(await svc.get_explore_items(session, user)) < svc.MIN_EXPLORE_ITEMS:
            raise HTTPException(status_code=422, detail="not_enough")
        user.onboarding_step = OnboardingStep.ELIMINATE
        return await explore_payload(session, user)

    @app.put("/api/eliminate")
    async def eliminate(
        body: EliminateIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        # до сохранения «зачем» выбор ещё можно пересмотреть
        require_step(user, OnboardingStep.ELIMINATE, OnboardingStep.INTENT)
        try:
            await svc.set_selection(session, user, body.selected)
        except ValueError:
            raise HTTPException(status_code=422, detail="pick_exactly_3") from None
        await svc.confirm_priorities(session, user)
        return await intent_payload(session, user)

    # ---------- Essential intent ----------

    async def intent_payload(session: AsyncSession, user: User) -> dict:
        priorities = await svc.get_priorities(session, user)
        items = await svc.get_explore_items(session, user)
        return {
            "priorities": [
                {"id": p.id, "position": p.position, "title": p.title, "intent": p.intent.text if p.intent else ""}
                for p in priorities
            ],
            "not_now": [i.text for i in items if not i.selected],
            "min_len": svc.MIN_INTENT_LEN,
        }

    @app.get("/api/intent")
    async def get_intent(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        return await intent_payload(session, user)

    @app.put("/api/intent")
    async def save_intent(
        body: IntentIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.INTENT, OnboardingStep.TACTICS)
        try:
            await svc.set_intents(session, user, body.intents)
        except ValueError:
            raise HTTPException(status_code=422, detail="intent_required") from None
        return await intent_payload(session, user)

    # ---------- Тактики и 12-недельный план ----------

    async def plan_payload(session: AsyncSession, user: User) -> dict:
        priorities = await svc.get_priorities(session, user)
        all_tactics = []
        out = []
        for p in priorities:
            active = [t for t in sorted(p.tactics, key=lambda t: t.id) if t.is_active]
            all_tactics += active
            out.append(
                {
                    "id": p.id,
                    "position": p.position,
                    "title": p.title,
                    "intent": p.intent.text if p.intent else "",
                    "tactics": [
                        {
                            "id": t.id,
                            "text": t.text,
                            "weeks": t.weeks,
                            "days": t.days,
                            "label": tactics.weeks_label(t.weeks, t.days),
                            # контрольная точка / разовая тактика и так «да или нет» в свою неделю
                            "measurable": t.weeks is not None or svc.is_measurable(t.text),
                        }
                        for t in active
                    ],
                }
            )
        team = await teams.get_team_of(session, user)
        start = user.cycle_start or svc.cycle_start_for(local_today(settings), settings.cycle_start)
        return {
            "step": user.onboarding_step.value,
            "priorities": out,
            "load": tactics.load_per_week(all_tactics),
            "max_per_priority": tactics.MAX_TACTICS_PER_PRIORITY,
            "recommended": [tactics.RECOMMENDED_MIN, tactics.RECOMMENDED_MAX],
            "weeks_total": len(tactics.ALL_WEEKS),
            "cycle_start": start.isoformat(),
            "team": teams.team_name(team) if team else None,
            # план можно менять: во время онбординга и после подтверждения — пока цикл не начался
            "editable": user.onboarding_step == OnboardingStep.TACTICS or before_start(user),
        }

    async def own_priority(session: AsyncSession, user: User, priority_id: int) -> Priority:
        priority = await session.get(Priority, priority_id)
        if priority is None or priority.user_id != user.id or priority.cycle != user.cycle:
            raise HTTPException(status_code=404, detail="no_priority")
        return priority

    async def own_tactic(session: AsyncSession, user: User, tactic_id: int) -> WeeklyTactic:
        tactic = await session.get(WeeklyTactic, tactic_id)
        if tactic is None or tactic.user_id != user.id or not tactic.is_active:
            raise HTTPException(status_code=404, detail="no_tactic")
        return tactic

    def clean_tactic(body: TacticIn) -> tuple[str, list[int] | None, list[int] | None]:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="empty")
        if len(text) > tactics.MAX_TACTIC_LEN:
            raise HTTPException(status_code=422, detail="too_long")
        try:
            weeks = tactics.normalize_weeks(body.weeks)
        except ValueError:
            raise HTTPException(status_code=422, detail="bad_weeks") from None
        if weeks is not None:
            return text, weeks, None  # дни недели — только у еженедельных тактик
        try:
            days = tactics.normalize_days(body.days)
        except ValueError:
            raise HTTPException(status_code=422, detail="bad_days") from None
        if days is None:
            raise HTTPException(status_code=422, detail="pick_days")
        return text, None, days

    @app.put("/api/priorities/{priority_id}")
    async def edit_priority(
        priority_id: int, body: PriorityIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        """Переименовать цель и поправить её «зачем»."""
        require_plan_editable(user, OnboardingStep.TACTICS)
        priority = await own_priority(session, user, priority_id)
        title, intent = body.title.strip(), body.intent.strip()
        if not title or len(title) > svc.MAX_ITEM_LEN:
            raise HTTPException(status_code=422, detail="bad_title")
        if len(intent) < svc.MIN_INTENT_LEN:
            raise HTTPException(status_code=422, detail="intent_required")
        priority.title = title
        existing = await session.scalar(select(EssentialIntent).where(EssentialIntent.priority_id == priority.id))
        if existing is None:
            session.add(EssentialIntent(priority_id=priority.id, text=intent))
        else:
            existing.text = intent
        await session.flush()
        return await plan_payload(session, user)

    @app.post("/api/plan/reselect")
    async def reselect_priorities(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        """Выбрать 3 приоритета заново из списка Explore. Тактики старых приоритетов удалятся при выборе."""
        require_plan_editable(user, OnboardingStep.INTENT, OnboardingStep.TACTICS)
        user.onboarding_step = OnboardingStep.ELIMINATE
        return await explore_payload(session, user)

    @app.get("/api/plan")
    async def get_plan(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        return await plan_payload(session, user)

    @app.post("/api/tactics")
    async def add_tactic(
        body: TacticIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.TACTICS)
        priority = await own_priority(session, user, body.priority_id or 0)
        text, weeks, days = clean_tactic(body)
        count = await session.scalar(
            select(func.count(WeeklyTactic.id)).where(WeeklyTactic.priority_id == priority.id, WeeklyTactic.is_active)
        )
        if count >= tactics.MAX_TACTICS_PER_PRIORITY:
            raise HTTPException(status_code=422, detail="limit")
        session.add(WeeklyTactic(priority_id=priority.id, user_id=user.id, text=text, weeks=weeks, days=days))
        await session.flush()
        return await plan_payload(session, user)

    @app.put("/api/tactics/{tactic_id}")
    async def edit_tactic(
        tactic_id: int, body: TacticIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.TACTICS)
        tactic = await own_tactic(session, user, tactic_id)
        tactic.text, tactic.weeks, tactic.days = clean_tactic(body)
        await session.flush()
        return await plan_payload(session, user)

    @app.delete("/api/tactics/{tactic_id}")
    async def delete_tactic(
        tactic_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.TACTICS)
        tactic = await own_tactic(session, user, tactic_id)
        if user.onboarding_step == OnboardingStep.DONE:
            # в подтверждённом плане у каждой цели остаётся хотя бы одна тактика
            left = await session.scalar(
                select(func.count(WeeklyTactic.id)).where(
                    WeeklyTactic.priority_id == tactic.priority_id, WeeklyTactic.is_active
                )
            )
            if left <= 1:
                raise HTTPException(status_code=422, detail="last_tactic")
        await session.delete(tactic)
        await session.flush()
        return await plan_payload(session, user)

    # ---------- Чек-ин и scorecard ----------

    CHECKIN_STEPS = (OnboardingStep.DONE, OnboardingStep.FINISHED)

    @app.get("/api/checkin")
    async def get_checkin(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = local_today(settings)
        week = checkins.default_week(user, today)
        n = checkins.checkin_week_number(user, week)
        if n is None:
            not_started = user.cycle_start is not None and today < user.cycle_start
            return {
                "status": "not_started" if not_started else "over",
                "cycle_start": user.cycle_start.isoformat() if user.cycle_start else None,
            }
        week_tactics = await checkins.tactics_for_week(session, user, week)
        marks = await checkins.get_marks(session, user, week)
        done, planned, unmarked = checkins.summarize(week_tactics, marks)
        daily = await today_svc.daily_marks(session, user, week)
        return {
            "status": "active",
            "week_number": n,
            "week_start": week.isoformat(),
            "week_end": (week + timedelta(days=6)).isoformat(),
            "tactics": [
                {
                    "id": t.id,
                    "text": t.text,
                    "label": tactics.weeks_label(t.weeks, t.days),
                    "priority_position": t.priority.position,
                    "priority_title": t.priority.title,
                    "done": marks.get(t.id),
                    # все дни недели отмечены во вкладке «Сегодня» — подставим «сделано»
                    "suggested": today_svc.suggested_done(t, week, daily),
                }
                for t in week_tactics
            ],
            "result": result_payload(done, planned) if planned and not unmarked else None,
        }

    # ---------- Вкладка «Сегодня» ----------

    def item(t: WeeklyTactic, **extra) -> dict:
        return {
            "id": t.id,
            "text": t.text,
            "label": tactics.weeks_label(t.weeks, t.days),
            "priority_position": t.priority.position,
            "priority_title": t.priority.title,
        } | extra

    @app.get("/api/today")
    async def get_today(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = local_today(settings)
        all_tactics = await checkins.active_tactics(session, user)
        base = {
            "today": today.isoformat(),
            "weekday": today.weekday(),
            "cycle_start": user.cycle_start.isoformat() if user.cycle_start else None,
            "priorities": len({t.priority_id for t in all_tactics}),
            "tactics": len(all_tactics),
            "editable": before_start(user),
        }
        n = scorecard.week_number(user.cycle_start, today)
        if n is None:
            if user.cycle_start and today < user.cycle_start:
                week1 = [t for t in all_tactics if t.in_week(1)]
                return base | {
                    "status": "not_started",
                    "days_until": (user.cycle_start - today).days,
                    "week1": [item(t) for t in week1],
                }
            return base | {"status": "over"}

        week = scorecard.week_start(today)
        week_tactics = [t for t in all_tactics if t.in_week(n)]
        checkin = await checkins.get_marks(session, user, week)
        daily = await today_svc.daily_marks(session, user, week)
        states = {t.id: today_svc.week_state(t, week, checkin, daily) for t in week_tactics}
        done = sum(1 for v in states.values() if v)
        checked = week_checked(user, week)
        return base | {
            "status": "active",
            "week_number": n,
            "week_start": week.isoformat(),
            "week_end": (week + timedelta(days=6)).isoformat(),
            # сегодня: еженедельные действия в свои дни — галочка «сделано сегодня»
            "today_items": [
                item(t, done=today in daily.get(t.id, set()), week_done=states[t.id])
                for t in today_svc.scheduled_on(week_tactics, today)
            ],
            # на этой неделе: разовые, контрольные точки и еженедельные без дней — отметка за неделю
            "week_items": [
                item(t, done=checkin.get(t.id), week_done=states[t.id]) for t in week_tactics if not today_svc.is_daily(t)
            ],
            "week_progress": {
                "done": done,
                "planned": len(week_tactics),
                "percent": scorecard.percent(done, len(week_tactics)),
            },
            # чек-ин сохранён (здесь или в чате) — итог недели зафиксирован, отметки меняются только через чек-ин
            "checkin_done": checked,
        }

    def week_checked(user: User, week: date) -> bool:
        return user.last_reported_week is not None and user.last_reported_week >= week

    def require_open_week(user: User, week: date) -> None:
        if week_checked(user, week):
            raise HTTPException(status_code=409, detail="week_checked")

    @app.post("/api/today/daily")
    async def mark_daily(body: DailyIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = local_today(settings)
        tactic = await own_tactic(session, user, body.tactic_id)
        n = scorecard.week_number(user.cycle_start, today)
        if n is None or not tactic.in_week(n) or not today_svc.scheduled_on([tactic], today):
            raise HTTPException(status_code=422, detail="not_today")
        require_open_week(user, scorecard.week_start(today))
        await today_svc.set_daily(session, user, tactic, today, body.done)
        return await get_today(user, session)

    @app.post("/api/today/week")
    async def mark_week(body: WeekMarkIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = local_today(settings)
        week = scorecard.week_start(today)
        tactic = await own_tactic(session, user, body.tactic_id)
        if tactic.id not in {t.id for t in await checkins.tactics_for_week(session, user, week)}:
            raise HTTPException(status_code=422, detail="not_this_week")
        require_open_week(user, week)
        if body.done is None:
            await today_svc.clear_week_mark(session, user, tactic.id, week)
        else:
            await checkins.set_mark(session, user, tactic.id, week, body.done)
        return await get_today(user, session)

    def result_payload(done: int, planned: int) -> dict:
        value = scorecard.percent(done, planned) or 0
        return {
            "percent": value,
            "level": scorecard.level(value),
            "rating": scorecard.rating(value),
            "done": done,
            "planned": planned,
            "advice": advice_for(value),
        }

    @app.put("/api/checkin")
    async def save_checkin(
        body: CheckinIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_step(user, *CHECKIN_STEPS)
        try:
            week = date.fromisoformat(body.week_start)
        except ValueError:
            raise HTTPException(status_code=422, detail="bad_week") from None
        if not checkins.can_check_in(user, week, local_today(settings)):
            raise HTTPException(status_code=409, detail="week_closed")
        week_tactics = await checkins.tactics_for_week(session, user, week)
        if not week_tactics:
            raise HTTPException(status_code=422, detail="no_tactics")
        if set(body.marks) != {t.id for t in week_tactics}:
            raise HTTPException(status_code=422, detail="mark_all")
        for tactic_id, done in body.marks.items():
            await checkins.set_mark(session, user, tactic_id, week, done)
        done = sum(1 for v in body.marks.values() if v)
        result = result_payload(done, len(week_tactics))
        if app.state.bot is not None:
            try:  # отчёт по флагу send_report — один раз за неделю
                await send_report(app.state.bot, session, user, week, result["percent"], settings)
            except TelegramAPIError as e:
                log.warning("Не удалось отправить отчёт %s: %s", user.telegram_id, e)
        else:
            user.last_reported_week = max(user.last_reported_week or week, week)
        return result

    @app.get("/api/scorecard")
    async def get_scorecard(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = local_today(settings)
        all_tactics = await checkins.active_tactics(session, user)
        weeks = []
        if user.cycle_start:
            for n in tactics.ALL_WEEKS:
                week = user.cycle_start + timedelta(days=7 * (n - 1))
                value = await scorecard.week_percent(session, user, week)
                weeks.append(
                    {
                        "n": n,
                        "week_start": week.isoformat(),
                        "planned": len(tactics.for_week(all_tactics, n)),
                        "percent": value,
                        "level": scorecard.level(value) if value is not None else None,
                        "future": week > scorecard.week_start(today),
                    }
                )
        done_total = 0
        if user.cycle_start:
            done_total = await session.scalar(
                select(func.count(Checkin.id)).where(
                    Checkin.user_id == user.id,
                    Checkin.done,
                    Checkin.week_start >= user.cycle_start,
                    Checkin.week_start < user.cycle_start + timedelta(days=84),
                )
            )
        marked = [w["percent"] for w in weeks if w["percent"] is not None]
        avg = round(sum(marked) / len(marked)) if marked else None
        return {
            "cycle_start": user.cycle_start.isoformat() if user.cycle_start else None,
            "current_week": scorecard.week_number(user.cycle_start, today),
            "weeks": weeks,
            "average": avg,
            "average_level": scorecard.level(avg) if avg is not None else None,
            "done_total": done_total,
            "thresholds": {"good": scorecard.EXCELLENT, "warning": scorecard.GOOD},
        }

    @app.post("/api/plan/confirm")
    async def confirm_plan(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        """Итоговый экран → «готова»: старт цикла с ближайшего понедельника и распределение в команду."""
        require_step(user, OnboardingStep.TACTICS)
        priorities = await svc.get_priorities(session, user)
        if any(not any(t.is_active for t in p.tactics) for p in priorities):
            raise HTTPException(status_code=422, detail="empty_priority")
        user.onboarding_step = OnboardingStep.DONE
        user.onboarding_position = None
        user.is_ready = True
        user.cycle_start = svc.cycle_start_for(local_today(settings), settings.cycle_start)
        await session.flush()
        if app.state.bot is not None:
            from bot.handlers.teams import join_team

            try:  # в чат — сообщение о команде; сокомандницам и админам — уведомления
                await join_team(ChatTarget(app.state.bot, user.telegram_id), app.state.bot, session, user, settings)
            except TelegramAPIError as e:
                log.warning("Не удалось отправить сообщение о команде %s: %s", user.telegram_id, e)
                await teams.assign_to_team(session, user)
        else:
            await teams.assign_to_team(session, user)
        return await plan_payload(session, user)

    # ---------- фронтенд ----------

    # Метка версии в ссылках на app.js/style.css: WebView Telegram кэширует статику,
    # и без метки после обновления участница видела бы новую разметку со старым кодом.
    version = hashlib.sha256(
        b"".join((STATIC_DIR / name).read_bytes() for name in ("app.js", "style.css", "index.html"))
    ).hexdigest()[:10]
    index_html = (
        (STATIC_DIR / "index.html")
        .read_text(encoding="utf-8")
        .replace("/static/app.js", f"/static/app.js?v={version}")
        .replace("/static/style.css", f"/static/style.css?v={version}")
    )

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
