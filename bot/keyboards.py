from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.wheel import MAX_SCORE, MIN_SCORE, Sphere

# callback_data (≤ 64 байт):
#   wheel:begin
#   wheel:rate:<sphere>:<score>
#   wheel:extra:<sphere>
#   wheel:finish


def wheel_begin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Начать колесо баланса 🎡", callback_data="wheel:begin")]]
    )


def wheel_scores(sphere: Sphere) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for score in range(MIN_SCORE, MAX_SCORE + 1):
        builder.button(text=str(score), callback_data=f"wheel:rate:{sphere.key}:{score}")
    builder.adjust(5, 5)
    return builder.as_markup()


def wheel_extras(extras: list[Sphere]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sphere in extras:
        builder.button(text=f"{sphere.emoji} {sphere.title}", callback_data=f"wheel:extra:{sphere.key}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="Нет, покажи результат →", callback_data="wheel:finish"))
    return builder.as_markup()
