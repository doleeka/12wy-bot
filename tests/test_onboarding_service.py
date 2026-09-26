from datetime import date

import pytest

from bot.models import OnboardingStep, User
from bot.services import onboarding as svc


async def _user(session, step=OnboardingStep.EXPLORE):
    user = User(telegram_id=1, onboarding_step=step)
    session.add(user)
    await session.flush()
    return user


def test_parse_explore_text():
    text = "- Спорт\n\n2) Английский\n• Книга  \n  Переезд"
    assert svc.parse_explore_text(text) == ["Спорт", "Английский", "Книга", "Переезд"]


def test_measurable():
    assert svc.is_measurable("3 тренировки по 30 минут")
    assert svc.is_measurable("Читать каждый вечер")
    assert not svc.is_measurable("Больше заботиться о здоровье")


def test_cycle_start_is_next_monday():
    assert svc.cycle_start_for(date(2026, 9, 28)) == date(2026, 9, 28)  # понедельник
    assert svc.cycle_start_for(date(2026, 9, 26)) == date(2026, 9, 28)  # суббота
    assert svc.cycle_start_for(date(2026, 9, 29)) == date(2026, 10, 5)  # вторник


async def test_explore_dedup_and_delete(sessionmaker):
    async with sessionmaker() as session:
        user = await _user(session)
        assert await svc.add_explore_items(session, user, ["Спорт", "спорт", "Книга"]) == 2
        assert await svc.add_explore_items(session, user, ["Книга"]) == 0
        items = await svc.get_explore_items(session, user)
        assert await svc.delete_explore_item(session, user, items[0].id)
        assert [i.text for i in await svc.get_explore_items(session, user)] == ["Книга"]


async def test_explore_limit(sessionmaker):
    async with sessionmaker() as session:
        user = await _user(session)
        added = await svc.add_explore_items(session, user, [f"п{i}" for i in range(svc.MAX_EXPLORE_ITEMS + 5)])
        assert added == svc.MAX_EXPLORE_ITEMS


async def test_eliminate_exactly_three(sessionmaker):
    async with sessionmaker() as session:
        user = await _user(session, OnboardingStep.ELIMINATE)
        await svc.add_explore_items(session, user, ["a", "b", "c", "d", "e"])
        items = await svc.get_explore_items(session, user)
        for item in items[:3]:
            await svc.toggle_selection(session, user, item.id)
        with pytest.raises(svc.TooManySelected):
            await svc.toggle_selection(session, user, items[3].id)
        await svc.toggle_selection(session, user, items[0].id)  # снять отметку
        with pytest.raises(ValueError):
            await svc.confirm_priorities(session, user)
        await svc.toggle_selection(session, user, items[4].id)
        priorities = await svc.confirm_priorities(session, user)
        assert [(p.position, p.title) for p in priorities] == [(1, "b"), (2, "c"), (3, "e")]
        assert user.onboarding_step == OnboardingStep.INTENT


async def test_intent_then_tactics_then_done(sessionmaker):
    async with sessionmaker() as session:
        user = await _user(session, OnboardingStep.ELIMINATE)
        await svc.add_explore_items(session, user, ["a", "b", "c"])
        for item in await svc.get_explore_items(session, user):
            await svc.toggle_selection(session, user, item.id)
        await svc.confirm_priorities(session, user)

        for n in range(3):
            priority = await svc.save_intent(session, user, f"причина номер {n} достаточно длинная")
            assert priority.position == n + 1
        assert await svc.save_intent(session, user, "лишняя") is None
        assert user.onboarding_step == OnboardingStep.TACTICS and user.onboarding_position == 1

        p1 = await svc.current_tactic_priority(session, user)
        await svc.add_tactic(session, user, p1, "3 раза в неделю")
        await svc.add_tactic(session, user, p1, "10 минут в день")
        with pytest.raises(ValueError):
            await svc.add_tactic(session, user, p1, "третья")

        assert not svc.advance_tactics(user, date(2026, 9, 26))
        assert not svc.advance_tactics(user, date(2026, 9, 26))
        assert svc.advance_tactics(user, date(2026, 9, 26))
        assert user.onboarding_step == OnboardingStep.DONE and user.is_ready
        assert user.cycle_start == date(2026, 9, 28)
