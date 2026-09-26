"""Mini App: тактики с расписанием по неделям, план, подтверждение → готова и в команде."""
from datetime import date, timedelta

import pytest

from bot.models import OnboardingStep
from bot.services import onboarding as svc
from bot.services import tactics, teams, wheel
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

CORE = {s.key: 5 for s in wheel.CORE_SPHERES}
WHY = "Потому что это делает мою жизнь лучше и спокойнее"


async def to_tactics(api, user_id=42):  # noqa: F811
    h = auth(user_id=user_id)
    await api.put("/api/wheel", headers=h, json={"scores": CORE})
    data = (await api.post("/api/explore", headers=h, json={"text": "Спорт\nАнглийский\nПроект"})).json()
    await api.post("/api/explore/done", headers=h)
    r = await api.put("/api/eliminate", headers=h, json={"selected": [i["id"] for i in data["items"]]})
    pids = [p["id"] for p in r.json()["priorities"]]
    await api.put("/api/intent", headers=h, json={"intents": {str(p): WHY for p in pids}})
    return pids


def test_weeks_helpers():
    assert tactics.normalize_weeks(None) is None
    assert tactics.normalize_weeks(range(1, 13)) is None  # все 12 = каждую неделю
    assert tactics.normalize_weeks([12, 2, 6, 6]) == [2, 6, 12]
    for bad in ([], [0], [13], [1, 14]):
        with pytest.raises(ValueError):
            tactics.normalize_weeks(bad)
    assert tactics.weeks_label(None) == "каждую неделю"
    assert tactics.weeks_label([5]) == "неделя 5"
    assert tactics.weeks_label([1, 2, 3, 4, 8, 11, 12]) == "недели 1–4, 8, 11–12"
    assert tactics.normalize_days([4, 0, 2, 2]) == [0, 2, 4] and tactics.normalize_days(None) is None
    for bad in ([], [-1], [7]):
        with pytest.raises(ValueError):
            tactics.normalize_days(bad)
    assert tactics.weeks_label(None, [0, 2, 4]) == "каждую неделю · пн, ср, пт"
    assert tactics.weeks_label(None, list(range(7))) == "каждый день"
    assert tactics.weeks_label([4], [0]) == "неделя 4"  # у недельных — без дней


async def test_tactics_crud_and_plan(api, sessionmaker):  # noqa: F811
    pids = await to_tactics(api)
    h = auth()
    plan = (await api.get("/api/plan", headers=h)).json()
    assert plan["step"] == "tactics" and [len(p["tactics"]) for p in plan["priorities"]] == [0, 0, 0]
    assert plan["max_per_priority"] == 8 and plan["weeks_total"] == 12
    assert date.fromisoformat(plan["cycle_start"]).weekday() == 0  # старт — понедельник

    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "3 тренировки по 30 минут", "days": [4, 0, 2, 2]})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "Забег 5 км", "weeks": [4, 8, 12]})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[1], "text": "Сдать пробный тест", "weeks": [6]})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[2], "text": "Больше заниматься проектом", "days": [5]})).json()
    assert not plan["priorities"][2]["tactics"][0]["measurable"]  # регулярная без числа — подсказка
    t0, t1 = plan["priorities"][0]["tactics"]
    assert t0["label"] == "каждую неделю · пн, ср, пт" and t0["weeks"] is None and t0["days"] == [0, 2, 4]
    assert t0["measurable"]
    assert t1["days"] is None  # у контрольных точек дней недели нет
    assert t1["label"] == "недели 4, 8, 12"
    assert plan["priorities"][1]["tactics"][0]["label"] == "неделя 6"
    assert plan["priorities"][1]["tactics"][0]["measurable"]  # разовая тактика — и так «да / нет»
    assert plan["load"][0] == 2 and plan["load"][3] == 3 and plan["load"][5] == 3 and plan["load"][11] == 3

    # правка и удаление
    plan = (await api.put(f"/api/tactics/{t1['id']}", headers=h, json={"text": "Забег 10 км", "weeks": list(range(1, 13)), "days": [6]})).json()
    assert plan["priorities"][0]["tactics"][1] | {} == {**plan["priorities"][0]["tactics"][1], "label": "каждую неделю · вс", "text": "Забег 10 км"}
    plan = (await api.delete(f"/api/tactics/{t1['id']}", headers=h)).json()
    assert len(plan["priorities"][0]["tactics"]) == 1


@pytest.mark.parametrize(
    "body, detail",
    [
        ({"text": "  "}, "empty"),
        ({"text": "x" * 201}, "too_long"),
        ({"text": "ok", "weeks": []}, "bad_weeks"),
        ({"text": "ok", "weeks": [13]}, "bad_weeks"),
        ({"text": "ok"}, "pick_days"),  # еженедельная — нужен день недели
        ({"text": "ok", "days": []}, "bad_days"),
        ({"text": "ok", "days": [7]}, "bad_days"),
    ],
)
async def test_tactic_validation(api, body, detail):  # noqa: F811
    pids = await to_tactics(api)
    r = await api.post("/api/tactics", headers=auth(), json={"priority_id": pids[0], **body})
    assert r.status_code == 422 and r.json()["detail"] == detail


async def test_tactic_limit_and_ownership(api):  # noqa: F811
    pids = await to_tactics(api)
    other = await to_tactics(api, user_id=7)
    h = auth()
    for i in range(8):
        await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": f"тактика {i}", "days": [0]})
    r = await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "девятая", "days": [0]})
    assert r.json()["detail"] == "limit"
    # чужой приоритет и чужая тактика
    assert (await api.post("/api/tactics", headers=h, json={"priority_id": other[0], "text": "x", "days": [0]})).status_code == 404
    theirs = (await api.post("/api/tactics", headers=auth(user_id=7), json={"priority_id": other[0], "text": "их", "days": [0]})).json()
    tid = theirs["priorities"][0]["tactics"][0]["id"]
    assert (await api.put(f"/api/tactics/{tid}", headers=h, json={"text": "моё", "days": [0]})).status_code == 404
    assert (await api.delete(f"/api/tactics/{tid}", headers=h)).status_code == 404


async def test_confirm_plan_makes_ready_and_assigns_team(api, sessionmaker):  # noqa: F811
    pids = await to_tactics(api)
    h = auth()
    await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "3 тренировки", "days": [0, 2, 4]})
    assert (await api.post("/api/plan/confirm", headers=h)).json()["detail"] == "empty_priority"
    for pid in pids[1:]:
        await api.post("/api/tactics", headers=h, json={"priority_id": pid, "text": "2 часа в неделю", "weeks": [1, 2, 3]})
    plan = (await api.post("/api/plan/confirm", headers=h)).json()
    assert plan["step"] == "done" and plan["team"] == "Команда №1"
    assert "Команда №1" in "\n".join(api.tg.sent(42))  # сообщение о команде — в чат

    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 42)
        assert user.is_ready and user.onboarding_step == OnboardingStep.DONE
        assert user.cycle_start == svc.cycle_start_for(date.today())

    # до старта цикла план можно менять; повторно подтверждать не нужно
    assert (await api.post("/api/plan/confirm", headers=h)).status_code == 409
    plan = (await api.get("/api/plan", headers=h)).json()
    assert plan["team"] == "Команда №1" and plan["editable"]
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "ещё", "days": [0]})).json()
    assert len(plan["priorities"][0]["tactics"]) == 2

    # после старта — только чтение
    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 42)
        user.cycle_start = date.today() - timedelta(days=date.today().weekday())
        await session.commit()
    assert (await api.get("/api/plan", headers=h)).json()["editable"] is False
    r = await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "ещё", "days": [0]})
    assert r.status_code == 409 and r.json()["detail"] == "plan_locked"
    assert (await api.put(f"/api/priorities/{pids[0]}", headers=h, json={"title": "X", "intent": WHY})).status_code == 409
    assert (await api.post("/api/plan/reselect", headers=h)).status_code == 409


async def confirmed(api, sessionmaker):  # noqa: F811
    pids = await to_tactics(api)
    h = auth()
    for pid in pids:
        await api.post("/api/tactics", headers=h, json={"priority_id": pid, "text": "2 часа", "days": [0]})
    await api.post("/api/plan/confirm", headers=h)
    return pids


async def test_edit_goal_before_start(api, sessionmaker):  # noqa: F811
    pids = await confirmed(api, sessionmaker)
    h = auth()
    plan = (await api.put(f"/api/priorities/{pids[1]}", headers=h, json={"title": "Разговорный английский", "intent": WHY + "!"})).json()
    p = plan["priorities"][1]
    assert p["title"] == "Разговорный английский" and p["intent"] == WHY + "!" and len(p["tactics"]) == 1
    bad = await api.put(f"/api/priorities/{pids[1]}", headers=h, json={"title": "  ", "intent": WHY})
    assert bad.json()["detail"] == "bad_title"
    bad = await api.put(f"/api/priorities/{pids[1]}", headers=h, json={"title": "ok", "intent": "надо"})
    assert bad.json()["detail"] == "intent_required"
    # последнюю тактику цели удалить нельзя
    tid = p["tactics"][0]["id"]
    assert (await api.delete(f"/api/tactics/{tid}", headers=h)).json()["detail"] == "last_tactic"


async def test_reselect_priorities_before_start(api, sessionmaker):  # noqa: F811
    await confirmed(api, sessionmaker)
    h = auth()
    data = (await api.post("/api/plan/reselect", headers=h)).json()
    ids = [i["id"] for i in data["items"]]
    await api.post("/api/explore", headers=h, json={"text": "Новая цель"})  # список можно дописать
    data = (await api.get("/api/explore", headers=h)).json()
    new_ids = [i["id"] for i in data["items"] if i["text"] in ("Спорт", "Английский", "Новая цель")]
    r = await api.put("/api/eliminate", headers=h, json={"selected": new_ids})
    pids = [p["id"] for p in r.json()["priorities"]]
    assert [p["title"] for p in r.json()["priorities"]] == ["Спорт", "Английский", "Новая цель"]
    await api.put("/api/intent", headers=h, json={"intents": {str(p): WHY for p in pids}})
    plan = (await api.get("/api/plan", headers=h)).json()
    assert plan["step"] == "tactics" and all(not p["tactics"] for p in plan["priorities"])  # тактики — заново
    for pid in pids:
        await api.post("/api/tactics", headers=h, json={"priority_id": pid, "text": "1 раз", "days": [2]})
    plan = (await api.post("/api/plan/confirm", headers=h)).json()
    assert plan["step"] == "done" and plan["team"] == "Команда №1"  # команда та же
    assert "остаётся прежней" in api.tg.sent(42)[-1]
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 42)).cycle_start == svc.cycle_start_for(date.today())
