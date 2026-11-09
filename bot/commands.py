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
    BotCommand(command="plan", description="Мой план: приоритеты и действия"),
    BotCommand(command="team", description="Моя команда и проценты"),
    BotCommand(command="newcycle", description="Новый цикл после 12 недель"),
    BotCommand(command="help", description="Что умеет бот"),
]
ADMIN_COMMANDS = PARTICIPANT_COMMANDS + [
    BotCommand(command="myapp", description="Мои личные цели и прогресс"),
    BotCommand(command="testapp", description="Тестовая версия — отдельные данные"),
    BotCommand(command="admin", description="Управление сообществом"),
    BotCommand(command="remove", description="Админ: исключить из сообщества (тихо, обратимо)"),
    BotCommand(command="restore", description="Админ: вернуть исключённых / список"),
    BotCommand(command="payment", description="Админ: оплата — реквизиты, вкл/выкл, чеки"),
    BotCommand(command="price", description="Админ: цены тарифов по потокам и личные"),
    BotCommand(command="cohort", description="Админ: потоки — старт и набор"),
    BotCommand(command="who", description="Админ: кто оплатил, кто нет"),
    BotCommand(command="tariff", description="Админ: сменить тариф (можно многим)"),
    BotCommand(command="paid", description="Админ: отметить оплату без чека (можно многим)"),
    BotCommand(command="unpaid", description="Админ: отменить ошибочную отметку оплаты"),
    BotCommand(command="revenue", description="Админ: деньги за месяц, паузы, чеки"),
    BotCommand(command="announce", description="Админ: объявление в общий чат"),
    BotCommand(command="postbot", description="Админ: пост «Откройте бота» в общий чат"),
    BotCommand(command="meeting", description="Админ: встреча — анонс и напоминания"),
    BotCommand(command="meetings", description="Админ: список встреч"),
    BotCommand(command="broadcast", description="Админ: сообщение всем (с предпросмотром)"),
    BotCommand(command="stats", description="Админ: сводка недели (без имён)"),
    BotCommand(command="teams", description="Админ: все команды"),
    BotCommand(command="noteam", description="Админ: я вне команд (off — вернуть)"),
    BotCommand(command="moveteam", description="Админ: перевести в команду"),
    BotCommand(command="regroup", description="Админ: пересобрать тройки (сначала план)"),
    BotCommand(command="solo", description="Админ: только приложение, без тройки / список"),
    BotCommand(command="unsolo", description="Админ: вернуть в тройку"),
    BotCommand(command="report", description="Админ: куда слать отчёт"),
    BotCommand(command="backup", description="Админ: бэкап базы"),
    BotCommand(command="resetme", description="Тест: как начать заново"),
    BotCommand(command="testcycle", description="Тест: сменить неделю в тестовом профиле"),
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
