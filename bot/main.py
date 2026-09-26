"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import load_settings
from bot.db import create_engine, create_sessionmaker
from bot.migrate import run_migrations
from bot.commands import set_bot_commands
from bot.handlers import admin, checkin, cycle, fallback, group, onboarding, start, teams, wheel
from bot.middlewares import DbSessionMiddleware
from bot.scheduler import setup_scheduler
from bot.services.backup import apply_pending_restore


def build_dispatcher(sessionmaker) -> Dispatcher:  # noqa: ANN001
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    # group — только групповые чаты; остальные роутеры — только личка (фильтр задан в каждом модуле)
    dp.include_routers(
        group.router,
        start.router,
        admin.router,
        teams.router,
        checkin.router,
        cycle.router,
        wheel.router,
        onboarding.router,
        fallback.router,  # последним: /help и неизвестные команды
    )
    dp.errors.register(fallback.on_error)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        apply_pending_restore(settings.database_path)
    except Exception:  # noqa: BLE001 — битый restore.db не должен ронять бота в цикл перезапусков
        logging.exception("restore.db не подошёл — работаю с текущей базой")
    await asyncio.to_thread(run_migrations, settings.database_path)
    engine = create_engine(settings.database_url)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sessionmaker = create_sessionmaker(engine)
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = settings

    try:
        await set_bot_commands(bot, settings)
    except Exception:  # noqa: BLE001 — без меню бот всё равно работает
        logging.exception("Не удалось установить меню команд")

    scheduler = setup_scheduler(bot, sessionmaker, settings)
    scheduler.start()

    logging.info("DB: %s", settings.database_path.resolve())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
