"""Встречи сообщества: /meeting (разовая или каждую неделю) — анонс в общий чат, напоминания — в scheduler."""
from __future__ import annotations

import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import community, texts
from bot.config import Settings, local_now
from bot.filters import IsAdmin
from bot.models import Meeting
from bot.services import meetings

router = Router(name="meetings")
router.message.filter(F.chat.type == "private")

_pending: dict[str, tuple[int, meetings.Parsed]] = {}


def post_text(meeting: Meeting, occ) -> str:  # noqa: ANN001
    return texts.MEETING_POST.format(when=meetings.when_text(meeting, occ), details=escape(meeting.details))


@router.message(Command("meeting"), IsAdmin())
async def cmd_meeting(message: Message, command: CommandObject, session: AsyncSession,
                      settings: Settings | None = None) -> None:
    args = (command.args or "").strip()
    parts = args.split()
    if parts[:1] == ["off"] and len(parts) == 2 and parts[1].isdigit():
        meeting = await session.get(Meeting, int(parts[1]))
        if meeting is None or not meeting.active:
            await message.answer(texts.MEETING_NOT_FOUND.format(id=parts[1]))
            return
        meeting.active = False
        await message.answer(texts.MEETING_OFF.format(id=meeting.id))
        return
    if not args:
        await message.answer(texts.MEETING_USAGE)
        return
    parsed = meetings.parse(args, local_now(settings))
    if parsed is None:
        await message.answer(texts.MEETING_BAD + texts.MEETING_USAGE)
        return
    code = secrets.token_hex(4)
    _pending[code] = (message.from_user.id, parsed)
    preview = Meeting(details=parsed.details, starts_at=parsed.starts_at, weekly=parsed.weekly)
    await message.answer(texts.MEETING_PREVIEW.format(post=post_text(preview, parsed.starts_at)),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="📅 Опубликовать", callback_data=f"mt:yes:{code}"),
                             InlineKeyboardButton(text="✖ Отмена", callback_data=f"mt:no:{code}")]]))


@router.callback_query(F.data.startswith("mt:"), IsAdmin())
async def on_meeting(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    _, action, code = (callback.data.split(":") + ["", ""])[:3]
    pending = _pending.pop(code, None)
    if pending is None or pending[0] != callback.from_user.id:
        await callback.answer(texts.ANNOUNCE_STALE.replace("/announce", "/meeting"), show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if action != "yes":
        await bot.send_message(callback.from_user.id, texts.ANNOUNCE_CANCELLED)
        return
    meeting = await meetings.create(session, pending[1], callback.from_user.id, local_now(settings))
    await session.commit()
    ok = await community.post(bot, session, settings, community.MEETINGS, post_text(meeting, meeting.starts_at))
    await bot.send_message(callback.from_user.id,
                           (texts.MEETING_SAVED if ok else texts.MEETING_SAVED_NOT_POSTED).format(id=meeting.id))


@router.message(Command("meetings"), IsAdmin())
async def cmd_meetings(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    now = local_now(settings)
    rows = []
    for m in await meetings.active(session):
        occ = meetings.next_occurrence(m, now)
        if occ is not None:
            rows.append(texts.MEETING_ROW.format(id=m.id, when=meetings.when_text(m, occ), details=escape(m.details)))
    await message.answer(texts.MEETING_LIST.format(rows="\n".join(rows)) if rows
                         else texts.MEETING_NONE + texts.MEETING_USAGE)


@router.message(Command("meeting", "meetings"))
async def meetings_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
