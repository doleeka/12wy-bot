"""Поддельный Telegram для прогона апдейтов через настоящий Dispatcher (фильтры, роутеры, middleware)."""
from __future__ import annotations

from datetime import datetime
from itertools import count

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import CreateChatInviteLink, GetMe, SendMessage
from aiogram.types import Chat, ChatInviteLink, Message, MessageEntity, Update, User

BOT_USER = User(id=1000, is_bot=True, first_name="12WY", username="twelve_wy_bot")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = {}  # chat_id -> исключение, которое бросить при отправке туда
        self._ids = count(1)

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        self.calls.append(method)
        if isinstance(method, SendMessage) and method.chat_id in self.fail:
            raise self.fail[method.chat_id]
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=method.text
            )
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(
                invite_link="https://t.me/+team", creator=BOT_USER, creates_join_request=False, is_primary=False, is_revoked=False
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def sent(self, chat_id=None) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]

    def of_type(self, cls):
        return [c for c in self.calls if isinstance(c, cls)]


def make_fake_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot("123456:TEST", session=session), session


_update_ids = count(1)


def command_update(text: str, chat_id: int, chat_type: str, user_id: int, first_name: str = "U") -> Update:
    cmd_len = len(text.split()[0])
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_update_ids),
            date=datetime.now(),
            chat=Chat(id=chat_id, type=chat_type, title=None if chat_type == "private" else "Команда"),
            from_user=User(id=user_id, is_bot=False, first_name=first_name),
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=cmd_len)],
        ),
    )
