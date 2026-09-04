export const PRESETS = {
  "community-6v6": {
    id: "community-6v6",
    label: "Community 6v6",
    teamSize: 6,
    timerSeconds: 30,
    sequence: [
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
    ]
  },
  "community-6v6-no-timer": {
    id: "community-6v6-no-timer",
    label: "Community 6v6 — без таймера",
    teamSize: 6,
    timerSeconds: 0,
    sequence: null
  },
  "6v6-no-bans": {
    id: "6v6-no-bans",
    label: "6v6 без банов",
    teamSize: 6,
    timerSeconds: 30,
    sequence: [
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
    ]
  }
};

PRESETS["community-6v6-no-timer"].sequence = PRESETS["community-6v6"].sequence.map((step) => ({ ...step }));

export function buildRules(presetId, timerOverride) {
  const preset = PRESETS[presetId] || PRESETS["community-6v6"];
  const timerSeconds = Number.isFinite(timerOverride) ? Math.max(0, Math.min(90, timerOverride)) : preset.timerSeconds;
  return {
    presetId: preset.id,
    teamSize: preset.teamSize,
    timerSeconds,
    sequence: preset.sequence.map((step, index) => ({ ...step, index }))
  };
}

export function createLocalRoom(rules, teamNames) {
  const now = Date.now();
  return {
    schema: 1,
    status: "drafting",
    version: 1,
    rules,
    teamNames: {
      A: cleanTeamName(teamNames?.A, "Команда A"),
      B: cleanTeamName(teamNames?.B, "Команда B")
    },
    currentStep: 0,
    picks: { A: [], B: [] },
    bans: { A: [], B: [] },
    turnStartedAt: now,
    turnDeadlineAt: deadlineFrom(now, rules.timerSeconds),
    timedOut: false
  };
}

export function applyLocalAction(room, heroId) {
  if (!room || room.status !== "drafting") {
    return room;
  }
  const step = room.rules.sequence[room.currentStep];
  if (!step || heroAlreadyUsed(room, heroId) || room.timedOut) {
    return room;
  }
  const next = structuredClone(room);
  next[step.action === "pick" ? "picks" : "bans"][step.side].push(heroId);
  next.currentStep += 1;
  next.version += 1;
  const now = Date.now();
  next.turnStartedAt = now;
  next.turnDeadlineAt = deadlineFrom(now, next.rules.timerSeconds);
  next.timedOut = false;
  if (next.currentStep >= next.rules.sequence.length) {
    next.status = "completed";
    next.turnDeadlineAt = null;
  }
  return next;
}

export function pauseLocalOnTimeout(room) {
  if (!room || room.status !== "drafting" || !room.turnDeadlineAt || Date.now() < room.turnDeadlineAt) {
    return room;
  }
  const next = structuredClone(room);
  next.timedOut = true;
  next.version += 1;
  return next;
}

export function resumeLocalTurn(room) {
  if (!room?.timedOut) {
    return room;
  }
  const next = structuredClone(room);
  const now = Date.now();
  next.timedOut = false;
  next.turnStartedAt = now;
  next.turnDeadlineAt = deadlineFrom(now, next.rules.timerSeconds);
  next.version += 1;
  return next;
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

function deadlineFrom(now, timerSeconds) {
  return timerSeconds > 0 ? now + timerSeconds * 1000 : null;
}
