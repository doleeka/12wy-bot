import time
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.config import Settings
from bot.models import OnboardingStep
from bot.services import teams, wheel
from tests.fake_telegram import make_fake_bot
from tests.webapp_helpers import TOKEN, auth, sign_init_data
from webapp.app import create_app

CORE = {s.key: 5 for s in wheel.CORE_SPHERES}


@pytest.fixture
async def api(sessionmaker):
    bot, tg = make_fake_bot()
    app = create_app(sessionmaker, Settings(bot_token=TOKEN, database_path=None, admin_ids=[7]), bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.tg = tg
        yield client


# ---------- авторизация ----------

async def test_requires_valid_init_data(api):
    assert (await api.get("/api/me")).status_code == 401
    assert (await api.get("/api/me", headers={"Authorization": "Bearer x"})).status_code == 401
    forged = {"Authorization": "tma " + sign_init_data({"id": 1, "first_name": "X"}, token="999:OTHER")}
    r = await api.get("/api/me", headers=forged)
    assert r.status_code == 401 and r.json()["detail"] == "bad_signature"
    old = auth(auth_date=int(time.time()) - 2 * 24 * 3600)
    assert (await api.get("/api/me", headers=old)).json()["detail"] == "expired"


async def test_tampered_user_rejected(api):
    headers = auth(user_id=42)
    headers["Authorization"] = headers["Authorization"].replace("42", "43")  # подменили id
    assert (await api.get("/api/me", headers=headers)).status_code == 401


async def test_me_creates_user(api, sessionmaker):
    r = await api.get("/api/me", headers=auth(user_id=7, first_name="Админ"))
    assert r.status_code == 200
    assert r.json() == {"first_name": "Админ", "step": "wheel", "cycle": 1, "is_admin": True}
    async with sessionmaker() as session:
        assert (await teams.get_user_by_telegram_id(session, 7)).first_name == "Админ"


# ---------- колесо ----------

async def test_wheel_flow(api, sessionmaker):
    r = await api.get("/api/wheel", headers=auth())
    data = r.json()
    assert len(data["spheres"]) == 10 and data["max_extra"] == 2 and data["scores"] == {}

    scores = CORE | {"health": 3, "rest": 2, "home": 8}
    r = await api.put("/api/wheel", headers=auth(), json={"scores": scores})
    assert r.status_code == 200
    body = r.json()
    assert body["scores"] == scores
    assert body["lows"][:2] == ["rest", "health"] and "home" not in body["lows"]  # сначала самые низкие

    async with sessionmaker() as session:
        user = await teams.get_user_by_telegram_id(session, 42)
        assert user.onboarding_step == OnboardingStep.EXPLORE
    assert "Explore" in api.tg.sent(42)[0]  # следующий шаг — в чат

    # до Eliminate можно переоценить и снять доп. сферу; подсказка в чат второй раз не приходит
    r = await api.put("/api/wheel", headers=auth(), json={"scores": CORE | {"health": 9}})
    assert r.json()["scores"] == CORE | {"health": 9}
    assert len(api.tg.sent(42)) == 1


@pytest.mark.parametrize(
    "scores, detail",
    [
        ({"career": 5}, "bad_spheres"),  # не все основные
        (CORE | {"mars": 5}, "bad_spheres"),
        (CORE | {"home": 5, "spirit": 5, "friends": 5}, "bad_spheres"),  # больше 2 доп.
        (CORE | {"career": 11}, "bad_score"),
        (CORE | {"career": 0}, "bad_score"),
    ],
)
async def test_wheel_validation(api, scores, detail):
    r = await api.put("/api/wheel", headers=auth(), json={"scores": scores})
    assert r.status_code == 422 and r.json()["detail"] == detail


async def test_wheel_locked_after_eliminate(api, sessionmaker):
    await api.get("/api/me", headers=auth())
    async with sessionmaker() as session:
        (await teams.get_user_by_telegram_id(session, 42)).onboarding_step = OnboardingStep.ELIMINATE
        await session.commit()
    r = await api.put("/api/wheel", headers=auth(), json={"scores": CORE})
    assert r.status_code == 409


async def test_frontend_served(api):
    r = await api.get("/")
    assert r.status_code == 200 and "html" in r.headers["content-type"]
    assert (await api.get("/healthz")).json() == {"ok": True}


# ---------- бот ↔ Mini App ----------

def test_webapp_url_from_fly(monkeypatch):
    from bot.config import load_settings

    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    monkeypatch.setenv("FLY_APP_NAME", "twelve-weeks-astana")
    assert load_settings().webapp_url == "https://twelve-weeks-astana.fly.dev"
    monkeypatch.setenv("WEBAPP_URL", "https://abc.ngrok-free.app/")
    assert load_settings().webapp_url == "https://abc.ngrok-free.app"
    monkeypatch.delenv("WEBAPP_URL")
    monkeypatch.delenv("FLY_APP_NAME")
    assert load_settings().webapp_url == ""


async def test_start_offers_app_button(sessionmaker):
    from bot.handlers.start import cmd_start
    from tests.helpers import make_message

    settings = Settings(bot_token=TOKEN, database_path=None, webapp_url="https://app.example")
    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session, settings=settings)
    buttons = [b for row in msg.answer.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert buttons[0].web_app.url == "https://app.example"
    assert buttons[1].callback_data == "wheel:begin"  # запасной путь в чате

    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session, settings=Settings(bot_token=TOKEN, database_path=None))
    buttons = [b for row in msg.answer.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["wheel:begin"]  # без адреса — только чат


async def test_menu_button():
    from aiogram.methods import SetChatMenuButton

    from bot.commands import set_webapp_menu_button

    bot, tg = make_fake_bot()
    await set_webapp_menu_button(bot, Settings(bot_token=TOKEN, database_path=None, webapp_url="https://app.example"))
    await set_webapp_menu_button(bot, Settings(bot_token=TOKEN, database_path=None))
    first, second = tg.of_type(SetChatMenuButton)
    assert first.menu_button.web_app.url == "https://app.example" and second.menu_button.type == "commands"
