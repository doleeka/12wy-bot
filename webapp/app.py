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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text as sqltext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings, local_today
from bot.models import Checkin, DailyMark, EssentialIntent, ExploreItem, OnboardingStep, Priority, User, WeeklyTactic, WheelOfBalance
from bot.services import checkins, cycle, scorecard, tactics, teams, wheel, plan_history
from bot.services import today as today_svc
from bot.services import vision as vision_svc
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
    replace: bool = False
    first_action: str = ""


class VisionIn(BaseModel):
    work: str = ""
    life: str = ""
    me: str = ""
    main: str = ""


class ReflectionIn(BaseModel):
    closer: str = ""
    changed: str = ""
    next: str = ""


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


def create_app(sessionmaker: async_sessionmaker, settings: Settings, bot: Bot | None = None, *, sandbox: bool = False) -> FastAPI:
    app = FastAPI(title="12 Week Year", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessionmaker = sessionmaker
    app.state.settings = settings
    app.state.bot = None if sandbox else bot

    def today_for(user: User) -> date:
        today = local_today(settings)
        if sandbox and user.cycle_start and user.sandbox_week:
            return user.cycle_start + timedelta(weeks=user.sandbox_week - 1, days=today.weekday())
        return today

    async def db(request: Request) -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            if request.method in {"POST", "PUT", "DELETE"}:
                await session.execute(sqltext("BEGIN IMMEDIATE"))
            yield session
            await session.commit()

    async def current_user(
        tg: WebAppInitData = Depends(telegram_user), session: AsyncSession = Depends(db)
    ) -> User:
        if sandbox and tg.user.id not in settings.admin_ids:
            raise HTTPException(403, "admin_only")
        user = await get_or_create_user(session, tg.user)
        if user.excluded_at is not None and user.telegram_id not in settings.admin_ids:
            raise HTTPException(status_code=403, detail="excluded")  # исключена из сообщества (/remove)
        if sandbox:
            user.no_team = True
            if user.sandbox_week is None:
                user.sandbox_week = scorecard.week_number(user.cycle_start, local_today(settings)) or 1
            if user.sandbox_checked_weeks is None:
                user.sandbox_checked_weeks = [user.last_reported_week.isoformat()] if user.last_reported_week else []
        return user

    if sandbox:
        @app.post("/api/sandbox/reset")
        async def sandbox_reset(user: User = Depends(current_user), session: AsyncSession = Depends(db)):
            # Start a fresh test cycle while retaining previous test records.
            user.cycle += 1
            user.onboarding_step = OnboardingStep.WHEEL
            user.cycle_start = None
            user.last_reported_week = None
            user.test_mode_since = None
            user.onboarding_position = None
            user.is_ready = False
            user.sandbox_week = 1
            user.sandbox_checked_weeks = []
            return {"ok": True}

        @app.post("/api/sandbox/week/{week}")
        async def sandbox_week(week: int, user: User = Depends(current_user)):
            if week not in range(1, 13):
                raise HTTPException(422, "week")
            if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
                raise HTTPException(409, "finish_onboarding")
            user.sandbox_week = week
            user.onboarding_step = OnboardingStep.DONE
            return {"ok": True}

    # ---------- профиль ----------

    @app.get("/api/me")
    async def me(user: User = Depends(current_user)) -> dict:
        return {
            "first_name": user.first_name,
            "step": user.onboarding_step.value,
            "cycle": user.cycle,
            "is_admin": user.telegram_id in settings.admin_ids,
            **({"sandbox": True, "test_week": scorecard.week_number(user.cycle_start, today_for(user))} if sandbox else {}),
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
        """Подтвердила план, но цикл ещё не начался."""
        return (
            user.onboarding_step == OnboardingStep.DONE
            and user.cycle_start is not None
            and today_for(user) < user.cycle_start
        )

    def plan_editable(user: User) -> bool:
        """Подтверждённый план можно менять: до старта и в первые 3 дня цикла (единое правило для API и UI)."""
        return cycle.plan_editable(user, today_for(user))

    def require_plan_editable(user: User, *steps: OnboardingStep) -> None:
        if user.onboarding_step not in steps and not plan_editable(user):
            raise HTTPException(status_code=409, detail="plan_locked")

    def edit_until_iso(user: User) -> str | None:
        until = cycle.plan_edit_until(user)
        return until.isoformat() if until else None

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

    async def preserve_plan(session, user, request, kind):
        if user.onboarding_step != OnboardingStep.DONE:
            return
        raw = request.query_params.get("effective_week")
        try:
            effective = int(raw) if raw else None
            current = scorecard.week_number(user.cycle_start, today_for(user)) or 1
            if (effective or current) == current and user.cycle_start and week_checked(user, scorecard.week_start(today_for(user))):
                raise ValueError("checked_plan")
            await plan_history.before_edit(session, user, today_for(user), effective, kind)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.get("/api/plan/history")
    async def plan_history_api(user: User = Depends(current_user), session: AsyncSession = Depends(db)):
        from bot.models import PlanChange, PlanWeekSnapshot
        changes = list(await session.scalars(select(PlanChange).where(PlanChange.user_id == user.id,
            PlanChange.cycle == user.cycle).order_by(PlanChange.id.desc())))
        weeks = list(await session.scalars(select(PlanWeekSnapshot).where(PlanWeekSnapshot.user_id == user.id,
            PlanWeekSnapshot.cycle == user.cycle).order_by(PlanWeekSnapshot.week_number)))
        return {"weeks": [{"week": w.week_number, "plan": w.plan} for w in weeks],
            "changes": [{"id": c.id, "date": c.created_at.isoformat(), "week": c.effective_week,
                "kind": c.kind, "plan": c.plan_before, "marks": c.marks_before} for c in changes]}

    @app.put("/api/intent")
    async def save_intent(
        body: IntentIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.INTENT, OnboardingStep.TACTICS)
        try:
            await preserve_plan(session, user, request, "Изменено зачем")
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
        start = user.cycle_start or svc.cycle_start_for(today_for(user), settings.cycle_start)
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
            "editable": user.onboarding_step == OnboardingStep.TACTICS or plan_editable(user),
            "editable_until": edit_until_iso(user),  # последний день правок, включительно
            "started": user.cycle_start is not None and today_for(user) >= user.cycle_start,
            "display_from_week": (scorecard.week_number(user.cycle_start, today_for(user)) or 1) + int(bool(await plan_history.snapshot(session, user, scorecard.week_number(user.cycle_start, today_for(user)) or 1))),
            "current_week": scorecard.week_number(user.cycle_start, today_for(user)) or 1,
            "current_checked": week_checked(user, scorecard.week_start(today_for(user))),
            "scheduled": bool(await plan_history.snapshot(session, user, scorecard.week_number(user.cycle_start, today_for(user)) or 1)),
            "reselect_allowed": user.onboarding_step == OnboardingStep.TACTICS or before_start(user),
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
        priority_id: int, body: PriorityIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        """Переименовать цель и поправить её «зачем»."""
        require_plan_editable(user, OnboardingStep.TACTICS)
        priority = await own_priority(session, user, priority_id)
        title, intent = body.title.strip(), body.intent.strip()
        if not title or len(title) > svc.MAX_ITEM_LEN:
            raise HTTPException(status_code=422, detail="bad_title")
        if len(intent) < svc.MIN_INTENT_LEN:
            raise HTTPException(status_code=422, detail="intent_required")
        if body.replace and not body.first_action.strip():
            raise HTTPException(status_code=422, detail="empty")
        if len(body.first_action.strip()) > tactics.MAX_TACTIC_LEN:
            raise HTTPException(status_code=422, detail="too_long")
        await preserve_plan(session, user, request, "Заменён приоритет" if body.replace else "Уточнена цель")
        if body.replace:
            old = await session.scalars(select(WeeklyTactic).where(WeeklyTactic.priority_id == priority.id))
            for t in old:
                t.is_active = False
            session.add(WeeklyTactic(priority_id=priority.id, user_id=user.id,
                text=body.first_action.strip(), weeks=None, days=None))
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
        """Выбрать 3 приоритета заново из списка Explore. Тактики старых приоритетов удалятся при выборе.

        Только до старта: после старта это стёрло бы отметки первых дней и вывело из «Сегодня»;
        в первые 3 дня цели переименовываются, а действия правятся по одному."""
        if user.onboarding_step not in (OnboardingStep.INTENT, OnboardingStep.TACTICS) and not before_start(user):
            raise HTTPException(status_code=409, detail="plan_locked")
        user.onboarding_step = OnboardingStep.ELIMINATE
        return await explore_payload(session, user)

    @app.get("/api/plan")
    async def get_plan(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        return await plan_payload(session, user)

    @app.post("/api/tactics")
    async def add_tactic(
        body: TacticIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.TACTICS)
        priority = await own_priority(session, user, body.priority_id or 0)
        text, weeks, days = clean_tactic(body)
        count = await session.scalar(
            select(func.count(WeeklyTactic.id)).where(WeeklyTactic.priority_id == priority.id, WeeklyTactic.is_active)
        )
        if count >= tactics.MAX_TACTICS_PER_PRIORITY:
            raise HTTPException(status_code=422, detail="limit")
        await preserve_plan(session, user, request, "Добавлено действие")
        session.add(WeeklyTactic(priority_id=priority.id, user_id=user.id, text=text, weeks=weeks, days=days))
        await session.flush()
        return await plan_payload(session, user)

    @app.put("/api/tactics/{tactic_id}")
    async def edit_tactic(
        tactic_id: int, body: TacticIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        require_plan_editable(user, OnboardingStep.TACTICS)
        tactic = await own_tactic(session, user, tactic_id)
        text, weeks, days = clean_tactic(body)
        await preserve_plan(session, user, request, "Изменено действие")
        if user.onboarding_step == OnboardingStep.DONE:
            tactic.is_active = False
            session.add(WeeklyTactic(priority_id=tactic.priority_id, user_id=user.id,
                                    text=text, weeks=weeks, days=days))
        else:
            tactic.text, tactic.weeks, tactic.days = text, weeks, days
        await session.flush()
        return await plan_payload(session, user)

    @app.delete("/api/tactics/{tactic_id}")
    async def delete_tactic(
        tactic_id: int, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(db)
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
        await preserve_plan(session, user, request, "Убрано действие")
        tactic.is_active = False  # Keep IDs, marks, and historical references even without marks.
        await session.flush()
        return await plan_payload(session, user)

    # ---------- Моя команда: только проценты за неделю ----------

    @app.get("/api/team")
    async def get_team(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        """Проценты тройки за эту и прошлую неделю — без целей, действий и приоритетов (как /team в чате)."""
        team = await teams.get_team_of(session, user)
        if team is None:
            return {"team": None, "organizer": user.no_team, "members": []}
        this_week = scorecard.week_start(today_for(user))
        members = []
        for m in await teams.team_members(session, team.id):
            current = await scorecard.week_percent(session, m, this_week)
            prev = await scorecard.week_percent(session, m, this_week - timedelta(days=7))
            members.append({
                "name": m.first_name or "Участница",
                "you": m.id == user.id,
                "current": current,
                "level": scorecard.level(current) if current is not None else None,
                "prev": prev,
            })
        return {"team": teams.team_name(team), "organizer": False, "members": members}

    # ---------- Видение на 3+ года ----------
    # Необязательное и личное: любые блоки можно оставить пустыми, правится в любой момент (и после
    # старта — в отличие от тактического плана), команде и в уведомления не уходит.

    async def vision_payload(session: AsyncSession, user: User) -> dict:
        vision = await vision_svc.get_vision(session, user)
        texts = {f: getattr(vision, f) if vision else "" for f in vision_svc.FIELDS}
        today = today_for(user)
        refl = await vision_svc.get_reflection(session, user)
        return {
            "vision": texts,
            "filled": sum(1 for v in texts.values() if v),
            "saved": vision is not None,
            "max_len": vision_svc.MAX_LEN,
            "reflection": {
                "open": vision_svc.reflection_open(user, today),
                "cycle": user.cycle,
                **{f: getattr(refl, f) if refl else "" for f in vision_svc.REFLECTION_FIELDS},
            },
        }

    @app.get("/api/vision")
    async def get_vision(user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        return await vision_payload(session, user)

    @app.put("/api/vision")
    async def save_vision(body: VisionIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        try:
            texts = vision_svc.clean(body.model_dump(), vision_svc.FIELDS)
        except ValueError:
            raise HTTPException(status_code=422, detail="too_long") from None
        await vision_svc.save_vision(session, user, texts)
        return await vision_payload(session, user)

    @app.put("/api/vision/reflection")
    async def save_reflection(
        body: ReflectionIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)
    ) -> dict:
        if not vision_svc.reflection_open(user, today_for(user)):
            raise HTTPException(status_code=409, detail="reflection_closed")
        try:
            texts = vision_svc.clean(body.model_dump(), vision_svc.REFLECTION_FIELDS)
        except ValueError:
            raise HTTPException(status_code=422, detail="too_long") from None
        await vision_svc.save_reflection(session, user, texts)
        return await vision_payload(session, user)

    # ---------- Чек-ин и scorecard ----------

    CHECKIN_STEPS = (OnboardingStep.DONE, OnboardingStep.FINISHED)

    @app.get("/api/checkin")
    async def get_checkin(user: User = Depends(current_user), session: AsyncSession = Depends(db), week_number: int | None = None) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = today_for(user)
        week = scorecard.week_start(today) if sandbox else checkins.default_week(user, today)
        if week_number is not None:
            if not 1 <= week_number <= 12 or user.cycle_start is None:
                raise HTTPException(422, "bad_week")
            week = user.cycle_start + timedelta(weeks=week_number - 1)
            if not checkins.can_check_in(user, week, today):
                raise HTTPException(409, "week_closed")
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
            "deadline": None if n == 1 else (week + timedelta(days=8)).isoformat(),
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
        today = today_for(user)
        all_tactics = await checkins.active_tactics(session, user)
        target_week = scorecard.week_start(today) if sandbox else checkins.default_week(user, today)
        target_n = checkins.checkin_week_number(user, target_week)
        target_available = checkins.can_check_in(user, target_week, today)
        base = {
            "checkin_target_week": target_n if target_available else None,
            "checkin_deadline": (target_week + timedelta(days=8)).isoformat() if target_available else None,
            "today": today.isoformat(),
            "weekday": today.weekday(),
            "cycle_start": user.cycle_start.isoformat() if user.cycle_start else None,
            "priorities": len({t.priority_id for t in all_tactics}),
            "tactics": len(all_tactics),
            "editable": plan_editable(user),
            "editable_until": edit_until_iso(user),
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
        week_tactics = await checkins.tactics_for_week(session, user, week)
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
        if sandbox:
            return week.isoformat() in (user.sandbox_checked_weeks or [])
        return user.last_reported_week is not None and user.last_reported_week >= week

    def require_open_week(user: User, week: date) -> None:
        if week_checked(user, week):
            raise HTTPException(status_code=409, detail="week_checked")

    @app.post("/api/today/daily")
    async def mark_daily(body: DailyIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = today_for(user)
        original = await session.get(WeeklyTactic, body.tactic_id)
        if original is None or original.user_id != user.id:
            raise HTTPException(status_code=404, detail="no_tactic")
        scheduled = await checkins.tactics_for_week(session, user, scorecard.week_start(today))
        tactic = next((t for t in scheduled if t.id == body.tactic_id), None)
        if tactic is None:
            raise HTTPException(status_code=422, detail="not_today")
        n = scorecard.week_number(user.cycle_start, today)
        if n is None or not tactic.in_week(n) or not today_svc.scheduled_on([tactic], today):
            raise HTTPException(status_code=422, detail="not_today")
        require_open_week(user, scorecard.week_start(today))
        await today_svc.set_daily(session, user, tactic, today, body.done)
        return await get_today(user, session)

    @app.post("/api/today/week")
    async def mark_week(body: WeekMarkIn, user: User = Depends(current_user), session: AsyncSession = Depends(db)) -> dict:
        require_step(user, *CHECKIN_STEPS)
        today = today_for(user)
        week = scorecard.week_start(today)
        original = await session.get(WeeklyTactic, body.tactic_id)
        if original is None or original.user_id != user.id:
            raise HTTPException(status_code=404, detail="no_tactic")
        scheduled = await checkins.tactics_for_week(session, user, scorecard.week_start(today))
        tactic = next((t for t in scheduled if t.id == body.tactic_id), None)
        if tactic is None:
            raise HTTPException(status_code=422, detail="not_this_week")
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
        if not checkins.can_check_in(user, week, today_for(user)):
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
        if sandbox:
            user.sandbox_checked_weeks = sorted(set(user.sandbox_checked_weeks or []) | {week.isoformat()})
        elif app.state.bot is not None:
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
        today = today_for(user)
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
                        "planned": len(await checkins.tactics_for_week(session, user, week)),
                        "percent": value,
                        "level": scorecard.level(value) if value is not None else None,
                        "future": week > scorecard.week_start(today) and not (sandbox and value is not None),
                    }
                )
        done_total = 0
        if user.cycle_start:
            for w in weeks:
                week = date.fromisoformat(w["week_start"])
                ts = await checkins.tactics_for_week(session, user, week)
                marks = await checkins.get_marks(session, user, week)
                done_total += sum(marks.get(t.id) is True for t in ts)
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
        user.cycle_start = svc.cycle_start_for(today_for(user), settings.cycle_start)
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

    if sandbox:
        index_html = index_html.replace('<body>', '<body data-sandbox="true">')
        index_html = index_html.replace('<body data-sandbox="true">', '<body data-sandbox="true"><aside id="sandbox-panel" style="position:fixed;top:0;left:0;right:0;z-index:999;background:#fff0ce;color:#332200;padding:8px;font-size:13px;text-align:center">ТЕСТОВЫЙ РЕЖИМ · отдельные данные <button id="sandbox-reset">Новый тест</button> <label>Неделя <select id="sandbox-week">' + ''.join(f'<option value="{n}">{n}</option>' for n in range(1,13)) + '</select></label> <button id="sandbox-apply">Перейти</button><span id="sandbox-result" role="status"></span></aside><style>body{padding-top:85px!important}</style>')

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
