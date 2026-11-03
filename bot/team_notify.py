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


# ---------- общий чат (базовый + фокус-группа, все потоки) и пауза в чатах до оплаты ----------

COMMON_CHAT = "common_chat_id"


async def common_chat_id(session: AsyncSession) -> int | None:
    from bot.services import payments  # noqa: PLC0415 — payments импортирует teams; без цикла

    raw = await payments.get_setting(session, COMMON_CHAT)
    return int(raw) if raw and raw.lstrip("-").isdigit() else None


def in_common_chat(user: User) -> bool:
    """В общий чат — «Базовый» и «Фокус-группа»; «Приложение» (без тройки) — нет."""
    return not user.no_team


async def personal_invite(bot: Bot, chat_id: int, name: str) -> str | None:
    """Одноразовая личная ссылка в чат (по ней войдёт один человек). Нужны права админа у бота."""
    try:
        link = await bot.create_chat_invite_link(chat_id, name=name[:32], member_limit=1)
        return link.invite_link
    except TelegramAPIError as e:
        log.warning("Не удалось создать ссылку в чат %s: %s", chat_id, e)
        return None


async def chat_links(bot: Bot, session: AsyncSession, user: User, common: bool = True, team_chat: bool = True) -> str:
    """Строки со ссылками в её чаты: тройки / фокус-группы и общий. Пусто — чатов нет."""
    lines = []
    team = await teams.get_team_of(session, user) if team_chat else None
    if team is not None and team.chat_id:
        link = await personal_invite(bot, team.chat_id, teams.team_name(team)) or team.invite_link
        if link:
            lines.append(texts.CHAT_LINK_TEAM.format(team=escape(teams.team_name(team)), link=escape(link)))
    common_id = await common_chat_id(session) if common and in_common_chat(user) else None
    if common_id:
        link = await personal_invite(bot, common_id, "Общий чат")
        if link:
            lines.append(texts.CHAT_LINK_COMMON.format(link=escape(link)))
    return "\n".join(lines)


async def send_common_link(bot: Bot, session: AsyncSession, user: User) -> bool:
    links = await chat_links(bot, session, user, team_chat=False)
    return bool(links) and await safe_send(bot, user.telegram_id, texts.CHAT_LINKS_JOIN.format(links=links))


async def pause_chats(bot: Bot, session: AsyncSession, user: User) -> None:
    """Доступ на паузе до оплаты: тихо убираем из чата тройки / фокус-группы и из общего чата."""
    if user.chats_paused:
        return
    team = await teams.get_team_of(session, user)
    chats = [team.chat_id] if team is not None and team.chat_id else []
    common_id = await common_chat_id(session)
    if common_id and in_common_chat(user):
        chats.append(common_id)
    for chat_id in chats:
        await remove_from_chat(bot, chat_id, user.telegram_id)
    user.chats_paused = True
    await session.flush()


async def restore_chats(bot: Bot, session: AsyncSession, user: User) -> None:
    """После оплаты — личные ссылки вернуться в те чаты, из которых бот убрал её на паузе."""
    if not user.chats_paused:
        return
    user.chats_paused = False
    await session.flush()
    links = await chat_links(bot, session, user)
    if links:
        await safe_send(bot, user.telegram_id, texts.CHAT_LINKS_BACK.format(links=links))
