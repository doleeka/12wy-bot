from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.wheel import MAX_SCORE, MIN_SCORE, Sphere

# callback_data (≤ 64 байт):
#   wheel:begin
#   wheel:rate:<sphere>:<score>
#   wheel:extra:<sphere>
#   wheel:finish


def open_app(webapp_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌿 Открыть приложение", web_app=WebAppInfo(url=webapp_url))]]
    )


def wheel_begin(webapp_url: str = "") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Начать колесо баланса 🎡", callback_data="wheel:begin")]]
    if webapp_url:
        # Mini App — основной путь; кнопка в чате остаётся запасной, пока все шаги не переехали в приложение
        rows.insert(0, [InlineKeyboardButton(text="🌿 Открыть приложение", web_app=WebAppInfo(url=webapp_url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


# ---------- Explore / Eliminate / Tactics ----------
#   explore:done | explore:delmenu | explore:del:<id> | explore:back
#   elim:t:<id> | elim:ok | elim:back
#   tac:keep | tac:next:<position>

BUTTON_TEXT_LEN = 40


def short(text: str, limit: int = BUTTON_TEXT_LEN) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def explore_list(has_items: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_items:
        builder.button(text="✕ Убрать пункт", callback_data="explore:delmenu")
    builder.button(text="Готово →", callback_data="explore:done")
    builder.adjust(2)
    return builder.as_markup()


def explore_delete(items) -> InlineKeyboardMarkup:  # noqa: ANN001
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=f"✕ {short(item.text)}", callback_data=f"explore:del:{item.id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="← Назад", callback_data="explore:back"))
    return builder.as_markup()


def eliminate(items) -> InlineKeyboardMarkup:  # noqa: ANN001
    builder = InlineKeyboardBuilder()
    for item in items:
        mark = "✅" if item.selected else "▫️"
        builder.button(text=f"{mark} {short(item.text)}", callback_data=f"elim:t:{item.id}")
    builder.adjust(1)
    selected = sum(i.selected for i in items)
    builder.row(InlineKeyboardButton(text=f"Подтвердить ({selected}/3)", callback_data="elim:ok"))
    builder.row(InlineKeyboardButton(text="← Дописать в список", callback_data="elim:back"))
    return builder.as_markup()


def tactic_next(position: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Дальше →", callback_data=f"tac:next:{position}")]]
    )


def tactic_keep() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оставить как есть", callback_data="tac:keep")]]
    )


# ---------- Чек-ин ----------
#   ci:m:<tactic_id>:<week ordinal>:<1|0> | ci:done:<week ordinal> | ci:edit:<week ordinal>


def checkin(tactics, week_ordinal: int, webapp_url: str = "") -> InlineKeyboardMarkup:  # noqa: ANN001
    builder = InlineKeyboardBuilder()
    if webapp_url:  # основной путь — приложение: там галочки за неделю уже подставлены
        builder.row(InlineKeyboardButton(text="🌿 Отметить в приложении", web_app=WebAppInfo(url=webapp_url)))
    for n, tactic in enumerate(tactics, 1):
        builder.button(text=f"{n} ✅", callback_data=f"ci:m:{tactic.id}:{week_ordinal}:1")
        builder.button(text=f"{n} ❌", callback_data=f"ci:m:{tactic.id}:{week_ordinal}:0")
    builder.adjust(*([1] if webapp_url else []), 4)
    builder.row(InlineKeyboardButton(text="Посчитать →", callback_data=f"ci:done:{week_ordinal}"))
    return builder.as_markup()


def checkin_edit(week_ordinal: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить отметки", callback_data=f"ci:edit:{week_ordinal}")]]
    )


# ---------- Итоги цикла ----------
#   cycle:new | cycle:pause


def cycle_finish() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать новый цикл 🌱", callback_data="cycle:new")],
            [InlineKeyboardButton(text="Взять паузу", callback_data="cycle:pause")],
        ]
    )
