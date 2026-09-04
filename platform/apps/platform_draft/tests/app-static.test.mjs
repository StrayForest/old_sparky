import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appSource = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
const coreSource = await readFile(new URL("../public/draft-core.js", import.meta.url), "utf8");
const createSource = appSource.slice(appSource.indexOf("function renderCreate()"), appSource.indexOf("function resetCreateSequence()"));
const sequenceSource = appSource.slice(appSource.indexOf("function renderSequenceEditor()"), appSource.indexOf("function renderCreate()"));

test("create screen keeps the requested control order and default format order", () => {
  const labels = ["Формат", "Баны на команду", "Таймер хода", "Первый ход"];
  const positions = labels.map((label) => createSource.indexOf(`field-title">${label}`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual([...positions].sort((left, right) => left - right), positions);
  assert.match(coreSource, /TEAM_SIZES = Object\.freeze\(\[6, 4, 2\]\)/u);
  assert.doesNotMatch(createSource, /id="team-[ab]"/u);
});

test("sequence editor has unlabeled two-row cells with a three-state interaction", () => {
  assert.doesNotMatch(sequenceSource, /Команда [12]/u);
  assert.match(sequenceSource, /data-sequence-side="\$\{side\}"/u);
  assert.match(appSource, /cycleSequenceCell\(customSequence, index, side\)/u);
  assert.match(sequenceSource, /Серые ячейки/u);
});

test("create showcase markup and styles are removed", () => {
  assert.doesNotMatch(appSource, /preview|Предпросмотр/u);
  assert.doesNotMatch(stylesSource, /preview|Предпросмотр/u);
});
