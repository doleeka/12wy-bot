"""Групповые чаты: чат команды (привязка, отвязка, /team), общий чат сообщества (/linkcommon), служебные события."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import community, texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.handlers.teams import team_rows
from bot.notify import display_name, safe_send
from bot.services import payments, teams
from bot.team_notify import COMMON_CHAT, COMMON_THREAD, common_chat_id, create_invite, notify_admins

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


@router.message(Command("linkcommon"), IsAdmin())
async def cmd_linkcommon(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Общий чат сообщества (базовый + фокус-группа, все потоки): итоги недели и ссылки для новеньких."""
    await payments.set_setting(session, COMMON_CHAT, str(message.chat.id))
    # в чате с темами — пишем в ту тему, где отправили команду; в общей ветке или обычном чате — без темы
    thread = message.message_thread_id if message.is_topic_message else None
    await payments.set_setting(session, COMMON_THREAD, str(thread or ""))
    await community.set_topic(session, community.RESULTS, thread)  # итоги — сюда
    await message.answer(texts.COMMON_LINKED)  # в чат — коротко, его видят все; подробности — админу в личку
    details = texts.COMMON_LINKED_ADMIN.format(topic=texts.COMMON_LINKED_ADMIN_TOPIC if thread else "")
    try:  # ссылки новеньким и возврат после оплаты — только если бот админ с правом приглашать
        me = await bot.get_chat_member(message.chat.id, (await bot.me()).id)
        can_invite = me.status == "creator" or getattr(me, "can_invite_users", False)
    except Exception:  # noqa: BLE001
        can_invite = False
    if not can_invite:
        details += "\n\n" + texts.COMMON_INVITE_FAILED
    await safe_send(bot, message.from_user.id, details)


@router.message(Command("topic"), IsAdmin())
async def cmd_topic(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    """/topic встречи — в этой теме общего чата будут встречи (итоги, приветствие, бот, объявления, оплата)."""
    if await common_chat_id(session) != message.chat.id:
        await message.answer(texts.TOPIC_NOT_COMMON)
        return
    kind = community.KIND_ALIASES.get((command.args or "").strip().lower())
    if kind is None:
        rows = []
        for k, thread in (await community.topics(session)).items():
            where = texts.TOPIC_THREAD.format(id=thread) if thread else texts.TOPIC_GENERAL
            rows.append(texts.TOPIC_ROW.format(kind=community.KINDS[k], where=where))
        await safe_send(bot, message.from_user.id, texts.TOPIC_LIST.format(rows="\n".join(rows)) + "\n\n" + texts.TOPIC_USAGE)
        return
    thread = message.message_thread_id if message.is_topic_message else None
    await community.set_topic(session, kind, thread)
    await message.answer(texts.TOPIC_SET.format(kind=community.KINDS[kind]))


@router.message(F.new_chat_members)
async def on_new_members(message: Message, session: AsyncSession, bot: Bot,
                         settings: Settings | None = None) -> None:
    """Новенькие в общем чате — приветствие с кнопкой «Открыть бота» (в тему «приветствие»)."""
    if await common_chat_id(session) != message.chat.id:
        return
    people = [m for m in message.new_chat_members if not m.is_bot]
    if not people:
        return
    names = ", ".join(escape(m.first_name or "") or "подруга" for m in people)
    await community.post(bot, session, settings, community.WELCOME, texts.CHAT_WELCOME.format(names=names),
                         await community.open_bot_button(bot))


@router.message(Command("unlinkcommon"), IsAdmin())
async def cmd_unlinkcommon(message: Message, session: AsyncSession) -> None:
    if await common_chat_id(session) == message.chat.id:
        await payments.set_setting(session, COMMON_CHAT, "")
        await message.answer(texts.COMMON_UNLINKED)
    else:
        await message.answer(texts.GROUP_NOT_LINKED)


@router.message(Command("linkteam", "unlinkteam", "linkcommon", "unlinkcommon", "topic"))
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
