"""Оплата по месяцам: тариф кнопкой → сумма и реквизиты → чек в бот → админ подтверждает.

Стоит первым из личных роутеров: у кого доступ закрыт до оплаты, дальше не проходят — видят только выбор тарифа.
Админ: /payment (реквизиты, вкл/выкл), /price (цены потоков и личные), /cohort (потоки), /paid (без чека)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings, local_today
from bot.filters import IsAdmin
from bot.models import OnboardingStep, Payment, PaymentStatus, User
from bot.notify import display_name, safe_send
from bot.services import members, payments, teams
from bot.services.payments import TARIFF_NAMES, money, months_word
from bot.services.users import get_or_create_user
from bot.team_notify import common_chat_id, in_common_chat, remove_from_chat, restore_chats, send_common_link

router = Router(name="payments")
router.message.filter(F.chat.type == "private")


def _admins(settings: Settings | None) -> list[int]:
    return settings.admin_ids if settings else []


def weeks(month: int) -> str:
    return f"{4 * month - 3}–{4 * month}"


async def month_to_pay(session: AsyncSession, user: User, settings: Settings | None) -> int | None:
    """За какой месяц она платит сейчас: закрытый доступ, иначе открытое окно оплаты."""
    today = local_today(settings)
    due = await payments.due_month(session, user, today, _admins(settings))
    if due is not None:
        return due
    win = await payments.window(session, user, today)
    return win[0] if win else None


async def offer(session: AsyncSession, user: User, month: int, header: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст с выбором тарифа и кнопки с ценами её потока (и её личными ценами)."""
    opts = await payments.options(session, user, month)
    if not opts:
        return texts.PAY_NO_OPTIONS, None
    rows = [[InlineKeyboardButton(text=o.label, callback_data=f"pay:pick:{o.code}")] for o in opts]
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


async def offer_due(session: AsyncSession, user: User, month: int, name: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    newcomer = user.onboarding_step == OnboardingStep.WHEEL and user.paid_through == 0 and user.cycle == 1
    header = texts.PAY_NEW.format(name=escape(name)) if newcomer else texts.PAY_DUE.format(month=month, weeks=weeks(month))
    return await offer(session, user, month, header)


class NeedsPayment(Filter):
    """Доступ закрыт до оплаты (оплата включена, нужный месяц не оплачен). Админов не касается."""

    async def __call__(self, event: TelegramObject, session: AsyncSession, settings: Settings | None = None) -> bool:
        tg_user = event.from_user
        if tg_user is None or tg_user.id in _admins(settings):
            return False
        user = await teams.get_user_by_telegram_id(session, tg_user.id)
        if user is None:  # ещё ни разу не писала боту — новенькая: оплата нужна, если она включена
            return await payments.is_required(session)
        return await payments.locked(session, user, local_today(settings), _admins(settings))


class HasPayChoice(Filter):
    """Выбрала тариф и сейчас есть что оплачивать — ждём чек (в том числе заранее, в окне оплаты)."""

    async def __call__(self, event: TelegramObject, session: AsyncSession, settings: Settings | None = None) -> bool:
        user = await teams.get_user_by_telegram_id(session, event.from_user.id) if event.from_user else None
        return user is not None and user.pay_choice is not None and await month_to_pay(session, user, settings) is not None


async def welcome(bot: Bot, user: User, settings: Settings | None) -> None:
    """Приветствие онбординга — как по /start (без тройки — без обещания команды)."""
    text = texts.WELCOME.format(name=escape(user.first_name or ""))
    if user.no_team:
        text = text.replace(texts.WELCOME_TEAM_LINE, texts.WELCOME_SOLO_LINE)
    await safe_send(bot, user.telegram_id, text, reply_markup=keyboards.wheel_begin(settings.webapp_url if settings else ""))


async def after_paid(bot: Bot, session: AsyncSession, user: User, settings: Settings | None, text: str | None,
                     old_tariff: str | None) -> None:
    """Ей — что доступ открыт; тариф меняет команду (приложение — без тройки, фокус — фокус-группа) и общий чат;
    была на паузе — ссылки вернуться в чаты; ещё не начинала — приветствие онбординга."""
    from bot.handlers.teams import apply_tariff_team  # noqa: PLC0415 — без циклического импорта

    await session.commit()
    if text:  # None — отметили тихо (админ отметила прошлую оплату, доступ у неё и не закрывался)
        await safe_send(bot, user.telegram_id, text)
    common_before = old_tariff in (payments.BASIC, payments.FOCUS)
    common_now = in_common_chat(user)
    await apply_tariff_team(bot, session, user, settings)
    paused = await payments.locked(session, user, local_today(settings), _admins(settings))
    if user.chats_paused and not paused:
        await restore_chats(bot, session, user)
    elif user.is_ready and common_now and not common_before and not paused:  # с «Приложения» — в общий чат
        await send_common_link(bot, session, user)
    if common_before and not common_now and (chat_id := await common_chat_id(session)):
        await remove_from_chat(bot, chat_id, user.telegram_id)  # «Приложение» — без общего чата
    await session.commit()
    if text and user.onboarding_step == OnboardingStep.WHEEL:
        await welcome(bot, user, settings)


def _approved_text(user: User) -> str:
    return texts.PAY_APPROVED.format(tariff=TARIFF_NAMES[user.tariff], month=user.paid_through,
                                     last_week=4 * user.paid_through)


def _period(payment: Payment) -> str:
    a, b = payment.month_from, payment.month_from + payment.months - 1
    return texts.PAY_MONTHS_RANGE.format(a=a) if a == b else texts.PAY_MONTHS_RANGE_MANY.format(a=a, b=b)


def decision_buttons(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Оплата пришла", callback_data=f"pay:ok:{payment_id}"),
        InlineKeyboardButton(text="❌ Оплаты нет", callback_data=f"pay:no:{payment_id}"),
    ]])


# ---------- участница ----------

@router.callback_query(F.data.startswith("pay:pick:"))
async def on_pick(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, callback.from_user)
    month = await month_to_pay(session, user, settings)
    opt = await payments.option(session, user, callback.data.removeprefix("pay:pick:"), month) if month else None
    if opt is None:
        await callback.answer(texts.PAY_ADMIN_DECIDED if month is None else texts.PAY_NO_OPTIONS, show_alert=True)
        return
    await callback.answer()
    if opt.amount == 0 and opt.tariff == payments.APP and user.tariff not in (None, payments.APP) and user.is_ready:
        # бесплатное «Приложение» уводит из тройки и общего чата — одним случайным нажатием этого не делаем
        await safe_send(bot, user.telegram_id, texts.PAY_FREE_CONFIRM.format(tariff=TARIFF_NAMES[opt.tariff]),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Да, перейти", callback_data=f"pay:free:{opt.code}")],
                            [InlineKeyboardButton(text="↩️ Выбрать другой тариф", callback_data="pay:again")]]))
        return
    if opt.amount == 0:  # бесплатный тариф — без чека
        await _grant_free(bot, session, user, opt, month, settings)
        return
    user.pay_choice = opt.code
    await session.commit()
    await safe_send(bot, user.telegram_id, texts.PAY_CHOSEN.format(
        label=escape(opt.label), instructions=await payments.instructions(session) or ""))


async def _grant_free(bot: Bot, session: AsyncSession, user: User, opt, month: int,  # noqa: ANN001
                      settings: Settings | None) -> None:
    old_tariff = user.tariff
    await payments.grant(session, user, opt, month, None)
    await after_paid(bot, session, user, settings, texts.PAY_FREE.format(tariff=TARIFF_NAMES[opt.tariff]), old_tariff)


@router.callback_query(F.data.startswith("pay:free:"))
async def on_free_confirm(callback: CallbackQuery, session: AsyncSession, bot: Bot,
                          settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, callback.from_user)
    month = await month_to_pay(session, user, settings)
    opt = await payments.option(session, user, callback.data.removeprefix("pay:free:"), month) if month else None
    if opt is None or opt.amount != 0:
        await callback.answer(texts.PAY_ADMIN_DECIDED, show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await _grant_free(bot, session, user, opt, month, settings)


@router.callback_query(F.data == "pay:again")
async def on_pick_again(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, callback.from_user)
    month = await month_to_pay(session, user, settings)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if month is None:
        await safe_send(bot, user.telegram_id, texts.PAY_NOTHING_DUE)
        return
    text, markup = await offer(session, user, month, texts.PAY_PICK_AGAIN)
    await safe_send(bot, user.telegram_id, text, reply_markup=markup)


@router.message(HasPayChoice(), F.photo | F.document)
async def on_receipt(message: Message, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    month = await month_to_pay(session, user, settings)
    opt = await payments.option(session, user, user.pay_choice, month)
    if opt is None:  # цена поменялась, пока она платила, — пусть выберет заново
        user.pay_choice = None
        text, markup = await offer_due(session, user, month)
        await message.answer(texts.PAY_CHOOSE_FIRST, reply_markup=markup)
        return
    file_id, kind = (message.photo[-1].file_id, "photo") if message.photo else (message.document.file_id, "document")
    payment = await payments.add_receipt(session, user, opt, month, file_id, kind)
    await session.commit()
    cohort = await payments.cohort_of(session, user)
    caption = texts.PAY_ADMIN_RECEIPT.format(
        name=display_name(user), tg_id=user.telegram_id, cohort=cohort.number if cohort else "—",
        tariff=TARIFF_NAMES[opt.tariff], period=months_word(opt.months), months=_period(payment), amount=money(opt.amount))
    buttons = decision_buttons(payment.id)
    for admin_id in _admins(settings):
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id, caption=caption, reply_markup=buttons)
        except Exception:  # noqa: BLE001 — один недоступный админ не должен терять чек для остальных
            await safe_send(bot, admin_id, caption, reply_markup=buttons)
    await message.answer(texts.PAY_RECEIPT_RECEIVED)


@router.message(NeedsPayment())
async def on_locked(message: Message, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    from bot.handlers.start import SOLO_START, _join_solo_by_link  # noqa: PLC0415 — без циклического импорта

    user = await get_or_create_user(session, message.from_user)
    if (message.text or "").split() == ["/start", SOLO_START]:  # ссылка «без тройки» работает и до оплаты
        await _join_solo_by_link(bot, session, user, settings)
    if await payments.pending_of(session, user):
        await message.answer(texts.PAY_WAITING)
        return
    month = await payments.due_month(session, user, local_today(settings), _admins(settings)) or 1
    text, markup = await offer_due(session, user, month, message.from_user.first_name or "")
    if message.photo or message.document:  # чек без выбранного тарифа
        text = texts.PAY_CHOOSE_FIRST
    await message.answer(text, reply_markup=markup)


@router.callback_query(NeedsPayment())
async def on_locked_callback(callback: CallbackQuery) -> None:
    await callback.answer(texts.PAY_CALLBACK_LOCKED, show_alert=True)


@router.message(F.photo | F.document)
async def on_unexpected_file(message: Message, session: AsyncSession, bot: Bot,
                             settings: Settings | None = None) -> None:
    """Файл или фото, когда оплату от неё не ждём (например, чек после смены тарифа), — не теряем: админу,
    с подсказкой, что сделать; ей — что передали."""
    if message.from_user.id in _admins(settings):
        return
    user = await get_or_create_user(session, message.from_user)
    caption = texts.PAY_ADMIN_UNEXPECTED.format(name=display_name(user), tg_id=user.telegram_id,
                                                tariff=TARIFF_NAMES.get(user.tariff or "", "—"), month=user.paid_through)
    for admin_id in _admins(settings):
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id, caption=caption)
        except Exception:  # noqa: BLE001
            await safe_send(bot, admin_id, caption)
    await message.answer(texts.PAY_UNEXPECTED_RECEIVED)


# ---------- админ: решение по чеку ----------

@router.callback_query(F.data.regexp(r"^pay:(ok|no):\d+$"), IsAdmin())
async def on_decision(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    _, action, payment_id = callback.data.split(":")
    payment = await session.get(Payment, int(payment_id))
    if payment is None or payment.status != PaymentStatus.PENDING:
        await callback.answer(texts.PAY_ADMIN_DECIDED, show_alert=True)
        return
    user = await session.get(User, payment.user_id)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if action == "ok":
        old_tariff = user.tariff
        payments.approve(payment, user, callback.from_user.id)
        for other in await payments.pending_of(session, user):  # другие её чеки за то же — тоже закрыты
            if other.id != payment.id:
                payments.approve(other, user, callback.from_user.id)
        await after_paid(bot, session, user, settings, _approved_text(user), old_tariff)
        reply = texts.PAY_ADMIN_APPROVED.format(name=display_name(user), tariff=TARIFF_NAMES[user.tariff],
                                                month=user.paid_through)
    else:
        payments.reject(payment, callback.from_user.id)
        await session.commit()
        await safe_send(bot, user.telegram_id, texts.PAY_REJECTED)
        reply = texts.PAY_ADMIN_REJECTED.format(name=display_name(user))
    await safe_send(bot, callback.from_user.id, reply)


# ---------- админ: команды ----------

@router.message(Command("payment"), IsAdmin())
async def cmd_payment(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                      settings: Settings | None = None) -> None:
    parts = (command.args or "").strip().split(maxsplit=1)  # реквизиты — всё после «text», с переносами строк
    head, rest = (parts[0].lower() if parts else ""), (parts[1].strip() if len(parts) > 1 else "")
    if head == "text" and rest:
        await payments.set_setting(session, payments.TEXT, rest)
        await message.answer(texts.PAY_TEXT_SAVED.format(preview=texts.PAY_CHOSEN.format(
            label="Базовый · 1 месяц — 2 990 ₸", instructions=rest)))
        return
    if head == "on":
        if not await payments.instructions(session):
            await message.answer(texts.PAY_NEED_TEXT_FIRST)
            return
        today = local_today(settings)
        to_lock = await payments.would_lock(session, today, _admins(settings))
        if to_lock and rest.lower() != "confirm":
            rows = "\n".join(texts.PAY_ON_ROW.format(name=display_name(u), tg_id=u.telegram_id, month=m,
                                                     tariff=TARIFF_NAMES.get(u.tariff or "", "тариф не выбран"))
                             for u, m in to_lock)
            await message.answer(texts.PAY_ON_PREVIEW.format(n=len(to_lock), rows=rows))
            return
        await payments.set_setting(session, payments.REQUIRED, "1")
        await session.commit()
        sent = 0
        for user, month in to_lock:
            if user.onboarding_step == OnboardingStep.WHEEL and month == 1:
                continue  # ещё не начинала — увидит выбор тарифа, как только напишет боту
            text, markup = await offer_due(session, user, month, user.first_name or "")
            text += texts.PAY_CHATS_WARNING  # из чатов — в ближайшую среду, если не оплатит
            sent += await safe_send(bot, user.telegram_id, text, reply_markup=markup)
        await session.commit()
        await message.answer(texts.PAY_ON.format(sent=sent))
        return
    if head == "off":
        await payments.set_setting(session, payments.REQUIRED, "0")
        await message.answer(texts.PAY_OFF)
        return
    waiting = await payments.pending(session)
    enrolling = await payments.enrolling_cohort(session)
    await message.answer(texts.PAY_STATUS.format(
        state=texts.PAY_STATE_ON if await payments.is_required(session) else texts.PAY_STATE_OFF,
        enrolling=f"{enrolling.number} (старт {enrolling.start:%d.%m.%Y})" if enrolling else "—",
        text=escape(await payments.instructions(session) or texts.PAY_NO_TEXT),
        pending=str(len(waiting)) + "".join(texts.PAY_PENDING_ROW.format(
            name=display_name(u), tg_id=u.telegram_id, tariff=TARIFF_NAMES[p.tariff], amount=money(p.amount))
            for p, u in waiting),
    ))


async def _one_user(message: Message, session: AsyncSession, q: str, cmd: str) -> User | None:
    found = await members.find_users(session, q)
    if len(found) == 1:
        return found[0]
    rows = "\n".join(f"• {display_name(u)} · id <code>{u.telegram_id}</code>" for u in found)
    await message.answer(texts.SOLO_MANY.format(q=escape(q), rows=rows, cmd=cmd) if found
                         else texts.REMOVE_NOT_FOUND.format(q=escape(q)))
    return None


async def _mark_paid(bot: Bot, session: AsyncSession, user: User, settings: Settings | None, admin_id: int,
                     tariff: str | None, to_end: bool) -> int | None:
    """Отметить оплату без чека: следующий нужный месяц (или до конца цикла). None — уже всё оплачено.
    Ей пишем, только если у неё был закрыт доступ или она ждала решения по чеку; иначе — тихо."""
    today = local_today(settings)
    month_from = await payments.due_month(session, user, today, _admins(settings), assume_on=True) or user.paid_through + 1
    if month_from > payments.CYCLE_MONTHS:
        return None
    notify = await payments.locked(session, user, today, _admins(settings)) or bool(await payments.pending_of(session, user))
    months = payments.CYCLE_MONTHS - month_from + 1 if to_end else 1
    tariff = tariff or user.tariff or payments.BASIC
    monthly = await payments.price(session, user, tariff, 1) or 0
    amount = (await payments.price(session, user, tariff, 3) if months == 3 else None) or monthly * months
    old_tariff = user.tariff
    await payments.grant(session, user, payments.Option(tariff, months, amount), month_from, admin_id)
    for other in await payments.pending_of(session, user):  # её чеки, если были, больше не ждут решения
        payments.approve(other, user, admin_id)
        user.tariff = tariff
    await after_paid(bot, session, user, settings, _approved_text(user) if notify else None, old_tariff)
    return user.paid_through


@router.message(Command("paid"), IsAdmin())
async def cmd_paid(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                   settings: Settings | None = None) -> None:
    """/paid @ник1 @ник2 id … [тариф] [3] — отметить оплату без чека одной или сразу многим."""
    tokens = (command.args or "").replace(",", " ").split()
    tariff = next((payments.TARIFF_ALIASES[t.lower()] for t in tokens if t.lower() in payments.TARIFF_ALIASES), None)
    to_end = any(t.lower() in {"3", "все", "all"} for t in tokens)
    queries = [t for t in tokens if t.lower() not in payments.TARIFF_ALIASES and t.lower() not in {"3", "все", "all"}]
    if not queries:
        await message.answer(texts.PAID_USAGE)
        return
    if len(queries) == 1:  # один человек — подробные ответы, как раньше
        user = await _one_user(message, session, queries[0], "paid")
        if user is None:
            return
        month = await _mark_paid(bot, session, user, settings, message.from_user.id, tariff, to_end)
        await message.answer(texts.PAID_NOTHING.format(who=display_name(user)) if month is None else
                             texts.PAID_DONE.format(who=display_name(user), month=month, tariff=TARIFF_NAMES[user.tariff]))
        return
    done, already, missing, many = [], [], [], []
    for q in dict.fromkeys(queries):  # без повторов, в том же порядке
        found = await members.find_users(session, q)
        if len(found) != 1:
            (many if found else missing).append(escape(q))
            continue
        month = await _mark_paid(bot, session, found[0], settings, message.from_user.id, tariff, to_end)
        (already if month is None else done).append(
            display_name(found[0]) + ("" if month is None else f" — по {month}-й месяц, {TARIFF_NAMES[found[0].tariff]}"))
    parts = [texts.PAID_BULK_DONE.format(n=len(done), rows="\n".join(f"• {r}" for r in done) or "—")]
    if already:
        parts.append(texts.PAID_BULK_ALREADY.format(rows=", ".join(already)))
    if missing:
        parts.append(texts.PAID_BULK_MISSING.format(rows=", ".join(missing)))
    if many:
        parts.append(texts.PAID_BULK_MANY.format(rows=", ".join(many)))
    await message.answer("\n\n".join(parts))


@router.message(Command("unpaid"), IsAdmin())
async def cmd_unpaid(message: Message, command: CommandObject, session: AsyncSession,
                     settings: Settings | None = None) -> None:
    """/unpaid id1 @ник2 … — отменить последнюю отметку об оплате (поставили по ошибке). Ей ничего не пишем."""
    queries = (command.args or "").replace(",", " ").split()
    if not queries:
        await message.answer(texts.UNPAID_USAGE)
        return
    today = local_today(settings)
    done, nothing, missing = [], [], []
    for q in dict.fromkeys(queries):
        found = await members.find_users(session, q)
        if len(found) != 1:
            missing.append(escape(q) + (" (несколько совпадений)" if found else ""))
            continue
        u = found[0]
        payment = await payments.undo_last(session, u, message.from_user.id)
        if payment is None:
            nothing.append(display_name(u))
            continue
        locked = await payments.locked(session, u, today, _admins(settings))
        done.append(texts.UNPAID_ROW.format(
            name=display_name(u), tariff=TARIFF_NAMES.get(payment.tariff, payment.tariff), period=_period(payment),
            amount=money(payment.amount), now=texts.UNPAID_NOW.format(month=u.paid_through) if u.paid_through
            else texts.UNPAID_NOW_NONE, locked=texts.UNPAID_LOCKED if locked else ""))
    await session.commit()
    parts = [texts.UNPAID_DONE.format(n=len(done), rows="\n".join(done) or "—")]
    if nothing:
        parts.append(texts.UNPAID_NOTHING.format(rows=", ".join(nothing)))
    if missing:
        parts.append(texts.PAID_BULK_MISSING.format(rows=", ".join(missing)))
    await message.answer("\n\n".join(parts))


async def change_tariff(bot: Bot, session: AsyncSession, user: User, tariff: str, settings: Settings | None) -> None:
    """Сменить тариф без оплаты: «Приложение» — без тройки и общего чата, «Базовый» — тройка, «Фокус-группа» —
    фокус-группа потока. Оплаченные месяцы не меняются; цена следующих — по новому тарифу."""
    old_tariff = user.tariff
    user.tariff = tariff
    user.no_team = tariff == payments.APP
    user.pay_choice = None
    await after_paid(bot, session, user, settings, None, old_tariff)  # команда и общий чат — как после оплаты
    await safe_send(bot, user.telegram_id, texts.TARIFF_CHANGED.format(tariff=TARIFF_NAMES[tariff]))


@router.message(Command("tariff"), IsAdmin())
async def cmd_tariff(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                     settings: Settings | None = None) -> None:
    """/tariff фокус id1 @ник2 … — сменить тариф одной или сразу многим, без отметки об оплате."""
    tokens = (command.args or "").replace(",", " ").split()
    tariff = payments.TARIFF_ALIASES.get(tokens[0].lower()) if tokens else None
    queries = tokens[1:]
    if tariff is None or not queries:
        await message.answer(texts.TARIFF_USAGE)
        return
    done, same, missing = [], [], []
    for q in dict.fromkeys(queries):
        found = await members.find_users(session, q)
        if len(found) != 1:
            missing.append(escape(q) + (" (несколько совпадений)" if found else ""))
            continue
        u = found[0]
        if u.telegram_id in _admins(settings):
            missing.append(escape(q) + " (админ)")
        elif u.tariff == tariff and u.no_team == (tariff == payments.APP):
            same.append(display_name(u))
        else:
            await change_tariff(bot, session, u, tariff, settings)
            done.append(display_name(u))
    parts = [texts.TARIFF_DONE.format(tariff=TARIFF_NAMES[tariff], n=len(done), rows=", ".join(done) or "—")]
    if same:
        parts.append(texts.TARIFF_SAME.format(rows=", ".join(same)))
    if missing:
        parts.append(texts.PAID_BULK_MISSING.format(rows=", ".join(missing)))
    await message.answer("\n\n".join(parts))


async def _send_long(message: Message, text: str, limit: int = 3800) -> None:
    """Telegram режет сообщения длиннее 4096 символов — делим по строкам."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk)


@router.message(Command("who"), IsAdmin())
async def cmd_who(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    """Кто оплатил, кто нет — по потокам, с id (сверить с переводами и отметить /paid сразу многих)."""
    today = local_today(settings)
    blocks = []
    for cohort in await payments.cohorts(session):
        users = list(await session.scalars(select(User).where(
            User.cohort_id == cohort.id, User.excluded_at.is_(None)).order_by(User.id)))
        users = [u for u in users if u.telegram_id not in _admins(settings)]
        if not users:
            continue
        paid, unpaid, free, onboarding = [], [], [], 0
        for u in users:
            row = texts.WHO_ROW.format(name=display_name(u), tg_id=u.telegram_id,
                                       tariff=TARIFF_NAMES.get(u.tariff or "", "—"), joined=f"{u.created_at:%d.%m}")
            if u.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED) and u.paid_through == 0:
                onboarding += 1
            elif await payments.is_free(session, u):
                free.append(row)
            elif (month := await payments.due_month(session, u, today, _admins(settings), assume_on=True)) is not None:
                unpaid.append(row + texts.WHO_DUE.format(month=month))
            else:
                paid.append(row + texts.WHO_PAID.format(month=u.paid_through))
        blocks.append(texts.WHO_COHORT.format(
            n=cohort.number, start=f"{cohort.start:%d.%m.%Y}",
            unpaid_n=len(unpaid), unpaid="\n".join(unpaid) or "—",
            paid_n=len(paid), paid="\n".join(paid) or "—",
            free_n=len(free), free="\n".join(free) or "—", onboarding=onboarding))
    await _send_long(message, "\n\n".join(blocks) + "\n\n" + texts.WHO_HINT if blocks else texts.WHO_EMPTY)


def _amount_arg(raw: str) -> int | None | str:
    """«2990» → 2990, «нет» → None (убрать), иначе 'bad'."""
    raw = raw.lower().replace(" ", "").replace("₸", "")
    if raw in {"нет", "no", "off", "-"}:
        return None
    return int(raw) if raw.isdigit() else "bad"


async def _price_table(session: AsyncSession, cohort) -> str:  # noqa: ANN001
    prices = await payments.cohort_prices(session, cohort)

    def cell(tariff: str, months: int) -> str:
        amount = prices.get((tariff, months))
        return texts.PRICE_NONE if amount is None else money(amount)

    rows = "\n".join(texts.PRICE_ROW.format(tariff=TARIFF_NAMES[t], m1=cell(t, 1), m3=cell(t, 3)) for t in payments.TARIFFS)
    return texts.PRICE_TABLE.format(n=cohort.number, start=f"{cohort.start:%d.%m.%Y}",
                                    enrolling=texts.COHORT_ENROLLING if cohort.enrolling else "", rows=rows)


@router.message(Command("price"), IsAdmin())
async def cmd_price(message: Message, command: CommandObject, session: AsyncSession) -> None:
    args = (command.args or "").split()
    if not args:
        tables = [await _price_table(session, c) for c in await payments.cohorts(session)]
        personal = await payments.personal_prices(session)
        text = "\n\n".join(tables) or "Потоков пока нет — /cohort"
        if personal:
            text += texts.PRICE_PERSONAL.format(rows="\n".join(texts.PRICE_PERSONAL_ROW.format(
                name=display_name(u), tg_id=u.telegram_id, tariff=TARIFF_NAMES.get(p.tariff, p.tariff),
                period=months_word(p.months), amount=money(p.amount)) for u, p in personal))
        await message.answer(text + "\n\n" + texts.PRICE_USAGE)
        return
    target = args[0]
    is_cohort = target.isdigit() and int(target) < 100  # номер потока; Telegram id — длинные
    if len(args) == 1 and is_cohort:
        cohort = await payments.cohort_by_number(session, int(target))
        await message.answer(await _price_table(session, cohort) if cohort else texts.COHORT_NOT_FOUND.format(n=target))
        return
    if len(args) != 4 or payments.TARIFF_ALIASES.get(args[1].lower()) is None or args[2] not in {"1", "3"} \
            or _amount_arg(args[3]) == "bad":
        await message.answer(texts.PRICE_BAD + texts.PRICE_USAGE)
        return
    tariff, months, amount = payments.TARIFF_ALIASES[args[1].lower()], int(args[2]), _amount_arg(args[3])
    if is_cohort:
        cohort = await payments.cohort_by_number(session, int(target))
        if cohort is None:
            await message.answer(texts.COHORT_NOT_FOUND.format(n=target))
            return
        await payments.set_cohort_price(session, cohort, tariff, months, amount)
        await message.answer(texts.PRICE_SAVED.format(table=await _price_table(session, cohort)))
        return
    user = await _one_user(message, session, target, "price")
    if user is None:
        return
    await payments.set_personal_price(session, user, tariff, months, amount)
    await message.answer(texts.PRICE_PERSONAL_SAVED.format(
        name=display_name(user), tariff=TARIFF_NAMES[tariff], period=months_word(months),
        amount=texts.PRICE_NONE + " (как у потока)" if amount is None else money(amount)))


async def _move_kind(session: AsyncSession, user: User, cohort, today) -> str:  # noqa: ANN001
    """'keep' — цикл ещё не начался; 'shift' — начался, но до старта потока она ничего не отмечала (пришла поздно):
    тот же план, неделя 1 — со стартом потока; 'carry' — правда проходила прежние недели: они — в историю."""
    from bot.services import cycle  # noqa: PLC0415

    if user.cycle_start is None or user.cycle_start > today:
        return "keep"
    return "carry" if await cycle.has_marks_before(session, user, cohort.start) else "shift"


async def _move(bot: Bot, session: AsyncSession, user: User, cohort, settings: Settings | None) -> None:  # noqa: ANN001
    """Перевести в другой поток, сохранив план. Тройка — из нового потока (из чата прежней тройки убираем);
    общий чат — общий для всех потоков, остаётся. Оплата нового цикла — с нуля, по ценам нового потока."""
    from bot.handlers.teams import apply_tariff_team  # noqa: PLC0415 — без циклического импорта
    from bot.services import cycle  # noqa: PLC0415

    kind = await _move_kind(session, user, cohort, local_today(settings))
    old_team = await teams.get_team_of(session, user)
    if old_team is not None:
        await teams.leave_team(session, user)
        if old_team.chat_id:
            await remove_from_chat(bot, old_team.chat_id, user.telegram_id)
    if kind == "carry":
        await cycle.carry_plan_to_new_cycle(session, user, cohort.start)
        if user.onboarding_step == OnboardingStep.FINISHED:
            user.onboarding_step = OnboardingStep.DONE
    if kind == "shift":
        await cycle.shift_cycle_start(session, user, cohort.start)
    elif user.cycle_start is not None:  # план подтверждён — неделя 1 вместе с новым потоком
        user.cycle_start = cohort.start
    user.cohort_id = cohort.id
    user.paid_through = 0
    user.pay_choice = None
    user.chats_paused = False
    await session.commit()
    await apply_tariff_team(bot, session, user, settings)  # подтверждённый план — сразу в тройку нового потока
    await safe_send(bot, user.telegram_id, texts.COHORT_MOVED.format(n=cohort.number, start=f"{cohort.start:%d.%m.%Y}")
                    + (texts.COHORT_MOVED_HISTORY if kind == "carry" else ""))


async def cmd_cohort_move(message: Message, args: list[str], session: AsyncSession, bot: Bot,
                          settings: Settings | None) -> None:
    """/cohort move 2 @ник1 id2 … [confirm] — сначала предпросмотр, с confirm — перевод."""
    confirm = args[-1:] == ["confirm"]
    args = args[:-1] if confirm else args
    if len(args) < 2 or not args[0].isdigit():
        await message.answer(texts.COHORT_MOVE_USAGE)
        return
    cohort = await payments.cohort_by_number(session, int(args[0]))
    if cohort is None:
        await message.answer(texts.COHORT_NOT_FOUND.format(n=args[0]))
        return
    today = local_today(settings)
    rows, problems, targets = [], [], []
    for q in dict.fromkeys(t for t in " ".join(args[1:]).replace(",", " ").split()):
        found = await members.find_users(session, q)
        if len(found) != 1:
            problems.append(escape(q) + (" — несколько совпадений, укажи id" if found else " — не нашла"))
            continue
        u = found[0]
        if u.cohort_id == cohort.id:
            problems.append(f"{display_name(u)} — уже в потоке {cohort.number}")
            continue
        targets.append(u)
        what = {"keep": texts.COHORT_MOVE_KEEP, "shift": texts.COHORT_MOVE_SHIFT,
                "carry": texts.COHORT_MOVE_RESTART}[await _move_kind(session, u, cohort, today)]
        rows.append(f"• {display_name(u)} · <code>{u.telegram_id}</code> — {what}")
    tail = ("\n\n⚠️ " + "\n⚠️ ".join(problems)) if problems else ""
    if not targets:
        await message.answer(texts.COHORT_MOVE_NOBODY + tail)
        return
    if not confirm:
        await message.answer(texts.COHORT_MOVE_PREVIEW.format(
            n=cohort.number, start=f"{cohort.start:%d.%m.%Y}", rows="\n".join(rows),
            cmd=f"/cohort move {cohort.number} " + " ".join(str(u.telegram_id) for u in targets) + " confirm") + tail)
        return
    for u in targets:
        await _move(bot, session, u, cohort, settings)
    await message.answer(texts.COHORT_MOVE_DONE.format(n=cohort.number, count=len(targets), rows="\n".join(rows)) + tail)


@router.message(Command("cohort"), IsAdmin())
async def cmd_cohort(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                     settings: Settings | None = None) -> None:
    args = (command.args or "").split()
    if args[:1] == ["move"]:
        await cmd_cohort_move(message, args[1:], session, bot, settings)
        return
    if args[:1] == ["new"] and len(args) == 2:
        try:
            start = datetime.strptime(args[1], "%d.%m.%Y").date()
        except ValueError:
            await message.answer(texts.COHORT_BAD_DATE)
            return
        if start.weekday() != 0:
            await message.answer(texts.COHORT_NOT_MONDAY.format(day=f"{start:%d.%m.%Y}"))
            return
        cohort = await payments.create_cohort(session, start)
        await message.answer(texts.COHORT_CREATED.format(n=cohort.number, start=f"{start:%d.%m.%Y}"))
        return
    if args[:1] == ["enroll"] and len(args) == 2 and args[1].isdigit():
        cohort = await payments.cohort_by_number(session, int(args[1]))
        if cohort is None:
            await message.answer(texts.COHORT_NOT_FOUND.format(n=args[1]))
            return
        await payments.set_enrolling(session, cohort)
        await message.answer(texts.COHORT_ENROLL_DONE.format(n=cohort.number, start=f"{cohort.start:%d.%m.%Y}"))
        return
    rows = []
    for c in await payments.cohorts(session):
        count = await session.scalar(select(func.count(User.id)).where(User.cohort_id == c.id, User.excluded_at.is_(None)))
        rows.append(texts.COHORT_ROW.format(n=c.number, start=f"{c.start:%d.%m.%Y}", count=count,
                                            enrolling=texts.COHORT_ENROLLING if c.enrolling else ""))
    await message.answer(texts.COHORT_LIST.format(rows="\n".join(rows) or "—"))


MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
               "Ноябрь", "Декабрь"]


def _month_bounds(year: int, month: int, tz: str) -> tuple[datetime, datetime]:
    """Календарный месяц по местному времени → границы в UTC (так хранится время подтверждения)."""
    zone = ZoneInfo(tz)
    start = datetime(year, month, 1, tzinfo=zone)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=zone)
    return (start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None))


@router.message(Command("revenue"), IsAdmin())
async def cmd_revenue(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    """Деньги: этот и прошлый месяц по потокам и тарифам, кто на паузе, чеки на проверке."""
    today = local_today(settings)
    tz = settings.timezone if settings else "Asia/Almaty"
    prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    parts = {}
    for key, (y, m) in (("this", (today.year, today.month)), ("prev", (prev_year, prev_month))):
        r = await payments.revenue(session, *_month_bounds(y, m, tz))
        parts[f"{key}_name"] = f"{MONTH_NAMES[m - 1]} {y}"
        parts[f"{key}_total"] = money(r.total) if r.total else "0 ₸"
        parts[f"{key}_count"] = r.count
        parts[f"{key}_rows"] = "\n".join(texts.REVENUE_ROW.format(
            cohort=n or "—", tariff=TARIFF_NAMES.get(t, t), count=c, total=money(a)) for n, t, c, a in r.rows) \
            or texts.REVENUE_NONE
    users = list(await session.scalars(select(User).where(
        User.onboarding_step.in_([OnboardingStep.DONE, OnboardingStep.FINISHED]), User.excluded_at.is_(None))))
    paused = free = 0
    for u in users:
        if u.telegram_id in _admins(settings):
            continue
        paused += await payments.locked(session, u, today, _admins(settings))
        free += await payments.is_free(session, u)
    waiting = await payments.pending(session)
    await message.answer(texts.REVENUE.format(
        **parts, paused=paused, pending=len(waiting),
        pending_total=money(sum(p.amount for p, _ in waiting)) if waiting else "0 ₸", free=free))


@router.message(Command("payment", "paid", "price", "cohort", "revenue", "who", "tariff", "unpaid"))
async def payments_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)

