"""Mini App: тактики с расписанием по неделям, план, подтверждение → готова и в команде."""
from datetime import date

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


async def test_tactics_crud_and_plan(api, sessionmaker):  # noqa: F811
    pids = await to_tactics(api)
    h = auth()
    plan = (await api.get("/api/plan", headers=h)).json()
    assert plan["step"] == "tactics" and [len(p["tactics"]) for p in plan["priorities"]] == [0, 0, 0]
    assert plan["max_per_priority"] == 8 and plan["weeks_total"] == 12
    assert date.fromisoformat(plan["cycle_start"]).weekday() == 0  # старт — понедельник

    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "3 тренировки по 30 минут"})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "Забег 5 км", "weeks": [4, 8, 12]})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[1], "text": "Сдать пробный тест", "weeks": [6]})).json()
    plan = (await api.post("/api/tactics", headers=h, json={"priority_id": pids[2], "text": "Больше заниматься проектом"})).json()
    assert not plan["priorities"][2]["tactics"][0]["measurable"]  # регулярная без числа — подсказка
    t0, t1 = plan["priorities"][0]["tactics"]
    assert t0["label"] == "каждую неделю" and t0["weeks"] is None and t0["measurable"]
    assert t1["label"] == "недели 4, 8, 12"
    assert plan["priorities"][1]["tactics"][0]["label"] == "неделя 6"
    assert plan["priorities"][1]["tactics"][0]["measurable"]  # разовая тактика — и так «да / нет»
    assert plan["load"][0] == 2 and plan["load"][3] == 3 and plan["load"][5] == 3 and plan["load"][11] == 3

    # правка и удаление
    plan = (await api.put(f"/api/tactics/{t1['id']}", headers=h, json={"text": "Забег 10 км", "weeks": list(range(1, 13))})).json()
    assert plan["priorities"][0]["tactics"][1] | {} == {**plan["priorities"][0]["tactics"][1], "label": "каждую неделю", "text": "Забег 10 км"}
    plan = (await api.delete(f"/api/tactics/{t1['id']}", headers=h)).json()
    assert len(plan["priorities"][0]["tactics"]) == 1


@pytest.mark.parametrize(
    "body, detail",
    [
        ({"text": "  "}, "empty"),
        ({"text": "x" * 201}, "too_long"),
        ({"text": "ok", "weeks": []}, "bad_weeks"),
        ({"text": "ok", "weeks": [13]}, "bad_weeks"),
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
        await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": f"тактика {i}"})
    r = await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "девятая"})
    assert r.json()["detail"] == "limit"
    # чужой приоритет и чужая тактика
    assert (await api.post("/api/tactics", headers=h, json={"priority_id": other[0], "text": "x"})).status_code == 404
    theirs = (await api.post("/api/tactics", headers=auth(user_id=7), json={"priority_id": other[0], "text": "их"})).json()
    tid = theirs["priorities"][0]["tactics"][0]["id"]
    assert (await api.put(f"/api/tactics/{tid}", headers=h, json={"text": "моё"})).status_code == 404
    assert (await api.delete(f"/api/tactics/{tid}", headers=h)).status_code == 404


async def test_confirm_plan_makes_ready_and_assigns_team(api, sessionmaker):  # noqa: F811
    pids = await to_tactics(api)
    h = auth()
    await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "3 тренировки"})
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

    # после подтверждения план только для чтения (правки в середине цикла — отдельный шаг)
    assert (await api.post("/api/tactics", headers=h, json={"priority_id": pids[0], "text": "ещё"})).status_code == 409
    assert (await api.post("/api/plan/confirm", headers=h)).status_code == 409
    assert (await api.get("/api/plan", headers=h)).json()["team"] == "Команда №1"
