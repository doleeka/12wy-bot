"""Резервные копии SQLite и восстановление из файла."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RESTORE_FILENAME = "restore.db"


def make_backup(db_path: Path, dest_dir: Path, now: datetime | None = None) -> Path:
    """Согласованная копия базы через sqlite3 backup API.

    Простое копирование файла здесь не годится: в режиме WAL свежие записи лежат
    в bot.db-wal, и копия без них была бы неполной.
    """
    now = now or datetime.now()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"12wy-bot_{now:%Y-%m-%d_%H%M}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # один самодостаточный файл без -wal
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def check_backup(path: Path) -> dict[str, int]:
    """Проверяет целостность копии и возвращает число строк в ключевых таблицах."""
    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check не прошёл")
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — имена наши
            for table in ("users", "teams", "checkins")
        }
    finally:
        conn.close()


def apply_pending_restore(db_path: Path, now: datetime | None = None) -> Path | None:
    """Если рядом с базой лежит restore.db — подменяет им базу (старую сохраняет).

    Вызывается при старте бота, до открытия соединений. Возвращает путь к сохранённой
    старой базе или None, если восстанавливать нечего.
    """
    restore = db_path.parent / RESTORE_FILENAME
    if not restore.exists():
        return None
    check_backup(restore)  # битый файл не подкладываем
    now = now or datetime.now()
    saved = db_path.with_name(f"{db_path.stem}.before-restore-{now:%Y%m%d-%H%M%S}{db_path.suffix}")
    if db_path.exists():
        # сначала вливаем WAL в основной файл, чтобы сохранённая копия была полной
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        shutil.move(db_path, saved)
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    shutil.move(restore, db_path)
    log.warning("База восстановлена из %s, прежняя сохранена как %s", RESTORE_FILENAME, saved.name)
    return saved
