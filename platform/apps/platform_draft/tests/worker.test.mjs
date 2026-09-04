import assert from "node:assert/strict";
import test from "node:test";

import worker, { DraftRoom } from "../worker.js";
import { buildRules } from "../public/draft-core.js";

function makeEnvironment() {
  const created = [];
  const stub = {
    fetch(input, init) {
      const request = new Request(input, init);
      return request.json().then((body) => {
        created.push(body);
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" }
        });
      });
    }
  };
  return {
    created,
    env: {
      ALLOWED_ORIGIN: "https://old-sparky.com",
      DRAFT_ROOMS: {
        idFromName: (name) => name,
        get: () => stub
      }
    }
  };
}

async function createRoom(body, environment = makeEnvironment(), origin = "https://old-sparky.com") {
  const response = await worker.fetch(
    new Request("https://old-sparky.com/draft/api/rooms", {
      method: "POST",
      headers: {
        Origin: origin,
        "content-type": "application/json"
      },
      body: JSON.stringify(body)
    }),
    environment.env
  );
  return { response, environment };
}

test("same-origin room creation works without an Origin header", async () => {
  const environment = makeEnvironment();
  const response = await worker.fetch(
    new Request("https://old-sparky.com/draft/api/rooms", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ presetId: "6v6-no-bans", timerSeconds: 0 })
    }),
    environment.env
  );
  assert.equal(response.status, 201);
  assert.equal(environment.created.length, 1);
});

test("local browser origins with a port are accepted for development", async () => {
  const { response } = await createRoom({ presetId: "6v6-no-bans", timerSeconds: 0 }, makeEnvironment(), "http://localhost:8788");
  assert.equal(response.status, 201);
});

test("Wrangler local websocket origins are accepted during local development", async () => {
  const environment = makeEnvironment();
  const response = await worker.fetch(
    new Request("http://old-sparky.com/draft/api/rooms", {
      method: "POST",
      headers: {
        Origin: "http://old-sparky.com",
        "content-type": "application/json"
      },
      body: JSON.stringify({ presetId: "6v6-no-bans", timerSeconds: 0 })
    }),
    environment.env
  );
  assert.equal(response.status, 201);
});

test("same-origin browser requests may use their same-origin referrer", async () => {
  const environment = makeEnvironment();
  const response = await worker.fetch(
    new Request("https://old-sparky.com/draft/api/rooms", {
      method: "POST",
      headers: {
        Referer: "https://old-sparky.com/draft",
        "content-type": "application/json"
      },
      body: JSON.stringify({ presetId: "6v6-no-bans", timerSeconds: 0 })
    }),
    environment.env
  );
  assert.equal(response.status, 201);
});

class MemoryStorage {
  constructor() {
    this.values = new Map();
    this.alarmAt = null;
  }

  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async setAlarm(value) { this.alarmAt = value; }
  async deleteAll() { this.values.clear(); }
}

class FakeSocket {
  constructor(attachment) {
    this.attachment = attachment;
    this.messages = [];
    this.bufferedAmount = 0;
  }

  serializeAttachment(value) { this.attachment = value; }
  deserializeAttachment() { return this.attachment; }
  send(value) { this.messages.push(JSON.parse(value)); }
  close() {}
}

function makeDraftRoomContext() {
  const sockets = [];
  const storage = new MemoryStorage();
  return {
    storage,
    sockets,
    ctx: {
      storage,
      acceptWebSocket() {},
      getWebSockets: () => sockets
    }
  };
}

async function seedDraftRoom() {
  const environment = makeDraftRoomContext();
  const draft = new DraftRoom(environment.ctx, {});
  await draft.create(new Request("https://draft-room.internal/create", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      rules: buildRules("standard", 30),
      teamNames: { A: "Команда A", B: "Команда B" },
      hostToken: "host-token-12345678901234567890",
      guestToken: "guest-token-12345678901234567890"
    })
  }));
  return { ...environment, draft };
}

test("online room creation preserves the submitted custom sequence", async () => {
  const { response, environment } = await createRoom({
    presetId: "standard",
    timerSeconds: 0,
    firstMove: "B",
    customRules: {
      teamSize: 2,
      banCount: 2,
      sequence: [
        { action: "ban", side: "B" },
        { action: "pick", side: "A" },
        { action: "ban", side: "A" },
        { action: "pick", side: "B" },
        { action: "pick", side: "A" },
        { action: "pick", side: "B" }
      ]
    },
    teamNames: { A: "Alpha", B: "Bravo" }
  });

  assert.equal(response.status, 201);
  assert.equal(environment.created.length, 1);
  assert.deepEqual(environment.created[0].rules, {
    presetId: "standard",
    teamSize: 2,
    banCount: 2,
    banCounts: { A: 1, B: 1 },
    firstSide: "B",
    timerSeconds: 0,
    sequence: [
      { action: "ban", side: "B", index: 0 },
      { action: "pick", side: "A", index: 1 },
      { action: "ban", side: "A", index: 2 },
      { action: "pick", side: "B", index: 3 },
      { action: "pick", side: "A", index: 4 },
      { action: "pick", side: "B", index: 5 }
    ]
  });
});

test("online room creation rejects custom settings the browser would otherwise normalize", async () => {
  const { response, environment } = await createRoom({
    presetId: "standard",
    timerSeconds: 30,
    customRules: {
      teamSize: 2,
      banCount: 1,
      sequence: [
        { action: "ban", side: "A" },
        { action: "pick", side: "A" },
        { action: "pick", side: "B" },
        { action: "pick", side: "A" },
        { action: "pick", side: "B" }
      ],
      extra: true
    }
  });

  assert.equal(response.status, 400);
  assert.equal(environment.created.length, 0);
});

test("online room creation rejects unbalanced custom picks and extra settings", async () => {
  const { response, environment } = await createRoom({
    presetId: "standard",
    timerSeconds: 30,
    customRules: {
      teamSize: 4,
      banCount: 0,
      sequence: [
        { action: "pick", side: "A" },
        { action: "pick", side: "A" },
        { action: "pick", side: "A" },
        { action: "pick", side: "A" },
        { action: "pick", side: "B" },
        { action: "pick", side: "B" },
        { action: "pick", side: "B" },
        { action: "pick", side: "A" }
      ]
    }
  });

  assert.equal(response.status, 400);
  assert.equal(environment.created.length, 0);
});

test("online room stays in lobby until both seats are ready and syncs team names", async () => {
  const { draft, storage, sockets } = await seedDraftRoom();
  const host = new FakeSocket({ role: "host", authenticated: true, count: 0, windowStartedAt: Date.now() });
  const guest = new FakeSocket({ role: "guest", authenticated: true, count: 0, windowStartedAt: Date.now() });
  sockets.push(host, guest);

  await draft.webSocketMessage(host, JSON.stringify({ type: "team-name", expectedVersion: 1, name: "Alpha" }));
  let room = await storage.get("room");
  assert.equal(room.status, "waiting");
  assert.equal(room.teamNames.A, "Alpha");

  await draft.webSocketMessage(host, JSON.stringify({ type: "ready", expectedVersion: 2 }));
  room = await storage.get("room");
  assert.equal(room.status, "waiting");
  assert.deepEqual(room.ready, { A: true, B: false });

  await draft.webSocketMessage(guest, JSON.stringify({ type: "ready", expectedVersion: 3 }));
  room = await storage.get("room");
  assert.equal(room.status, "drafting");
  assert.deepEqual(room.ready, { A: true, B: true });
  assert.equal(room.currentStep, 0);
  assert.ok(host.messages.some((message) => message.type === "state" && message.room.status === "drafting"));
});

test("ready lobby starts after a captain reconnects", async () => {
  const { draft, storage, sockets } = await seedDraftRoom();
  const host = new FakeSocket({ role: "host", authenticated: true, count: 0, windowStartedAt: Date.now() });
  const guest = new FakeSocket({ role: "guest", authenticated: true, count: 0, windowStartedAt: Date.now() });
  sockets.push(host);

  await draft.webSocketMessage(host, JSON.stringify({ type: "ready", expectedVersion: 1 }));
  sockets.splice(sockets.indexOf(host), 1);
  sockets.push(guest);
  await draft.webSocketMessage(guest, JSON.stringify({ type: "ready", expectedVersion: 2 }));
  let room = await storage.get("room");
  assert.equal(room.status, "waiting");
  assert.deepEqual(room.ready, { A: true, B: true });

  const reconnectedHost = new FakeSocket({ role: "pending", authenticated: false, count: 0, windowStartedAt: Date.now() });
  sockets.push(reconnectedHost);
  await draft.authenticateSocket(
    reconnectedHost,
    { role: "host", token: "host-token-12345678901234567890" },
    reconnectedHost.deserializeAttachment()
  );
  room = await storage.get("room");
  assert.equal(room.status, "drafting");
  assert.ok(room.turnStartedAt);
});

test("expired online turn automatically applies the current ban", async () => {
  const { draft, storage, sockets } = await seedDraftRoom();
  const host = new FakeSocket({ role: "host", authenticated: true, count: 0, windowStartedAt: Date.now() });
  sockets.push(host);
  const room = await storage.get("room");
  room.status = "drafting";
  room.turnStartedAt = Date.now() - 31_000;
  room.turnDeadlineAt = Date.now() - 1;
  await storage.put("room", room);

  await draft.webSocketMessage(host, JSON.stringify({ type: "deadline", expectedVersion: room.version }));
  const advanced = await storage.get("room");
  assert.equal(advanced.currentStep, 1);
  assert.deepEqual(advanced.bans.A, ["abrams"]);
  assert.equal(advanced.timedOut, undefined);
  assert.ok(advanced.turnDeadlineAt > Date.now());
});

test("spectators cannot trigger the automatic deadline action", async () => {
  const { draft, storage, sockets } = await seedDraftRoom();
  const spectator = new FakeSocket({ role: "spectator", authenticated: true, count: 0, windowStartedAt: Date.now() });
  sockets.push(spectator);
  const room = await storage.get("room");
  room.status = "drafting";
  room.turnStartedAt = Date.now() - 31_000;
  room.turnDeadlineAt = Date.now() - 1;
  await storage.put("room", room);

  await draft.webSocketMessage(spectator, JSON.stringify({ type: "deadline", expectedVersion: room.version }));
  const unchanged = await storage.get("room");
  assert.equal(unchanged.currentStep, 0);
  assert.equal(spectator.messages.at(-1).type, "error");
});
