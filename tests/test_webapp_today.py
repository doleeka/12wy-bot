"""Вкладка «Сегодня»: действия дня, дневные отметки, прогресс недели, подсказка для чек-ина."""
from datetime import date, timedelta

from sqlalchemy import select

from bot.models import OnboardingStep, Priority, User, WeeklyTactic
from bot.services import scorecard, teams
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

TODAY = date.today()
THIS_WEEK = scorecard.week_start(TODAY)
WD = TODAY.weekday()
OTHER = (WD + 1) % 7  # другой день недели


async def make(sessionmaker, cycle_start=THIS_WEEK):
    """Бег — сегодня и в другой день; Чтение — только в другой день; Тест — разовый в неделю 1; Старое — без дней."""
    async with sessionmaker() as session:
        user = User(telegram_id=42, first_name="Анна", onboarding_step=OnboardingStep.DONE, is_ready=True, cycle_start=cycle_start)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add_all([
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Бег", days=sorted({WD, OTHER})),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Чтение", days=[OTHER]),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Тест", weeks=[1]),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Старое"),
        ])
        await session.commit()
    async with sessionmaker() as session:
        return {t.text: t.id for t in await session.scalars(select(WeeklyTactic))}


async def test_today_lists(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker)
    d = (await api.get("/api/today", headers=auth())).json()
    assert d["status"] == "active" and d["week_number"] == 1 and d["tactics"] == 4
    assert [i["text"] for i in d["today_items"]] == ["Бег"]  # «Чтение» — не сегодня
    assert [i["text"] for i in d["week_items"]] == ["Тест", "Старое"]
    assert d["week_progress"] == {"done": 0, "planned": 4, "percent": 0}

    # дневная галочка — только в запланированный день
    assert (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Чтение"], "done": True})).json()["detail"] == "not_today"
    d = (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})).json()
    assert d["today_items"][0]["done"] is True
    # «Бег» запланирован на 2 дня — один отмечен, неделя ещё не выполнена
    assert d["week_progress"]["done"] == 0
    d = (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": False})).json()
    assert d["today_items"][0]["done"] is False

    # отметка за неделю для разового
    d = (await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Тест"], "done": True})).json()
    assert d["week_items"][0]["done"] is True and d["week_progress"] == {"done": 1, "planned": 4, "percent": 25}
    d = (await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Тест"], "done": None})).json()
    assert d["week_items"][0]["done"] is None and d["week_progress"]["done"] == 0


async def test_all_days_ticked_counts_for_week_and_prefills_checkin(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker)
    # отмечаем «Бег» во все его дни (в том числе «другой» — напрямую в базе, как будто вчера/завтра)
    from bot.models import DailyMark

    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 42)
        for d in (WD, OTHER):
            session.add(DailyMark(user_id=user.id, tactic_id=ids["Бег"], day=THIS_WEEK + timedelta(days=d)))
        await session.commit()
    d = (await api.get("/api/today", headers=auth())).json()
    assert d["week_progress"]["done"] == 1
    ci = (await api.get("/api/checkin", headers=auth())).json()
    sugg = {t["text"]: t["suggested"] for t in ci["tactics"]}
    assert sugg == {"Бег": True, "Чтение": False, "Тест": False, "Старое": False}


async def test_week_mark_rejects_other_weeks_and_foreign(api, sessionmaker):  # noqa: F811
    await make(sessionmaker, cycle_start=THIS_WEEK - timedelta(days=7))  # сейчас неделя 2 — «Тест» не в ней
    ids = {t["text"]: t["id"] for t in (await api.get("/api/plan", headers=auth())).json()["priorities"][0]["tactics"]}
    r = await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Тест"], "done": True})
    assert r.json()["detail"] == "not_this_week"
    assert (await api.post("/api/today/week", headers=auth(user_id=7), json={"tactic_id": ids["Старое"], "done": True})).status_code in (404, 409)


async def test_today_before_start(api, sessionmaker):  # noqa: F811
    await make(sessionmaker, cycle_start=THIS_WEEK + timedelta(days=14))
    d = (await api.get("/api/today", headers=auth())).json()
    assert d["status"] == "not_started" and d["days_until"] == (THIS_WEEK + timedelta(days=14) - TODAY).days
    assert {i["text"] for i in d["week1"]} == {"Бег", "Чтение", "Тест", "Старое"} and d["editable"]


async def test_scorecard_done_total(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker)
    await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Тест"], "done": True})
    await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Старое"], "done": False})
    assert (await api.get("/api/scorecard", headers=auth())).json()["done_total"] == 1
