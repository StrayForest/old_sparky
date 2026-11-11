import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import type { Locator } from "@playwright/test";
import type { PlatformPatchDetail, PlatformPatchSection } from "../../lib/platform-types";

const patchIds = [
  "1848664643342973",
  "1848664643339845",
  "1847920302240420",
  "1846651706696737",
] as const;
const nursePatchId = "1847920302240420";
const nurseIntroductionPatchId = "1846651706696737";
const sourceOwnerPrefixPattern = /^(?:Nurse|Медсестра|Mid Boss|Средний босс|Items|Heroes|Предметы|Герои)\s*:/iu;
const rawPatchMarkupPattern = /STEAM_CLAN_LOC_IMAGE|clan\.fastly\.steamstatic\.com|\[\s*\/?\s*img\b|\[\s*url\b/iu;
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

async function saveMobileCardScreenshot(
  locator: Locator,
  filename: string,
  artifactDirectory: string | undefined,
) {
  if (!artifactDirectory) return;
  await mkdir(artifactDirectory, { recursive: true, mode: 0o700 });
  await locator.screenshot({ path: path.join(artifactDirectory, filename) });
}

test("four parsed patch DTOs render correct hero and ability ownership at mobile and desktop widths", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "This renderer replay checks both widths in one Chromium run.");
  test.setTimeout(120_000);
  expect(["STEAM_CLAN_LOC_IMAGE", "[img]", "[img src=\" \"][/img]", "[url=\"\"]label[/url]"]
    .every((value) => rawPatchMarkupPattern.test(value))).toBe(true);
  const fixtures = await patchFixtures;
  const fixturesById = new Map(fixtures.map((fixture) => [fixture.id, fixture]));
  const artifactDirectory = process.env.PLATFORM_PATCH_RENDER_ARTIFACT_DIR;

  await page.route("**/api/v1/content/patches/*", async (route) => {
    const requestId = new URL(route.request().url()).pathname.split("/").at(-1);
    const fixture = requestId ? fixturesById.get(requestId) : undefined;
    if (!fixture) {
      await route.continue();
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", json: fixture });
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
      await expect(sectionCard.locator(".patch-hero-display-name")).toHaveCount(0);
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
      await saveMobileCardScreenshot(
        celeste.locator(".patch-ability").first(),
        "patch-render-celeste-ability-mobile.png",
        artifactDirectory,
      );
      capturedCeleste = true;
    }

    const nurseIndex = fixture!.sections.findIndex((section) => (
      section.kind === "hero"
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
        expect(nurse.abilities.map((ability) => ability.changes.length)).toEqual([4, 1, 3, 5]);
        expect(nurse.abilities.every((ability) => !ability.description && !(ability.stats?.length))).toBe(true);
        expect(abilityNames).toEqual(expect.arrayContaining([
          expect.stringMatching(/Wretched Vapors|Гнусные пары/i),
          expect.stringMatching(/Healing Needles|Лечебные иглы/i),
        ]));
        await nurseCard.locator(".patch-ability-icon").first().scrollIntoViewIfNeeded();
        await expect.poll(() => nurseCard.locator(".patch-ability-icon").first().evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        await saveMobileCardScreenshot(nurseCard, "patch-render-nurse-card-mobile.png", artifactDirectory);
        capturedNurse = true;
      } else if (patchId === nurseIntroductionPatchId) {
        expect(nurse.abilities).toHaveLength(4);
        expect(nurse.hero_presentation_kind).toBe("introduction");
        expect(nurse.hero_image_url).toBeTruthy();
        await nurseCard.locator(".patch-hero-image").scrollIntoViewIfNeeded();
        await expect.poll(() => nurseCard.locator(".patch-hero-image").evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        expect(nurse.abilities.every((ability) => ability.presentation_kind === "introduction")).toBe(true);
        expect(nurse.abilities.every((ability) => Boolean(ability.description?.trim()))).toBe(true);
        expect(nurse.abilities.reduce((sum, ability) => sum + (ability.stats?.length ?? 0), 0)).toBe(67);
        expect(nurse.abilities.some((ability) => ability.stats_pending)).toBe(false);
        await saveMobileCardScreenshot(nurseCard, "patch-render-nurse-intro-mobile.png", artifactDirectory);
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
      await saveMobileCardScreenshot(midBoss, "patch-render-mid-boss-mobile.png", artifactDirectory);
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

  expect(capturedCeleste).toBe(true);
  expect(capturedNurse).toBe(true);
  expect(capturedMidBoss).toBe(true);
});
