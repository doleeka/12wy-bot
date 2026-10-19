from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.backup_job import send_backup
from bot.config import Settings, local_today
from bot.digest import stats_message
from bot.filters import IsAdmin
from bot.models import OnboardingStep
from bot.services.cycle import end_test_cycle, find_test_marks, in_test_mode, real_start_for, start_test_cycle
from bot.services import teams
from bot.services.onboarding import reset_onboarding
from bot.services.users import get_or_create_user

router = Router(name="admin")
# Личные команды — только в личке: в группе /checkin или /plan показали бы цели и тактики всем
router.message.filter(F.chat.type == "private")


@router.message(Command("backup"), IsAdmin())
async def cmd_backup(message: Message, bot: Bot, settings: Settings) -> None:
    await message.answer(texts.BACKUP_STARTED)
    await send_backup(bot, settings)


@router.message(Command("myapp", "testapp", "admin"), IsAdmin())
async def cmd_modes(message: Message, command: CommandObject, settings: Settings, session: AsyncSession | None = None) -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    url = settings.webapp_url.rstrip("/")
    mode = command.command
    if mode == "admin":
        await message.answer("Управление настоящим сообществом\n\n/teams — состав команд\n/moveteam — перевести участницу\n/stats — сводка\n/backup — резервная копия\n\n/myapp — мои настоящие цели и прогресс\n/testapp — отдельная тестовая версия\n\nАдминистративные команды меняют реальные команды. Тестируй личные сценарии через /testapp.")
        return
    if not url:
        await message.answer("Адрес приложения пока не настроен.")
        return
    test = mode == "testapp"
    rows = [[InlineKeyboardButton(text="Открыть тестовую версию" if test else "Моё личное приложение",
            web_app=WebAppInfo(url=url + "/sandbox/" if test else url))]]
    if not test and session is not None:
        user = await get_or_create_user(session, message.from_user)
        rows.append([InlineKeyboardButton(text="Поставить личные цели с нуля", callback_data=f"personal:restart:{user.cycle}")])
    await message.answer(
        "Тестовая версия: отдельный профиль и база. Личные цели и данные участниц не затрагиваются, рассылок нет." if test else "Твоё личное прохождение: настоящие цели, отметки и история.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("personal:"), IsAdmin())
async def personal_restart(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer()
        return
    user = await get_or_create_user(session, callback.from_user)
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) != user.cycle:
        await callback.answer("Этот запрос уже выполнен. Открой /myapp.", show_alert=True)
        return
    if parts[1] == "restart":
        await callback.message.answer(
            "Начать твой личный план с колеса баланса? Прежние записи сохранятся как предыдущий цикл. Команда останется прежней. Тестовый профиль и другие участницы не изменятся.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, поставить цели с нуля", callback_data=f"personal:confirm:{user.cycle}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"personal:cancel:{user.cycle}")]]))
    elif parts[1] == "confirm":
        user.cycle += 1
        user.onboarding_step = OnboardingStep.WHEEL
        user.onboarding_position = None
        user.cycle_start = None
        user.last_reported_week = None
        user.test_mode_since = None
        user.is_ready = False
        await session.commit()
        from bot.keyboards import open_app
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Личный план готов к заполнению с нуля: колесо баланса → желания → приоритеты → действия. Прежние записи сохранены.", reply_markup=open_app(settings.webapp_url))
    elif parts[1] == "cancel":
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.message(Command("resetme"), IsAdmin())
async def cmd_resetme(message: Message, session: AsyncSession) -> None:
    await message.answer("Личное прохождение защищено от тестового сброса. Открой /testapp и нажми «Новый тест» — это затронет только тестовые данные.")


@router.message(Command("testcycle"), IsAdmin())
async def cmd_testcycle(message: Message, command: CommandObject, session: AsyncSession,
                        settings: Settings | None = None, sandbox_sessionmaker=None) -> None:
    if sandbox_sessionmaker is None:
        await message.answer("Тестовая база недоступна. Личное прохождение не изменено. Открой /testapp.")
        return
    await message.answer("Только тестовый профиль: проверяй в /testapp. Личное прохождение /myapp не изменится.")
    async with sandbox_sessionmaker() as test_session:
        user = await get_or_create_user(test_session, message.from_user)
        arg = (command.args or "1").strip().lower()
        if not arg.isdigit() or not 1 <= int(arg) <= 12:
            await message.answer("Выбери тестовую неделю от 1 до 12: /testcycle 3. Новый тест — кнопка в /testapp.")
            return
        if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
            await message.answer(texts.TEST_CYCLE_NOT_READY)
            return
        user.sandbox_week = int(arg)
        user.onboarding_step = OnboardingStep.DONE
        await message.answer(f"Тестовая неделя {arg} из 12. Прогресс остальных недель сохранён. Открой /testapp.")
        await test_session.commit()


async def _testcycle(message: Message, command: CommandObject, session: AsyncSession,
                     settings: Settings | None = None) -> None:
    user = await get_or_create_user(session, message.from_user)
    if user.onboarding_step not in (OnboardingStep.DONE, OnboardingStep.FINISHED):
        await message.answer(texts.TEST_CYCLE_NOT_READY)
        return
    arg = (command.args or "").strip().lower()
    today = local_today(settings)
    if arg in ("off", "off confirm"):
        # идёт общий цикл — возвращаемся в него (05.10), а не на следующий понедельник
        real_start = real_start_for(today, settings.cycle_start if settings else None)
        if not in_test_mode(user, real_start):
            await message.answer(texts.TEST_CYCLE_NOT_ON.format(start=f"{real_start:%d.%m}"))
            return
        if arg == "off":  # сначала — что именно изменится; без подтверждения ничего не трогаем
            marks = await find_test_marks(session, user, real_start)
            weeks = "\n".join(
                texts.TEST_CYCLE_OFF_WEEK.format(start=f"{d:%d.%m}", n=n, done=done) for d, n, done in marks.by_week()
            ) or texts.TEST_CYCLE_OFF_NO_WEEKS
            await message.answer(texts.TEST_CYCLE_OFF_PREVIEW.format(
                now=f"{user.cycle_start:%d.%m}" if user.cycle_start else "—", start=f"{real_start:%d.%m}",
                weeks=weeks, daily=len(marks.daily),
            ))
            return
        marks = await end_test_cycle(session, user, real_start)
        user.onboarding_step = OnboardingStep.DONE
        await message.answer(texts.TEST_CYCLE_OFF.format(
            start=f"{real_start:%d.%m}", checkins=len(marks.checkins), daily=len(marks.daily)
        ))
        return
    if arg and not (arg.isdigit() and 1 <= int(arg) <= 12):
        await message.answer(texts.TEST_CYCLE_USAGE)
        return
    week = int(arg or 1)
    start = start_test_cycle(user, today, week)
    user.onboarding_step = OnboardingStep.DONE
    await message.answer(texts.TEST_CYCLE_ON.format(start=f"{start:%d.%m}", week=week))


@router.message(Command("noteam"), IsAdmin())
async def cmd_noteam(message: Message, command: CommandObject, session: AsyncSession) -> None:
    """Организатор вне команд: выйти из своей тройки и не попадать в распределение (/noteam off — вернуть)."""
    user = await get_or_create_user(session, message.from_user)
    if (command.args or "").strip().lower() == "off":
        user.no_team = False
        await message.answer(texts.NO_TEAM_OFF)
        return
    user.no_team = True
    team = await teams.leave_team(session, user)
    left = texts.NO_TEAM_LEFT.format(team=escape(teams.team_name(team))) if team else ""
    await message.answer(texts.NO_TEAM_ON.format(left=left))


@router.message(Command("stats"), IsAdmin())
async def cmd_stats(message: Message, session: AsyncSession, settings: Settings | None = None) -> None:
    """Сводка сообщества: прошлая неделя и текущая. Только количество — без имён."""
    await message.answer(await stats_message(session, settings, local_today(settings)))


@router.message(Command("backup", "resetme", "testcycle", "stats", "noteam", "myapp", "testapp", "admin"))
async def backup_admin_only(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY)
