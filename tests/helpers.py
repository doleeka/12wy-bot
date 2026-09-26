from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

TG_USER = SimpleNamespace(id=42, username="anna", first_name="Анна")


def make_message(text=None):
    return SimpleNamespace(
        from_user=TG_USER, text=text, answer=AsyncMock(), edit_text=AsyncMock(), edit_reply_markup=AsyncMock()
    )


def make_callback(data, message):
    return SimpleNamespace(from_user=TG_USER, data=data, message=message, answer=AsyncMock())


def make_state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_USER.id, user_id=TG_USER.id))


def all_texts(mock: AsyncMock) -> str:
    return "\n".join(call.args[0] for call in mock.call_args_list)
