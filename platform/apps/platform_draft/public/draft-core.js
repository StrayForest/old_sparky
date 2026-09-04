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
  if (counts.pick !== teamSize * 2 || picks.A !== teamSize || picks.B !== teamSize || bans.A > banCount || bans.B > banCount) {
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

export function countSequenceActions(sequence, side) {
  if (!Array.isArray(sequence) || !["A", "B"].includes(side)) return { pick: 0, ban: 0 };
  return sequence.reduce((counts, step) => {
    if (step?.side === side && ["pick", "ban"].includes(step.action)) counts[step.action] += 1;
    return counts;
  }, { pick: 0, ban: 0 });
}

export function cycleSequenceCell(sequence, index, side, limits = {}) {
  if (!Array.isArray(sequence) || !Number.isInteger(index) || !["A", "B"].includes(side) || index < 0 || index >= sequence.length) {
    return sequence;
  }
  const pickLimit = Number.isInteger(limits.pickLimit) && limits.pickLimit >= 0 ? limits.pickLimit : Number.POSITIVE_INFINITY;
  const banLimit = Number.isInteger(limits.banLimit) && limits.banLimit >= 0 ? limits.banLimit : Number.POSITIVE_INFINITY;
  const counts = countSequenceActions(sequence, side);
  const next = sequence.map((step) => step ? { ...step } : null);
  const current = next[index];
  if (!current || current.side !== side) {
    if (counts.pick < pickLimit) {
      next[index] = { action: "pick", side };
    } else if (counts.ban < banLimit) {
      next[index] = { action: "ban", side };
    } else {
      return sequence;
    }
    return next;
  }
  if (current.action === "pick" && counts.ban < banLimit) {
    next[index] = { action: "ban", side };
  } else {
    next[index] = null;
  }
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
  const capacity = step.action === "pick" ? room.rules.teamSize : room.rules.banCount;
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

function deadlineFrom(now, timerSeconds) {
  return timerSeconds > 0 ? now + timerSeconds * 1000 : null;
}
