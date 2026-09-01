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

type ProfileBootstrapPayload = ProfileWorkspacePayload & {
  account: {
    id: string;
    email: string | null;
    display_name: string;
    status: string;
    created_at: string;
    roles: string[];
    steam_id: string | null;
    steam_linked: boolean;
  };
};

export async function getServerProfileBootstrap(
  cookieHeader: string
): Promise<ProfileWorkspace | null> {
  const response = await fetch(platformApiUrl("/profiles/me/bootstrap"), {
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
    throw new Error(`Profile bootstrap request failed with ${response.status}.`);
  }

  return mapProfileWorkspacePayload(
    (await response.json()) as ProfileBootstrapPayload
  );
}

type GameAssetsCache = {
  checkedAt: number;
  etag: string;
  heroNames: string[];
};

const gameAssetsCheckIntervalMs = 15 * 60 * 1000;
let gameAssetsCache: GameAssetsCache | null = null;
let gameAssetsRequest: Promise<string[]> | null = null;

export async function getServerProfileHeroNames(): Promise<string[]> {
  const now = Date.now();
  if (gameAssetsCache && now - gameAssetsCache.checkedAt < gameAssetsCheckIntervalMs) {
    return [...gameAssetsCache.heroNames];
  }
  if (gameAssetsRequest) {
    return gameAssetsRequest;
  }

  const staleHeroNames = gameAssetsCache?.heroNames;
  gameAssetsRequest = fetchGameAssetNames(staleHeroNames);
  const request = gameAssetsRequest;
  request.then(
    () => {
      if (gameAssetsRequest === request) {
        gameAssetsRequest = null;
      }
    },
    () => {
      if (gameAssetsRequest === request) {
        gameAssetsRequest = null;
      }
    }
  );
  return request;
}

async function fetchGameAssetNames(staleHeroNames?: string[]): Promise<string[]> {
  try {
    const headers = new Headers({ accept: "application/json" });
    if (gameAssetsCache?.etag) {
      headers.set("if-none-match", gameAssetsCache.etag);
    }
    const response = await fetch(platformApiUrl("/content/game-assets"), {
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(profileReadTimeoutMs),
    });
    if (response.status === 304 && gameAssetsCache) {
      gameAssetsCache.checkedAt = Date.now();
      return [...gameAssetsCache.heroNames];
    }
    if (!response.ok) {
      return rememberGameAssetFallback(staleHeroNames);
    }
    const payload = (await response.json()) as PlatformDeadlockGameAssets;
    const heroNames = mergeHeroNames(
      payload.heroes.map((hero) => hero.name),
      deadlockHeroes
    );
    gameAssetsCache = {
      checkedAt: Date.now(),
      etag: response.headers.get("etag")?.trim() ?? "",
      heroNames,
    };
    return [...heroNames];
  } catch {
    return rememberGameAssetFallback(staleHeroNames);
  }
}

function rememberGameAssetFallback(staleHeroNames?: string[]): string[] {
  const heroNames = staleHeroNames ? [...staleHeroNames] : [...deadlockHeroes];
  gameAssetsCache = {
    checkedAt: Date.now(),
    etag: gameAssetsCache?.etag ?? "",
    heroNames,
  };
  return [...heroNames];
}

function mergeHeroNames(
  primary: readonly string[],
  fallback: readonly string[]
): string[] {
  const merged = [...primary, ...fallback];
  return merged.filter((hero, index) => hero && merged.indexOf(hero) === index);
}
