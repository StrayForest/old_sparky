import type { MetadataRoute } from "next";

const SITE_ORIGIN = "https://old-sparky.com";
const MAX_PATCHES = 4;
const configuredServerApiBaseUrl = (
  process.env.PLATFORM_API_BASE_URL
  ?? `${process.env.PLATFORM_API_INTERNAL_ORIGIN ?? "http://127.0.0.1:8010"}/api/v1`
).replace(/\/$/u, "");

export const dynamic = "force-dynamic";

type PatchSitemapEntry = {
  id: string;
  published_at: string;
};

function trustedServerApiBaseUrl(): string | null {
  try {
    const url = new URL(configuredServerApiBaseUrl);
    if (
      !["http:", "https:"].includes(url.protocol)
      || !["127.0.0.1", "::1", "localhost"].includes(url.hostname.toLowerCase())
      || url.username
      || url.password
      || !url.pathname.endsWith("/api/v1")
    ) {
      return null;
    }
    return url.toString().replace(/\/$/u, "");
  } catch {
    return null;
  }
}

function validPatchEntries(payload: unknown): PatchSitemapEntry[] {
  if (!payload || typeof payload !== "object" || !("patches" in payload)) {
    return [];
  }
  const patches = (payload as { patches?: unknown }).patches;
  if (!Array.isArray(patches)) {
    return [];
  }
  const entries: PatchSitemapEntry[] = [];
  const seen = new Set<string>();
  for (const candidate of patches) {
    if (entries.length >= MAX_PATCHES || !candidate || typeof candidate !== "object") {
      break;
    }
    const entry = candidate as { id?: unknown; published_at?: unknown };
    const id = typeof entry.id === "string" ? entry.id.trim() : "";
    const publishedAt = typeof entry.published_at === "string" ? entry.published_at : "";
    if (!/^\d{1,32}$/u.test(id) || seen.has(id) || Number.isNaN(Date.parse(publishedAt))) {
      continue;
    }
    seen.add(id);
    entries.push({ id, published_at: publishedAt });
  }
  return entries;
}

async function getPatchSitemapEntries(): Promise<PatchSitemapEntry[]> {
  const serverApiBaseUrl = trustedServerApiBaseUrl();
  if (!serverApiBaseUrl) {
    return [];
  }
  try {
    const response = await fetch(`${serverApiBaseUrl}/content/patch-index`, {
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(2_000)
    });
    if (!response.ok) {
      return [];
    }
    return validPatchEntries(await response.json());
  } catch {
    return [];
  }
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const patchEntries = await getPatchSitemapEntries();
  return [
    { url: `${SITE_ORIGIN}/`, changeFrequency: "daily", priority: 1 },
    { url: `${SITE_ORIGIN}/tournaments`, changeFrequency: "hourly", priority: 0.9 },
    { url: `${SITE_ORIGIN}/info`, changeFrequency: "monthly", priority: 0.6 },
    { url: `${SITE_ORIGIN}/privacy`, changeFrequency: "yearly", priority: 0.3 },
    { url: `${SITE_ORIGIN}/terms`, changeFrequency: "yearly", priority: 0.3 },
    { url: `${SITE_ORIGIN}/stats`, changeFrequency: "daily", priority: 0.5 },
    ...patchEntries.map((patch) => ({
      url: `${SITE_ORIGIN}/patches/${patch.id}`,
      lastModified: patch.published_at,
      changeFrequency: "monthly" as const,
      priority: 0.7
    }))
  ];
}
