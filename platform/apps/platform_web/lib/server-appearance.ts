import "server-only";

import {
  appearanceDefaults,
  resolveAppearanceColors,
  type AppearancePalette
} from "@/lib/appearance-palette";

type CachedPalette = { expiresAt: number; colors: AppearancePalette };
const configuredApiAddress = process.env.PLATFORM_API_INTERNAL_ORIGIN
  ?? process.env.PLATFORM_API_BASE_URL
  ?? "http://127.0.0.1:8010/api/v1";

function normalizeApiBase(value: string): string {
  try {
    const url = new URL(value);
    const path = url.pathname.replace(/\/+$/u, "");
    if (!path.endsWith("/api/v1")) url.pathname = `${path}/api/v1`;
    return url.toString().replace(/\/$/u, "");
  } catch {
    return value.replace(/\/+$/u, "");
  }
}

const apiBase = normalizeApiBase(configuredApiAddress);
const paletteCacheTtlMs = 1_000;
let paletteCache: CachedPalette | null = null;
let pendingRead: Promise<AppearancePalette> | null = null;

function trustedApiBase(): string | null {
  try {
    const url = new URL(apiBase);
    if (!(["http:", "https:"].includes(url.protocol)) || url.username || url.password || url.search || url.hash) return null;
    if (!["127.0.0.1", "::1", "localhost"].includes(url.hostname.toLowerCase())) return null;
    return url.toString().replace(/\/$/u, "");
  } catch {
    return null;
  }
}

function safeResolvedColors(value: unknown): AppearancePalette | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  let defaults: AppearancePalette;
  try {
    defaults = resolveAppearanceColors(appearanceDefaults);
  } catch {
    return null;
  }
  if (Object.keys(record).length !== Object.keys(defaults).length) return null;
  const colors: AppearancePalette = {};
  for (const key of Object.keys(defaults)) {
    const color = record[key];
    if (typeof color !== "string" || !/^#[0-9a-f]{6}$/iu.test(color)) return null;
    colors[key] = color.toUpperCase();
  }
  return colors;
}

async function readPalette(): Promise<AppearancePalette> {
  const base = trustedApiBase();
  if (!base) return resolveAppearanceColors(appearanceDefaults);
  try {
    const response = await fetch(`${base}/appearance`, {
      cache: "no-store",
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(750)
    });
    if (!response.ok) return paletteCache?.colors ?? resolveAppearanceColors(appearanceDefaults);
    const body: unknown = await response.json();
    const colors = safeResolvedColors(
      body && typeof body === "object" && !Array.isArray(body)
        ? (body as Record<string, unknown>).colors
        : null
    );
    if (!colors) return paletteCache?.colors ?? resolveAppearanceColors(appearanceDefaults);
    paletteCache = { expiresAt: Date.now() + paletteCacheTtlMs, colors };
    return colors;
  } catch {
    return paletteCache?.colors ?? resolveAppearanceColors(appearanceDefaults);
  }
}

export async function getServerAppearanceColors(): Promise<AppearancePalette> {
  if (paletteCache && paletteCache.expiresAt > Date.now()) return paletteCache.colors;
  if (!pendingRead) {
    pendingRead = readPalette().finally(() => { pendingRead = null; });
  }
  return pendingRead;
}
