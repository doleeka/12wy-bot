"""Сквозной прогон: колесо → Explore → Eliminate → Intent → Тактики → готова."""
from bot.handlers.onboarding import cmd_plan, on_eliminate_callback, on_explore_callback, on_tactic_callback, on_text
from bot.handlers.start import cmd_start
from bot.handlers.wheel import on_wheel_callback
from bot.models import OnboardingStep
from bot.services import onboarding as svc
from bot.services import wheel
from bot.services.users import get_or_create_user
from tests.helpers import TG_USER, all_texts, make_bot, make_callback, make_message, make_state


class Bot:
    """Маленький драйвер: каждое действие — отдельная сессия, как в реальном боте."""

    def __init__(self, sessionmaker):
        self.sm = sessionmaker
        self.state = make_state()
        self.msg = make_message()
        self.bot = make_bot()

    async def text(self, text):
        self.msg = make_message(text)
        async with self.sm() as session:
            await on_text(self.msg, session, self.state, self.bot)
            await session.commit()
        return self.msg

    async def press(self, handler, data, **kw):
        cb = make_callback(data, self.msg)
        async with self.sm() as session:
            if handler is on_tactic_callback:
                kw.setdefault("bot", self.bot)
            await handler(cb, session, **kw)
            await session.commit()
        return cb

    async def user(self):
        async with self.sm() as session:
            return await get_or_create_user(session, TG_USER)

    async def items(self):
        async with self.sm() as session:
            return await svc.get_explore_items(session, await get_or_create_user(session, TG_USER))


async def test_full_onboarding(sessionmaker):
    bot = Bot(sessionmaker)
    async with sessionmaker() as session:
        await cmd_start(bot.msg, session)
        await session.commit()
    for sphere in wheel.CORE_SPHERES:
        await bot.press(on_wheel_callback, f"wheel:rate:{sphere.key}:6")
    await bot.press(on_wheel_callback, "wheel:finish")
    assert "Explore" in all_texts(bot.msg.answer)

    # Explore: список из нескольких строк + отдельный пункт; HTML экранируется
    msg = await bot.text("- Спорт\n- Английский <b>")
    assert "Английский &lt;b&gt;" in msg.answer.call_args.args[0]
    assert "хотя бы 3" in msg.answer.call_args.args[0]
    cb = await bot.press(on_explore_callback, "explore:done")
    assert cb.answer.call_args.kwargs.get("show_alert")  # меньше 3 — нельзя дальше
    await bot.text("Книга")
    await bot.text("Переезд")
    await bot.text("Спорт")  # дубль
    assert len(await bot.items()) == 4

    await bot.press(on_explore_callback, "explore:done")
    assert (await bot.user()).onboarding_step == OnboardingStep.ELIMINATE
    assert "ровно 3" in bot.msg.answer.call_args.args[0]

    # Eliminate: 4-й выбрать нельзя, подтвердить можно только с 3
    items = await bot.items()
    for item in items[:3]:
        await bot.press(on_eliminate_callback, f"elim:t:{item.id}")
    cb = await bot.press(on_eliminate_callback, f"elim:t:{items[3].id}")
    assert "Только 3" in cb.answer.call_args.args[0]
    await bot.press(on_eliminate_callback, "elim:ok")
    done_text = bot.msg.edit_text.call_args.args[0]
    assert "не сейчас" in done_text and "Переезд" in done_text
    assert "Почему это для тебя важно" in bot.msg.answer.call_args.args[0]

    # Intent: слишком коротко — просим подробнее
    msg = await bot.text("надо")
    assert "подробнее" in msg.answer.call_args.args[0]
    await bot.text("Хочу чувствовать энергию и силы каждый день")
    await bot.text("Английский откроет работу в международной команде")
    msg = await bot.text("Книги дают мне спокойствие и новые идеи")
    assert "буфер" in all_texts(msg.answer)  # подсказка про буфер при планировании
    assert (await bot.user()).onboarding_step == OnboardingStep.TACTICS

    # Тактики. Приоритет 1: неизмеримая → «оставить как есть», затем вторая → авто-переход
    msg = await bot.text("Больше заботиться о себе")
    assert "измеримой" in msg.answer.call_args.args[0]
    await bot.press(on_tactic_callback, "tac:keep", state=bot.state)
    assert "Тактика добавлена" in bot.msg.answer.call_args.args[0]
    msg = await bot.text("3 тренировки по 30 минут")
    assert "Приоритет 2 из 3" in msg.answer.call_args.args[0]

    # Приоритет 2: одна тактика и «Дальше»; устаревшая кнопка игнорируется
    await bot.text("2 урока английского в неделю")
    await bot.press(on_tactic_callback, "tac:next:1", state=bot.state)
    assert (await bot.user()).onboarding_position == 2
    await bot.press(on_tactic_callback, "tac:next:2", state=bot.state)
    assert "Приоритет 3 из 3" in bot.msg.answer.call_args.args[0]

    # Приоритет 3: «Дальше» без тактик не работает
    await bot.press(on_tactic_callback, "tac:next:3", state=bot.state)
    assert (await bot.user()).onboarding_position == 3
    await bot.text("Читать 20 страниц")
    await bot.press(on_tactic_callback, "tac:next:3", state=bot.state)

    user = await bot.user()
    assert user.onboarding_step == OnboardingStep.DONE and user.is_ready and user.cycle_start.weekday() == 0
    final = bot.msg.answer.call_args_list[-2].args[0]
    assert "Команда №1" in bot.msg.answer.call_args.args[0]  # сразу распределена в команду
    assert "Ты готова" in final and "3 тренировки по 30 минут" in final and "Читать 20 страниц" in final

    plan_msg = make_message()
    async with sessionmaker() as session:
        await cmd_plan(plan_msg, session)
    assert "Английский" in plan_msg.answer.call_args.args[0]


async def test_eliminate_back_to_explore(sessionmaker):
    bot = Bot(sessionmaker)
    async with sessionmaker() as session:
        user = await get_or_create_user(session, TG_USER)
        user.onboarding_step = OnboardingStep.EXPLORE
        await session.commit()
    await bot.text("a\nb\nc")
    await bot.press(on_explore_callback, "explore:done")
    await bot.press(on_eliminate_callback, "elim:back")
    assert (await bot.user()).onboarding_step == OnboardingStep.EXPLORE
    await bot.text("d")
    assert len(await bot.items()) == 4


async def test_start_resumes_each_step(sessionmaker):
    bot = Bot(sessionmaker)
    async with sessionmaker() as session:
        user = await get_or_create_user(session, TG_USER)
        user.onboarding_step = OnboardingStep.EXPLORE
        await session.commit()
    await bot.text("a\nb\nc")

    msg = make_message()
    async with sessionmaker() as session:
        await cmd_start(msg, session)
    assert "Explore" in all_texts(msg.answer) and "1. a" in all_texts(msg.answer)
