"""Админ: исключить из сообщества (/remove) и вернуть (/restore). Тихо — никому ничего не приходит."""
from __future__ import annotations

import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings
from bot.filters import IsAdmin
from bot.models import User
from bot.notify import display_name
from bot.services import members, teams
from bot.team_notify import remove_from_chat

router = Router(name="members")
router.message.filter(F.chat.type == "private")

# Ожидающие подтверждения исключения: одноразовый код в кнопке → (кто нажимает, кого исключаем)
_pending: dict[str, tuple[int, int]] = {}


def _row(u: User) -> str:
    return f"• {display_name(u)} · id <code>{u.telegram_id}</code>"


@router.message(Command("remove"), IsAdmin())
async def cmd_remove(message: Message, command: CommandObject, session: AsyncSession, settings: Settings | None = None) -> None:
    q = (command.args or "").strip()
    if not q:
        await message.answer(texts.REMOVE_USAGE)
        return
    found = await members.find_users(session, q)
    if not found:
        await message.answer(texts.REMOVE_NOT_FOUND.format(q=q))
        return
    if len(found) > 1:
        await message.answer(texts.REMOVE_MANY.format(q=q, rows="\n".join(_row(u) for u in found)))
        return
    user = found[0]
    if settings and user.telegram_id in settings.admin_ids:
        await message.answer(texts.REMOVE_SELF)
        return
    if user.excluded_at is not None:
        await message.answer(texts.REMOVE_ALREADY.format(who=display_name(user), tg_id=user.telegram_id))
        return
    team = await teams.get_team_of(session, user)
    code = secrets.token_hex(4)
    _pending[code] = (message.from_user.id, user.id)
    await message.answer(
        texts.REMOVE_PREVIEW.format(
            who=display_name(user),
            tg_id=user.telegram_id,
            team=texts.REMOVE_TEAM.format(team=teams.team_name(team)) if team else "",
            chat=texts.REMOVE_CHAT if team and team.chat_id else "",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Исключить", callback_data=f"rm:yes:{code}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data=f"rm:no:{code}"),
        ]]),
    )


@router.callback_query(F.data.startswith("rm:"), IsAdmin())
async def on_remove(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    _, action, code = (callback.data.split(":") + ["", ""])[:3]
    pending = _pending.pop(code, None)
    if pending is None or pending[0] != callback.from_user.id:
        await callback.answer("Этот запрос устарел — отправь /remove ещё раз.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    if action != "yes":
        await callback.message.answer(texts.REMOVE_CANCELLED)
        return
    user = await session.get(User, pending[1])
    team = await members.exclude(session, user)
    if team is not None and team.chat_id:
        await remove_from_chat(bot, team.chat_id, user.telegram_id)  # тихо: бот в чат ничего не пишет
    await callback.message.answer(texts.REMOVE_DONE.format(
        who=display_name(user), tg_id=user.telegram_id,
        team=texts.REMOVE_TEAM.format(team=teams.team_name(team)) if team else "",
    ))


@router.message(Command("restore"), IsAdmin())
async def cmd_restore(message: Message, command: CommandObject, session: AsyncSession) -> None:
    q = (command.args or "").strip()
    if not q:
        excluded = await members.list_excluded(session)
        await message.answer(
            texts.RESTORE_LIST.format(rows="\n".join(_row(u) for u in excluded)) if excluded else texts.RESTORE_NONE
        )
        return
    found = await members.find_users(session, q)
    if len(found) != 1:
        await message.answer(
            texts.REMOVE_MANY.format(q=q, rows="\n".join(_row(u) for u in found)).replace("/remove", "/restore")
            if found else texts.REMOVE_NOT_FOUND.format(q=q)
        )
        return
    user = found[0]
    if user.excluded_at is None:
        await message.answer(texts.RESTORE_NOT_EXCLUDED.format(who=display_name(user)))
        return
    members.restore(user)
    await message.answer(texts.RESTORE_DONE.format(who=display_name(user)))


@router.message(Command("remove", "restore"))
async def members_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
