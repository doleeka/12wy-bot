"""Сообщения «всей команде»: в групповой чат, если он привязан, иначе каждой в личку."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramMigrateToChat
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings
from bot.models import Team, User
from bot.notify import safe_send
from bot.services import teams

log = logging.getLogger(__name__)


async def notify_admins(bot: Bot, settings: Settings | None, text: str) -> None:
    for admin_id in settings.admin_ids if settings else []:
        await safe_send(bot, admin_id, text)


async def send_to_team_chat(
    bot: Bot, session: AsyncSession, team: Team, text: str, settings: Settings | None = None
) -> bool:
    """Отправляет в чат команды. False — чата нет или он недоступен (тогда он отвязывается)."""
    if team.chat_id is None:
        return False
    try:
        await bot.send_message(team.chat_id, text)
        return True
    except TelegramMigrateToChat as e:
        # группа превратилась в супергруппу и сменила id
        team.chat_id = e.migrate_to_chat_id
        await session.flush()
        try:
            await bot.send_message(team.chat_id, text)
            return True
        except TelegramAPIError as retry_error:
            log.warning("Чат команды %s недоступен после миграции: %s", team.id, retry_error)
    except TelegramAPIError as e:
        log.warning("Чат команды %s недоступен: %s", team.id, e)
    teams.unlink_chat(team)
    await session.flush()
    await notify_admins(bot, settings, texts.ADMIN_CHAT_LOST.format(team=escape(teams.team_name(team))))
    return False


async def notify_team(
    bot: Bot,
    session: AsyncSession,
    team: Team,
    text: str,
    settings: Settings | None = None,
    exclude: User | None = None,
) -> None:
    if await send_to_team_chat(bot, session, team, text, settings):
        return
    for member in await teams.team_members(session, team.id):
        if exclude is None or member.id != exclude.id:
            await safe_send(bot, member.telegram_id, text)


async def create_invite(bot: Bot, team: Team) -> str | None:
    """Ссылка-приглашение в чат команды. Нужны права админа у бота."""
    try:
        link = await bot.create_chat_invite_link(team.chat_id, name=teams.team_name(team)[:32])
        return link.invite_link
    except TelegramAPIError as e:
        log.warning("Не удалось создать приглашение в чат команды %s: %s", team.id, e)
        return None


async def remove_from_chat(bot: Bot, chat_id: int, telegram_id: int) -> bool:
    """Убирает участницу из чата прежней команды (ban + unban = «исключить», вернуться можно по ссылке)."""
    try:
        await bot.ban_chat_member(chat_id, telegram_id)
        await bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)
        return True
    except TelegramAPIError as e:
        log.warning("Не удалось убрать %s из чата %s: %s", telegram_id, chat_id, e)
        return False


def chat_line(team: Team | None) -> str:
    """Строка про чат команды для личных сообщений участнице."""
    if team is None or team.chat_id is None:
        return ""
    if team.invite_link:
        return texts.TEAM_CHAT_LINE.format(link=escape(team.invite_link))
    return texts.TEAM_CHAT_LINE_NO_LINK
