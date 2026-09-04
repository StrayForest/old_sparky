import { buildRules, DEFAULT_CUSTOM_RULES, MAX_BANS_PER_TEAM, validateSequence } from "./public/draft-core.js";
import { HERO_BY_ID } from "./public/heroes.js";

const ROOM_TTL_MS = 2 * 60 * 60 * 1000;
const COMPLETED_TTL_MS = 15 * 60 * 1000;
const MAX_BODY_BYTES = 4096;
const MAX_WS_MESSAGE_BYTES = 2048;
const MAX_SPECTATORS = 50;
const MAX_MESSAGES_PER_WINDOW = 40;
const MESSAGE_WINDOW_MS = 10_000;
const MUTABLE_ASSETS = new Set(["/draft/app.js", "/draft/styles.css", "/draft/draft-core.js", "/draft/heroes.js"]);
const KNOWN_PRESET_IDS = new Set(["standard", "community-6v6", "community-6v6-no-timer", "6v6-no-bans", "custom"]);
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
      schema: 2,
      status: "waiting",
      version: 1,
      rules: validateRules(payload.rules),
      teamNames: {
        A: cleanTeamName(payload.teamNames?.A, "Команда 1"),
        B: cleanTeamName(payload.teamNames?.B, "Команда 2")
      },
      currentStep: 0,
      picks: { A: [], B: [] },
      bans: { A: [], B: [] },
      ready: { A: false, B: false },
      turnStartedAt: null,
      turnDeadlineAt: null,
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
      if (message.type === "ready") {
        await this.applyReady(ws, message, attachment);
        return;
      }
      if (message.type === "team-name") {
        await this.updateTeamName(ws, message, attachment);
        return;
      }
      if (message.type === "deadline") {
        await this.advanceExpiredTurn(ws, message, attachment);
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
    if (maybeStartDraft(room, this.connectedSeats())) {
      await this.saveAndBroadcast(room);
    } else {
      this.broadcastState(room);
    }
  }

  async applyReady(ws, message, attachment) {
    if (!["host", "guest"].includes(attachment.role)) {
      this.sendError(ws, "Только игрок может подтвердить готовность");
      return;
    }
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    if (room.status !== "waiting") {
      this.sendState(ws, room);
      return;
    }
    if (!Number.isInteger(message.expectedVersion) || message.expectedVersion !== room.version) {
      this.sendError(ws, "Состояние комнаты изменилось — обновлено актуальное состояние", room);
      return;
    }
    const side = attachment.role === "host" ? "A" : "B";
    room.ready = room.ready || { A: false, B: false };
    room.ready[side] = true;
    room.version += 1;
    maybeStartDraft(room, this.connectedSeats());
    await this.saveAndBroadcast(room);
  }

  async updateTeamName(ws, message, attachment) {
    if (!["host", "guest"].includes(attachment.role)) {
      this.sendError(ws, "Только игрок может менять название команды");
      return;
    }
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    if (room.status !== "waiting") {
      this.sendError(ws, "Название команды уже нельзя менять", room);
      return;
    }
    if (!Number.isInteger(message.expectedVersion) || message.expectedVersion !== room.version) {
      this.sendError(ws, "Состояние комнаты изменилось — обновлено актуальное состояние", room);
      return;
    }
    if (typeof message.name !== "string" || message.name.length > 40) {
      this.sendError(ws, "Название команды слишком длинное", room);
      return;
    }
    const side = attachment.role === "host" ? "A" : "B";
    room.teamNames[side] = cleanTeamName(message.name, side === "A" ? "Команда 1" : "Команда 2");
    room.version += 1;
    await this.saveAndBroadcast(room);
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
    const now = Date.now();
    if (room.turnDeadlineAt && now >= room.turnDeadlineAt) {
      if (advanceRoomWithAutoAction(room, now)) await this.saveAndBroadcast(room);
      return;
    }
    if (!seatCanAct(attachment.role, step.side)) {
      this.sendError(ws, "Сейчас ход другой команды", room);
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

    applyRoomAction(room, step, message.heroId, now);
    await this.saveAndBroadcast(room);
  }

  async advanceExpiredTurn(ws, message, attachment) {
    if (!["host", "guest"].includes(attachment.role)) {
      this.sendError(ws, "Только игрок может подтвердить автоматический ход");
      return;
    }
    const room = await this.ctx.storage.get("room");
    if (!room) {
      ws.close(4404, "room not found");
      return;
    }
    if (room.status !== "drafting" || !room.turnDeadlineAt) return;
    if (!Number.isInteger(message.expectedVersion) || message.expectedVersion !== room.version) {
      this.sendState(ws, room);
      return;
    }
    const now = Date.now();
    if (now < room.turnDeadlineAt) return;
    if (advanceRoomWithAutoAction(room, now)) await this.saveAndBroadcast(room);
  }

  async saveAndBroadcast(room) {
    await this.ctx.storage.put("room", room);
    if (room.status === "drafting" && room.turnDeadlineAt) {
      await this.ctx.storage.setAlarm(Math.min(room.expiresAt, room.turnDeadlineAt));
    } else {
      await this.ctx.storage.setAlarm(room.expiresAt);
    }
    const payload = JSON.stringify({ type: "state", room: publicRoom(room, this.connectedSeats()), serverTime: Date.now() });
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
      ws.send(JSON.stringify({ type: "state", room: publicRoom(room, this.connectedSeats()), serverTime: Date.now() }));
    } catch {
      try { ws.close(1011, "send failed"); } catch { /* noop */ }
    }
  }

  broadcastState(room) {
    const payload = JSON.stringify({ type: "state", room: publicRoom(room, this.connectedSeats()), serverTime: Date.now() });
    for (const ws of this.ctx.getWebSockets()) {
      if (!ws.deserializeAttachment()?.authenticated) continue;
      try { ws.send(payload); } catch { try { ws.close(1011, "send failed"); } catch { /* noop */ } }
    }
  }

  connectedSeats() {
    const presence = { A: false, B: false };
    for (const ws of this.ctx.getWebSockets()) {
      const role = ws.deserializeAttachment()?.role;
      if (role === "host") presence.A = true;
      if (role === "guest") presence.B = true;
    }
    return presence;
  }

  sendError(ws, error, room = null) {
    try {
      ws.send(JSON.stringify({ type: "error", error, room: room ? publicRoom(room, this.connectedSeats()) : undefined }));
    } catch {
      try { ws.close(1011, "send failed"); } catch { /* noop */ }
    }
  }

  async alarm() {
    const room = await this.ctx.storage.get("room");
    const now = Date.now();
    if (room && room.status === "drafting" && room.turnDeadlineAt && now >= room.turnDeadlineAt) {
      if (advanceRoomWithAutoAction(room, now)) await this.saveAndBroadcast(room);
    }
    const refreshed = await this.ctx.storage.get("room");
    if (refreshed && refreshed.expiresAt > now) {
      const nextAlarm = refreshed.status === "drafting" && refreshed.turnDeadlineAt
        ? Math.min(refreshed.expiresAt, refreshed.turnDeadlineAt)
        : refreshed.expiresAt;
      await this.ctx.storage.setAlarm(nextAlarm);
      return;
    }
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.close(4404, "room expired"); } catch { /* noop */ }
    }
    await this.ctx.storage.deleteAll();
  }

  webSocketClose(ws) {
    try { ws.close(1000, "closed"); } catch { /* noop */ }
    void this.broadcastStoredState();
  }

  webSocketError(ws) {
    try { ws.close(1011, "socket error"); } catch { /* noop */ }
    void this.broadcastStoredState();
  }

  async broadcastStoredState() {
    const room = await this.ctx.storage.get("room");
    if (room) this.broadcastState(room);
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
  const presetId = typeof payload?.presetId === "string" ? payload.presetId : "standard";
  let rules;
  try {
    if (!KNOWN_PRESET_IDS.has(presetId)) throw new Error("invalid preset");
    const timerSeconds = parseTimerSeconds(payload?.timerSeconds);
    const customRules = ["standard", "custom"].includes(presetId)
      ? validateCustomCreateSettings(payload?.customRules || {
          teamSize: DEFAULT_CUSTOM_RULES.teamSize,
          banCount: DEFAULT_CUSTOM_RULES.banCount,
          sequence: DEFAULT_CUSTOM_RULES.sequence.map(({ action, side }) => ({ action, side }))
        })
      : null;
    const firstSide = resolveFirstSide(payload?.firstMove);
    rules = buildRules(presetId, timerSeconds, {
      ...(customRules || {}),
      firstSide
    });
    validateRules(rules);
  } catch {
    return json({ error: "Некорректные правила" }, 400);
  }
  const teamNames = {
    A: cleanTeamName(payload?.teamNames?.A, "Команда 1"),
    B: cleanTeamName(payload?.teamNames?.B, "Команда 2")
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
  const isDraftShell = path === "/draft" || path === "/draft/" || path === "/draft/result" || /^\/draft\/[A-Za-z0-9_-]{6,32}\/?$/u.test(path) || path === "/draft/solo";
  if (isDraftShell) {
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
  if (assetPath === "/draft/index.html" || MUTABLE_ASSETS.has(assetPath)) {
    headers.set("Cache-Control", "no-store, max-age=0");
    headers.set("CDN-Cache-Control", "no-store");
    headers.set("Cloudflare-CDN-Cache-Control", "no-store");
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
  if (!Number.isInteger(rules.banCount) || rules.banCount < 0 || rules.banCount > MAX_BANS_PER_TEAM) {
    throw new Error("invalid sequence");
  }
  if (!Array.isArray(rules.sequence)) throw new Error("invalid sequence");
  const sequence = rules.sequence.map(({ action, side }) => ({ action, side }));
  validateSequence(sequence, rules.teamSize, rules.banCount);
  for (const [index, step] of rules.sequence.entries()) {
    if (!step || !["pick", "ban"].includes(step.action) || !["A", "B"].includes(step.side) || step.index !== index) {
      throw new Error("invalid step");
    }
  }
  return rules;
}

function validateCustomCreateSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid custom settings");
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 3 || keys.join(",") !== "banCount,sequence,teamSize") {
    throw new Error("invalid custom settings");
  }
  if (!Number.isInteger(value.teamSize) || ![2, 4, 6].includes(value.teamSize) || !Number.isInteger(value.banCount) || value.banCount < 0 || value.banCount > MAX_BANS_PER_TEAM) {
    throw new Error("invalid custom team size");
  }
  if (!Array.isArray(value.sequence) || value.sequence.some((step) => !step || Object.keys(step).sort().join(",") !== "action,side")) throw new Error("invalid custom sequence");
  validateSequence(value.sequence, value.teamSize, value.banCount);
  return value;
}

function parseTimerSeconds(value) {
  if (value === undefined) return undefined;
  if (!Number.isInteger(value) || !VALID_TIMER_SECONDS.has(value)) {
    throw new Error("invalid timer");
  }
  return value;
}

function publicRoom(room, presence = { A: false, B: false }) {
  return {
    schema: room.schema,
    status: room.status,
    version: room.version,
    rules: room.rules,
    teamNames: room.teamNames,
    ready: room.ready || { A: false, B: false },
    presence,
    currentStep: room.currentStep,
    picks: room.picks,
    bans: room.bans,
    turnStartedAt: room.turnStartedAt,
    turnDeadlineAt: room.turnDeadlineAt,
  };
}

function applyRoomAction(room, step, heroId, now) {
  room[step.action === "pick" ? "picks" : "bans"][step.side].push(heroId);
  room.currentStep += 1;
  room.version += 1;
  room.turnStartedAt = now;
  room.turnDeadlineAt = deadlineFrom(now, room.rules.timerSeconds);
  if (room.currentStep >= room.rules.sequence.length) {
    room.status = "completed";
    room.turnDeadlineAt = null;
    room.expiresAt = now + COMPLETED_TTL_MS;
  }
}

function maybeStartDraft(room, presence) {
  if (room.status !== "waiting" || !room.ready?.A || !room.ready?.B || !presence.A || !presence.B) return false;
  room.status = "drafting";
  const now = Date.now();
  room.turnStartedAt = now;
  room.turnDeadlineAt = deadlineFrom(now, room.rules.timerSeconds);
  return true;
}

function advanceRoomWithAutoAction(room, now) {
  const step = room.rules.sequence[room.currentStep];
  if (!step) return false;
  const heroId = [...HERO_BY_ID.keys()].find((candidate) => !heroUsed(room, candidate));
  if (!heroId) return false;
  applyRoomAction(room, step, heroId, now);
  return true;
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

function resolveFirstSide(value) {
  if (value === undefined) return "A";
  if (value === "B") return "B";
  if (value === "random") {
    const bytes = new Uint8Array(1);
    crypto.getRandomValues(bytes);
    return bytes[0] % 2 === 0 ? "A" : "B";
  }
  if (value === "A") return "A";
  throw new Error("invalid first move");
}

function cleanTeamName(value, fallback) {
  const text = String(value || "").replace(/[\u0000-\u001f\u007f]/gu, "").trim().slice(0, 40);
  return text || fallback;
}

function originAllowed(request, env) {
  if (isLocalRequestHost(request)) return true;
  if (sameOriginRefererAllowed(request)) return true;
  const origin = request.headers.get("Origin");
  if (!origin) return sameOriginHostAllowed(request, env);
  if (origin === env.ALLOWED_ORIGIN) return true;
  if (/^https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?$/u.test(origin)) return true;
  try {
    const url = new URL(origin);
    return ["localhost", "127.0.0.1"].includes(url.hostname);
  } catch {
    return false;
  }
}

function sameOriginRefererAllowed(request) {
  const referer = request.headers.get("Referer");
  if (!referer) return false;
  try {
    const requestUrl = new URL(request.url);
    const refererUrl = new URL(referer);
    return refererUrl.protocol === requestUrl.protocol && refererUrl.hostname === requestUrl.hostname;
  } catch {
    return false;
  }
}

function sameOriginHostAllowed(request, env) {
  try {
    const requestUrl = new URL(request.url);
    const allowedUrl = new URL(env.ALLOWED_ORIGIN);
    return requestUrl.hostname === allowedUrl.hostname || isLocalRequestHost(request);
  } catch {
    return false;
  }
}

function isLocalRequestHost(request) {
  try {
    const url = new URL(request.url);
    return ["localhost", "127.0.0.1"].includes(url.hostname) || (url.protocol === "http:" && url.hostname === "old-sparky.com");
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
