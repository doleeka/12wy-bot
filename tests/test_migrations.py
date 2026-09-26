import os
import shutil
import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from bot import migrate
from bot.models import Base


def rows(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migrations_match_models(tmp_path):
    """Сторож: если поменять models.py и забыть миграцию, этот тест упадёт.

    Как чинить: `alembic revision --autogenerate -m "что поменялось"`, проверить файл, закоммитить.
    """
    db = tmp_path / "bot.db"
    assert migrate.run_migrations(db) is None  # пустая база — копия не нужна
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_second_run_is_noop(tmp_path):
    db = tmp_path / "bot.db"
    migrate.run_migrations(db)
    assert migrate.run_migrations(db) is None
    assert not (tmp_path / migrate.BACKUPS_DIR_NAME).exists()


def _database_at(db, revision):
    from alembic import command

    command.upgrade(migrate.alembic_config(db), revision)


def test_legacy_database_without_alembic_is_stamped(tmp_path):
    """База, созданная через create_all до появления миграций, — это схема ревизии 0001 без alembic_version."""
    db = tmp_path / "bot.db"
    _database_at(db, migrate.BASELINE_REVISION)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE alembic_version")
    conn.execute("INSERT INTO users (telegram_id, onboarding_step, is_ready, send_report, cycle) VALUES (1,'DONE',1,'TEAM',1)")
    conn.commit()
    conn.close()

    migrate.run_migrations(db)
    assert rows(db, "SELECT version_num FROM alembic_version") == [(migrate.head_revision(migrate.alembic_config(db)),)]
    assert rows(db, "SELECT telegram_id FROM users") == [(1,)]


def test_0002_team_chat_keeps_members(tmp_path):
    """0002 пересоздаёт teams (уникальный chat_id), а team_members ссылается на неё с ON DELETE CASCADE."""
    db = tmp_path / "bot.db"
    _database_at(db, "0001")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        INSERT INTO users (id, telegram_id, onboarding_step, is_ready, send_report, cycle) VALUES (1, 1, 'DONE', 1, 'TEAM', 1), (2, 2, 'DONE', 1, 'TEAM', 1);
        INSERT INTO teams (id, chat_id) VALUES (1, -100500), (2, NULL);
        INSERT INTO team_members (team_id, user_id) VALUES (1, 1), (2, 2);
        """
    )
    conn.commit()
    conn.close()

    backup = migrate.run_migrations(db)
    assert rows(db, "SELECT team_id, user_id FROM team_members ORDER BY user_id") == [(1, 1), (2, 2)]
    assert rows(db, "SELECT id, chat_id, invite_link FROM teams ORDER BY id") == [(1, -100500, None), (2, None, None)]
    assert backup and "pre-migration_0001-to-0002" in backup
    with pytest.raises(sqlite3.IntegrityError):  # один чат — одна команда
        conn = sqlite3.connect(db)
        conn.execute("UPDATE teams SET chat_id = -100500 WHERE id = 2")


NEW_REVISION = '''
from alembic import op
import sqlalchemy as sa

revision = "9999"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade():
    # пересоздаёт таблицу users целиком (так SQLite делает большинство изменений) —
    # на неё ссылаются внешние ключи с ON DELETE CASCADE
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("timezone", sa.String(64), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("timezone")
'''


def test_new_migration_keeps_related_rows_and_makes_backup(tmp_path, monkeypatch):
    db = tmp_path / "bot.db"
    migrate.run_migrations(db)
    head = migrate.head_revision(migrate.alembic_config(db))
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        INSERT INTO users (id, telegram_id, onboarding_step, is_ready, send_report, cycle) VALUES (1, 42, 'DONE', 1, 'TEAM', 1);
        INSERT INTO explore_list (user_id, cycle, text, selected) VALUES (1, 1, 'Спорт', 0), (1, 1, 'Книга', 1);
        INSERT INTO teams (id) VALUES (1);
        INSERT INTO team_members (team_id, user_id) VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    # копия каталога миграций + новая ревизия, как будто вышла новая версия бота
    migrations = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, migrations)
    (migrations / "versions" / "9999_add_timezone.py").write_text(NEW_REVISION.format(head=head))
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migrations)

    backup = migrate.run_migrations(db)

    assert rows(db, "SELECT version_num FROM alembic_version") == [("9999",)]
    assert "timezone" in [r[1] for r in rows(db, "PRAGMA table_info(users)")]
    # связанные строки не удалены каскадом при пересоздании users
    assert rows(db, "SELECT COUNT(*) FROM explore_list") == [(2,)]
    assert rows(db, "SELECT COUNT(*) FROM team_members") == [(1,)]
    assert rows(db, "PRAGMA foreign_key_check") == []
    # перед миграцией сделана копия, и в ней старая схема
    assert backup and f"pre-migration_{head}-to-9999" in backup
    assert "timezone" not in [r[1] for r in rows(backup, "PRAGMA table_info(users)")]


def test_old_pre_migration_backups_are_pruned(tmp_path):
    db = tmp_path / "bot.db"
    migrate.run_migrations(db)
    backups = tmp_path / migrate.BACKUPS_DIR_NAME
    backups.mkdir()
    for i in range(7):
        old = backups / f"pre-migration_zz-old{i}.db"  # имена «больше» новых — сортировать надо по времени
        old.write_text("")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    fresh = migrate._pre_migration_backup(db, "a", "b")
    left = list(backups.glob("pre-migration_*.db"))
    assert len(left) == migrate.KEEP_PRE_MIGRATION_BACKUPS and fresh in left
