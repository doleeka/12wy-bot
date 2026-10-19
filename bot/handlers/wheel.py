"""Шаг 1 онбординга: колесо баланса.

Состояние не хранится в FSM: какая сфера следующая, вычисляется по уже сохранённым
в базе оценкам. Поэтому перезапуск бота посреди опроса ничего не ломает.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers.onboarding import send_step_prompt
from bot.models import OnboardingStep, User
from bot.services import cycle, wheel
from bot.services.users import get_or_create_user

router = Router(name="wheel")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


def _question(sphere: wheel.Sphere) -> tuple[str, InlineKeyboardMarkup]:
    if sphere.key in {s.key for s in wheel.CORE_SPHERES}:
        progress = f"сфера {wheel.CORE_SPHERES.index(sphere) + 1} из {len(wheel.CORE_SPHERES)}"
    else:
        progress = "дополнительная сфера"
    text = texts.WHEEL_QUESTION.format(progress=progress, emoji=sphere.emoji, title=sphere.title)
    return text, keyboards.wheel_scores(sphere)


def wheel_question(scores: dict[str, int]) -> tuple[str, InlineKeyboardMarkup]:
    """Следующий вопрос: очередная основная сфера или предложение добавить дополнительные."""
    sphere = wheel.next_core_sphere(scores)
    if sphere is not None:
        return _question(sphere)
    extras = wheel.available_extras(scores)
    left = wheel.MAX_EXTRA - wheel.rated_extra_count(scores)
    text = texts.WHEEL_EXTRA_OFFER.format(left=f"1–{left}" if left > 1 else "1", spheres_word=texts.spheres_word(left))
    return text, keyboards.wheel_extras(extras)


def wheel_result_text(scores: dict[str, int]) -> str:
    lows = wheel.low_spheres(scores)
    lows_text = texts.WHEEL_LOWS.format(items=", ".join(f"{s.emoji} {s.title} ({v})" for s, v in lows))
    return texts.WHEEL_RESULT.format(chart=wheel.render_chart(scores), average=wheel.average(scores), lows=lows_text)


def comparison_text(rows: list[tuple[wheel.Sphere, int, int]]) -> str:
    lines = []
    for sphere, before, after in rows:
        diff = after - before
        delta = f"(+{diff}) 🌱" if diff > 0 else f"({diff})" if diff < 0 else "(=)"
        lines.append(
            texts.WHEEL_COMPARISON_ROW.format(emoji=sphere.emoji, title=sphere.title, before=before, after=after, delta=delta)
        )
    return texts.WHEEL_COMPARISON.format(rows="\n".join(lines))


async def _finish(callback: CallbackQuery, session: AsyncSession, user: User, scores: dict[str, int]) -> None:
    user.onboarding_step = OnboardingStep.EXPLORE
    text = wheel_result_text(scores)
    comparison = await cycle.wheel_comparison(session, user)
    if comparison:
        text += "\n\n" + comparison_text(comparison)
    await callback.message.edit_text(text)
    await send_step_prompt(callback.message, session, user)


@router.callback_query(F.data.startswith("wheel:"))
async def on_wheel_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(session, callback.from_user)
    if user.onboarding_step != OnboardingStep.WHEEL:
        await callback.answer(texts.NOT_ON_WHEEL_STEP)
        return

    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    scores = await wheel.get_scores(session, user)

    if action == "begin":
        text, markup = wheel_question(scores)
        await callback.message.answer(text, reply_markup=markup)
        await callback.message.edit_reply_markup(reply_markup=None)

    elif action == "rate" and len(parts) == 4 and parts[3].isdigit():
        sphere_key, score = parts[2], int(parts[3])
        if not wheel.can_rate(scores, sphere_key) or not wheel.MIN_SCORE <= score <= wheel.MAX_SCORE:
            await callback.answer()
            return
        await wheel.save_score(session, user, sphere_key, score)
        scores[sphere_key] = score
        if wheel.next_core_sphere(scores) is None and not wheel.available_extras(scores):
            await _finish(callback, session, user, scores)
        else:
            text, markup = wheel_question(scores)
            await callback.message.edit_text(text, reply_markup=markup)

    elif action == "extra" and len(parts) == 3:
        sphere = wheel.SPHERES.get(parts[2])
        if sphere is None or not wheel.can_rate(scores, sphere.key):
            await callback.answer()
            return
        text, markup = _question(sphere)
        await callback.message.edit_text(text, reply_markup=markup)

    elif action == "finish":
        if wheel.next_core_sphere(scores) is not None:
            await callback.answer()
            return
        await _finish(callback, session, user, scores)

    await callback.answer()
