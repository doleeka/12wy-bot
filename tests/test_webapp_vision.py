"""Видение на 3+ года: необязательное, частичное, редактируемое после старта, личное; рефлексия 12-й недели."""
from datetime import date, timedelta

from sqlalchemy import func, select

from bot.models import OnboardingStep, Priority, User, Vision, WeeklyTactic
from bot.services import scorecard, teams
from tests.test_webapp import api  # noqa: F401 — фикстура
from tests.webapp_helpers import auth

THIS_WEEK = scorecard.week_start(date.today())
EMPTY = {"work": "", "life": "", "me": "", "main": ""}


async def make_user(sessionmaker, step=OnboardingStep.DONE, cycle_start=THIS_WEEK, tg_id=42):
    async with sessionmaker() as session:
        user = User(telegram_id=tg_id, first_name="Анна", onboarding_step=step, is_ready=step == OnboardingStep.DONE,
                    cycle_start=cycle_start)
        session.add(user)
        await session.flush()
        p = Priority(user_id=user.id, position=1, title="Спорт")
        session.add(p)
        await session.flush()
        session.add(WeeklyTactic(priority_id=p.id, user_id=user.id, text="Бег 3 раза"))
        await session.commit()
        if step == OnboardingStep.DONE:
            await teams.assign_to_team(session, user)
            await session.commit()


async def test_new_user_can_skip_and_save_partially(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker, step=OnboardingStep.EXPLORE, cycle_start=None)
    d = (await api.get("/api/vision", headers=auth())).json()
    assert d["vision"] == EMPTY and d["filled"] == 0 and d["saved"] is False
    assert d["reflection"]["open"] is False
    # частично: заполнен один блок, без минимальной длины; пробелы по краям убираются
    d = (await api.put("/api/vision", headers=auth(), json={"work": "  Своё дело  "})).json()
    assert d["vision"] == EMPTY | {"work": "Своё дело"} and d["filled"] == 1 and d["saved"]
    # шаг онбординга не меняется: видение — не один из 6 шагов
    assert (await api.get("/api/me", headers=auth())).json()["step"] == "explore"


async def test_edit_all_blocks_and_clear(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker)
    full = {"work": "Работаю удалённо", "life": "Живу у моря", "me": "Бегаю полумарафоны", "main": "Больше свободы"}
    assert (await api.put("/api/vision", headers=auth(), json=full)).json()["filled"] == 4
    d = (await api.put("/api/vision", headers=auth(), json=full | {"life": "", "main": " "})).json()
    assert d["vision"] == full | {"life": "", "main": ""} and d["filled"] == 2
    # очистить всё — тоже допустимо (запись остаётся, блоки пустые)
    d = (await api.put("/api/vision", headers=auth(), json={})).json()
    assert d["vision"] == EMPTY and d["saved"] is True
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Vision.id))) == 1


async def test_too_long_is_rejected_and_nothing_changes(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker)
    await api.put("/api/vision", headers=auth(), json={"work": "Было"})
    r = await api.put("/api/vision", headers=auth(), json={"work": "я" * 1001})
    assert r.status_code == 422 and r.json()["detail"] == "too_long"
    assert (await api.get("/api/vision", headers=auth())).json()["vision"]["work"] == "Было"


async def test_editable_after_start_while_plan_is_locked(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker, cycle_start=THIS_WEEK - timedelta(days=14))  # идёт неделя 3
    plan = (await api.get("/api/plan", headers=auth())).json()
    assert plan["editable"] is False
    tid = plan["priorities"][0]["tactics"][0]["id"]
    r = await api.put(f"/api/tactics/{tid}", headers=auth(), json={"text": "Бег", "days": [1]})
    assert r.json()["detail"] == "plan_locked"
    r = await api.put("/api/vision", headers=auth(), json={"main": "Уверенность в себе"})
    assert r.status_code == 200 and r.json()["vision"]["main"] == "Уверенность в себе"


async def test_existing_user_keeps_step_and_vision_stays_private(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker)
    await make_user(sessionmaker, tg_id=43)  # сокомандница
    sent_before = len(api.tg.sent(43)) + len(api.tg.sent(42))
    d = (await api.get("/api/vision", headers=auth())).json()
    assert d["saved"] is False  # у давно зарегистрированной видения ещё нет — и это нормально
    await api.put("/api/vision", headers=auth(), json={"work": "Секретный план"})
    assert (await api.get("/api/me", headers=auth())).json()["step"] == "done"  # не отправляем в онбординг
    assert len(api.tg.sent(43)) + len(api.tg.sent(42)) == sent_before  # ни команде, ни в чат
    # другая участница видит только своё
    assert (await api.get("/api/vision", headers=auth(user_id=43))).json()["vision"] == EMPTY


async def test_reflection_only_in_week_12_and_does_not_touch_score(api, sessionmaker):  # noqa: F811
    await make_user(sessionmaker)  # неделя 1
    r = await api.put("/api/vision/reflection", headers=auth(), json={"closer": "Да"})
    assert r.status_code == 409 and r.json()["detail"] == "reflection_closed"

    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 42))
        user.cycle_start = THIS_WEEK - timedelta(weeks=11)  # сейчас неделя 12
        await session.commit()
    before = (await api.get("/api/scorecard", headers=auth())).json()
    d = (await api.get("/api/vision", headers=auth())).json()
    assert d["reflection"] == {"open": True, "cycle": 1, "closer": "", "changed": "", "next": ""}
    d = (await api.put("/api/vision/reflection", headers=auth(), json={"closer": " Ближе ", "next": "Английский"})).json()
    assert d["reflection"] | {} == {"open": True, "cycle": 1, "closer": "Ближе", "changed": "", "next": "Английский"}
    assert (await api.get("/api/scorecard", headers=auth())).json() == before  # скоринг не меняется
