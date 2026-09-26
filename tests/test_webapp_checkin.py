"""Mini App: чек-ин недели и scorecard."""
from datetime import date, timedelta

from bot.models import OnboardingStep, Priority, User, WeeklyTactic
from bot.services import scorecard, teams
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

THIS_WEEK = scorecard.week_start(date.today())


async def make_ready(sessionmaker, tg_id=42, cycle_start=THIS_WEEK, name="Анна"):
    """Готовая участница: «Бег» каждую неделю, «Забег» в неделю 1, «Тест» в неделю 2."""
    async with sessionmaker() as session:
        user = User(telegram_id=tg_id, first_name=name, onboarding_step=OnboardingStep.DONE, is_ready=True, cycle_start=cycle_start)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add_all([
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Бег"),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Забег", weeks=[1]),
            WeeklyTactic(priority_id=p.id, user_id=user.id, text="Тест", weeks=[2]),
        ])
        await session.commit()
        await teams.assign_to_team(session, user)


async def test_checkin_flow_and_report(api, sessionmaker):  # noqa: F811
    await make_ready(sessionmaker)
    await make_ready(sessionmaker, tg_id=43, name="Маша")  # сокомандница
    h = auth()
    data = (await api.get("/api/checkin", headers=h)).json()
    assert data["status"] == "active" and data["week_number"] == 1 and data["result"] is None
    assert [t["text"] for t in data["tactics"]] == ["Бег", "Забег"]  # «Тест» — во 2-ю неделю
    ids = {t["text"]: t["id"] for t in data["tactics"]}

    # всё нужно отметить
    r = await api.put("/api/checkin", headers=h, json={"week_start": data["week_start"], "marks": {str(ids["Бег"]): True}})
    assert r.json()["detail"] == "mark_all"

    marks = {str(ids["Бег"]): True, str(ids["Забег"]): False}
    r = await api.put("/api/checkin", headers=h, json={"week_start": data["week_start"], "marks": marks})
    res = r.json()
    assert res["percent"] == 50 and res["level"] == "critical" and res["done"] == 1 and res["planned"] == 2
    assert "Что помешало" in res["advice"]
    report = api.tg.sent(43)
    assert len(report) == 1 and "50%" in report[0] and "Бег" not in report[0]  # только процент

    # повторное открытие — отметки и результат на месте; исправить можно, отчёт второй раз не уходит
    data = (await api.get("/api/checkin", headers=h)).json()
    assert {t["text"]: t["done"] for t in data["tactics"]} == {"Бег": True, "Забег": False}
    assert data["result"]["percent"] == 50
    marks[str(ids["Забег"])] = True
    assert (await api.put("/api/checkin", headers=h, json={"week_start": data["week_start"], "marks": marks})).json()["percent"] == 100
    assert len(api.tg.sent(43)) == 1

    sc = (await api.get("/api/scorecard", headers=h)).json()
    assert sc["current_week"] == 1 and len(sc["weeks"]) == 12
    assert sc["weeks"][0] | {} == {**sc["weeks"][0], "percent": 100, "level": "good", "planned": 2, "future": False}
    assert sc["weeks"][1]["planned"] == 2 and sc["weeks"][1]["percent"] is None and sc["weeks"][1]["future"]
    assert sc["average"] == 100 and sc["thresholds"] == {"good": 85, "warning": 70}


async def test_checkin_rejects_foreign_and_closed_weeks(api, sessionmaker):  # noqa: F811
    await make_ready(sessionmaker)
    await make_ready(sessionmaker, tg_id=7)
    h = auth()
    data = (await api.get("/api/checkin", headers=h)).json()
    other = (await api.get("/api/checkin", headers=auth(user_id=7))).json()["tactics"][0]["id"]
    marks = {str(t["id"]): True for t in data["tactics"]}
    bad = dict(marks)
    bad.pop(next(iter(bad)))
    bad[str(other)] = True
    assert (await api.put("/api/checkin", headers=h, json={"week_start": data["week_start"], "marks": bad})).json()["detail"] == "mark_all"
    future = (THIS_WEEK + timedelta(days=7)).isoformat()
    assert (await api.put("/api/checkin", headers=h, json={"week_start": future, "marks": marks})).json()["detail"] == "week_closed"
    assert (await api.put("/api/checkin", headers=h, json={"week_start": "вчера", "marks": marks})).status_code == 422


async def test_before_cycle_start_and_before_onboarding(api, sessionmaker):  # noqa: F811
    await make_ready(sessionmaker, cycle_start=THIS_WEEK + timedelta(days=14))
    data = (await api.get("/api/checkin", headers=auth())).json()
    assert data == {"status": "not_started", "cycle_start": (THIS_WEEK + timedelta(days=14)).isoformat()}
    sc = (await api.get("/api/scorecard", headers=auth())).json()
    assert sc["current_week"] is None and all(w["future"] for w in sc["weeks"])
    await api.get("/api/me", headers=auth(user_id=5))  # ещё в онбординге
    assert (await api.get("/api/checkin", headers=auth(user_id=5))).status_code == 409
