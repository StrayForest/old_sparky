import type {
  PlatformPatchChangeList,
  PlatformPatchChangeGroup,
  PlatformPatchDetail,
  PlatformPatchSection,
} from "../../lib/platform-types";
import { expect } from "@playwright/test";
import type { Locator } from "@playwright/test";

export type PatchAcceptanceIssue =
  | "untranslated_change"
  | "steam_markup"
  | "detached_owner_or_header"
  | "duplicated_percent"
  | "invented_percent_speed_unit"
  | "effectiveness_comparison"
  | "missing_street_brawl_owner"
  | "move_piece_owner";

const RAW_STEAM_MARKUP = /STEAM_CLAN_LOC_IMAGE|clan\.fastly\.steamstatic\.com|\[\s*\/?\s*(?:img|url|p|b|i|list|h[1-6])\b|\[\s*\/\s*\*\s*\]/iu;
const OWNER_OR_SECTION_LABELS = [
  "General", "Heroes", "Items", "Objectives", "Nurse", "Doorman",
  "Mid Boss", "Mid-Boss", "Street Brawl", "Общие изменения", "Герои", "Предметы",
];
const BASELINE_WORD = "(?:обычн\\p{L}*|базов\\p{L}*|прежн\\p{L}*|нормальн\\p{L}*|исходн\\p{L}*)";
const MIXED_ENGLISH_PATCH_WORD = /\b(?:now|from|to|by|per|with|for|increased?|reduced?|decreased?|improved?|lowered?|raised?|removed?|added|fixed|deals?|grants?|gains?|loses?|less|more|better|worse|effective|effectively|damage|cooldown|duration|range|radius|speed|health|healing|resist(?:ance)?|scaling|increases?|reduces?|decreases?|changed|base|fire|rate|bullets?|shots?|seconds?|minutes?)\b/iu;

type PatchRow = { section: PlatformPatchSection; change: string; abilityName?: string };

function allRows(patch: PlatformPatchDetail): PatchRow[] {
  return patch.sections.flatMap((section) => [
    ...section.changes.map((change) => ({ section, change })),
    ...section.abilities.flatMap((ability) => ability.changes.map((change) => ({
      section,
      change,
      abilityName: ability.name,
    }))),
  ]);
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
}

function identityLabels(patch: PlatformPatchDetail): string[] {
  const labels = new Set<string>(OWNER_OR_SECTION_LABELS);
  for (const value of patch.canonical_entity_names || []) {
    const label = value.trim();
    if (label) labels.add(label);
  }
  for (const section of patch.sections) {
    for (const value of [
      section.title,
      section.mode_name,
      section.hero_name,
      section.hero_display_name,
      section.item_name,
    ]) {
      const label = value?.trim();
      if (!label) continue;
      labels.add(label);
      labels.add(label.replace(/^the\s+/iu, ""));
    }
    for (const ability of section.abilities) {
      for (const value of [ability.name, ability.display_name]) {
        if (value?.trim()) labels.add(value.trim());
      }
    }
  }
  return [...labels].filter(Boolean).sort((left, right) => right.length - left.length);
}

function removeKnownNames(value: string, labels: string[]): string {
  let result = value;
  for (const label of labels) {
    const pieces = label.trim().split(/[\s_\-‐-―]+/u).filter(Boolean);
    if (pieces.length === 0) continue;
    const boundaryClass = /^[\x00-\x7F]*$/u.test(label)
      ? "A-Za-z0-9"
      : "\\p{L}\\p{N}";
    const pattern = [
      new RegExp(`^[${boundaryClass}]`, "u").test(label)
        ? `(?<![${boundaryClass}])`
        : "",
      pieces.map(escapeRegex).join("[\\s_\\-‐-―]*"),
      new RegExp(`[${boundaryClass}]$`, "u").test(label)
        ? `(?![${boundaryClass}])`
        : "",
    ].join("");
    result = result.replace(
      new RegExp(pattern, "giu"),
      " ",
    );
  }
  return result;
}

function hasSubstantiveEnglish(value: string, labels: string[]): boolean {
  const remaining = removeKnownNames(value, labels)
    .replace(/\b(?:tier\s*)?t[1-4]\b/giu, " ")
    .replace(/[+-]?\d+(?:[.,]\d+)?\s*%?/gu, " ")
    .replace(/\b(?:m\/s|ms|sec(?:ond)?s?|mins?|minutes?|hours?|hp|dps)\b/giu, " ")
    .replace(/[^\p{L}]+/gu, " ");
  const latinWords = remaining.match(/[A-Za-z]{3,}/gu) || [];
  if (latinWords.length === 0) return false;
  if (!/[А-Яа-яЁё]/u.test(value)) return true;
  return MIXED_ENGLISH_PATCH_WORD.test(remaining);
}

function isDetachedOwnerOrHeader(change: string, labels: string[]): boolean {
  const trimmed = change.trim();
  if (/^(?:general|heroes|items|objectives|mid[- ]boss|street brawl|общие изменения|герои|предметы)\s*:?\s*$/iu.test(trimmed)) {
    return true;
  }
  for (const label of labels) {
    if (!label || label.length < 3) continue;
    if (new RegExp(`^${escapeRegex(label)}\\s*(?::|[—–-])`, "iu").test(trimmed)) return true;
  }
  return /(?:^|\s)(?:general|heroes|items|objectives|предметы|герои)\s*:\s*$/iu.test(trimmed);
}

function effectivenessIssue(source: string, rows: string[]): boolean {
  const sourceComparisons = [...source.matchAll(/\bonly\s+([+-]?\d+(?:[.,]\d+)?)\s*%\s+as\s+effective\b/giu)];
  for (const match of sourceComparisons) {
    const sourceNumber = match[1];
    if (!sourceNumber) continue;
    const number = escapeRegex(sourceNumber.replace(",", "."));
    const candidateRows = rows.filter((row) => (
      new RegExp(`(?<![\\d.,])${number}\\s*%`, "iu").test(row)
      && /эффект|эффектив|сопротив|воздейств|влиян|дейст|обычн|базов|прежн|нормальн|исходн/iu.test(row)
    ));
    const reversed = candidateRows.some((row) => (
      new RegExp(`(?:на\\s+)?${number}\\s*%\\s*(?:менее|более|меньше|больше|лучше|хуже|сильнее|слабее|эффективнее)`, "iu").test(row)
      || new RegExp(`(?:менее|более|меньше|больше|эффективнее|лучше|хуже)\\s+(?:чем\\s+)?(?:на\\s+)?${number}\\s*%`, "iu").test(row)
    ));
    const fraction = candidateRows.some((row) => (
      new RegExp(`${number}\\s*%\\s*(?:от\\s+)?${BASELINE_WORD}`, "iu").test(row)
      || new RegExp(`${number}\\s*%\\s*(?:от\\s+)?(?:эффективност|эффект|уровн|значен)\\p{L}*`, "iu").test(row)
    ));
    if (reversed || candidateRows.length === 0 || !fraction) return true;
  }
  return false;
}

function sourceLinesForRow(row: PatchRow, source: string): string[] {
  const lines = source.split(/\r?\n/u);
  const hasScopeMarkers = lines.some((line) => /^\s*\[(?:General|Items|Heroes|Objectives)\]\s*$/iu.test(line));
  if (row.section.kind === "general" && hasScopeMarkers) {
    let inGeneral = false;
    const generalLines: string[] = [];
    for (const line of lines) {
      const marker = line.trim().match(/^\[(General|Items|Heroes|Objectives)\]$/iu)?.[1];
      if (marker) {
        inGeneral = marker.toLowerCase() === "general";
        continue;
      }
      if (inGeneral) generalLines.push(line);
    }
    return generalLines;
  }

  const prefixes = row.section.kind === "hero"
    ? [row.section.hero_name || "", ...(row.section.hero_name === "Nurse Harrow" ? ["Nurse"] : [])]
        .map((name) => name.replace(/^the\s+/iu, ""))
    : row.section.kind === "item"
      ? [row.section.item_name || ""]
      : row.section.kind === "objective" && row.section.objective_key === "mid_boss"
        ? ["Mid Boss", "Mid-Boss"]
        : [];
  const prefixPatterns = prefixes.filter(Boolean).map((name) => new RegExp(
    `^\\s*[-*]?\\s*${escapeRegex(name)}\\s*:\\s*`,
    "iu",
  ));
  const matchingOwnerLines = lines.filter((line) => prefixPatterns.some((pattern) => pattern.test(line)));
  if (row.abilityName) {
    const abilityPattern = new RegExp(`\\b${escapeRegex(row.abilityName)}\\s*[-—:]`, "iu");
    return matchingOwnerLines.filter((line) => abilityPattern.test(line));
  }
  if (matchingOwnerLines.length > 0) return matchingOwnerLines;
  if (row.section.kind === "general") return lines;
  return [];
}

function inventedPercentSpeedUnit(row: PatchRow, source: string): boolean {
  const { change } = row;
  const values = [...change.matchAll(/([+-]?\d+(?:[.,]\d+)?)\s*%\s*(?:м\/с|m\/s)(?=$|[^\p{L}])/giu)];
  return values.some((match) => {
    const number = match[1];
    if (!number) return false;
    const sourceLines = sourceLinesForRow(row, source).filter((line) => (
      new RegExp(`(?<![\\d.,])${escapeRegex(number)}\\s*%`, "iu").test(line)
    ));
    if (sourceLines.length !== 1) return true;
    return !new RegExp(
      `(?<![\\d.,])${escapeRegex(number)}\\s*%\\s*(?:м\\s*\/\\s*с|m\\s*\/\\s*s)(?=$|[^\\p{L}])`,
      "iu",
    ).test(sourceLines[0] || "");
  });
}

function hasStreetBrawlHeading(source: string): boolean {
  return /(?:^|\n)\s*street brawl\s*:?\s*(?:\n|$)/iu.test(source);
}

function movePieceSourceRows(source: string): string[] {
  return [...source.matchAll(/\bSolomon:\s*Move Piece\s*[-—:]/giu)].map(
    (match) => match[0] ?? "",
  );
}

/** Return closed, source-aware acceptance failures for the four current patch details. */
export function patchAcceptanceIssues(patch: PlatformPatchDetail): PatchAcceptanceIssue[] {
  const rows = allRows(patch);
  const changes = rows.map(({ change }) => change);
  const labels = identityLabels(patch);
  const source = patch.content || "";
  const issues = new Set<PatchAcceptanceIssue>();

  for (const row of rows) {
    const { change } = row;
    const trimmed = change.trim();
    if (RAW_STEAM_MARKUP.test(change)) issues.add("steam_markup");
    if (isDetachedOwnerOrHeader(trimmed, labels)) issues.add("detached_owner_or_header");
    // Canonical names, recognized abbreviations/units and numeric-only rows
    // may remain English; substantive Latin prose must be localized.
    if (hasSubstantiveEnglish(trimmed, labels)) issues.add("untranslated_change");
    if (/(?:\d+(?:[.,]\d+)?)\s*%{2,}/u.test(change)) issues.add("duplicated_percent");
    if (inventedPercentSpeedUnit(row, source)) {
      issues.add("invented_percent_speed_unit");
    }
  }

  if (effectivenessIssue(source, changes)) issues.add("effectiveness_comparison");

  const modeSections = patch.sections.filter((section) => section.mode_name === "Street Brawl");
  if (hasStreetBrawlHeading(source) && modeSections.length === 0) {
    issues.add("missing_street_brawl_owner");
  }
  if (modeSections.length > 0 && source.trim() && !hasStreetBrawlHeading(source)) {
    issues.add("missing_street_brawl_owner");
  } else if (modeSections.length > 0) {
    const modeRows = modeSections.flatMap((section) => [
      ...section.changes,
      ...section.abilities.flatMap((ability) => ability.changes),
    ]);
    if (
      modeSections.some((section) => section.kind !== "hero" || !section.hero_name)
      || modeRows.some((change) => /street brawl\s*:/iu.test(change))
      || new Set(modeRows).size !== modeRows.length
    ) {
      issues.add("missing_street_brawl_owner");
    }
  }

  const sourceMovePieceRows = movePieceSourceRows(source);
  if (sourceMovePieceRows.length > 0) {
    const movePieceOwners = patch.sections
      .filter((section) => section.kind === "hero" && section.hero_name === "Solomon" && !section.mode_name)
      .flatMap((section) => section.abilities.filter((ability) => ability.name === "Move Piece"));
    const ownedChanges = movePieceOwners.flatMap((ability) => ability.changes);
    const otherChanges = rows
      .filter(({ section, abilityName }) => (
        !(section.kind === "hero" && section.hero_name === "Solomon" && !section.mode_name && abilityName === "Move Piece")
      ))
      .map(({ change }) => change);
    if (
      movePieceOwners.length !== 1
      || ownedChanges.length !== sourceMovePieceRows.length
      || ownedChanges.some((change) => rows.filter((row) => row.change === change).length !== 1)
      || otherChanges.some((change) => /^Move Piece\s*[-—:]/iu.test(change.trim()))
    ) {
      issues.add("move_piece_owner");
    }
  }

  return [...issues].sort();
}

const steamPatchImagePattern = /^https:\/\/clan\.fastly\.steamstatic\.com\/images\/\d+\/[a-f0-9]{32,64}\.(?:avif|gif|jpe?g|png|webp)$/iu;

/** Assert patch rows preserve source order, owner scope and explicit group ranges. */
export async function expectRenderedPatchChangeList(
  list: Locator,
  changes: string[],
  groups: PlatformPatchChangeGroup[] = [],
  headings: number[] = [],
  lists: PlatformPatchChangeList[] = [],
  headingLevel: 3 | 4 = 3,
): Promise<void> {
  const rows = list.locator(":scope > *");
  const orderedHeadings = groups.map((group) => group.heading_index);
  expect(orderedHeadings).toEqual([...orderedHeadings].sort((left, right) => left - right));
  expect(headings).toEqual([...headings].sort((left, right) => left - right));

  let sourceIndex = 0;
  let rowIndex = 0;
  let groupIndex = 0;
  while (sourceIndex < changes.length) {
    const group = groups[groupIndex];
    if (group && group.heading_index === sourceIndex) {
      expect(Number.isInteger(group.heading_index)).toBe(true);
      expect(Number.isInteger(group.description_start)).toBe(true);
      expect(Number.isInteger(group.description_end)).toBe(true);
      expect(group.description_start).toBe(group.heading_index + 1);
      expect(group.description_end).toBeGreaterThan(group.description_start);
      expect(group.description_end).toBeLessThanOrEqual(changes.length);
      const renderedGroup = rows.nth(rowIndex);
      await expect(renderedGroup).toHaveClass(/(?:^|\s)patch-change-group(?:\s|$)/u);
      await expect(renderedGroup.locator(`:scope > h${headingLevel}`)).toHaveText(changes[sourceIndex]);

      const descriptions = changes.slice(group.description_start, group.description_end);
      const descriptionRows = renderedGroup.locator(":scope > .patch-change-group-list > li");
      await expect(descriptionRows).toHaveCount(descriptions.length);
      for (const [descriptionIndex, description] of descriptions.entries()) {
        await expectRenderedPatchChangeRow(descriptionRows.nth(descriptionIndex), description);
      }
      await expect(renderedGroup.locator(".patch-change-group-list .patch-change-group")).toHaveCount(0);
      sourceIndex = group.description_end;
      groupIndex += 1;
    } else if (headings.includes(sourceIndex)) {
      await expect(rows.nth(rowIndex)).toHaveClass(/(?:^|\s)patch-change-heading(?:\s|$)/u);
      await expect(rows.nth(rowIndex).evaluate((node) => node.tagName.toLowerCase()))
        .resolves.toBe(`h${headingLevel}`);
      await expect(rows.nth(rowIndex)).toHaveText(changes[sourceIndex]);
      sourceIndex += 1;
    } else if (lists.some((item) => item.start === sourceIndex)) {
      const descriptor = lists.find((item) => item.start === sourceIndex)!;
      expect(descriptor.end).toBeGreaterThan(descriptor.start);
      expect(descriptor.end).toBeLessThanOrEqual(changes.length);
      const renderedList = rows.nth(rowIndex);
      await expect(renderedList.evaluate((node) => node.tagName.toLowerCase()))
        .resolves.toBe(descriptor.ordered ? "ol" : "ul");
      const itemRows = renderedList.locator(":scope > li");
      const itemChanges = changes.slice(descriptor.start, descriptor.end);
      await expect(itemRows).toHaveCount(itemChanges.length);
      for (const [itemIndex, itemChange] of itemChanges.entries()) {
        await expectRenderedPatchChangeRow(itemRows.nth(itemIndex), itemChange);
      }
      sourceIndex = descriptor.end;
    } else {
      if (group) expect(group.heading_index).toBeGreaterThan(sourceIndex);
      if (steamPatchImagePattern.test(changes[sourceIndex])) {
        await expect(rows.nth(rowIndex)).toHaveClass(/(?:^|\s)patch-inline-image(?:\s|$)/u);
        await expect(rows.nth(rowIndex).locator(":scope > img")).toHaveAttribute("src", changes[sourceIndex]);
      } else {
        await expect(rows.nth(rowIndex)).toHaveClass(/(?:^|\s)patch-change-paragraph(?:\s|$)/u);
        await expect(rows.nth(rowIndex)).toHaveText(changes[sourceIndex]);
      }
      sourceIndex += 1;
    }
    rowIndex += 1;
  }

  expect(groupIndex).toBe(groups.length);
  await expect(rows).toHaveCount(rowIndex);
}

async function expectRenderedPatchChangeRow(row: Locator, change: string): Promise<void> {
  if (steamPatchImagePattern.test(change)) {
    await expect(row).toHaveClass(/(?:^|\s)patch-inline-image(?:\s|$)/u);
    await expect(row.locator(":scope > img")).toHaveAttribute("src", change);
    return;
  }
  await expect(row).not.toHaveClass(/patch-inline-image/u);
  await expect(row).toHaveText(change);
}
