import { expect, test } from "@playwright/test";

test("public discovery documents expose the canonical origin without publishing support email", async ({ request }) => {
  const [robots, sitemap, manifest, security, privacy, terms] = await Promise.all([
    request.get("/robots.txt"),
    request.get("/sitemap.xml"),
    request.get("/manifest.webmanifest"),
    request.get("/.well-known/security.txt"),
    request.get("/privacy"),
    request.get("/terms")
  ]);

  expect(robots.ok()).toBe(true);
  const robotsBody = await robots.text();
  expect(robotsBody).toContain("User-Agent: *");
  expect(robotsBody).toContain("Allow: /");
  expect(robotsBody).toContain("Sitemap: https://old-sparky.com/sitemap.xml");
  expect(robotsBody).not.toMatch(/(?:admin|auth|profile|api|reset-password)/iu);
  expect(sitemap.ok()).toBe(true);
  const sitemapBody = await sitemap.text();
  expect(sitemapBody).toContain("https://old-sparky.com/privacy");
  expect(sitemapBody).toContain("https://old-sparky.com/terms");
  const sitemapUrls = [...sitemapBody.matchAll(/<loc>([^<]+)<\/loc>/gu)].map((match) => match[1]);
  expect(new Set(sitemapUrls).size).toBe(sitemapUrls.length);
  expect(sitemapUrls.filter((url) => url.includes("/patches/")).length).toBe(4);
  expect(sitemapBody).toContain("https://old-sparky.com/patches/1836506165584438");
  expect(manifest.ok()).toBe(true);
  expect((await manifest.json()).name).toBe("Old Sparky Arena");
  expect(security.ok()).toBe(true);
  const publicContactDocuments = [await security.text(), await privacy.text(), await terms.text()];
  expect(publicContactDocuments[0]).toContain("Contact: https://old-sparky.com/info#support");
  expect(publicContactDocuments.every((document) => !document.includes("support@old-sparky.com"))).toBe(true);
  expect(publicContactDocuments.every((document) => !document.includes("mailto:"))).toBe(true);
});
