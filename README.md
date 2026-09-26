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
| `/backup` | админ | прислать бэкап базы прямо сейчас |

Админы задаются в `ADMIN_IDS`.

Расписание (часовой пояс `TIMEZONE`): чек-ин — `CHECKIN_DAY` в `CHECKIN_TIME` (по умолчанию воскресенье 18:00),
напоминание о старте недели с подсказкой про буфер — понедельник в `PLANNING_TIME` (09:00),
бэкап базы всем `ADMIN_IDS` — каждую ночь в `BACKUP_TIME` (03:00).

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
  scheduler.py     APScheduler: воскресный чек-ин, понедельничное напоминание, ночной бэкап
  backup_job.py    отправка бэкапа админам
  handlers/admin.py  /backup
  services/backup.py копия через SQLite backup API, восстановление из restore.db
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
- [x] Автобэкап базы админам (каждую ночь + `/backup`), восстановление через `restore.db`
- [x] Dockerfile + fly.toml с persistent volume

## Деплой на Fly.io

База — файл SQLite на **persistent volume** (`/data/bot.db`). Без volume файл жил бы в
файловой системе контейнера и пропадал при каждом деплое и перезапуске.

> Про стоимость: условия бесплатного тарифа Fly менялись — для новых аккаунтов его может не быть.
> Проверьте актуальные цены на https://fly.io/docs/about/pricing/. Этому боту хватает самой маленькой
> машины (shared-cpu-1x, 256 МБ) и volume на 1 ГБ.

Один раз:

```bash
# 1. Установить flyctl и войти: https://fly.io/docs/flyctl/install/
fly auth login

# 2. Создать приложение. Имя уникально во всём Fly — если занято, поменяйте его и в fly.toml (app = "...")
fly apps create 12wy-bot

# 3. Создать volume в том же регионе, что primary_region в fly.toml
fly volumes create bot_data --size 1 --region fra --app 12wy-bot

# 4. Секреты — токен и админы (в репозиторий не попадают)
fly secrets set BOT_TOKEN=123456:ABC... ADMIN_IDS=123456789 --app 12wy-bot

# 5. Деплой. Ровно ОДНА машина: две одновременно опрашивали бы Telegram (ошибка Conflict)
#    и у каждой была бы своя отдельная база
fly deploy --ha=false
```

Дальше обновление — просто `fly deploy`. Полезное:

```bash
fly logs                 # логи бота
fly status               # должна быть одна машина в состоянии started
fly scale count 1        # если машин вдруг стало больше
fly ssh console          # зайти внутрь; база лежит в /data/bot.db
```

## Бэкапы и восстановление

Каждую ночь бот присылает всем `ADMIN_IDS` файл базы документом (`12wy-bot_ГГГГ-ММ-ДД_ЧЧММ.db`)
с подписью: сколько участниц, команд и отметок. Копия делается через SQLite backup API и проверяется
`integrity_check`, так что файл целостный и самодостаточный. Если бэкап не удался — админам придёт
сообщение об ошибке. Вручную — команда `/backup`.

Дополнительно Fly сам делает ежедневные снапшоты volume (`fly volumes snapshots list <volume_id>`).

Восстановить из бэкапа:

```bash
# 1. Скачать нужный файл из Telegram, затем загрузить его на volume под именем restore.db
fly ssh sftp shell --app 12wy-bot
» put 12wy-bot_2026-09-27_0300.db /data/restore.db
# 2. Перезапустить бота
fly apps restart 12wy-bot
```

При старте бот увидит `/data/restore.db`, проверит его и подменит им базу. Прежняя база
не удаляется — она остаётся рядом как `bot.before-restore-<дата>.db`. Если файл битый,
бот запишет ошибку в лог и продолжит работать с текущей базой.

Локально это работает так же: положите файл рядом с базой как `data/restore.db` и перезапустите.

## Docker локально

```bash
docker build -t 12wy-bot .
docker run --env-file .env -e DATABASE_PATH=/data/bot.db -v 12wy-data:/data 12wy-bot
```
