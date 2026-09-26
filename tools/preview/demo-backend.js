/* Демо-бэкенд для режима «Новая участница»: повторяет ответы webapp/app.py для онбординга
   (колесо → выгрузка → выбор 3 → «зачем» → действия → подтверждение) и экрана «до старта».
   Состояние живёт только в памяти вкладки. Сверяется с настоящим API тестом diff_backend.py. */
(function (root) {
  "use strict";

  const CORE = [["career", "Карьера", "💼"], ["health", "Здоровье", "💪"], ["relationships", "Отношения", "❤️"],
    ["finance", "Финансы", "💰"], ["growth", "Саморазвитие", "📚"], ["rest", "Отдых", "🌴"]];
  const EXTRA = [["friends", "Окружение", "👭"], ["creativity", "Творчество", "🎨"], ["spirit", "Духовность", "🕊"], ["home", "Дом и быт", "🏡"]];
  const SPHERES = CORE.concat(EXTRA);
  const MAX_EXTRA = 2, LOW = 5, PICK = 3, MIN_ITEMS = 3, MAX_ITEMS = 40, MAX_ITEM_LEN = 200, MIN_INTENT = 15;
  const MAX_TACTICS = 8, MAX_TACTIC_LEN = 200, WEEKS = 12;
  const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
  const BULLET = /^\s*(?:[-–—•*·]+|\d+[.)])\s*/;
  const MEASURABLE = /\d|раз|кажд|ежедн|ежеднев|минут|час|страниц|шаг|км|километр|тренировк|сесси|урок/i;

  // ---------- даты (ISO yyyy-mm-dd, без часовых поясов) ----------
  const toDate = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)); };
  const toIso = (dt) => dt.toISOString().slice(0, 10);
  const addDays = (iso, n) => { const d = toDate(iso); d.setUTCDate(d.getUTCDate() + n); return toIso(d); };
  const weekday = (iso) => (toDate(iso).getUTCDay() + 6) % 7; // 0 = понедельник
  const daysBetween = (a, b) => Math.round((toDate(b) - toDate(a)) / 86400000);
  const weekStart = (iso) => addDays(iso, -weekday(iso));

  const len = (s) => Array.from(s).length; // как len() в Python — по символам
  const strip = (s) => s.replace(/^\s+|\s+$/g, "");
  const err = (status, detail) => ({ status, data: { detail } });
  const ok = (data) => ({ status: 200, data });

  function weeksLabel(weeks, days) {
    if (weeks == null) {
      if (days && days.length === 7) return "каждый день";
      return "каждую неделю" + (days && days.length ? " · " + (days.length === 7 ? "каждый день" : days.map((d) => WEEKDAYS[d]).join(", ")) : "");
    }
    if (weeks.length === 1) return "неделя " + weeks[0];
    const parts = [];
    let start = weeks[0];
    for (let i = 0; i < weeks.length; i++) {
      const prev = weeks[i], cur = weeks[i + 1];
      if (cur !== prev + 1) { parts.push(start === prev ? String(start) : start + "–" + prev); start = cur; }
    }
    return "недели " + parts.join(", ");
  }

  const uniqSorted = (xs) => Array.from(new Set(xs)).sort((a, b) => a - b);

  function DemoBackend(opts) {
    this.today = opts.today;                 // «сегодня» демо
    this.cohortStart = opts.cohortStart;     // общая дата старта сообщества
    this.firstName = opts.firstName || "Демо";
    this.step = "wheel";
    this.cycleStart = null;
    this.scores = {};
    this.items = [];       // {id, text, selected}
    this.priorities = [];  // {id, position, title, intent|null, item_id}
    this.tactics = [];     // {id, priority_id, text, weeks, days}
    this.team = null;
  }

  const nextId = (rows) => rows.reduce((m, r) => Math.max(m, r.id), 0) + 1; // как SQLite rowid

  DemoBackend.prototype = {
    // ---------- вспомогательное ----------
    beforeStart() { return this.step === "done" && this.cycleStart != null && this.today < this.cycleStart; },
    requireStep(...steps) { if (!steps.includes(this.step)) throw err(409, "wrong_step"); },
    requireEditable(...steps) { if (!steps.includes(this.step) && !this.beforeStart()) throw err(409, "plan_locked"); },
    cycleStartFor() {
      const nearest = addDays(this.today, (7 - weekday(this.today)) % 7);
      return this.cohortStart && this.cohortStart > nearest ? this.cohortStart : nearest;
    },
    ordered() { return SPHERES.filter(([k]) => k in this.scores).map(([k, t, e]) => [k, t, e, this.scores[k]]); },
    lows() {
      const items = this.ordered();
      if (!items.length) return [];
      let lows = items.filter((s) => s[3] <= LOW);
      if (!lows.length) { const min = Math.min(...items.map((s) => s[3])); lows = items.filter((s) => s[3] === min); }
      return lows.slice().sort((a, b) => a[3] - b[3]);
    },
    activeTactics() {
      const pos = (t) => this.priorities.find((p) => p.id === t.priority_id).position;
      return this.tactics.slice().sort((a, b) => pos(a) - pos(b) || a.id - b.id);
    },
    inWeek: (t, n) => t.weeks == null || t.weeks.includes(n),
    item(t) {
      const p = this.priorities.find((x) => x.id === t.priority_id);
      return { id: t.id, text: t.text, label: weeksLabel(t.weeks, t.days), priority_position: p.position, priority_title: p.title };
    },
    deletePriorities() {
      const ids = new Set(this.priorities.map((p) => p.id));
      this.tactics = this.tactics.filter((t) => !ids.has(t.priority_id)); // каскад
      this.priorities = [];
    },

    // ---------- ответы ----------
    wheelPayload(scores) {
      const vals = Object.values(scores);
      return {
        spheres: SPHERES.map(([key, title, emoji]) => ({ key, title, emoji, core: CORE.some((c) => c[0] === key) })),
        max_extra: MAX_EXTRA, low_threshold: LOW, scores,
        average: vals.length ? Math.round((vals.reduce((a, b) => a + b, 0) / vals.length) * 10) / 10 : null,
        lows: this.lows().map((s) => s[0]),
        comparison: [],
      };
    },
    explorePayload() {
      return {
        items: this.items.map((i) => ({ id: i.id, text: i.text, selected: i.selected })),
        min: MIN_ITEMS, max: MAX_ITEMS, pick: PICK,
        lows: this.lows().map((s) => s[2] + " " + s[1]),
        previous: [],
      };
    },
    intentPayload() {
      return {
        priorities: this.priorities.map((p) => ({ id: p.id, position: p.position, title: p.title, intent: p.intent || "" })),
        not_now: this.items.filter((i) => !i.selected).map((i) => i.text),
        min_len: MIN_INTENT,
      };
    },
    planPayload() {
      const all = [];
      const out = this.priorities.map((p) => {
        const active = this.tactics.filter((t) => t.priority_id === p.id).sort((a, b) => a.id - b.id);
        all.push(...active);
        return {
          id: p.id, position: p.position, title: p.title, intent: p.intent || "",
          tactics: active.map((t) => ({
            id: t.id, text: t.text, weeks: t.weeks, days: t.days, label: weeksLabel(t.weeks, t.days),
            measurable: t.weeks != null || MEASURABLE.test(t.text),
          })),
        };
      });
      const load = [];
      for (let n = 1; n <= WEEKS; n++) load.push(all.filter((t) => this.inWeek(t, n)).length);
      return {
        step: this.step, priorities: out, load, max_per_priority: MAX_TACTICS, recommended: [3, 8], weeks_total: WEEKS,
        cycle_start: this.cycleStart || this.cycleStartFor(), team: this.team,
        editable: this.step === "tactics" || this.beforeStart(),
      };
    },
    cleanTactic(body) {
      const text = strip(body.text || "");
      if (!text) throw err(422, "empty");
      if (len(text) > MAX_TACTIC_LEN) throw err(422, "too_long");
      let weeks = body.weeks == null ? null : uniqSorted(body.weeks);
      if (weeks != null) {
        if (!weeks.length || weeks.some((w) => w < 1 || w > WEEKS)) throw err(422, "bad_weeks");
        if (weeks.length === WEEKS) weeks = null;
      }
      if (weeks != null) return [text, weeks, null];
      if (body.days == null) throw err(422, "pick_days");
      const days = uniqSorted(body.days);
      if (!days.length || days.some((d) => d < 0 || d > 6)) throw err(422, "bad_days");
      return [text, null, days];
    },
    ownPriority(id) { const p = this.priorities.find((x) => x.id === id); if (!p) throw err(404, "no_priority"); return p; },
    ownTactic(id) { const t = this.tactics.find((x) => x.id === id); if (!t) throw err(404, "no_tactic"); return t; },

    // ---------- маршруты ----------
    handle(method, path, body) {
      try { return this.route(method, path.replace(/^\/api\//, ""), body || {}); }
      catch (e) { if (e && e.status) return e; throw e; }
    },
    route(method, p, body) {
      const m = (re) => p.match(re);
      let id;
      if (method === "GET" && p === "me") return ok({ first_name: this.firstName, step: this.step, cycle: 1, is_admin: false });

      if (p === "wheel") {
        if (method === "GET") return ok(this.wheelPayload(this.scores));
        if (!["wheel", "explore"].includes(this.step)) throw err(409, "wheel_locked");
        const scores = body.scores || {};
        const keys = Object.keys(scores);
        const unknown = keys.some((k) => !SPHERES.some((s) => s[0] === k));
        const missing = CORE.some(([k]) => !(k in scores));
        const extras = keys.filter((k) => !CORE.some((c) => c[0] === k));
        if (unknown || missing || extras.length > MAX_EXTRA) throw err(422, "bad_spheres");
        if (Object.values(scores).some((v) => v < 1 || v > 10)) throw err(422, "bad_score");
        this.scores = Object.assign({}, scores);
        this.step = "explore";
        return ok(this.wheelPayload(scores));
      }

      if (p === "explore" && method === "GET") return ok(this.explorePayload());
      if (p === "explore" && method === "POST") {
        this.requireStep("explore", "eliminate");
        const texts = String(body.text || "").split(/\r\n|\r|\n/).map((l) => strip(l.replace(BULLET, ""))).filter(Boolean)
          .map((l) => Array.from(l).slice(0, MAX_ITEM_LEN).join(""));
        if (!texts.length) throw err(422, "empty");
        if (this.items.length >= MAX_ITEMS) throw err(422, "limit");
        const seen = new Set(this.items.map((i) => i.text.toLowerCase()));
        let added = 0;
        for (const t of texts) {
          if (this.items.length >= MAX_ITEMS) break;
          if (seen.has(t.toLowerCase())) continue;
          seen.add(t.toLowerCase());
          this.items.push({ id: nextId(this.items), text: t, selected: false });
          added++;
        }
        return ok(Object.assign(this.explorePayload(), { added }));
      }
      if (p === "explore/done" && method === "POST") {
        this.requireStep("explore", "eliminate");
        if (this.items.length < MIN_ITEMS) throw err(422, "not_enough");
        this.step = "eliminate";
        return ok(this.explorePayload());
      }
      if ((id = m(/^explore\/(\d+)$/))) {
        this.requireStep("explore", "eliminate");
        const itemId = Number(id[1]);
        if (method === "DELETE") { this.items = this.items.filter((i) => i.id !== itemId); return ok(this.explorePayload()); }
        const text = Array.from(strip(body.text || "")).slice(0, MAX_ITEM_LEN).join("");
        if (!text) throw err(422, "empty");
        const item = this.items.find((i) => i.id === itemId);
        if (!item) throw err(404, "no_item");
        item.text = text;
        return ok(this.explorePayload());
      }

      if (p === "eliminate" && method === "PUT") {
        this.requireStep("eliminate", "intent");
        const wanted = new Set(body.selected || []);
        if (wanted.size !== PICK || [...wanted].some((w) => !this.items.some((i) => i.id === w))) throw err(422, "pick_exactly_3");
        this.items.forEach((i) => { i.selected = wanted.has(i.id); });
        this.deletePriorities();
        this.items.filter((i) => i.selected).forEach((i, n) => {
          this.priorities.push({ id: nextId(this.priorities), position: n + 1, title: i.text, intent: null, item_id: i.id });
        });
        this.step = "intent";
        return ok(this.intentPayload());
      }

      if (p === "intent") {
        if (method === "GET") return ok(this.intentPayload());
        this.requireEditable("intent", "tactics");
        const intents = body.intents || {};
        const texts = {};
        Object.keys(intents).forEach((k) => { texts[Number(k)] = strip(intents[k]); });
        const keys = Object.keys(texts).map(Number).sort((a, b) => a - b);
        const ids = this.priorities.map((x) => x.id).sort((a, b) => a - b);
        if (keys.join() !== ids.join() || Object.values(texts).some((t) => len(t) < MIN_INTENT)) throw err(422, "intent_required");
        this.priorities.forEach((x) => { x.intent = texts[x.id]; });
        if (this.step === "intent") this.step = "tactics";
        return ok(this.intentPayload());
      }

      if ((id = m(/^priorities\/(\d+)$/)) && method === "PUT") {
        this.requireEditable("tactics");
        const pr = this.ownPriority(Number(id[1]));
        const title = strip(body.title || ""), intent = strip(body.intent || "");
        if (!title || len(title) > MAX_ITEM_LEN) throw err(422, "bad_title");
        if (len(intent) < MIN_INTENT) throw err(422, "intent_required");
        pr.title = title; pr.intent = intent;
        return ok(this.planPayload());
      }
      if (p === "plan/reselect" && method === "POST") {
        this.requireEditable("intent", "tactics");
        this.step = "eliminate";
        return ok(this.explorePayload());
      }
      if (p === "plan" && method === "GET") return ok(this.planPayload());
      if (p === "tactics" && method === "POST") {
        this.requireEditable("tactics");
        const pr = this.ownPriority(body.priority_id || 0);
        const [text, weeks, days] = this.cleanTactic(body);
        if (this.tactics.filter((t) => t.priority_id === pr.id).length >= MAX_TACTICS) throw err(422, "limit");
        this.tactics.push({ id: nextId(this.tactics), priority_id: pr.id, text, weeks, days });
        return ok(this.planPayload());
      }
      if ((id = m(/^tactics\/(\d+)$/))) {
        this.requireEditable("tactics");
        const t = this.ownTactic(Number(id[1]));
        if (method === "PUT") { [t.text, t.weeks, t.days] = this.cleanTactic(body); return ok(this.planPayload()); }
        if (this.step === "done" && this.tactics.filter((x) => x.priority_id === t.priority_id).length <= 1) throw err(422, "last_tactic");
        this.tactics = this.tactics.filter((x) => x !== t);
        return ok(this.planPayload());
      }
      if (p === "plan/confirm" && method === "POST") {
        this.requireStep("tactics");
        if (this.priorities.some((x) => !this.tactics.some((t) => t.priority_id === x.id))) throw err(422, "empty_priority");
        this.step = "done";
        this.cycleStart = this.cycleStartFor();
        this.team = this.team || "Команда №1";
        return ok(this.planPayload());
      }

      // ---------- после подтверждения: цикл ещё не начался ----------
      const CHECKIN = ["done", "finished"];
      if (p === "today" && method === "GET") {
        this.requireStep(...CHECKIN);
        const all = this.activeTactics();
        const base = {
          today: this.today, weekday: weekday(this.today), cycle_start: this.cycleStart,
          priorities: new Set(all.map((t) => t.priority_id)).size, tactics: all.length, editable: this.beforeStart(),
        };
        if (this.today < this.cycleStart) {
          return ok(Object.assign(base, { status: "not_started", days_until: daysBetween(this.today, this.cycleStart),
            week1: all.filter((t) => this.inWeek(t, 1)).map((t) => this.item(t)) }));
        }
        return err(501, "demo_not_supported");
      }
      if (p === "today/daily" && method === "POST") { this.requireStep(...CHECKIN); this.ownTactic(body.tactic_id); throw err(422, "not_today"); }
      if (p === "today/week" && method === "POST") { this.requireStep(...CHECKIN); this.ownTactic(body.tactic_id); throw err(422, "not_this_week"); }
      if (p === "checkin" && method === "GET") {
        this.requireStep(...CHECKIN);
        return ok({ status: "not_started", cycle_start: this.cycleStart });
      }
      if (p === "scorecard" && method === "GET") {
        this.requireStep(...CHECKIN);
        const all = this.activeTactics();
        const weeks = [];
        for (let n = 1; n <= WEEKS; n++) {
          const ws = addDays(this.cycleStart, 7 * (n - 1));
          weeks.push({ n, week_start: ws, planned: all.filter((t) => this.inWeek(t, n)).length, percent: null, level: null,
            future: ws > weekStart(this.today) });
        }
        return ok({ cycle_start: this.cycleStart, current_week: null, weeks, average: null, average_level: null, done_total: 0,
          thresholds: { good: 85, warning: 70 } });
      }
      return err(404, "not_found");
    },
  };

  root.DemoBackend = DemoBackend;
  if (typeof module !== "undefined") module.exports = DemoBackend;
})(typeof window !== "undefined" ? window : globalThis);
