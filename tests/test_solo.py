"""Самостоятельное прохождение (/solo, /unsolo): только приложение, без тройки. Решает админ; личные напоминания
остаются, командного — нет; в /teams и в сводке такие участницы — отдельно."""
from datetime import date

from aiogram.methods import BanChatMember

from bot.config import Settings
from bot.digest import digest_text
from bot.scheduler import send_week_planning
from bot.services import checkins, scorecard, teams
from bot.services.stats import week_stats
from tests.fake_telegram import make_fake_bot
from tests.test_checkin import make_user
from tests.test_group_chat import ADMIN_ID, send
from tests.test_members import community, tg  # noqa: F401 — фикстура tg

WEEK = scorecard.week_start(date.today())


async def user(sessionmaker, tg_id):
    async with sessionmaker() as session:
        u = await teams.get_user_by_telegram_id(session, tg_id)
        return u, await teams.get_team_of(session, u)


async def test_solo_leaves_trio_keeps_personal_reminders(sessionmaker, tg):  # noqa: F811
    await community(sessionmaker)  # админ, Арман, Дана — команда 1; Айгерим — команда 2
    async with sessionmaker() as session:
        team = await teams.get_team_of(session, await teams.get_user_by_telegram_id(session, 2))
        team.chat_id = -100500
        await session.commit()
    tg[2].calls.clear()

    await send(tg, "/solo Дана", ADMIN_ID, "private", ADMIN_ID)
    report = tg[2].sent(ADMIN_ID)[-1]
    assert "Дана проходит самостоятельно" in report and "Команда №1" in report and "/unsolo 2" in report
    assert "Из группового чата" in report
    dana, team = await user(sessionmaker, 2)
    assert dana.no_team and team is None and dana.is_ready
    # ей — одно сообщение, что всё сохранено; тройке — что она теперь сама; из чата тройки убрана
    assert "самостоятельное прохождение" in tg[2].sent(2)[-1] and "сохранены" in tg[2].sent(2)[-1]
    assert any("самостоятельно" in t for t in tg[2].sent(-100500))  # у тройки есть чат — сообщение туда
    assert [(b.chat_id, b.user_id) for b in tg[2].of_type(BanChatMember)] == [(-100500, 2)]

    # личные напоминания — как у всех
    async with sessionmaker() as session:
        assert 2 in [u.telegram_id for u in await checkins.users_for_checkin(session, WEEK)]
        assert 2 not in [u.telegram_id for u in await teams.ready_without_team(session)]  # не «ждёт команду»
    bot2, tg2 = make_fake_bot()
    await send_week_planning(bot2, sessionmaker, Settings(bot_token="x", database_path=None, admin_ids=[]))
    assert tg2.sent(2)

    # новые участницы её место в тройке занимают, её саму не распределяют
    await make_user(sessionmaker, 9)
    assert (await user(sessionmaker, 9))[1].id == (await user(sessionmaker, 1))[1].id
    assert (await user(sessionmaker, 2))[1] is None

    # /teams — отдельный раздел; /solo — список
    await send(tg, "/teams", ADMIN_ID, "private", ADMIN_ID)
    text = tg[2].sent(ADMIN_ID)[-1]
    solo_block = text.split("Самостоятельно")[1]
    assert "Дана" in solo_block and "Готовы, но без команды" not in text
    await send(tg, "/solo", ADMIN_ID, "private", ADMIN_ID)
    assert "Дана" in tg[2].sent(ADMIN_ID)[-1] and "(1)" in tg[2].sent(ADMIN_ID)[-1]


async def test_unsolo_returns_to_a_trio(sessionmaker, tg):  # noqa: F811
    await community(sessionmaker)
    await send(tg, "/solo @arman", ADMIN_ID, "private", ADMIN_ID)
    tg[2].calls.clear()
    await send(tg, "/unsolo 1", ADMIN_ID, "private", ADMIN_ID)
    assert any("снова в тройке" in t for t in tg[2].sent(ADMIN_ID))
    arman, team = await user(sessionmaker, 1)
    assert not arman.no_team and team is not None
    assert "перевела тебя в команду" in tg[2].sent(1)[-1]
    # не прошла онбординг — вернётся в распределение позже
    async with sessionmaker() as session:
        a = await teams.get_user_by_telegram_id(session, 3)
        a.no_team, a.is_ready = True, False
        await teams.leave_team(session, a)
        await session.commit()
    await send(tg, "/unsolo Айгерим", ADMIN_ID, "private", ADMIN_ID)
    assert "когда закончит онбординг" in tg[2].sent(ADMIN_ID)[-1]


async def test_guards(sessionmaker, tg):  # noqa: F811
    await community(sessionmaker)
    await send(tg, "/solo Дана", 1, "private", 1)
    assert "только для админа" in tg[2].sent(1)[-1]
    assert not (await user(sessionmaker, 2))[0].no_team

    await send(tg, f"/solo {ADMIN_ID}", ADMIN_ID, "private", ADMIN_ID)
    assert "/noteam" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/solo Нет такой", ADMIN_ID, "private", ADMIN_ID)
    assert "Не нашла" in tg[2].sent(ADMIN_ID)[-1]
    await send(tg, "/solo", ADMIN_ID, "private", ADMIN_ID)
    assert "все участницы — в тройках" in tg[2].sent(ADMIN_ID)[-1]

    await make_user(sessionmaker, 4)
    async with sessionmaker() as session:
        (await teams.get_user_by_telegram_id(session, 4)).first_name = "Дана"
        await session.commit()
    await send(tg, "/solo дана", ADMIN_ID, "private", ADMIN_ID)
    assert "несколько" in tg[2].sent(ADMIN_ID)[-1] and "/solo id" in tg[2].sent(ADMIN_ID)[-1]

    await send(tg, "/solo 2", ADMIN_ID, "private", ADMIN_ID)
    tg[2].calls.clear()
    await send(tg, "/solo 2", ADMIN_ID, "private", ADMIN_ID)
    assert "уже проходит самостоятельно" in tg[2].sent(ADMIN_ID)[-1]
    assert not tg[2].sent(2)  # повтор — ей второй раз ничего не пишем
    await send(tg, "/unsolo 3", ADMIN_ID, "private", ADMIN_ID)
    assert "и так в распределении" in tg[2].sent(ADMIN_ID)[-1]


async def test_moveteam_none_works_the_same(sessionmaker, tg):  # noqa: F811
    await community(sessionmaker)
    await send(tg, "/moveteam 2 none", ADMIN_ID, "private", ADMIN_ID)
    assert "Дана проходит самостоятельно" in tg[2].sent(ADMIN_ID)[-1]
    dana, team = await user(sessionmaker, 2)
    assert dana.no_team and team is None


async def test_digest_counts_solo_separately(sessionmaker, tg):  # noqa: F811
    await community(sessionmaker)
    await send(tg, "/solo Дана", ADMIN_ID, "private", ADMIN_ID)
    async with sessionmaker() as session:
        dana = await teams.get_user_by_telegram_id(session, 2)
        for t in await checkins.tactics_for_week(session, dana, WEEK):
            await checkins.set_mark(session, dana, t.id, WEEK, True)
        await session.commit()
        s = await week_stats(session, WEEK)
    assert (s.solo.checked, s.solo.total) == (1, 1)
    assert sum(t.total for t in s.teams) + s.solo.total == s.participants
    text = digest_text(s, None)
    assert "Самостоятельно</b> (без тройки) — сделали чек-ин: 1 из 1" in text and "Дана" not in text
