import { expect } from "@playwright/test";
import type { Locator } from "@playwright/test";
import type { PlatformPatchChangeGroup } from "../../lib/platform-types";

const steamPatchImagePattern = /^https:\/\/clan\.fastly\.steamstatic\.com\/images\/\d+\/[a-f0-9]{32,64}\.(?:avif|gif|jpe?g|png|webp)$/iu;

export async function expectRenderedPatchChangeList(
  list: Locator,
  changes: string[],
  groups: PlatformPatchChangeGroup[] = [],
): Promise<void> {
  const rows = list.locator(":scope > li");
  const orderedHeadings = groups.map((group) => group.heading_index);
  expect(orderedHeadings).toEqual([...orderedHeadings].sort((left, right) => left - right));

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
      await expect(renderedGroup.locator(":scope > strong")).toHaveText(changes[sourceIndex]);

      const descriptions = changes.slice(group.description_start, group.description_end);
      const descriptionRows = renderedGroup.locator(":scope > .patch-change-group-list > li");
      await expect(descriptionRows).toHaveCount(descriptions.length);
      for (const [descriptionIndex, description] of descriptions.entries()) {
        await expectRenderedPatchChangeRow(descriptionRows.nth(descriptionIndex), description);
      }
      await expect(renderedGroup.locator(".patch-change-group-list .patch-change-group")).toHaveCount(0);
      sourceIndex = group.description_end;
      groupIndex += 1;
    } else {
      if (group) expect(group.heading_index).toBeGreaterThan(sourceIndex);
      await expectRenderedPatchChangeRow(rows.nth(rowIndex), changes[sourceIndex]);
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
