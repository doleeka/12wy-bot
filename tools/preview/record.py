"""Записывает ответы настоящего API на демо-данных (без реальных пользователей) в fixtures.json."""
import itertools, json, sqlite3, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unittest.mock import patch

from fastapi.testclient import TestClient
from bot import texts
from bot.config import Settings
from bot.db import create_engine, create_sessionmaker
from bot.migrate import run_migrations
from tests.webapp_helpers import TOKEN, sign_init_data
import webapp.app as appmod

# дата демо закреплена (суббота): есть «дела на сегодня», превью воспроизводимо
today = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date(2026, 9, 26); this_week = today - timedelta(days=today.weekday())
out = {"today": today.isoformat(), "modes": {},
       "advice": {"good": texts.SCORE_ADVICE_EXCELLENT, "warning": texts.SCORE_ADVICE_GOOD, "critical": texts.SCORE_ADVICE_LOW}}

# у режима «Старт, день 2» своя дата: вторник 06.10 — план ещё можно править (до 07.10)
MODE_TODAY = {"day2": date(2026, 10, 6)}

for mode in ("before", "first", "day2", "mid", "w12"):
    db = Path(tempfile.mkdtemp()) / "bot.db"; run_migrations(db)
    m_today = MODE_TODAY.get(mode, today); m_week = m_today - timedelta(days=m_today.weekday())
    start = {"mid": m_week - timedelta(days=14), "first": m_week, "w12": m_week - timedelta(weeks=11)}.get(mode, date(2026, 10, 5))
    c = sqlite3.connect(db)
    c.executescript(f"""
    INSERT INTO users (id, telegram_id, first_name, onboarding_step, is_ready, send_report, cycle, cycle_start) VALUES (1, 1000001, 'Демо', 'DONE', 1, 'TEAM', 1, '{start}');
    INSERT INTO priorities (id, user_id, cycle, position, title) VALUES
     (1,1,1,1,'Выучить английский'),(2,1,1,2,'Бегать по утрам'),(3,1,1,3,'Проект по репетиторству');
    INSERT INTO essential_intent (priority_id, text) VALUES
     (1,'Хочу свободно проводить встречи с иностранными партнёрами и не бояться звонков.'),
     (2,'Больше энергии днём и спокойная голова.'),
     (3,'Собственный доход и дело, которое мне нравится.');
    INSERT INTO wheel_of_balance (user_id, cycle, sphere, score) VALUES
     (1,1,'health',5),(1,1,'career',6),(1,1,'finance',5),(1,1,'relationships',8),(1,1,'growth',4),(1,1,'rest',5),(1,1,'friends',7),(1,1,'spirit',6);
    INSERT INTO weekly_tactics (id, priority_id, user_id, text, weeks, days, is_active) VALUES
     (1,1,1,'Урок английского 45 минут',NULL,'[1, 3, 5]',1),
     (2,1,1,'Пробный тест IELTS','[4, 8, 12]',NULL,1),
     (3,2,1,'Пробежка 5 км',NULL,'[0, 2, 4]',1),
     (4,3,1,'Найти 2 учеников','[1, 2, 3]',NULL,1),
     (5,3,1,'Пост о занятиях в соцсетях',NULL,NULL,1);
    """)
    if mode == "mid":
        w1, w2 = start, start + timedelta(days=7)
        c.executescript(f"""
        INSERT INTO checkins (user_id, tactic_id, week_start, week_number, done) VALUES
         (1,1,'{w1}',1,1),(1,3,'{w1}',1,1),(1,4,'{w1}',1,1),(1,5,'{w1}',1,1),
         (1,1,'{w2}',2,1),(1,3,'{w2}',2,0),(1,4,'{w2}',2,1),(1,5,'{w2}',2,1);
        """)
    if mode == "w12":  # недели 1–11 отмечены: пара просадок, чтобы история и рефлексия выглядели живыми
        in_week = {1: None, 2: [4, 8, 12], 3: None, 4: [1, 2, 3], 5: None}
        missed = {(5, 3), (5, 6), (5, 9), (3, 5), (3, 6), (1, 10)}
        for n in range(1, 12):
            ws = (start + timedelta(weeks=n - 1)).isoformat()
            for tid, weeks in in_week.items():
                if weeks is None or n in weeks:
                    c.execute("INSERT INTO checkins (user_id, tactic_id, week_start, week_number, done) VALUES (1, ?, ?, ?, ?)",
                              (tid, ws, n, 0 if (tid, n) in missed else 1))
        c.execute("UPDATE users SET last_reported_week = ?", ((start + timedelta(weeks=10)).isoformat(),))
    if mode in ("mid", "w12"):  # видение заполнено частично — как бывает на практике
        c.execute("INSERT INTO visions (user_id, work, life, me, main) VALUES (1, ?, '', ?, ?)",
                  ("Веду свои занятия по английскому онлайн, доход стабильный", "Бегаю по утрам, свободно говорю по-английски",
                   "Спокойствие: сама выбираю, чем заниматься"))
    if mode != "before":  # во вторник и четверг урок уже отмечен, пробежки — все три
        for tid, wd in [(1, 1), (1, 3), (3, 0), (3, 2), (3, 4)]:
            if wd < m_today.weekday():  # только прошедшие дни недели
                c.execute("INSERT INTO daily_marks (user_id, tactic_id, day) VALUES (1, ?, ?)", (tid, (m_week + timedelta(days=wd)).isoformat()))
    c.commit(); c.close()
    app = appmod.create_app(create_sessionmaker(create_engine(f"sqlite+aiosqlite:///{db}")),
                     Settings(bot_token=TOKEN, database_path=db, admin_ids=[]), None)
    with patch.object(appmod, "local_today", lambda _s: m_today), TestClient(app) as cl:
        h = {"Authorization": "tma " + sign_init_data({"id": 1000001, "first_name": "Демо"})}
        get = lambda p: (lambda r: (r.raise_for_status(), r.json())[1])(cl.get(p, headers=h))
        m = {k: get("/api/" + k) for k in ("me", "plan", "scorecard", "checkin", "today", "wheel", "intent", "vision", "team")}
        m["states"] = {}
        t = m["today"]
        if t["status"] == "active":
            actions = [("daily", i["id"]) for i in t["today_items"]] + [("week", i["id"]) for i in t["week_items"]]
            def toggle(kind, tid, on):
                body = {"tactic_id": tid, "done": on} if kind == "daily" else {"tactic_id": tid, "done": True if on else None}
                cl.post(f"/api/today/{kind}", json=body, headers=h).raise_for_status()
            for r in range(len(actions) + 1):
                for combo in itertools.combinations(actions, r):
                    for a in combo: toggle(*a, True)
                    key = ",".join(f"{k}{i}" for k, i in sorted(combo))
                    m["states"][key] = {"today": get("/api/today"), "checkin": get("/api/checkin")}
                    for a in combo: toggle(*a, False)
        out["modes"][mode] = m
    print(mode, "ok", list(m["states"]))
Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1))
