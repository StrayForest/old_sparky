import type { Page } from "@playwright/test";

export type LiveImageInventoryRow = {
  alt: string;
  currentSrc: string;
  loaded: boolean;
  source: string;
};

export type LiveImageInventoryResult = {
  images: LiveImageInventoryRow[];
  complete: boolean;
  scrollAttempts: number;
};

type CollectLiveImageInventoryOptions = {
  route: string;
  waitForContent: () => Promise<void>;
  timeoutMs?: number;
};

type ImageInventoryWindow = Window & {
  __oldsparkyLiveImageInventory?: {
    ids: WeakMap<HTMLImageElement, number>;
    scrolledSources: WeakMap<HTMLImageElement, string>;
    nextId: number;
  };
};

export async function collectLiveImageInventory(
  page: Page,
  { route, waitForContent, timeoutMs = 30_000 }: CollectLiveImageInventoryOptions,
): Promise<LiveImageInventoryResult> {
  await waitForContent();
  await page.evaluate(() => {
    const target = window as ImageInventoryWindow;
    target.__oldsparkyLiveImageInventory ??= {
      ids: new WeakMap<HTMLImageElement, number>(),
      scrolledSources: new WeakMap<HTMLImageElement, string>(),
      nextId: 0,
    };
  });

  const deadline = performance.now() + timeoutMs;
  const imageLocator = page.locator("img");
  let previousSignature = "";
  let stablePasses = 0;
  let scrollAttempts = 0;
  let images: LiveImageInventoryRow[] = [];

  while (performance.now() < deadline) {
    const handles = await imageLocator.elementHandles();
    for (const imageHandle of handles) {
      let shouldScroll = false;
      try {
        shouldScroll = await imageHandle.evaluate((element) => {
          const image = element as HTMLImageElement;
          const state = (window as ImageInventoryWindow).__oldsparkyLiveImageInventory;
          if (!state) return false;
          if (!state.ids.has(image)) {
            state.nextId += 1;
            state.ids.set(image, state.nextId);
          }
          const source = `${image.getAttribute("src") ?? ""}\0${image.getAttribute("srcset") ?? ""}`;
          if (state.scrolledSources.get(image) === source) return false;
          state.scrolledSources.set(image, source);
          return true;
        });
        if (shouldScroll) {
          await imageHandle.evaluate((element) => {
            (element as HTMLImageElement).scrollIntoView({ block: "center", inline: "nearest" });
          });
          scrollAttempts += 1;
          await page.waitForTimeout(50);
        }
      } catch {
        // A replaced node is rediscovered on the next bounded pass.
      }
    }

    const snapshot = await imageLocator.evaluateAll((elements) => {
      const state = (window as ImageInventoryWindow).__oldsparkyLiveImageInventory;
      return elements.map((element) => {
        const image = element as HTMLImageElement;
        if (state && !state.ids.has(image)) {
          state.nextId += 1;
          state.ids.set(image, state.nextId);
        }
        return {
          id: state?.ids.get(image) ?? 0,
          alt: image.alt,
          currentSrc: image.currentSrc,
          loaded: image.complete && image.naturalWidth > 0,
          source: image.getAttribute("src") ?? "",
          sourceSet: image.getAttribute("srcset") ?? "",
        };
      });
    });
    images = snapshot.map(({ alt, currentSrc, loaded, source }) => ({
      alt,
      currentSrc,
      loaded,
      source,
    }));
    const signature = snapshot.map((image) => (
      `${image.id}\0${image.source}\0${image.sourceSet}\0${image.currentSrc}\0${image.loaded}`
    )).join("\n");
    const allLoaded = images.length > 0 && images.every((image) => (
      Boolean(image.currentSrc) && image.loaded
    ));
    if (allLoaded && signature === previousSignature) {
      stablePasses += 1;
      if (stablePasses >= 2) return { images, complete: true, scrollAttempts };
    } else {
      stablePasses = 0;
    }
    previousSignature = signature;
    await page.waitForTimeout(50);
  }

  return { images, complete: false, scrollAttempts };
}
