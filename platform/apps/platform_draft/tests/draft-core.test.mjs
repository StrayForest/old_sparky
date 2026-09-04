import assert from "node:assert/strict";
import test from "node:test";

import {
  PRESETS,
  applyLocalAction,
  applyLocalTimeout,
  buildRules,
  cycleSequenceCell,
  createDefaultSequence,
  createLocalRoom,
  decodeResult,
  encodeResult
} from "../public/draft-core.js";


test("community 6v6 has six bans and six picks per team", () => {
  const rules = buildRules("community-6v6", 30);
  assert.equal(rules.teamSize, 6);
  assert.equal(rules.banCount, 3);
  assert.deepEqual(rules.banCounts, { A: 3, B: 3 });
  assert.equal(rules.timerSeconds, 30);
  assert.equal(rules.sequence.filter((step) => step.action === "ban").length, 6);
  assert.equal(rules.sequence.filter((step) => step.action === "pick").length, 12);
  assert.equal(rules.sequence.filter((step) => step.action === "pick" && step.side === "A").length, 6);
  assert.equal(rules.sequence.filter((step) => step.action === "pick" && step.side === "B").length, 6);
  assert.deepEqual(
    rules.sequence.filter((step) => step.action === "ban").map((step) => step.side),
    ["A", "B", "B", "A", "A", "B"]
  );
});


test("local actions advance the authoritative sequence and reject reused heroes", () => {
  const room = createLocalRoom(buildRules("community-6v6", 30), { A: "Alpha", B: "Bravo" });
  const afterAbrams = applyLocalAction(room, "abrams");
  assert.equal(afterAbrams.version, room.version + 1);
  assert.equal(afterAbrams.currentStep, 1);
  assert.deepEqual(afterAbrams.bans.A, ["abrams"]);

  const afterDuplicate = applyLocalAction(afterAbrams, "abrams");
  assert.strictEqual(afterDuplicate, afterAbrams);

  const afterHaze = applyLocalAction(afterAbrams, "haze");
  assert.equal(afterHaze.currentStep, 2);
  assert.deepEqual(afterHaze.bans.B, ["haze"]);
});


test("local timeout automatically consumes the next free hero", () => {
  const room = createLocalRoom(buildRules("standard", 30), { A: "Alpha", B: "Bravo" });
  room.turnDeadlineAt = Date.now() - 1;
  const next = applyLocalTimeout(room, ["abrams", "haze"]);
  assert.equal(next.currentStep, 1);
  assert.deepEqual(next.bans.A, ["abrams"]);
  assert.equal(next.timedOut, undefined);
});

test("local timeout automatically picks when the current step is a pick", () => {
  const rules = buildRules("standard", 30, {
    teamSize: 2,
    banCount: 0,
    sequence: createDefaultSequence(2, 0, "A")
  });
  const room = createLocalRoom(rules, { A: "Alpha", B: "Bravo" });
  room.turnDeadlineAt = Date.now() - 1;
  const next = applyLocalTimeout(room, ["abrams", "haze"]);
  assert.equal(next.currentStep, 1);
  assert.deepEqual(next.picks.A, ["abrams"]);
  assert.deepEqual(next.bans.A, []);
});


test("custom sequence keeps every configured ban and pick step", () => {
  const sequence = [
    { action: "ban", side: "B" },
    { action: "pick", side: "A" },
    { action: "ban", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" }
  ];
  const rules = buildRules("standard", 30, { teamSize: 2, banCount: 2, sequence, firstSide: "B" });
  assert.deepEqual(rules.sequence, sequence.map((step, index) => ({ ...step, index })));
  assert.equal(rules.sequence.length, 6);
  assert.deepEqual(rules.banCounts, { A: 1, B: 1 });
  assert.deepEqual(createDefaultSequence(2, 0, "B"), [
    { action: "pick", side: "B" },
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" }
  ]);
});

test("manual sequence may change ban count within the per-team limit", () => {
  const sequence = [
    { action: "pick", side: "B" },
    { action: "ban", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" }
  ];
  const rules = buildRules("standard", 30, { teamSize: 2, banCount: 0, sequence, firstSide: "A" });
  assert.deepEqual(rules.sequence.map(({ action }) => action), ["pick", "ban", "pick", "pick", "pick"]);
  assert.equal(rules.sequence[0].side, "A");
  assert.deepEqual(rules.banCounts, { A: 0, B: 1 });
});

test("default sequence treats ban count as bans per team", () => {
  const sequence = createDefaultSequence(6, 3, "A");
  assert.equal(sequence.filter((step) => step.action === "ban").length, 6);
  assert.equal(sequence.filter((step) => step.action === "ban" && step.side === "A").length, 3);
  assert.equal(sequence.filter((step) => step.action === "ban" && step.side === "B").length, 3);
});

test("sequence editor cycles empty, pick and ban cells and switches rows", () => {
  let slots = [null, null];
  slots = cycleSequenceCell(slots, 0, "A");
  assert.deepEqual(slots, [{ action: "pick", side: "A" }, null]);
  slots = cycleSequenceCell(slots, 0, "A");
  assert.deepEqual(slots, [{ action: "ban", side: "A" }, null]);
  slots = cycleSequenceCell(slots, 0, "A");
  assert.deepEqual(slots, [null, null]);
  slots = cycleSequenceCell(slots, 1, "B");
  assert.deepEqual(slots, [null, { action: "pick", side: "B" }]);
  slots = cycleSequenceCell(slots, 1, "A");
  assert.deepEqual(slots, [null, { action: "pick", side: "A" }]);
});

test("an explicit empty custom sequence is rejected instead of falling back to standard", () => {
  assert.throws(() => buildRules("standard", 30, { teamSize: 2, banCount: 0, sequence: [] }));
});

test("manual sequence rejects more than three bans for one team", () => {
  const sequence = [
    { action: "ban", side: "A" },
    { action: "ban", side: "A" },
    { action: "ban", side: "A" },
    { action: "ban", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "B" }
  ];
  assert.throws(() => buildRules("standard", 30, { teamSize: 2, banCount: 3, sequence }));
});


test("all shipped presets normalize to bounded indexed sequences", () => {
  for (const preset of Object.values(PRESETS)) {
    const rules = buildRules(preset.id, preset.timerSeconds);
    assert.ok(rules.sequence.length >= 4 && rules.sequence.length <= 40);
    rules.sequence.forEach((step, index) => assert.equal(step.index, index));
  }
});


test("result payload round-trips without server storage", () => {
  let room = createLocalRoom(buildRules("6v6-no-bans", 30), { A: "Команда А", B: "Команда Б" });
  for (const hero of ["abrams", "haze", "bebop", "ivy", "dynamo", "warden", "shiv", "seven", "lash", "yamato", "viscous", "vindicta"]) {
    room = applyLocalAction(room, hero);
  }
  assert.equal(room.status, "completed");

  const encoded = encodeResult(room);
  assert.match(encoded, /^[A-Za-z0-9_-]+$/u);
  const decoded = decodeResult(encoded);
  assert.equal(decoded.v, 1);
  assert.equal(decoded.a, "Команда А");
  assert.equal(decoded.b, "Команда Б");
  assert.equal(decoded.pa.length, 6);
  assert.equal(decoded.pb.length, 6);
});

test("compact result payload keeps sequence and shortens hero data", () => {
  const heroes = ["abrams", "haze", "bebop", "ivy", "dynamo", "warden", "shiv", "seven", "lash", "yamato", "viscous", "vindicta"];
  let room = createLocalRoom(buildRules("6v6-no-bans", 30), { A: "Команда А", B: "Команда Б" });
  for (const hero of heroes) room = applyLocalAction(room, hero);
  const legacy = encodeResult(room);
  const compact = encodeResult(room, heroes);
  const decoded = decodeResult(compact, heroes);
  assert.equal(decoded.v, 2);
  assert.deepEqual(decoded.pa, room.picks.A);
  assert.equal(decoded.sequence.length, room.rules.sequence.length);
  assert.ok(compact.length < legacy.length);
});

test("timer cannot be disabled", () => {
  assert.throws(() => buildRules("standard", 0));
});


test("malformed and oversized result fragments are rejected", () => {
  assert.throws(() => decodeResult(""));
  assert.throws(() => decodeResult("x".repeat(6001)));
});
