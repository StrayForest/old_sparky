export const TEAM_SIZES = Object.freeze([2, 4, 6]);
export const BAN_COUNTS = Object.freeze([0, 1, 2, 3, 4, 5, 6]);
export const TIMER_SECONDS = Object.freeze([0, 30, 45, 60, 90]);

export const PRESETS = {
  standard: { id: "standard", label: "Стандарт", teamSize: 6, timerSeconds: 30, sequence: null },
  "community-6v6": { id: "community-6v6", label: "Community 6v6", teamSize: 6, timerSeconds: 30, sequence: null },
  "community-6v6-no-timer": { id: "community-6v6-no-timer", label: "Community 6v6 — без таймера", teamSize: 6, timerSeconds: 0, sequence: null },
  "6v6-no-bans": { id: "6v6-no-bans", label: "6v6 без банов", teamSize: 6, timerSeconds: 30, sequence: null },
  custom: { id: "custom", label: "Свой", teamSize: 6, timerSeconds: 30, sequence: null }
};

export const DEFAULT_CUSTOM_RULES = Object.freeze({
  teamSize: 6,
  banCount: 6,
  firstSide: "A",
  sequence: Object.freeze([
    { action: "ban", side: "A" },
    { action: "ban", side: "B" },
    { action: "ban", side: "B" },
    { action: "ban", side: "A" },
    { action: "ban", side: "A" },
    { action: "ban", side: "B" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" }
  ])
});

export function buildRules(presetId, timerOverride, customRules = null) {
  if (presetId === "standard" || presetId === "custom") {
    return buildCustomRules({
      ...DEFAULT_CUSTOM_RULES,
      ...(customRules || {}),
      presetId,
      timerSeconds: timerOverride
    });
  }
  const preset = PRESETS[presetId] || PRESETS.standard;
  return buildCustomRules({
    ...DEFAULT_CUSTOM_RULES,
    teamSize: preset.teamSize,
    banCount: preset.id === "6v6-no-bans" ? 0 : DEFAULT_CUSTOM_RULES.banCount,
    firstSide: "A",
    sequence: null,
    presetId: preset.id,
    timerSeconds: timerOverride === undefined ? preset.timerSeconds : timerOverride
  });
}

export function buildCustomRules(value) {
  const teamSize = Number(value?.teamSize);
  if (!TEAM_SIZES.includes(teamSize)) {
    throw new Error("Размер команды должен быть 2, 4 или 6");
  }
  const timerSeconds = normalizeTimer(value?.timerSeconds, 30);
  const banCount = Number(value?.banCount ?? value?.banSequence?.length ?? 0);
  if (!BAN_COUNTS.includes(banCount)) {
    throw new Error("Количество банов должно быть от 0 до 6");
  }
  const firstSide = value?.firstSide === "B" ? "B" : "A";
  const sequence = Array.isArray(value?.sequence) && value.sequence.length
    ? value.sequence.map((step) => ({ action: step?.action, side: step?.side }))
    : value?.sequence === null || (!Object.hasOwn(value || {}, "banSequence") && !Object.hasOwn(value || {}, "pickSequence"))
      ? createDefaultSequence(teamSize, banCount, firstSide)
      : [
          ...parseSideSequence(value?.banSequence, banCount, true).map((side) => ({ action: "ban", side })),
          ...parseSideSequence(value?.pickSequence, teamSize * 2, false).map((side) => ({ action: "pick", side }))
        ];
  validateSequence(sequence, teamSize, banCount);
  const firstIndex = sequence.findIndex((step) => step.side === firstSide);
  if (firstIndex > 0) {
    [sequence[0], sequence[firstIndex]] = [sequence[firstIndex], sequence[0]];
  }
  return {
    presetId: value?.presetId || "custom",
    teamSize,
    banCount,
    firstSide,
    timerSeconds,
    sequence: sequence.map((step, index) => ({ ...step, index }))
  };
}

export function createDefaultSequence(teamSize, banCount, firstSide = "A") {
  const sideOrder = makeTurnOrder(teamSize * 2, firstSide);
  const banOrder = makeTurnOrder(banCount, firstSide);
  return [
    ...banOrder.map((side) => ({ action: "ban", side })),
    ...sideOrder.map((side) => ({ action: "pick", side }))
  ];
}

function makeTurnOrder(count, firstSide = "A") {
  const first = firstSide === "B" ? "B" : "A";
  const second = first === "A" ? "B" : "A";
  const pattern = [first, second, second, first];
  return Array.from({ length: count }, (_, index) => pattern[index % pattern.length]);
}

export function validateSequence(sequence, teamSize, banCount) {
  if (!Array.isArray(sequence) || sequence.length !== banCount + teamSize * 2 || sequence.length < 4 || sequence.length > 40) {
    throw new Error("Последовательность драфта заполнена не полностью");
  }
  const counts = { ban: 0, pick: 0, A: 0, B: 0 };
  const picks = { A: 0, B: 0 };
  sequence.forEach((step) => {
    if (!step || !["pick", "ban"].includes(step.action) || !["A", "B"].includes(step.side)) {
      throw new Error("В последовательности есть некорректный шаг");
    }
    counts[step.action] += 1;
    counts[step.side] += 1;
    if (step.action === "pick") picks[step.side] += 1;
  });
  if (counts.ban !== banCount || counts.pick !== teamSize * 2 || picks.A !== teamSize || picks.B !== teamSize) {
    throw new Error("Последовательность не соответствует выбранному формату");
  }
}

export function normalizeSideSequence(value) {
  return String(value ?? "")
    .toUpperCase()
    .replace(/[^AB]/gu, "");
}

export function createLocalRoom(rules, teamNames) {
  const now = Date.now();
  return {
    schema: 2,
    status: "drafting",
    version: 1,
    rules,
    teamNames: {
      A: cleanTeamName(teamNames?.A, "Команда 1"),
      B: cleanTeamName(teamNames?.B, "Команда 2")
    },
    currentStep: 0,
    picks: { A: [], B: [] },
    bans: { A: [], B: [] },
    turnStartedAt: now,
    turnDeadlineAt: deadlineFrom(now, rules.timerSeconds)
  };
}

export function applyLocalAction(room, heroId) {
  if (!room || room.status !== "drafting") {
    return room;
  }
  const step = room.rules.sequence[room.currentStep];
  if (!step || heroAlreadyUsed(room, heroId)) {
    return room;
  }
  const next = structuredClone(room);
  next[step.action === "pick" ? "picks" : "bans"][step.side].push(heroId);
  next.currentStep += 1;
  next.version += 1;
  const now = Date.now();
  next.turnStartedAt = now;
  next.turnDeadlineAt = deadlineFrom(now, next.rules.timerSeconds);
  if (next.currentStep >= next.rules.sequence.length) {
    next.status = "completed";
    next.turnDeadlineAt = null;
  }
  return next;
}

export function applyLocalTimeout(room, heroIds) {
  if (!room || room.status !== "drafting" || !room.turnDeadlineAt || Date.now() < room.turnDeadlineAt) {
    return room;
  }
  const heroId = heroIds.find((candidate) => !heroAlreadyUsed(room, candidate));
  if (!heroId) return room;
  return applyLocalAction(room, heroId);
}

export function heroAlreadyUsed(room, heroId) {
  return [room.picks.A, room.picks.B, room.bans.A, room.bans.B].some((items) => items.includes(heroId));
}

export function currentStep(room) {
  return room?.rules?.sequence?.[room.currentStep] || null;
}

export function makeResultPayload(room) {
  return {
    v: 1,
    preset: room.rules.presetId,
    a: room.teamNames.A,
    b: room.teamNames.B,
    pa: room.picks.A,
    pb: room.picks.B,
    ba: room.bans.A,
    bb: room.bans.B
  };
}

export function encodeResult(room) {
  const json = JSON.stringify(makeResultPayload(room));
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

export function decodeResult(value) {
  if (!value || value.length > 6000) {
    throw new Error("Некорректная ссылка результата");
  }
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  const payload = JSON.parse(new TextDecoder().decode(bytes));
  if (!payload || payload.v !== 1 || !Array.isArray(payload.pa) || !Array.isArray(payload.pb) || !Array.isArray(payload.ba) || !Array.isArray(payload.bb)) {
    throw new Error("Версия результата не поддерживается");
  }
  return payload;
}

export function cleanTeamName(value, fallback) {
  const text = String(value || "").trim().slice(0, 40);
  return text || fallback;
}

function parseSideSequence(value, maxLength, allowEmpty) {
  const normalized = normalizeSideSequence(value);
  if (!normalized && allowEmpty) {
    return [];
  }
  if (!normalized || normalized.length > maxLength) {
    throw new Error("Некорректный порядок ходов");
  }
  return [...normalized];
}

function normalizeTimer(value, fallback) {
  const timer = Number.isFinite(Number(value)) ? Number(value) : fallback;
  if (![0, 30, 45, 60, 90].includes(timer)) {
    throw new Error("Некорректный таймер");
  }
  return timer;
}

function deadlineFrom(now, timerSeconds) {
  return timerSeconds > 0 ? now + timerSeconds * 1000 : null;
}
