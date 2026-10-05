from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from bot.config import Settings
from bot.models import ArchivedCheckin, ArchivedDailyMark, Checkin, DailyMark, OnboardingStep
from bot.services import checkins, scorecard, teams
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.fake_telegram import make_fake_bot
from bot.main import build_dispatcher

import pytest

THIS_WEEK = scorecard.week_start(date.today())
# Настоящий старт — всегда в будущем относительно дня прогона (раньше был зашит 05.10.2026 и тест
# сломался в сам день старта: отметки текущей недели уже не «до старта»)
OCT5 = THIS_WEEK + timedelta(days=7)


@pytest.fixture
async def tg(sessionmaker):
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID], cycle_start=OCT5)
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def user(sessionmaker, tg_id=ADMIN_ID):
    async with sessionmaker() as session:
        return await teams.get_user_by_telegram_id(session, tg_id)


async def test_testcycle_on_checkin_off(sessionmaker, tg):
    await make_user(sessionmaker, ADMIN_ID, cycle_start=OCT5)

    await send(tg, "/testcycle", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 1 из 12" in tg[2].sent(ADMIN_ID)[-1]
    assert (await user(sessionmaker)).cycle_start == THIS_WEEK

    # чек-ин недели 1 доступен как в настоящем цикле
    await send(tg, "/checkin", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 1 из 12" in tg[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        for t in await checkins.tactics_for_week(session, u, THIS_WEEK):
            await checkins.set_mark(session, u, t.id, THIS_WEEK, True)
        await session.commit()

    await send(tg, "/testcycle 3", ADMIN_ID, "private", ADMIN_ID)
    assert "неделя 3 из 12" in tg[2].sent(ADMIN_ID)[-1]
    assert (await user(sessionmaker)).cycle_start == THIS_WEEK - timedelta(days=14)

    # сначала только предпросмотр — ничего не меняется
    await send(tg, "/testcycle off", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "предпросмотр" in preview and f"{OCT5:%d.%m}" in preview and "4 отметок" in preview and "off confirm" in preview
    assert (await user(sessionmaker)).cycle_start == THIS_WEEK - timedelta(days=14)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Checkin)) == 4

    await send(tg, "/testcycle off confirm", ADMIN_ID, "private", ADMIN_ID)
    assert f"{OCT5:%d.%m}" in tg[2].sent(ADMIN_ID)[-1] and "чек-ина: 4" in tg[2].sent(ADMIN_ID)[-1]
    u = await user(sessionmaker)
    assert u.cycle_start == OCT5 and u.onboarding_step == OnboardingStep.DONE and u.test_mode_since is None
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Checkin)) == 0
        assert await session.scalar(select(func.count()).select_from(ArchivedCheckin)) == 4  # ничего не потеряно
    await send(tg, "/testcycle off", ADMIN_ID, "private", ADMIN_ID)
    assert "не включён" in tg[2].sent(ADMIN_ID)[-1]


async def test_testcycle_guards(sessionmaker, tg):
    await make_user(sessionmaker, 5)
    await send(tg, "/testcycle", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]
    assert (await user(sessionmaker, 5)).cycle_start == THIS_WEEK  # не тронуто

    await send(tg, "/start", ADMIN_ID, "private", ADMIN_ID)  # админ ещё в онбординге
    await send(tg, "/testcycle", ADMIN_ID, "private", ADMIN_ID)
    assert "подтверди план" in tg[2].sent(ADMIN_ID)[-1]

    await make_user(sessionmaker, 77)
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        u.onboarding_step = OnboardingStep.DONE
        await session.commit()
    await send(tg, "/testcycle 13", ADMIN_ID, "private", ADMIN_ID)
    assert "Режим проверки" in tg[2].sent(ADMIN_ID)[-1]


# ---------- реальный случай: старт сдвинут на 28.09, недели 1 (28.09) и 2 (05.10) — тестовые ----------

REAL = date(2026, 10, 5)


@pytest.fixture
def oct(sessionmaker, monkeypatch):
    """Бот с общим стартом 05.10 и «сегодня» = 06.10 (вторник)."""
    import bot.handlers.admin as admin
    monkeypatch.setattr(admin, "local_today", lambda _s: date(2026, 10, 6))
    bot, session = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID], cycle_start=REAL)
    yield dp, bot, session
    for router in list(dp.sub_routers):
        router._parent_router = None
    dp.sub_routers.clear()


async def test_legacy_test_mode_archives_all_including_real_week(sessionmaker, oct):
    admin_u = await make_user(sessionmaker, ADMIN_ID, cycle_start=date(2026, 9, 28))  # тест включён до этой версии
    other = await make_user(sessionmaker, 55, cycle_start=REAL)                         # настоящая участница
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        o = await teams.get_user_by_telegram_id(session, 55)
        ta = await checkins.active_tactics(session, a)
        to = await checkins.active_tactics(session, o)
        for i, t in enumerate(ta):  # тестовая неделя 1 (28.09): 3 из 4 — как «история недели 1»
            await checkins.set_mark(session, a, t.id, date(2026, 9, 28), i != 0)
        await checkins.set_mark(session, a, ta[0].id, REAL, True)  # тестовая «неделя 2» = настоящая неделя 1
        session.add(DailyMark(user_id=a.id, tactic_id=ta[1].id, day=REAL))
        await checkins.set_mark(session, o, to[0].id, REAL, True)   # настоящая отметка другой участницы
        session.add(DailyMark(user_id=o.id, tactic_id=to[0].id, day=REAL))
        await session.commit()

    await send(oct, "/testcycle off", ADMIN_ID, "private", ADMIN_ID)
    preview = oct[2].sent(ADMIN_ID)[-1]
    assert "28.09" in preview and "05.10" in preview and "сделано 3" in preview and "дневных галочек: 1" in preview
    assert (await user(sessionmaker)).cycle_start == date(2026, 9, 28)  # предпросмотр ничего не меняет

    await send(oct, "/testcycle off confirm", ADMIN_ID, "private", ADMIN_ID)
    assert "05.10" in oct[2].sent(ADMIN_ID)[-1] and "чек-ина: 5" in oct[2].sent(ADMIN_ID)[-1] and "дневных: 1" in oct[2].sent(ADMIN_ID)[-1]
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert a.cycle_start == REAL  # общий старт, а не следующий понедельник (12.10)
        assert await session.scalar(select(func.count()).select_from(Checkin).where(Checkin.user_id == a.id)) == 0
        assert await session.scalar(select(func.count()).select_from(DailyMark).where(DailyMark.user_id == a.id)) == 0
        arch = list(await session.scalars(select(ArchivedCheckin).order_by(ArchivedCheckin.week_start, ArchivedCheckin.tactic_id)))
        assert len(arch) == 5 and {x.cycle_start_was for x in arch} == {date(2026, 9, 28)} and {x.reason for x in arch} == {"testcycle"}
        assert [(x.week_start, x.week_number, x.done) for x in arch][:1] == [(date(2026, 9, 28), 1, False)]
        assert await session.scalar(select(func.count()).select_from(ArchivedDailyMark)) == 1
        # другая участница не тронута
        o = await teams.get_user_by_telegram_id(session, 55)
        assert o.cycle_start == REAL
        assert await session.scalar(select(func.count()).select_from(Checkin).where(Checkin.user_id == o.id)) == 1
        assert await session.scalar(select(func.count()).select_from(DailyMark).where(DailyMark.user_id == o.id)) == 1
    # неделя 1 у админа снова чистая — можно отмечать по-настоящему
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert checkins.checkin_week_number(a, REAL) == 1
        assert await checkins.get_marks(session, a, REAL) == {}
    assert admin_u is not None


async def test_known_test_start_keeps_marks_made_before_test(sessionmaker, oct):
    await make_user(sessionmaker, ADMIN_ID, cycle_start=REAL)
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        t = (await checkins.active_tactics(session, a))[0]
        await checkins.set_mark(session, a, t.id, REAL, True)  # настоящая отметка — до теста
        await session.commit()
    async with sessionmaker() as session:  # «отметка была раньше»: сдвигаем время её создания в прошлое
        from sqlalchemy import update
        await session.execute(update(Checkin).values(created_at=datetime(2026, 10, 1, 8, 0), updated_at=datetime(2026, 10, 1, 8, 0)))
        await session.commit()
    await send(oct, "/testcycle 2", ADMIN_ID, "private", ADMIN_ID)  # режим проверки — с этого момента
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, ADMIN_ID)
        assert a.test_mode_since is not None and a.cycle_start == date(2026, 9, 28)
        t2 = (await checkins.active_tactics(session, a))[1]
        await checkins.set_mark(session, a, t2.id, date(2026, 9, 28), True)  # тестовая
        await session.commit()
    await send(oct, "/testcycle off confirm", ADMIN_ID, "private", ADMIN_ID)
    async with sessionmaker() as session:
        live = list(await session.scalars(select(Checkin)))
        assert [(c.week_start, c.done) for c in live] == [(REAL, True)]  # настоящая осталась
        assert await session.scalar(select(func.count()).select_from(ArchivedCheckin)) == 1
