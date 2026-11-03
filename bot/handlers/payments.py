"""Оплата через бот: пока доступ не оплачен, бот показывает только инструкцию и принимает чек; админ подтверждает
кнопкой — с тройкой или без. Стоит первым из личных роутеров: неоплаченные дальше не проходят."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings
from bot.filters import IsAdmin
from bot.models import Payment, PaymentStatus, User
from bot.notify import display_name, safe_send
from bot.services import members, payments, teams
from bot.services.users import get_or_create_user

router = Router(name="payments")
router.message.filter(F.chat.type == "private")


class NeedsPayment(Filter):
    async def __call__(self, event: TelegramObject, session: AsyncSession, settings: Settings | None = None) -> bool:
        tg_user = event.from_user
        if tg_user is None:
            return False
        user = await teams.get_user_by_telegram_id(session, tg_user.id)
        admin_ids = settings.admin_ids if settings else []
        if user is None:  # ещё ни разу не писала боту — станет «новенькой» прямо сейчас
            return tg_user.id not in admin_ids and await payments.is_required(session)
        return await payments.needs_payment(session, user, admin_ids)


async def welcome(bot: Bot, user: User, settings: Settings | None) -> None:
    """После оплаты — то же приветствие, что и по /start (без тройки — без обещания команды)."""
    text = texts.WELCOME.format(name=escape(user.first_name or ""))
    if user.no_team:
        text = text.replace(texts.WELCOME_TEAM_LINE, texts.WELCOME_SOLO_LINE)
    await safe_send(bot, user.telegram_id, text, reply_markup=keyboards.wheel_begin(settings.webapp_url if settings else ""))


def _decision_buttons(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ С тройкой", callback_data=f"pay:ok:team:{payment_id}"),
         InlineKeyboardButton(text="✅ Без тройки", callback_data=f"pay:ok:solo:{payment_id}")],
        [InlineKeyboardButton(text="❌ Оплаты нет", callback_data=f"pay:no:x:{payment_id}")],
    ])


# ---------- участница без оплаты ----------

@router.message(NeedsPayment(), F.photo | F.document)
async def on_receipt(message: Message, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    file_id, kind = (message.photo[-1].file_id, "photo") if message.photo else (message.document.file_id, "document")
    payment = await payments.add_receipt(session, user, file_id, kind)
    await session.commit()
    caption = texts.PAY_ADMIN_RECEIPT.format(name=display_name(user), tg_id=user.telegram_id,
                                             solo=texts.PAY_ADMIN_SOLO_HINT if user.no_team else "")
    for admin_id in settings.admin_ids if settings else []:
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id, caption=caption,
                                   reply_markup=_decision_buttons(payment.id))
        except Exception:  # noqa: BLE001 — один недоступный админ не должен терять чек для остальных
            await safe_send(bot, admin_id, caption, reply_markup=_decision_buttons(payment.id))
    await message.answer(texts.PAY_RECEIPT_RECEIVED)


@router.message(NeedsPayment())
async def on_unpaid(message: Message, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    from bot.handlers.start import SOLO_START, _join_solo_by_link  # noqa: PLC0415 — без циклического импорта

    user = await get_or_create_user(session, message.from_user)
    if (message.text or "").split() == ["/start", SOLO_START]:  # ссылка «без тройки» работает и до оплаты
        await _join_solo_by_link(bot, session, user, settings)
    if any(u.id == user.id for _, u in await payments.pending(session)):
        await message.answer(texts.PAY_WAITING)
        return
    await message.answer(texts.PAY_REQUIRED.format(
        name=escape(message.from_user.first_name or ""), instructions=await payments.instructions(session) or ""))


@router.callback_query(NeedsPayment())
async def on_unpaid_callback(callback: CallbackQuery) -> None:
    await callback.answer(texts.PAY_CALLBACK_LOCKED, show_alert=True)


# ---------- админ ----------

@router.callback_query(F.data.startswith("pay:"), IsAdmin())
async def on_decision(callback: CallbackQuery, session: AsyncSession, bot: Bot, settings: Settings | None = None) -> None:
    _, action, tariff, payment_id = (callback.data.split(":") + ["", "", ""])[:4]
    payment = await session.get(Payment, int(payment_id)) if payment_id.isdigit() else None
    if payment is None or payment.status != PaymentStatus.PENDING:
        await callback.answer(texts.PAY_ADMIN_DECIDED, show_alert=True)
        return
    user = await session.get(User, payment.user_id)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if action == "ok" and tariff in (payments.TEAM, payments.SOLO):
        for p, u in await payments.pending(session):  # другие её чеки тоже закрываем — доступ уже открыт
            if u.id == user.id:
                payments.approve(p, user, tariff, callback.from_user.id)
        await session.commit()
        await safe_send(bot, user.telegram_id, texts.PAY_APPROVED)
        await welcome(bot, user, settings)
        reply = texts.PAY_ADMIN_APPROVED.format(name=display_name(user), tariff=texts.PAY_TARIFF[tariff])
    else:
        payments.reject(payment, callback.from_user.id)
        await session.commit()
        await safe_send(bot, user.telegram_id, texts.PAY_REJECTED)
        reply = texts.PAY_ADMIN_REJECTED.format(name=display_name(user))
    await safe_send(bot, callback.from_user.id, reply)


@router.message(Command("payment"), IsAdmin())
async def cmd_payment(message: Message, command: CommandObject, session: AsyncSession, settings: Settings | None = None) -> None:
    parts = (command.args or "").strip().split(maxsplit=1)  # текст — всё после «text», вместе с переносами строк
    head, rest = (parts[0].lower() if parts else ""), (parts[1] if len(parts) > 1 else "")
    if head == "text" and rest.strip():
        await payments.set_setting(session, payments.TEXT, rest.strip())
        await message.answer(texts.PAY_TEXT_SAVED.format(preview=texts.PAY_REQUIRED.format(
            name="Айгерим", instructions=rest.strip())))
        return
    if head == "on":
        if not await payments.instructions(session):
            await message.answer(texts.PAY_NEED_TEXT_FIRST)
            return
        await payments.set_setting(session, payments.REQUIRED, "1")
        await message.answer(texts.PAY_ON)
        return
    if head == "off":
        await payments.set_setting(session, payments.REQUIRED, "0")
        await message.answer(texts.PAY_OFF)
        return
    waiting = await payments.pending(session)
    await message.answer(texts.PAY_STATUS.format(
        state=texts.PAY_STATE_ON if await payments.is_required(session) else texts.PAY_STATE_OFF,
        text=await payments.instructions(session) or texts.PAY_NO_TEXT,
        pending=str(len(waiting)) + "".join(texts.PAY_PENDING_ROW.format(name=display_name(u), tg_id=u.telegram_id)
                                            for _, u in waiting),
    ))


@router.message(Command("paid"), IsAdmin())
async def cmd_paid(message: Message, command: CommandObject, session: AsyncSession, bot: Bot,
                   settings: Settings | None = None) -> None:
    q = (command.args or "").strip()
    found = await members.find_users(session, q) if q else []
    if len(found) != 1:
        rows = "\n".join(f"• {display_name(u)} · id <code>{u.telegram_id}</code>" for u in found)
        await message.answer(texts.SOLO_MANY.format(q=escape(q), rows=rows, cmd="paid") if len(found) > 1
                             else texts.REMOVE_NOT_FOUND.format(q=escape(q)) if q else texts.PAID_USAGE)
        return
    user = found[0]
    if user.paid_at is not None:
        await message.answer(texts.PAID_ALREADY.format(who=display_name(user)))
        return
    payments.mark_paid(user)
    for payment, u in await payments.pending(session):  # её чеки, если были, больше не ждут решения
        if u.id == user.id:
            payments.approve(payment, user, payments.SOLO if user.no_team else payments.TEAM, message.from_user.id)
    await session.commit()
    await safe_send(bot, user.telegram_id, texts.PAY_APPROVED)
    await welcome(bot, user, settings)
    await message.answer(texts.PAID_DONE.format(who=display_name(user)))


@router.message(Command("payment", "paid"))
async def payments_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
