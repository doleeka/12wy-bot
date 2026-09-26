"""Настройки бота из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


# Астана живёт по зоне Asia/Almaty (с 1 марта 2024 весь Казахстан — UTC+5).
# Отдельной зоны Asia/Astana в базе часовых поясов нет, поэтому принимаем её как синоним.
DEFAULT_TIMEZONE = "Asia/Almaty"
TIMEZONE_ALIASES = {"asia/astana": "Asia/Almaty", "astana": "Asia/Almaty", "asia/nur-sultan": "Asia/Almaty"}


def parse_timezone(raw: str) -> str:
    name = TIMEZONE_ALIASES.get(raw.strip().lower(), raw.strip())
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(
            f"Неизвестный часовой пояс TIMEZONE={raw!r}. Для Астаны укажите Asia/Almaty."
        ) from None
    return name


def _parse_admin_ids(raw: str) -> list[int]:
    return [int(part) for part in raw.replace(" ", "").split(",") if part]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_path: Path
    admin_ids: list[int] = field(default_factory=list)
    timezone: str = "Asia/Almaty"
    # Когда присылать чек-ин (день недели в формате cron: mon..sun) и напоминание о новой неделе (понедельник)
    checkin_day: str = "sun"
    checkin_time: str = "18:00"
    planning_time: str = "09:00"
    # Ночной бэкап базы админам
    backup_time: str = "03:00"
    # Общая дата старта цикла сообщества (понедельник). Кто готов раньше — стартует в этот день,
    # кто позже — с ближайшего понедельника. None — каждая со своего ближайшего понедельника.
    cycle_start: date | None = None
    # Mini App: публичный HTTPS-адрес приложения и порт веб-сервера внутри контейнера
    webapp_url: str = ""
    port: int = 8080

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"


def _cycle_start() -> date | None:
    raw = os.getenv("CYCLE_START", "").strip()
    if not raw:
        return None
    value = date.fromisoformat(raw)
    if value.weekday() != 0:
        raise RuntimeError(f"CYCLE_START={raw} — не понедельник. Неделя цикла начинается с понедельника.")
    return value


def _webapp_url() -> str:
    """WEBAPP_URL, а на Fly по умолчанию https://<имя приложения>.fly.dev (Fly сам задаёт FLY_APP_NAME)."""
    url = os.getenv("WEBAPP_URL", "").strip()
    if url:
        return url.rstrip("/")
    app = os.getenv("FLY_APP_NAME", "").strip()
    return f"https://{app}.fly.dev" if app else ""


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен.")
    return Settings(
        bot_token=token,
        database_path=Path(os.getenv("DATABASE_PATH", "./data/bot.db")).expanduser(),
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        timezone=parse_timezone(os.getenv("TIMEZONE", DEFAULT_TIMEZONE)),
        checkin_day=os.getenv("CHECKIN_DAY", "sun"),
        checkin_time=os.getenv("CHECKIN_TIME", "18:00"),
        planning_time=os.getenv("PLANNING_TIME", "09:00"),
        backup_time=os.getenv("BACKUP_TIME", "03:00"),
        cycle_start=_cycle_start(),
        webapp_url=_webapp_url(),
        port=int(os.getenv("PORT", "8080")),
    )


def local_today(settings: Settings | None) -> date:
    """Сегодняшняя дата в часовом поясе бота."""
    tz = ZoneInfo(settings.timezone) if settings else None
    return datetime.now(tz).date()
