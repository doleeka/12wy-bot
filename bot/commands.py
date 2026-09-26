"""Меню команд в Telegram (то, что появляется при вводе «/»)."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)

from bot.config import Settings

log = logging.getLogger(__name__)

PARTICIPANT_COMMANDS = [
    BotCommand(command="start", description="Начать / продолжить"),
    BotCommand(command="checkin", description="Чек-ин недели"),
    BotCommand(command="plan", description="Мой план: приоритеты и тактики"),
    BotCommand(command="team", description="Моя команда и проценты"),
    BotCommand(command="newcycle", description="Новый цикл после 12 недель"),
    BotCommand(command="help", description="Что умеет бот"),
]
ADMIN_COMMANDS = PARTICIPANT_COMMANDS + [
    BotCommand(command="teams", description="Админ: все команды"),
    BotCommand(command="moveteam", description="Админ: перевести в команду"),
    BotCommand(command="report", description="Админ: куда слать отчёт"),
    BotCommand(command="backup", description="Админ: бэкап базы"),
    BotCommand(command="resetme", description="Админ: пройти онбординг заново"),
    BotCommand(command="testcycle", description="Админ: проверить цикл (будто идёт неделя N)"),
    BotCommand(command="unlinkteam", description="Админ: отвязать чат команды"),
]
GROUP_COMMANDS = [
    BotCommand(command="team", description="Проценты команды за неделю"),
    BotCommand(command="linkteam", description="Админ: привязать чат к команде"),
]


async def set_bot_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(PARTICIPANT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as e:  # админ ещё не писал боту — Telegram не знает этот чат
            log.warning("Меню админа %s не установлено: %s", admin_id, e)


async def set_webapp_menu_button(bot: Bot, settings: Settings) -> None:
    """Кнопка слева от поля ввода: открывает Mini App (или обычное меню команд, если адреса нет)."""
    if settings.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=settings.webapp_url))
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
