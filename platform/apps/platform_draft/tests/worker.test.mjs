import assert from "node:assert/strict";
import test from "node:test";

import worker from "../worker.js";

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

async function createRoom(body, environment = makeEnvironment()) {
  const response = await worker.fetch(
    new Request("https://old-sparky.com/draft/api/rooms", {
      method: "POST",
      headers: {
        Origin: "https://old-sparky.com",
        "content-type": "application/json"
      },
      body: JSON.stringify(body)
    }),
    environment.env
  );
  return { response, environment };
}

test("online room creation preserves the submitted custom sequence", async () => {
  const { response, environment } = await createRoom({
    presetId: "custom",
    timerSeconds: 0,
    customRules: {
      teamSize: 2,
      banSequence: "BAAB",
      pickSequence: "BABA"
    },
    teamNames: { A: "Alpha", B: "Bravo" }
  });

  assert.equal(response.status, 201);
  assert.equal(environment.created.length, 1);
  assert.deepEqual(environment.created[0].rules, {
    presetId: "custom",
    teamSize: 2,
    timerSeconds: 0,
    sequence: [
      { action: "ban", side: "B", index: 0 },
      { action: "ban", side: "A", index: 1 },
      { action: "ban", side: "A", index: 2 },
      { action: "ban", side: "B", index: 3 },
      { action: "pick", side: "B", index: 4 },
      { action: "pick", side: "A", index: 5 },
      { action: "pick", side: "B", index: 6 },
      { action: "pick", side: "A", index: 7 }
    ]
  });
});

test("online room creation rejects custom settings the browser would otherwise normalize", async () => {
  const { response, environment } = await createRoom({
    presetId: "custom",
    timerSeconds: 30,
    customRules: {
      teamSize: 2,
      banSequence: "AB!",
      pickSequence: "ABBA"
    }
  });

  assert.equal(response.status, 400);
  assert.equal(environment.created.length, 0);
});

test("online room creation rejects unbalanced custom picks and extra settings", async () => {
  const { response, environment } = await createRoom({
    presetId: "custom",
    timerSeconds: 30,
    customRules: {
      teamSize: 4,
      banSequence: "",
      pickSequence: "AAAABBBB",
      timerSeconds: 30
    }
  });

  assert.equal(response.status, 400);
  assert.equal(environment.created.length, 0);
});
