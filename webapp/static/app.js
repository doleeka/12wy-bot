/* Mini App «12 недель»: приветствие и колесо баланса. Чистый JS, без сборки. */
(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (sel) => document.querySelector(sel);
  const state = { me: null, wheel: null, scores: {}, touched: new Set(), extras: [] };
  let mainHandler = null;
  let backHandler = null;

  // ---------- Telegram ----------

  function applyTheme() {
    if (!tg) return;
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
  }

  function mainButton(text, onClick, enabled = true) {
    if (!tg) return;
    const mb = tg.MainButton;
    if (mainHandler) mb.offClick(mainHandler);
    mainHandler = onClick;
    mb.setText(text);
    enabled ? mb.enable() : mb.disable();
    mb.setParams({ is_active: enabled, color: enabled ? tg.themeParams.button_color : tg.themeParams.hint_color });
    mb.onClick(onClick);
    mb.show();
  }

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
    mainButton("Начать с колеса баланса", showWheel);
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
      row.querySelector(".sphere-value").textContent = rated ? state.scores[s.key] : "—";
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
    const left = unratedCount();
    mainButton(left ? "Оцени ещё " + left : "Сохранить колесо", saveWheel, left === 0);
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
    $("#avg").textContent = String(w.average).replace(".", ",");

    const lows = w.lows.map((k) => sphereByKey(k).emoji + " " + sphereByKey(k).title + " (" + w.scores[k] + ")");
    $("#lows").innerHTML = "<b>Где просадки</b><p></p>";
    $("#lows p").textContent = lows.join(", ");

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
    mainButton("Вернуться в чат", () => tg.close());
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
    try {
      state.me = await api("GET", "/api/me");
      document.querySelectorAll("[data-name]").forEach((n) => (n.textContent = state.me.first_name || ""));
      if (state.me.step === "wheel" || state.me.step === "explore") {
        loadWheelState(await api("GET", "/api/wheel"));
      }
      if (state.me.step === "wheel") {
        Object.keys(state.wheel.scores).length ? showWheel() : showWelcome();
      } else if (state.me.step === "explore") {
        showWheelDone();
      } else {
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
