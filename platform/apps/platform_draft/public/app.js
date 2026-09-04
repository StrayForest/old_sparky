import { HEROES, HERO_BY_ID } from "./heroes.js";
import {
  PRESETS,
  applyLocalAction,
  buildRules,
  createLocalRoom,
  currentStep,
  decodeResult,
  encodeResult,
  heroAlreadyUsed,
  pauseLocalOnTimeout,
  resumeLocalTurn
} from "./draft-core.js";

const app = document.querySelector("#app");
const SOLO_KEY = "oldsparky:draft:solo";
const SEAT_KEY_PREFIX = "oldsparky:draft:seat:";
const INVITE_KEY_PREFIX = "oldsparky:draft:invite:";

let createMode = "online";
let selectedPreset = "community-6v6";
let selectedTimer = 30;
let room = null;
let selectedHeroId = null;
let heroSearch = "";
let runtimeMode = null;
let roomCode = null;
let seat = { role: "spectator", token: null };
let socket = null;
let connected = false;
let reconnectAttempt = 0;
let reconnectHandle = null;
let timerHandle = null;
let timeoutSentVersion = null;
let lastError = "";

window.addEventListener("popstate", () => route());
window.addEventListener("beforeunload", () => closeSocket());

route();

function route() {
  stopTimer();
  closeSocket();
  selectedHeroId = null;
  heroSearch = "";
  lastError = "";

  const path = normalizePath(location.pathname);
  if (path === "/draft" || path === "/draft/") {
    runtimeMode = null;
    room = null;
    renderCreate();
    return;
  }
  if (path === "/draft/solo") {
    runtimeMode = "solo";
    roomCode = "solo";
    const stored = readJson(sessionStorage.getItem(SOLO_KEY));
    if (!stored) {
      navigate("/draft");
      return;
    }
    room = stored;
    seat = { role: "solo", token: null };
    renderRoom();
    startTimer();
    return;
  }
  if (path === "/draft/result") {
    runtimeMode = "result";
    renderResult();
    return;
  }
  const match = path.match(/^\/draft\/([A-Za-z0-9_-]{6,32})\/?$/u);
  if (match) {
    runtimeMode = "online";
    roomCode = match[1];
    room = null;
    seat = recoverSeat(roomCode);
    renderLoadingRoom();
    connectRoom();
    return;
  }
  renderNotFound();
}

function renderBrand() {
  return `
    <div class="draft-brand">
      <div class="draft-brand__identity">
        <span class="draft-brand__name">OLD SPARKY</span>
        <span class="draft-brand__section">DRAFT</span>
      </div>
      <a class="draft-brand__back" href="/">← На Old Sparky</a>
    </div>
  `;
}

function renderCreate() {
  const preset = PRESETS[selectedPreset];
  app.innerHTML = `
    ${renderBrand()}
    <section class="create-layout">
      <div class="panel create-main">
        <p class="eyebrow">Deadlock picks & bans</p>
        <h1>Драфт без лишнего</h1>
        <p class="create-lead">Создай комнату, отправь ссылку сопернику и пикайте в реальном времени. Аккаунт и турнир не нужны.</p>

        <div class="segmented" aria-label="Режим драфта">
          <button type="button" data-mode="online" class="${createMode === "online" ? "active" : ""}">Онлайн</button>
          <button type="button" data-mode="solo" class="${createMode === "solo" ? "active" : ""}">Соло</button>
        </div>

        <div class="form-grid">
          <div class="field field--wide">
            <label for="preset">Шаблон</label>
            <select id="preset">
              ${Object.values(PRESETS).map((item) => `<option value="${item.id}" ${item.id === selectedPreset ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
            </select>
          </div>

          <div class="field field--wide">
            <span class="field-title">Таймер хода</span>
            <div class="chip-row" id="timer-options">
              ${[0, 30, 45, 60, 90].map((seconds) => `<button type="button" data-timer="${seconds}" class="${seconds === selectedTimer ? "active" : ""}">${seconds === 0 ? "Выкл" : `${seconds}с`}</button>`).join("")}
            </div>
          </div>

          <div class="field">
            <label for="team-a">Команда A</label>
            <input id="team-a" maxlength="40" placeholder="Команда A" autocomplete="off" />
          </div>
          <div class="field">
            <label for="team-b">Команда B</label>
            <input id="team-b" maxlength="40" placeholder="Команда B" autocomplete="off" />
          </div>
        </div>

        <div class="create-actions">
          <button id="create-draft" class="primary-button" type="button">${createMode === "online" ? "Создать онлайн-драфт" : "Начать соло-драфт"}</button>
        </div>
        <div id="create-error"></div>
      </div>

      <aside class="panel create-summary">
        <h2>Правила</h2>
        ${renderRulesSummary({ ...preset, timerSeconds: selectedTimer })}
        <div class="notice" style="margin-top:18px">${createMode === "online" ? "Онлайн-комната живёт только пока она нужна. История на сервере не сохраняется." : "Соло работает только в этом браузере и не создаёт сетевую комнату."}</div>
      </aside>
    </section>
    <div class="ad-zone" aria-label="Реклама"></div>
  `;

  app.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      createMode = button.dataset.mode;
      renderCreate();
    });
  });
  app.querySelector("#preset").addEventListener("change", (event) => {
    selectedPreset = event.target.value;
    selectedTimer = PRESETS[selectedPreset].timerSeconds;
    renderCreate();
  });
  app.querySelectorAll("[data-timer]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedTimer = Number(button.dataset.timer);
      renderCreate();
    });
  });
  app.querySelector("#create-draft").addEventListener("click", () => void createDraft());
}

async function createDraft() {
  const button = app.querySelector("#create-draft");
  const error = app.querySelector("#create-error");
  button.disabled = true;
  error.innerHTML = "";
  const teamNames = {
    A: app.querySelector("#team-a").value,
    B: app.querySelector("#team-b").value
  };
  const rules = buildRules(selectedPreset, selectedTimer);

  try {
    if (createMode === "solo") {
      room = createLocalRoom(rules, teamNames);
      sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
      history.pushState({}, "", "/draft/solo");
      route();
      return;
    }

    const response = await fetch("/draft/api/rooms", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ presetId: selectedPreset, timerSeconds: selectedTimer, teamNames })
    });
    const payload = await safeJson(response);
    if (!response.ok) {
      throw new Error(payload?.error || "Не удалось создать комнату");
    }
    const code = payload.roomCode;
    sessionStorage.setItem(`${SEAT_KEY_PREFIX}${code}`, JSON.stringify({ role: "host", token: payload.hostToken }));
    sessionStorage.setItem(`${INVITE_KEY_PREFIX}${code}`, payload.guestToken);
    history.pushState({}, "", `/draft/${encodeURIComponent(code)}`);
    route();
  } catch (cause) {
    error.innerHTML = `<div class="error-box" style="margin-top:14px">${escapeHtml(errorMessage(cause))}</div>`;
    button.disabled = false;
  }
}

function renderRulesSummary(rules) {
  const banCount = rules.sequence.filter((step) => step.action === "ban").length;
  const pickCount = rules.sequence.filter((step) => step.action === "pick").length;
  const banSequence = rules.sequence.filter((step) => step.action === "ban").map((step) => step.side).join(" ") || "—";
  const pickSequence = rules.sequence.filter((step) => step.action === "pick").map((step) => step.side).join(" ");
  return `
    <div class="summary-list">
      <div class="summary-line"><span>Состав</span><strong>${rules.teamSize} × ${rules.teamSize}</strong></div>
      <div class="summary-line"><span>Баны</span><strong>${banCount}</strong></div>
      <div class="summary-line"><span>Пики</span><strong>${pickCount}</strong></div>
      <div class="summary-line"><span>Таймер</span><strong>${rules.timerSeconds ? `${rules.timerSeconds} сек` : "выключен"}</strong></div>
      <div class="summary-line"><span>BAN</span><strong>${banSequence}</strong></div>
      <div class="summary-line"><span>PICK</span><strong>${pickSequence}</strong></div>
    </div>
  `;
}

function renderLoadingRoom() {
  app.innerHTML = `
    ${renderBrand()}
    <section class="panel result-panel">
      <p class="eyebrow">Комната ${escapeHtml(roomCode || "")}</p>
      <h1 style="font-size:clamp(30px,5vw,52px)">Подключаем драфт…</h1>
      <p class="create-lead">Получаем актуальное состояние комнаты.</p>
    </section>
  `;
}

function renderRoom() {
  if (!room) {
    renderLoadingRoom();
    return;
  }
  if (room.status === "completed") {
    stopTimer();
  }

  const step = currentStep(room);
  const canAct = canCurrentSeatAct(step);
  const selectedHero = selectedHeroId ? HERO_BY_ID.get(selectedHeroId) : null;
  const filteredHeroes = HEROES.filter((hero) => hero.name.toLocaleLowerCase("ru").includes(heroSearch.toLocaleLowerCase("ru")));
  const timerText = formatTimer(room);
  const roleLabel = runtimeMode === "solo" ? "Соло" : seat.role === "host" ? "Команда A" : seat.role === "guest" ? "Команда B" : "Зритель";
  const activeSide = step?.side || null;
  const actionText = step?.action === "ban" ? "БАН" : step?.action === "pick" ? "ПИК" : room.status === "completed" ? "ЗАВЕРШЕНО" : "ПАУЗА";

  app.innerHTML = `
    ${renderBrand()}
    <div class="room-shell">
      ${lastError ? `<div class="error-box">${escapeHtml(lastError)}</div>` : ""}
      <div class="room-topbar">
        <div class="room-meta">
          ${runtimeMode === "online" ? `<span class="connection-dot ${connected ? "connected" : ""}" aria-hidden="true"></span>` : ""}
          <span class="room-code">${runtimeMode === "solo" ? "Локальный драфт" : `Комната ${escapeHtml(roomCode)}`}</span>
          <span class="room-code">· ${roleLabel}</span>
        </div>
        <div class="turn-center">
          <span class="turn-label ${activeSide === "A" ? "team-a" : activeSide === "B" ? "team-b" : ""}">${activeSide ? `${escapeHtml(room.teamNames[activeSide])} — ${actionText}` : actionText}</span>
          <span class="timer" id="draft-timer">${timerText}</span>
        </div>
        <div class="room-actions">
          ${runtimeMode === "online" && seat.role === "host" ? `<button class="secondary-button" id="copy-opponent" type="button">Ссылка сопернику</button>` : ""}
          ${runtimeMode === "online" ? `<button class="secondary-button" id="copy-watch" type="button">Ссылка зрителю</button>` : ""}
          <button class="icon-button" id="new-draft" type="button">Новый</button>
        </div>
      </div>

      <div class="mobile-compositions">
        ${renderMobileTeam("A")}
        ${renderMobileTeam("B")}
      </div>

      <div class="draft-layout">
        ${renderTeamPanel("A")}
        <section class="panel hero-panel">
          <div class="hero-toolbar">
            <input id="hero-search" class="hero-search" type="search" autocomplete="off" placeholder="Найти героя…" value="${escapeAttr(heroSearch)}" />
          </div>
          <div class="hero-grid">
            ${filteredHeroes.map((hero) => renderHeroCard(hero, canAct)).join("")}
          </div>
        </section>
        ${renderTeamPanel("B")}
      </div>

      <div class="panel sequence-bar" aria-label="Порядок драфта">
        ${room.rules.sequence.map((sequenceStep, index) => {
          const classes = ["sequence-step"];
          if (index < room.currentStep) classes.push("done");
          if (index === room.currentStep && room.status === "drafting") classes.push("current");
          return `<span class="${classes.join(" ")}" title="${sequenceStep.action === "ban" ? "Бан" : "Пик"} команды ${sequenceStep.side}">${sequenceStep.action === "ban" ? "B" : "P"}${sequenceStep.side}</span>`;
        }).join("")}
      </div>

      ${room.status === "completed" ? renderCompletedActions() : renderActionBar(selectedHero, step, canAct)}
      <div class="ad-zone" aria-label="Реклама"></div>
    </div>
  `;

  attachRoomEvents(canAct);
  attachImageFallbacks();
}

function renderTeamPanel(side) {
  const picks = room.picks[side];
  const bans = room.bans[side];
  const emptyPicks = Math.max(0, room.rules.teamSize - picks.length);
  return `
    <aside class="panel team-panel team-${side.toLowerCase()}">
      <div class="team-heading">
        <h2 class="team-name">${escapeHtml(room.teamNames[side])}</h2>
        <span class="team-side">TEAM ${side}</span>
      </div>
      <div class="team-block">
        <h3>Пики</h3>
        <div class="mini-list">
          ${picks.map(renderMiniHero).join("")}
          ${Array.from({ length: emptyPicks }, () => `<div class="empty-slot">Свободный слот</div>`).join("")}
        </div>
      </div>
      <div class="team-block">
        <h3>Баны</h3>
        <div class="mini-list">
          ${bans.length ? bans.map(renderMiniHero).join("") : `<div class="empty-slot">Пока нет</div>`}
        </div>
      </div>
    </aside>
  `;
}

function renderMobileTeam(side) {
  const picks = room.picks[side].map((id) => HERO_BY_ID.get(id)?.name || id).join(", ") || "пиков нет";
  const bans = room.bans[side].map((id) => HERO_BY_ID.get(id)?.name || id).join(", ") || "банов нет";
  return `<div class="mobile-team-summary"><strong>${escapeHtml(room.teamNames[side])}</strong><span>${escapeHtml(picks)} · ${escapeHtml(bans)}</span></div>`;
}

function renderMiniHero(heroId) {
  const hero = HERO_BY_ID.get(heroId);
  if (!hero) return "";
  return `<div class="mini-hero"><img src="${hero.image}" alt="" width="26" height="26" loading="lazy" /><span>${escapeHtml(hero.name)}</span></div>`;
}

function renderHeroCard(hero, canAct) {
  const state = heroUsageState(hero.id);
  const used = state !== "available";
  const selected = hero.id === selectedHeroId;
  const disabled = used || !canAct || room.timedOut || room.status !== "drafting";
  const stateLabel = state === "banned" ? "БАН" : state === "picked-a" ? "A" : state === "picked-b" ? "B" : "";
  return `
    <button
      class="hero-card ${selected ? "selected" : ""} ${used ? "used" : ""}"
      type="button"
      data-hero="${hero.id}"
      ${disabled ? "disabled" : ""}
      aria-label="${escapeAttr(hero.name)}${stateLabel ? ` — ${stateLabel}` : ""}"
    >
      <img src="${hero.image}" alt="" width="192" height="224" loading="lazy" decoding="async" />
      ${stateLabel ? `<span class="hero-state ${state}">${stateLabel}</span>` : ""}
      <span class="hero-name">${escapeHtml(hero.name)}</span>
    </button>
  `;
}

function heroUsageState(heroId) {
  if (room.bans.A.includes(heroId) || room.bans.B.includes(heroId)) return "banned";
  if (room.picks.A.includes(heroId)) return "picked-a";
  if (room.picks.B.includes(heroId)) return "picked-b";
  return "available";
}

function renderActionBar(selectedHero, step, canAct) {
  const action = step?.action === "ban" ? "ЗАБАНИТЬ" : "ВЫБРАТЬ";
  const unavailableReason = room.timedOut
    ? "Время вышло — возобновите ход"
    : !step
      ? "Нет активного хода"
      : !canAct
        ? seat.role === "spectator" ? "Режим зрителя" : "Сейчас ход другой команды"
        : "Выберите героя";
  return `
    <div class="action-bar">
      <div class="selected-summary">
        ${selectedHero ? `<img src="${selectedHero.image}" alt="" width="46" height="46" /><div><strong>${escapeHtml(selectedHero.name)}</strong><br /><span>${step?.action === "ban" ? "Будет забанен" : "Будет выбран"}</span></div>` : `<span>${escapeHtml(unavailableReason)}</span>`}
      </div>
      ${room.timedOut && canAct ? `<button id="resume-turn" class="secondary-button" type="button">Продолжить таймер</button>` : `<button id="confirm-action" class="confirm-button" type="button" ${!selectedHero || !canAct ? "disabled" : ""}>${selectedHero ? `${action} ${escapeHtml(selectedHero.name)}` : action}</button>`}
    </div>
  `;
}

function renderCompletedActions() {
  return `
    <div class="action-bar">
      <div class="selected-summary"><strong>Драфт завершён</strong></div>
      <button id="open-result" class="confirm-button" type="button">Открыть результат</button>
    </div>
  `;
}

function attachRoomEvents(canAct) {
  const search = app.querySelector("#hero-search");
  if (search) {
    search.addEventListener("input", (event) => {
      heroSearch = event.target.value.slice(0, 60);
      renderRoom();
      const nextSearch = app.querySelector("#hero-search");
      nextSearch?.focus({ preventScroll: true });
      if (nextSearch) nextSearch.setSelectionRange(heroSearch.length, heroSearch.length);
    });
  }

  app.querySelectorAll("[data-hero]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!canAct || button.disabled) return;
      selectedHeroId = button.dataset.hero;
      renderRoom();
    });
  });

  app.querySelector("#confirm-action")?.addEventListener("click", () => void confirmAction());
  app.querySelector("#resume-turn")?.addEventListener("click", () => resumeTurn());
  app.querySelector("#copy-opponent")?.addEventListener("click", () => void copyOpponentLink());
  app.querySelector("#copy-watch")?.addEventListener("click", () => void copyText(`${location.origin}/draft/${roomCode}`, "Ссылка зрителя скопирована"));
  app.querySelector("#new-draft")?.addEventListener("click", () => {
    if (runtimeMode === "solo") sessionStorage.removeItem(SOLO_KEY);
    navigate("/draft");
  });
  app.querySelector("#open-result")?.addEventListener("click", () => {
    const encoded = encodeResult(room);
    history.pushState({}, "", `/draft/result#v1.${encoded}`);
    route();
  });
}

async function confirmAction() {
  if (!selectedHeroId || !room) return;
  if (runtimeMode === "solo") {
    room = applyLocalAction(room, selectedHeroId);
    selectedHeroId = null;
    sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
    timeoutSentVersion = null;
    renderRoom();
    startTimer();
    return;
  }
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    lastError = "Соединение ещё не восстановлено";
    renderRoom();
    return;
  }
  socket.send(JSON.stringify({ type: "action", expectedVersion: room.version, heroId: selectedHeroId }));
}

function resumeTurn() {
  if (runtimeMode === "solo") {
    room = resumeLocalTurn(room);
    sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
    timeoutSentVersion = null;
    renderRoom();
    startTimer();
    return;
  }
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resume", expectedVersion: room.version }));
  }
}

function canCurrentSeatAct(step) {
  if (!step || room.status !== "drafting") return false;
  if (runtimeMode === "solo") return true;
  return (step.side === "A" && seat.role === "host") || (step.side === "B" && seat.role === "guest");
}

function connectRoom() {
  closeSocket();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/draft/ws/${encodeURIComponent(roomCode)}`);

  socket.addEventListener("open", () => {
    connected = true;
    reconnectAttempt = 0;
    lastError = "";
    socket.send(JSON.stringify({ type: "hello", role: seat.role, token: seat.token }));
    if (room) renderRoom();
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "state" && message.room) {
      if (!room || message.room.version >= room.version) {
        room = message.room;
        selectedHeroId = null;
        timeoutSentVersion = null;
        lastError = "";
        renderRoom();
        startTimer();
      }
      return;
    }
    if (message.type === "error") {
      lastError = message.error || "Действие отклонено";
      if (message.room && (!room || message.room.version >= room.version)) room = message.room;
      renderRoom();
    }
  });

  socket.addEventListener("close", (event) => {
    connected = false;
    if (event.code === 4404) {
      lastError = "Драфт больше не существует";
      renderExpiredRoom();
      return;
    }
    if (event.code === 4403) {
      sessionStorage.removeItem(`${SEAT_KEY_PREFIX}${roomCode}`);
      seat = { role: "spectator", token: null };
      lastError = "Ссылка капитана недействительна. Открыт режим зрителя.";
    }
    if (room) renderRoom();
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    connected = false;
  });
}

function scheduleReconnect() {
  if (runtimeMode !== "online" || reconnectHandle) return;
  const delay = Math.min(10000, 500 * 2 ** reconnectAttempt) + Math.floor(Math.random() * 350);
  reconnectAttempt = Math.min(reconnectAttempt + 1, 6);
  reconnectHandle = window.setTimeout(() => {
    reconnectHandle = null;
    connectRoom();
  }, delay);
}

function closeSocket() {
  if (reconnectHandle) {
    clearTimeout(reconnectHandle);
    reconnectHandle = null;
  }
  if (socket) {
    socket.onclose = null;
    socket.close(1000, "navigation");
    socket = null;
  }
  connected = false;
}

function recoverSeat(code) {
  const hash = new URLSearchParams(location.hash.replace(/^#/u, ""));
  const joinToken = hash.get("join");
  if (joinToken && joinToken.length <= 256) {
    const recovered = { role: "guest", token: joinToken };
    sessionStorage.setItem(`${SEAT_KEY_PREFIX}${code}`, JSON.stringify(recovered));
    history.replaceState({}, "", `/draft/${encodeURIComponent(code)}`);
    return recovered;
  }
  const stored = readJson(sessionStorage.getItem(`${SEAT_KEY_PREFIX}${code}`));
  if (stored && ["host", "guest"].includes(stored.role) && typeof stored.token === "string") return stored;
  return { role: "spectator", token: null };
}

async function copyOpponentLink() {
  const token = sessionStorage.getItem(`${INVITE_KEY_PREFIX}${roomCode}`);
  if (!token) {
    lastError = "Секрет приглашения недоступен в этой вкладке. Создайте новую комнату, если нужна новая ссылка.";
    renderRoom();
    return;
  }
  await copyText(`${location.origin}/draft/${roomCode}#join=${encodeURIComponent(token)}`, "Ссылка сопернику скопирована");
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
    lastError = "";
    showTransientNotice(successMessage);
  } catch {
    window.prompt("Скопируйте ссылку:", value);
  }
}

function showTransientNotice(message) {
  const node = document.createElement("div");
  node.className = "notice";
  node.style.position = "fixed";
  node.style.right = "14px";
  node.style.bottom = "14px";
  node.style.zIndex = "60";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 1800);
}

function startTimer() {
  stopTimer();
  if (!room || room.status !== "drafting" || !room.turnDeadlineAt || room.timedOut) return;
  updateTimer();
  timerHandle = window.setInterval(updateTimer, 250);
}

function stopTimer() {
  if (timerHandle) {
    clearInterval(timerHandle);
    timerHandle = null;
  }
}

function updateTimer() {
  if (!room || !room.turnDeadlineAt || room.status !== "drafting") {
    stopTimer();
    return;
  }
  const remaining = Math.max(0, room.turnDeadlineAt - Date.now());
  const timer = document.querySelector("#draft-timer");
  if (timer) timer.textContent = formatMilliseconds(remaining);
  if (remaining > 0 || timeoutSentVersion === room.version) return;
  timeoutSentVersion = room.version;
  if (runtimeMode === "solo") {
    room = pauseLocalOnTimeout(room);
    sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
    renderRoom();
    stopTimer();
    return;
  }
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "timeout", expectedVersion: room.version }));
  }
}

function formatTimer(value) {
  if (room.timedOut) return "ПАУЗА";
  if (!value.turnDeadlineAt) return "∞";
  return formatMilliseconds(Math.max(0, value.turnDeadlineAt - Date.now()));
}

function formatMilliseconds(ms) {
  const totalSeconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function renderResult() {
  const raw = location.hash.replace(/^#v1\./u, "");
  let payload;
  try {
    payload = decodeResult(raw);
  } catch (cause) {
    app.innerHTML = `${renderBrand()}<section class="panel result-panel"><div class="error-box">${escapeHtml(errorMessage(cause))}</div><div class="result-actions"><a class="secondary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">Создать новый драфт</a></div></section>`;
    return;
  }

  app.innerHTML = `
    ${renderBrand()}
    <section class="panel result-panel">
      <p class="eyebrow">Результат</p>
      <h1 style="font-size:clamp(34px,6vw,58px)">Драфт завершён</h1>
      <p class="create-lead">Результат хранится прямо в этой ссылке. Сервер не хранит историю комнаты.</p>
      <div class="result-grid">
        ${renderResultTeam(payload.a || "Команда A", payload.pa, payload.ba, "A")}
        ${renderResultTeam(payload.b || "Команда B", payload.pb, payload.bb, "B")}
      </div>
      <div class="result-actions">
        <button id="copy-result" class="primary-button" type="button">Скопировать ссылку</button>
        <a class="secondary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">Новый драфт</a>
      </div>
      <div class="ad-zone" aria-label="Реклама"></div>
    </section>
  `;
  app.querySelector("#copy-result").addEventListener("click", () => void copyText(location.href, "Ссылка результата скопирована"));
  attachImageFallbacks();
}

function renderResultTeam(name, picks, bans, side) {
  const cleanPicks = picks.filter((id) => HERO_BY_ID.has(id)).slice(0, 12);
  const cleanBans = bans.filter((id) => HERO_BY_ID.has(id)).slice(0, 12);
  return `
    <section class="result-team team-${side.toLowerCase()}">
      <div class="team-heading"><h2 class="team-name">${escapeHtml(String(name).slice(0, 40))}</h2><span class="team-side">TEAM ${side}</span></div>
      <div class="team-block"><h3>Пики</h3><div class="mini-list">${cleanPicks.length ? cleanPicks.map(renderMiniHeroFromId).join("") : `<div class="empty-slot">Нет</div>`}</div></div>
      <div class="team-block"><h3>Баны</h3><div class="mini-list">${cleanBans.length ? cleanBans.map(renderMiniHeroFromId).join("") : `<div class="empty-slot">Нет</div>`}</div></div>
    </section>
  `;
}

function renderMiniHeroFromId(heroId) {
  const hero = HERO_BY_ID.get(heroId);
  return hero ? `<div class="mini-hero"><img src="${hero.image}" alt="" width="26" height="26" loading="lazy" /><span>${escapeHtml(hero.name)}</span></div>` : "";
}

function renderExpiredRoom() {
  stopTimer();
  app.innerHTML = `
    ${renderBrand()}
    <section class="panel result-panel">
      <p class="eyebrow">Комната недоступна</p>
      <h1 style="font-size:clamp(32px,6vw,56px)">Драфт больше не существует</h1>
      <p class="create-lead">Комнаты временные и удаляются после завершения или простоя.</p>
      <div class="result-actions"><a class="primary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">Создать новый</a></div>
    </section>
  `;
}

function renderNotFound() {
  app.innerHTML = `${renderBrand()}<section class="panel result-panel"><h1 style="font-size:clamp(32px,6vw,56px)">Страница не найдена</h1><div class="result-actions"><a class="primary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">К драфтам</a></div></section>`;
}

function attachImageFallbacks() {
  app.querySelectorAll("img").forEach((image) => {
    image.addEventListener("error", () => {
      image.style.visibility = "hidden";
    }, { once: true });
  });
}

function navigate(path) {
  history.pushState({}, "", path);
  route();
}

function normalizePath(value) {
  const path = value.replace(/\/{2,}/gu, "/");
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
}

function readJson(value) {
  try {
    return value ? JSON.parse(value) : null;
  } catch {
    return null;
  }
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function errorMessage(value) {
  return value instanceof Error ? value.message : "Неизвестная ошибка";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
