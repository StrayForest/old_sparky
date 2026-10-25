import "server-only";

import * as http from "node:http";
import * as https from "node:https";
import { cache } from "react";
import type { PlatformAuthBootstrap, PlatformUser } from "@/lib/platform-types";
import {
  getSsrAdmissionStageObserver,
  getSsrAuthBodyParseObserver,
  getServerRequestCorrelationHeaders,
  isSsrDiagnosticsEnabled,
  isSsrObservabilityActive,
  measureSsrStage
} from "@/lib/server-ssr-observability";
import {
  runWithServerInternalRead
} from "@/lib/server-internal-read-scheduler";
import {
  cancelServerAuthFetchTerminalResponse,
  cancelServerFetchResponse
} from "@/lib/server-fetch-response";

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

type ServerAuthJsonParseObserver = (
  durationMs: number,
  outcome: "ok" | "error",
) => void | Promise<void>;

const serverAuthFetchControllers = new WeakMap<Response, AbortController>();

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
    const admissionObserver = isSsrObservabilityActive()
      ? await getSsrAdmissionStageObserver()
      : undefined;
    return runWithServerInternalRead(async (): Promise<ServerAuthSnapshot> => {
      try {
        const requestHeaders = isSsrDiagnosticsEnabled()
          ? await getServerRequestCorrelationHeaders()
          : { accept: "application/json", cookie: cookieHeader };
        if (requestHeaders instanceof Headers) {
          requestHeaders.set("accept", "application/json");
          requestHeaders.set("cookie", cookieHeader);
        }
        const jsonParseObserver = serverAuthTransport === "node" && isSsrObservabilityActive()
          ? await getSsrAuthBodyParseObserver()
          : undefined;
        const response = await measureSsrStage("auth_http_fetch", () =>
          requestServerAuthJson(`${baseUrl}/users/me`, requestHeaders, jsonParseObserver)
        );
        const fetchController = getServerAuthFetchController(response);
        const fetchTerminal = fetchController
          ? cancelServerAuthFetchTerminalResponse(response as Response, fetchController)
          : null;
        if (fetchTerminal) return fetchTerminal;
        if (response.status === 401 || response.status === 403) {
          await cancelServerAuthBody(response);
          return { status: "anonymous", user: null };
        }
        if (!response.ok) {
          await cancelServerAuthBody(response);
          return { status: "unavailable", user: null };
        }
        const user = response instanceof Response
          ? await measureSsrStage("auth_body_parse", async () =>
            await response.json() as unknown
          )
          : await response.json() as unknown;
        return measureSsrStage("auth_projection", async () =>
          isPlatformUser(user)
            ? { status: "authenticated", user }
            : { status: "unavailable", user: null }
        );
      } catch {
        // Public pages remain available during a transient internal API failure;
        // protected APIs still enforce the session independently.
        return { status: "unavailable", user: null };
      }
    }, undefined, admissionObserver).catch((): ServerAuthSnapshot => ({ status: "unavailable", user: null }));
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
    const admissionObserver = isSsrObservabilityActive()
      ? await getSsrAdmissionStageObserver()
      : undefined;
    return runWithServerInternalRead(async (): Promise<ServerAuthSnapshot> => {
      try {
        const requestHeaders = isSsrDiagnosticsEnabled()
          ? await getServerRequestCorrelationHeaders()
          : { accept: "application/json", cookie: cookieHeader };
        if (requestHeaders instanceof Headers) {
          requestHeaders.set("accept", "application/json");
          requestHeaders.set("cookie", cookieHeader);
        }
        const jsonParseObserver = serverAuthTransport === "node" && isSsrObservabilityActive()
          ? await getSsrAuthBodyParseObserver()
          : undefined;
        const response = await measureSsrStage("auth_http_fetch", () =>
          requestServerAuthJson(`${baseUrl}/auth/bootstrap`, requestHeaders, jsonParseObserver)
        );
        const fetchController = getServerAuthFetchController(response);
        const fetchTerminal = fetchController
          ? cancelServerAuthFetchTerminalResponse(response as Response, fetchController)
          : null;
        if (fetchTerminal) return fetchTerminal;
        if (response.status === 401 || response.status === 403) {
          await cancelServerAuthBody(response);
          return { status: "anonymous", user: null };
        }
        if (!response.ok) {
          await cancelServerAuthBody(response);
          return { status: "unavailable", user: null };
        }
        const bootstrap = response instanceof Response
          ? await measureSsrStage("auth_body_parse", async () =>
            await response.json() as unknown
          )
          : await response.json() as unknown;
        return measureSsrStage("auth_projection", async () =>
          isPlatformAuthBootstrap(bootstrap)
            ? { status: "authenticated", user: bootstrap }
            : { status: "unavailable", user: null }
        );
      } catch {
        return { status: "unavailable", user: null };
      }
    }, undefined, admissionObserver).catch((): ServerAuthSnapshot => ({ status: "unavailable", user: null }));
  });
});

function getServerAuthFetchController(
  response: Response | ServerJsonResponse
): AbortController | undefined {
  if (!(response instanceof Response)) return undefined;
  const controller = serverAuthFetchControllers.get(response);
  if (controller) serverAuthFetchControllers.delete(response);
  return controller;
}

async function cancelServerAuthBody(
  response: Response | ServerJsonResponse
): Promise<void> {
  const controller = getServerAuthFetchController(response);
  if (controller && response instanceof Response) {
    cancelServerFetchResponse(response, controller);
    return;
  }
  if (!("body" in response) || !response.body) return;
  try {
    await response.body.cancel();
  } catch {
    // Keep the established auth outcome when a non-success body cannot be drained.
  }
}

function requestServerAuthJson(
  input: string,
  headers: HeadersInit,
  jsonParseObserver?: ServerAuthJsonParseObserver,
): Promise<Response | ServerJsonResponse> {
  if (serverAuthTransport === "node") {
    return requestServerJson(input, headers, jsonParseObserver);
  }
  const controller = new AbortController();
  const timeout = AbortSignal.timeout(serverAuthTimeoutMs);
  return fetch(input, {
    headers,
    cache: "no-store",
    signal: AbortSignal.any([controller.signal, timeout]),
  }).then((response) => {
    serverAuthFetchControllers.set(response, controller);
    return response;
  });
}

function requestServerJson(
  input: string,
  headers: HeadersInit,
  jsonParseObserver?: ServerAuthJsonParseObserver,
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
          const status = response.statusCode ?? 0;
          const bodyText = Buffer.concat(chunks).toString("utf8");
          const ok = status >= 200 && status < 300;
          // Preserve the native transport's historical eager parse on every
          // status. Malformed 401/403 bodies map to unavailable, not anonymous.
          const parseStartedAt = jsonParseObserver ? performance.now() : null;
          let payload: unknown;
          try {
            payload = JSON.parse(bodyText) as unknown;
          } catch (error) {
            if (parseStartedAt !== null) {
              notifyJsonParseObserver(
                jsonParseObserver,
                performance.now() - parseStartedAt,
                "error",
              );
            }
            reject(error instanceof Error ? error : new Error("Invalid server auth response."));
            return;
          }
          if (parseStartedAt !== null) {
            notifyJsonParseObserver(
              jsonParseObserver,
              performance.now() - parseStartedAt,
              "ok",
            );
          }
          resolve({ status, ok, json: async () => payload });
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

function notifyJsonParseObserver(
  observer: ServerAuthJsonParseObserver | undefined,
  durationMs: number,
  outcome: "ok" | "error",
): void {
  if (!observer) return;
  try {
    void Promise.resolve(observer(Math.max(0, durationMs), outcome)).catch(() => {});
  } catch {
    // Diagnostic failures must not change the auth response outcome.
  }
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
