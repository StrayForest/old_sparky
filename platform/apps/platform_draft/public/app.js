import { HEROES, HERO_BY_ID } from "./heroes.js";
import {
  BAN_COUNTS,
  DEFAULT_CUSTOM_RULES,
  MAX_BANS_PER_TEAM,
  MAX_TEAM_NAME_LENGTH,
  TEAM_SIZES,
  TIMER_SECONDS,
  applyLocalAction,
  applyLocalTimeout,
  buildRules,
  countSequenceActions,
  cycleSequenceCell,
  createDefaultSequence,
  createLocalRoom,
  currentStep
} from "./draft-core.js";

const app = document.querySelector("#app");
const SOLO_KEY = "oldsparky:draft:solo";
const SEAT_KEY_PREFIX = "oldsparky:draft:seat:";
const INVITE_KEY_PREFIX = "oldsparky:draft:invite:";
const COPY_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 9.5A2.5 2.5 0 0 1 11.5 7h6A2.5 2.5 0 0 1 20 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-6A2.5 2.5 0 0 1 9 17.5z"/><path d="M15 7V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v8A2.5 2.5 0 0 0 6.5 17H9"/></svg>`;
const teamLabel = (side) => side === "A" ? "А" : "Б";

let createMode = "online";
let selectedTeamSize = DEFAULT_CUSTOM_RULES.teamSize;
let selectedBanCount = DEFAULT_CUSTOM_RULES.banCount;
let selectedTimer = 30;
let selectedFirstMove = "A";
let customSequence = createEditorSequence(createDefaultSequence(selectedTeamSize, selectedBanCount, selectedFirstMove));
let room = null;
let selectedHeroId = null;
let runtimeMode = null;
let roomCode = null;
let seat = { role: "spectator", token: null };
let socket = null;
let connected = false;
let reconnectAttempt = 0;
let reconnectHandle = null;
let timerHandle = null;
let deadlineSentVersion = null;
let lastError = "";

window.addEventListener("popstate", () => route());
window.addEventListener("beforeunload", () => closeSocket());

route();

function route() {
  stopTimer();
  closeSocket();
  selectedHeroId = null;
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
  const match = path.match(/^\/draft\/(?!result\/?$)([A-Za-z0-9_-]{6,32})\/?$/u);
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

function currentCustomRules() {
  return {
    teamSize: selectedTeamSize,
    banCount: selectedBanCount,
    sequence: compactCreateSequence()
  };
}

function createEditorSequence(sequence) {
  const slotCount = selectedTeamSize * 2 + MAX_BANS_PER_TEAM * 2;
  return [...sequence, ...Array.from({ length: Math.max(0, slotCount - sequence.length) }, () => null)];
}

function compactCreateSequence() {
  const firstEmptyIndex = customSequence.findIndex((step) => !step);
  if (firstEmptyIndex >= 0 && customSequence.slice(firstEmptyIndex).some(Boolean)) {
    throw new Error("Заполните последовательность без пропусков между шагами");
  }
  return customSequence.filter(Boolean).map(({ action, side }) => ({ action, side }));
}

function renderSequenceEditor() {
  return `
    <div class="sequence-editor" aria-label="Настройка порядка банов и пиков">
      <div class="sequence-editor__header"><span>Последовательность пиков и банов</span><span class="sequence-editor__hint">Нажмите ячейку: пик → бан → пусто</span></div>
      <div class="sequence-editor__scroll">
        <div class="sequence-editor__grid" style="--sequence-length:${customSequence.length}">
          <div class="sequence-track sequence-track--numbers sequence-track--editable">${customSequence.map((_, index) => `<span>${index + 1}</span>`).join("")}<span></span><span></span></div>
          ${renderSequenceTrack("A")}
          ${renderSequenceTrack("B")}
        </div>
      </div>
    </div>
  `;
}

function renderSequenceTrack(side) {
  return `<div class="sequence-track sequence-track--${side.toLowerCase()} sequence-track--editable">${customSequence.map((step, index) => {
    const active = step?.side === side;
    const state = active ? step.action : "empty";
    const symbol = state === "ban" ? "×" : state === "pick" ? "✓" : "";
    const label = active ? `${state === "ban" ? "Бан" : "Пик"}, шаг ${index + 1}` : `Пусто, шаг ${index + 1}`;
    return `<button class="sequence-editor-step ${state}" type="button" data-sequence-index="${index}" data-sequence-side="${side}" aria-label="${label}"><span aria-hidden="true">${symbol}</span></button>`;
  }).join("")}${renderCreateSequenceCounters(side)}</div>`;
}

function renderCreateSequenceCounters(side) {
  const counts = countSequenceActions(customSequence, side);
  return `
    <span class="sequence-counter sequence-counter--pick" aria-label="Пики ${counts.pick} из ${selectedTeamSize}">${counts.pick}/${selectedTeamSize}</span>
    <span class="sequence-counter sequence-counter--ban" aria-label="Баны ${counts.ban} из ${selectedBanCount}">${counts.ban}/${selectedBanCount}</span>
  `;
}

function renderCreate() {
  app.innerHTML = `
    ${renderBrand()}
    <section class="create-layout">
      <div class="panel create-main">
        <p class="eyebrow">Deadlock picks & bans</p>
        <h1>Драфты</h1>
        <p class="create-lead">Создай комнату, отправь ссылку сопернику и пикайте в реальном времени.</p>

        <div class="segmented" aria-label="Режим драфта">
          <button type="button" data-mode="online" class="${createMode === "online" ? "active" : ""}">Стандарт</button>
          <button type="button" data-mode="solo" class="${createMode === "solo" ? "active" : ""}">Соло</button>
        </div>

        <div class="form-grid">
          <div class="field field--wide">
            <span class="field-title">Формат</span>
            <div class="chip-row" role="group" aria-label="Формат команды">
              ${TEAM_SIZES.map((size) => `<button type="button" data-team-size="${size}" class="${size === selectedTeamSize ? "active" : ""}">${size}v${size}</button>`).join("")}
            </div>
          </div>

          <div class="field field--wide">
            <span class="field-title">Баны на команду</span>
            <div class="chip-row" role="group" aria-label="Количество банов">
              ${BAN_COUNTS.map((count) => `<button type="button" data-ban-count="${count}" class="${count === selectedBanCount ? "active" : ""}">${count === 0 ? "—" : count}</button>`).join("")}
            </div>
          </div>

          <div class="field field--wide">
            <span class="field-title">Таймер хода</span>
            <div class="chip-row" role="group" aria-label="Таймер хода">
              ${TIMER_SECONDS.map((seconds) => `<button type="button" data-timer="${seconds}" class="${seconds === selectedTimer ? "active" : ""}">${seconds}с</button>`).join("")}
            </div>
          </div>

          <div class="field field--wide">
            <span class="field-title">Первый ход</span>
            <div class="chip-row" role="group" aria-label="Команда первого хода">
              <button type="button" data-first-move="A" class="${selectedFirstMove === "A" ? "active" : ""}">${teamLabel("A")}</button>
              <button type="button" data-first-move="B" class="${selectedFirstMove === "B" ? "active" : ""}">${teamLabel("B")}</button>
              <button type="button" data-first-move="random" class="${selectedFirstMove === "random" ? "active" : ""}">Рандом</button>
            </div>
          </div>
        </div>

        <div class="sequence-editor-wrap">
          ${renderSequenceEditor()}
        </div>

        <div class="create-actions">
          <button id="create-draft" class="primary-button" type="button">${createMode === "online" ? "Создать комнату" : "Начать соло-драфт"}</button>
        </div>
        <div id="create-error"></div>
      </div>
    </section>
    <div class="ad-zone" aria-label="Реклама"></div>
  `;

  app.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      createMode = button.dataset.mode;
      renderCreate();
    });
  });
  app.querySelectorAll("[data-team-size]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedTeamSize = Number(button.dataset.teamSize);
      resetCreateSequence();
      renderCreate();
    });
  });
  app.querySelectorAll("[data-ban-count]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedBanCount = Number(button.dataset.banCount);
      resetCreateSequence();
      renderCreate();
    });
  });
  app.querySelectorAll("[data-first-move]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedFirstMove = button.dataset.firstMove;
      renderCreate();
    });
  });
  app.querySelectorAll("[data-timer]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedTimer = Number(button.dataset.timer);
      renderCreate();
    });
  });

  app.querySelectorAll("[data-sequence-index]").forEach((button) => {
    button.addEventListener("click", () => {
      cycleCreateSequence(Number(button.dataset.sequenceIndex), button.dataset.sequenceSide);
      renderCreate();
    });
  });
  app.querySelector("#create-draft").addEventListener("click", () => void createDraft());
}

function resetCreateSequence() {
  const firstSide = selectedFirstMove === "B" ? "B" : "A";
  customSequence = createEditorSequence(createDefaultSequence(selectedTeamSize, selectedBanCount, firstSide));
}

function cycleCreateSequence(index, side) {
  customSequence = cycleSequenceCell(customSequence, index, side, {
    pickLimit: selectedTeamSize,
    banLimit: selectedBanCount
  });
}

async function createDraft() {
  const button = app.querySelector("#create-draft");
  const error = app.querySelector("#create-error");
  button.disabled = true;
  error.innerHTML = "";

  try {
    const customRules = currentCustomRules();
    const firstMove = resolveLocalFirstSide(selectedFirstMove);
    const rules = buildRules("standard", selectedTimer, { ...customRules, firstSide: firstMove });
    if (createMode === "solo") {
      room = createLocalRoom(rules);
      sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
      history.pushState({}, "", "/draft/solo");
      route();
      return;
    }

    const response = await fetch("/draft/api/rooms", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ presetId: "standard", timerSeconds: selectedTimer, firstMove: selectedFirstMove, customRules })
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

function resolveLocalFirstSide(value) {
  if (value === "B") return "B";
  if (value !== "random") return "A";
  const bytes = new Uint8Array(1);
  crypto.getRandomValues(bytes);
  return bytes[0] % 2 === 0 ? "A" : "B";
}

function renderLoadingRoom() {
  app.innerHTML = `
    ${renderBrand()}
    <section class="panel message-panel">
      <p class="eyebrow">Подключение</p>
      <h1 style="font-size:clamp(30px,5vw,52px)">Подключаем драфт…</h1>
      <p class="create-lead">Получаем актуальное состояние комнаты.</p>
    </section>
  `;
}

function renderLobby() {
  const ownSide = seat.role === "host" ? "A" : seat.role === "guest" ? "B" : null;
  const ready = room.ready || { A: false, B: false };
  const presence = room.presence || { A: false, B: false };
  const lobbyTeam = (side) => {
    const isOwn = ownSide === side;
    const connectedLabel = presence[side] ? (isOwn ? "Вы в комнате" : "Игрок подключён") : "Ожидаем игрока";
    const nameControl = !isOwn
      ? `<h2 class="lobby-team-name">${escapeHtml(room.teamNames[side])}</h2>`
      : `<label class="lobby-name-field"><span>Название команды</span><input data-team-name="${side}" maxlength="${MAX_TEAM_NAME_LENGTH}" value="${escapeAttr(room.teamNames[side])}" autocomplete="off" ${ready[side] ? "disabled" : ""} /></label>`;
    return `
      <section class="lobby-team team-${side.toLowerCase()} ${isOwn ? "lobby-team--own" : ""}">
        <div class="lobby-team__header"><span class="team-side">${teamLabel(side)}</span><span class="lobby-presence ${presence[side] ? "online" : ""}"><i aria-hidden="true"></i>${connectedLabel}</span></div>
        ${nameControl}
        <div class="lobby-team__footer">
          <span class="ready-status ${ready[side] ? "ready" : ""}"><i aria-hidden="true"></i>${ready[side] ? "Готово" : "Ожидаем готовность"}</span>
          ${isOwn && !ready[side] ? `<button type="button" class="primary-button" id="ready-up">Готов</button>` : ""}
        </div>
      </section>
    `;
  };
  const firstSide = teamLabel(room.rules.firstSide);
  app.innerHTML = `
    ${renderBrand()}
    <section class="lobby-shell">
      <div class="lobby-status"><i aria-hidden="true"></i>Ожидание игроков</div>
      <h1 class="lobby-title"><span class="team-a">${escapeHtml(room.teamNames.A)}</span><em>VS</em><span class="team-b">${escapeHtml(room.teamNames.B)}</span></h1>
      ${ownSide === "A" ? `<button class="opponent-link" id="copy-opponent" type="button">${COPY_ICON}<span>Ссылка сопернику</span></button>` : ""}
      <div class="lobby-meta"><span>${room.rules.teamSize}v${room.rules.teamSize}</span><span>${formatRoomBans(room.rules)}</span><span>${room.rules.timerSeconds}с таймер</span><span>Первый ход: ${firstSide}</span></div>
      ${lastError ? `<div class="error-box">${escapeHtml(lastError)}</div>` : ""}
      <div class="lobby-teams">${lobbyTeam("A")}${lobbyTeam("B")}</div>
    </section>
  `;
  app.querySelector("#ready-up")?.addEventListener("click", () => sendRoomMessage({ type: "ready", expectedVersion: room.version }));
  app.querySelector("[data-team-name]")?.addEventListener("change", (event) => {
    const input = event.target;
    sendRoomMessage({ type: "team-name", expectedVersion: room.version, name: input.value.slice(0, MAX_TEAM_NAME_LENGTH) });
  });
  app.querySelector("#copy-opponent")?.addEventListener("click", () => void copyOpponentLink());
}

function formatRoomBans(rules) {
  const counts = rules.banCounts || rules.sequence.reduce((result, step) => {
    if (step.action === "ban") result[step.side] += 1;
    return result;
  }, { A: 0, B: 0 });
  if (counts.A === counts.B) return `${counts.A} бан${counts.A === 1 ? "" : counts.A < 5 ? "а" : "ов"} на команду`;
  return `Баны ${counts.A}/${counts.B}`;
}

function renderRoom() {
  if (!room) {
    renderLoadingRoom();
    return;
  }
  if (room.status === "waiting") {
    renderLobby();
    return;
  }
  if (room.status === "completed") {
    stopTimer();
  }

  const step = currentStep(room);
  const canAct = canCurrentSeatAct(step);
  const selectedHero = selectedHeroId ? HERO_BY_ID.get(selectedHeroId) : null;
  const timerText = formatTimer(room);
  const roleLabel = runtimeMode === "solo" ? "Соло" : seat.role === "host" ? teamLabel("A") : seat.role === "guest" ? teamLabel("B") : "Зритель";
  const activeSide = step?.side || null;
  const actionText = step?.action === "ban" ? "БАН" : step?.action === "pick" ? "ПИК" : "ЗАВЕРШЕНО";
  const showHeroes = room.status !== "completed";

  app.innerHTML = `
    <div class="room-view">
      ${renderBrand()}
      ${room.status === "drafting" ? `<div class="draft-rotate-notice" role="status">
        <div class="draft-rotate-notice__icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none"><rect x="15" y="7" width="18" height="34" rx="3" stroke="currentColor" stroke-width="2.5"/><path d="M20 12h8M23 36h2" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/><path d="M7 20c1.7-5 6.1-8.5 11.4-9.1M7 20l-.8-5.7M7 20l5.5-.6M41 28c-1.7 5-6.1 8.5-11.4 9.1M41 28l.8 5.7M41 28l-5.5.6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <p class="eyebrow">Драфт идёт</p>
        <h2>Поверните телефон</h2>
      </div>` : ""}
      <div class="room-shell">
      ${lastError ? `<div class="error-box">${escapeHtml(lastError)}</div>` : ""}
      <div class="room-topbar">
        <div class="room-meta">
          ${runtimeMode === "online" ? `<span class="connection-dot ${connected ? "connected" : ""}" aria-hidden="true"></span>` : ""}
          <span class="room-team-name room-team-name--a team-a">${escapeHtml(room.teamNames.A)}</span>
          <span class="room-role">${roleLabel}</span>
        </div>
        <div class="turn-center">
          <span class="turn-label ${activeSide === "A" ? "team-a" : activeSide === "B" ? "team-b" : ""}">${activeSide ? `${escapeHtml(room.teamNames[activeSide])} — ${actionText}` : actionText}</span>
          <span class="timer" id="draft-timer">${timerText}</span>
        </div>
        <span class="mobile-room-mode">${escapeHtml(roleLabel)}</span>
        <div class="room-actions">
          <span class="room-team-name room-team-name--b team-b">${escapeHtml(room.teamNames.B)}</span>
          ${runtimeMode === "online" && seat.role === "host" ? `<button class="opponent-link opponent-link--compact" id="copy-opponent" type="button">${COPY_ICON}<span>Ссылка сопернику</span></button>` : ""}
          ${runtimeMode === "online" ? `<button class="secondary-button" id="copy-watch" type="button">Ссылка зрителю</button>` : ""}
        </div>
      </div>

      <div class="mobile-compositions">
        ${renderMobileTeam("A")}
        ${renderMobileTeam("B")}
      </div>

      <div class="draft-layout">
        ${renderTeamPanel("A")}
        <section class="draft-center-column">
          <section class="panel hero-panel">
            ${showHeroes ? `<div class="hero-grid">
              ${HEROES.map((hero) => renderHeroCard(hero, canAct)).join("")}
            </div>` : `<div class="hero-complete-state"><span class="hero-complete-state__mark">✓</span><strong>Драфт завершён</strong><span>Пики и баны показаны по сторонам</span></div>`}
            ${room.status === "completed" ? renderCompletedActions("mobile-side") : renderActionBar(selectedHero, step, canAct, "mobile-side")}
          </section>
          ${renderLiveSequence()}
          ${room.status === "completed" ? renderCompletedActions("desktop") : renderActionBar(selectedHero, step, canAct, "desktop")}
        </section>
        ${renderTeamPanel("B")}
      </div>
      <div class="ad-zone" aria-label="Реклама"></div>
      </div>
    </div>
  `;

  attachRoomEvents(canAct);
  attachImageFallbacks();
}

function renderTeamPanel(side) {
  const picks = room.picks[side];
  const bans = room.bans[side];
  const expectedBans = room.rules.sequence.filter((step) => step.action === "ban" && step.side === side).length;
  const renderSlots = (items, count, kind) => Array.from({ length: count }, (_, index) => items[index] ? renderMiniHero(items[index], kind) : renderEmptySlot(kind)).join("");
  return `
    <aside class="panel team-panel team-${side.toLowerCase()}">
      <div class="team-heading">
        <h2 class="team-name">${escapeHtml(room.teamNames[side])}</h2>
        <span class="team-side" aria-label="Сторона ${teamLabel(side)}"><span class="team-side__full">${teamLabel(side)}</span><span class="team-side__short" aria-hidden="true">${teamLabel(side)}</span></span>
      </div>
      <div class="team-block">
        <h3>Пики</h3>
        <div class="mini-list mini-list--slots">
          ${renderSlots(picks, room.rules.teamSize, "pick")}
        </div>
      </div>
      <div class="team-block">
        <h3>Баны</h3>
        <div class="mini-list mini-list--slots">
          ${renderSlots(bans, expectedBans, "ban")}
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

function renderMiniHero(heroId, kind = "pick") {
  const hero = HERO_BY_ID.get(heroId);
  if (!hero) return "";
  return `<div class="mini-hero mini-hero--${kind}" title="${escapeAttr(hero.name)}"><img src="${hero.image}" alt="" width="54" height="54" loading="lazy" /><span class="sr-only">${escapeHtml(hero.name)}</span></div>`;
}

function renderEmptySlot(kind = "pick") {
  return `<div class="empty-slot empty-slot--${kind}" aria-label="Свободный слот"><span aria-hidden="true"></span></div>`;
}

function renderHeroCard(hero, canAct) {
  const state = heroUsageState(hero.id);
  const used = state !== "available";
  const selected = hero.id === selectedHeroId;
  const step = currentStep(room);
  const quotaAvailable = stepHasCapacity(step);
  const disabled = used || !canAct || room.status !== "drafting" || !quotaAvailable;
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

function stepHasCapacity(step) {
  if (!step || !room) return false;
  const items = room[step.action === "pick" ? "picks" : "bans"][step.side];
  const limit = step.action === "pick" ? room.rules.teamSize : room.rules.banCount;
  return items.length < limit;
}

function renderLiveSequence() {
  const sequence = room.rules.sequence;
  return `
    <section class="sequence-editor sequence-editor--live" aria-label="Последовательность пиков и банов">
      <div class="sequence-editor__header"><span>Последовательность пиков и банов</span><span class="sequence-editor__hint">${room.status === "completed" ? "Завершено" : `Шаг ${room.currentStep + 1} из ${sequence.length}`}</span></div>
      <div class="sequence-editor__scroll">
        <div class="sequence-editor__grid" style="--sequence-length:${sequence.length}">
          <div class="sequence-track sequence-track--numbers sequence-track--live"><span></span>${sequence.map((_, index) => `<span>${index + 1}</span>`).join("")}</div>
          ${renderLiveSequenceTrack("A")}
          ${renderLiveSequenceTrack("B")}
        </div>
      </div>
    </section>
  `;
}

function renderLiveSequenceTrack(side) {
  return `<div class="sequence-track sequence-track--${side.toLowerCase()} sequence-track--live"><span class="sequence-track__label">${teamLabel(side)}</span>${room.rules.sequence.map((step, index) => {
    const active = step.side === side;
    const state = active ? step.action : "empty";
    const classes = ["sequence-editor-step", "sequence-editor-step--live", state];
    if (index < room.currentStep) classes.push("done");
    if (index === room.currentStep && room.status === "drafting") classes.push("current");
    const symbol = state === "ban" ? "×" : state === "pick" ? "✓" : "";
    const label = active ? `${step.action === "ban" ? "Бан" : "Пик"}, ${teamLabel(side)}, шаг ${index + 1}` : `Пусто, шаг ${index + 1}`;
    return `<span class="${classes.join(" ")}" role="img" aria-label="${label}"><span aria-hidden="true">${symbol}</span></span>`;
  }).join("")}</div>`;
}

function renderActionBar(selectedHero, step, canAct, variant = "default") {
  const action = step?.action === "ban" ? "ЗАБАНИТЬ" : "ВЫБРАТЬ";
  const buttonLabel = variant === "mobile-side"
    ? (step?.action === "ban" ? "БАН" : "ПИК")
    : selectedHero ? `${action} ${escapeHtml(selectedHero.name)}` : action;
  const unavailableReason = !step
      ? "Нет активного хода"
      : !canAct
        ? seat.role === "spectator" ? "Режим зрителя" : "Сейчас ход другой команды"
        : !stepHasCapacity(step)
          ? step.action === "ban" ? "Лимит банов уже исчерпан" : "Лимит пиков уже исчерпан"
        : "Выберите героя";
  return `
    <div class="action-bar action-bar--${variant}">
      <div class="selected-summary">
        ${selectedHero ? `<img src="${selectedHero.image}" alt="" width="46" height="46" /><div><strong>${escapeHtml(selectedHero.name)}</strong><br /><span>${step?.action === "ban" ? "Будет забанен" : "Будет выбран"}</span></div>` : `<span>${escapeHtml(unavailableReason)}</span>`}
      </div>
      <button id="${variant === "mobile-side" ? "confirm-action-mobile" : "confirm-action"}" data-confirm-action class="confirm-button" type="button" aria-label="${escapeAttr(selectedHero ? `${action} ${selectedHero.name}` : unavailableReason)}" ${!selectedHero || !canAct ? "disabled" : ""}>${buttonLabel}</button>
    </div>
  `;
}

function renderCompletedActions(variant = "default") {
  return `
    <div class="action-bar action-bar--${variant}">
      <div class="selected-summary"><strong>Драфт завершён</strong></div>
      <button id="${variant === "mobile-side" ? "restart-draft-mobile" : "restart-draft"}" data-restart-draft class="confirm-button" type="button">Новый драфт</button>
    </div>
  `;
}

function attachRoomEvents(canAct) {
  app.querySelectorAll("[data-hero]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!canAct || button.disabled) return;
      selectedHeroId = button.dataset.hero;
      renderRoom();
    });
  });

  app.querySelectorAll("[data-confirm-action]").forEach((button) => {
    button.addEventListener("click", () => void confirmAction());
  });
  app.querySelector("#copy-opponent")?.addEventListener("click", () => void copyOpponentLink());
  app.querySelector("#copy-watch")?.addEventListener("click", () => void copyText(`${location.origin}/draft/${roomCode}`, "Ссылка зрителя скопирована"));
  app.querySelectorAll("[data-restart-draft]").forEach((button) => button.addEventListener("click", () => {
    if (runtimeMode === "solo") sessionStorage.removeItem(SOLO_KEY);
    navigate("/draft");
  }));
}

async function confirmAction() {
  if (!selectedHeroId || !room) return;
  if (runtimeMode === "solo") {
    room = applyLocalAction(room, selectedHeroId);
    selectedHeroId = null;
    sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
    deadlineSentVersion = null;
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

function sendRoomMessage(message) {
  if (socket?.readyState !== WebSocket.OPEN) {
    lastError = "Соединение ещё не восстановлено";
    if (room) renderRoom();
    return;
  }
  socket.send(JSON.stringify(message));
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
        deadlineSentVersion = null;
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
    if (event.code === 4001) {
      lastError = "Место капитана открыто в другой вкладке. Эта вкладка больше не переподключается как капитан.";
      if (room) renderRoom();
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
  if (!room || room.status !== "drafting" || !room.turnDeadlineAt) return;
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
  if (remaining > 0 || deadlineSentVersion === room.version) return;
  deadlineSentVersion = room.version;
  if (runtimeMode === "solo") {
    room = applyLocalTimeout(room, HEROES.map((hero) => hero.id));
    sessionStorage.setItem(SOLO_KEY, JSON.stringify(room));
    renderRoom();
    startTimer();
    return;
  }
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "deadline", expectedVersion: room.version }));
  }
}

function formatTimer(value) {
  if (value.status === "waiting") return "—";
  if (!value.turnDeadlineAt) return "—";
  return formatMilliseconds(Math.max(0, value.turnDeadlineAt - Date.now()));
}

function formatMilliseconds(ms) {
  const totalSeconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function renderExpiredRoom() {
  stopTimer();
  app.innerHTML = `
    ${renderBrand()}
    <section class="panel message-panel">
      <p class="eyebrow">Комната недоступна</p>
      <h1 style="font-size:clamp(32px,6vw,56px)">Драфт больше не существует</h1>
      <p class="create-lead">Комнаты временные и удаляются после завершения или простоя.</p>
      <div class="message-actions"><a class="primary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">Создать новый</a></div>
    </section>
  `;
}

function renderNotFound() {
  app.innerHTML = `${renderBrand()}<section class="panel message-panel"><h1 style="font-size:clamp(32px,6vw,56px)">Страница не найдена</h1><div class="message-actions"><a class="primary-button" style="display:inline-flex;align-items:center;text-decoration:none" href="/draft">К драфтам</a></div></section>`;
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
