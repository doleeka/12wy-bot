"""Вкладка «Сегодня» — граничные случаи: пустой день, неполные дни, неделя без действий, повторы и ошибки."""
import asyncio
from datetime import date, timedelta

from sqlalchemy import func, select

from bot.models import Checkin, DailyMark, OnboardingStep, Priority, User, WeeklyTactic
from bot.services import scorecard
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

TODAY = date.today()
THIS_WEEK = scorecard.week_start(TODAY)
WD = TODAY.weekday()
A, B = (WD + 1) % 7, (WD + 2) % 7  # два других дня недели


async def make(sessionmaker, tactics, cycle_start=THIS_WEEK):
    async with sessionmaker() as session:
        user = User(telegram_id=42, first_name="Анна", onboarding_step=OnboardingStep.DONE, is_ready=True, cycle_start=cycle_start)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add_all([WeeklyTactic(priority_id=p.id, user_id=user.id, **t) for t in tactics])
        await session.commit()
    async with sessionmaker() as session:
        return {t.text: t.id for t in await session.scalars(select(WeeklyTactic))}


async def tick_days(sessionmaker, tactic_id, days):
    async with sessionmaker() as session:
        for d in days:
            session.add(DailyMark(user_id=1, tactic_id=tactic_id, day=THIS_WEEK + timedelta(days=d)))
        await session.commit()


async def today(api):  # noqa: F811
    return (await api.get("/api/today", headers=auth())).json()


async def test_nothing_scheduled_today(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": [A, B]}, {"text": "Пост"}])
    d = await today(api)
    assert d["today_items"] == [] and [i["text"] for i in d["week_items"]] == ["Пост"]
    assert d["week_progress"] == {"done": 0, "planned": 2, "percent": 0}
    # отметить «сегодня» то, что не на сегодня, нельзя — и ничего не записывается
    r = await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})
    assert r.status_code == 422 and r.json()["detail"] == "not_today"
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(DailyMark.id))) == 0


async def test_partial_days_do_not_count_and_untick_reverts(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": sorted({WD, A, B})}])
    await tick_days(sessionmaker, ids["Бег"], [A])
    d = (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})).json()
    assert d["week_progress"]["done"] == 0  # 2 из 3 дней — ещё не выполнено
    ci = (await api.get("/api/checkin", headers=auth())).json()
    assert ci["tactics"][0]["suggested"] is False and ci["tactics"][0]["done"] is None

    await tick_days(sessionmaker, ids["Бег"], [B])
    assert (await today(api))["week_progress"] == {"done": 1, "planned": 1, "percent": 100}
    # сняла галочку сегодня — неделя снова не выполнена
    d = (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": False})).json()
    assert d["week_progress"]["done"] == 0 and d["today_items"][0]["done"] is False


async def test_checkin_mark_overrides_daily_ticks(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": [WD]}])
    d = (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})).json()
    assert d["week_progress"]["done"] == 1 and not d["checkin_done"]
    # в чек-ине честно отметила «не получилось» — чек-ин важнее галочек
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": THIS_WEEK.isoformat(), "marks": {str(ids["Бег"]): False}})
    assert r.json()["percent"] == 0
    d = await today(api)
    assert d["week_progress"]["done"] == 0 and d["checkin_done"] and d["today_items"][0]["done"] is True
    # повторный чек-ин исправляет результат
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": THIS_WEEK.isoformat(), "marks": {str(ids["Бег"]): True}})
    assert r.json()["percent"] == 100 and (await today(api))["week_progress"]["percent"] == 100


async def test_week_without_actions_is_buffer(api, sessionmaker):  # noqa: F811
    # сейчас неделя 2, действия — в неделях 3 и 5 (неделя 1 тоже пустая: в понедельник чек-ин открывается за неё)
    ids = await make(sessionmaker, [{"text": "Тест", "weeks": [3, 5]}], cycle_start=THIS_WEEK - timedelta(days=7))
    d = await today(api)
    assert d["status"] == "active" and d["week_number"] == 2
    assert d["today_items"] == [] and d["week_items"] == []
    assert d["week_progress"] == {"done": 0, "planned": 0, "percent": None} and d["checkin_done"] is False
    ci = (await api.get("/api/checkin", headers=auth())).json()
    assert ci["tactics"] == [] and ci["result"] is None
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": ci["week_start"], "marks": {}})
    assert r.status_code == 422 and r.json()["detail"] == "no_tactics"
    r = await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Тест"], "done": True})
    assert r.json()["detail"] == "not_this_week"
    sc = (await api.get("/api/scorecard", headers=auth())).json()
    assert sc["weeks"][1]["planned"] == 0 and sc["weeks"][1]["percent"] is None and sc["average"] is None


async def test_repeated_and_concurrent_marks_are_idempotent(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": [WD]}, {"text": "Пост"}])
    body = {"tactic_id": ids["Бег"], "done": True}
    # двойное нажатие: два одинаковых запроса одновременно — без ошибки и без дубля
    rs = await asyncio.gather(*[api.post("/api/today/daily", headers=auth(), json=body) for _ in range(3)])
    assert [r.status_code for r in rs] == [200, 200, 200]
    wb = {"tactic_id": ids["Пост"], "done": True}
    rs = await asyncio.gather(*[api.post("/api/today/week", headers=auth(), json=wb) for _ in range(3)])
    assert [r.status_code for r in rs] == [200, 200, 200]
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(DailyMark.id))) == 1
        assert await session.scalar(select(func.count(Checkin.id))) == 1
    # снять отметку дважды — тоже без ошибки
    for _ in range(2):
        r = await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": False})
        assert r.status_code == 200 and r.json()["today_items"][0]["done"] is False
        r = await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Пост"], "done": None})
        assert r.status_code == 200 and r.json()["week_items"][0]["done"] is None


async def test_errors_leave_state_untouched_and_retry_saves(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": [WD]}, {"text": "Пост"}])
    week = THIS_WEEK.isoformat()
    # чужое / несуществующее действие
    assert (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": 999, "done": True})).status_code == 404
    assert (await api.post("/api/today/week", headers=auth(), json={"tactic_id": 999, "done": True})).status_code == 404
    # кривое тело запроса
    assert (await api.post("/api/today/daily", headers=auth(), json={"tactic_id": "x"})).status_code == 422
    # чек-ин не полностью — отказ, ничего не сохранено; повтор с полными отметками проходит
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": week, "marks": {str(ids["Бег"]): True}})
    assert r.json()["detail"] == "mark_all"
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Checkin.id))) == 0
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": week, "marks": {str(ids["Бег"]): True, str(ids["Пост"]): False}})
    assert r.status_code == 200 and r.json()["percent"] == 50
    # то же сохранение ещё раз (повтор после сбоя сети) — тот же результат, без дублей
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": week, "marks": {str(ids["Бег"]): True, str(ids["Пост"]): False}})
    assert r.status_code == 200 and r.json()["percent"] == 50
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Checkin.id))) == 2
    # будущая неделя закрыта
    r = await api.put("/api/checkin", headers=auth(), json={"week_start": (THIS_WEEK + timedelta(days=7)).isoformat(), "marks": {}})
    assert r.json()["detail"] == "week_closed"


async def test_today_marks_before_start_and_after_end(api, sessionmaker):  # noqa: F811
    ids = await make(sessionmaker, [{"text": "Бег", "days": [WD]}], cycle_start=THIS_WEEK + timedelta(days=7))
    r = await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})
    assert r.json()["detail"] == "not_today"
    r = await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})
    assert r.json()["detail"] == "not_this_week"
    async with sessionmaker() as session:
        user = await session.get(User, 1)
        user.cycle_start = THIS_WEEK - timedelta(days=7 * 12)  # цикл закончился
        await session.commit()
    assert (await today(api))["status"] == "over"
    r = await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ids["Бег"], "done": True})
    assert r.json()["detail"] == "not_today"


async def test_after_checkin_today_is_locked_until_edited_in_checkin(api, sessionmaker):  # noqa: F811
    """Единый контракт: после сохранённого чек-ина итог недели меняется только через чек-ин."""
    ids = await make(sessionmaker, [{"text": "Бег", "days": [WD]}, {"text": "Пост"}])
    week = THIS_WEEK.isoformat()
    # до чек-ина отметка «за неделю» — ещё не сохранённый чек-ин
    d = (await api.post("/api/today/week", headers=auth(), json={"tactic_id": ids["Пост"], "done": True})).json()
    assert d["checkin_done"] is False and d["week_items"][0]["week_done"] is True
    marks = {str(ids["Бег"]): True, str(ids["Пост"]): False}
    assert (await api.put("/api/checkin", headers=auth(), json={"week_start": week, "marks": marks})).json()["percent"] == 50
    d = await today(api)
    assert d["checkin_done"] is True and d["week_progress"]["percent"] == 50
    assert d["today_items"][0]["week_done"] is True and d["week_items"][0]["week_done"] is False
    # галочки больше не переключаются молча — явный отказ, ничего не меняется
    for path, body in (("daily", {"tactic_id": ids["Бег"], "done": False}), ("week", {"tactic_id": ids["Пост"], "done": True}),
                       ("week", {"tactic_id": ids["Пост"], "done": None})):
        r = await api.post(f"/api/today/{path}", headers=auth(), json=body)
        assert r.status_code == 409 and r.json()["detail"] == "week_checked"
    assert (await today(api))["week_progress"]["percent"] == 50
    # изменить ответы — через чек-ин
    marks[str(ids["Пост"])] = True
    assert (await api.put("/api/checkin", headers=auth(), json={"week_start": week, "marks": marks})).json()["percent"] == 100
    d = await today(api)
    assert d["week_progress"]["percent"] == 100 and d["week_items"][0]["week_done"] is True
