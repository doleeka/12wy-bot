from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.config import DEFAULT_TIMEZONE, load_settings, parse_timezone


def test_astana_is_utc_plus_5():
    """С 1 марта 2024 Казахстан живёт по UTC+5. Устаревшая база часовых поясов дала бы +6."""
    offset = datetime(2026, 9, 27, 12, tzinfo=ZoneInfo(DEFAULT_TIMEZONE)).utcoffset()
    assert offset.total_seconds() == 5 * 3600


def test_timezone_aliases_and_errors():
    assert parse_timezone("Asia/Astana") == "Asia/Almaty"
    assert parse_timezone(" astana ") == "Asia/Almaty"
    assert parse_timezone("Europe/Moscow") == "Europe/Moscow"
    with pytest.raises(RuntimeError, match="Asia/Almaty"):
        parse_timezone("Mars/Olympus")


def test_default_timezone(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.delenv("TIMEZONE", raising=False)
    assert load_settings().timezone == "Asia/Almaty"
