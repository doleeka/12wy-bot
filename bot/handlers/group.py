"""Групповой чат команды: привязка, отвязка, /team в группе, служебные события."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.handlers.teams import team_rows
from bot.notify import display_name, safe_send
from bot.services import teams
from bot.team_notify import create_invite, notify_admins

log = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}

router = Router(name="group")
router.message.filter(F.chat.type.in_(GROUP_TYPES))
router.my_chat_member.filter(F.chat.type.in_(GROUP_TYPES))


@router.message(Command("linkteam"), IsAdmin())
async def cmd_linkteam(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(texts.GROUP_LINK_USAGE)
        return
    try:
        team, previous = await teams.link_chat(session, int(arg), message.chat.id)
    except teams.TeamNotFound:
        await message.answer(texts.ADMIN_TEAM_NOT_FOUND.format(team_id=arg))
        return

    name = escape(teams.team_name(team))
    members = await teams.team_members(session, team.id)
    text = texts.GROUP_LINKED.format(team=name, members=", ".join(display_name(m) for m in members) or "—")
    if previous is not None:
        text += "\n\n" + texts.GROUP_LINK_MOVED.format(team=escape(teams.team_name(previous)))
    await message.answer(text)

    team.invite_link = await create_invite(bot, team)
    if team.invite_link is None:
        await message.answer(texts.GROUP_INVITE_FAILED)
        return
    sent = 0
    for member in members:
        invite = texts.TEAM_CHAT_INVITE.format(team=name, link=escape(team.invite_link))
        sent += await safe_send(bot, member.telegram_id, invite)
    await message.answer(texts.GROUP_INVITE_SENT.format(count=sent, total=len(members)))


@router.message(Command("unlinkteam"), IsAdmin())
async def cmd_unlinkteam_group(message: Message, session: AsyncSession) -> None:
    team = await teams.get_team_by_chat(session, message.chat.id)
    if team is None:
        await message.answer(texts.GROUP_NOT_LINKED)
        return
    teams.unlink_chat(team)
    await message.answer(texts.GROUP_UNLINKED.format(team=escape(teams.team_name(team))))


@router.message(Command("linkteam", "unlinkteam"))
async def group_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)


@router.message(Command("team"))
async def cmd_team_group(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    team = await teams.get_team_by_chat(session, message.chat.id)
    if team is None:
        await message.answer(texts.GROUP_NOT_LINKED)
        return
    rows = await team_rows(session, team, local_today(settings))
    await message.answer(texts.GROUP_TEAM_VIEW.format(team=escape(teams.team_name(team)), rows=rows))


@router.message(F.migrate_to_chat_id)
async def on_group_migrated(message: Message, session: AsyncSession) -> None:
    """Группа стала супергруппой — у неё новый id, переносим привязку."""
    team = await teams.migrate_chat(session, message.chat.id, message.migrate_to_chat_id)
    if team is not None:
        log.info("Чат команды %s: %s → %s", team.id, message.chat.id, message.migrate_to_chat_id)


@router.my_chat_member()
async def on_bot_membership_changed(
    event: ChatMemberUpdated, session: AsyncSession, bot: Bot, settings: Settings | None = None
) -> None:
    """Бота удалили из группы — отвязываем чат, отчёты снова пойдут в личку."""
    if event.new_chat_member.status not in ("left", "kicked"):
        return
    team = await teams.get_team_by_chat(session, event.chat.id)
    if team is None:
        return
    teams.unlink_chat(team)
    await notify_admins(bot, settings, texts.ADMIN_CHAT_LOST.format(team=escape(teams.team_name(team))))
