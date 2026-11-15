import { expect, test } from "@playwright/test";
import { collectLiveImageInventory } from "../support/live-image-inventory";

const fixtureSvg = [
  '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">',
  '<rect width="32" height="32" fill="#d69a2d"/>',
  "</svg>",
].join("");

test("WebKit inventories delayed offscreen lazy images after route content is ready", async ({ page }) => {
  await page.route("http://image-inventory.test/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/fixture") {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content" style="padding-top: 1800px"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              const addImages = (names) => {
                const content = document.querySelector("#image-content");
                for (const name of names) {
                  const image = document.createElement("img");
                  image.alt = name;
                  image.loading = "lazy";
                  image.width = 32;
                  image.height = 32;
                  image.src = "/images/" + name + ".svg";
                  content.append(image);
                }
              };
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((names) => {
                  addImages(names);
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                  setTimeout(() => {
                    void fetch("/api/content/late")
                      .then((response) => response.json())
                      .then(addImages);
                  }, 30);
                });
            </script>
          </body></html>`,
      });
      return;
    }
    if (pathname === "/api/content/initial") {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({ contentType: "application/json", body: '["image-one", "image-two"]' });
      return;
    }
    if (pathname === "/api/content/late") {
      await new Promise((resolve) => setTimeout(resolve, 140));
      await route.fulfill({ contentType: "application/json", body: '["image-three"]' });
      return;
    }
    if (/^\/images\/image-(?:one|two|three)\.svg$/u.test(pathname)) {
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    await route.abort();
  });

  await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
  const inventory = await collectLiveImageInventory(page, {
    route: "delayed-image-fixture",
    waitForContent: async () => {
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
    },
  });

  expect(inventory.complete).toBe(true);
  expect(inventory.images.map((image) => image.source)).toEqual([
    "/images/image-one.svg",
    "/images/image-two.svg",
    "/images/image-three.svg",
  ]);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(inventory.scrollAttempts).toBe(3);
});
