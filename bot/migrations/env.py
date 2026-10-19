"""Окружение Alembic. URL базы берётся из конфига Alembic (его задаёт bot.migrate)
или, при запуске `alembic` из консоли, из DATABASE_PATH."""
from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

from bot.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from dotenv import load_dotenv

    load_dotenv()
    path = Path(os.getenv("DATABASE_PATH", "./data/bot.db")).expanduser()
    return f"sqlite:///{path}"


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


def _run(connection) -> None:  # noqa: ANN001
    # render_as_batch: SQLite не умеет ALTER для большинства изменений, Alembic пересоздаёт таблицу
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
