import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import type { Locator } from "@playwright/test";
import type { PlatformPatchAbilityStat, PlatformPatchDetail, PlatformPatchSection } from "../../lib/platform-types";
import { patchAcceptanceIssues } from "../support/patch-acceptance";

const patchIds = [
  "1848664643342973",
  "1848664643339845",
  "1847920302240420",
  "1846651706696737",
] as const;
const nursePatchId = "1847920302240420";
const nurseIntroductionPatchId = "1846651706696737";
const sourceOwnerPrefixPattern = /^(?:Nurse|Медсестра|Mid Boss|Средний босс|Items|Heroes|Предметы|Герои)\s*:/iu;
const rawPatchMarkupPattern = /STEAM_CLAN_LOC_IMAGE|clan\.fastly\.steamstatic\.com|\[\s*\/?\s*img\b|\[\s*url\b|\[\s*\/\s*\*\s*\]/iu;
const separatorOnlyChangePattern = /^-{2,}$/u;
const patchFixtures = Promise.all(patchIds.map(async (id) => (
  JSON.parse(await readFile(path.join(process.cwd(), "tests", "fixtures", "patch-details", `${id}.json`), "utf8")) as PlatformPatchDetail
)));

function sectionHeading(section: PlatformPatchSection): string {
  if (section.objective_key === "urn") return "Урна";
  if (section.objective_key === "unstable_rift") return "Нестабильный разлом";
  if (section.objective_key === "mid_boss") return "Mid Boss";
  return section.title;
}

async function expectChangeRows(rows: Locator, changes: string[]) {
  await expect(rows).toHaveCount(changes.length);
  for (const [index, change] of changes.entries()) {
    expect(sourceOwnerPrefixPattern.test(change.trim())).toBe(false);
    expect(rawPatchMarkupPattern.test(change)).toBe(false);
    expect(separatorOnlyChangePattern.test(change.trim())).toBe(false);
    const row = rows.nth(index);
    await expect(row).toHaveText(change);
  }
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  const overflow = await page.evaluate(() => (
    Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
  ));
  expect(overflow).toBeLessThanOrEqual(2);
}

async function savePatchCardScreenshot(
  locator: Locator,
  filename: string,
  artifactDirectory: string | undefined,
) {
  if (!artifactDirectory) return;
  await mkdir(artifactDirectory, { recursive: true, mode: 0o700 });
  await locator.screenshot({ path: path.join(artifactDirectory, filename) });
}

async function expectSemanticStatRows(rows: Locator, stats: PlatformPatchAbilityStat[]) {
  await expect(rows.locator(":scope > .patch-ability-stat")).toHaveCount(stats.length);
  for (const [index, stat] of stats.entries()) {
    const row = rows.locator(":scope > .patch-ability-stat").nth(index);
    await expect(row).toHaveAttribute("data-stat-type", stat.type ?? "unknown");
    await expect(row).toHaveAttribute("data-stat-unit", stat.unit ?? "unknown");
    await expect(row).toHaveAttribute("data-stat-tier", String(stat.tier ?? "base"));
    await expect(row).toHaveAttribute("data-has-scaling", stat.scaling ? "true" : "false");
    if (stat.identity) await expect(row).toHaveAttribute("data-stat-identity", stat.identity);
    await expect(row.locator("dt > span")).toHaveText(stat.label);
    await expect(row.locator("dd")).toHaveText(stat.value);
    await expect(row.locator("dt svg")).toHaveCount(1);
  }
}

async function expectStatLabelsKeepWords(locator: Locator) {
  const splitWords = await locator.locator(".patch-ability-stat dt span").evaluateAll((labels) => (
    labels.flatMap((label) => {
      const textNode = label.firstChild;
      if (!textNode || textNode.nodeType !== Node.TEXT_NODE) return [];
      const text = textNode.textContent ?? "";
      const broken: string[] = [];
      for (const match of text.matchAll(/\S+/gu)) {
        const word = match[0];
        const start = match.index;
        if (start === undefined) continue;
        const range = document.createRange();
        range.setStart(textNode, start);
        range.setEnd(textNode, start + word.length);
        if (range.getClientRects().length > 1) broken.push(word);
      }
      return broken;
    })
  ));
  expect(splitWords).toEqual([]);
}

test("four parsed patch DTOs render correct hero and ability ownership at mobile and desktop widths", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "This renderer replay checks both widths in one Chromium run.");
  test.setTimeout(120_000);
  expect(["STEAM_CLAN_LOC_IMAGE", "[img]", "[img src=\" \"][/img]", "[url=\"\"]label[/url]"]
    .every((value) => rawPatchMarkupPattern.test(value))).toBe(true);
  // These DTO fixtures pin source parsing and structure before translation; the
  // helper cases above and the live suite own translated-content acceptance.
  const fixtures = await patchFixtures;
  const acceptanceTemplate = fixtures[0];
  expect(acceptanceTemplate).toBeDefined();
  const acceptanceDetail = (
    content: string,
    changes: string[],
    sectionOverrides: Partial<PlatformPatchSection> = {},
  ): PlatformPatchDetail => ({
    ...acceptanceTemplate!,
    content,
    sections: [{
      kind: "general",
      title: "General",
      hero_name: null,
      changes,
      abilities: [],
      ...sectionOverrides,
    }],
  });
  const cleanFraction = acceptanceDetail(
    "[General]\nonly 50% as effective\n[Items]",
    ["На 50% от прежней эффективности"],
  );
  expect(patchAcceptanceIssues(cleanFraction)).toEqual([]);
  const cleanCanonicalAndNumeric = acceptanceDetail(
    "[Heroes]\nSolomon: Move Piece - Range increased\nSolomon: Move Piece - Placement improved\n[General]",
    ["Move Piece", "50% → 40%"],
    {
      kind: "hero",
      title: "Solomon",
      hero_name: "Solomon",
      abilities: [{ name: "Move Piece", icon_url: null, changes: ["Move Piece", "50% → 40%"] }],
      changes: [],
    },
  );
  expect(patchAcceptanceIssues(cleanCanonicalAndNumeric)).toEqual([]);
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nCooldown reduced by 2 seconds\n[Items]",
    ["Перезарядка reduced by 2 seconds"],
  ))).toContain("untranslated_change");
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nDamage increased by 10%.\n[Items]",
    ["Damage increased by 10%."],
  ))).toContain("untranslated_change");
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nonly 50% as effective\n[Items]",
    ["На 50% эффективнее"],
  ))).toContain("effectiveness_comparison");
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nDamage increased\n[Items]",
    ["Урон увеличен на 50%%"],
  ))).toContain("duplicated_percent");
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nMovement increased from 50% to 40%\n[Items]\nItem: Dash speed increased from 50% m/s to 40% m/s",
    ["Скорость снижена с 50% м/с до 40% м/с"],
  ))).toContain("invented_percent_speed_unit");
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\nDash speed reduced from 50 m/s to 40 m/s\n[Items]",
    ["Скорость снижена с 50 м/с до 40 м/с"],
  ))).toEqual([]);
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[General]\n[img src=\"https://example.invalid/icon.png\"][/img]\n[Items]",
    ["[img src=\" \"][/img]", "General"],
  ))).toEqual(expect.arrayContaining(["steam_markup", "detached_owner_or_header"]));
  expect(patchAcceptanceIssues(acceptanceDetail(
    "[Heroes]\nStreet Brawl:\nSolomon: Move Piece - Range increased\n[General]",
    ["Range increased"],
  ))).toEqual(expect.arrayContaining(["missing_street_brawl_owner", "move_piece_owner"]));
  const fixturesById = new Map(fixtures.map((fixture) => [fixture.id, fixture]));
  const artifactDirectory = process.env.PLATFORM_PATCH_RENDER_ARTIFACT_DIR;
  const alternateNurseImage = "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/nurse-card-alternate.png";
  const missingNurseImage = "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/nurse-card-missing.webp";
  const alternateImageBytes = await readFile(path.join(process.cwd(), "public", "assets", "heroes", "Abrams.png"));
  let missingNurseImageRequests = 0;
  let alternateNurseImageRequests = 0;
  let focusHealingNeedlesScreenshot = false;

  await page.route("**/api/v1/content/patches/*", async (route) => {
    const requestId = new URL(route.request().url()).pathname.split("/").at(-1);
    const fixture = requestId ? fixturesById.get(requestId) : undefined;
    if (!fixture) {
      await route.continue();
      return;
    }
    const renderedFixture = requestId === nurseIntroductionPatchId && focusHealingNeedlesScreenshot
      ? {
        ...fixture,
        sections: fixture.sections
          .filter((section) => section.kind === "hero" && section.hero_name === "Nurse Harrow")
          .map((section) => ({
            ...section,
            abilities: section.abilities.filter((ability) => ability.name === "Healing Needles"),
          })),
      }
      : requestId === nurseIntroductionPatchId
      ? {
        ...fixture,
        sections: fixture.sections.map((section) => (
          section.kind === "hero" && section.hero_name === "Nurse Harrow"
            ? {
              ...section,
              hero_image_url: missingNurseImage,
              hero_image_candidates: [missingNurseImage, alternateNurseImage],
            }
            : section
        )),
      }
      : fixture;
    await route.fulfill({ status: 200, contentType: "application/json", json: renderedFixture });
  });
  await page.route(missingNurseImage, async (route) => {
    missingNurseImageRequests += 1;
    await route.fulfill({ status: 404, contentType: "text/plain", body: "missing primary image" });
  });
  await page.route(alternateNurseImage, async (route) => {
    alternateNurseImageRequests += 1;
    await route.fulfill({ status: 200, contentType: "image/png", body: alternateImageBytes });
  });

  let capturedCeleste = false;
  let capturedNurse = false;
  let capturedMidBoss = false;
  for (const patchId of patchIds) {
    const fixture = fixturesById.get(patchId);
    expect(fixture).toBeDefined();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/patches/${patchId}`);
    await expect(page.locator(".patch-article")).toBeVisible();
    const sections = page.locator(".patch-section");
    await expect(sections).toHaveCount(fixture!.sections.length);

    for (const [sectionIndex, section] of fixture!.sections.entries()) {
      const sectionCard = sections.nth(sectionIndex);
      await expect(sectionCard.getByRole("heading", { level: 2 })).toHaveText(sectionHeading(section));
      if (section.mode_name) {
        await expect(sectionCard.locator(".patch-mode-context")).toHaveText(section.mode_name);
      } else {
        await expect(sectionCard.locator(".patch-mode-context")).toHaveCount(0);
      }
      if (section.kind === "hero" && section.hero_presentation_kind !== "introduction") {
        await expect(sectionCard.locator(".patch-hero-description")).toHaveCount(0);
      }
      if (section.kind === "hero" && section.hero_display_name) {
        await expect(sectionCard.getByText(section.hero_display_name, { exact: true })).toHaveCount(0);
      }
      await expectChangeRows(
        sectionCard.locator(":scope > .patch-change-list > li"),
        section.changes,
      );

      const abilityCards = sectionCard.locator(".patch-ability");
      await expect(abilityCards).toHaveCount(section.abilities.length);
      for (const [abilityIndex, ability] of section.abilities.entries()) {
        const abilityCard = abilityCards.nth(abilityIndex);
        await expect(abilityCard.getByRole("heading", { level: 3 }))
          .toHaveText(ability.display_name || ability.name);
        const realIconCount = await abilityCard.locator(".patch-ability-icon").count();
        const fallbackCount = await abilityCard.locator(".patch-ability-icon-fallback").count();
        expect(realIconCount + fallbackCount).toBe(1);
        if (ability.icon_url && realIconCount === 1) {
          await expect(abilityCard.locator(".patch-ability-icon")).toHaveAttribute("src", ability.icon_url);
        }
        await expectChangeRows(
          abilityCard.locator(":scope > .patch-change-list > li"),
          ability.changes,
        );
        if (ability.presentation_kind !== "introduction" && ability.presentation_kind !== "replacement") {
          expect(ability.description ?? "").toBe("");
          expect(ability.stats ?? []).toHaveLength(0);
          expect(ability.stats_pending ?? false).toBe(false);
          await expect(abilityCard.locator(".patch-ability-description, .patch-ability-stats"))
            .toHaveCount(0);
        }
      }
    }

    if (patchId === nursePatchId) {
      const modeSections = fixture!.sections.filter((section) => section.mode_name === "Street Brawl");
      expect(modeSections.map((section) => section.hero_name)).toEqual([
        "Deadman Danny",
        "Nurse Harrow",
        "Paige",
        "Rat King",
        "Violet",
      ]);
      const modeRows = modeSections.flatMap((section) => [
        ...section.changes,
        ...section.abilities.flatMap((ability) => ability.changes),
      ]);
      expect(modeRows).toHaveLength(13);
      expect(new Set(modeRows).size).toBe(13);
      expect(modeRows.filter((change) => change.includes("Street Brawl:"))).toHaveLength(0);
      const yamato = fixture!.sections.find((section) => section.hero_name === "Yamato");
      expect(yamato?.mode_name).toBeFalsy();
      expect(yamato?.abilities.flatMap((ability) => ability.changes)
        .some((change) => change.includes("Street Brawl:"))).toBe(false);
      expect(fixture!.sections.some((section) => section.changes.includes("Street Brawl:"))).toBe(false);

      const solomonIndex = fixture!.sections.findIndex((section) => section.hero_name === "Solomon");
      expect(solomonIndex).toBeGreaterThanOrEqual(0);
      const solomon = fixture!.sections[solomonIndex];
      const movePieceIndex = solomon.abilities.findIndex((ability) => ability.name === "Move Piece");
      expect(movePieceIndex).toBeGreaterThanOrEqual(0);
      const movePiece = solomon.abilities[movePieceIndex];
      expect(movePiece).toBeDefined();
      expect(movePiece?.icon_url).toBe(
        "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/chess/chessmaster_movepiece.png",
      );
      expect(movePiece?.changes).toHaveLength(2);
      const movePieceCard = sections.nth(solomonIndex).locator(".patch-ability").nth(movePieceIndex);
      await movePieceCard.locator(".patch-ability-icon").scrollIntoViewIfNeeded();
      await expect.poll(() => movePieceCard.locator(".patch-ability-icon").evaluate((image) => (
        (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
    }

    const celesteIndex = fixture!.sections.findIndex((section) => (
      section.kind === "hero"
      && `${section.hero_name ?? ""} ${section.title}`.toLowerCase().includes("celeste")
    ));
    if (celesteIndex >= 0) {
      const celeste = sections.nth(celesteIndex);
      await expect(celeste.getByRole("heading", { level: 2 })).toHaveText("Celeste");
      await expect(celeste.getByText("СЕЛЕСТА", { exact: true })).toHaveCount(0);
      await celeste.locator(".patch-ability").first().scrollIntoViewIfNeeded();
      await expect.poll(() => celeste.locator(".patch-ability-icon").first().evaluate((image) => (
        (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
      await savePatchCardScreenshot(
        celeste.locator(".patch-ability").first(),
        "patch-render-celeste-ability-mobile.png",
        artifactDirectory,
      );
      capturedCeleste = true;
    }

    const nurseIndex = fixture!.sections.findIndex((section) => (
      section.kind === "hero"
      && !section.mode_name
      && `${section.hero_name ?? ""} ${section.title}`.toLowerCase().includes("nurse")
    ));
    if (nurseIndex >= 0) {
      const nurse = fixture!.sections[nurseIndex];
      const nurseCard = sections.nth(nurseIndex);
      const generalRows = page.locator(".general-patch-section > .patch-change-list > li");
      await expect(generalRows.getByText(/(?:Nurse|Медсестра)\s*:/iu)).toHaveCount(0);
      const abilityNames = nurse.abilities.map((ability) => ability.display_name || ability.name);
      if (patchId === nursePatchId) {
        expect(nurse.abilities).toHaveLength(4);
        expect(nurse.hero_presentation_kind).toBe("balance");
        expect(nurse.hero_image_url).toBeTruthy();
        await nurseCard.locator(".patch-hero-image").scrollIntoViewIfNeeded();
        await expect.poll(() => nurseCard.locator(".patch-hero-image").evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        expect(nurse.abilities.every((ability) => ability.presentation_kind === "balance")).toBe(true);
        expect(nurse.abilities.map((ability) => ability.changes.length)).toEqual([3, 1, 3, 3]);
        expect(nurse.abilities.every((ability) => !ability.description && !(ability.stats?.length))).toBe(true);
        expect(abilityNames).toEqual(expect.arrayContaining([
          expect.stringMatching(/Wretched Vapors|Гнусные пары/i),
          expect.stringMatching(/Healing Needles|Лечебные иглы/i),
        ]));
        const modeNurse = fixture!.sections.find((section) => (
          section.hero_name === "Nurse Harrow" && section.mode_name === "Street Brawl"
        ));
        expect(modeNurse?.abilities.map((ability) => [ability.name, ability.changes.length]))
          .toEqual([["Leech Therapy", 1], ["Lurking Terrors", 2]]);
        await nurseCard.locator(".patch-ability-icon").first().scrollIntoViewIfNeeded();
        await expect.poll(() => nurseCard.locator(".patch-ability-icon").first().evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        await savePatchCardScreenshot(nurseCard, "patch-render-nurse-card-mobile.png", artifactDirectory);
        capturedNurse = true;
      } else if (patchId === nurseIntroductionPatchId) {
        expect(nurse.abilities).toHaveLength(4);
        expect(nurse.hero_presentation_kind).toBe("introduction");
        expect(nurse.hero_image_url).toBeTruthy();
        await nurseCard.locator(".patch-hero-image").scrollIntoViewIfNeeded();
        await expect.poll(() => nurseCard.locator(".patch-hero-image").evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        await expect(nurseCard.locator(".patch-hero-image")).toHaveAttribute("src", alternateNurseImage);
        expect(missingNurseImageRequests).toBe(1);
        expect(alternateNurseImageRequests).toBe(1);
        expect(nurse.abilities.every((ability) => ability.presentation_kind === "introduction")).toBe(true);
        expect(nurse.abilities.every((ability) => Boolean(ability.description?.trim()))).toBe(true);
        expect(nurse.abilities.some((ability) => ability.stats_pending)).toBe(false);
        for (const [abilityIndex, sourceAbility] of nurse.abilities.entries()) {
          const statRows = sourceAbility.stats ?? [];
          expect(statRows.every((stat) => (
            Boolean(stat.identity)
            && Boolean(stat.type)
            && Boolean(stat.unit)
            && stat.tier !== undefined
          ))).toBe(true);
          const abilityCard = nurseCard.locator(".patch-ability").nth(abilityIndex);
          const sourceBaseStats = statRows.filter((stat) => stat.tier == null);
          await expectSemanticStatRows(
            abilityCard.locator('dl[data-stat-layout="base"]'),
            sourceBaseStats,
          );
          const sourceTiers = new Map<number, PlatformPatchAbilityStat[]>();
          for (const stat of statRows) {
            if (stat.tier == null) continue;
            const tierStats = sourceTiers.get(stat.tier) ?? [];
            tierStats.push(stat);
            sourceTiers.set(stat.tier, tierStats);
          }
          const renderedTiers = abilityCard.locator(".patch-ability-tier");
          await expect(renderedTiers).toHaveCount(sourceTiers.size);
          for (const [tier, tierStats] of sourceTiers) {
            const sourceTierCard = abilityCard.locator(`.patch-ability-tier[data-tier="${tier}"]`);
            await expect(sourceTierCard.locator("h5")).toHaveText(`T${tier}`);
            await expectSemanticStatRows(sourceTierCard.locator('dl[data-stat-layout="tier"]'), tierStats);
          }
          const abilityIcon = abilityCard.locator(".patch-ability-icon");
          await abilityIcon.scrollIntoViewIfNeeded();
          await expect.poll(() => abilityIcon.evaluate((image) => (
            (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
          ))).toBe(true);
        }
        const needleIndex = nurse.abilities.findIndex((ability) => ability.name === "Healing Needles");
        expect(needleIndex).toBeGreaterThanOrEqual(0);
        const needle = nurse.abilities[needleIndex];
        const needleStats = needle?.stats ?? [];
        const baseHealing = needleStats.find((stat) => stat.identity === "heal_regen" && stat.tier == null);
        expect(baseHealing?.scaling?.coefficient).toBe("0.24");
        expect(baseHealing?.value).toMatch(/22.*0,24.*Спиритическая мощь/u);
        expect(needleStats.some((stat) => /-1(?:[,.]0)?\s*(?:с\.|сек\.?)/iu.test(stat.value))).toBe(false);
        expect(needleStats.filter((stat) => stat.tier === 1)).toHaveLength(1);
        expect(needleStats.filter((stat) => stat.tier === 2)).toHaveLength(2);
        expect(needleStats.filter((stat) => stat.tier === 3)).toHaveLength(2);
        const tierThreeScale = needleStats.find((stat) => stat.tier === 3 && stat.unit === "coefficient");
        expect(tierThreeScale?.identity).toBe("heal_regen");
        expect(tierThreeScale?.upgrade_type).toBe("add_to_scale");
        expect(tierThreeScale?.value).toBe("+0,4");
        const needleCard = nurseCard.locator(".patch-ability").nth(needleIndex);
        await expect(needleCard.getByRole("heading", { level: 3 })).toHaveText("Лечебные иглы");
        await savePatchCardScreenshot(needleCard, "patch-render-healing-needles-mobile.png", artifactDirectory);
        await savePatchCardScreenshot(nurseCard, "patch-render-nurse-intro-mobile.png", artifactDirectory);
      }
    }

    const midBossIndex = fixture!.sections.findIndex((section) => section.objective_key === "mid_boss");
    if (midBossIndex >= 0) {
      const midBoss = sections.nth(midBossIndex);
      await expect(midBoss.getByRole("heading", { level: 2 })).toHaveText("Mid Boss");
      const midBossIcon = midBoss.locator(".patch-objective-image");
      await midBossIcon.scrollIntoViewIfNeeded();
      await expect.poll(() => midBossIcon.evaluate((image) => (
        (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
      await savePatchCardScreenshot(midBoss, "patch-render-mid-boss-mobile.png", artifactDirectory);
      capturedMidBoss = true;
    }

    await expectNoHorizontalOverflow(page);
    const mobileMain = await page.locator("main.patch-page-main").boundingBox();
    const mobileArticle = await page.locator(".patch-article").boundingBox();
    expect(mobileMain).not.toBeNull();
    expect(mobileArticle).not.toBeNull();
    expect(mobileArticle!.x).toBeGreaterThanOrEqual(mobileMain!.x - 1);
    expect(mobileArticle!.x + mobileArticle!.width).toBeLessThanOrEqual(mobileMain!.x + mobileMain!.width + 1);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await expectNoHorizontalOverflow(page);
    const desktopMain = await page.locator("main.patch-page-main").boundingBox();
    const desktopArticle = await page.locator(".patch-article").boundingBox();
    expect(desktopMain).not.toBeNull();
    expect(desktopArticle).not.toBeNull();
    expect(desktopArticle!.x).toBeGreaterThanOrEqual(desktopMain!.x - 1);
    expect(desktopArticle!.x + desktopArticle!.width).toBeLessThanOrEqual(desktopMain!.x + desktopMain!.width + 1);
  }

  if (artifactDirectory) {
    focusHealingNeedlesScreenshot = true;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/patches/${nurseIntroductionPatchId}`);
    const focusedNurse = page.locator(".patch-section").first();
    const focusedAbility = focusedNurse.locator(".patch-ability").first();
    await expect(focusedNurse.getByRole("heading", { level: 2 })).toHaveText("Nurse Harrow");
    await expect(focusedAbility.getByRole("heading", { level: 3 })).toHaveText("Лечебные иглы");
    for (const image of [focusedNurse.locator(".patch-hero-image"), focusedAbility.locator(".patch-ability-icon")]) {
      await image.scrollIntoViewIfNeeded();
      await expect.poll(() => image.evaluate((element) => (
        (element as HTMLImageElement).complete && (element as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
    }
    await expectNoHorizontalOverflow(page);
    const mobileHeader = await focusedNurse.locator(".patch-section-header").boundingBox();
    const mobileBiography = await focusedNurse.locator(".patch-hero-description").boundingBox();
    expect(mobileHeader).not.toBeNull();
    expect(mobileBiography).not.toBeNull();
    expect(mobileBiography!.width).toBeGreaterThan(mobileHeader!.width * 0.9);
    const mobileColumns = await focusedAbility.locator('dl[data-stat-layout="base"]').evaluate((element) => (
      getComputedStyle(element).gridTemplateColumns.split(" ").length
    ));
    const mobileTierColumns = await focusedAbility.locator(".patch-ability-tier-rail").evaluate((element) => (
      getComputedStyle(element).gridTemplateColumns.split(" ").length
    ));
    expect(mobileColumns).toBe(1);
    expect(mobileTierColumns).toBe(1);
    const longTierLabel = focusedAbility.locator('.patch-ability-tier[data-tier="3"] .patch-ability-stat').last().locator("dt");
    const longTierRowLocator = longTierLabel.locator("xpath=..");
    const longTierRow = await longTierRowLocator.boundingBox();
    const longTierLabelBox = await longTierLabel.boundingBox();
    const longTierValue = await longTierRowLocator.locator("dd").boundingBox();
    expect(longTierRow).not.toBeNull();
    expect(longTierLabelBox).not.toBeNull();
    expect(longTierValue).not.toBeNull();
    expect(longTierLabelBox!.width).toBeGreaterThan(longTierRow!.width * 0.9);
    expect(longTierValue!.y).toBeGreaterThanOrEqual(longTierLabelBox!.y + longTierLabelBox!.height - 1);
    await expectStatLabelsKeepWords(focusedAbility);
    await savePatchCardScreenshot(focusedNurse, "healing-needles-mobile.png", artifactDirectory);

    await page.setViewportSize({ width: 320, height: 800 });
    await expectNoHorizontalOverflow(page);
    await expectStatLabelsKeepWords(focusedAbility);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await expectNoHorizontalOverflow(page);
    const desktopColumns = await focusedAbility.locator('dl[data-stat-layout="base"]').evaluate((element) => (
      getComputedStyle(element).gridTemplateColumns.split(" ").length
    ));
    const desktopTierColumns = await focusedAbility.locator(".patch-ability-tier-rail").evaluate((element) => (
      getComputedStyle(element).gridTemplateColumns.split(" ").length
    ));
    expect(desktopColumns).toBe(4);
    expect(desktopTierColumns).toBe(3);
    await savePatchCardScreenshot(focusedNurse, "healing-needles-desktop.png", artifactDirectory);
  }

  expect(capturedCeleste).toBe(true);
  expect(capturedNurse).toBe(true);
  expect(capturedMidBoss).toBe(true);
});
