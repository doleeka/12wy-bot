"""Отправка бэкапа базы админам в личку."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from bot import texts
from bot.config import Settings
from bot.notify import safe_send
from bot.services import backup

log = logging.getLogger(__name__)


def _human_size(n: int) -> str:
    return f"{n / 1024:.0f} КБ" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} МБ"


async def send_backup(bot: Bot, settings: Settings) -> int:
    """Делает копию, проверяет её и отправляет документом каждому админу. Возвращает число получателей."""
    if not settings.admin_ids:
        log.warning("ADMIN_IDS пуст — бэкап некому отправить")
        return 0
    now = datetime.now(ZoneInfo(settings.timezone))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await asyncio.to_thread(backup.make_backup, settings.database_path, Path(tmp), now)
            stats = await asyncio.to_thread(backup.check_backup, path)
        except Exception as e:  # noqa: BLE001 — любой сбой должен дойти до админа
            log.exception("Бэкап не удался")
            for admin_id in settings.admin_ids:
                await safe_send(bot, admin_id, texts.BACKUP_FAILED.format(error=escape(str(e))))
            return 0
        caption = texts.BACKUP_CAPTION.format(
            when=f"{now:%d.%m.%Y %H:%M}", size=_human_size(path.stat().st_size), **stats
        )
        sent = 0
        for admin_id in settings.admin_ids:
            try:
                await bot.send_document(admin_id, FSInputFile(path), caption=caption)
                sent += 1
            except TelegramAPIError as e:
                log.warning("Не удалось отправить бэкап %s: %s", admin_id, e)
    log.info("Бэкап отправлен: %d", sent)
    return sent
