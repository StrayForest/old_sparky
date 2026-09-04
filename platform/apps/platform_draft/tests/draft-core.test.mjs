import assert from "node:assert/strict";
import test from "node:test";

import {
  PRESETS,
  applyLocalAction,
  applyLocalTimeout,
  buildRules,
  countSequenceActions,
  cycleSequenceCell,
  createDefaultSequence,
  createLocalRoom
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

test("local actions cannot exceed pick or ban stock", () => {
  const pickRules = buildRules("standard", 30, {
    teamSize: 2,
    banCount: 0,
    sequence: createDefaultSequence(2, 0, "A")
  });
  const pickRoom = createLocalRoom(pickRules, { A: "Alpha", B: "Bravo" });
  pickRoom.picks.A = ["abrams", "haze"];
  assert.strictEqual(applyLocalAction(pickRoom, "bebop"), pickRoom);

  const banRules = buildRules("standard", 30, {
    teamSize: 2,
    banCount: 1,
    sequence: createDefaultSequence(2, 1, "A")
  });
  const banRoom = createLocalRoom(banRules, { A: "Alpha", B: "Bravo" });
  banRoom.bans.A = ["abrams"];
  assert.strictEqual(applyLocalAction(banRoom, "haze"), banRoom);
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

test("manual sequence may use fewer bans than the configured per-team limit", () => {
  const sequence = [
    { action: "pick", side: "B" },
    { action: "ban", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "A" }
  ];
  const rules = buildRules("standard", 30, { teamSize: 2, banCount: 2, sequence, firstSide: "A" });
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

test("sequence editor enforces configured pick and ban quotas per row", () => {
  const limits = { pickLimit: 2, banLimit: 1 };
  let slots = [
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "ban", side: "A" },
    null
  ];
  assert.deepEqual(countSequenceActions(slots, "A"), { pick: 2, ban: 1 });
  assert.strictEqual(cycleSequenceCell(slots, 3, "A", limits), slots);

  slots = cycleSequenceCell(slots, 0, "A", limits);
  assert.equal(slots[0], null);
  assert.deepEqual(countSequenceActions(slots, "A"), { pick: 1, ban: 1 });
  slots = cycleSequenceCell(slots, 3, "A", limits);
  assert.deepEqual(slots[3], { action: "pick", side: "A" });

  slots = cycleSequenceCell(slots, 2, "A", limits);
  assert.equal(slots[2], null);
  slots = cycleSequenceCell(slots, 0, "A", limits);
  assert.deepEqual(slots[0], { action: "ban", side: "A" });
  assert.deepEqual(countSequenceActions(slots, "A"), { pick: 2, ban: 1 });
});

test("an explicit empty custom sequence is rejected instead of falling back to standard", () => {
  assert.throws(() => buildRules("standard", 30, { teamSize: 2, banCount: 0, sequence: [] }));
});

test("manual sequence rejects more bans than the configured per-team limit", () => {
  const sequence = [
    { action: "ban", side: "A" },
    { action: "ban", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "A" },
    { action: "pick", side: "B" },
    { action: "pick", side: "B" }
  ];
  assert.throws(() => buildRules("standard", 30, { teamSize: 2, banCount: 1, sequence }));
});


test("all shipped presets normalize to bounded indexed sequences", () => {
  for (const preset of Object.values(PRESETS)) {
    const rules = buildRules(preset.id, preset.timerSeconds);
    assert.ok(rules.sequence.length >= 4 && rules.sequence.length <= 40);
    rules.sequence.forEach((step, index) => assert.equal(step.index, index));
  }
});


test("timer cannot be disabled", () => {
  assert.throws(() => buildRules("standard", 0));
});
