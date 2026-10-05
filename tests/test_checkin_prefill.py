"""Чек-ин в чате подставляет «сделано» по галочкам из приложения и зовёт в приложение."""
from datetime import timedelta

from sqlalchemy import select

from bot.config import Settings
from bot.handlers.checkin import build_checkin
from bot.models import DailyMark, WeeklyTactic
from bot.scheduler import send_weekly_checkins
from bot.services import checkins, scorecard, teams
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user

from datetime import date

WEEK = scorecard.week_start(date.today())


async def setup(sessionmaker):
    await make_user(sessionmaker, 1, cycle_start=WEEK)
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        t = list(await session.scalars(select(WeeklyTactic).order_by(WeeklyTactic.id)))
        t[0].days, t[1].days, t[2].days = [0, 2], [1], [3]  # пн+ср, вт, чт
        for tid, wd in [(t[0].id, 0), (t[0].id, 2), (t[1].id, 1)]:  # t0 — все дни, t1 — все дни, t2 — нет
            session.add(DailyMark(user_id=u.id, tactic_id=tid, day=WEEK + timedelta(days=wd)))
        await session.commit()
        return [x.id for x in t]


async def test_prefill_from_app_ticks_and_app_button(sessionmaker):
    ids = await setup(sessionmaker)
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        await checkins.set_mark(session, u, ids[1], WEEK, False)  # уже ответила «❌» вручную — не трогаем
        text, markup = await build_checkin(session, u, WEEK, "https://app.example")
        await session.commit()
        marks = await checkins.get_marks(session, u, WEEK)
    assert marks == {ids[0]: True, ids[1]: False}  # t2 (не все дни) и t3 (без дней) — без подстановки
    assert "подставлено" in text and "в приложении" in text
    assert markup.inline_keyboard[0][0].web_app.url == "https://app.example"
    assert markup.inline_keyboard[-1][0].text == "Посчитать →"
    # без адреса приложения — без кнопки и подсказки
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        text, markup = await build_checkin(session, u, WEEK, "")
    assert "в приложении" not in text and markup.inline_keyboard[0][0].web_app is None


async def test_sunday_send_persists_prefill(sessionmaker):
    ids = await setup(sessionmaker)
    bot, tg = make_fake_bot()
    settings = Settings(bot_token="x", database_path=None, admin_ids=[], webapp_url="https://app.example")
    assert await send_weekly_checkins(bot, sessionmaker, settings) == 1
    assert "подставлено" in tg.sent(1)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, 1)
        assert await checkins.get_marks(session, u, WEEK) == {ids[0]: True, ids[1]: True}
