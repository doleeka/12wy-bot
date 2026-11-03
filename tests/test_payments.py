"""Оплата через бот (вариант «перевод на Kaspi + админ подтверждает»): выключена по умолчанию; включённая
касается только новых; без оплаты — только инструкция и приём чека; доступ открывает кнопка админа."""
from datetime import datetime
from itertools import count

import httpx
import pytest
from aiogram.methods import CopyMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User as TgUser

from bot.config import Settings
from bot.main import build_dispatcher
from bot.models import Payment, PaymentStatus
from bot.services import teams
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.webapp_helpers import TOKEN, auth
from webapp.app import create_app

_ids = count(70_000)
KASPI = "Стоимость — 15 000 ₸ за 12 недель.\nKaspi: +7 700 000 00 00 (Динара Н.)"
NEW = 501


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token=TOKEN, database_path=None, admin_ids=[ADMIN_ID], webapp_url="https://app.test")
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def receipt(tg, user_id, document=False):
    dp, bot, _ = tg
    extra = ({"document": Document(file_id="pdf1", file_unique_id="u1", file_name="kaspi.pdf")} if document
             else {"photo": [PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
                             PhotoSize(file_id="big", file_unique_id="b", width=900, height=900)]})
    await dp.feed_update(bot, Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Новенькая"), **extra)))


async def press(tg, data, user_id=ADMIN_ID):
    dp, bot, _ = tg
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"), text="чек")
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=TgUser(id=user_id, is_bot=False, first_name="A"), chat_instance="x",
        message=msg, data=data)))


def buttons(tg):
    copy = tg[2].of_type(CopyMessage)[-1]
    return copy, [b.callback_data for row in copy.reply_markup.inline_keyboard for b in row]


async def existing(sessionmaker, tg_id):
    """Участница, которая была в боте до включения оплаты (в базе ей это проставляет миграция 0012)."""
    await make_user(sessionmaker, tg_id)
    async with sessionmaker() as session:
        (await teams.get_user_by_telegram_id(session, tg_id)).paid_at = datetime(2026, 10, 1)
        await session.commit()


async def turn_on(tg):
    await send(tg, "/payment text " + KASPI, ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/payment on", ADMIN_ID, "private", ADMIN_ID)


async def app_status(sessionmaker, user_id):
    app = create_app(sessionmaker, Settings(bot_token=TOKEN, database_path=None, admin_ids=[ADMIN_ID]), None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/me", headers=auth(user_id=user_id))
        return r.status_code, r.json().get("detail")


async def test_off_by_default_nothing_changes(sessionmaker, tg):
    await send(tg, "/start", NEW, "private", NEW)
    assert "Привет" in tg[2].sent(NEW)[0] and "оплат" not in tg[2].sent(NEW)[0]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, NEW)).paid_at is not None  # доступ останется и после включения
    await send(tg, "/payment", ADMIN_ID, "private", ADMIN_ID)
    assert "выключена" in tg[2].sent(ADMIN_ID)[-1] and "ещё не задано" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/payment on", ADMIN_ID, "private", ADMIN_ID)  # без цены и реквизитов не включаем
    assert "Сначала задай цену" in tg[2].sent(ADMIN_ID)[-1]


async def test_new_person_pays_and_admin_lets_her_in(sessionmaker, tg):
    await existing(sessionmaker, 7)  # уже в боте до включения оплаты
    await turn_on(tg)
    assert "15 000 ₸" in tg[2].sent(ADMIN_ID)[-2] and "включена" in tg[2].sent(ADMIN_ID)[-1]

    # старенькие — как прежде
    await send(tg, "/plan", 7, "private", 7)
    assert "оплат" not in tg[2].sent(7)[-1]

    # новенькая видит только инструкцию — на любые команды
    await send(tg, "/start", NEW, "private", NEW)
    assert "после оплаты" in tg[2].sent(NEW)[-1] and "Kaspi: +7 700" in tg[2].sent(NEW)[-1]
    await send(tg, "/checkin", NEW, "private", NEW)
    assert "после оплаты" in tg[2].sent(NEW)[-1]
    await press(tg, "wheel:begin", user_id=NEW)
    assert await app_status(sessionmaker, NEW) == (403, "unpaid")

    # прислала чек — он у админа с кнопками, ей — «чек у организатора»
    await receipt(tg, NEW)
    copy, btns = buttons(tg)
    assert copy.chat_id == ADMIN_ID and "Новенькая" in copy.caption and "id <code>501</code>" in copy.caption
    assert [b.split(":")[:3] for b in btns] == [["pay", "ok", "team"], ["pay", "ok", "solo"], ["pay", "no", "x"]]
    assert "Чек у организатора" in tg[2].sent(NEW)[-1]
    await send(tg, "/start", NEW, "private", NEW)
    assert "жду подтверждения" in tg[2].sent(NEW)[-1]

    # «✅ Без тройки» — доступ открыт, приветствие без обещания команды
    await press(tg, btns[1])
    sent = tg[2].sent(NEW)
    assert "Оплата подтверждена" in sent[-2] and "самостоятельно" in sent[-1] and "маленькая команда" not in sent[-1]
    assert "без тройки" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, NEW)
        assert u.paid_at is not None and u.no_team
        p = await session.get(Payment, 1)
        assert p.status == PaymentStatus.APPROVED and p.tariff == "solo" and p.decided_by == ADMIN_ID
    assert (await app_status(sessionmaker, NEW))[0] == 200
    await send(tg, "/plan", NEW, "private", NEW)
    assert "после оплаты" not in tg[2].sent(NEW)[-1]

    # повторное нажатие (второй админ / двойной клик) ничего не меняет
    await press(tg, btns[0])
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, NEW)).no_team


async def test_rejected_receipt_can_be_sent_again(sessionmaker, tg):
    await turn_on(tg)
    await receipt(tg, NEW, document=True)
    _, btns = buttons(tg)
    await press(tg, btns[2])
    assert "не нашла эту оплату" in tg[2].sent(NEW)[-1]
    assert (await app_status(sessionmaker, NEW))[0] == 403
    await send(tg, "/start", NEW, "private", NEW)
    assert "после оплаты" in tg[2].sent(NEW)[-1]  # снова инструкция, а не «жду подтверждения»
    await receipt(tg, NEW)
    _, btns = buttons(tg)
    await press(tg, btns[0])  # «✅ С тройкой»
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, NEW)
        assert u.paid_at is not None and not u.no_team
    assert "маленькая команда" in tg[2].sent(NEW)[-1]


async def test_solo_link_before_payment_and_manual_paid(sessionmaker, tg):
    await turn_on(tg)
    await send(tg, "/start solo", NEW, "private", NEW)
    assert "после оплаты" in tg[2].sent(NEW)[-1]
    assert any("по ссылке «без тройки»" in t for t in tg[2].sent(ADMIN_ID))
    await receipt(tg, NEW)
    copy, _ = buttons(tg)
    assert "по ссылке «без тройки»" in copy.caption

    # оплатила наличными — /paid без кнопки; её чек тоже закрыт
    await send(tg, "/paid 501", ADMIN_ID, "private", ADMIN_ID)
    assert "доступ открыт" in tg[2].sent(ADMIN_ID)[-1] and "Оплата подтверждена" in tg[2].sent(NEW)[-2]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, NEW)
        assert u.paid_at is not None and u.no_team
        assert (await session.get(Payment, 1)).status == PaymentStatus.APPROVED
    await send(tg, "/paid 501", ADMIN_ID, "private", ADMIN_ID)
    assert "уже открыт" in tg[2].sent(ADMIN_ID)[-1]

    await send(tg, "/payment off", ADMIN_ID, "private", ADMIN_ID)
    await send(tg, "/start", 502, "private", 502)
    assert "после оплаты" not in tg[2].sent(502)[0]


async def test_admin_only_and_admins_never_pay(sessionmaker, tg):
    await turn_on(tg)
    await send(tg, "/payment off", NEW, "private", NEW)
    assert "после оплаты" in tg[2].sent(NEW)[-1]  # новенькая без оплаты — даже не «только для админа»
    await existing(sessionmaker, 7)
    await send(tg, "/payment off", 7, "private", 7)
    assert "только для админа" in tg[2].sent(7)[-1]
    await send(tg, "/start", ADMIN_ID, "private", ADMIN_ID)
    assert "после оплаты" not in tg[2].sent(ADMIN_ID)[-1]
    await press(tg, "pay:ok:team:1", user_id=7)  # кнопки решения — только у админа
    async with sessionmaker() as session:
        assert await session.get(Payment, 1) is None
