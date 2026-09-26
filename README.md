# 12WY Bot — 12 Week Year + Essentialism

Telegram-бот для комьюнити: ведёт участниц по системе 12 Week Year через фильтр Essentialism
(Explore → Eliminate → Execute), с еженедельными чек-инами, скорингом и командами поддержки.

## Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # вписать BOT_TOKEN и ADMIN_IDS
python -m bot.main
```

Тесты: `pytest`

## Команды бота

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | все | онбординг / продолжить с текущего шага |
| `/checkin` | участница | чек-ин недели: ✅/❌ по тактикам → % и оценка |
| `/plan` | участница | мои приоритеты, «зачем» и тактики |
| `/team` | участница | моя команда и % выполнения за неделю (только проценты) |
| `/teams` | админ | все команды с telegram_id участниц |
| `/moveteam <telegram_id> <team_id\|new>` | админ | перевести участницу в другую / новую команду |
| `/report <telegram_id> [team\|admin\|none]` | админ | куда отправлять отчёт участницы (без аргумента — показать) |

Админы задаются в `ADMIN_IDS`.

Расписание (часовой пояс `TIMEZONE`): чек-ин — `CHECKIN_DAY` в `CHECKIN_TIME` (по умолчанию воскресенье 18:00),
напоминание о старте недели с подсказкой про буфер — понедельник в `PLANNING_TIME` (09:00).

## Структура

```
bot/
  main.py          точка входа, сборка Dispatcher
  config.py        настройки из .env (BOT_TOKEN, DATABASE_PATH, ADMIN_IDS, TIMEZONE)
  db.py            движок SQLite, сессии, создание таблиц
  models.py        все таблицы
  middlewares.py   сессия БД на каждый апдейт
  texts.py         все тексты бота
  keyboards.py     inline-клавиатуры
  handlers/        start.py (/start), wheel.py (колесо баланса),
                   onboarding.py (Explore, Eliminate, Intent, тактики, /plan),
                   teams.py (/team, /teams, /moveteam), checkin.py (/checkin, /report)
  scheduler.py     APScheduler: воскресный чек-ин, понедельничное напоминание
  services/        бизнес-логика без привязки к Telegram
tests/
```

Путь к базе задаётся `DATABASE_PATH` — на Fly.io это будет путь на persistent volume (например `/data/bot.db`).

## Статус

- [x] Структура проекта, `requirements.txt`
- [x] Модели: users, wheel_of_balance, explore_list, priorities, essential_intent, weekly_tactics, checkins, teams, team_members
- [x] Настройка бота через `.env`
- [x] `/start` + колесо баланса (6 основных сфер + до 2 дополнительных, визуализация и просадки)
- [x] Explore → Eliminate (ровно 3) → Essential intent → Тактики, `/plan`
- [x] Автораспределение в команды (до 3 человек), `/team`, админские `/teams` и `/moveteam`
- [x] Еженедельный чек-ин и scorecard, `send_report` (`/report`), расписание (APScheduler)
- [ ] Автобэкап базы админу (APScheduler)
- [ ] Dockerfile + fly.toml с volume
