import "server-only";

import * as http from "node:http";
import * as https from "node:https";
import { cache } from "react";
import type { PlatformAuthBootstrap, PlatformUser } from "@/lib/platform-types";
import {
  getServerRequestCorrelationHeaders,
  isSsrDiagnosticsEnabled,
  measureSsrStage
} from "@/lib/server-ssr-observability";

export type ServerAuthSnapshot = {
  status: "authenticated" | "anonymous" | "unavailable";
  user: PlatformUser | null;
};

const serverApiBaseUrl = (
  process.env.PLATFORM_API_BASE_URL
  ?? `${process.env.PLATFORM_API_INTERNAL_ORIGIN ?? "http://127.0.0.1:8010"}/api/v1`
).replace(/\/$/u, "");
const serverAuthTimeoutMs = 2_000;
const serverAuthTransport = process.env.PLATFORM_WEB_SERVER_AUTH_TRANSPORT?.trim().toLowerCase() === "node"
  ? "node"
  : "fetch";
const serverAuthResponseMaxBytes = 256 * 1024;
const serverAuthHttpAgent = new http.Agent({
  keepAlive: true,
  maxFreeSockets: 16,
  maxSockets: 128,
});
const serverAuthHttpsAgent = new https.Agent({
  keepAlive: true,
  maxFreeSockets: 16,
  maxSockets: 128,
});

type ServerJsonResponse = {
  status: number;
  ok: boolean;
  json: () => Promise<unknown>;
};

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
  return measureSsrStage("auth_current_user_fetch", async () => {
    try {
      const requestHeaders = isSsrDiagnosticsEnabled()
        ? await getServerRequestCorrelationHeaders()
        : { accept: "application/json", cookie: cookieHeader };
      if (requestHeaders instanceof Headers) {
        requestHeaders.set("accept", "application/json");
        requestHeaders.set("cookie", cookieHeader);
      }
      const response = await requestServerAuthJson(`${baseUrl}/users/me`, requestHeaders);
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
  return measureSsrStage("auth_bootstrap_fetch", async () => {
    try {
      const requestHeaders = isSsrDiagnosticsEnabled()
        ? await getServerRequestCorrelationHeaders()
        : { accept: "application/json", cookie: cookieHeader };
      if (requestHeaders instanceof Headers) {
        requestHeaders.set("accept", "application/json");
        requestHeaders.set("cookie", cookieHeader);
      }
      const response = await requestServerAuthJson(`${baseUrl}/auth/bootstrap`, requestHeaders);
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
});

function requestServerAuthJson(
  input: string,
  headers: HeadersInit,
): Promise<Response | ServerJsonResponse> {
  if (serverAuthTransport === "node") {
    return requestServerJson(input, headers);
  }
  return fetch(input, {
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(serverAuthTimeoutMs),
  });
}

function requestServerJson(
  input: string,
  headers: HeadersInit,
): Promise<ServerJsonResponse> {
  const url = new URL(input);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return Promise.reject(new Error("Unsupported server API protocol."));
  }

  return new Promise<ServerJsonResponse>((resolve, reject) => {
    let settled = false;
    const abortController = new AbortController();
    const timeout = setTimeout(() => {
      abortController.abort();
    }, serverAuthTimeoutMs);
    const settle = (operation: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeout) {
        clearTimeout(timeout);
      }
      operation();
    };

    function settleResponse(response: http.IncomingMessage): void {
      const chunks: Buffer[] = [];
      let totalBytes = 0;
      const contentLength = Number(response.headers["content-length"]);
      if (Number.isFinite(contentLength) && contentLength > serverAuthResponseMaxBytes) {
        response.resume();
        settle(() => reject(new Error("Server auth response exceeded its size limit.")));
        return;
      }
      response.on("data", (chunk: Buffer | string) => {
        const bytes = Buffer.byteLength(chunk);
        totalBytes += bytes;
        if (totalBytes > serverAuthResponseMaxBytes) {
          response.destroy(new Error("Server auth response exceeded its size limit."));
          settle(() => reject(new Error("Server auth response exceeded its size limit.")));
          return;
        }
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      });
      response.on("end", () => {
        settle(() => {
          try {
            const payload = JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
            const status = response.statusCode ?? 0;
            resolve({
              status,
              ok: status >= 200 && status < 300,
              json: async () => payload,
            });
          } catch (error) {
            reject(error instanceof Error ? error : new Error("Invalid server auth response."));
          }
        });
      });
      response.on("error", (error) => {
        settle(() => reject(error));
      });
    }

    const requestHeaders: Record<string, string> = {};
    new Headers(headers).forEach((value, key) => {
      requestHeaders[key] = value;
    });
    requestHeaders["accept-encoding"] = "identity";
    const requestOptions = {
      hostname: url.hostname.replace(/^\[|\]$/gu, ""),
      method: "GET" as const,
      path: `${url.pathname}${url.search}`,
      port: url.port || undefined,
      headers: requestHeaders,
      signal: abortController.signal,
    };
    const request = url.protocol === "https:"
      ? https.request({ ...requestOptions, agent: serverAuthHttpsAgent }, settleResponse)
      : http.request({ ...requestOptions, agent: serverAuthHttpAgent }, settleResponse);
    request.on("error", (error) => {
      settle(() => reject(error));
    });
    request.end();
  });
}

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
