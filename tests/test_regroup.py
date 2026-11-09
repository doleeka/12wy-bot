"""/regroup: после ухода части участниц — одиночек к парам (2 → 3), оставшихся одиночек друг к другу по трое.
Сначала план, с confirm — переводы с уведомлениями; фокус-группы, полные тройки и другие потоки не трогаем."""
from aiogram.methods import BanChatMember

from bot.models import Team
from bot.services import teams
from tests.test_group_chat import ADMIN_ID, send
from tests.test_payments import clock, member, tg  # noqa: F401


async def build(sessionmaker, layout):
    """layout: {имя команды: [telegram_id, …]} — создаём участниц и раскладываем по командам 1-го потока."""
    ids = {}
    async with sessionmaker() as s:
        for name in layout:
            t = Team(name=name, cohort_id=1, is_focus=name.startswith("Фокус"))
            s.add(t)
            await s.flush()
            ids[name] = t.id
        await s.commit()
    for name, people in layout.items():
        for tg_id in people:
            await member(sessionmaker, tg_id, "focus" if name.startswith("Фокус") else "basic")
            async with sessionmaker() as s:
                await teams.move_to_team(s, await teams.get_user_by_telegram_id(s, tg_id), ids[name])
    return ids


async def team_of(sessionmaker, tg_id):
    async with sessionmaker() as s:
        return (await teams.get_team_of(s, await teams.get_user_by_telegram_id(s, tg_id))).name


async def test_regroup_plan_preview_and_confirm(sessionmaker, tg):
    await build(sessionmaker, {"A": [1, 2, 3], "B": [4, 5], "C": [6], "D": [7], "E": [8], "F": [9], "Фокус": [10]})
    async with sessionmaker() as s:
        for name, chat in (("C", -1006), ("D", -1007)):
            t = await s.scalar(teams.select(Team).where(Team.name == name))
            t.chat_id = chat
        await s.commit()
    await send(tg, "/regroup", ADMIN_ID, "private", ADMIN_ID)
    preview = tg[2].sent(ADMIN_ID)[-1]
    assert "переводов: 3" in preview and "/regroup confirm" in preview
    assert await team_of(sessionmaker, 9) == "F"  # предпросмотр ничего не меняет

    tg[2].calls.clear()
    await send(tg, "/regroup confirm", ADMIN_ID, "private", ADMIN_ID)
    assert await team_of(sessionmaker, 9) == "B"  # одиночка без чата — к паре
    assert {await team_of(sessionmaker, i) for i in (6, 7, 8)} == {"C"}  # остальные одиночки — вместе, в команду с чатом
    assert await team_of(sessionmaker, 10) == "Фокус"
    async with sessionmaker() as s:
        sizes = sorted(len(m) for t, m in await teams.list_teams(s) if m and not t.is_focus)
    assert sizes == [3, 3, 3]
    assert "перевела тебя в команду" in tg[2].sent(9)[-1] and "U4" in tg[2].sent(9)[-1]
    assert (-1007, 7) in [(b.chat_id, b.user_id) for b in tg[2].of_type(BanChatMember)]  # из прежнего чата
    report = tg[2].sent(ADMIN_ID)[-1]
    assert "переведено: 3" in report and "Опустели команды с чатом: D" in report

    await send(tg, "/regroup", ADMIN_ID, "private", ADMIN_ID)
    assert "Пересобирать нечего" in tg[2].sent(ADMIN_ID)[-1]


async def test_regroup_counts(sessionmaker, tg):
    # четыре одиночки без пар — 2 + 2, а не 3 + 1; другой поток не смешиваем
    await build(sessionmaker, {"P": [21], "Q": [22], "R": [23], "S": [24]})
    async with sessionmaker() as s:
        lone = Team(name="Поток2", cohort_id=2)
        s.add(lone)
        await s.commit()
        lone_id = lone.id
    await member(sessionmaker, 25, "basic", cohort=2)
    async with sessionmaker() as s:
        await teams.move_to_team(s, await teams.get_user_by_telegram_id(s, 25), lone_id)
        plan = await teams.regroup_plan(s)
    assert len(plan) == 2 and {n.name for _, _, n in plan} == {"P", "R"}
    await send(tg, "/regroup", 5, "private", 5)
    assert "только для админа" in tg[2].sent(5)[-1]
