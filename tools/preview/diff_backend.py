"""Сверка demo-backend.js с настоящим API: одинаковый сценарий → одинаковые ответы (статус + JSON)."""
import json, subprocess, sys, tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient
from bot.config import Settings
from bot.db import create_engine, create_sessionmaker
from bot.migrate import run_migrations
from tests.webapp_helpers import TOKEN, sign_init_data
import webapp.app as appmod

HERE = Path(__file__).parent
TODAY, COHORT = date(2026, 9, 26), date(2026, 10, 5)
S = [
 ("GET", "/api/me", None), ("GET", "/api/wheel", None), ("GET", "/api/explore", None),
 ("POST", "/api/explore", {"text": "рано"}),
 ("PUT", "/api/wheel", {"scores": {"career": 7, "health": 4}}),
 ("PUT", "/api/wheel", {"scores": {"career": 7, "health": 4, "relationships": 8, "finance": 5, "growth": 6, "rest": 11}}),
 ("PUT", "/api/wheel", {"scores": {"career": 7, "health": 4, "relationships": 8, "finance": 5, "growth": 6, "rest": 3, "home": 6, "spirit": 2, "friends": 5}}),
 ("PUT", "/api/wheel", {"scores": {"career": 7, "health": 4, "relationships": 8, "finance": 5, "growth": 6, "rest": 3, "home": 6, "spirit": 2}}),
 ("PUT", "/api/wheel", {"scores": {"career": 7, "health": 9, "relationships": 8, "finance": 6, "growth": 6, "rest": 7, "home": 6}}),
 ("GET", "/api/wheel", None),
 ("POST", "/api/explore/done", None),
 ("POST", "/api/explore", {"text": "   \n  - \n"}),
 ("POST", "/api/explore", {"text": "- Найти стажировку\n2) Запустить сайт\n• найти стажировку\n\n* Бегать 3 раза в неделю"}),
 ("PUT", "/api/explore/2", {"text": "  Запустить сайт-визитку  "}),
 ("PUT", "/api/explore/99", {"text": "x"}),
 ("PUT", "/api/explore/2", {"text": "   "}),
 ("POST", "/api/explore", {"text": "Увеличить доход"}),
 ("DELETE", "/api/explore/4", None),
 ("POST", "/api/explore", {"text": "Выучить английский\nПереехать"}),
 ("PUT", "/api/eliminate", {"selected": [1, 2, 3]}),
 ("POST", "/api/explore/done", None),
 ("PUT", "/api/eliminate", {"selected": [1, 2]}),
 ("PUT", "/api/eliminate", {"selected": [1, 2, 99]}),
 ("PUT", "/api/eliminate", {"selected": [3, 1, 4]}),
 ("PUT", "/api/eliminate", {"selected": [1, 3, 4]}),
 ("GET", "/api/intent", None),
 ("PUT", "/api/intent", {"intents": {"1": "коротко", "2": "Хочу стабильный доход, чтобы меньше зависеть от работы", "3": "Больше энергии и спокойная голова"}}),
 ("PUT", "/api/intent", {"intents": {"1": "Хочу стажировку в хорошей компании к лету", "2": "Хочу стабильный доход, чтобы меньше зависеть от работы"}}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Разослать 5 резюме", "days": [0]}),
 ("PUT", "/api/intent", {"intents": {"1": "Хочу стажировку в хорошей компании к лету", "2": "Хочу стабильный доход, чтобы меньше зависеть от работы", "3": "  Больше энергии и спокойная голова  "}}),
 ("GET", "/api/plan", None),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Разослать 5 резюме"}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Разослать 5 резюме", "days": []}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Разослать 5 резюме", "days": [4, 0, 2, 2]}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Пробное собеседование", "weeks": [4, 8, 12, 8]}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Пробное собеседование", "weeks": []}),
 ("POST", "/api/tactics", {"priority_id": 1, "text": "Пробное собеседование", "weeks": [0, 13]}),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "Страница сайта", "weeks": list(range(1, 13))}),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "Страница сайта", "weeks": list(range(1, 13)), "days": [0, 1, 2, 3, 4, 5, 6]}),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "Созвон с дизайнером", "weeks": [1, 2, 3, 5, 7, 8]}),
 ("POST", "/api/tactics", {"priority_id": 99, "text": "x", "days": [1]}),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "   ", "days": [1]}),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "я" * 201, "days": [1]}),
 ("PUT", "/api/tactics/1", {"text": "Разослать 3 резюме", "days": [1, 3]}),
 ("PUT", "/api/tactics/77", {"text": "x", "days": [1]}),
 ("DELETE", "/api/tactics/3", None),
 ("PUT", "/api/priorities/2", {"title": "  Запустить сайт  ", "intent": "коротко"}),
 ("PUT", "/api/priorities/2", {"title": "", "intent": "Хочу показывать работы клиентам"}),
 ("PUT", "/api/priorities/2", {"title": "Запустить сайт", "intent": "Хочу показывать работы клиентам"}),
 ("POST", "/api/plan/confirm", None),
 ("POST", "/api/tactics", {"priority_id": 3, "text": "Бег по утрам", "days": [0, 2, 4]}),
 ("GET", "/api/today", None),
 ("POST", "/api/plan/confirm", None),
 ("GET", "/api/me", None), ("GET", "/api/plan", None), ("GET", "/api/today", None), ("GET", "/api/scorecard", None),
 ("GET", "/api/checkin", None),
 ("POST", "/api/today/daily", {"tactic_id": 1, "done": True}),
 ("POST", "/api/today/week", {"tactic_id": 1, "done": True}),
 ("PUT", "/api/wheel", {"scores": {"career": 7}}),
 ("POST", "/api/explore", {"text": "ещё"}),
 ("POST", "/api/tactics", {"priority_id": 3, "text": "Растяжка 10 минут", "weeks": [2]}),
 ("DELETE", "/api/tactics/6", None),
 ("DELETE", "/api/tactics/4", None),
 ("PUT", "/api/intent", {"intents": {"1": "Хочу стажировку в хорошей компании к лету", "2": "Хочу показывать работы клиентам", "3": "Больше энергии и спокойная голова"}}),
 ("POST", "/api/plan/reselect", None),
 ("GET", "/api/me", None),
 ("PUT", "/api/eliminate", {"selected": [1, 5, 6]}),
 ("GET", "/api/plan", None),
 ("GET", "/api/intent", None),
 ("PUT", "/api/eliminate", {"selected": [1, 4, 5]}),
 ("GET", "/api/plan", None), ("GET", "/api/me", None), ("GET", "/api/intent", None),
 ("PUT", "/api/intent", {"intents": {"1": "Хочу стажировку в хорошей компании к лету", "2": "Свободно говорить на встречах", "3": "Жить ближе к семье и морю"}}),
 ("GET", "/api/me", None),
 ("POST", "/api/tactics", {"priority_id": 2, "text": "Урок 45 минут", "days": [1, 3, 5]}),
 ("POST", "/api/tactics", {"priority_id": 3, "text": "Посмотреть 3 квартиры", "weeks": [6]}),
 ("POST", "/api/plan/confirm", None), ("GET", "/api/today", None), ("GET", "/api/scorecard", None),
]

db = Path(tempfile.mkdtemp()) / "bot.db"; run_migrations(db)
real = []
with patch.object(appmod, "local_today", lambda s: TODAY):
    app = appmod.create_app(create_sessionmaker(create_engine(f"sqlite+aiosqlite:///{db}")),
                            Settings(bot_token=TOKEN, database_path=db, admin_ids=[], cycle_start=COHORT), None)
    with TestClient(app) as cl:
        h = {"Authorization": "tma " + sign_init_data({"id": 1000001, "first_name": "Демо"})}
        for m, p, b in S:
            r = cl.request(m, p, headers=h, json=b)
            real.append({"status": r.status_code, "data": r.json()})

js = f"""
const B = require({json.dumps(str(HERE / 'demo-backend.js'))});
const b = new B({{ today: "{TODAY}", cohortStart: "{COHORT}" }});
const S = {json.dumps(S, ensure_ascii=False)};
console.log(JSON.stringify(S.map(([m, p, body]) => b.handle(m, p, body ? JSON.parse(JSON.stringify(body)) : null))));
"""
demo = json.loads(subprocess.check_output(["node", "-e", js], text=True))
bad = 0
for (m, p, b), r, d in zip(S, real, demo):
    if r != d:
        bad += 1
        print("MISMATCH", m, p, json.dumps(b, ensure_ascii=False)[:80])
        print("  real:", json.dumps(r, ensure_ascii=False)[:600])
        print("  demo:", json.dumps(d, ensure_ascii=False)[:600])
print(f"{len(S)} запросов, расхождений: {bad}")
# print("ответы:", [(r["status"], r["data"].get("detail") if isinstance(r["data"], dict) else None) for r in real])
# print("последний план:", json.dumps(real[-2]["data"], ensure_ascii=False)[:500])
