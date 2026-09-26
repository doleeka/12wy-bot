from datetime import datetime

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat
from aiogram.methods import BanChatMember, CreateChatInviteLink, SendMessage, UnbanChatMember
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Update, User as TgUser

from bot.config import Settings
from bot.handlers.checkin import send_report
from bot.main import build_dispatcher
from bot.models import Team
from bot.services import scorecard, teams
from tests.fake_telegram import BOT_USER, command_update, make_fake_bot
from tests.test_checkin import make_user

ADMIN_ID = 999
GROUP = -100777


@pytest.fixture
async def tg(sessionmaker):
    """Настоящий Dispatcher с роутерами и middleware + поддельный Telegram."""
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])
    yield dp, bot, session
    # роутеры — модульные объекты; отвязываем, чтобы следующий тест мог собрать диспетчер заново
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def send(tg, text, chat_id, chat_type, user_id):
    dp, bot, _ = tg
    await dp.feed_update(bot, command_update(text, chat_id, chat_type, user_id, first_name=f"U{user_id}"))


async def team_of(sessionmaker, tg_id) -> Team:
    async with sessionmaker() as session:
        return await teams.get_team_of(session, await teams.get_user_by_telegram_id(session, tg_id))


# ---------- приватность ----------

async def test_personal_commands_ignored_in_group(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    for cmd in ("/checkin", "/plan", "/start", "/teams", "/moveteam 1 new", "/report 1 none"):
        await send(tg, cmd, GROUP, "supergroup", 1)
    await send(tg, "/teams", GROUP, "supergroup", ADMIN_ID)  # и админские со списком id — тоже
    assert tg[2].sent() == []


async def test_personal_commands_still_work_in_private(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    await send(tg, "/checkin", 1, "private", 1)
    assert "Чек-ин" in tg[2].sent(1)[0]


# ---------- привязка ----------

async def test_linkteam_by_admin(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2)
    team_id = (await team_of(sessionmaker, 1)).id

    await send(tg, f"/linkteam {team_id}", GROUP, "supergroup", ADMIN_ID)
    in_group = tg[2].sent(GROUP)
    assert "Это чат команды" in in_group[0] and "U1" in in_group[0] and "только проценты" in in_group[0]
    assert "2 из 2" in in_group[1]
    assert tg[2].of_type(CreateChatInviteLink)
    assert all("https://t.me/+team" in tg[2].sent(uid)[0] for uid in (1, 2))

    team = await team_of(sessionmaker, 1)
    assert team.chat_id == GROUP and team.invite_link == "https://t.me/+team"

    # /team в личке — с ссылкой на чат
    await send(tg, "/team", 1, "private", 1)
    assert "https://t.me/+team" in tg[2].sent(1)[-1]


async def test_linkteam_non_admin_and_bad_args(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    await send(tg, "/linkteam 1", GROUP, "supergroup", 1)
    await send(tg, "/linkteam abc", GROUP, "supergroup", ADMIN_ID)
    await send(tg, "/linkteam 77", GROUP, "supergroup", ADMIN_ID)
    msgs = tg[2].sent(GROUP)
    assert "только для админа" in msgs[0] and "Использование" in msgs[1] and "нет" in msgs[2]
    assert (await team_of(sessionmaker, 1)).chat_id is None


async def test_invite_failure_is_reported(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    original = tg[2].make_request

    async def no_invites(bot, method, timeout=None):
        if isinstance(method, CreateChatInviteLink):
            raise TelegramForbiddenError(method=method, message="not enough rights")
        return await original(bot, method, timeout)

    tg[2].make_request = no_invites
    await send(tg, "/linkteam 1", GROUP, "group", ADMIN_ID)
    assert "администратором" in tg[2].sent(GROUP)[-1]
    assert (await team_of(sessionmaker, 1)).chat_id == GROUP  # чат привязан и без ссылки


async def test_one_chat_one_team(sessionmaker, tg):
    for uid in (1, 2, 3, 4):
        await make_user(sessionmaker, uid)  # команды 1 (U1-U3) и 2 (U4)
    await send(tg, "/linkteam 1", GROUP, "supergroup", ADMIN_ID)
    await send(tg, "/linkteam 2", GROUP, "supergroup", ADMIN_ID)
    assert "раньше был привязан" in [m for m in tg[2].sent(GROUP) if "Это чат" in m][-1]
    assert (await team_of(sessionmaker, 1)).chat_id is None
    assert (await team_of(sessionmaker, 4)).chat_id == GROUP


async def test_team_in_group_and_unlink(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    await send(tg, "/team", GROUP, "supergroup", 1)
    assert "не привязан" in tg[2].sent(GROUP)[-1]
    await send(tg, "/linkteam 1", GROUP, "supergroup", ADMIN_ID)
    await send(tg, "/team", GROUP, "supergroup", 1)
    assert "U1 — ещё не отмечала" in tg[2].sent(GROUP)[-1] and "Приоритет" not in tg[2].sent(GROUP)[-1]
    await send(tg, "/unlinkteam", GROUP, "supergroup", ADMIN_ID)
    assert "отвязан" in tg[2].sent(GROUP)[-1]
    assert (await team_of(sessionmaker, 1)).chat_id is None


async def test_bot_removed_from_group(sessionmaker, tg):
    await make_user(sessionmaker, 1)
    await send(tg, "/linkteam 1", GROUP, "supergroup", ADMIN_ID)
    dp, bot, session = tg
    chat = Chat(id=GROUP, type="supergroup", title="Команда")
    admin = TgUser(id=ADMIN_ID, is_bot=False, first_name="A")
    event = ChatMemberUpdated(
        chat=chat,
        from_user=admin,
        date=datetime.now(),
        old_chat_member=ChatMemberMember(user=BOT_USER),
        new_chat_member=ChatMemberLeft(user=BOT_USER),
    )
    await dp.feed_update(bot, Update(update_id=9999, my_chat_member=event))
    assert (await team_of(sessionmaker, 1)).chat_id is None
    assert "отчёты идут в личку" in session.sent(ADMIN_ID)[-1]


# ---------- доставка ----------

async def _report(sessionmaker, bot, reporter_tg_id):
    settings = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, reporter_tg_id)
        week = scorecard.week_start(datetime.now().date())
        await send_report(bot, session, user, week, 80, settings)
        await session.commit()


async def _link(sessionmaker, tg_id, chat_id):
    async with sessionmaker() as session:
        team = await teams.get_team_of(session, await teams.get_user_by_telegram_id(session, tg_id))
        await teams.link_chat(session, team.id, chat_id)
        await session.commit()


async def test_report_goes_to_group_not_dms(sessionmaker):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2)
    await _link(sessionmaker, 1, GROUP)
    bot, session = make_fake_bot()
    await _report(sessionmaker, bot, 1)
    assert len(session.sent(GROUP)) == 1 and "80%" in session.sent(GROUP)[0]
    assert session.sent(2) == []


async def test_lost_chat_falls_back_to_dms(sessionmaker):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2)
    await _link(sessionmaker, 1, GROUP)
    bot, session = make_fake_bot()
    session.fail[GROUP] = TelegramForbiddenError(method=SendMessage(chat_id=GROUP, text=""), message="bot was kicked")
    await _report(sessionmaker, bot, 1)
    assert "80%" in session.sent(2)[0]  # ушло в личку
    assert "отчёты идут в личку" in session.sent(ADMIN_ID)[0]
    assert (await team_of(sessionmaker, 1)).chat_id is None


async def test_supergroup_migration_on_send(sessionmaker):
    await make_user(sessionmaker, 1)
    await make_user(sessionmaker, 2)
    await _link(sessionmaker, 1, -555)
    bot, session = make_fake_bot()
    session.fail[-555] = TelegramMigrateToChat(
        method=SendMessage(chat_id=-555, text=""), message="migrated", migrate_to_chat_id=GROUP
    )
    await _report(sessionmaker, bot, 1)
    assert "80%" in session.sent(GROUP)[0] and session.sent(2) == []
    assert (await team_of(sessionmaker, 1)).chat_id == GROUP


async def test_move_removes_from_old_chat(sessionmaker, tg):
    for uid in (1, 2, 3, 4):
        await make_user(sessionmaker, uid)
    await send(tg, "/linkteam 1", GROUP, "supergroup", ADMIN_ID)
    await send(tg, "/moveteam 1 2", ADMIN_ID, "private", ADMIN_ID)
    session = tg[2]
    assert [c.user_id for c in session.of_type(BanChatMember)] == [1]
    assert session.of_type(UnbanChatMember)[0].only_if_banned
    assert any("U1 перешла" in m for m in session.sent(GROUP))  # старой команде — в чат
    assert any("новая участница" in m and "U1" in m for m in session.sent(4))  # новой (без чата) — в личку
