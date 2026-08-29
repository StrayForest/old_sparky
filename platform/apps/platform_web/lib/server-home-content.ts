import "server-only";

import { platformApiUrl } from "@/lib/platform-api";
import type { PlatformHomeContent } from "@/lib/platform-types";

const homeContentReadTimeoutMs = 2_000;

export async function getServerHomeContent(): Promise<PlatformHomeContent | null> {
  try {
    const response = await fetch(platformApiUrl("/content/home"), {
      headers: { accept: "application/json" },
      next: { revalidate: 300 },
      signal: AbortSignal.timeout(homeContentReadTimeoutMs),
    });

    if (!response.ok) {
      return null;
    }

    return (await response.json()) as PlatformHomeContent;
  } catch {
    return null;
  }
}
