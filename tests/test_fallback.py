from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from bot.commands import set_bot_commands
from bot.config import Settings
from bot.handlers import checkin
from tests.fake_telegram import make_fake_bot
from tests.test_group_chat import ADMIN_ID, send, tg  # noqa: F401 — фикстура tg


async def test_help_for_participant_and_admin(sessionmaker, tg):  # noqa: F811
    await send(tg, "/help", 5, "private", 5)
    await send(tg, "/help", ADMIN_ID, "private", ADMIN_ID)
    participant, admin = tg[2].sent(5)[0], tg[2].sent(ADMIN_ID)[0]
    assert "/checkin" in participant and "/teams" not in participant
    assert "/checkin" in admin and "/teams" in admin and "/backup" in admin


async def test_unknown_command_is_not_silent(sessionmaker, tg):  # noqa: F811
    await send(tg, "/menu", 5, "private", 5)
    assert "Не знаю такой команды" in tg[2].sent(5)[0] and "/start" in tg[2].sent(5)[0]
    await send(tg, "/menu", -100, "supergroup", 5)  # в группе чужие команды не комментируем
    assert tg[2].sent(-100) == []


async def test_known_commands_not_caught_by_fallback(sessionmaker, tg):  # noqa: F811
    await send(tg, "/start", 5, "private", 5)
    assert "Привет" in tg[2].sent(5)[0] and len(tg[2].sent(5)) == 1


async def test_error_reported_to_user_and_admin(sessionmaker, tg, monkeypatch):  # noqa: F811
    async def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(checkin, "get_or_create_user", boom)
    await send(tg, "/checkin", 5, "private", 5)
    assert "что-то пошло не так" in tg[2].sent(5)[0]
    assert "RuntimeError: database is locked" in tg[2].sent(ADMIN_ID)[0]


async def test_set_bot_commands():
    bot, session = make_fake_bot()
    await set_bot_commands(bot, Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID]))
    calls = session.of_type(SetMyCommands)
    scopes = [type(c.scope) for c in calls]
    assert scopes == [BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat]
    admin_cmds = {c.command for c in calls[2].commands}
    assert {"checkin", "teams", "backup"} <= admin_cmds
    assert "teams" not in {c.command for c in calls[0].commands}
