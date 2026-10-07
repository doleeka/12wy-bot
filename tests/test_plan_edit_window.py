"""Окно правок плана: до старта и первые 3 календарных дня цикла по Asia/Almaty (старт 05.10 → до 07.10
включительно, с 08.10 закрыт). Видение — всегда. «Выбрать 3 заново» — только до старта."""
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

import bot.config as config
import webapp.app as appmod
from bot.config import Settings
from bot.models import Checkin, DailyMark, WeeklyTactic
from bot.services import teams
from tests.fake_telegram import make_fake_bot
from tests.test_webapp_tactics import WHY, to_tactics
from tests.webapp_helpers import TOKEN, auth

OCT5 = date(2026, 10, 5)  # общий старт сообщества (понедельник)


@pytest.fixture
async def api(sessionmaker, monkeypatch):
    """Клиент с общим стартом 05.10; «сегодня» задаётся через api.today = date(...)."""
    bot, tg = make_fake_bot()
    app = appmod.create_app(sessionmaker, Settings(bot_token=TOKEN, database_path=None, admin_ids=[], cycle_start=OCT5), bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.today = date(2026, 10, 1)
        monkeypatch.setattr(appmod, "local_today", lambda _s: client.today)
        yield client


async def confirmed(api, user_id=42):
    pids = await to_tactics(api, user_id=user_id)
    for pid in pids:
        await api.post("/api/tactics", headers=auth(user_id=user_id), json={"priority_id": pid, "text": "2 часа", "days": [0, 2]})
    plan = (await api.post("/api/plan/confirm", headers=auth(user_id=user_id))).json()
    assert plan["step"] == "done"
    return plan


async def edits(api, plan):
    """Все виды правок плана → их статусы."""
    h = auth()
    p0 = plan["priorities"][0]
    t0 = p0["tactics"][0]["id"]
    return {
        "priority": (await api.put(f"/api/priorities/{p0['id']}", headers=h, json={"title": "Спорт!", "intent": WHY})).status_code,
        "intent": (await api.put("/api/intent", headers=h, json={"intents": {str(p["id"]): WHY for p in plan["priorities"]}})).status_code,
        "add": (await api.post("/api/tactics", headers=h, json={"priority_id": p0["id"], "text": "Ещё 1 раз", "weeks": [2]})).status_code,
        "edit": (await api.put(f"/api/tactics/{t0}", headers=h, json={"text": "3 часа", "days": [1]})).status_code,
        "vision": (await api.put("/api/vision", headers=h, json={"work": "Своё дело"})).status_code,
    }


@pytest.mark.parametrize("today, editable", [
    (date(2026, 10, 4), True),   # до старта
    (date(2026, 10, 5), True),   # день 1
    (date(2026, 10, 7), True),   # день 3 — последний
    (date(2026, 10, 8), False),  # день 4 — закрыт
    (date(2026, 11, 20), False),
])
async def test_boundaries(api, today, editable):
    plan = await confirmed(api)
    assert plan["cycle_start"] == "2026-10-05" and plan["editable_until"] == "2026-10-07"
    api.today = today
    plan = (await api.get("/api/plan", headers=auth())).json()
    assert plan["editable"] is editable and plan["editable_until"] == "2026-10-07"
    t = (await api.get("/api/today", headers=auth())).json()
    assert t["editable"] is editable and t["editable_until"] == "2026-10-07"
    want = 200 if editable else 409
    assert await edits(api, plan) == {"priority": want, "intent": want, "add": want, "edit": want, "vision": 200}
    # «выбрать 3 заново» — только до старта (иначе стёрлись бы отметки первых дней)
    assert plan["reselect_allowed"] is (today < OCT5)


async def test_reselect_only_before_start(api):
    await confirmed(api)
    api.today = date(2026, 10, 5)
    r = await api.post("/api/plan/reselect", headers=auth())
    assert r.status_code == 409 and r.json()["detail"] == "plan_locked"
    api.today = date(2026, 10, 4)
    assert (await api.post("/api/plan/reselect", headers=auth())).status_code == 200


async def test_midnight_in_almaty_not_utc(api, monkeypatch):
    """Граница — полночь по Asia/Almaty (UTC+5), а не по серверному UTC."""
    await confirmed(api)
    monkeypatch.setattr(appmod, "local_today", config.local_today)  # настоящий расчёт даты по поясу

    def at(utc):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc.astimezone(tz) if tz else utc
        monkeypatch.setattr(config, "datetime", Frozen)

    at(datetime(2026, 10, 7, 18, 59, tzinfo=timezone.utc))  # 23:59 07.10 в Астане
    assert (await api.get("/api/plan", headers=auth())).json()["editable"] is True
    at(datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc))   # 00:00 08.10 в Астане (в UTC ещё 7-е)
    assert (await api.get("/api/plan", headers=auth())).json()["editable"] is False


@pytest.mark.parametrize("confirm_on, start, until", [
    (date(2026, 10, 5), "2026-10-05", "2026-10-07"),  # подтвердила в день старта — правки до 07.10
    (date(2026, 10, 6), "2026-10-05", "2026-10-07"),  # позже — тот же общий старт и то же окно правок
])
async def test_late_onboarding(api, confirm_on, start, until):
    api.today = confirm_on
    plan = await confirmed(api)
    assert plan["cycle_start"] == start and plan["editable_until"] == until and plan["editable"]
    api.today = date.fromisoformat(until)
    assert (await api.get("/api/plan", headers=auth())).json()["editable"] is True
    api.today = date.fromordinal(date.fromisoformat(until).toordinal() + 1)
    assert (await api.get("/api/plan", headers=auth())).json()["editable"] is False


async def test_deleting_action_with_marks_keeps_them(api, sessionmaker):
    plan = await confirmed(api)
    api.today = date(2026, 10, 5)  # понедельник, день 1
    pid = plan["priorities"][0]["id"]
    plan = (await api.post("/api/tactics", headers=auth(), json={"priority_id": pid, "text": "Бег 2 раза", "days": [0]})).json()
    ticked, spare = [t["id"] for t in plan["priorities"][0]["tactics"]]
    # «2 часа» (пн, ср) отмечено сегодня и в чек-ине недели 1
    await api.post("/api/today/daily", headers=auth(), json={"tactic_id": ticked, "done": True})
    await api.post("/api/today/week", headers=auth(), json={"tactic_id": ticked, "done": True})
    # убрали из плана — отметки сохраняются, действие скрыто
    plan = (await api.delete(f"/api/tactics/{ticked}", headers=auth())).json()
    assert [t["id"] for t in plan["priorities"][0]["tactics"]] == [spare]
    async with sessionmaker() as session:
        t = await session.get(WeeklyTactic, ticked)
        assert t is not None and t.is_active is False
        assert await session.scalar(select(func.count(Checkin.id)).where(Checkin.tactic_id == ticked)) == 1
        assert await session.scalar(select(func.count(DailyMark.id)).where(DailyMark.tactic_id == ticked)) == 1
    # скрытое действие не входит в неделю и прогресс
    today = (await api.get("/api/today", headers=auth())).json()
    assert ticked not in [i["id"] for i in today["today_items"] + today["week_items"]]
    assert today["week_progress"]["done"] == 0
    assert (await api.get("/api/scorecard", headers=auth())).json()["done_total"] == 0
    # действие без отметок удаляется как раньше
    plan = (await api.post("/api/tactics", headers=auth(), json={"priority_id": pid, "text": "Растяжка 10 минут", "days": [3]})).json()
    fresh = plan["priorities"][0]["tactics"][-1]["id"]
    await api.delete(f"/api/tactics/{fresh}", headers=auth())
    async with sessionmaker() as session:
        assert await session.get(WeeklyTactic, fresh) is None


async def test_everyone_starts_on_cohort_date(api, sessionmaker):
    api.today = date(2026, 10, 1)
    await confirmed(api, user_id=43)
    api.today = date(2026, 10, 9)
    plan = await confirmed(api)  # подтвердила уже после окна правок — старт всё равно 05.10
    assert plan["cycle_start"] == "2026-10-05" and plan["editable"] is False
    t = (await api.get("/api/today", headers=auth())).json()
    assert t["status"] == "active" and t["week_number"] == 1
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 43)).cycle_start == OCT5
        assert (await teams.get_user_by_telegram_id(session, 42)).cycle_start == OCT5
