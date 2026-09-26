"""Mini App: Explore → Eliminate → Essential intent, стыковка с чатом на шаге тактик."""
from bot.models import OnboardingStep
from bot.services import onboarding as svc
from bot.services import teams, wheel
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

CORE = {s.key: 5 for s in wheel.CORE_SPHERES}
WHY = "Потому что это делает мою жизнь лучше и спокойнее"


async def to_explore(api):  # noqa: F811
    await api.put("/api/wheel", headers=auth(), json={"scores": CORE | {"rest": 2}})


async def step(sessionmaker, tg_id=42):
    async with sessionmaker() as session:
        return (await teams.get_user_by_telegram_id(session, tg_id)).onboarding_step


async def test_full_path_to_tactics(api, sessionmaker):  # noqa: F811
    await to_explore(api)
    data = (await api.get("/api/explore", headers=auth())).json()
    assert data["items"] == [] and data["min"] == 3 and data["pick"] == 3
    assert data["lows"][0] == "🌴 Отдых"  # подсказка из колеса

    # несколько строк за раз, маркеры списка убираются, дубли не добавляются
    data = (await api.post("/api/explore", headers=auth(), json={"text": "- Спорт\n- Английский"})).json()
    assert [i["text"] for i in data["items"]] == ["Спорт", "Английский"] and data["added"] == 2
    data = (await api.post("/api/explore", headers=auth(), json={"text": "спорт"})).json()
    assert data["added"] == 0

    # меньше 3 — дальше нельзя
    assert (await api.post("/api/explore/done", headers=auth())).json()["detail"] == "not_enough"
    for text in ("Книга", "Переезд", "Сон"):
        data = (await api.post("/api/explore", headers=auth(), json={"text": text})).json()
    ids = {i["text"]: i["id"] for i in data["items"]}
    data = (await api.delete(f"/api/explore/{ids['Сон']}", headers=auth())).json()
    assert len(data["items"]) == 4

    assert (await api.post("/api/explore/done", headers=auth())).status_code == 200
    assert await step(sessionmaker) == OnboardingStep.ELIMINATE
    # на шаге выбора список ещё можно дописать
    assert (await api.post("/api/explore", headers=auth(), json={"text": "Сон"})).status_code == 200

    # ровно 3
    for bad in ([ids["Спорт"]], [ids["Спорт"], ids["Книга"], ids["Переезд"], ids["Английский"]], [ids["Спорт"], ids["Книга"], 999]):
        r = await api.put("/api/eliminate", headers=auth(), json={"selected": bad})
        assert r.status_code == 422, bad
    r = await api.put("/api/eliminate", headers=auth(), json={"selected": [ids["Спорт"], ids["Книга"], ids["Переезд"]]})
    body = r.json()
    assert [p["title"] for p in body["priorities"]] == ["Спорт", "Книга", "Переезд"]
    assert set(body["not_now"]) == {"Английский", "Сон"}
    assert await step(sessionmaker) == OnboardingStep.INTENT

    # до сохранения «зачем» выбор можно пересмотреть
    r = await api.put("/api/eliminate", headers=auth(), json={"selected": [ids["Спорт"], ids["Книга"], ids["Английский"]]})
    pids = [p["id"] for p in r.json()["priorities"]]
    assert [p["title"] for p in r.json()["priorities"]] == ["Спорт", "Английский", "Книга"]  # порядок списка

    # «зачем» обязателен для каждого и не короче 15 символов
    r = await api.put("/api/intent", headers=auth(), json={"intents": {str(pids[0]): WHY, str(pids[1]): WHY}})
    assert r.status_code == 422
    r = await api.put("/api/intent", headers=auth(), json={"intents": {str(p): ("надо" if i == 2 else WHY) for i, p in enumerate(pids)}})
    assert r.status_code == 422
    assert api.tg.sent(42) == []

    r = await api.put("/api/intent", headers=auth(), json={"intents": {str(p): WHY for p in pids}})
    assert r.status_code == 200 and all(p["intent"] == WHY for p in r.json()["priorities"])
    assert await step(sessionmaker) == OnboardingStep.TACTICS
    # эстафета в чат: бот сразу спрашивает первую тактику
    chat = "\n".join(api.tg.sent(42))
    assert "Тактики" in chat and "Приоритет 1 из 3" in chat and "Спорт" in chat and WHY in chat

    # поправить «зачем» можно, повторного сообщения в чат нет; Explore и выбор уже закрыты
    r = await api.put("/api/intent", headers=auth(), json={"intents": {str(p): WHY + "!" for p in pids}})
    assert r.status_code == 200 and len(api.tg.sent(42)) == 2
    assert (await api.post("/api/explore", headers=auth(), json={"text": "Ещё"})).status_code == 409
    assert (await api.put("/api/eliminate", headers=auth(), json={"selected": pids})).status_code == 409

    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 42)
        assert user.onboarding_position == 1
        assert [p.intent.text for p in await svc.get_priorities(session, user)] == [WHY + "!"] * 3


async def test_steps_are_guarded(api):  # noqa: F811
    await api.get("/api/me", headers=auth())  # шаг — колесо
    assert (await api.post("/api/explore", headers=auth(), json={"text": "x"})).status_code == 409
    assert (await api.post("/api/explore/done", headers=auth())).status_code == 409
    assert (await api.put("/api/intent", headers=auth(), json={"intents": {}})).status_code == 409


async def test_explore_empty_and_limit(api, sessionmaker):  # noqa: F811
    await to_explore(api)
    assert (await api.post("/api/explore", headers=auth(), json={"text": "  \n - "})).json()["detail"] == "empty"
    lines = "\n".join(f"пункт {i}" for i in range(svc.MAX_EXPLORE_ITEMS))
    await api.post("/api/explore", headers=auth(), json={"text": lines})
    assert (await api.post("/api/explore", headers=auth(), json={"text": "ещё"})).json()["detail"] == "limit"


async def test_cannot_delete_foreign_item(api, sessionmaker):  # noqa: F811
    await to_explore(api)
    await api.put("/api/wheel", headers=auth(user_id=7), json={"scores": CORE})
    other = (await api.post("/api/explore", headers=auth(user_id=7), json={"text": "чужое"})).json()["items"][0]["id"]
    await api.delete(f"/api/explore/{other}", headers=auth())
    assert len((await api.get("/api/explore", headers=auth(user_id=7))).json()["items"]) == 1
