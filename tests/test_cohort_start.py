from datetime import date

import pytest

from bot.config import load_settings
from bot.models import OnboardingStep, User
from bot.services import onboarding as svc

OCT5 = date(2026, 10, 5)


def test_cycle_start_with_cohort():
    assert svc.cycle_start_for(date(2026, 9, 26), OCT5) == OCT5  # раньше общего старта — ждём 5.10
    assert svc.cycle_start_for(date(2026, 10, 5), OCT5) == OCT5  # в сам понедельник старта
    # подтвердила, когда общий цикл уже идёт, — тот же старт 5.10 (у всех одна неделя)
    assert svc.cycle_start_for(date(2026, 10, 7), OCT5) == OCT5
    assert svc.cycle_start_for(date(2026, 12, 27), OCT5) == OCT5   # последний день 12-й недели
    assert svc.cycle_start_for(date(2026, 12, 28), OCT5) == date(2026, 12, 28)  # общий цикл закончился
    assert svc.cycle_start_for(date(2026, 9, 26)) == date(2026, 9, 28)  # без общей даты — как раньше


def test_config_requires_monday(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("CYCLE_START", "2026-10-05")
    assert load_settings().cycle_start == OCT5
    monkeypatch.setenv("CYCLE_START", "2026-10-01")  # четверг
    with pytest.raises(RuntimeError, match="понедельник"):
        load_settings()
    monkeypatch.delenv("CYCLE_START")
    assert load_settings().cycle_start is None


async def test_align_moves_only_not_started(sessionmaker):
    today = date(2026, 9, 26)
    async with sessionmaker() as session:
        session.add_all([
            User(telegram_id=1, onboarding_step=OnboardingStep.DONE, cycle_start=date(2026, 9, 28)),  # ещё не начался
            User(telegram_id=2, onboarding_step=OnboardingStep.DONE, cycle_start=date(2026, 9, 21)),  # уже идёт
            User(telegram_id=3, onboarding_step=OnboardingStep.DONE, cycle_start=date(2026, 10, 12)),  # опоздала на неделю
            User(telegram_id=4, onboarding_step=OnboardingStep.WHEEL),  # без старта
            User(telegram_id=5, onboarding_step=OnboardingStep.DONE, cycle_start=date(2027, 1, 4)),  # следующий цикл
        ])
        await session.commit()
        assert await svc.align_to_cohort_start(session, OCT5, today) == 2
        await session.commit()
    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = {u.telegram_id: u.cycle_start for u in await session.scalars(select(User))}
    assert rows == {1: OCT5, 2: date(2026, 9, 21), 3: OCT5, 4: None, 5: date(2027, 1, 4)}


def test_chat_onboarding_uses_cohort():
    user = User(onboarding_step=OnboardingStep.TACTICS, onboarding_position=3)
    assert svc.advance_tactics(user, date(2026, 9, 26), OCT5)
    assert user.cycle_start == OCT5
