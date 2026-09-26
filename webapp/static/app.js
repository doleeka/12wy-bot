/* Mini App «12 недель». Чистый JS, без сборки. */
(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (sel) => document.querySelector(sel);
  const state = {
    me: null, wheel: null, scores: {}, touched: new Set(), extras: [],
    explore: null, picked: new Set(), intent: null, drafts: {}, plan: null, edit: null, checkin: null, marks: {}, editPriority: null,
  };
  let mainHandler = null;
  let backHandler = null;

  // ---------- Telegram ----------

  // Цвета — свои (бордовый бренд), от Telegram берём только светлую/тёмную схему
  const BRAND = { light: { bg: "#FCFAF9", button: "#7A1F2B", off: "#B9B1AE" }, dark: { bg: "#1C1718", button: "#8E2433", off: "#4A4142" } };
  const scheme = () => (document.documentElement.dataset.theme === "dark" ? BRAND.dark : BRAND.light);

  function applyTheme() {
    if (!tg) return;
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    try {
      tg.setHeaderColor(scheme().bg);
      tg.setBackgroundColor(scheme().bg);
      if (tg.setBottomBarColor) tg.setBottomBarColor(scheme().bg);
    } catch (_) { /* старые клиенты Telegram */ }
  }

  // Один главный CTA на экран: глагол + результат. Почему недоступна — объясняет строка состояния (status)
  function mainButton(text, onClick, enabled = true) {
    if (!tg) return;
    const mb = tg.MainButton;
    if (mainHandler) mb.offClick(mainHandler);
    mainHandler = onClick;
    mb.setText(text);
    enabled ? mb.enable() : mb.disable();
    mb.setParams({ is_active: enabled, color: enabled ? scheme().button : scheme().off, text_color: "#FFFFFF" });
    mb.onClick(onClick);
    mb.show();
  }

  // Строка состояния под кнопкой: «Оценено 3 из 6», «Выбрано 2 из 3»
  function status(text, ok = false) {
    const el = $("#cta-status");
    el.hidden = !text;
    el.textContent = text || "";
    el.classList.toggle("ok", ok);
    document.body.classList.toggle("has-status", !!text);
  }

  // Шаги первоначального плана: номер, русское название, метод — вторичным текстом
  const STEPS = {
    "screen-wheel": [1, "Оцени", "Колесо баланса"],
    "screen-wheel-done": [1, "Оцени", "Колесо баланса"],
    "screen-explore": [2, "Выгрузи", "Explore"],
    "screen-eliminate": [3, "Выбери 3", "Eliminate"],
    "screen-intent": [4, "Определи зачем", "Essential intent"],
    "screen-tactics": [5, "Действия", "Execute"],
    "screen-tactic-edit": [5, "Действия", "Execute"],
    "screen-priority-edit": [5, "Действия", "Execute"],
    "screen-plan": [6, "Проверка", "Итоговый план"],
  };

  function onboardingProgress(id) {
    const info = STEPS[id];
    const inOnboarding = state.me && !["done", "finished"].includes(state.me.step);
    const header = $("#ob-progress");
    header.hidden = !(info && inOnboarding);
    if (header.hidden) return;
    const [n, name, method] = info;
    $("#ob-step").textContent = "Шаг " + n + " из 6 · " + name;
    $("#ob-method").textContent = method;
    $("#ob-fill").style.width = (n / 6) * 100 + "%";
    $("#ob-bar").setAttribute("aria-valuenow", String(n));
  }

  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
  };

  function backButton(onClick) {
    if (!tg) return;
    if (backHandler) tg.BackButton.offClick(backHandler);
    backHandler = onClick;
    if (onClick) {
      tg.BackButton.onClick(onClick);
      tg.BackButton.show();
    } else {
      tg.BackButton.hide();
    }
  }

  const haptic = {
    tick: () => tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged(),
    ok: () => tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success"),
    err: () => tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("error"),
  };

  // ---------- API ----------

  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", Authorization: "tma " + tg.initData },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = new Error("api " + res.status);
      err.status = res.status;
      try { err.detail = (await res.json()).detail; } catch (_) { /* пусто */ }
      throw err;
    }
    return res.json();
  }

  // ---------- экраны ----------

  function show(id) {
    document.querySelectorAll(".screen").forEach((s) => (s.hidden = s.id !== id));
    status(null);
    onboardingProgress(id);
    window.scrollTo(0, 0);
  }

  function showError(text) {
    if (text) $("#error-text").textContent = text;
    if (tg) tg.MainButton.hide();
    backButton(null);
    show("screen-error");
  }

  function showWelcome() {
    show("screen-welcome");
    backButton(null);
    mainButton("Начать · ~5 минут", showWheel);
  }

  // ---------- колесо: данные ----------

  const sphereByKey = (key) => state.wheel.spheres.find((s) => s.key === key);
  const coreSpheres = () => state.wheel.spheres.filter((s) => s.core);
  const activeSpheres = () => coreSpheres().concat(state.extras.map(sphereByKey));
  const isLow = (v) => v != null && v <= state.wheel.low_threshold;

  function loadWheelState(data) {
    state.wheel = data;
    state.scores = Object.assign({}, data.scores);
    state.touched = new Set(Object.keys(data.scores));
    state.extras = Object.keys(data.scores).filter((k) => !sphereByKey(k).core);
  }

  function unratedCount() {
    return activeSpheres().filter((s) => !state.touched.has(s.key)).length;
  }

  // ---------- колесо: радар ----------

  const NS = "http://www.w3.org/2000/svg";
  const C = 160; // центр
  const R = 100; // радиус 10 баллов
  // поле вокруг круга — под подписи сфер (длинные «Саморазвитие» слева и справа)
  const VB = { x: -62, y: 18, w: 444, h: 284 };

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    if (parent) parent.appendChild(node);
    return node;
  }

  function point(i, n, value) {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
    const r = (R * value) / 10;
    return [C + r * Math.cos(angle), C + r * Math.sin(angle)];
  }

  function drawRadar(svg, spheres, scores, comparison, tip) {
    svg.innerHTML = "";
    svg.setAttribute("viewBox", [VB.x, VB.y, VB.w, VB.h].join(" "));
    const n = spheres.length;
    [2, 4, 6, 8, 10].forEach((v) => {
      const pts = spheres.map((_, i) => point(i, n, v).join(",")).join(" ");
      el("polygon", { class: "ring", points: pts }, svg);
    });

    spheres.forEach((s, i) => {
      const [x, y] = point(i, n, 10);
      el("line", { class: "axis", x1: C, y1: C, x2: x, y2: y }, svg);
      // подпись сферы: снаружи окружности, выравнивание по стороне
      const [lx, ly] = point(i, n, 12.4);
      const anchor = Math.abs(lx - C) < 8 ? "middle" : lx > C ? "start" : "end";
      const rated = scores[s.key] != null;
      const text = el("text", { class: "label" + (rated ? "" : " unrated"), x: lx, y: ly + 4, "text-anchor": anchor }, svg);
      if (rated && isLow(scores[s.key])) el("tspan", { class: "warn" }, text).textContent = "⚠ ";
      text.appendChild(document.createTextNode(s.title));
    });

    const before = comparison && comparison.length ? Object.fromEntries(comparison.map((c) => [c.key, c.before])) : null;
    if (before) {
      const pts = spheres.map((s, i) => point(i, n, before[s.key] || 0).join(",")).join(" ");
      el("polygon", { class: "area-before", points: pts }, svg);
    }
    const pts = spheres.map((s, i) => point(i, n, scores[s.key] || 0).join(",")).join(" ");
    el("polygon", { class: "area-now", points: pts }, svg);

    spheres.forEach((s, i) => {
      const v = scores[s.key];
      if (v == null) return;
      const [x, y] = point(i, n, v);
      el("circle", { class: "dot", cx: x, cy: y, r: 4.5 }, svg);
      if (!tip) return;
      // зона нажатия больше самой точки
      const hit = el("circle", { class: "hit", cx: x, cy: y, r: 16 }, svg);
      const text = s.emoji + " " + s.title + ": " + v + (before && before[s.key] != null ? " (было " + before[s.key] + ")" : "");
      const showTip = () => {
        const box = svg.getBoundingClientRect();
        tip.textContent = text;
        tip.style.left = ((x - VB.x) / VB.w) * box.width + "px";
        tip.style.top = ((y - VB.y) / VB.h) * box.height + "px";
        tip.hidden = false;
      };
      hit.addEventListener("pointerenter", showTip);
      hit.addEventListener("pointerdown", showTip);
      hit.addEventListener("pointerleave", () => (tip.hidden = true));
    });
    svg.setAttribute("aria-label", spheres.map((s) => s.title + " " + (scores[s.key] ?? "—")).join(", "));
  }

  // ---------- колесо: экран ----------

  function renderSphereRow(s) {
    const row = document.createElement("div");
    row.className = "sphere";
    row.innerHTML =
      '<div class="sphere-head"><span>' + s.emoji + '</span><span class="sphere-title"></span>' +
      '<span class="sphere-value"></span></div>' +
      '<input type="range" min="1" max="10" step="1">' +
      '<div class="scale"><span>1 · совсем плохо</span><span>10 · лучше не бывает</span></div>';
    row.querySelector(".sphere-title").textContent = s.title;
    const input = row.querySelector("input");
    input.setAttribute("aria-label", s.title);
    input.value = state.scores[s.key] || 5;
    if (!s.core) {
      const rm = document.createElement("button");
      rm.className = "sphere-remove";
      rm.setAttribute("aria-label", "Убрать сферу " + s.title);
      rm.textContent = "×";
      rm.addEventListener("click", () => {
        state.extras = state.extras.filter((k) => k !== s.key);
        delete state.scores[s.key];
        state.touched.delete(s.key);
        renderWheel();
      });
      row.querySelector(".sphere-head").appendChild(rm);
    }
    const update = () => {
      const rated = state.touched.has(s.key);
      row.classList.toggle("unrated", !rated);
      row.classList.toggle("low", rated && isLow(state.scores[s.key]));
      row.querySelector(".sphere-value").textContent = rated ? state.scores[s.key] + "/10" : "—";
    };
    const onInput = () => {
      const v = Number(input.value);
      if (state.scores[s.key] !== v || !state.touched.has(s.key)) haptic.tick();
      state.scores[s.key] = v;
      state.touched.add(s.key);
      update();
      refreshWheel();
    };
    input.addEventListener("input", onInput);
    // касание без сдвига тоже считается оценкой (например, согласна на 5)
    input.addEventListener("change", onInput);
    update();
    return row;
  }

  function renderChips() {
    const box = $("#extra-chips");
    box.innerHTML = "";
    // доп. сферы спрятаны под «+ Добавить сферу» — чтобы не выглядели обязательными
    const toggle = $("#toggle-extras");
    toggle.hidden = state.extras.length >= state.wheel.max_extra;
    if (toggle.hidden) box.hidden = true;
    const full = state.extras.length >= state.wheel.max_extra;
    state.wheel.spheres.filter((s) => !s.core && !state.extras.includes(s.key)).forEach((s) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = "+ " + s.emoji + " " + s.title;
      chip.disabled = full;
      chip.addEventListener("click", () => {
        state.extras.push(s.key);
        renderWheel();
      });
      box.appendChild(chip);
    });
  }

  function refreshWheel() {
    const scores = {};
    state.touched.forEach((k) => (scores[k] = state.scores[k]));
    drawRadar($("#radar"), activeSpheres(), scores, state.wheel.comparison, $("#radar-tip"));
    $("#radar-legend").hidden = !state.wheel.comparison.length;
    const core = coreSpheres();
    const rated = core.filter((s) => state.touched.has(s.key)).length;
    const left = unratedCount();
    mainButton("Продолжить", saveWheel, left === 0);
    status(
      left === 0 ? "Всё оценено ✓" :
      rated < core.length ? "Оценено " + rated + " из " + core.length + " обязательных" :
      "Оцени и добавленную сферу — или убери её",
      left === 0,
    );
  }

  function renderWheel() {
    const list = $("#sphere-list");
    list.innerHTML = "";
    activeSpheres().forEach((s) => list.appendChild(renderSphereRow(s)));
    renderChips();
    refreshWheel();
  }

  async function showWheel() {
    show("screen-wheel");
    backButton(state.me.step === "wheel" ? showWelcome : showWheelDone);
    renderWheel();
  }

  async function saveWheel() {
    if (unratedCount()) return;
    const scores = {};
    activeSpheres().forEach((s) => (scores[s.key] = state.scores[s.key]));
    tg.MainButton.showProgress();
    try {
      const data = await api("PUT", "/api/wheel", { scores });
      loadWheelState(data);
      state.me.step = "explore";
      haptic.ok();
      showWheelDone();
    } catch (e) {
      haptic.err();
      tg.showAlert(e.status === 409 ? "Колесо уже нельзя изменить: ты прошла шаг Eliminate." : "Не получилось сохранить. Попробуй ещё раз.");
    } finally {
      tg.MainButton.hideProgress();
    }
  }

  // ---------- итог колеса ----------

  function showWheelDone() {
    show("screen-wheel-done");
    backButton(null);
    const w = state.wheel;
    const spheres = Object.keys(w.scores).map(sphereByKey);
    const ordered = w.spheres.filter((s) => spheres.includes(s));
    drawRadar($("#radar-done"), ordered, w.scores, w.comparison, null);
    $("#radar-done-legend").hidden = !w.comparison.length;
    // не «средний балл» (звучит как оценка жизни), а где меньше и больше всего внимания
    const entries = ordered.map((s) => [s, w.scores[s.key]]);
    const min = Math.min(...entries.map((e) => e[1]));
    const max = Math.max(...entries.map((e) => e[1]));
    const names = (v) => entries.filter((e) => e[1] === v).slice(0, 2).map(([s]) => s.emoji + " " + s.title).join(", ") + " " + v + "/10";
    $("#wheel-low").textContent = names(min);
    $("#wheel-high").textContent = names(max);

    const cmp = $("#comparison");
    cmp.hidden = !w.comparison.length;
    if (w.comparison.length) {
      cmp.innerHTML = "<b>Как изменилось за 12 недель</b><ul></ul>";
      w.comparison.forEach((c) => {
        const s = sphereByKey(c.key);
        const d = c.after - c.before;
        const li = document.createElement("li");
        li.textContent = s.emoji + " " + s.title + ": " + c.before + " → " + c.after + " " + (d > 0 ? "(+" + d + ") 🌱" : d < 0 ? "(" + d + ")" : "(=)");
        cmp.querySelector("ul").appendChild(li);
      });
    }
    mainButton("Продолжить", showExplore);
  }

  // ---------- Explore ----------

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }

  const QUICK = ["Работа", "Деньги", "Здоровье", "Отношения", "Учёба", "Проект", "Переезд"];
  const EXAMPLES = ["найти стажировку", "запустить сайт", "начать тренироваться", "увеличить доход", "познакомиться с новыми людьми"];

  function renderQuick() {
    const box = $("#explore-quick");
    if (box.childElementCount) return;
    QUICK.forEach((q) => {
      const chip = el2("button", "chip", q);
      chip.type = "button";
      // тема — начало мысли: подставляем в поле и даём дописать
      chip.addEventListener("click", () => {
        const input = $("#explore-input");
        input.value = q + ": ";
        input.focus();
        autosize(input);
      });
      box.appendChild(chip);
    });
  }

  async function saveExploreEdit(item, span) {
    const text = span.textContent.trim();
    span.contentEditable = "false";
    if (!text || text === item.text) { span.textContent = item.text; return; }
    try {
      state.explore = await api("PUT", "/api/explore/" + item.id, { text });
      renderExplore();
    } catch (e) { span.textContent = item.text; failed(e); }
  }

  function renderExplore() {
    const d = state.explore;
    renderQuick();
    const input = $("#explore-input");
    if (!input.placeholder) input.placeholder = "Например: " + EXAMPLES[Math.floor(Math.random() * EXAMPLES.length)];
    const hints = [];
    if (d.lows.length) hints.push("<b>Подсказка из колеса:</b> <span data-t='lows'></span>");
    if (d.previous.length) hints.push("<b>Прошлые приоритеты:</b> <span data-t='prev'></span> — можно вписать снова, если это всё ещё важно.");
    const box = $("#explore-hints");
    box.hidden = !hints.length;
    box.innerHTML = hints.map((h) => "<p>" + h + "</p>").join("");
    if (d.lows.length) box.querySelector("[data-t=lows]").textContent = d.lows.join(", ");
    if (d.previous.length) box.querySelector("[data-t=prev]").textContent = d.previous.join(", ");

    const list = $("#explore-list");
    list.innerHTML = "";
    d.items.forEach((item) => {
      const li = document.createElement("li");
      li.innerHTML = "<span></span><button aria-label='Удалить'>×</button>";
      const span = li.querySelector("span");
      span.textContent = item.text;
      // нажатие на пункт — исправить прямо в карточке; Enter или уход из поля — сохранить
      span.addEventListener("click", () => {
        if (span.contentEditable === "true") return;
        span.contentEditable = "true";
        span.focus();
        document.getSelection().selectAllChildren(span);
      });
      span.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); span.blur(); }
      });
      span.addEventListener("blur", () => saveExploreEdit(item, span));
      li.querySelector("button").addEventListener("click", async () => {
        try {
          state.explore = await api("DELETE", "/api/explore/" + item.id);
          haptic.tick();
          renderExplore();
        } catch (e) { failed(e); }
      });
      list.appendChild(li);
    });
    const n = d.items.length;
    const left = d.min - n;
    mainButton("Выбрать " + d.pick + " приоритета", exploreDone, left <= 0);
    status(
      !n ? "Нужно хотя бы " + d.min + " пункта — чтобы было из чего выбирать" :
      left > 0 ? "Добавлено " + n + " " + plural(n, "пункт", "пункта", "пунктов") + " · ещё " + left + ", чтобы было из чего выбрать" :
      "Добавлено " + n + " " + plural(n, "пункт", "пункта", "пунктов") + " ✓",
      left <= 0,
    );
  }

  async function addExplore(ev) {
    ev.preventDefault();
    const input = $("#explore-input");
    const text = input.value.trim();
    if (!text) return;
    try {
      state.explore = await api("POST", "/api/explore", { text });
      if (state.explore.added === 0) tg.showAlert("Это уже есть в списке 🙂");
      input.value = "";
      autosize(input);
      haptic.tick();
      renderExplore();
    } catch (e) {
      e.detail === "limit" ? tg.showAlert("В списке уже " + state.explore.max + " пунктов — это максимум. Пора выбирать 🙂") : failed(e);
    }
    input.focus();
  }

  async function showExplore() {
    try {
      state.explore = await api("GET", "/api/explore");
    } catch (e) { return failed(e); }
    show("screen-explore");
    backButton(null);
    renderExplore();
  }

  async function exploreDone() {
    try {
      state.explore = await api("POST", "/api/explore/done");
      state.me.step = "eliminate";
      showEliminate();
    } catch (e) { failed(e); }
  }

  // ---------- Eliminate ----------

  function renderEliminate() {
    const d = state.explore;
    const picked = state.picked;
    const full = picked.size >= d.pick;
    const list = $("#eliminate-list");
    list.innerHTML = "";
    d.items.forEach((item) => {
      const on = picked.has(item.id);
      const label = document.createElement("label");
      label.className = "check" + (on ? " on" : full ? " off" : "");
      label.innerHTML = "<input type='checkbox'><i class='tick' aria-hidden='true'>✓</i><span></span>";
      const box = label.querySelector("input");
      box.checked = on;
      box.disabled = !on && full; // после третьей остальные заблокированы
      label.querySelector("span").textContent = item.text;
      box.addEventListener("change", () => {
        box.checked ? picked.add(item.id) : picked.delete(item.id);
        haptic.tick();
        renderEliminate();
      });
      list.appendChild(label);
    });
    const left = d.pick - picked.size;
    mainButton("Продолжить", confirmEliminate, left === 0);
    status("Выбрано " + picked.size + " из " + d.pick + (left === 0 ? " ✓" : ""), left === 0);
  }

  async function showEliminate() {
    if (!state.explore) {
      try { state.explore = await api("GET", "/api/explore"); } catch (e) { return failed(e); }
    }
    state.picked = new Set(state.explore.items.filter((i) => i.selected).map((i) => i.id));
    show("screen-eliminate");
    backButton(showExplore);
    renderEliminate();
  }

  async function confirmEliminate() {
    tg.MainButton.showProgress();
    try {
      state.intent = await api("PUT", "/api/eliminate", { selected: Array.from(state.picked) });
      state.me.step = "intent";
      haptic.ok();
      showIntent();
    } catch (e) { failed(e); } finally { tg.MainButton.hideProgress(); }
  }

  // ---------- Essential intent ----------

  function intentReady() {
    return state.intent.priorities.every((p) => (state.drafts[p.title] || "").trim().length >= state.intent.min_len);
  }

  const DRAFTS_KEY = () => "12w-intent-drafts-" + (tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : "");

  function saveDrafts() {
    try { localStorage.setItem(DRAFTS_KEY(), JSON.stringify(state.drafts)); } catch (_) { /* приватный режим и т.п. */ }
  }

  function loadDrafts() {
    try { return JSON.parse(localStorage.getItem(DRAFTS_KEY()) || "{}") || {}; } catch (_) { return {}; }
  }

  function refreshIntentButton() {
    const total = state.intent.priorities.length;
    const ready = state.intent.priorities.filter((p) => (state.drafts[p.title] || "").trim().length >= state.intent.min_len).length;
    mainButton("Продолжить к действиям", saveIntent, ready === total);
    status("Готово " + ready + " из " + total + (ready === total ? " ✓" : ""), ready === total);
  }

  function renderIntent() {
    const d = state.intent;
    const notNow = $("#not-now");
    notNow.hidden = !d.not_now.length;
    const ul = notNow.querySelector("ul");
    ul.innerHTML = "";
    d.not_now.forEach((t) => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });

    const list = $("#intent-list");
    list.innerHTML = "";
    const stored = loadDrafts();
    d.priorities.forEach((p) => {
      if (state.drafts[p.title] == null) state.drafts[p.title] = p.intent || stored[p.title] || "";
      const wrap = document.createElement("div");
      wrap.className = "intent";
      wrap.innerHTML = "<label></label><textarea maxlength='2000'></textarea><div class='hint'></div>";
      const id = "intent-" + p.id;
      wrap.querySelector("label").textContent = p.position + ". " + p.title;
      wrap.querySelector("label").setAttribute("for", id);
      const ta = wrap.querySelector("textarea");
      ta.id = id;
      ta.placeholder = "Почему это важно? Что изменится, когда получится?";
      ta.value = state.drafts[p.title];
      const hint = wrap.querySelector(".hint");
      const update = () => {
        const left = d.min_len - ta.value.trim().length;
        hint.textContent = left > 0 ? "Ещё " + left + " симв. — пару слов о том, зачем" : "✓ Готово";
        hint.classList.toggle("ok", left <= 0);
      };
      ta.addEventListener("input", () => {
        state.drafts[p.title] = ta.value;
        saveDrafts();
        update();
        refreshIntentButton();
      });
      update();
      list.appendChild(wrap);
    });
    refreshIntentButton();
  }

  async function showIntent() {
    if (!state.intent) {
      try { state.intent = await api("GET", "/api/intent"); } catch (e) { return failed(e); }
    }
    show("screen-intent");
    // пока «зачем» не сохранены, выбор трёх можно пересмотреть
    backButton(state.me.step === "intent" ? showEliminate : showTactics);
    renderIntent();
  }

  async function saveIntent() {
    if (!intentReady()) return;
    const intents = {};
    state.intent.priorities.forEach((p) => (intents[p.id] = state.drafts[p.title].trim()));
    tg.MainButton.showProgress();
    try {
      state.intent = await api("PUT", "/api/intent", { intents });
      state.me.step = "tactics";
      haptic.ok();
      showTactics();
    } catch (e) { failed(e); } finally { tg.MainButton.hideProgress(); }
  }

  // ---------- Тактики ----------

  const MEASURABLE = /\d|раз|кажд|ежедн|минут|час|страниц|шаг|км|километр|тренировк|сесси|урок/i;
  const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  const DAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"];
  const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];

  function formatDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return d + " " + MONTHS[m - 1];
  }

  function el2(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // Таблица «тактики × 12 недель»: строка — тактика (цвет приоритета), колонка — неделя, внизу — нагрузка
  function renderGrid(box, plan) {
    const weeks = Array.from({ length: plan.weeks_total }, (_, i) => i + 1);
    const table = el2("table");
    table.setAttribute("aria-label", "План по неделям");
    const head = table.createTHead().insertRow();
    head.appendChild(el2("th", "name", ""));
    weeks.forEach((w) => head.appendChild(el2("th", null, String(w))));
    const body = table.createTBody();
    plan.priorities.forEach((p) => {
      if (!p.tactics.length) return;
      const g = body.insertRow();
      g.className = "group-row";
      const gh = el2("th", null, p.position + ". " + p.title);
      gh.colSpan = weeks.length + 1;
      g.appendChild(gh);
      p.tactics.forEach((t) => {
        const row = body.insertRow();
        row.className = "p" + p.position;
        const name = el2("th", "name", t.text);
        name.title = t.text + " — " + t.label;
        row.appendChild(name);
        weeks.forEach((w) => {
          const on = t.weeks == null || t.weeks.includes(w);
          const td = el2("td", "cell" + (on ? " on" : ""));
          td.title = "Неделя " + w + ": " + (on ? t.text : "—");
          row.appendChild(td);
        });
      });
    });
    const foot = table.createTFoot().insertRow();
    foot.appendChild(el2("th", "name", "в неделю"));
    const peak = Math.max(...plan.load);
    plan.load.forEach((n) => foot.appendChild(el2("td", n === peak && n > 0 ? "peak" : null, String(n))));
    box.innerHTML = "";
    box.appendChild(table);
  }

  function bufferText(plan) {
    const peak = Math.max(...plan.load);
    if (!peak) return "";
    const week = plan.load.indexOf(peak) + 1;
    return "Больше всего тактик — " + peak + " — на неделе " + week + ". Оставь буфер на непредвиденное: неделя, забитая на 100%, ломается от первого форс-мажора.";
  }

  function renderTactics() {
    const plan = state.plan;
    const box = $("#tactic-groups");
    box.innerHTML = "";
    plan.priorities.forEach((p) => {
      const group = el2("div", "group");
      const head = el2("div", "group-head");
      const dot = el2("span", "dot-p");
      dot.style.background = "var(--p" + p.position + ")";
      head.append(dot, el2("b", null, p.position + ". " + p.title));
      const pen = el2("button", "edit-prio", "✏️");
      pen.setAttribute("aria-label", "Изменить цель и «зачем»");
      pen.addEventListener("click", () => openPriorityEditor(p));
      head.appendChild(pen);
      group.append(head, el2("p", "why", "Зачем: " + p.intent));
      p.tactics.forEach((t) => {
        const btn = el2("button", "tactic");
        const main = el2("span", "t-main");
        main.append(el2("span", "t-text", t.text), el2("span", "t-when", t.label));
        if (!t.measurable) main.appendChild(el2("span", "t-warn", "💡 добавь число — сколько раз, минут, страниц?"));
        btn.append(main, el2("span", "chev", "›"));
        btn.addEventListener("click", () => openEditor(p, t));
        group.appendChild(btn);
      });
      const add = el2("button", "add-tactic", "+ Тактика");
      add.disabled = p.tactics.length >= plan.max_per_priority;
      add.addEventListener("click", () => openEditor(p, null));
      group.appendChild(add);
      const n = p.tactics.length;
      const [lo, hi] = plan.recommended;
      if (n && n < lo) group.appendChild(el2("p", "count-note", "Обычно " + lo + "–" + hi + " тактик: регулярные и контрольные точки."));
      if (n >= plan.max_per_priority) group.appendChild(el2("p", "count-note", "Максимум " + plan.max_per_priority + " — меньше, но лучше."));
      box.appendChild(group);
    });
    renderGrid($("#tactics-grid"), plan);
    $("#buffer-hint").textContent = bufferText(plan);
    const missing = plan.priorities.find((p) => !p.tactics.length);
    const doneLabel = planConfirmed() ? "Готово" : "Посмотреть итоговый план";
    const next = planConfirmed() ? () => showPlan("view") : () => showPlan("review");
    const withTactics = plan.priorities.filter((p) => p.tactics.length).length;
    mainButton(doneLabel, next, !missing);
    status(
      missing ? "Действия есть в " + withTactics + " из " + plan.priorities.length + " приоритетов · добавь в «" + missing.title.slice(0, 24) + "»"
              : "Действия есть во всех " + plan.priorities.length + " приоритетах ✓",
      !missing,
    );
  }

  const planConfirmed = () => state.plan && state.plan.step === "done";

  function toTactics() {
    show("screen-tactics");
    // в онбординге назад — к «зачем»; в подтверждённом плане (правка до старта) — к плану
    backButton(planConfirmed() ? () => showPlan("view") : showIntent);
    renderTactics();
  }

  async function showTactics() {
    try { state.plan = await api("GET", "/api/plan"); } catch (e) { return failed(e); }
    toTactics();
  }

  // ---------- цель и «зачем» ----------

  function priorityValid() {
    return $("#prio-title").value.trim().length > 0 && $("#prio-intent").value.trim().length >= 15;
  }

  function refreshPriorityEditor() {
    const left = 15 - $("#prio-intent").value.trim().length;
    $("#prio-hint").textContent = left > 0 ? "«Зачем» — ещё " + left + " симв., чуть подробнее, чем «надо»" : "";
    mainButton("Сохранить", savePriority, priorityValid());
  }

  function openPriorityEditor(p) {
    state.editPriority = p;
    show("screen-priority-edit");
    $("#prio-eyebrow").textContent = "Приоритет " + p.position;
    $("#prio-title").value = p.title;
    $("#prio-intent").value = p.intent;
    backButton(toTactics);
    refreshPriorityEditor();
  }

  async function savePriority() {
    if (!priorityValid()) return;
    tg.MainButton.showProgress();
    try {
      state.plan = await api("PUT", "/api/priorities/" + state.editPriority.id, {
        title: $("#prio-title").value.trim(),
        intent: $("#prio-intent").value.trim(),
      });
      haptic.ok();
      toTactics();
    } catch (e) { failed(e); } finally { tg.MainButton.hideProgress(); }
  }

  // ---------- редактор тактики ----------

  function openEditor(priority, tactic) {
    const weeks = tactic && tactic.weeks ? tactic.weeks : null;
    state.edit = {
      priorityId: priority.id,
      tacticId: tactic ? tactic.id : null,
      mode: weeks == null ? "every" : weeks.length === 1 ? "one" : "some",
      weeks: new Set(weeks || []),
      days: new Set((tactic && tactic.days) || []),
    };
    show("screen-tactic-edit");
    $("#edit-priority").textContent = priority.position + ". " + priority.title;
    $("#edit-title").textContent = tactic ? "Тактика" : "Новая тактика";
    const ta = $("#tactic-text");
    ta.value = tactic ? tactic.text : "";
    $("#delete-tactic").hidden = !tactic;
    backButton(showTactics);
    renderEditor();
    if (!tactic) ta.focus();
  }

  function editorValid() {
    const e = state.edit;
    const text = $("#tactic-text").value.trim();
    if (!text) return false;
    if (e.mode === "some") return e.weeks.size >= 1;
    if (e.mode === "one") return e.weeks.size === 1;
    return e.days.size >= 1; // еженедельная — нужен хотя бы один день
  }

  function renderEditor() {
    const e = state.edit;
    document.querySelectorAll("#schedule-mode button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === e.mode)));
    const grid = $("#week-grid");
    grid.hidden = e.mode === "every";
    grid.innerHTML = "";
    if (e.mode !== "every") {
      for (let w = 1; w <= 12; w++) {
        const b = el2("button", null, String(w));
        b.type = "button";
        b.setAttribute("aria-pressed", String(e.weeks.has(w)));
        b.setAttribute("aria-label", "Неделя " + w);
        b.addEventListener("click", () => {
          if (e.mode === "one") e.weeks = new Set([w]);
          else e.weeks.has(w) ? e.weeks.delete(w) : e.weeks.add(w);
          haptic.tick();
          renderEditor();
        });
        grid.appendChild(b);
      }
    }
    // дни недели — только для еженедельной тактики
    $("#day-picker").hidden = e.mode !== "every";
    const dayGrid = $("#day-grid");
    dayGrid.innerHTML = "";
    DAYS.forEach((name, d) => {
      const b = el2("button", null, name);
      b.type = "button";
      b.setAttribute("aria-pressed", String(e.days.has(d)));
      b.setAttribute("aria-label", DAYS_FULL[d]);
      b.addEventListener("click", () => {
        e.days.has(d) ? e.days.delete(d) : e.days.add(d);
        haptic.tick();
        renderEditor();
      });
      dayGrid.appendChild(b);
    });
    const days = Array.from(e.days).sort((a, b) => a - b);
    const sorted = Array.from(e.weeks).sort((a, b) => a - b);
    $("#weeks-summary").textContent =
      e.mode === "every" ? (!days.length ? "Выбери дни — например, пн, ср, пт для «3 тренировок»." :
        days.length === 7 ? "Каждый день, все 12 недель." :
        "Каждую неделю: " + days.map((d) => DAYS[d].toLowerCase()).join(", ") + ".") :
      !sorted.length ? (e.mode === "one" ? "Выбери неделю." : "Отметь недели — например, 4, 8 и 12 как контрольные точки.") :
      (sorted.length === 1 ? "Неделя " : "Недели ") + sorted.join(", ");
    const text = $("#tactic-text").value.trim();
    // для регулярных тактик: «каждую неделю» без числа обычно размыто; контрольная точка и так «да / нет»
    $("#measurable-hint").textContent = text && e.mode === "every" && !MEASURABLE.test(text) ? "💡 Похоже на цель. Сделай измеримой: сколько раз, минут, страниц?" : "";
    mainButton("Сохранить тактику", saveTactic, editorValid());
  }

  async function saveTactic() {
    if (!editorValid()) return;
    const e = state.edit;
    const every = e.mode === "every";
    const body = {
      text: $("#tactic-text").value.trim(),
      weeks: every ? null : Array.from(e.weeks),
      days: every ? Array.from(e.days) : null,
    };
    tg.MainButton.showProgress();
    try {
      state.plan = e.tacticId
        ? await api("PUT", "/api/tactics/" + e.tacticId, body)
        : await api("POST", "/api/tactics", Object.assign({ priority_id: e.priorityId }, body));
      haptic.ok();
      toTactics();
    } catch (err) { failed(err); } finally { tg.MainButton.hideProgress(); }
  }

  function deleteTactic() {
    tg.showConfirm("Удалить эту тактику?", async (ok) => {
      if (!ok) return;
      try {
        state.plan = await api("DELETE", "/api/tactics/" + state.edit.tacticId);
        toTactics();
      } catch (err) { failed(err); }
    });
  }

  // ---------- итог плана ----------

  function showPlan(mode) {
    const plan = state.plan;
    const review = mode === "review";
    show("screen-plan");
    $("#plan-eyebrow").textContent = review ? "Шаг 6 · Итог" : "12 недель";
    $("#plan-title").textContent = review ? "Твой план на 12 недель" : "Мой план";
    const [y, m, d] = plan.cycle_start.split("-").map(Number);
    const started = new Date(y, m - 1, d) <= new Date();
    $("#plan-start").textContent =
      (started ? "Цикл идёт с " : "Старт — в понедельник, ") + formatDate(plan.cycle_start) +
      (plan.team ? " · " + plan.team : "");
    const box = $("#plan-priorities");
    box.innerHTML = "";
    plan.priorities.forEach((p) => {
      const card = el2("div", "card plan-prio");
      card.append(el2("b", null, p.position + ". " + p.title), el2("p", "why", "Зачем: " + p.intent));
      const ul = el2("ul");
      p.tactics.forEach((t) => {
        const li = el2("li", null, t.text + " ");
        li.appendChild(el2("span", null, "· " + t.label));
        ul.appendChild(li);
      });
      card.appendChild(ul);
      box.appendChild(card);
    });
    renderGrid($("#plan-grid"), plan);
    $("#plan-buffer").textContent = bufferText(plan);
    $("#edit-plan").hidden = !review;
    $("#plan-edit-box").hidden = review || !plan.editable;
    if (review) {
      backButton(showTactics);
      mainButton("Подтвердить план", confirmPlan);
      const count = plan.priorities.reduce((n, p) => n + p.tactics.length, 0);
      status(plan.priorities.length + " приоритета · " + count + " " + plural(count, "действие", "действия", "действий") + " · старт " + formatDate(plan.cycle_start), true);
    } else {
      backButton(showHome);
      mainButton("На главную", showHome);
    }
  }

  async function confirmPlan() {
    tg.MainButton.showProgress();
    try {
      state.plan = await api("POST", "/api/plan/confirm");
      state.me.step = "done";
      haptic.ok();
      show("screen-ready");
      backButton(null);
      $("#ready-start").textContent = "Неделя 1 из 12 начинается в понедельник, " + formatDate(state.plan.cycle_start) + ".";
      $("#ready-team").textContent = state.plan.team ? "Твоя команда — " + state.plan.team + " 🤝 Подробности я написала в чат." : "";
      mainButton("На главную", showHome);
    } catch (e) { failed(e); } finally { tg.MainButton.hideProgress(); }
  }

  // ---------- Главная в цикле: неделя, средний %, график по неделям ----------

  const STATUS = {
    good: { icon: "🟢", label: "отлично" },
    warning: { icon: "🟡", label: "хорошо, есть что подтянуть" },
    critical: { icon: "🔴", label: "сбой — разберись, что помешало" },
  };

  function period(startIso, endIso) {
    const [, m1, d1] = startIso.split("-").map(Number);
    const [, m2, d2] = endIso.split("-").map(Number);
    return m1 === m2 ? d1 + "–" + d2 + " " + MONTHS[m2 - 1] : d1 + " " + MONTHS[m1 - 1] + " – " + d2 + " " + MONTHS[m2 - 1];
  }

  function daysUntil(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((new Date(y, m - 1, d) - today) / 86400000);
  }

  // Столбики по 12 неделям: высота — %, цвет — статус (со значком и подписью в подсказке и легенде)
  function drawScoreChart(svg, sc, tip) {
    svg.innerHTML = "";
    const X0 = 30, X1 = 352, Y0 = 14, Y1 = 158;
    const y = (v) => Y1 - ((Y1 - Y0) * v) / 100;
    const step = (X1 - X0) / 12;
    const bw = step * 0.62;
    [0, 50, 100].forEach((v) => {
      el("line", { class: "gridline", x1: X0, x2: X1, y1: y(v), y2: y(v) }, svg);
      el("text", { class: "axis-label", x: X0 - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = v;
    });
    [[sc.thresholds.good, "85"], [sc.thresholds.warning, "70"]].forEach(([v, label]) => {
      el("line", { class: "threshold", x1: X0, x2: X1, y1: y(v), y2: y(v) }, svg);
      el("text", { class: "thr-label", x: X1 + 2, y: y(v) + 3 }, svg).textContent = label;
    });
    sc.weeks.forEach((w, i) => {
      const cx = X0 + step * i + step / 2;
      const x = cx - bw / 2;
      el("text", { class: "axis-label" + (w.n === sc.current_week ? " now" : ""), x: cx, y: Y1 + 16, "text-anchor": "middle" }, svg).textContent = w.n;
      let text;
      if (w.percent != null) {
        const h = Math.max(Y1 - y(w.percent), 3);
        el("rect", { class: "bar " + w.level, x, y: Y1 - h, width: bw, height: h, rx: 3 }, svg);
        text = "Неделя " + w.n + ": " + w.percent + "% " + STATUS[w.level].icon + " " + STATUS[w.level].label;
      } else if (w.future) {
        el("rect", { class: "slot", x, y: y(100), width: bw, height: Y1 - y(100), rx: 3 }, svg);
        text = "Неделя " + w.n + ": впереди · тактик по плану: " + w.planned;
      } else {
        el("rect", { class: "missed", x, y: Y1 - 3, width: bw, height: 3, rx: 1.5 }, svg);
        text = "Неделя " + w.n + ": " + (w.planned ? "не отмечена" : "буфер — тактик не было");
      }
      const hit = el("rect", { x: cx - step / 2, y: Y0, width: step, height: Y1 - Y0 + 20, fill: "transparent" }, svg);
      const showTip = () => {
        const box = svg.getBoundingClientRect();
        tip.textContent = text;
        tip.style.left = Math.min(Math.max((cx / 360) * box.width, 90), box.width - 90) + "px";
        tip.style.top = (((w.percent != null ? y(w.percent) : Y1)) / 190) * box.height + "px";
        tip.hidden = false;
      };
      hit.addEventListener("pointerenter", showTip);
      hit.addEventListener("pointerdown", showTip);
      hit.addEventListener("pointerleave", () => (tip.hidden = true));
    });
    svg.setAttribute("aria-label", sc.weeks.map((w) => "неделя " + w.n + ": " + (w.percent != null ? w.percent + "%" : "—")).join(", "));
  }

  async function showHome() {
    let sc, ci;
    try {
      [sc, ci] = await Promise.all([api("GET", "/api/scorecard"), api("GET", "/api/checkin")]);
    } catch (e) { return failed(e); }
    state.checkin = ci;
    show("screen-home");
    backButton(null);
    if (ci.status === "not_started") {
      const days = daysUntil(ci.cycle_start);
      $("#home-title").textContent = "Старт — " + formatDate(ci.cycle_start);
      $("#home-sub").textContent = days > 0 ? "До начала 12 недель: " + days + " дн. До старта план ещё можно поменять." : "";
      $("#home-week-label").textContent = "Неделя 1";
      $("#home-week").textContent = "—";
      $("#home-week-note").textContent = "ещё не началась";
    } else if (ci.status === "active") {
      $("#home-title").textContent = "Неделя " + ci.week_number + " из 12";
      $("#home-sub").textContent = period(ci.week_start, ci.week_end);
      $("#home-week-label").textContent = "Неделя " + ci.week_number;
      $("#home-week").textContent = ci.result ? ci.result.percent + "%" : "—";
      $("#home-week-note").textContent = ci.result ? STATUS[ci.result.level].icon + " " + STATUS[ci.result.level].label : ci.tactics.length ? "ещё не отмечена" : "буфер 🌿";
    } else {
      $("#home-title").textContent = "12 недель позади 🎉";
      $("#home-sub").textContent = "Итоги и старт нового цикла — в чате с ботом.";
    }
    $("#home-avg").textContent = sc.average != null ? sc.average + "%" : "—";
    $("#home-avg-note").textContent = sc.average_level ? STATUS[sc.average_level].icon + " " + STATUS[sc.average_level].label : "появится после первого чек-ина";
    drawScoreChart($("#score-chart"), sc, $("#score-tip"));

    if (ci.status === "active" && ci.tactics.length) {
      mainButton(ci.result ? "Изменить отметки" : "Отметить неделю " + ci.week_number, showCheckin);
    } else if (ci.status === "not_started") {
      mainButton("Мой план", openPlanView);
    } else {
      mainButton("Вернуться в чат", () => tg.close());
    }
  }

  async function openPlanView() {
    try { state.plan = await api("GET", "/api/plan"); } catch (e) { return failed(e); }
    showPlan("view");
  }

  // ---------- Чек-ин ----------

  function renderCheckin() {
    const ci = state.checkin;
    const marks = state.marks;
    const list = $("#checkin-list");
    list.innerHTML = "";
    let group = null;
    ci.tactics.forEach((t) => {
      if (t.priority_position !== group) {
        group = t.priority_position;
        const g = el2("div", "ci-group");
        const dot = el2("span", "dot-p");
        dot.style.cssText = "width:10px;height:10px;border-radius:50%;background:var(--p" + group + ")";
        g.append(dot, document.createTextNode(t.priority_position + ". " + t.priority_title));
        list.appendChild(g);
      }
      const row = el2("div", "ci-row");
      const txt = el2("div", "ci-text");
      txt.append(el2("b", null, t.text), el2("span", null, t.label));
      const yn = el2("div", "yn");
      [["yes", "✓", true, "Сделано"], ["no", "✕", false, "Не сделано"]].forEach(([cls, sym, val, aria]) => {
        const b = el2("button", cls, sym);
        b.type = "button";
        b.setAttribute("aria-label", aria + ": " + t.text);
        b.setAttribute("aria-pressed", String(marks[t.id] === val));
        b.addEventListener("click", () => {
          marks[t.id] = val;
          haptic.tick();
          renderCheckin();
        });
        yn.appendChild(b);
      });
      row.append(txt, yn);
      list.appendChild(row);
    });
    const marked = ci.tactics.filter((t) => marks[t.id] != null).length;
    $("#checkin-count").textContent = "Отмечено " + marked + " из " + ci.tactics.length;
    const left = ci.tactics.length - marked;
    mainButton(left ? "Отметь ещё " + left : "Сохранить неделю", saveCheckin, left === 0);
  }

  function showCheckin() {
    const ci = state.checkin;
    state.marks = {};
    ci.tactics.forEach((t) => { if (t.done != null) state.marks[t.id] = t.done; });
    show("screen-checkin");
    $("#checkin-eyebrow").textContent = "Чек-ин · неделя " + ci.week_number + " · " + period(ci.week_start, ci.week_end);
    backButton(showHome);
    renderCheckin();
  }

  async function saveCheckin() {
    const ci = state.checkin;
    tg.MainButton.showProgress();
    try {
      const res = await api("PUT", "/api/checkin", { week_start: ci.week_start, marks: state.marks });
      res.level === "critical" ? haptic.tick() : haptic.ok();
      show("screen-result");
      backButton(null);
      $("#result-eyebrow").textContent = "Неделя " + ci.week_number;
      const v = $("#result-value");
      v.textContent = res.percent + "%";
      v.style.color = "var(--" + (res.level === "critical" ? "bad" : res.level) + ")";
      $("#result-rating").textContent = STATUS[res.level].icon + " " + res.rating;
      $("#result-count").textContent = "Выполнено " + res.done + " из " + res.planned;
      $("#result-advice").textContent = res.advice;
      mainButton("На главную", showHome);
    } catch (e) { failed(e); } finally { tg.MainButton.hideProgress(); }
  }

  function failed(e) {
    haptic.err();
    const messages = {
      wrong_step: "Этот шаг уже пройден — открой приложение заново.",
      mark_all: "Отметь все тактики этой недели.",
      week_closed: "Эта неделя уже закрыта для отметок.",
      plan_locked: "Цикл уже начался — план закрыт для изменений.",
      last_tactic: "У каждой цели должна остаться хотя бы одна тактика.",
      bad_title: "Напиши цель.",
      not_enough: "Нужно хотя бы 3 пункта, чтобы было из чего выбирать.",
      pick_exactly_3: "Нужно выбрать ровно 3.",
      intent_required: "Нужно «зачем» для каждого приоритета — хотя бы пару предложений.",
      empty_priority: "В каждом приоритете нужна хотя бы одна тактика.",
      limit: "Это максимум — меньше, но лучше.",
      bad_weeks: "Выбери хотя бы одну неделю.",
      pick_days: "Выбери хотя бы один день недели.",
      bad_days: "Выбери хотя бы один день недели.",
    };
    tg.showAlert(messages[e.detail] || "Не получилось. Проверь интернет и попробуй ещё раз.");
  }

  // ---------- старт ----------

  async function start() {
    if (!tg || !tg.initData) {
      showError();
      return;
    }
    tg.ready();
    tg.expand();
    applyTheme();
    tg.onEvent("themeChanged", applyTheme);
    $("#edit-wheel").addEventListener("click", showWheel);
    $("#show-wheel").addEventListener("click", async () => {
      try { loadWheelState(await api("GET", "/api/wheel")); } catch (e) { return failed(e); }
      showWheelDone();
    });
    $("#explore-form").addEventListener("submit", addExplore);
    const input = $("#explore-input");
    input.addEventListener("input", () => autosize(input));
    // Enter — добавить; Shift+Enter — новая строка (для вставки списка)
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) addExplore(ev);
    });
    $("#back-to-explore").addEventListener("click", showExplore);
    $("#tactic-text").addEventListener("input", () => { autosize($("#tactic-text")); renderEditor(); });
    document.querySelectorAll("#schedule-mode button").forEach((b) => b.addEventListener("click", () => {
      const e = state.edit;
      e.mode = b.dataset.mode;
      if (e.mode === "one" && e.weeks.size > 1) e.weeks = new Set([Math.min(...e.weeks)]);
      if (e.mode === "every") e.weeks = new Set();
      else e.days = new Set();
      haptic.tick();
      renderEditor();
    }));
    $("#delete-tactic").addEventListener("click", deleteTactic);
    $("#edit-plan").addEventListener("click", showTactics);
    $("#toggle-extras").addEventListener("click", () => {
      const box = $("#extra-chips");
      box.hidden = !box.hidden;
    });
    $("#open-plan").addEventListener("click", openPlanView);
    $("#plan-edit").addEventListener("click", showTactics);
    $("#plan-reselect").addEventListener("click", () => {
      tg.showConfirm("Выбрать 3 приоритета заново? Тактики нынешних приоритетов удалятся, команда останется.", async (ok) => {
        if (!ok) return;
        try {
          state.explore = await api("POST", "/api/plan/reselect");
          state.me.step = "eliminate";
          showEliminate();
        } catch (e) { failed(e); }
      });
    });
    ["#prio-title", "#prio-intent"].forEach((sel) => $(sel).addEventListener("input", refreshPriorityEditor));
    try {
      state.me = await api("GET", "/api/me");
      document.querySelectorAll("[data-name]").forEach((n) => (n.textContent = state.me.first_name || ""));
      const step = state.me.step;
      if (step === "wheel" || step === "explore") {
        loadWheelState(await api("GET", "/api/wheel"));
      }
      if (step === "wheel") {
        Object.keys(state.wheel.scores).length ? showWheel() : showWelcome();
      } else if (step === "explore") {
        showExplore();
      } else if (step === "eliminate") {
        showEliminate();
      } else if (step === "intent") {
        showIntent();
      } else if (step === "tactics") {
        showTactics();
      } else if (step === "done") {
        showHome();
      } else {
        $("#later-text").textContent = "12 недель позади — итоги и старт нового цикла пока в чате с ботом.";
        show("screen-later");
        backButton(null);
        mainButton("Вернуться в чат", () => tg.close());
      }
    } catch (e) {
      showError(e.status === 401 ? "Сессия устарела — закройте приложение и откройте его снова из бота." : "Не получилось загрузиться. Попробуйте открыть приложение ещё раз.");
    }
  }

  start();
})();
