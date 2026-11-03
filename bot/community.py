"""Общий чат сообщества с темами: каждый вид сообщений бота — в свою тему (/topic <вид> в нужной теме).

Не указана тема — в общую ветку (итоги — в тему, где отправили /linkcommon). Тему удалили или закрыли —
публикуем в общей ветке и пишем админам, чтобы выбрали новую."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings
from bot.notify import safe_send
from bot.services import payments
from bot.team_notify import COMMON_THREAD, common_chat_id

log = logging.getLogger(__name__)

RESULTS, WELCOME, OPEN_BOT, NEWS, MEETINGS, PAYMENT = "results", "welcome", "bot", "news", "meetings", "payment"
KINDS = {
    RESULTS: "итоги недели",
    WELCOME: "приветствие новеньких",
    OPEN_BOT: "пост «Откройте бота»",
    NEWS: "объявления",
    MEETINGS: "встречи",
    PAYMENT: "оплата",
}
KIND_ALIASES = {
    "итоги": RESULTS, "статистика": RESULTS, "results": RESULTS,
    "приветствие": WELCOME, "знакомство": WELCOME, "новенькие": WELCOME, "welcome": WELCOME,
    "бот": OPEN_BOT, "bot": OPEN_BOT,
    "объявления": NEWS, "новости": NEWS, "news": NEWS,
    "встречи": MEETINGS, "встреча": MEETINGS, "meetings": MEETINGS,
    "оплата": PAYMENT, "payment": PAYMENT,
}


def _key(kind: str) -> str:
    return f"topic_{kind}"


async def topic_for(session: AsyncSession, kind: str) -> int | None:
    """Тема для этого вида сообщений; None — общая ветка."""
    raw = await payments.get_setting(session, _key(kind))
    if raw is None and kind == RESULTS:  # итоги — по умолчанию туда, где отправили /linkcommon
        raw = await payments.get_setting(session, COMMON_THREAD)
    return int(raw) if raw and raw.isdigit() else None


async def set_topic(session: AsyncSession, kind: str, thread_id: int | None) -> None:
    await payments.set_setting(session, _key(kind), str(thread_id or ""))


async def topics(session: AsyncSession) -> dict[str, int | None]:
    return {kind: await topic_for(session, kind) for kind in KINDS}


async def post(bot: Bot, session: AsyncSession, settings: Settings | None, kind: str, text: str,
               markup: InlineKeyboardMarkup | None = None) -> bool:
    """Опубликовать в общий чат, в тему этого вида. False — общий чат не привязан или недоступен."""
    chat_id = await common_chat_id(session)
    if chat_id is None:
        return False
    thread_id = await topic_for(session, kind)
    if await safe_send(bot, chat_id, text, markup, thread_id=thread_id):
        return True
    if thread_id is None:
        return False
    sent = await safe_send(bot, chat_id, text, markup)  # тему удалили или закрыли — в общую ветку
    for admin_id in settings.admin_ids if settings else []:
        await safe_send(bot, admin_id, texts.COMMON_TOPIC_LOST.format(kind=KINDS[kind]))
    return sent


async def bot_link(bot: Bot, start: str = "") -> str:
    username = (await bot.me()).username
    return f"https://t.me/{username}" + (f"?start={start}" if start else "")


async def open_bot_button(bot: Bot, label: str = "🌿 Открыть бота", start: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=await bot_link(bot, start))]])
