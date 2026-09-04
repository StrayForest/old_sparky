import { buildRules } from "./public/draft-core.js";
import { HERO_BY_ID } from "./public/heroes.js";

const ROOM_TTL_MS = 2 * 60 * 60 * 1000;
const COMPLETED_TTL_MS = 15 * 60 * 1000;
const MAX_BODY_BYTES = 4096;
const MAX_WS_MESSAGE_BYTES = 2048;
const MAX_SPECTATORS = 50;
const MAX_MESSAGES_PER_WINDOW = 40;
const MESSAGE_WINDOW_MS = 10_000;
const KNOWN_PRESET_IDS = new Set(["community-6v6", "community-6v6-no-timer", "6v6-no-bans", "custom"]);
const VALID_TIMER_SECONDS = new Set([0, 30, 45, 60, 90]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = normalizedPath(url.pathname);

    if (path === "/draft/api/rooms" && request.method === "POST") {
      if (!originAllowed(request, env)) return json({ error: "Недопустимый источник запроса" }, 403);
      return createRoom(request, env);
    }

    const wsMatch = path.match(/^\/draft\/ws\/([A-Za-z0-9_-]{6,32})$/u);
    if (wsMatch) {
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return new Response("WebSocket required", { status: 426 });
      }
      if (!originAllowed(request, env)) return new Response("Forbidden", { status: 403 });
      const stub = env.DRAFT_ROOMS.get(env.DRAFT_ROOMS.idFromName(wsMatch[1]));
      return stub.fetch(new Request("https://draft-room.internal/websocket", request));
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD, POST" } });
    }

    return serveStatic(request, env, path);
  }
};

export class DraftRoom {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/create" && request.method === "POST") {
      return this.create(request);
    }
    if (url.pathname === "/websocket" && request.headers.get("Upgrade")?.toLowerCase() === "websocket") {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      server.serializeAttachment({ role: "pending", authenticated: false, count: 0, windowStartedAt: Date.now() });
      this.ctx.acceptWebSocket(server);
      return new Response(null, { status: 101, webSocket: client });
    }
    return new Response("Not found", { status: 404 });
  }

  async create(request) {
    const existing = await this.ctx.storage.get("room");
    if (existing) return json({ error: "Room collision" }, 409);
    const payload = await request.json();
    const now = Date.now();
    const room = {
      schema: 1,
      status: "drafting",
      version: 1,
      rules: validateRules(payload.rules),
      teamNames: {
        A: cleanTeamName(payload.teamNames?.A, "Команда A"),
        B: cleanTeamName(payload.teamNames?.B, "Команда B")
      },
      currentStep: 0,
      picks: { A: [], B: [] },
      bans: { A: [], B: [] },
      turnStartedAt: now,
      turnDeadlineAt: deadlineFrom(now, payload.rules?.timerSeconds),
      timedOut: false,
      createdAt: now,
      expiresAt: now + ROOM_TTL_MS,
      hostTokenHash: await hashToken(payload.hostToken),
      guestTokenHash: await hashToken(payload.guestToken)
    };
    await this.ctx.storage.put("room", room);
    await this.ctx.storage.setAlarm(room.expiresAt);
    return json({ ok: true });
  }

  async webSocketMessage(ws, rawMessage) {
    try {
      if (typeof rawMessage !== "string" || rawMessage.length > MAX_WS_MESSAGE_BYTES) {
        ws.close(4400, "invalid message");
        return;
      }
      let attachment = ws.deserializeAttachment() || { role: "pending", authenticated: false, count: 0, windowStartedAt: Date.now() };
      attachment = enforceSocketRate(attachment);
      if (!attachment) {
        ws.close(4429, "rate limited");
        return;
      }
      ws.serializeAttachment(attachment);

      const message = JSON.parse(rawMessage);
      if (!message || typeof message.type !== "string") {
        this.sendError(ws, "Некорректное сообщение");
        return;
      }

      if (!attachment.authenticated) {
        if (message.type !== "hello") {
          ws.close(4403, "authentication required");
          return;
        }
        await this.authenticateSocket(ws, message, attachment);
        return;
      }

      if (message.type === "action") {
        await this.applyAction(ws, message, attachment);
        return;
      }
      if (message.type === "timeout") {
        await this.applyTimeout(ws, message, attachment);
        return;
      }
      if (message.type === "resume") {
        await this.resumeTimer(ws, message, attachment);
        return;
      }
      if (message.type === "ping") {
        ws.send(JSON.stringify({ type: "pong", at: Date.now() }));
        return;
      }
      this.sendError(ws, "Неизвестное сообщение");
    } catch {
      this.sendError(ws, "Некорректное сообщение");
    }
  }

  async authenticateSocket(ws, message, attachment) {
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    const role = ["host", "guest", "spectator"].includes(message.role) ? message.role : "spectator";
    if (role === "host" || role === "guest") {
      if (typeof message.token !== "string" || message.token.length < 20 || message.token.length > 256) {
        ws.close(4403, "invalid seat token");
        return;
      }
      const tokenHash = await hashToken(message.token);
      const expectedHash = role === "host" ? room.hostTokenHash : room.guestTokenHash;
      if (!timingSafeStringEqual(tokenHash, expectedHash)) {
        ws.close(4403, "invalid seat token");
        return;
      }
      for (const other of this.ctx.getWebSockets()) {
        if (other === ws) continue;
        const otherAttachment = other.deserializeAttachment();
        if (otherAttachment?.authenticated && otherAttachment.role === role) {
          try { other.close(4001, "seat replaced"); } catch { /* noop */ }
        }
      }
    } else {
      const spectatorCount = this.ctx.getWebSockets().filter((candidate) => candidate.deserializeAttachment()?.role === "spectator").length;
      if (spectatorCount >= MAX_SPECTATORS) {
        ws.close(4429, "spectator limit");
        return;
      }
    }
    ws.serializeAttachment({ ...attachment, role, authenticated: true });
    this.sendState(ws, room);
  }

  async applyAction(ws, message, attachment) {
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    if (room.status !== "drafting") {
      this.sendError(ws, "Драфт уже завершён", room);
      return;
    }
    if (!Number.isInteger(message.expectedVersion) || message.expectedVersion !== room.version) {
      this.sendError(ws, "Состояние изменилось — обновлено актуальное состояние", room);
      return;
    }
    const step = room.rules.sequence[room.currentStep];
    if (!step) {
      this.sendError(ws, "Нет активного хода", room);
      return;
    }
    if (!seatCanAct(attachment.role, step.side)) {
      this.sendError(ws, "Сейчас ход другой команды", room);
      return;
    }
    if (room.timedOut) {
      this.sendError(ws, "Ход на паузе", room);
      return;
    }
    const now = Date.now();
    if (room.turnDeadlineAt && now >= room.turnDeadlineAt) {
      room.timedOut = true;
      room.version += 1;
      await this.saveAndBroadcast(room);
      return;
    }
    if (typeof message.heroId !== "string" || !HERO_BY_ID.has(message.heroId)) {
      this.sendError(ws, "Неизвестный герой", room);
      return;
    }
    if (heroUsed(room, message.heroId)) {
      this.sendError(ws, "Этот герой уже выбран или забанен", room);
      return;
    }

    room[step.action === "pick" ? "picks" : "bans"][step.side].push(message.heroId);
    room.currentStep += 1;
    room.version += 1;
    room.turnStartedAt = now;
    room.turnDeadlineAt = deadlineFrom(now, room.rules.timerSeconds);
    room.timedOut = false;

    if (room.currentStep >= room.rules.sequence.length) {
      room.status = "completed";
      room.turnDeadlineAt = null;
      room.expiresAt = now + COMPLETED_TTL_MS;
      await this.ctx.storage.setAlarm(room.expiresAt);
    }
    await this.saveAndBroadcast(room);
  }

  async applyTimeout(ws, message, attachment) {
    if (!["host", "guest"].includes(attachment.role)) {
      this.sendError(ws, "Недостаточно прав");
      return;
    }
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    if (room.status !== "drafting" || room.timedOut || !room.turnDeadlineAt) return;
    if (message.expectedVersion !== room.version) {
      this.sendState(ws, room);
      return;
    }
    if (Date.now() < room.turnDeadlineAt) return;
    room.timedOut = true;
    room.version += 1;
    await this.saveAndBroadcast(room);
  }

  async resumeTimer(ws, message, attachment) {
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    const step = room.rules.sequence[room.currentStep];
    if (!room.timedOut || room.status !== "drafting" || !step) return;
    if (message.expectedVersion !== room.version) {
      this.sendState(ws, room);
      return;
    }
    if (!seatCanAct(attachment.role, step.side)) {
      this.sendError(ws, "Возобновить ход может активная команда", room);
      return;
    }
    const now = Date.now();
    room.timedOut = false;
    room.version += 1;
    room.turnStartedAt = now;
    room.turnDeadlineAt = deadlineFrom(now, room.rules.timerSeconds);
    await this.saveAndBroadcast(room);
  }

  async saveAndBroadcast(room) {
    await this.ctx.storage.put("room", room);
    const payload = JSON.stringify({ type: "state", room: publicRoom(room), serverTime: Date.now() });
    for (const ws of this.ctx.getWebSockets()) {
      const attachment = ws.deserializeAttachment();
      if (!attachment?.authenticated) continue;
      try {
        if (typeof ws.bufferedAmount === "number" && ws.bufferedAmount > 512 * 1024) {
          ws.close(4008, "slow client");
          continue;
        }
        ws.send(payload);
      } catch {
        try { ws.close(1011, "send failed"); } catch { /* noop */ }
      }
    }
  }

  sendState(ws, room) {
    try {
      ws.send(JSON.stringify({ type: "state", room: publicRoom(room), serverTime: Date.now() }));
    } catch {
      try { ws.close(1011, "send failed"); } catch { /* noop */ }
    }
  }

  sendError(ws, error, room = null) {
    try {
      ws.send(JSON.stringify({ type: "error", error, room: room ? publicRoom(room) : undefined }));
    } catch {
      try { ws.close(1011, "send failed"); } catch { /* noop */ }
    }
  }

  async alarm() {
    const room = await this.ctx.storage.get("room");
    if (room && room.expiresAt > Date.now()) {
      await this.ctx.storage.setAlarm(room.expiresAt);
      return;
    }
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.close(4404, "room expired"); } catch { /* noop */ }
    }
    await this.ctx.storage.deleteAll();
  }

  webSocketClose(ws) {
    try { ws.close(1000, "closed"); } catch { /* noop */ }
  }

  webSocketError(ws) {
    try { ws.close(1011, "socket error"); } catch { /* noop */ }
  }
}

async function createRoom(request, env) {
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) return json({ error: "Слишком большой запрос" }, 413);
  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Некорректный JSON" }, 400);
  }
  const presetId = typeof payload?.presetId === "string" ? payload.presetId : "community-6v6";
  let rules;
  try {
    if (!KNOWN_PRESET_IDS.has(presetId)) throw new Error("invalid preset");
    const timerSeconds = parseTimerSeconds(payload?.timerSeconds);
    const customRules = presetId === "custom" ? validateCustomCreateSettings(payload?.customRules) : null;
    rules = buildRules(presetId, timerSeconds, customRules);
    validateRules(rules);
  } catch {
    return json({ error: "Некорректные правила" }, 400);
  }
  const teamNames = {
    A: cleanTeamName(payload?.teamNames?.A, "Команда A"),
    B: cleanTeamName(payload?.teamNames?.B, "Команда B")
  };

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const roomCode = randomToken(9).replaceAll("-", "").replaceAll("_", "").slice(0, 12);
    const hostToken = randomToken(24);
    const guestToken = randomToken(24);
    const stub = env.DRAFT_ROOMS.get(env.DRAFT_ROOMS.idFromName(roomCode));
    const createResponse = await stub.fetch("https://draft-room.internal/create", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ rules, teamNames, hostToken, guestToken })
    });
    if (createResponse.status === 409) continue;
    if (!createResponse.ok) return json({ error: "Не удалось создать комнату" }, 503);
    return json({ roomCode, hostToken, guestToken }, 201, { "cache-control": "no-store" });
  }
  return json({ error: "Не удалось создать уникальную комнату" }, 503);
}

async function serveStatic(request, env, path) {
  let assetPath;
  if (path === "/draft" || path === "/draft/" || path === "/draft/result" || /^\/draft\/[A-Za-z0-9_-]{6,32}\/?$/u.test(path) || path === "/draft/solo") {
    assetPath = "/draft/index.html";
  } else if (path.startsWith("/draft/")) {
    assetPath = path;
  } else {
    return new Response("Not found", { status: 404 });
  }

  const assetUrl = new URL(request.url);
  assetUrl.pathname = assetPath;
  const assetRequest = new Request(assetUrl.toString(), request);
  const assetResponse = await env.ASSETS.fetch(assetRequest);
  if (!assetResponse.ok) return assetResponse;

  const headers = new Headers(assetResponse.headers);
  headers.set("X-Old-Sparky-Draft", "1");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()");
  headers.set("X-Frame-Options", "SAMEORIGIN");
  headers.set(
    "Content-Security-Policy",
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; form-action 'self'; script-src 'self' https://*.googlesyndication.com https://*.googleadservices.com https://*.google.com https://*.gstatic.com https://*.doubleclick.net 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https: wss:; frame-src https:; font-src 'self' data: https:"
  );
  if (assetPath === "/draft/index.html") {
    headers.set("Cache-Control", "no-cache");
  } else {
    headers.set("Cache-Control", "public, max-age=300");
  }
  if (path === "/draft/result" || /^\/draft\/[A-Za-z0-9_-]{6,32}\/?$/u.test(path) || path === "/draft/solo") {
    headers.set("X-Robots-Tag", "noindex, nofollow");
  }
  return new Response(assetResponse.body, { status: assetResponse.status, headers });
}

function validateRules(rules) {
  if (!rules || ![2, 4, 6].includes(rules.teamSize) || ![0, 30, 45, 60, 90].includes(rules.timerSeconds)) {
    throw new Error("invalid rules");
  }
  if (!Array.isArray(rules.sequence) || rules.sequence.length < 4 || rules.sequence.length > 40) {
    throw new Error("invalid sequence");
  }
  const picks = { A: 0, B: 0 };
  for (const [index, step] of rules.sequence.entries()) {
    if (!step || !["pick", "ban"].includes(step.action) || !["A", "B"].includes(step.side) || step.index !== index) {
      throw new Error("invalid step");
    }
    if (step.action === "pick") picks[step.side] += 1;
  }
  if (picks.A !== rules.teamSize || picks.B !== rules.teamSize) throw new Error("invalid pick count");
  return rules;
}

function validateCustomCreateSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid custom settings");
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 3 || keys.join(",") !== "banSequence,pickSequence,teamSize") {
    throw new Error("invalid custom settings");
  }
  if (!Number.isInteger(value.teamSize) || ![2, 4, 6].includes(value.teamSize)) {
    throw new Error("invalid custom team size");
  }
  if (typeof value.banSequence !== "string" || value.banSequence.length > 12 || !/^[AB]*$/u.test(value.banSequence)) {
    throw new Error("invalid custom ban sequence");
  }
  if (
    typeof value.pickSequence !== "string" ||
    value.pickSequence.length > 24 ||
    value.pickSequence.length !== value.teamSize * 2 ||
    !/^[AB]+$/u.test(value.pickSequence) ||
    [...value.pickSequence].filter((side) => side === "A").length !== value.teamSize ||
    [...value.pickSequence].filter((side) => side === "B").length !== value.teamSize
  ) {
    throw new Error("invalid custom pick sequence");
  }
  return value;
}

function parseTimerSeconds(value) {
  if (value === undefined) return undefined;
  if (!Number.isInteger(value) || !VALID_TIMER_SECONDS.has(value)) {
    throw new Error("invalid timer");
  }
  return value;
}

function publicRoom(room) {
  return {
    schema: room.schema,
    status: room.status,
    version: room.version,
    rules: room.rules,
    teamNames: room.teamNames,
    currentStep: room.currentStep,
    picks: room.picks,
    bans: room.bans,
    turnStartedAt: room.turnStartedAt,
    turnDeadlineAt: room.turnDeadlineAt,
    timedOut: room.timedOut
  };
}

function heroUsed(room, heroId) {
  return room.picks.A.includes(heroId) || room.picks.B.includes(heroId) || room.bans.A.includes(heroId) || room.bans.B.includes(heroId);
}

function seatCanAct(role, side) {
  return (role === "host" && side === "A") || (role === "guest" && side === "B");
}

function deadlineFrom(now, timerSeconds) {
  return timerSeconds > 0 ? now + timerSeconds * 1000 : null;
}

function cleanTeamName(value, fallback) {
  const text = String(value || "").replace(/[\u0000-\u001f\u007f]/gu, "").trim().slice(0, 40);
  return text || fallback;
}

function originAllowed(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return false;
  if (origin === env.ALLOWED_ORIGIN) return true;
  try {
    const url = new URL(origin);
    return ["localhost", "127.0.0.1"].includes(url.hostname);
  } catch {
    return false;
  }
}

function normalizedPath(path) {
  return path.replace(/\/{2,}/gu, "/");
}

function enforceSocketRate(attachment) {
  const now = Date.now();
  if (!attachment.windowStartedAt || now - attachment.windowStartedAt >= MESSAGE_WINDOW_MS) {
    return { ...attachment, count: 1, windowStartedAt: now };
  }
  const count = Number(attachment.count || 0) + 1;
  if (count > MAX_MESSAGES_PER_WINDOW) return null;
  return { ...attachment, count };
}

function randomToken(bytes) {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  let binary = "";
  for (const byte of data) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

async function hashToken(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(value)));
  const bytes = new Uint8Array(digest);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function timingSafeStringEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) return false;
  let diff = 0;
  for (let index = 0; index < left.length; index += 1) diff |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return diff === 0;
}

function json(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...extraHeaders }
  });
}
