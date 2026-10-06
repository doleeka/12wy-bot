"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot import texts
from bot.commands import set_bot_commands, set_webapp_menu_button
from bot.config import load_settings, local_today
from bot.db import create_engine, create_sessionmaker
from bot.migrate import run_migrations
from bot.handlers import admin, broadcast, checkin, cycle, fallback, group, onboarding, start, teams, wheel
from bot.middlewares import DbSessionMiddleware
from bot.notify import safe_send
from bot.scheduler import setup_scheduler
from bot.services.backup import apply_pending_restore
from bot.services.onboarding import align_to_cohort_start
from webapp.app import create_app


class WebServer(uvicorn.Server):
    """uvicorn без своих обработчиков сигналов: остановкой управляет aiogram (см. main)."""

    @contextlib.contextmanager
    def capture_signals(self):  # noqa: ANN201
        yield


def build_dispatcher(sessionmaker) -> Dispatcher:  # noqa: ANN001
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    # group — только групповые чаты; остальные роутеры — только личка (фильтр задан в каждом модуле)
    dp.include_routers(
        group.router,
        start.router,
        admin.router,
        broadcast.router,
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
    if settings.cycle_start:
        async with create_sessionmaker(engine)() as session:
            moved = await align_to_cohort_start(session, settings.cycle_start, local_today(settings))
            await session.commit()
        if moved:
            logging.info("Перенесено на общий старт %s: %d", settings.cycle_start, moved)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sessionmaker = create_sessionmaker(engine)
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = settings

    # Если у токена остался webhook (бот раньше был подключён к другому сервису), Telegram не отдаёт
    # обновления через getUpdates — бот работает, но не получает ни одного сообщения. Снимаем его.
    webhook = await bot.get_webhook_info()
    if webhook.url:
        logging.warning("У бота был webhook %s — снимаю, чтобы работал long polling", webhook.url)
        await bot.delete_webhook(drop_pending_updates=False)

    try:
        await set_bot_commands(bot, settings)
        await set_webapp_menu_button(bot, settings)
    except Exception:  # noqa: BLE001 — без меню бот всё равно работает
        logging.exception("Не удалось установить меню команд")

    scheduler = setup_scheduler(bot, sessionmaker, settings)
    scheduler.start()

    logging.info("DB: %s", settings.database_path.resolve())
    for admin_id in settings.admin_ids:
        await safe_send(bot, admin_id, texts.BOT_STARTED)
    # Mini App (FastAPI) в том же процессе: общая база, тот же бот для уведомлений
    web = WebServer(
        uvicorn.Config(create_app(sessionmaker, settings, bot), host="0.0.0.0", port=settings.port, log_level="warning")
    )
    web_task = asyncio.create_task(web.serve())
    logging.info("Mini App: порт %s, адрес %s", settings.port, settings.webapp_url or "не задан (WEBAPP_URL)")
    try:
        # aiogram ловит SIGINT/SIGTERM и завершает polling; следом останавливаем веб-сервер
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        web.should_exit = True
        await web_task
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
