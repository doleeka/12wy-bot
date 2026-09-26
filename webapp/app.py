"""FastAPI: API для Mini App и статика фронтенда. Работает в одном процессе с ботом (см. bot.main)."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import WebAppInitData
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.models import OnboardingStep, User, WheelOfBalance
from bot.services import cycle, wheel
from bot.services.users import get_or_create_user
from webapp.auth import telegram_user

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# До шага Eliminate колесо ещё можно переоценить
WHEEL_EDITABLE_STEPS = {OnboardingStep.WHEEL, OnboardingStep.EXPLORE}


class WheelIn(BaseModel):
    scores: dict[str, int]


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

        first_time = user.onboarding_step == OnboardingStep.WHEEL
        user.onboarding_step = OnboardingStep.EXPLORE
        if first_time and app.state.bot is not None:
            # Следующие шаги пока идут в чате — сразу подсказываем, что дальше
            from bot.handlers.onboarding import send_step_prompt

            try:
                await send_step_prompt(ChatTarget(app.state.bot, user.telegram_id), session, user)
            except TelegramAPIError as e:  # заблокировала бота и т.п. — колесо всё равно сохранено
                log.warning("Не удалось отправить подсказку Explore %s: %s", user.telegram_id, e)
        return wheel_payload(scores, await cycle.wheel_comparison(session, user))

    # ---------- фронтенд ----------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
