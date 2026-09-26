"""Настройки бота из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _parse_admin_ids(raw: str) -> list[int]:
    return [int(part) for part in raw.replace(" ", "").split(",") if part]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_path: Path
    admin_ids: list[int] = field(default_factory=list)
    timezone: str = "Europe/Moscow"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен.")
    return Settings(
        bot_token=token,
        database_path=Path(os.getenv("DATABASE_PATH", "./data/bot.db")).expanduser(),
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
    )


def local_today(settings: Settings | None) -> date:
    """Сегодняшняя дата в часовом поясе бота."""
    tz = ZoneInfo(settings.timezone) if settings else None
    return datetime.now(tz).date()
