import pytest

from bot.db import create_engine, create_sessionmaker, init_db


@pytest.fixture
async def sessionmaker(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    yield create_sessionmaker(engine)
    await engine.dispose()
