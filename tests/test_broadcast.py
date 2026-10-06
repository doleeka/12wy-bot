"""Рассылка админа: /broadcast текст → предпросмотр (никому не уходит) → «Отправить» / «Отмена»."""
from datetime import datetime
from itertools import count

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

from bot.config import Settings
from bot.main import build_dispatcher
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send

_ids = count(10_000)


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID])
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


def buttons(session, chat_id):
    msg = [c for c in session.of_type(SendMessage) if c.chat_id == chat_id and c.reply_markup][-1]
    return [b.callback_data for b in msg.reply_markup.inline_keyboard[0]]


async def press(tg, data, user_id=ADMIN_ID):
    dp, bot, _ = tg
    message = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"), text="preview")
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=TgUser(id=user_id, is_bot=False, first_name="A"), chat_instance="x",
        message=message, data=data,
    )))


async def test_preview_then_send_to_everyone_but_me(sessionmaker, tg):
    await make_user(sessionmaker, ADMIN_ID)
    for tg_id in (1, 2, 3):
        await make_user(sessionmaker, tg_id)
    tg[2].calls.clear()
    await send(tg, "/broadcast Девочки, план можно поправить до 7 октября <важно>\nВторая строка", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "Предпросмотр" in preview and "получат: <b>3</b>" in preview and "&lt;важно&gt;\nВторая строка" in preview
    assert all(not tg[2].sent(i) for i in (1, 2, 3))  # предпросмотр никому не уходит
    send_btn, cancel_btn = buttons(tg[2], ADMIN_ID)
    assert send_btn.startswith("bc:send:") and cancel_btn.startswith("bc:cancel:")

    tg[2].fail[3] = TelegramForbiddenError(method=None, message="bot was blocked by the user")
    await press(tg, send_btn)
    for i in (1, 2):
        assert tg[2].sent(i) == ["Девочки, план можно поправить до 7 октября &lt;важно&gt;\nВторая строка"]
    assert "доставлено 2 из 3" in tg[2].sent(ADMIN_ID)[-1] and "Не доставлено 1" in tg[2].sent(ADMIN_ID)[-1]
    # повторное нажатие той же кнопки — ничего не шлёт
    before = len(tg[2].sent(1))
    await press(tg, send_btn)
    assert len(tg[2].sent(1)) == before


async def test_cancel_sends_nothing(sessionmaker, tg):
    await make_user(sessionmaker, ADMIN_ID)
    await make_user(sessionmaker, 1)
    tg[2].calls.clear()
    await send(tg, "/broadcast Привет", ADMIN_ID, "private", ADMIN_ID)
    _, cancel_btn = buttons(tg[2], ADMIN_ID)
    await press(tg, cancel_btn)
    assert "отменена" in tg[2].sent(ADMIN_ID)[-1] and not tg[2].sent(1)


async def test_usage_and_admin_only(sessionmaker, tg):
    await make_user(sessionmaker, ADMIN_ID)
    await make_user(sessionmaker, 1)
    await send(tg, "/broadcast", ADMIN_ID, "private", ADMIN_ID)
    assert "предпросмотр" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/broadcast всем привет", 1, "private", 1)
    assert "только для админа" in tg[2].sent(1)[-1]
    # чужая кнопка не срабатывает
    await send(tg, "/broadcast Привет", ADMIN_ID, "private", ADMIN_ID)
    send_btn, _ = buttons(tg[2], ADMIN_ID)
    tg[2].calls.clear()
    await press(tg, send_btn, user_id=1)
    assert not tg[2].sent(1) or "Привет" not in tg[2].sent(1)[-1]
