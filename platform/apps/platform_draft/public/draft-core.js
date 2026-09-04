export const TEAM_SIZES = Object.freeze([6, 4, 2]);
export const MAX_BANS_PER_TEAM = 3;
export const BAN_COUNTS = Object.freeze([0, 1, 2, 3]);
export const TIMER_SECONDS = Object.freeze([30, 45, 60, 90]);

export const PRESETS = {
  standard: { id: "standard", label: "Стандарт", teamSize: 6, timerSeconds: 30, sequence: null },
  "community-6v6": { id: "community-6v6", label: "Community 6v6", teamSize: 6, timerSeconds: 30, sequence: null },
  "6v6-no-bans": { id: "6v6-no-bans", label: "6v6 без банов", teamSize: 6, timerSeconds: 30, sequence: null },
  custom: { id: "custom", label: "Свой", teamSize: 6, timerSeconds: 30, sequence: null }
};

export const DEFAULT_CUSTOM_RULES = Object.freeze({
  teamSize: 6,
  banCount: MAX_BANS_PER_TEAM,
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
    throw new Error(`Количество банов на команду должно быть от 0 до ${MAX_BANS_PER_TEAM}`);
  }
  const firstSide = value?.firstSide === "B" ? "B" : "A";
  const sequence = Array.isArray(value?.sequence)
    ? value.sequence.map((step) => ({ action: step?.action, side: step?.side }))
    : value?.sequence === null || (!Object.hasOwn(value || {}, "banSequence") && !Object.hasOwn(value || {}, "pickSequence"))
      ? createDefaultSequence(teamSize, banCount, firstSide)
      : [
          ...parseSideSequence(value?.banSequence, banCount * 2, true).map((side) => ({ action: "ban", side })),
          ...parseSideSequence(value?.pickSequence, teamSize * 2, false).map((side) => ({ action: "pick", side }))
        ];
  validateSequence(sequence, teamSize, banCount);
  const orientedSequence = orientSequence(sequence, firstSide);
  const banCounts = countSequenceBans(orientedSequence);
  return {
    presetId: value?.presetId || "custom",
    teamSize,
    banCount,
    banCounts,
    firstSide,
    timerSeconds,
    sequence: orientedSequence.map((step, index) => ({ ...step, index }))
  };
}

export function createDefaultSequence(teamSize, banCount, firstSide = "A") {
  const sideOrder = makeTurnOrder(teamSize * 2, firstSide);
  const banOrder = makeTurnOrder(banCount * 2, firstSide);
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
  if (!Array.isArray(sequence) || sequence.length < teamSize * 2 || sequence.length > teamSize * 2 + MAX_BANS_PER_TEAM * 2) {
    throw new Error("Последовательность драфта заполнена не полностью");
  }
  if (!BAN_COUNTS.includes(Number(banCount))) {
    throw new Error("Некорректное количество банов на команду");
  }
  const counts = { ban: 0, pick: 0, A: 0, B: 0 };
  const picks = { A: 0, B: 0 };
  const bans = { A: 0, B: 0 };
  sequence.forEach((step) => {
    if (!step || !["pick", "ban"].includes(step.action) || !["A", "B"].includes(step.side)) {
      throw new Error("В последовательности есть некорректный шаг");
    }
    counts[step.action] += 1;
    counts[step.side] += 1;
    if (step.action === "pick") picks[step.side] += 1;
    if (step.action === "ban") bans[step.side] += 1;
  });
  if (counts.pick !== teamSize * 2 || picks.A !== teamSize || picks.B !== teamSize || bans.A > MAX_BANS_PER_TEAM || bans.B > MAX_BANS_PER_TEAM) {
    throw new Error("Последовательность не соответствует выбранному формату");
  }
}

export function orientSequence(sequence, firstSide) {
  if (!sequence.length || sequence[0].side === firstSide) return sequence.map((step) => ({ ...step }));
  return sequence.map((step) => ({
    ...step,
    side: step.side === "A" ? "B" : "A"
  }));
}

export function cycleSequenceCell(sequence, index, side) {
  if (!Array.isArray(sequence) || !Number.isInteger(index) || !["A", "B"].includes(side) || index < 0 || index >= sequence.length) {
    return sequence;
  }
  const next = sequence.map((step) => step ? { ...step } : null);
  const current = next[index];
  if (!current || current.side !== side) {
    next[index] = { action: "pick", side };
    return next;
  }
  next[index] = current.action === "pick" ? { action: "ban", side } : null;
  return next;
}

function countSequenceBans(sequence) {
  const bans = { A: 0, B: 0 };
  sequence.forEach((step) => {
    if (step.action === "ban") bans[step.side] += 1;
  });
  return bans;
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
      A: cleanTeamName(teamNames?.A, "Команда А"),
      B: cleanTeamName(teamNames?.B, "Команда Б")
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
  const collection = room[step.action === "pick" ? "picks" : "bans"][step.side];
  const capacity = step.action === "pick" ? room.rules.teamSize : MAX_BANS_PER_TEAM;
  if (collection.length >= capacity) return room;
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

export function encodeResult(room, heroOrder = null) {
  const payload = Array.isArray(heroOrder)
    ? makeCompactResultPayload(room, heroOrder)
    : makeResultPayload(room);
  const json = JSON.stringify(payload);
  return encodeBase64Url(json);
}

function makeCompactResultPayload(room, heroOrder) {
  const indexByHeroId = new Map(heroOrder.map((hero, index) => [typeof hero === "string" ? hero : hero.id, index]));
  const encodeHeroes = (heroIds) => heroIds.map((heroId) => {
    const index = indexByHeroId.get(heroId);
    if (!Number.isInteger(index)) throw new Error("Некорректный герой в результате");
    return index;
  });
  return {
    v: 2,
    a: room.teamNames.A,
    b: room.teamNames.B,
    pa: encodeHeroes(room.picks.A),
    pb: encodeHeroes(room.picks.B),
    ba: encodeHeroes(room.bans.A),
    bb: encodeHeroes(room.bans.B),
    s: room.rules.sequence.map((step) => `${step.side}${step.action === "ban" ? "b" : "p"}`).join("")
  };
}

function encodeBase64Url(json) {
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

export function decodeResult(value, heroOrder = null) {
  if (!value || value.length > 6000) {
    throw new Error("Некорректная ссылка результата");
  }
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  let payload = JSON.parse(new TextDecoder().decode(bytes));
  if (!payload || ![1, 2].includes(payload.v) || !Array.isArray(payload.pa) || !Array.isArray(payload.pb) || !Array.isArray(payload.ba) || !Array.isArray(payload.bb)) {
    throw new Error("Версия результата не поддерживается");
  }
  if (payload.v === 2) {
    if (!Array.isArray(heroOrder) || typeof payload.s !== "string") {
      throw new Error("Для результата нужна актуальная версия героев");
    }
    const heroIds = heroOrder.map((hero) => typeof hero === "string" ? hero : hero.id);
    const decodeHeroes = (indexes) => indexes.map((index) => {
      if (!Number.isInteger(index) || index < 0 || index >= heroIds.length) throw new Error("Некорректный герой в результате");
      return heroIds[index];
    });
    payload = {
      ...payload,
      pa: decodeHeroes(payload.pa),
      pb: decodeHeroes(payload.pb),
      ba: decodeHeroes(payload.ba),
      bb: decodeHeroes(payload.bb),
      sequence: decodeCompactSequence(payload.s)
    };
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
  if (![30, 45, 60, 90].includes(timer)) {
    throw new Error("Некорректный таймер");
  }
  return timer;
}

function decodeCompactSequence(value) {
  if (!value || value.length > 80 || value.length % 2 !== 0) throw new Error("Некорректная последовательность результата");
  const sequence = [];
  for (let index = 0; index < value.length; index += 2) {
    const side = value[index];
    const action = value[index + 1] === "b" ? "ban" : value[index + 1] === "p" ? "pick" : null;
    if (!['A', 'B'].includes(side) || !action) throw new Error("Некорректная последовательность результата");
    sequence.push({ action, side, index: index / 2 });
  }
  return sequence;
}

function deadlineFrom(now, timerSeconds) {
  return timerSeconds > 0 ? now + timerSeconds * 1000 : null;
}
