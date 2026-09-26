"""Команды: автораспределение после онбординга, /team, админские /teams и /moveteam."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.models import OnboardingStep, Team, User
from bot.notify import display_name, safe_send
from bot.services import scorecard, teams
from bot.services.users import get_or_create_user

router = Router(name="teams")


def _mates_text(user: User, members: list[User]) -> str:
    mates = [m for m in members if m.id != user.id]
    if not mates:
        return texts.TEAM_FIRST
    return texts.TEAM_MATES.format(names=", ".join(display_name(m) for m in mates))


async def join_team(
    message: Message, bot: Bot, session: AsyncSession, user: User, settings: Settings | None
) -> Team:
    """Шаг 7 онбординга: распределить в команду и всем сообщить.

    В новом цикле участница уже в команде — тогда просто напоминаем, какая у неё команда.
    """
    existing = await teams.get_team_of(session, user)
    if existing is not None:
        await message.answer(texts.TEAM_KEPT.format(team=escape(teams.team_name(existing))))
        return existing
    team, created = await teams.assign_to_team(session, user)
    members = await teams.team_members(session, team.id)
    name = escape(teams.team_name(team))
    await message.answer(texts.TEAM_JOINED.format(team=name, mates=_mates_text(user, members)))
    for mate in members:
        if mate.id != user.id:
            await safe_send(bot, mate.telegram_id, texts.TEAM_NEW_MEMBER.format(team=name, name=display_name(user)))
    for admin_id in settings.admin_ids if settings else []:
        await safe_send(
            bot,
            admin_id,
            texts.ADMIN_USER_READY.format(
                name=display_name(user), tg_id=user.telegram_id, team=name, new=" (новая команда)" if created else ""
            ),
        )
    return team


# ---------- участница ----------

def _fmt_percent(value: int | None) -> str:
    return texts.TEAM_NO_DATA if value is None else f"{value}%"


@router.message(Command("team"))
async def cmd_team(message: Message, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    team = await teams.get_team_of(session, user)
    if team is None:
        if user.onboarding_step != OnboardingStep.DONE:
            await message.answer(texts.TEAM_NONE_YET)
            return
        team = await join_team(message, bot, session, user, settings)

    today = local_today(settings)
    this_week = scorecard.week_start(today)
    prev_week = this_week - timedelta(days=7)

    n = scorecard.week_number(user.cycle_start, today)
    if n is not None:
        week = texts.TEAM_WEEK.format(n=n)
    elif user.cycle_start is None:
        week = texts.TEAM_WEEK_PLANNING
    elif user.cycle_start and today < user.cycle_start:
        week = texts.TEAM_WEEK_NOT_STARTED.format(start=user.cycle_start.strftime("%d.%m"))
    else:
        week = texts.TEAM_WEEK_OVER

    rows = []
    for member in await teams.team_members(session, team.id):
        current = await scorecard.week_percent(session, member, this_week)
        prev = await scorecard.week_percent(session, member, prev_week)
        rows.append(
            texts.TEAM_ROW.format(
                emoji=scorecard.rating_emoji(current),
                name=escape(member.first_name or "") or "Участница",
                you=" (ты)" if member.id == user.id else "",
                current=_fmt_percent(current),
                prev=texts.TEAM_PREV.format(value=f"{prev}%") if prev is not None else "",
            )
        )
    await message.answer(texts.TEAM_VIEW.format(team=escape(teams.team_name(team)), week=week, rows="\n".join(rows)))


# ---------- админ ----------

@router.message(Command("teams"), IsAdmin())
async def cmd_teams(message: Message, session: AsyncSession) -> None:
    blocks = []
    for team, members in await teams.list_teams(session):
        member_lines = "\n".join(
            texts.ADMIN_TEAMS_MEMBER.format(name=display_name(m), tg_id=m.telegram_id) for m in members
        ) or "   —"
        blocks.append(
            texts.ADMIN_TEAMS_ROW.format(
                team=escape(teams.team_name(team)), id=team.id, count=len(members), members=member_lines
            )
        )
    unassigned = await teams.ready_without_team(session)
    if unassigned:
        blocks.append(
            texts.ADMIN_TEAMS_UNASSIGNED.format(
                members="\n".join(
                    texts.ADMIN_TEAMS_MEMBER.format(name=display_name(u), tg_id=u.telegram_id) for u in unassigned
                )
            )
        )
    await message.answer("\n\n".join(blocks) or texts.ADMIN_TEAMS_EMPTY)


@router.message(Command("moveteam"), IsAdmin())
async def cmd_moveteam(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not (args[1].isdigit() or args[1].lower() == "new"):
        await message.answer(texts.ADMIN_MOVE_USAGE)
        return
    tg_id = int(args[0])
    team_id = None if args[1].lower() == "new" else int(args[1])

    user = await teams.get_user_by_telegram_id(session, tg_id)
    if user is None:
        await message.answer(texts.ADMIN_USER_NOT_FOUND.format(tg_id=tg_id))
        return
    if not user.is_ready:
        await message.answer(texts.ADMIN_USER_NOT_READY.format(name=display_name(user)))
        return
    try:
        old_team, new_team = await teams.move_to_team(session, user, team_id)
    except teams.TeamNotFound:
        await message.answer(texts.ADMIN_TEAM_NOT_FOUND.format(team_id=team_id))
        return
    except teams.TeamFull:
        await message.answer(texts.ADMIN_TEAM_FULL.format(team_id=team_id))
        return

    new_name = teams.team_name(new_team)
    old_name = teams.team_name(old_team) if old_team else "без команды"
    await message.answer(texts.ADMIN_MOVED.format(name=display_name(user), team=escape(new_name), old=escape(old_name)))
    if old_team is not None and old_team.id == new_team.id:
        return

    members = await teams.team_members(session, new_team.id)
    await safe_send(bot, user.telegram_id, texts.TEAM_MOVED.format(team=escape(new_name), mates=_mates_text(user, members)))
    for mate in members:
        if mate.id != user.id:
            await safe_send(bot, mate.telegram_id, texts.TEAM_NEW_MEMBER.format(team=escape(new_name), name=display_name(user)))
    if old_team is not None:
        for mate in await teams.team_members(session, old_team.id):
            await safe_send(bot, mate.telegram_id, texts.TEAM_MEMBER_LEFT.format(name=display_name(user)))


@router.message(Command("teams", "moveteam"))
async def admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
