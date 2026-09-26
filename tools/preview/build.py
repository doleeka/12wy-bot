import json, subprocess, sys
from pathlib import Path
R = Path(__file__).resolve().parents[2]; P = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else P
commit = subprocess.check_output(["git", "-C", str(R), "rev-parse", "--short", "HEAD"], text=True).strip()
fx = json.loads((OUT / "fixtures.json").read_text())
index = (R / "webapp/static/index.html").read_text()
css = (R / "webapp/static/style.css").read_text()
js = (R / "webapp/static/app.js").read_text()

MOCK = r"""
(function(){
 const CFG = parent.__demoCfg; const host = parent.__tgHost; const M = CFG.fixtures;
 const deep = (o) => JSON.parse(JSON.stringify(o));
 let mainHandlers = [], backHandlers = [];
 const MB = { text:'', isVisible:false, isActive:true, color:null, progress:false,
   _r(){ host.main({ text:this.text, visible:this.isVisible, active:this.isActive, color:this.color, progress:this.progress }); },
   setText(t){ this.text=t; this._r(); }, show(){ this.isVisible=true; this._r(); }, hide(){ this.isVisible=false; this._r(); },
   enable(){ this.isActive=true; this._r(); }, disable(){ this.isActive=false; this._r(); },
   setParams(p){ if ('is_active' in p) this.isActive=p.is_active; if (p.color) this.color=p.color; this._r(); },
   showProgress(){ this.progress=true; this._r(); }, hideProgress(){ this.progress=false; this._r(); },
   onClick(f){ mainHandlers.push(f); }, offClick(f){ mainHandlers = mainHandlers.filter(h => h !== f); } };
 const BB = { show(){ host.back(true); }, hide(){ host.back(false); },
   onClick(f){ backHandlers.push(f); }, offClick(f){ backHandlers = backHandlers.filter(h => h !== f); } };
 window.__demoMain = () => { if (MB.isActive && !MB.progress) mainHandlers.slice().forEach(h => h()); };
 window.__demoBack = () => backHandlers.slice().forEach(h => h());
 const noop = () => {};
 window.Telegram = { WebApp: {
   initData: 'demo', initDataUnsafe: { user: { id: 1000001, first_name: 'Демо' } }, colorScheme: CFG.theme,
   MainButton: MB, BackButton: BB, HapticFeedback: { selectionChanged: noop, notificationOccurred: noop, impactOccurred: noop },
   ready: noop, expand: noop, onEvent: noop, setHeaderColor: noop, setBackgroundColor: noop, setBottomBarColor: noop,
   close(){ host.alert('В Telegram здесь Mini App закроется.'); },
   showAlert(m, cb){ host.alert(m, cb); }, showConfirm(m, cb){ host.confirm(m, cb); } } };

 // ---------- демо-бэкенд ----------
 // «Новая участница»: stateful-бэкенд (demo-backend.js) живёт в родительской странице — переживает смену темы/размера.
 // Остальные режимы: ответы записаны с настоящего API на вымышленных данных.
 const NEW = CFG.mode === 'new';
 const on = new Set(); let saved = null;  // saved — отметки чек-ина, сделанного в превью
 const key = () => Array.from(on).sort().join(',');
 const snap = (name) => { const s = M.states && M.states[key()]; return deep(s ? s[name] : M[name]); };
 const level = (v) => v >= 85 ? 'good' : v >= 70 ? 'warning' : 'critical';
 const rating = { good: 'по плану', warning: 'почти по плану', critical: 'стоит пересмотреть план' };
 const ok = (data) => ({ status: 200, data });
 const fail = (status, detail) => ({ status, data: { detail } });
 function today() {
   const t = snap('today');
   if (saved && t.status === 'active') {  // как на сервере: после чек-ина итог недели — по нему
     const all = t.today_items.concat(t.week_items);
     all.forEach(i => { i.week_done = saved[i.id]; });
     t.week_items.forEach(i => { i.done = saved[i.id]; });
     const done = Object.values(saved).filter(Boolean).length;
     t.week_progress = { done, planned: t.week_progress.planned, percent: Math.round(done / t.week_progress.planned * 100) };
     t.checkin_done = true;
   }
   return t;
 }
 function result(marks) {
   const vals = Object.values(marks), done = vals.filter(Boolean).length, v = Math.round(done / vals.length * 100), l = level(v);
   return { percent: v, level: l, rating: rating[l], done, planned: vals.length, advice: CFG.advice[l] };
 }
 function checkin() {
   const c = snap('checkin');
   if (saved && c.status === 'active') { c.tactics.forEach(t => { t.done = saved[t.id]; }); c.result = result(saved); }
   return c;
 }
 function scorecard() {
   const s = deep(M.scorecard);
   if (saved) {
     const r = result(saved), w = s.weeks.find(w => w.n === s.current_week);
     w.percent = r.percent; w.level = r.level; s.done_total += r.done;
     const m = s.weeks.filter(w => w.percent != null).map(w => w.percent);
     s.average = Math.round(m.reduce((a, b) => a + b, 0) / m.length); s.average_level = level(s.average);
   }
   return s;
 }
 function route(method, path, body) {
   if (NEW) return JSON.parse(JSON.stringify(parent.__demoNew.handle(method, path, body)));
   const p = path.replace(/^\/api\//, '');
   if (method === 'GET') {
     if (p === 'today') return ok(today());
     if (p === 'checkin') return ok(checkin());
     if (p === 'scorecard') return ok(scorecard());
     if (p in M) return ok(deep(M[p]));
     return fail(404, 'not_found');
   }
   if (p === 'today/daily' || p === 'today/week') {
     if (saved) return fail(409, 'week_checked');
     const k = (p === 'today/daily' ? 'daily' : 'week') + body.tactic_id;
     body.done ? on.add(k) : on.delete(k);
     return ok(today());
   }
   if (p === 'checkin' && method === 'PUT') { saved = Object.assign({}, body.marks); return ok(result(saved)); }
   host.alert('Демо-превью: в этом режиме правки плана не сохраняются. Попробовать редактирование можно в режиме «Новая участница».');
   return ok(deep(M.plan));
 }
 window.fetch = async (path, opts) => {
   opts = opts || {};
   const body = opts.body ? JSON.parse(opts.body) : null;
   await new Promise(r => setTimeout(r, 120));
   const r = route(opts.method || 'GET', path, body);
   return new Response(JSON.stringify(r.data), { status: r.status, headers: { 'Content-Type': 'application/json' } });
 };
})();
"""

inner = index.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', "<script>__MOCK__</script>")
inner = inner.replace('<link rel="stylesheet" href="/static/style.css">', "<style>" + css + "</style>")
inner = inner.replace('<script src="/static/app.js"></script>', "<script>" + js + "</script>")
assert "__MOCK__" in inner and "app.js" not in inner and "/static/style.css" not in inner
inner = inner.replace("__MOCK__", MOCK)

host = (P / "host.html").read_text()
data = {"inner": inner, "cohort": "2026-10-05", "modes": fx["modes"], "advice": fx["advice"], "today": fx["today"], "commit": commit}
payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
backend = (P / "demo-backend.js").read_text()
assert "</" not in backend
out = host.replace("__BACKEND__", backend).replace("__DATA__", payload).replace("__COMMIT__", commit)
(OUT / "preview-12-weeks.html").write_text(out)
print(len(out), "bytes, commit", commit)
