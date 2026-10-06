"""Рассылка админа всем участницам: /broadcast текст → предпросмотр → «Отправить»."""
from __future__ import annotations

import asyncio
import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.filters import IsAdmin
from bot.models import User
from bot.notify import safe_send

router = Router(name="broadcast")
router.message.filter(F.chat.type == "private")

SEND_DELAY = 0.05  # пауза между сообщениями — не упираться в лимиты Telegram
# Черновики рассылок до нажатия «Отправить»: ключ — одноразовый код в кнопке (после перезапуска — устаревают)
_pending: dict[str, tuple[int, str]] = {}


async def recipients(session: AsyncSession, sender_tg_id: int) -> list[int]:
    """Все, кто запускал бота, кроме самой отправительницы."""
    rows = await session.scalars(select(User.telegram_id).where(User.telegram_id != sender_tg_id).order_by(User.id))
    return list(rows)


@router.message(Command("broadcast"), IsAdmin())
async def cmd_broadcast(message: Message, command: CommandObject, session: AsyncSession) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(texts.BROADCAST_USAGE)
        return
    to = await recipients(session, message.from_user.id)
    if not to:
        await message.answer(texts.BROADCAST_NOBODY)
        return
    text = escape(raw)  # как написала — без HTML-разметки, переносы строк сохраняются
    code = secrets.token_hex(4)
    _pending[code] = (message.from_user.id, text)
    await message.answer(
        texts.BROADCAST_PREVIEW.format(n=len(to), text=text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Отправить ({len(to)})", callback_data=f"bc:send:{code}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data=f"bc:cancel:{code}"),
        ]]),
    )


@router.callback_query(F.data.startswith("bc:"), IsAdmin())
async def on_broadcast(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    _, action, code = (callback.data.split(":") + ["", ""])[:3]
    draft = _pending.pop(code, None)
    if draft is None or draft[0] != callback.from_user.id:
        await callback.answer(texts.BROADCAST_STALE, show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)  # второй раз нажать нельзя
    if action != "send":
        await callback.message.answer(texts.BROADCAST_CANCELLED)
        await callback.answer()
        return
    await callback.answer("Отправляю…")
    to = await recipients(session, callback.from_user.id)
    ok = 0
    for tg_id in to:
        if await safe_send(bot, tg_id, draft[1]):
            ok += 1
        await asyncio.sleep(SEND_DELAY)
    failed = texts.BROADCAST_FAILED.format(n=len(to) - ok) if ok < len(to) else ""
    await callback.message.answer(texts.BROADCAST_SENT.format(ok=ok, n=len(to), failed=failed))


@router.message(Command("broadcast"))
async def broadcast_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
