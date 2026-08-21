import "server-only";

import { deadlockHeroes } from "@/lib/deadlock";
import {
  mapProfileWorkspacePayload,
  type ProfileWorkspace,
  type ProfileWorkspacePayload,
} from "@/lib/profile-model";
import { platformApiUrl } from "@/lib/platform-api";
import type { PlatformDeadlockGameAssets } from "@/lib/platform-types";

const profileReadTimeoutMs = 2_500;

export async function getServerProfileWorkspace(
  cookieHeader: string
): Promise<ProfileWorkspace | null> {
  const response = await fetch(platformApiUrl("/profiles/me/workspace"), {
    headers: {
      accept: "application/json",
      cookie: cookieHeader,
    },
    cache: "no-store",
    signal: AbortSignal.timeout(profileReadTimeoutMs),
  });

  if (response.status === 401 || response.status === 403) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Profile workspace request failed with ${response.status}.`);
  }

  return mapProfileWorkspacePayload(
    (await response.json()) as ProfileWorkspacePayload
  );
}

export async function getServerProfileHeroNames(): Promise<string[]> {
  try {
    const response = await fetch(platformApiUrl("/content/game-assets"), {
      headers: { accept: "application/json" },
      next: { revalidate: 900 },
      signal: AbortSignal.timeout(profileReadTimeoutMs),
    });
    if (!response.ok) {
      return [...deadlockHeroes];
    }
    const payload = (await response.json()) as PlatformDeadlockGameAssets;
    return mergeHeroNames(
      payload.heroes.map((hero) => hero.name),
      deadlockHeroes
    );
  } catch {
    return [...deadlockHeroes];
  }
}

function mergeHeroNames(
  primary: readonly string[],
  fallback: readonly string[]
): string[] {
  const merged = [...primary, ...fallback];
  return merged.filter((hero, index) => hero && merged.indexOf(hero) === index);
}
