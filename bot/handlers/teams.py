"""Команды: автораспределение после онбординга, /team, админские /teams, /moveteam, /solo и /unsolo."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.models import OnboardingStep, Team, User
from bot.notify import display_name, safe_send
from bot.team_notify import chat_line, notify_team, remove_from_chat, send_common_link
from bot.services import members as members_svc, scorecard, teams
from bot.services.users import get_or_create_user

router = Router(name="teams")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


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
        await message.answer(texts.TEAM_KEPT.format(team=escape(teams.team_name(existing))) + chat_line(existing))
        return existing
    if user.no_team:  # организатор вне команд или самостоятельное прохождение
        if settings and user.telegram_id in settings.admin_ids:
            await message.answer(texts.TEAM_NONE_ORGANIZER)
            return None
        await message.answer("Ты проходишь цикл самостоятельно и остаёшься частью сообщества. Твой план и прогресс сохраняются.")
        for admin_id in settings.admin_ids if settings else []:
            await safe_send(bot, admin_id, texts.ADMIN_USER_READY.format(
                name=display_name(user), tg_id=user.telegram_id, team=texts.SOLO_TEAM_LABEL, new=""))
        return None
    team, created = await teams.assign_to_team(session, user)
    members = await teams.team_members(session, team.id)
    name = escape(teams.team_name(team))
    await message.answer(texts.TEAM_JOINED.format(team=name, mates=_mates_text(user, members)) + chat_line(team))
    await send_common_link(bot, session, user)  # «Базовый» и «Фокус-группа» — ещё и в общий чат сообщества
    await notify_team(
        bot, session, team, texts.TEAM_NEW_MEMBER.format(team=name, name=display_name(user)), settings, exclude=user
    )
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
        if team is None:  # организатор вне команд — сообщение уже отправлено
            return

    today = local_today(settings)
    n = scorecard.week_number(user.cycle_start, today)
    if n is not None:
        week = texts.TEAM_WEEK.format(n=n)
    elif user.cycle_start is None:
        week = texts.TEAM_WEEK_PLANNING
    elif user.cycle_start and today < user.cycle_start:
        week = texts.TEAM_WEEK_NOT_STARTED.format(start=user.cycle_start.strftime("%d.%m"))
    else:
        week = texts.TEAM_WEEK_OVER

    rows = await team_rows(session, team, today, viewer=user)
    await message.answer(
        texts.TEAM_VIEW.format(team=escape(teams.team_name(team)), week=week, rows=rows) + chat_line(team)
    )


async def team_rows(session: AsyncSession, team: Team, today, viewer: User | None = None) -> str:  # noqa: ANN001
    """Строки «имя — % за эту неделю (прошлая)». Только проценты — без целей и тактик."""
    this_week = scorecard.week_start(today)
    prev_week = this_week - timedelta(days=7)
    rows = []
    for member in await teams.team_members(session, team.id):
        current = await scorecard.week_percent(session, member, this_week)
        prev = await scorecard.week_percent(session, member, prev_week)
        rows.append(
            texts.TEAM_ROW.format(
                emoji=scorecard.rating_emoji(current),
                name=escape(member.first_name or "") or "Участница",
                you=" (ты)" if viewer is not None and member.id == viewer.id else "",
                current=_fmt_percent(current),
                prev=texts.TEAM_PREV.format(value=f"{prev}%") if prev is not None else "",
            )
        )
    return "\n".join(rows)


# ---------- админ ----------

@router.message(Command("teams"), IsAdmin())
async def cmd_teams(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    blocks = []
    for team, members in await teams.list_teams(session):
        member_lines = "\n".join(
            texts.ADMIN_TEAMS_MEMBER.format(name=display_name(m), tg_id=m.telegram_id) for m in members
        ) or "   —"
        blocks.append(
            texts.ADMIN_TEAMS_ROW.format(
                team=escape(teams.team_name(team)) + (texts.ADMIN_TEAMS_CHAT if team.chat_id else ""),
                id=team.id,
                count=len(members),
                members=member_lines,
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
    solo = [u for u in await teams.list_solo(session) if not settings or u.telegram_id not in settings.admin_ids]
    if solo:
        blocks.append(texts.ADMIN_TEAMS_SOLO.format(count=len(solo), members="\n".join(
            texts.ADMIN_TEAMS_MEMBER.format(name=display_name(u), tg_id=u.telegram_id) for u in solo
        )))
    await message.answer("\n\n".join(blocks) or texts.ADMIN_TEAMS_EMPTY)


async def make_solo(bot: Bot, session: AsyncSession, user: User, settings: Settings | None) -> tuple[Team | None, bool, bool]:
    """Самостоятельное прохождение: выйти из тройки и не попадать в распределение.

    Ей — одно сообщение, что всё сохранено; прежней тройке — что она теперь сама; из чата тройки убираем,
    чтобы не видела чужие отчёты. Возвращает (прежняя команда, доставлено ли ей, убрана ли из чата).
    """
    was_solo = user.no_team
    user.no_team = True
    user.tariff = "app"  # без тройки — это тариф «Приложение»
    old_team = await teams.leave_team(session, user)
    await session.commit()
    delivered = removed = True
    if old_team is not None or not was_solo:
        delivered = await safe_send(bot, user.telegram_id, texts.TEAM_NOW_INDEPENDENT)
    if old_team is not None:
        await notify_team(bot, session, old_team, texts.TEAM_MEMBER_INDEPENDENT.format(name=display_name(user)), settings)
        if old_team.chat_id:
            removed = await remove_from_chat(bot, old_team.chat_id, user.telegram_id)
    return old_team, delivered, removed


async def apply_tariff_team(bot: Bot, session: AsyncSession, user: User, settings: Settings | None) -> None:
    """После оплаты: команда по тарифу. «Приложение» — без тройки; «Базовый» — тройка; «Фокус-группа» —
    фокус-группа потока. Кто ещё не закончил онбординг — распределится в конце онбординга, как обычно."""
    team = await teams.get_team_of(session, user)
    if user.no_team:
        if team is not None:
            await teams.leave_team(session, user)
            await session.commit()
            await notify_team(bot, session, team, texts.TEAM_MEMBER_INDEPENDENT.format(name=display_name(user)), settings)
            if team.chat_id:
                await remove_from_chat(bot, team.chat_id, user.telegram_id)
        return
    if not user.is_ready or (team is not None and team.is_focus == teams.wants_focus(user)):
        return
    if team is not None:
        await teams.leave_team(session, user)
        await session.commit()
        await notify_team(bot, session, team, texts.TEAM_MEMBER_LEFT.format(name=display_name(user)), settings)
        if team.chat_id:
            await remove_from_chat(bot, team.chat_id, user.telegram_id)
    new_team, _ = await teams.assign_to_team(session, user)
    if new_team is None:
        return
    name = escape(teams.team_name(new_team))
    members = await teams.team_members(session, new_team.id)
    await safe_send(bot, user.telegram_id,
                    texts.TEAM_JOINED.format(team=name, mates=_mates_text(user, members)) + chat_line(new_team))
    await notify_team(bot, session, new_team, texts.TEAM_NEW_MEMBER.format(team=name, name=display_name(user)),
                      settings, exclude=user)


@router.message(Command("moveteam"), IsAdmin())
async def cmd_moveteam(
    message: Message, command: CommandObject, session: AsyncSession, bot: Bot, settings: Settings | None = None
) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not (args[1].isdigit() or args[1].lower() in {"new", "none", "off"}):
        await message.answer(texts.ADMIN_MOVE_USAGE)
        return
    tg_id = int(args[0])
    destination = args[1].lower()
    team_id = int(destination) if destination.isdigit() else None

    user = await teams.get_user_by_telegram_id(session, tg_id)
    if user is None:
        await message.answer(texts.ADMIN_USER_NOT_FOUND.format(tg_id=tg_id))
        return
    if destination in {"none", "off"}:
        await _report_solo(message, user, *await make_solo(bot, session, user, settings))
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
    delivered = await safe_send(
        bot,
        user.telegram_id,
        texts.TEAM_MOVED.format(team=escape(new_name), mates=_mates_text(user, members)) + chat_line(new_team),
    )
    if not delivered:
        await message.answer(texts.ADMIN_MOVE_NOTIFY_FAILED.format(name=display_name(user)))
    await notify_team(
        bot,
        session,
        new_team,
        texts.TEAM_NEW_MEMBER.format(team=escape(new_name), name=display_name(user)),
        settings,
        exclude=user,
    )
    if old_team is not None:
        await notify_team(bot, session, old_team, texts.TEAM_MEMBER_LEFT.format(name=display_name(user)), settings)
        # чтобы бывшая участница не видела отчёты прежней команды
        if old_team.chat_id and not await remove_from_chat(bot, old_team.chat_id, user.telegram_id):
            await message.answer(texts.ADMIN_KICK_FAILED.format(name=display_name(user)))


@router.message(Command("regroup"), IsAdmin())
async def cmd_regroup(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                      settings: Settings | None = None) -> None:
    """Пересобрать тройки, где остались по одной: сначала план, с confirm — переводы и уведомления."""
    from bot.team_notify import personal_invite  # noqa: PLC0415

    plan = await teams.regroup_plan(session)
    if not plan:
        await message.answer(texts.REGROUP_NOTHING)
        return
    rows = "\n".join(texts.REGROUP_ROW.format(name=display_name(u), old=escape(teams.team_name(o)),
                                               new=escape(teams.team_name(n))) for u, o, n in plan)
    if (command.args or "").strip().lower() != "confirm":
        await message.answer(texts.REGROUP_PREVIEW.format(n=len(plan), rows=rows))
        return
    freed_chats = []
    for user, old_team, new_team in plan:
        await teams.move_to_team(session, user, new_team.id)
        members = await teams.team_members(session, new_team.id)
        name = escape(teams.team_name(new_team))
        link = await personal_invite(bot, new_team.chat_id, teams.team_name(new_team)) if new_team.chat_id else None
        chat = texts.CHAT_LINK_TEAM.format(team=name, link=escape(link)) if link else chat_line(new_team).strip()
        await safe_send(bot, user.telegram_id,
                        texts.TEAM_MOVED.format(team=name, mates=_mates_text(user, members)) + ("\n" + chat if chat else ""))
        await notify_team(bot, session, new_team, texts.TEAM_NEW_MEMBER.format(team=name, name=display_name(user)),
                          settings, exclude=user)
        if old_team.chat_id:
            await remove_from_chat(bot, old_team.chat_id, user.telegram_id)
            if not await teams.team_members(session, old_team.id):
                freed_chats.append(escape(teams.team_name(old_team)))
    text = texts.REGROUP_DONE.format(n=len(plan), rows=rows)
    if freed_chats:
        text += texts.REGROUP_FREED_CHATS.format(teams=", ".join(freed_chats))
    await message.answer(text + "\n\n" + texts.REGROUP_SEE_TEAMS)


async def _report_solo(message: Message, user: User, old_team: Team | None, delivered: bool, removed: bool) -> None:
    old = escape(teams.team_name(old_team)) if old_team else texts.SOLO_WAS_ALONE
    text = texts.SOLO_DONE.format(who=display_name(user), old=old, tg_id=user.telegram_id)
    if old_team is not None and old_team.chat_id and removed:
        text += texts.SOLO_CHAT_REMOVED.format(team=escape(teams.team_name(old_team)))
    await message.answer(text)
    if not delivered:
        await message.answer(texts.ADMIN_MOVE_NOTIFY_FAILED.format(name=display_name(user)))
    if not removed:
        await message.answer(texts.ADMIN_KICK_FAILED.format(name=display_name(user)))


def _row(u: User) -> str:
    return texts.ADMIN_TEAMS_MEMBER.format(name=display_name(u), tg_id=u.telegram_id)


async def _find_one(message: Message, session: AsyncSession, q: str, cmd: str) -> User | None:
    found = await members_svc.find_users(session, q)
    if not found:
        await message.answer(texts.REMOVE_NOT_FOUND.format(q=escape(q)))
    elif len(found) > 1:
        await message.answer(texts.SOLO_MANY.format(q=escape(q), rows="\n".join(_row(u) for u in found), cmd=cmd))
    else:
        return found[0]
    return None


async def solo_link(bot: Bot) -> str:
    from bot.handlers.start import SOLO_START
    return f"https://t.me/{(await bot.me()).username}?start={SOLO_START}"


@router.message(Command("solo"), IsAdmin())
async def cmd_solo(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                   settings: Settings | None = None) -> None:
    """Только приложение, без тройки: /solo — список, /solo @ник|id|Имя — перевести."""
    q = (command.args or "").strip()
    if not q:
        solo = [u for u in await teams.list_solo(session) if not settings or u.telegram_id not in settings.admin_ids]
        link = texts.SOLO_LINK.format(link=await solo_link(bot))
        await message.answer((texts.SOLO_LIST.format(count=len(solo), rows="\n".join(_row(u) for u in solo))
                              if solo else texts.SOLO_NONE + "\n\n" + texts.SOLO_USAGE) + "\n\n" + link)
        return
    user = await _find_one(message, session, q, "solo")
    if user is None:
        return
    if settings and user.telegram_id in settings.admin_ids:
        await message.answer(texts.SOLO_ADMIN)
    elif user.excluded_at is not None:
        await message.answer(texts.SOLO_EXCLUDED.format(who=display_name(user)))
    elif user.no_team:
        await message.answer(texts.SOLO_ALREADY.format(who=display_name(user), tg_id=user.telegram_id))
    else:
        await _report_solo(message, user, *await make_solo(bot, session, user, settings))


@router.message(Command("unsolo"), IsAdmin())
async def cmd_unsolo(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                     settings: Settings | None = None) -> None:
    """Вернуть в распределение по тройкам; если онбординг пройден — сразу в тройку со свободным местом."""
    q = (command.args or "").strip()
    if not q:
        await message.answer(texts.SOLO_USAGE)
        return
    user = await _find_one(message, session, q, "unsolo")
    if user is None:
        return
    if user.excluded_at is not None:
        await message.answer(texts.SOLO_EXCLUDED.format(who=display_name(user)))
        return
    if not user.no_team:
        await message.answer(texts.UNSOLO_NOT_SOLO.format(who=display_name(user)))
        return
    user.no_team = False
    if user.tariff in (None, "app"):
        user.tariff = "basic"  # снова в тройке — тариф «Базовый»
    await session.commit()
    if not user.is_ready:
        await message.answer(texts.UNSOLO_LATER.format(who=display_name(user)))
        return
    team, created = await teams.assign_to_team(session, user)
    name = escape(teams.team_name(team))
    await message.answer(texts.UNSOLO_DONE.format(who=display_name(user), team=name, new=" (новая команда)" if created else ""))
    members = await teams.team_members(session, team.id)
    if not await safe_send(bot, user.telegram_id,
                           texts.TEAM_MOVED.format(team=name, mates=_mates_text(user, members)) + chat_line(team)):
        await message.answer(texts.ADMIN_MOVE_NOTIFY_FAILED.format(name=display_name(user)))
    await notify_team(bot, session, team, texts.TEAM_NEW_MEMBER.format(team=name, name=display_name(user)),
                      settings, exclude=user)


@router.message(Command("unlinkteam"), IsAdmin())
async def cmd_unlinkteam(message: Message, command: CommandObject, session: AsyncSession) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(texts.ADMIN_UNLINK_USAGE)
        return
    team = await session.get(Team, int(arg))
    if team is None:
        await message.answer(texts.ADMIN_TEAM_NOT_FOUND.format(team_id=arg))
        return
    teams.unlink_chat(team)
    await message.answer(texts.ADMIN_UNLINKED.format(team=escape(teams.team_name(team))))


@router.message(Command("teams", "moveteam", "unlinkteam", "solo", "unsolo", "regroup"))
async def admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
