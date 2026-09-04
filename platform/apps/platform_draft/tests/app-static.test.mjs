import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appSource = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
const coreSource = await readFile(new URL("../public/draft-core.js", import.meta.url), "utf8");
const createSource = appSource.slice(appSource.indexOf("function renderCreate()"), appSource.indexOf("function resetCreateSequence()"));
const sequenceSource = appSource.slice(appSource.indexOf("function renderSequenceEditor()"), appSource.indexOf("function renderCreate()"));
const liveSequenceSource = appSource.slice(appSource.indexOf("function renderLiveSequence()"), appSource.indexOf("function renderActionBar("));

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
  assert.match(appSource, /cycleSequenceCell\(customSequence, index, side, \{/u);
  assert.match(appSource, /pickLimit: selectedTeamSize/u);
  assert.match(appSource, /banLimit: selectedBanCount/u);
  assert.doesNotMatch(sequenceSource, /Серые ячейки/u);
});

test("create sequence shows pick and ban quotas after each editable row", () => {
  assert.match(sequenceSource, /renderCreateSequenceCounters\(side\)/u);
  assert.match(sequenceSource, /sequence-counter--pick/u);
  assert.match(sequenceSource, /sequence-counter--ban/u);
  assert.match(sequenceSource, /\$\{counts\.pick\}\/\$\{selectedTeamSize\}/u);
  assert.match(sequenceSource, /\$\{counts\.ban\}\/\$\{selectedBanCount\}/u);
  assert.match(stylesSource, /\.sequence-track--editable \{\s*grid-template-columns: repeat\(var\(--sequence-length\), 32px\) 42px 42px;/u);
});

test("create showcase markup and styles are removed", () => {
  assert.doesNotMatch(appSource, /preview|Предпросмотр/u);
  assert.doesNotMatch(stylesSource, /preview|Предпросмотр/u);
});

test("draft controls use team A/B labels and finite timers", () => {
  assert.match(createSource, /Команда А/u);
  assert.match(createSource, /Команда Б/u);
  assert.doesNotMatch(createSource, /Выкл/u);
  assert.match(appSource, /sequence-editor--live/u);
  assert.doesNotMatch(appSource, /ROOM <strong>/u);
});

test("live sequence frames the active column without quota counters", () => {
  assert.doesNotMatch(liveSequenceSource, /sequence-counter/u);
  assert.doesNotMatch(appSource, /slot-counter/u);
  assert.match(stylesSource, /\.sequence-track--a \.sequence-editor-step--live\.current/u);
  assert.match(stylesSource, /\.sequence-track--b \.sequence-editor-step--live\.current/u);
  assert.match(stylesSource, /\.sequence-editor-step\.ban > span/u);
  assert.match(stylesSource, /\.sequence-editor-step \{[\s\S]*padding: 0;/u);
  assert.match(stylesSource, /\.sequence-counter--pick \{\s*color: var\(--secondary\);/u);
  assert.match(stylesSource, /background: linear-gradient\(135deg, var\(--primary-deep\), var\(--primary\)\)/u);
});

test("stateless result routes and link controls are removed", () => {
  assert.doesNotMatch(appSource, /open-result|copy-result|encodeResult|decodeResult|\/draft\/result|#v2\./u);
  assert.doesNotMatch(coreSource, /encodeResult|decodeResult|makeResultPayload/u);
});
