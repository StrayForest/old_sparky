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
  heroes: Array<{ name: string; imageUrl: string | null }>;
};

const gameAssetsCheckIntervalMs = 15 * 60 * 1000;
let gameAssetsCache: GameAssetsCache | null = null;
let gameAssetsRequest: Promise<Array<{ name: string; imageUrl: string | null }>> | null = null;

export async function getServerProfileHeroOptions(): Promise<Array<{ name: string; imageUrl: string | null }>> {
  const now = Date.now();
  if (gameAssetsCache && now - gameAssetsCache.checkedAt < gameAssetsCheckIntervalMs) {
    return gameAssetsCache.heroes.map((hero) => ({ ...hero }));
  }
  if (gameAssetsRequest) {
    return gameAssetsRequest;
  }

  const staleHeroes = gameAssetsCache?.heroes;
  gameAssetsRequest = fetchGameAssetOptions(staleHeroes);
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

async function fetchGameAssetOptions(staleHeroes?: Array<{ name: string; imageUrl: string | null }>): Promise<Array<{ name: string; imageUrl: string | null }>> {
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
      return gameAssetsCache.heroes.map((hero) => ({ ...hero }));
    }
    if (!response.ok) {
      return rememberGameAssetFallback(staleHeroes);
    }
    const payload = (await response.json()) as PlatformDeadlockGameAssets;
    const known = new Map(payload.heroes.map((hero) => [hero.name, hero.image_url]));
    const heroes = mergeHeroNames(payload.heroes.map((hero) => hero.name), deadlockHeroes)
      .map((name) => ({ name, imageUrl: known.get(name) ?? null }));
    gameAssetsCache = {
      checkedAt: Date.now(),
      etag: response.headers.get("etag")?.trim() ?? "",
      heroes,
    };
    return heroes.map((hero) => ({ ...hero }));
  } catch {
    return rememberGameAssetFallback(staleHeroes);
  }
}

function rememberGameAssetFallback(staleHeroes?: Array<{ name: string; imageUrl: string | null }>): Array<{ name: string; imageUrl: string | null }> {
  const heroes = staleHeroes ? staleHeroes.map((hero) => ({ ...hero })) : deadlockHeroes.map((name) => ({ name, imageUrl: null }));
  gameAssetsCache = {
    checkedAt: Date.now(),
    etag: gameAssetsCache?.etag ?? "",
    heroes,
  };
  return heroes.map((hero) => ({ ...hero }));
}

function mergeHeroNames(
  primary: readonly string[],
  fallback: readonly string[]
): string[] {
  const merged = [...primary, ...fallback];
  return merged.filter((hero, index) => hero && merged.indexOf(hero) === index);
}
