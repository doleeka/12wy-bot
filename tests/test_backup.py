import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.backup_job import send_backup
from bot.config import Settings
from bot.db import create_engine, create_sessionmaker, init_db
from bot.models import Team, User
from bot.services import backup


@pytest.fixture
async def db_with_data(tmp_path):
    """База в режиме WAL с данными, которые ещё не влиты в основной файл."""
    path = tmp_path / "bot.db"
    engine = create_engine(f"sqlite+aiosqlite:///{path}")
    await init_db(engine)
    async with create_sessionmaker(engine)() as session:
        session.add_all([User(telegram_id=i) for i in range(1, 6)] + [Team(), Team()])
        await session.commit()
    yield path, engine
    await engine.dispose()


async def test_backup_includes_wal_data(db_with_data, tmp_path):
    path, _engine = db_with_data  # движок открыт — свежие данные могут быть только в -wal
    dest = backup.make_backup(path, tmp_path / "out", datetime(2026, 9, 27, 3, 0))
    assert dest.name == "12wy-bot_2026-09-27_0300.db"
    assert backup.check_backup(dest) == {"users": 5, "teams": 2, "checkins": 0}
    assert not (tmp_path / "out" / f"{dest.name}-wal").exists()


async def test_send_backup_to_admins(db_with_data):
    path, _ = db_with_data
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    settings = Settings(bot_token="x", database_path=path, admin_ids=[10, 20])
    assert await send_backup(bot, settings) == 2
    assert [c.args[0] for c in bot.send_document.call_args_list] == [10, 20]
    caption = bot.send_document.call_args.kwargs["caption"]
    assert "Участниц: 5" in caption and "команд: 2" in caption


async def test_send_backup_failure_notifies_admin(tmp_path):
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    (tmp_path / "broken.db").write_text("это не sqlite")
    settings = Settings(bot_token="x", database_path=tmp_path / "broken.db", admin_ids=[10])
    assert await send_backup(bot, settings) == 0
    assert "Бэкап не удался" in bot.send_message.call_args.args[1]
    bot.send_document.assert_not_called()


async def test_send_backup_without_admins(db_with_data):
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    assert await send_backup(bot, Settings(bot_token="x", database_path=db_with_data[0])) == 0


async def test_restore_swaps_database(db_with_data, tmp_path):
    path, engine = db_with_data
    snapshot = backup.make_backup(path, tmp_path / "out")
    await engine.dispose()

    # после бэкапа данные изменились — восстановление должно вернуть 5 участниц
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()

    assert backup.apply_pending_restore(path) is None  # restore.db нет — ничего не делаем
    snapshot.rename(path.parent / backup.RESTORE_FILENAME)
    saved = backup.apply_pending_restore(path)

    assert backup.check_backup(path)["users"] == 5
    assert backup.check_backup(saved)["users"] == 0  # прежняя база сохранена рядом
    assert not (path.parent / backup.RESTORE_FILENAME).exists()


def test_restore_rejects_broken_file(tmp_path):
    path = tmp_path / "bot.db"
    sqlite3.connect(path).close()
    (tmp_path / backup.RESTORE_FILENAME).write_text("мусор")
    with pytest.raises(Exception):
        backup.apply_pending_restore(path)
    assert (tmp_path / backup.RESTORE_FILENAME).exists() and path.exists()
