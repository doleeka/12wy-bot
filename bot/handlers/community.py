"""Админ пишет в общий чат через бота: пост «Откройте бота» (/postbot) и объявления (/announce, с предпросмотром).
Встречи (/meeting, /meetings) — в bot/handlers/meetings.py."""
from __future__ import annotations

import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import community, texts
from bot.config import Settings
from bot.filters import IsAdmin

router = Router(name="community")
router.message.filter(F.chat.type == "private")

# Объявления, ждущие «Опубликовать»: одноразовый код → (кто, текст)
_pending: dict[str, tuple[int, str]] = {}


@router.message(Command("postbot"), IsAdmin())
async def cmd_postbot(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                      settings: Settings | None = None) -> None:
    """Пост с кнопкой «Открыть бота» — для тех, кто ещё не нажал «Старт». Свой текст — после команды."""
    text = escape(command.args.strip()) if command.args and command.args.strip() else texts.OPEN_BOT_POST
    ok = await community.post(bot, session, settings, community.OPEN_BOT, text, await community.open_bot_button(bot))
    await message.answer(texts.POSTED.format(kind=community.KINDS[community.OPEN_BOT]) if ok else texts.NOT_POSTED)


@router.message(Command("announce"), IsAdmin())
async def cmd_announce(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(texts.ANNOUNCE_USAGE)
        return
    code = secrets.token_hex(4)
    _pending[code] = (message.from_user.id, escape(text))
    await message.answer(texts.ANNOUNCE_PREVIEW.format(text=escape(text)), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Опубликовать", callback_data=f"ann:yes:{code}"),
                          InlineKeyboardButton(text="✖ Отмена", callback_data=f"ann:no:{code}")]]))


@router.callback_query(F.data.startswith("ann:"), IsAdmin())
async def on_announce(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    _, action, code = (callback.data.split(":") + ["", ""])[:3]
    pending = _pending.pop(code, None)
    if pending is None or pending[0] != callback.from_user.id:
        await callback.answer(texts.ANNOUNCE_STALE, show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if action != "yes":
        await bot.send_message(callback.from_user.id, texts.ANNOUNCE_CANCELLED)
        return
    ok = await community.post(bot, session, settings, community.NEWS, pending[1])
    await bot.send_message(callback.from_user.id,
                           texts.POSTED.format(kind=community.KINDS[community.NEWS]) if ok else texts.NOT_POSTED)


@router.message(Command("postbot", "announce"))
async def community_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
