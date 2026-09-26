"""Шаги 2–5 онбординга: Explore → Eliminate → Essential intent → Тактики.

Прогресс хранится в базе (users.onboarding_step и связанные таблицы), поэтому
/start после перезапуска бота продолжает с того же места. В FSM лежит только
черновик «неизмеримой» тактики, пока участница решает, оставить ли её.
"""
from __future__ import annotations

from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.models import OnboardingStep, Priority, User
from bot.services import cycle
from bot.services import onboarding as svc
from bot.handlers.teams import join_team
from bot.services.tactics import weeks_label
from bot.services.users import get_or_create_user

router = Router(name="onboarding")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")

EXPLORE_DISPLAY_LEN = 100


# ---------- рендер ----------

def explore_list_text(items) -> str:  # noqa: ANN001
    if not items:
        return texts.EXPLORE_EMPTY
    lines = "\n".join(f"{n}. {escape(keyboards.short(i.text, EXPLORE_DISPLAY_LEN))}" for n, i in enumerate(items, 1))
    footer = (
        texts.EXPLORE_FOOTER_READY
        if len(items) >= svc.MIN_EXPLORE_ITEMS
        else texts.EXPLORE_FOOTER_MORE.format(min=svc.MIN_EXPLORE_ITEMS)
    )
    return texts.EXPLORE_LIST.format(count=len(items), items=lines, footer=footer)


def eliminate_text(items) -> str:  # noqa: ANN001
    return texts.ELIMINATE_INTRO.format(selected=sum(i.selected for i in items))


def intent_question(priority: Priority) -> str:
    return texts.INTENT_QUESTION.format(n=priority.position, title=escape(priority.title))


def tactic_question(priority: Priority) -> str:
    intent = priority.intent.text if priority.intent else ""
    return texts.TACTIC_QUESTION.format(
        n=priority.position, title=escape(priority.title), intent=escape(keyboards.short(intent, 200))
    )


def plan_text(priorities: list[Priority]) -> str:
    blocks = []
    for p in priorities:
        tactics = "\n".join(
            texts.PLAN_TACTIC.format(text=escape(t.text))
            + ("" if t.weeks is None and not t.days else f" <i>({weeks_label(t.weeks, t.days)})</i>")
            for t in p.tactics
            if t.is_active
        )
        blocks.append(
            texts.PLAN_PRIORITY.format(
                n=p.position,
                title=escape(p.title),
                intent=escape(p.intent.text) if p.intent else "—",
                tactics=tactics,
            ).rstrip()
        )
    return "\n\n".join(blocks)


# ---------- «где я сейчас» ----------

async def send_step_prompt(message: Message, session: AsyncSession, user: User, today: date | None = None) -> None:
    """Отправляет подсказку текущего шага онбординга (после колеса баланса)."""
    step = user.onboarding_step
    if step == OnboardingStep.EXPLORE:
        items = await svc.get_explore_items(session, user)
        await message.answer(texts.EXPLORE_INTRO)
        previous = await cycle.previous_priorities(session, user)
        if previous:
            await message.answer(
                texts.EXPLORE_PREVIOUS.format(items="\n".join(f"• {escape(p.title)}" for p in previous))
            )
        if items:
            await message.answer(explore_list_text(items), reply_markup=keyboards.explore_list(True))
    elif step == OnboardingStep.ELIMINATE:
        items = await svc.get_explore_items(session, user)
        await message.answer(eliminate_text(items), reply_markup=keyboards.eliminate(items))
    elif step == OnboardingStep.INTENT:
        priority = svc.next_priority_without_intent(await svc.get_priorities(session, user))
        if priority is not None:
            await message.answer(intent_question(priority))
    elif step == OnboardingStep.TACTICS:
        priority = await svc.current_tactic_priority(session, user)
        if priority is not None:
            await message.answer(texts.TACTICS_INTRO)
            markup = keyboards.tactic_next(priority.position) if priority.tactics else None
            await message.answer(tactic_question(priority), reply_markup=markup)
    elif step == OnboardingStep.DONE:
        await message.answer(plan_text(await svc.get_priorities(session, user)) or texts.PLAN_EMPTY)
    elif step == OnboardingStep.FINISHED:
        from bot.handlers.cycle import send_summary  # локальный импорт: избегаем цикла импортов

        await send_summary(message, session, user, today or date.today())


# ---------- текстовые ответы ----------

@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot, settings: Settings | None = None
) -> None:
    user = await get_or_create_user(session, message.from_user)
    text = message.text.strip()
    step = user.onboarding_step

    if step == OnboardingStep.EXPLORE:
        await _on_explore_text(message, session, user, text)
    elif step == OnboardingStep.INTENT:
        await _on_intent_text(message, session, user, text)
    elif step == OnboardingStep.TACTICS:
        await _on_tactic_text(message, session, user, text, state, bot, settings)
    elif step == OnboardingStep.WHEEL:
        from bot.handlers.wheel import wheel_question  # локальный импорт: wheel импортирует этот модуль
        from bot.services import wheel

        question, markup = wheel_question(await wheel.get_scores(session, user))
        await message.answer(question, reply_markup=markup)
    else:
        # ELIMINATE — выбор кнопками; DONE — план; FINISHED — итоги цикла
        await send_step_prompt(message, session, user, local_today(settings))


async def _on_explore_text(message: Message, session: AsyncSession, user: User, text: str) -> None:
    parsed = svc.parse_explore_text(text)
    before = await svc.get_explore_items(session, user)
    if len(before) >= svc.MAX_EXPLORE_ITEMS:
        await message.answer(texts.EXPLORE_LIMIT.format(max=svc.MAX_EXPLORE_ITEMS))
    else:
        added = await svc.add_explore_items(session, user, parsed)
        if parsed and added == 0:
            await message.answer(texts.EXPLORE_DUPLICATE)
    items = await svc.get_explore_items(session, user)
    await message.answer(explore_list_text(items), reply_markup=keyboards.explore_list(bool(items)))


async def _on_intent_text(message: Message, session: AsyncSession, user: User, text: str) -> None:
    if len(text) < svc.MIN_INTENT_LEN:
        await message.answer(texts.INTENT_TOO_SHORT)
        return
    await svc.save_intent(session, user, text)
    priorities = await svc.get_priorities(session, user)
    nxt = svc.next_priority_without_intent(priorities)
    if nxt is not None:
        await message.answer(intent_question(nxt))
        return
    first = await svc.current_tactic_priority(session, user)
    await message.answer(texts.TACTICS_INTRO)
    await message.answer(tactic_question(first))


async def _on_tactic_text(
    message: Message,
    session: AsyncSession,
    user: User,
    text: str,
    state: FSMContext,
    bot: Bot,
    settings: Settings | None,
) -> None:
    priority = await svc.current_tactic_priority(session, user)
    if priority is None:
        return
    if not svc.is_measurable(text):
        await state.update_data(pending_tactic=text, pending_position=priority.position)
        await message.answer(
            texts.TACTIC_NOT_MEASURABLE.format(text=escape(keyboards.short(text, 200))),
            reply_markup=keyboards.tactic_keep(),
        )
        return
    await state.update_data(pending_tactic=None, pending_position=None)
    await _save_tactic(message, session, user, priority, text, bot, settings)


async def _save_tactic(
    message: Message,
    session: AsyncSession,
    user: User,
    priority: Priority,
    text: str,
    bot: Bot,
    settings: Settings | None,
) -> None:
    await svc.add_tactic(session, user, priority, text)
    if len(priority.tactics) >= svc.MAX_TACTICS_PER_PRIORITY:
        await _advance(message, session, user, bot, settings)
    else:
        await message.answer(texts.TACTIC_ADDED_ONE, reply_markup=keyboards.tactic_next(priority.position))


async def _advance(
    message: Message, session: AsyncSession, user: User, bot: Bot, settings: Settings | None
) -> None:
    finished = svc.advance_tactics(user, local_today(settings), settings.cycle_start if settings else None)
    if not finished:
        priority = await svc.current_tactic_priority(session, user)
        await message.answer(tactic_question(priority))
        return
    priorities = await svc.get_priorities(session, user)
    await message.answer(
        texts.ONBOARDING_DONE.format(plan=plan_text(priorities), start=user.cycle_start.strftime("%d.%m"))
    )
    await join_team(message, bot, session, user, settings)


# ---------- кнопки ----------

@router.callback_query(F.data.startswith("explore:"))
async def on_explore_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(session, callback.from_user)
    if user.onboarding_step != OnboardingStep.EXPLORE:
        await callback.answer("Этот шаг уже пройден 🙂")
        return
    parts = callback.data.split(":")
    action = parts[1]
    items = await svc.get_explore_items(session, user)

    if action == "done":
        if len(items) < svc.MIN_EXPLORE_ITEMS:
            await callback.answer(texts.EXPLORE_NOT_ENOUGH.format(min=svc.MIN_EXPLORE_ITEMS), show_alert=True)
            return
        user.onboarding_step = OnboardingStep.ELIMINATE
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(eliminate_text(items), reply_markup=keyboards.eliminate(items))
    elif action == "delmenu" and items:
        await callback.message.edit_text(texts.EXPLORE_DELETE_PROMPT, reply_markup=keyboards.explore_delete(items))
    elif action == "del" and len(parts) == 3 and parts[2].isdigit():
        await svc.delete_explore_item(session, user, int(parts[2]))
        items = await svc.get_explore_items(session, user)
        await callback.message.edit_text(explore_list_text(items), reply_markup=keyboards.explore_list(bool(items)))
    elif action == "back":
        await callback.message.edit_text(explore_list_text(items), reply_markup=keyboards.explore_list(bool(items)))
    await callback.answer()


@router.callback_query(F.data.startswith("elim:"))
async def on_eliminate_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await get_or_create_user(session, callback.from_user)
    if user.onboarding_step != OnboardingStep.ELIMINATE:
        await callback.answer("Этот шаг уже пройден 🙂")
        return
    parts = callback.data.split(":")
    action = parts[1]

    if action == "t" and len(parts) == 3 and parts[2].isdigit():
        try:
            await svc.toggle_selection(session, user, int(parts[2]))
        except svc.TooManySelected:
            await callback.answer(texts.ELIMINATE_TOO_MANY, show_alert=True)
            return
        items = await svc.get_explore_items(session, user)
        await callback.message.edit_text(eliminate_text(items), reply_markup=keyboards.eliminate(items))
    elif action == "ok":
        items = await svc.get_explore_items(session, user)
        selected = sum(i.selected for i in items)
        if selected != svc.PRIORITIES_COUNT:
            await callback.answer(texts.ELIMINATE_NEED_EXACTLY.format(selected=selected), show_alert=True)
            return
        priorities = await svc.confirm_priorities(session, user)
        not_now = [i for i in items if not i.selected]
        await callback.message.edit_text(
            texts.ELIMINATE_DONE.format(
                priorities="\n".join(f"{p.position}. <b>{escape(p.title)}</b>" for p in priorities),
                not_now=texts.ELIMINATE_NOT_NOW.format(
                    items="\n".join(f"• {escape(keyboards.short(i.text, EXPLORE_DISPLAY_LEN))}" for i in not_now)
                )
                if not_now
                else "",
            ).rstrip()
        )
        priorities = await svc.get_priorities(session, user)
        await callback.message.answer(intent_question(priorities[0]))
    elif action == "back":
        user.onboarding_step = OnboardingStep.EXPLORE
        items = await svc.get_explore_items(session, user)
        await callback.message.edit_text(explore_list_text(items), reply_markup=keyboards.explore_list(bool(items)))
    await callback.answer()


@router.callback_query(F.data.startswith("tac:"))
async def on_tactic_callback(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot, settings: Settings | None = None
) -> None:
    user = await get_or_create_user(session, callback.from_user)
    priority = await svc.current_tactic_priority(session, user)
    if priority is None:
        await callback.answer("Этот шаг уже пройден 🙂")
        return
    parts = callback.data.split(":")
    action = parts[1]

    if action == "next" and len(parts) == 3:
        if parts[2] != str(priority.position) or not priority.tactics:
            await callback.answer()
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await _advance(callback.message, session, user, bot, settings)
    elif action == "keep":
        data = await state.get_data()
        text = data.get("pending_tactic")
        if not text or data.get("pending_position") != priority.position:
            await callback.answer(texts.TACTIC_PENDING_LOST, show_alert=True)
            return
        await state.update_data(pending_tactic=None, pending_position=None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await _save_tactic(callback.message, session, user, priority, text, bot, settings)
    await callback.answer()


@router.message(Command("plan"))
async def cmd_plan(message: Message, session: AsyncSession) -> None:
    user = await get_or_create_user(session, message.from_user)
    if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
        await message.answer(texts.PLAN_EMPTY)
        return
    await message.answer(plan_text(await svc.get_priorities(session, user)))
