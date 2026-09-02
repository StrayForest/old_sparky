import "server-only";

import { cache } from "react";
import type { PlatformAuthBootstrap, PlatformUser } from "@/lib/platform-types";

export type ServerAuthSnapshot = {
  status: "authenticated" | "anonymous" | "unavailable";
  user: PlatformUser | null;
};

const serverApiBaseUrl = (
  process.env.PLATFORM_API_BASE_URL
  ?? `${process.env.PLATFORM_API_INTERNAL_ORIGIN ?? "http://127.0.0.1:8010"}/api/v1`
).replace(/\/$/u, "");
const serverAuthTimeoutMs = 2_000;

export function platformSessionCookieName(): string {
  return process.env.PLATFORM_SESSION_COOKIE_NAME?.trim()
    || "deadlock_platform_session";
}

function trustedServerApiBaseUrl(): string | null {
  try {
    const url = new URL(serverApiBaseUrl);
    if (!["http:", "https:"].includes(url.protocol)) {
      return null;
    }
    const hostname = url.hostname.toLowerCase();
    if (
      !["127.0.0.1", "::1", "localhost"].includes(hostname)
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

export const getServerCurrentUser = cache(async (
  cookieHeader: string
): Promise<ServerAuthSnapshot> => {
  if (!cookieHeader) {
    return { status: "anonymous", user: null };
  }
  const baseUrl = trustedServerApiBaseUrl();
  if (!baseUrl) {
    return { status: "unavailable", user: null };
  }
  try {
    const response = await fetch(`${baseUrl}/users/me`, {
      headers: {
        accept: "application/json",
        cookie: cookieHeader
      },
      cache: "no-store",
      signal: AbortSignal.timeout(serverAuthTimeoutMs)
    });
    if (response.status === 401 || response.status === 403) {
      return { status: "anonymous", user: null };
    }
    if (!response.ok) {
      return { status: "unavailable", user: null };
    }
    const user = await response.json() as unknown;
    if (!isPlatformUser(user)) {
      return { status: "unavailable", user: null };
    }
    return { status: "authenticated", user };
  } catch {
    // Public pages remain available during a transient internal API failure;
    // protected APIs still enforce the session independently.
    return { status: "unavailable", user: null };
  }
});

export const getServerAuthBootstrap = cache(async (
  cookieHeader: string
): Promise<ServerAuthSnapshot> => {
  if (!cookieHeader) {
    return { status: "anonymous", user: null };
  }
  const baseUrl = trustedServerApiBaseUrl();
  if (!baseUrl) {
    return { status: "unavailable", user: null };
  }
  try {
    const response = await fetch(`${baseUrl}/auth/bootstrap`, {
      headers: {
        accept: "application/json",
        cookie: cookieHeader
      },
      cache: "no-store",
      signal: AbortSignal.timeout(serverAuthTimeoutMs)
    });
    if (response.status === 401 || response.status === 403) {
      return { status: "anonymous", user: null };
    }
    if (!response.ok) {
      return { status: "unavailable", user: null };
    }
    const bootstrap = await response.json() as unknown;
    if (!isPlatformAuthBootstrap(bootstrap)) {
      return { status: "unavailable", user: null };
    }
    return { status: "authenticated", user: bootstrap };
  } catch {
    return { status: "unavailable", user: null };
  }
});

function isPlatformAuthBootstrap(value: unknown): value is PlatformAuthBootstrap {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<PlatformAuthBootstrap>;
  return typeof candidate.id === "string"
    && (candidate.email === null || typeof candidate.email === "string")
    && typeof candidate.display_name === "string"
    && typeof candidate.status === "string"
    && typeof candidate.created_at === "string"
    && !Number.isNaN(Date.parse(candidate.created_at))
    && Array.isArray(candidate.roles)
    && candidate.roles.every((role) => typeof role === "string")
    && typeof candidate.can_create_public_tournaments === "boolean"
    && typeof candidate.public_tournament_credits === "number"
    && Number.isFinite(candidate.public_tournament_credits)
    && typeof candidate.private_tournament_credits === "number"
    && Number.isFinite(candidate.private_tournament_credits)
    && (candidate.avatar_url === null || typeof candidate.avatar_url === "string")
    && (candidate.avatar_media === null
      || candidate.avatar_media === undefined
      || typeof candidate.avatar_media === "object");
}

function isPlatformUser(value: unknown): value is PlatformUser {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<PlatformUser>;
  const isOptionalNullableString = (field: unknown) => (
    field === undefined || field === null || typeof field === "string"
  );
  const isOptionalBoolean = (field: unknown) => (
    field === undefined || typeof field === "boolean"
  );
  const isOptionalNumber = (field: unknown) => (
    field === undefined || (typeof field === "number" && Number.isFinite(field))
  );
  return typeof candidate.id === "string"
    && (candidate.email === null || typeof candidate.email === "string")
    && typeof candidate.display_name === "string"
    && typeof candidate.status === "string"
    && typeof candidate.created_at === "string"
    && !Number.isNaN(Date.parse(candidate.created_at))
    && Array.isArray(candidate.roles)
    && candidate.roles.every((role) => typeof role === "string")
    && typeof candidate.can_create_public_tournaments === "boolean"
    && isOptionalNumber(candidate.public_tournament_credits)
    && isOptionalNumber(candidate.private_tournament_credits)
    && isOptionalNumber(candidate.private_tournament_monthly_remaining)
    && isOptionalNumber(candidate.private_tournament_monthly_limit)
    && isOptionalNullableString(candidate.avatar_url)
    && (candidate.avatar_media === undefined || candidate.avatar_media === null || typeof candidate.avatar_media === "object")
    && isOptionalNullableString(candidate.steam_id)
    && isOptionalBoolean(candidate.steam_linked)
    && isOptionalBoolean(candidate.has_password)
    && isOptionalBoolean(candidate.can_unlink_steam);
}
