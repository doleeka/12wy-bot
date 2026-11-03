"""Автоматизация: напоминание админу о непроверенных чеках, /revenue, дожим онбординга, итоги недели в общем
чате (только числа), пауза в чатах до оплаты и личные ссылки вернуться."""
from datetime import date, datetime, timedelta

from aiogram.methods import BanChatMember, CreateChatInviteLink
from sqlalchemy import update

from bot import scheduler
from bot.digest import send_common_weekly
from bot.handlers.teams import join_team
from bot.models import OnboardingStep, Payment, User
from bot.services import checkins, payments, teams
from bot.team_notify import COMMON_CHAT
from tests.test_group_chat import ADMIN_ID, send
from tests.test_payments import (MON, OCT5, SETTINGS, clock, last_buttons, member, press, receipt,  # noqa: F401
                                 tg, turn_on, user)

COMMON, TRIO_CHAT = -100900, -100500


async def link_chats(sessionmaker, tg_id=1):
    async with sessionmaker() as s:
        await payments.set_setting(s, COMMON_CHAT, str(COMMON))
        team = await teams.get_team_of(s, await teams.get_user_by_telegram_id(s, tg_id))
        team.chat_id = TRIO_CHAT
        await s.commit()


def ok_button(tg):
    return [d for _, d in last_buttons(tg, ADMIN_ID) if d.startswith("pay:ok")][0]


# ---------- 8. чаты: пауза до оплаты и возврат ----------

async def test_pause_removes_from_chats_and_payment_brings_links(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")
    await member(sessionmaker, 2, "app")  # «Приложение» бесплатно — не на паузе, из чатов не убираем
    await link_chats(sessionmaker)
    tg[2].calls.clear()
    await turn_on(tg)  # вторник 5-й недели: у «Базового» без оплаты — пауза
    assert sorted((b.chat_id, b.user_id) for b in tg[2].of_type(BanChatMember)) == [(COMMON, 1), (TRIO_CHAT, 1)]
    assert (await user(sessionmaker, 1)).chats_paused

    tg[2].calls.clear()
    await press(tg, "pay:pick:basic:1", user_id=1)
    await receipt(tg, 1)
    await press(tg, ok_button(tg))
    back = tg[2].sent(1)[-1]
    assert "С возвращением" in back and "Общий чат" in back and "Команда" in back
    invites = tg[2].of_type(CreateChatInviteLink)
    assert {i.chat_id for i in invites} == {COMMON, TRIO_CHAT} and all(i.member_limit == 1 for i in invites)
    assert not (await user(sessionmaker, 1)).chats_paused


async def test_switch_to_app_leaves_common_chat(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")
    await link_chats(sessionmaker)
    await turn_on(tg)
    tg[2].calls.clear()
    await press(tg, "pay:pick:app:1", user_id=1)  # «Приложение» в 1-м потоке бесплатно
    u = await user(sessionmaker, 1)
    assert u.tariff == "app" and u.no_team and not u.chats_paused
    async with sessionmaker() as s:
        assert await teams.get_team_of(s, u) is None
    assert not any("Общий чат" in t for t in tg[2].sent(1))  # в общий чат «Приложению» ссылки нет


async def test_new_basic_member_gets_common_chat_link(sessionmaker, tg):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async with sessionmaker() as s:
        await payments.set_setting(s, COMMON_CHAT, str(COMMON))
        for tg_id, tariff in ((5, "basic"), (6, "app")):
            s.add(User(telegram_id=tg_id, first_name=f"U{tg_id}", onboarding_step=OnboardingStep.DONE, is_ready=True,
                       cycle_start=OCT5, cohort_id=1, tariff=tariff, paid_through=3, no_team=tariff == "app"))
        await s.commit()
        for tg_id in (5, 6):
            await join_team(SimpleNamespace(answer=AsyncMock()), tg[1], s, await teams.get_user_by_telegram_id(s, tg_id),
                            SETTINGS)
    assert any("Общий чат" in t for t in tg[2].sent(5))
    assert not any("Общий чат" in t for t in tg[2].sent(6))


# ---------- 7. итоги недели в общем чате ----------

async def test_common_weekly_numbers_only(sessionmaker, tg, clock):
    for tg_id, tariff in ((1, "basic"), (2, "basic"), (3, "app")):
        await member(sessionmaker, tg_id, tariff)
    week = MON - timedelta(days=7)
    async with sessionmaker() as s:
        for tg_id, flags in ((1, [True] * 4), (2, [True, True, False, False]), (3, [True] * 4)):
            u = await teams.get_user_by_telegram_id(s, tg_id)
            for t, done in zip(await checkins.tactics_for_week(s, u, week), flags):
                await checkins.set_mark(s, u, t.id, week, done)
        await s.commit()
    clock["today"] = MON
    assert await send_common_weekly(tg[1], sessionmaker, SETTINGS) is False  # чат ещё не привязан

    await send(tg, "/linkcommon", COMMON, "supergroup", ADMIN_ID)
    assert "общий чат сообщества" in tg[2].sent(COMMON)[0]
    assert await send_common_weekly(tg[1], sessionmaker, SETTINGS) is True
    text = tg[2].sent(COMMON)[-1]
    assert "26 октября – 1 ноября" in text and "2 из 2" in text and "<b>1</b>" in text and "75%" in text
    assert "U1" not in text and "U2" not in text  # без имён; «Приложение» (U3) не в общем чате и не считается

    await send(tg, "/linkcommon", COMMON, "supergroup", 1)
    assert "только для админа" in tg[2].sent(COMMON)[-1]


# ---------- 2. чек ждёт проверки ----------

async def test_stale_receipt_reminds_admin_once_and_not_at_night(sessionmaker, tg):
    await member(sessionmaker, 1, "basic")
    await turn_on(tg)
    await press(tg, "pay:pick:basic:1", user_id=1)
    await receipt(tg, 1)
    async with sessionmaker() as s:
        await s.execute(update(Payment).values(created_at=datetime(2026, 11, 3, 5, 0)))  # 10:00 в Астане
        await s.commit()
    tg[2].calls.clear()
    assert await scheduler.remind_stale_receipts(tg[1], sessionmaker, SETTINGS, now=datetime(2026, 11, 3, 7, 0)) == 0
    assert await scheduler.remind_stale_receipts(tg[1], sessionmaker, SETTINGS, now=datetime(2026, 11, 3, 18, 0)) == 0
    assert await scheduler.remind_stale_receipts(tg[1], sessionmaker, SETTINGS, now=datetime(2026, 11, 3, 9, 0)) == 1
    assert "ждёт проверки больше 3 ч" in tg[2].sent(ADMIN_ID)[-1] and "1 990 ₸" in tg[2].sent(ADMIN_ID)[-1]
    assert any(d.startswith("pay:ok") for _, d in last_buttons(tg, ADMIN_ID))  # можно решить прямо отсюда
    assert await scheduler.remind_stale_receipts(tg[1], sessionmaker, SETTINGS, now=datetime(2026, 11, 3, 10, 0)) == 0


# ---------- 5. дожим онбординга ----------

async def test_onboarding_nudges(sessionmaker, tg, clock):
    now = datetime(2026, 11, 10, 7, 0)
    async with sessionmaker() as s:
        for tg_id, days_ago in ((10, 3), (11, 40), (12, 1)):
            s.add(User(telegram_id=tg_id, first_name=f"U{tg_id}", onboarding_step=OnboardingStep.EXPLORE,
                       cohort_id=1, created_at=now - timedelta(days=days_ago)))
        await s.commit()
    clock["today"] = date(2026, 11, 10)

    assert await scheduler.nudge_unfinished_onboarding(tg[1], sessionmaker, SETTINGS, now=now) == 1
    assert "выписать желания" in tg[2].sent(10)[-1] and not tg[2].sent(11) and not tg[2].sent(12)
    assert "Напомнила закончить онбординг: 1" in tg[2].sent(ADMIN_ID)[-1]
    assert await scheduler.nudge_unfinished_onboarding(tg[1], sessionmaker, SETTINGS, now=now + timedelta(days=1)) == 1
    assert len(tg[2].sent(10)) == 1  # 12-му: прошло 2 дня — первое; 10-му — второе только через 5 дней
    assert await scheduler.nudge_unfinished_onboarding(tg[1], sessionmaker, SETTINGS, now=now + timedelta(days=5)) == 1
    assert "напоминаю мягко" in tg[2].sent(10)[-1]
    assert await scheduler.nudge_unfinished_onboarding(tg[1], sessionmaker, SETTINGS, now=now + timedelta(days=20)) == 1
    assert len(tg[2].sent(10)) == 2  # больше двух — не надоедаем

    # оплата включена, а она не оплатила — у неё экран оплаты, онбордингом не дёргаем
    await turn_on(tg)
    async with sessionmaker() as s:
        s.add(User(telegram_id=13, first_name="U13", onboarding_step=OnboardingStep.WHEEL, cohort_id=1,
                   created_at=now - timedelta(days=3)))
        await s.commit()
    await scheduler.nudge_unfinished_onboarding(tg[1], sessionmaker, SETTINGS, now=now)
    assert not tg[2].sent(13)


# ---------- 3. /revenue ----------

async def test_revenue_report(sessionmaker, tg):
    for tg_id in (1, 2, 3):
        await member(sessionmaker, tg_id, "basic")
    await turn_on(tg)
    for tg_id, code in ((1, "basic:1"), (2, "focus:1")):
        await press(tg, f"pay:pick:{code}", user_id=tg_id)
        await receipt(tg, tg_id)
        await press(tg, ok_button(tg))
    await press(tg, "pay:pick:basic:1", user_id=3)
    await receipt(tg, 3)  # ждёт проверки
    async with sessionmaker() as s:
        await s.execute(update(Payment).where(Payment.decided_at.is_not(None)).values(decided_at=datetime(2026, 11, 3, 6)))
        await s.commit()
    await send(tg, "/revenue", ADMIN_ID, "private", ADMIN_ID)
    text = tg[2].sent(ADMIN_ID)[-1]
    assert "Ноябрь 2026</b>: 11 980 ₸ · оплат: 2" in text
    assert "Поток 1 · Базовый: 1 990 ₸ (1 опл.)" in text and "Поток 1 · Фокус-группа: 9 990 ₸ (1 опл.)" in text
    assert "Октябрь 2026</b>: 0 ₸" in text
    assert "на паузе до оплаты: <b>1</b>" in text and "ждут проверки: <b>1</b> на 1 990 ₸" in text
    await send(tg, "/revenue", 1, "private", 1)
    assert "только для админа" in tg[2].sent(1)[-1]
