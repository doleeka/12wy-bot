"""Сводка сообщества для админа: только количество, без имён; по понедельникам и по /stats."""
from datetime import date

from bot.config import Settings
from bot.digest import digest_text, send_admin_digest, stats_message
from bot.models import OnboardingStep, Priority, User, WeeklyTactic
from bot.services import checkins, teams
from bot.services.stats import week_stats
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user

OCT5, OCT12 = date(2026, 10, 5), date(2026, 10, 12)
SETTINGS = Settings(bot_token="x", database_path=None, admin_ids=[100], cycle_start=OCT5)
NAMES = ["Айгерим", "Бота", "Сауле", "Дана", "Ерке", "Фариза", "Гуля"]


async def mark(sessionmaker, tg_id, week, done_flags):
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, tg_id)
        for t, done in zip(await checkins.tactics_for_week(session, u, week), done_flags):
            await checkins.set_mark(session, u, t.id, week, done)
        await session.commit()


async def community(sessionmaker):
    """A, B, C — команда 1; D, E, F — команда 2; G ещё в онбординге."""
    for tg_id, name in zip(range(1, 8), NAMES):
        await make_user(sessionmaker, tg_id, cycle_start=OCT12 if tg_id == 6 else OCT5)  # F — поздняя, со 2-й недели
        async with sessionmaker() as session:
            u = await teams.get_user_by_telegram_id(session, tg_id)
            u.first_name = name
            if tg_id == 7:
                u.onboarding_step = OnboardingStep.TACTICS
            await session.commit()
    async with sessionmaker() as session:  # у E в неделю 1 действий нет — буфер
        e = await teams.get_user_by_telegram_id(session, 5)
        for t in await checkins.active_tactics(session, e):
            t.weeks = [2]
        await session.commit()
    await mark(sessionmaker, 1, OCT5, [True] * 4)                # 100%
    await mark(sessionmaker, 2, OCT5, [True, True, True, False])  # 75%
    await mark(sessionmaker, 3, OCT5, [True, True])               # отмечено не всё → не сделала
    await mark(sessionmaker, 4, OCT5, [True, True, False, False])  # 50%


async def test_week_counts_without_names(sessionmaker):
    await community(sessionmaker)
    async with sessionmaker() as session:
        s = await week_stats(session, OCT5)
    assert (s.participants, s.checked, s.missing, s.buffer) == (4, 3, 1, 1)
    assert s.average == 75 and (s.bucket("good"), s.bucket("warning"), s.bucket("critical")) == (1, 1, 1)
    assert s.no_gaps == 3
    assert [(t.name, t.checked, t.total) for t in s.teams] == [("Команда №1", 2, 3), ("Команда №2", 1, 1)]

    text = digest_text(s, SETTINGS)
    assert "Итоги недели 1" in text and "5–11 октября" in text
    assert "3 из 4" in text and "не отметили: 1" in text and "75%" in text and "буфер" in text
    assert "Команда №1: 2 из 3" in text and "Команда №2: 1 из 1" in text
    assert not any(name in text for name in NAMES)  # ни одного имени


async def test_no_gaps_counts_only_full_streaks(sessionmaker):
    await community(sessionmaker)
    await mark(sessionmaker, 1, OCT12, [True] * 4)
    await mark(sessionmaker, 3, OCT12, [True] * 4)   # неделю 1 пропустила
    await mark(sessionmaker, 6, OCT12, [True] * 4)   # поздняя: её неделя 1 = 12.10
    async with sessionmaker() as session:
        s = await week_stats(session, OCT12)
    assert s.checked == 3 and s.no_gaps == 2  # A и поздняя F; C — с пропуском


async def test_monday_digest_goes_only_to_admins(sessionmaker, monkeypatch):
    import bot.digest as digest
    await community(sessionmaker)
    bot, tg = make_fake_bot()
    monkeypatch.setattr(digest, "local_today", lambda _s: OCT12)  # понедельник после недели 1
    assert await send_admin_digest(bot, sessionmaker, SETTINGS) == 1
    assert "Итоги недели 1" in tg.sent(100)[-1]
    assert all(not tg.sent(i) for i in range(1, 8))  # участницам ничего не уходит
    # до старта цикла сводки нет — не шлём пустое
    bot2, tg2 = make_fake_bot()
    monkeypatch.setattr(digest, "local_today", lambda _s: OCT5)
    assert await send_admin_digest(bot2, sessionmaker, SETTINGS) == 0 and not tg2.sent(100)


async def test_stats_message_previous_and_running_week(sessionmaker):
    await community(sessionmaker)
    async with sessionmaker() as session:
        text = await stats_message(session, SETTINGS, date(2026, 10, 14))
    assert "Итоги недели 1" in text and "Неделя 2 — идёт" in text and "ещё не закончилась" in text
    assert not any(name in text for name in NAMES)


async def test_stats_command_admin_only(sessionmaker):
    from tests.test_group_chat import ADMIN_ID, send
    from bot.main import build_dispatcher
    bot, tg = make_fake_bot()
    dp = build_dispatcher(sessionmaker)
    dp["settings"] = Settings(bot_token="x", database_path=None, admin_ids=[ADMIN_ID], cycle_start=OCT5)
    try:
        await send((dp, bot, tg), "/stats", 5, "private", 5)
        assert "только для админа" in tg.sent(5)[-1]
        await send((dp, bot, tg), "/stats", ADMIN_ID, "private", ADMIN_ID)
        assert "📊" in tg.sent(ADMIN_ID)[-1]
    finally:
        for router in list(dp.sub_routers):
            router._parent_router = None
        dp.sub_routers.clear()
    assert User and Priority and WeeklyTactic  # импорт моделей для make_user
