import pytest

from bot.models import User
from bot.services import wheel


def test_core_order_and_extras():
    scores = {}
    assert wheel.next_core_sphere(scores).key == "career"
    assert not wheel.can_rate(scores, "home")  # доп. сферы — только после основных

    for s in wheel.CORE_SPHERES:
        scores[s.key] = 5
    assert wheel.next_core_sphere(scores) is None
    assert len(wheel.available_extras(scores)) == len(wheel.EXTRA_SPHERES)
    assert wheel.can_rate(scores, "home")

    scores["home"] = 7
    scores["spirit"] = 8
    assert wheel.available_extras(scores) == []
    assert not wheel.can_rate(scores, "friends")  # не больше 2 дополнительных
    assert wheel.can_rate(scores, "home")  # переоценить уже выбранную можно
    assert not wheel.can_rate(scores, "unknown")


def test_chart_and_lows():
    scores = {"career": 8, "health": 3, "relationships": 7, "finance": 5, "growth": 9, "rest": 2}
    chart = wheel.render_chart(scores).splitlines()
    assert len(chart) == 6
    assert chart[1].startswith("Здоровье") and "███░░░░░░░" in chart[1] and "⚠" in chart[1]
    assert [s.key for s, _ in wheel.low_spheres(scores)] == ["rest", "health", "finance"]
    assert wheel.average(scores) == 5.7

    high = {k: 8 for k in scores} | {"rest": 7}
    assert [s.key for s, _ in wheel.low_spheres(high)] == ["rest"]


async def test_save_score_upserts(sessionmaker):
    async with sessionmaker() as session:
        user = User(telegram_id=1)
        session.add(user)
        await session.flush()
        await wheel.save_score(session, user, "career", 4)
        await wheel.save_score(session, user, "career", 6)
        assert await wheel.get_scores(session, user) == {"career": 6}
        with pytest.raises(ValueError):
            await wheel.save_score(session, user, "career", 11)
        with pytest.raises(ValueError):
            await wheel.save_score(session, user, "nope", 5)
