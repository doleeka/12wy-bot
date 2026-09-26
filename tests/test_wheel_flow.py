"""Прогон онбординга колеса через хендлеры с поддельными объектами Telegram."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers.start import cmd_start
from bot.handlers.wheel import on_wheel_callback
from bot.models import OnboardingStep
from bot.services import wheel
from bot.services.users import get_or_create_user

TG_USER = SimpleNamespace(id=42, username="anna", first_name="Анна")


def make_message():
    return SimpleNamespace(from_user=TG_USER, answer=AsyncMock(), edit_text=AsyncMock(), edit_reply_markup=AsyncMock())


def make_callback(data, message):
    return SimpleNamespace(from_user=TG_USER, data=data, message=message, answer=AsyncMock())


async def press(sessionmaker, data, message):
    async with sessionmaker() as session:
        await on_wheel_callback(make_callback(data, message), session)
        await session.commit()


async def test_full_wheel_flow(sessionmaker):
    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session)
        await session.commit()
    welcome = msg.answer.call_args.args[0]
    assert "12 Week Year" in welcome and "Essentialism" in welcome and "Анна" in welcome

    await press(sessionmaker, "wheel:begin", msg)
    assert "Карьера" in msg.answer.call_args.args[0]

    # доп. сфера до основных — игнорируется
    await press(sessionmaker, "wheel:rate:home:5", msg)

    for i, sphere in enumerate(wheel.CORE_SPHERES):
        await press(sessionmaker, f"wheel:rate:{sphere.key}:{i + 3}", msg)
    assert "Основные 6 сфер готовы" in msg.edit_text.call_args.args[0]

    await press(sessionmaker, "wheel:extra:home", msg)
    assert "Дом и быт" in msg.edit_text.call_args.args[0]
    await press(sessionmaker, "wheel:rate:home:9", msg)
    assert "Основные 6 сфер готовы" in msg.edit_text.call_args.args[0]  # можно добавить ещё одну

    await press(sessionmaker, "wheel:finish", msg)
    result = msg.edit_text.call_args.args[0]
    assert "Твоё колесо баланса" in result and "Карьера" in result and "Дом и быт" in result
    assert "Explore" in msg.answer.call_args.args[0]

    async with sessionmaker() as session:
        user = await get_or_create_user(session, TG_USER)
        assert user.onboarding_step == OnboardingStep.EXPLORE
        scores = await wheel.get_scores(session, user)
        assert scores == {"career": 3, "health": 4, "relationships": 5, "finance": 6, "growth": 7, "rest": 8, "home": 9}

    # после завершения колесо больше не меняется
    await press(sessionmaker, "wheel:rate:career:10", msg)
    async with sessionmaker() as session:
        user = await get_or_create_user(session, TG_USER)
        assert (await wheel.get_scores(session, user))["career"] == 3


async def test_start_resumes_wheel(sessionmaker):
    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session)
        await session.commit()
    await press(sessionmaker, "wheel:rate:career:7", msg)

    msg2 = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg2, session)
    assert "Продолжим" in msg2.answer.call_args_list[0].args[0]
    assert "Здоровье" in msg2.answer.call_args_list[1].args[0]


def test_dispatcher_builds():
    from bot.main import build_dispatcher

    assert build_dispatcher(sessionmaker=None) is not None
