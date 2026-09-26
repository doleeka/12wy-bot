"""Применение миграций Alembic при старте бота."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect

from bot.services.backup import make_backup

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Ревизия, которая совпадает со схемой, создававшейся через create_all до появления миграций
BASELINE_REVISION = "0001"
BACKUPS_DIR_NAME = "backups"
KEEP_PRE_MIGRATION_BACKUPS = 5


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def head_revision(cfg: Config) -> str:
    return ScriptDirectory.from_config(cfg).get_current_head()


def run_migrations(db_path: Path) -> str | None:
    """Доводит базу до последней ревизии. Возвращает путь к копии, сделанной перед миграцией, если была.

    Движок здесь отдельный и синхронный, без PRAGMA foreign_keys=ON: SQLite меняет таблицы
    пересозданием (DROP + RENAME), и с включёнными внешними ключами DROP каскадно удалил бы
    связанные строки в других таблицах.
    """
    cfg = alembic_config(db_path)
    head = head_revision(cfg)
    engine = create_engine(f"sqlite:///{db_path}")
    # Явно, а не по умолчанию SQLite — на случай, если кто-то включит FK глобально
    event.listen(engine, "connect", lambda dbapi_conn, _record: dbapi_conn.execute("PRAGMA foreign_keys=OFF"))
    backup_path = None
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            current = MigrationContext.configure(conn).get_current_revision()

        has_data_tables = "users" in tables
        if has_data_tables and current is None:
            log.warning("База создана до миграций — помечаю её ревизией %s", BASELINE_REVISION)
            _with_connection(engine, cfg, lambda: command.stamp(cfg, BASELINE_REVISION))
            current = BASELINE_REVISION

        if current == head:
            return None
        if has_data_tables:
            backup_path = _pre_migration_backup(db_path, current, head)
            log.warning("Перед миграцией %s → %s сделана копия: %s", current, head, backup_path)
        _with_connection(engine, cfg, lambda: command.upgrade(cfg, "head"))
        with engine.connect() as conn:
            broken = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if broken:
            log.error("После миграции нарушены внешние ключи: %s", broken[:10])
        log.info("Миграции применены: %s → %s", current or "пусто", head)
        return str(backup_path) if backup_path else None
    finally:
        engine.dispose()


def _with_connection(engine, cfg: Config, action) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        try:
            action()
        finally:
            cfg.attributes["connection"] = None


def _pre_migration_backup(db_path: Path, current: str | None, head: str) -> Path:
    backups = db_path.parent / BACKUPS_DIR_NAME
    tmp = make_backup(db_path, backups)
    dest = backups / f"pre-migration_{current}-to-{head}_{datetime.now():%Y%m%d-%H%M%S}.db"
    tmp.rename(dest)
    old = sorted(backups.glob("pre-migration_*.db"), key=lambda p: p.stat().st_mtime)[:-KEEP_PRE_MIGRATION_BACKUPS]
    for path in old:
        path.unlink()
    return dest
