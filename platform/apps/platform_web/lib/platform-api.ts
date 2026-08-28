import { notifyPlatformUnauthorized } from "@/lib/auth-session-signal";
import { deadlockHeroes } from "@/lib/deadlock";
import {
  normalizeVisibility,
  ranks,
  registrationTimerLabel,
  sortRanksByStrengthDesc,
  statusLabel
} from "@/lib/tournament-model";
import type {
  Bracket,
  Match,
  PlayerProfile,
  Registration,
  Team,
  TeamMember,
  TournamentDetail,
  TournamentListQuery,
  TournamentPage,
  TournamentSchedule,
  TournamentStatus,
  TournamentSummary
} from "@/lib/types";
import type {
  PlatformDeadlockDreamSlot,
  PlatformMediaAccepted,
  PlatformMediaDeleteAccepted,
  PlatformMediaDescriptor,
  PlatformTournamentDeadlockAutoAssignmentJob,
  PlatformTournamentDeadlockAutoAssignmentState,
  PlatformTournamentDeadlockReadyCheckState,
  PlatformTournamentDeadlockReadyVote,
  PlatformStatsOverview,
  PlatformStatsRankBucket,
  PlatformTournamentScopedProfile,
  PlatformUser
} from "@/lib/platform-types";

type ApiTournamentListItem = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  cover_url?: string | null;
  cover_media?: PlatformMediaDescriptor | null;
  organizer_user_id: string;
  organizer_display_name?: string | null;
  organizer_avatar_url?: string | null;
  organizer_avatar_media?: PlatformMediaDescriptor | null;
  starts_at?: string | null;
  registration_starts_at?: string | null;
  registration_closes_at?: string | null;
  ready_check_starts_at?: string | null;
  ready_check_ends_at?: string | null;
  captain_selection_starts_at?: string | null;
  status: string;
  visibility: string;
  allowed_ranks?: string[] | null;
  participant_count?: number | null;
  max_participants?: number | null;
  teams_count?: number | null;
  format_slug: string;
  match_format?: string | null;
  final_format?: string | null;
  current_user_participant_status?: string | null;
  current_user_has_invite_access?: boolean | null;
  state_version?: number | null;
};

type ApiTournamentDetail = ApiTournamentListItem;

type ApiRegistration = {
  id: string;
  user_id: string;
  display_name: string;
  entry_type: string;
  team_name?: string | null;
  status: string;
  created_at: string;
};

type ApiTournamentWorkspace = {
  tournament: ApiTournamentDetail;
  server_time: string;
  current_user?: PlatformUser | null;
  current_user_active_commitment?: {
    id: string;
    tournament_id: string;
    tournament_slug: string;
    tournament_name: string;
    assignment_run_id: string;
    team_id: string;
    team_name: string;
    activated_at: string;
  } | null;
  participants?: ApiRegistration[] | null;
  participants_available?: boolean | null;
  bracket?: ApiBracket | null;
  ready_check?: PlatformTournamentDeadlockReadyCheckState | null;
  auto_assignment?: PlatformTournamentDeadlockAutoAssignmentState | null;
  state_version?: number | null;
};

type ApiTeamMember = {
  user_id: string;
  handle?: string | null;
  display_name?: string | null;
  avatar_url?: string | null;
  rank?: string | null;
  subrank?: number | null;
  is_captain?: boolean | null;
  is_substitute?: boolean | null;
};

type ApiTeam = {
  id: string;
  name?: string | null;
  seed?: number | null;
  starter_strength?: number | null;
  starter_average_strength?: number | null;
  captain_id?: string | null;
  color?: string | null;
  emblem?: string | null;
  members?: ApiTeamMember[] | null;
};

type ApiMatch = {
  id: string;
  round_number?: number | null;
  match_order?: number | null;
  sequence_number?: number | null;
  team_a_id?: string | null;
  team_b_id?: string | null;
  home_label?: string | null;
  away_label?: string | null;
  score_a?: number | null;
  score_b?: number | null;
  home_score?: number | null;
  away_score?: number | null;
  winner_team_id?: string | null;
  winner_side?: string | null;
  home_source_match_id?: string | null;
  away_source_match_id?: string | null;
  status?: string | null;
  match_format?: string | null;
  ready?: boolean | null;
  scheduled_at?: string | null;
};

type ApiBracket = {
  tournament_id?: string | null;
  status?: string | null;
  tournament_status?: string | null;
  revision?: number | null;
  can_manage?: boolean | null;
  capabilities?: ApiBracketCapabilities | null;
  teams?: ApiTeam[] | null;
  matches?: ApiMatch[] | null;
};

type ApiBracketCapabilities = {
  can_manage?: boolean | null;
  can_schedule_matches?: boolean | null;
  can_report_matches?: boolean | null;
};

type ApiPlayerProfile = {
  user_id?: string | null;
  id?: string;
  account_email?: string | null;
  handle?: string | null;
  display_name?: string | null;
  avatar_url?: string | null;
  banner_url?: string | null;
  avatar_media?: PlatformMediaDescriptor | null;
  banner_media?: PlatformMediaDescriptor | null;
  rank?: string | null;
  subrank?: string | number | null;
  hours_range?: string | null;
  playtime?: string | null;
  roles?: string[] | null;
  hero_pool?: string[] | null;
  pool?: string[] | null;
  completion_percent?: number | null;
  contact_email?: string | null;
  region?: string | null;
  steam_id?: string | null;
  discord_account?: string | null;
  captain_team_name?: string | null;
};

type ApiPublicPlayerProfile = ApiPlayerProfile & {
  deadlock_profile?: ApiDeadlockProfile | null;
};

type ApiDeadlockProfile = {
  user_id: string;
  rank: string;
  subrank: number;
  playtime: string;
  roles: string[];
  pool: string[];
  captain_priority?: string | null;
};

type ApiDeadlockProfileUpdate = {
  rank: string;
  subrank: number;
  playtime: string;
  roles: string[];
  pool: string[];
  captain_priority: string;
};

type ApiStatsOverview = Partial<Omit<PlatformStatsOverview, "rank_distribution">> & {
  deadlock_rank_distribution?: unknown;
};

export type PlayerProfileUpdatePayload = {
  rank?: string | null;
  subrank?: string | null;
  hours_range?: string | null;
  roles?: string[];
  hero_pool?: string[];
  captain_preference?: Record<string, unknown>;
};

export type AccountProfileUpdatePayload = {
  display_name?: string | null;
  handle?: string | null;
  bio?: string | null;
  contact_email?: string | null;
  region?: string | null;
  discord_account?: string | null;
  captain_team_name?: string | null;
};

export type AccountSecurityUpdatePayload = {
  current_password: string;
  email?: string | null;
  new_password?: string | null;
};

export type TournamentCreatePayload = {
  title: string;
  description: string;
  cover_url?: string | null;
  visibility: "public" | "private";
  invite_code?: string | null;
  status: "registration_open" | "registration_closed";
  bracket_type: string;
  match_format: string;
  final_format: string;
  participant_mode: string;
  allowed_rank_codes: string[];
  starts_at: string;
  max_participants?: number | null;
  teams_count: number;
  schedule: {
    registration_starts_at: string | null;
    registration_closes_at: string;
    check_in_starts_at: string;
    teams_form_at: string;
    starts_at: string;
    timezone: string;
  };
};

export type TournamentInviteCodeAvailability = {
  code: string;
  available: boolean;
};

const serverApiBaseUrl = process.env.PLATFORM_API_BASE_URL
  ?? `${process.env.PLATFORM_API_INTERNAL_ORIGIN ?? "http://127.0.0.1:8010"}/api/v1`;
const apiBaseUrl = (
  process.env.NEXT_PUBLIC_PLATFORM_API_BASE_URL
  ?? (typeof window === "undefined" ? serverApiBaseUrl : "/api/v1")
).replace(/\/$/, "");
const csrfHeaderName = "X-CSRF-Token";
const unsafeRequestMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type PlatformRequestInit = RequestInit & {
  csrfPolicy?: "session-bound" | "origin-only";
};

let cachedCsrfToken: string | null = null;
let csrfTokenGeneration = 0;
let pendingCsrfTokenRequest: Promise<string | null> | null = null;

export class PlatformApiError extends Error {
  status: number;
  retryAfterSeconds: number | null;

  constructor(message: string, status: number, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "PlatformApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

async function platformFetch(
  input: string | URL,
  init: PlatformRequestInit = {}
): Promise<Response> {
  const { csrfPolicy = "session-bound", ...requestInit } = init;
  const method = (requestInit.method ?? "GET").toUpperCase();
  const headers = new Headers(requestInit.headers);
  const shouldProtectRequest = (
    typeof window !== "undefined"
    && unsafeRequestMethods.has(method)
    && requestInit.credentials !== "omit"
    && csrfPolicy === "session-bound"
    && !headers.has(csrfHeaderName)
  );

  let sentCsrfToken: string | null = null;
  if (shouldProtectRequest) {
    const token = await getPlatformCsrfToken();
    if (token) {
      headers.set(csrfHeaderName, token);
      sentCsrfToken = token;
    }
  }

  const response = await fetch(input, { ...requestInit, headers });
  rememberIssuedCsrfToken(response);
  if (response.status === 401) {
    resetPlatformCsrfToken();
    await notifyIfSessionIsInvalid(response);
  }

  const csrfTokenWasRejected = shouldProtectRequest
    && Boolean(sentCsrfToken)
    && await isRejectedCsrfToken(response);
  if (
    !csrfTokenWasRejected
    || !canReplayRequestBody(requestInit.body)
    || requestInit.signal?.aborted
  ) {
    if (csrfTokenWasRejected && sentCsrfToken) {
      invalidateRejectedCsrfToken(sentCsrfToken);
    }
    return response;
  }

  const freshToken = await refreshPlatformCsrfToken(sentCsrfToken as string);
  if (!freshToken) {
    return response;
  }

  const retryHeaders = new Headers(headers);
  retryHeaders.set(csrfHeaderName, freshToken);
  const retryResponse = await fetch(input, { ...requestInit, headers: retryHeaders });
  rememberIssuedCsrfToken(retryResponse);
  if (retryResponse.status === 401) {
    resetPlatformCsrfToken();
    await notifyIfSessionIsInvalid(retryResponse);
  } else if (await isRejectedCsrfToken(retryResponse)) {
    invalidateRejectedCsrfToken(freshToken);
  }
  return retryResponse;
}

async function getPlatformCsrfToken(): Promise<string | null> {
  if (typeof window === "undefined") {
    return null;
  }
  if (cachedCsrfToken) {
    return cachedCsrfToken;
  }
  if (pendingCsrfTokenRequest) {
    return pendingCsrfTokenRequest;
  }

  const requestGeneration = csrfTokenGeneration;
  const request = (async () => {
    const response = await fetch(`${apiBaseUrl}/auth/csrf`, {
      headers: { Accept: "application/json" },
      credentials: "include",
      cache: "no-store"
    });
    if (response.status === 401 || response.status === 403) {
      return null;
    }
    if (!response.ok) {
      throw new PlatformApiError(
        response.statusText || "Не удалось подготовить защищённый запрос.",
        response.status
      );
    }

    const payload = await response.json() as { csrf_token?: unknown };
    if (typeof payload.csrf_token !== "string" || payload.csrf_token.length < 32) {
      throw new PlatformApiError("Сервер вернул некорректный CSRF-токен.", response.status);
    }
    if (requestGeneration === csrfTokenGeneration) {
      cachedCsrfToken = payload.csrf_token;
    }
    return payload.csrf_token;
  })();
  pendingCsrfTokenRequest = request;

  try {
    return await request;
  } finally {
    if (pendingCsrfTokenRequest === request) {
      pendingCsrfTokenRequest = null;
    }
  }
}

export function resetPlatformCsrfToken(): void {
  cachedCsrfToken = null;
  csrfTokenGeneration += 1;
  pendingCsrfTokenRequest = null;
}

function rememberIssuedCsrfToken(response: Response): void {
  if (typeof window === "undefined") {
    return;
  }
  const issuedToken = response.headers.get(csrfHeaderName)?.trim() ?? "";
  if (issuedToken.length < 32 || issuedToken.length > 128 || issuedToken === cachedCsrfToken) {
    return;
  }
  cachedCsrfToken = issuedToken;
  csrfTokenGeneration += 1;
  pendingCsrfTokenRequest = null;
}

async function notifyIfSessionIsInvalid(response: Response): Promise<void> {
  try {
    const payload = await response.clone().json() as { detail?: unknown };
    const detail = typeof payload.detail === "string" ? payload.detail : "";
    notifyPlatformUnauthorized(detail);
  } catch {
    // A non-JSON 401 is not enough evidence to discard a known client session.
  }
}

function invalidateRejectedCsrfToken(rejectedToken: string): void {
  if (cachedCsrfToken !== rejectedToken) {
    return;
  }
  cachedCsrfToken = null;
  csrfTokenGeneration += 1;
}

async function refreshPlatformCsrfToken(rejectedToken: string): Promise<string | null> {
  if (cachedCsrfToken && cachedCsrfToken !== rejectedToken) {
    return cachedCsrfToken;
  }
  if (pendingCsrfTokenRequest) {
    return pendingCsrfTokenRequest;
  }
  invalidateRejectedCsrfToken(rejectedToken);
  return getPlatformCsrfToken();
}

async function isRejectedCsrfToken(response: Response): Promise<boolean> {
  if (response.status !== 403) {
    return false;
  }
  try {
    const payload = await response.clone().json() as { detail?: unknown };
    return payload.detail === "CSRF token is missing or invalid.";
  } catch {
    return false;
  }
}

function canReplayRequestBody(body: BodyInit | null | undefined): boolean {
  if (body === undefined || body === null || typeof body === "string") {
    return true;
  }
  return (
    body instanceof URLSearchParams
    || body instanceof FormData
    || body instanceof Blob
    || body instanceof ArrayBuffer
    || ArrayBuffer.isView(body)
  );
}

export async function platformApiRequest<T>(
  path: string,
  init: PlatformRequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  const response = await platformFetch(platformApiUrl(path), {
    ...init,
    headers,
    credentials: "include",
    cache: init.cache ?? "no-store"
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json() as { detail?: unknown };
      detail = apiErrorDetail(payload.detail) || detail;
    } catch {
      detail = detail || "Platform request failed.";
    }
    const retryAfterHeader = response.headers.get("Retry-After");
    const retryAfterSeconds = retryAfterHeader && /^\d+$/u.test(retryAfterHeader)
      ? Number(retryAfterHeader)
      : null;
    throw new PlatformApiError(detail, response.status, retryAfterSeconds);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return await response.json() as T;
}

export type PlatformApiPage<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
};

export async function platformApiPageRequest<T>(
  path: string,
  init: RequestInit = {}
): Promise<PlatformApiPage<T>> {
  const headers = new Headers(init.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  const response = await platformFetch(platformApiUrl(path), {
    ...init,
    headers,
    credentials: "include",
    cache: init.cache ?? "no-store"
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json() as { detail?: unknown };
      detail = apiErrorDetail(payload.detail) || detail;
    } catch {
      detail = detail || "Platform request failed.";
    }
    throw new PlatformApiError(detail, response.status);
  }

  const payload = await response.json() as T[];
  const items = Array.isArray(payload) ? payload : [];
  const total = headerInteger(response.headers, "X-Total-Count") ?? items.length;
  const limit = headerInteger(response.headers, "X-Limit") ?? items.length;
  const offset = headerInteger(response.headers, "X-Offset") ?? 0;
  const hasMoreHeader = response.headers.get("X-Has-More");

  return {
    items,
    total,
    limit,
    offset,
    hasMore: hasMoreHeader === null
      ? offset + items.length < total
      : hasMoreHeader.toLowerCase() === "true"
  };
}

export function platformApiUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${apiBaseUrl}${normalizedPath}`;
}

function apiErrorDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    return typeof message === "string" ? message : "";
  }
  return "";
}

export function platformApiMessage(error: unknown, fallback: string): string {
  if (error instanceof PlatformApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

type TournamentPageRequestOptions = {
  signal?: AbortSignal;
};

export async function getTournamentSummaries(
  query: TournamentListQuery = {},
  options: TournamentPageRequestOptions = {}
): Promise<TournamentPage> {
  return requestTournamentPage("/tournaments", query, options.signal);
}

export async function getMyTournamentSummaries(
  query: TournamentListQuery = {},
  options: Pick<TournamentPageRequestOptions, "signal"> = {}
): Promise<TournamentPage> {
  return requestTournamentPage("/tournaments/mine", query, options.signal);
}

async function requestTournamentPage(
  path: string,
  query: TournamentListQuery,
  signal?: AbortSignal
): Promise<TournamentPage> {
  const limit = query.limit ?? 9;
  const offset = query.offset ?? 0;
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset)
  });
  const search = query.search?.trim();
  if (search) {
    params.set("search", search);
  }
  if (query.scope) {
    params.set("scope", query.scope);
  }
  if (query.status) {
    params.set("status", query.status);
  }
  if (query.rank) {
    params.append("rank", rankCodeToApiName(query.rank));
  }
  if (query.dateSort) {
    params.set("date_sort", query.dateSort);
  }

  const response = await platformFetch(`${apiBaseUrl}${path}?${params.toString()}`, {
    headers: { accept: "application/json" },
    credentials: "include",
    cache: "no-store",
    signal
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      detail = detail || "Platform request failed.";
    }
    throw new PlatformApiError(detail, response.status);
  }

  const payload = await response.json() as ApiTournamentListItem[];
  const items = payload.map(mapTournamentSummary);
  const total = headerInteger(response.headers, "X-Total-Count") ?? items.length;
  const responseLimit = headerInteger(response.headers, "X-Limit") ?? limit;
  const responseOffset = headerInteger(response.headers, "X-Offset") ?? offset;
  const hasMoreHeader = response.headers.get("X-Has-More");

  return {
    items,
    total,
    limit: responseLimit,
    offset: responseOffset,
    hasMore: hasMoreHeader === null
      ? responseOffset + items.length < total
      : hasMoreHeader.toLowerCase() === "true"
  };
}

function headerInteger(headers: Headers, name: string): number | null {
  const rawValue = headers.get(name);
  if (rawValue === null) {
    return null;
  }
  const value = Number.parseInt(rawValue, 10);
  return Number.isFinite(value) && value >= 0 ? value : null;
}


export async function getTournamentWorkspace(
  slug: string,
  requestHeaders: HeadersInit = {},
  options: {
    participantsLimit?: number;
    workspaceView?: "detail" | "bracket" | "bracket_summary";
    includeCurrentUser?: boolean;
  } = {}
): Promise<{ tournament: TournamentDetail; currentUser: PlatformUser | null } | null> {
  const headers = new Headers(requestHeaders);
  headers.set("accept", "application/json");
  const participantsLimit = Math.max(0, options.participantsLimit ?? 25);
  const params = new URLSearchParams({
    participants_limit: String(participantsLimit),
    participants_offset: "0",
    workspace_view: options.workspaceView ?? "bracket"
  });
  if (options.includeCurrentUser !== undefined) {
    params.set("include_current_user", String(options.includeCurrentUser));
  }

  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/workspace?${params.toString()}`, {
    headers,
    credentials: "include",
    cache: "no-store"
  });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new PlatformApiError(response.statusText || "Failed to load tournament.", response.status);
  }

  const workspace = await response.json() as ApiTournamentWorkspace;
  const registrations = workspace.participants_available
    ? dedupeRegistrations((workspace.participants ?? []).map(mapRegistration))
    : [];
  const assignmentState = workspace.auto_assignment ?? null;
  const activeCommitment = workspace.current_user_active_commitment ?? null;
  return {
    tournament: mapTournamentDetail(
      workspace.tournament,
      registrations,
      workspace.bracket ? mapBracket(workspace.bracket) : null,
      {
        serverTime: workspace.server_time,
        readyCheckState: workspace.ready_check ?? null,
        deadlockAssignment: assignmentState?.published_run ?? assignmentState?.latest_run ?? null,
        activeCommitment: activeCommitment
          ? {
              id: activeCommitment.id,
              tournamentId: activeCommitment.tournament_id,
              tournamentSlug: activeCommitment.tournament_slug,
              tournamentName: activeCommitment.tournament_name,
              assignmentRunId: activeCommitment.assignment_run_id,
              teamId: activeCommitment.team_id,
              teamName: activeCommitment.team_name,
              activatedAt: activeCommitment.activated_at
            }
          : null
      }
    ),
    currentUser: workspace.current_user ?? null
  };
}

export async function getPlatformStatsOverview(): Promise<PlatformStatsOverview> {
  const response = await platformFetch(`${apiBaseUrl}/stats/overview`, {
    headers: { accept: "application/json" },
    next: { revalidate: 60 }
  });
  if (!response.ok) {
    throw new PlatformApiError(response.statusText || "Failed to load platform stats.", response.status);
  }
  return mapStatsOverview(await response.json() as ApiStatsOverview);
}

export async function getCurrentPlayerProfile(
  requestHeaders: HeadersInit = {}
): Promise<PlayerProfile | null> {
  const headers = new Headers(requestHeaders);
  headers.set("accept", "application/json");

  const response = await platformFetch(`${apiBaseUrl}/profiles/me`, {
    headers,
    credentials: "include",
    cache: "no-store"
  });
  if (response.status === 401 || response.status === 403) {
    return null;
  }
  if (!response.ok) {
    throw new PlatformApiError(
      response.statusText || "Failed to load current profile.",
      response.status
    );
  }

  const profile = await response.json() as ApiPlayerProfile;
  let deadlockProfile: ApiDeadlockProfile | null = null;
  try {
    const deadlockResponse = await platformFetch(`${apiBaseUrl}/profiles/me/deadlock`, {
      headers,
      credentials: "include",
      cache: "no-store"
    });
    if (deadlockResponse.ok) {
      deadlockProfile = await deadlockResponse.json() as ApiDeadlockProfile;
    }
  } catch {
    deadlockProfile = null;
  }

  return mapPlayerProfile(profile, deadlockProfile);
}

export async function getPublicPlayerProfile(handle: string): Promise<PlayerProfile | null> {
  try {
    const profile = await platformApiRequest<ApiPublicPlayerProfile>(
      `/profiles/public/${encodeURIComponent(handle)}`
    );
    return mapPlayerProfile(profile, profile.deadlock_profile ?? null);
  } catch (error) {
    if (error instanceof PlatformApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

export async function getTournamentPlayerProfile(
  slug: string,
  userId: string,
  requestHeaders: HeadersInit = {}
): Promise<PlayerProfile | null> {
  const headers = new Headers(requestHeaders);
  headers.set("accept", "application/json");
  const response = await platformFetch(
    `${apiBaseUrl}/tournaments/${encodeURIComponent(slug)}/profiles/${encodeURIComponent(userId)}`,
    {
      headers,
      credentials: "include",
      cache: "no-store"
    }
  );
  if ([401, 403, 404, 409].includes(response.status)) {
    return null;
  }
  if (!response.ok) {
    throw new PlatformApiError(response.statusText || "Failed to load tournament profile.", response.status);
  }
  const payload = await response.json() as PlatformTournamentScopedProfile;
  return mapPlayerProfile(payload.profile, payload.deadlock_profile ?? null);
}

export async function updateCurrentPlayerProfile(
  payload: PlayerProfileUpdatePayload
): Promise<PlayerProfile | null> {
  const response = await platformFetch(`${apiBaseUrl}/profiles/me/deadlock`, {
    method: "PUT",
    headers: {
      accept: "application/json",
      "content-type": "application/json"
    },
    credentials: "include",
    body: JSON.stringify(mapDeadlockProfileUpdatePayload(payload)),
    cache: "no-store"
  });
  if (!response.ok) {
    return null;
  }

  return getCurrentPlayerProfile();
}

export async function updateCurrentAccountProfile(payload: AccountProfileUpdatePayload): Promise<PlayerProfile> {
  const profile = await platformApiRequest<ApiPlayerProfile>("/profiles/me", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  return mapPlayerProfile(profile);
}

export async function uploadCurrentProfileAvatar(file: File): Promise<PlatformMediaAccepted> {
  const body = new FormData();
  body.append("file", file);
  return platformApiRequest<PlatformMediaAccepted>("/profiles/me/avatar", {
    method: "POST",
    body,
  });
}

export async function deleteCurrentProfileAvatar(): Promise<PlatformMediaDeleteAccepted> {
  return platformApiRequest<PlatformMediaDeleteAccepted>("/profiles/me/avatar", {
    method: "DELETE"
  });
}

export async function waitForOwnedMedia(
  assetId: string,
  options: {
    initialDelayMs?: number;
    maxAttempts?: number;
    maxDelayMs?: number;
    onStatus?: (descriptor: PlatformMediaDescriptor) => void;
    signal?: AbortSignal;
  } = {}
): Promise<PlatformMediaDescriptor> {
  const maxAttempts = Math.min(20, Math.max(1, options.maxAttempts ?? 10));
  const maxDelayMs = Math.min(4_000, Math.max(100, options.maxDelayMs ?? 2_000));
  let delayMs = Math.min(maxDelayMs, Math.max(0, options.initialDelayMs ?? 300));
  let descriptor: PlatformMediaDescriptor | null = null;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    descriptor = await platformApiRequest<PlatformMediaDescriptor>(
      `/media/${encodeURIComponent(assetId)}/status`,
      { signal: options.signal }
    );
    options.onStatus?.(descriptor);
    if (!["pending", "processing"].includes(descriptor.status) || attempt === maxAttempts - 1) {
      return descriptor;
    }
    await abortableDelay(delayMs, options.signal);
    delayMs = Math.min(maxDelayMs, Math.max(100, Math.round(delayMs * 1.6)));
  }

  if (!descriptor) {
    throw new PlatformApiError("Не удалось получить состояние изображения.", 503);
  }
  return descriptor;
}

function abortableDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(new DOMException("Request aborted.", "AbortError"));
  }
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(resolve, milliseconds);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Request aborted.", "AbortError"));
    }, { once: true });
  });
}

export async function updateCurrentAccountSecurity(
  payload: AccountSecurityUpdatePayload
): Promise<PlatformUser> {
  return platformApiRequest<PlatformUser>("/auth/account", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function getCurrentDreamSlots(): Promise<PlatformDeadlockDreamSlot[]> {
  return platformApiRequest<PlatformDeadlockDreamSlot[]>("/profiles/me/deadlock/dream-slots");
}

export async function updateCurrentDreamSlots(slots: PlatformDeadlockDreamSlot[]): Promise<PlatformDeadlockDreamSlot[]> {
  return platformApiRequest<PlatformDeadlockDreamSlot[]>("/profiles/me/deadlock/dream-slots", {
    method: "PUT",
    body: JSON.stringify({
      slots: slots.map((slot) => ({
        slot_number: slot.slot_number,
        allowed_roles: slot.allowed_roles,
        desired_heroes: slot.desired_heroes
      }))
    })
  });
}

export async function createTournament(
  _actorUserId: string,
  payload: TournamentCreatePayload
): Promise<{ slug: string } | null> {
  const tournament = await platformApiRequest<ApiTournamentDetail>("/tournaments", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json"
    },
    body: JSON.stringify(mapCreatePayload(payload)),
    cache: "no-store"
  });
  return { slug: tournament.slug };
}

export async function suggestTournamentInviteCode(): Promise<TournamentInviteCodeAvailability> {
  return platformApiRequest<TournamentInviteCodeAvailability>("/tournaments/invites/suggest-code");
}

export async function checkTournamentInviteCode(code: string): Promise<TournamentInviteCodeAvailability> {
  return platformApiRequest<TournamentInviteCodeAvailability>(
    `/tournaments/invites/code-status?code=${encodeURIComponent(code)}`
  );
}

export async function uploadTournamentBanner(slug: string, file: File): Promise<PlatformMediaAccepted> {
  const formData = new FormData();
  formData.append("file", file);

  return platformApiRequest<PlatformMediaAccepted>(`/tournaments/${encodeURIComponent(slug)}/banner`, {
    method: "POST",
    body: formData
  });
}

export async function deleteTournamentBanner(slug: string): Promise<PlatformMediaDeleteAccepted> {
  return platformApiRequest<PlatformMediaDeleteAccepted>(`/tournaments/${encodeURIComponent(slug)}/banner`, {
    method: "DELETE"
  });
}

export async function registerForTournament(
  slug: string
): Promise<Registration | null> {
  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/join`, {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json"
    },
    credentials: "include",
    body: JSON.stringify({ entry_type: "solo", team_name: null }),
    cache: "no-store"
  });
  if (!response.ok) {
    return null;
  }
  return mapRegistration(await response.json() as ApiRegistration);
}

export async function leaveTournament(slug: string): Promise<boolean> {
  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/join`, {
    method: "DELETE",
    headers: { accept: "application/json" },
    credentials: "include",
    cache: "no-store"
  });
  return response.ok || response.status === 404;
}

export async function setTournamentReadyCheckChoice(
  slug: string,
  choice: "yes" | "no"
): Promise<PlatformTournamentDeadlockReadyVote | null> {
  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/deadlock/ready-check/vote`, {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json"
    },
    credentials: "include",
    body: JSON.stringify({ choice }),
    cache: "no-store"
  });
  if (!response.ok) {
    return null;
  }
  return await response.json() as PlatformTournamentDeadlockReadyVote;
}

export async function getTournamentAutoAssignmentState(
  slug: string,
  requestHeaders: HeadersInit = {}
): Promise<PlatformTournamentDeadlockAutoAssignmentState | null> {
  const headers = new Headers(requestHeaders);
  headers.set("accept", "application/json");
  try {
    const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/deadlock/auto-assignment`, {
      headers,
      credentials: "include",
      cache: "no-store"
    });
    if (!response.ok) {
      return null;
    }
    return await response.json() as PlatformTournamentDeadlockAutoAssignmentState;
  } catch {
    return null;
  }
}

export async function queueTournamentAutoAssignmentRun(
  slug: string
): Promise<PlatformTournamentDeadlockAutoAssignmentJob | null> {
  try {
    const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/deadlock/auto-assignment/run-async`, {
      method: "POST",
      headers: { accept: "application/json" },
      credentials: "include",
      cache: "no-store"
    });
    if (!response.ok) {
      return null;
    }
    return await response.json() as PlatformTournamentDeadlockAutoAssignmentJob;
  } catch {
    return null;
  }
}

export async function getTournamentBracket(
  slug: string,
  requestHeaders: HeadersInit = {},
  options: {
    teamsView?: "summary" | "full";
    signal?: AbortSignal;
    ifNoneMatch?: string | null;
    cachedBracket?: Bracket | null;
    onResponse?: (response: Response) => void;
  } = {}
): Promise<Bracket | null> {
  const headers = new Headers(requestHeaders);
  headers.set("accept", "application/json");
  if (options.ifNoneMatch) {
    headers.set("if-none-match", options.ifNoneMatch);
  }
  try {
    const params = new URLSearchParams({
      teams_view: options.teamsView ?? "summary"
    });
    const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/bracket?${params.toString()}`, {
      headers,
      credentials: "include",
      cache: "no-store",
      signal: options.signal
    });
    options.onResponse?.(response);
    if (response.status === 304) {
      return options.cachedBracket ?? null;
    }
    if (!response.ok) {
      return null;
    }
    return mapBracket(await response.json() as ApiBracket);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    return null;
  }
}

export async function generateTournamentBracket(slug: string): Promise<Bracket | null> {
  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/matches/seed-opening-round`, {
    method: "POST",
    headers: { accept: "application/json" },
    credentials: "include",
    cache: "no-store"
  });
  if (!response.ok) {
    return null;
  }
  return getTournamentBracket(slug);
}

export async function resetTournamentBracket(slug: string): Promise<Bracket | null> {
  const response = await platformFetch(`${apiBaseUrl}/tournaments/${slug}/bracket`, {
    method: "DELETE",
    headers: { accept: "application/json" },
    credentials: "include",
    cache: "no-store"
  });
  if (!response.ok) {
    return null;
  }
  return mapBracket(await response.json() as ApiBracket);
}

function mapTournamentSummary(item: ApiTournamentListItem): TournamentSummary {
  const status = mapStatus(item.status);
  const registrationClosesAt = item.registration_closes_at ?? item.ready_check_starts_at ?? item.captain_selection_starts_at ?? item.starts_at ?? null;

  return {
    id: item.id,
    slug: item.slug,
    title: item.name,
    organizerUserId: item.organizer_user_id,
    organizerName: item.organizer_display_name ?? "Организатор",
    organizerAvatarUrl: item.organizer_avatar_url ?? null,
    organizerAvatarMedia: item.organizer_avatar_media ?? null,
    coverUrl: item.cover_url ?? null,
    coverMedia: item.cover_media ?? null,
    startsAtIso: item.starts_at ?? null,
    registrationClosesAtIso: registrationClosesAt,
    startsAtLabel: formatStartsAt(item.starts_at ?? item.registration_starts_at ?? null),
    registrationTimerLabel: registrationTimerLabel(status),
    startTimerLabel: formatStartTimerLabel(item.starts_at),
    status,
    statusLabel: statusLabel(status),
    visibility: normalizeVisibility(item.visibility),
    bracketType: "single_elimination",
    theme: "theme-teal",
    allowedRanks: normalizeAllowedRanks(item.allowed_ranks ?? []),
    participantCount: item.participant_count ?? 0,
    maxParticipants: item.max_participants ?? null,
    teamsCount: item.teams_count ?? 128,
    currentUserParticipantStatus: item.current_user_participant_status ?? null,
    currentUserHasInviteAccess: Boolean(item.current_user_has_invite_access),
    stateVersion: item.state_version ?? null
  };
}

function mapTournamentDetail(
  item: ApiTournamentDetail,
  registrations: Registration[],
  bracket: Bracket | null = null,
  workflow: {
    serverTime: string;
    readyCheckState?: PlatformTournamentDeadlockReadyCheckState | null;
    deadlockAssignment?: PlatformTournamentDeadlockAutoAssignmentState["published_run"] | null;
    activeCommitment?: TournamentDetail["activeCommitment"];
  }
): TournamentDetail {
  const mappedBracket = bracket ?? emptyBracket(item.id, mapStatus(item.status));
  const teams = mappedBracket.teams;

  return {
    ...mapTournamentSummary(item),
    serverTime: workflow.serverTime,
    description: item.description ?? "",
    visibility: normalizeVisibility(item.visibility),
    bracketType: "single_elimination",
    matchFormat: item.match_format ?? "bo1",
    finalFormat: item.final_format ?? "bo3",
    participantMode: item.format_slug,
    schedule: mapSchedule(item),
    registrations,
    teams,
    bracket: { ...mappedBracket, teams },
    readyCheckState: workflow.readyCheckState ?? null,
    deadlockAssignment: workflow.deadlockAssignment ?? null,
    activeCommitment: workflow.activeCommitment ?? null
  };
}

function mapPlayerProfile(item: ApiPlayerProfile, deadlockProfile: ApiDeadlockProfile | null = null): PlayerProfile {
  const id = item.id ?? item.user_id ?? deadlockProfile?.user_id ?? "";
  const displayName = item.display_name ?? item.handle ?? id;
  const handle = item.handle ?? displayName;
  const heroes = deadlockProfile?.pool ?? item.hero_pool ?? item.pool ?? [];
  const availableHeroNames = [...deadlockHeroes, ...heroes.filter((hero) => !deadlockHeroes.includes(hero as (typeof deadlockHeroes)[number]))];
  const rank = deadlockProfile?.rank ?? item.rank ?? "";
  const subrank = deadlockProfile?.subrank ?? item.subrank;
  const hoursRange = deadlockProfile?.playtime ?? item.hours_range ?? item.playtime ?? "";
  const roles = deadlockProfile?.roles ?? item.roles ?? [];
  const completionPercent = item.completion_percent ?? calculateProfileCompletion({
    handle,
    rank,
    subrank,
    hoursRange,
    roles,
    heroes
  });

  return {
    id,
    displayName,
    handle,
    avatarUrl: item.avatar_url ?? null,
    avatarMedia: item.avatar_media ?? null,
    bannerUrl: item.banner_url ?? null,
    bannerMedia: item.banner_media ?? null,
    accountEmail: item.account_email ?? "",
    rank,
    subrank: subrank == null ? "" : romanSubrank(Number(subrank)),
    hoursRange,
    roles,
    heroes,
    heroPool: availableHeroNames.map((hero) => ({ name: hero, theme: "h-blue" })),
    completionPercent,
    teamName: item.captain_team_name ?? "",
    teammatePreferences: [],
    contacts: [
      { label: "Почта", value: item.contact_email ?? item.account_email ?? "" },
      { label: "Discord", value: item.discord_account ?? "" },
      { label: "Steam ID", value: item.steam_id ?? "" },
      { label: "Регион", value: item.region ?? "" }
    ]
  };
}

function mapStatsOverview(item: ApiStatsOverview): PlatformStatsOverview {
  return {
    total_tournaments: numericStat(item.total_tournaments),
    completed_tournaments: numericStat(item.completed_tournaments),
    active_upcoming_tournaments: numericStat(item.active_upcoming_tournaments),
    registered_participants: numericStat(item.registered_participants),
    completed_matches: numericStat(item.completed_matches),
    deadlock_profiles_total: numericStat(item.deadlock_profiles_total),
    registered_participants_with_deadlock_profile: numericStat(item.registered_participants_with_deadlock_profile),
    deadlock_profile_coverage_percent: numericStat(item.deadlock_profile_coverage_percent),
    deadlock_rank_distribution: normalizeRankDistribution(item.deadlock_rank_distribution)
  };
}

function numericStat(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

function normalizeRankDistribution(rawValue: unknown): PlatformStatsRankBucket[] {
  if (Array.isArray(rawValue)) {
    return rawValue
      .map((item) => {
        if (!item || typeof item !== "object") {
          return null;
        }
        const bucket = item as { rank?: unknown; rank_code?: unknown; count?: unknown; total?: unknown };
        const rank = typeof bucket.rank === "string"
          ? bucket.rank
          : typeof bucket.rank_code === "string"
            ? bucket.rank_code
            : "";
        const count = numericStat(bucket.count ?? bucket.total);
        return rank ? { rank, count } : null;
      })
      .filter((item): item is PlatformStatsRankBucket => item !== null);
  }

  if (rawValue && typeof rawValue === "object") {
    return Object.entries(rawValue as Record<string, unknown>).map(([rank, count]) => ({
      rank,
      count: numericStat(count)
    }));
  }

  return [];
}

function mapDeadlockProfileUpdatePayload(payload: PlayerProfileUpdatePayload): ApiDeadlockProfileUpdate {
  return {
    rank: payload.rank ?? "",
    subrank: numericSubrank(payload.subrank),
    playtime: payload.hours_range ?? "",
    roles: payload.roles ?? [],
    pool: payload.hero_pool ?? [],
    captain_priority: normalizeCaptainPriority(payload.captain_preference)
  };
}

function numericSubrank(subrank: string | null | undefined): number {
  const value = (subrank ?? "").trim();
  const roman: Record<string, number> = {
    I: 1,
    II: 2,
    III: 3,
    IV: 4,
    V: 5,
    VI: 6
  };
  return roman[value] ?? (Number(value) || 1);
}

function romanSubrank(subrank: number): string {
  return ["", "I", "II", "III", "IV", "V", "VI"][subrank] ?? String(subrank);
}

function normalizeCaptainPriority(value: Record<string, unknown> | undefined): string {
  const raw = String(value?.captain_probability ?? "Нейтрально");
  if (raw === "Повысить") {
    return "yes";
  }
  if (raw === "Понизить") {
    return "no";
  }
  return "neutral";
}

function calculateProfileCompletion(profile: {
  handle: string;
  rank: string;
  subrank: string | number | null | undefined;
  hoursRange: string;
  roles: string[];
  heroes: string[];
}): number {
  const checks = [
    Boolean(profile.handle),
    Boolean(profile.rank),
    Boolean(profile.subrank),
    Boolean(profile.hoursRange),
    profile.roles.length > 0,
    profile.heroes.length > 0
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}

function mapSchedule(item: ApiTournamentListItem): TournamentSchedule | null {
  const registrationStartsAt = item.registration_starts_at ?? "";
  const registrationClosesAt = item.registration_closes_at ?? item.ready_check_starts_at ?? "";
  const checkInStartsAt = item.ready_check_starts_at ?? "";
  const checkInEndsAt = item.ready_check_ends_at ?? item.captain_selection_starts_at ?? "";
  const teamsFormAt = item.captain_selection_starts_at ?? "";
  const startsAt = item.starts_at ?? "";

  if (!registrationStartsAt && !registrationClosesAt && !checkInStartsAt && !checkInEndsAt && !teamsFormAt && !startsAt) {
    return null;
  }

  return {
    registrationStartsAt,
    registrationClosesAt,
    checkInStartsAt,
    checkInEndsAt,
    teamsFormAt,
    startsAt,
    timezone: "Europe/Moscow"
  };
}

function mapRegistration(item: ApiRegistration): Registration {
  return {
    id: item.id,
    userId: item.user_id,
    displayName: item.display_name,
    entryType: item.entry_type,
    teamName: item.team_name ?? null,
    status: item.status,
    checkInStatus: item.status === "checked_in" ? "checked_in" : "pending",
    registeredAt: item.created_at,
    checkedInAt: null
  };
}

function dedupeRegistrations(registrations: Registration[]): Registration[] {
  const seen = new Set<string>();
  return registrations.filter((registration) => {
    if (seen.has(registration.id)) {
      return false;
    }
    seen.add(registration.id);
    return true;
  });
}

function mapTeam(item: ApiTeam): Team {
  return {
    id: item.id,
    name: item.name ?? "Команда",
    seed: item.seed ?? null,
    starterStrength: item.starter_strength ?? null,
    starterAverageStrength: item.starter_average_strength ?? null,
    captainId: item.captain_id ?? null,
    color: item.color ?? null,
    emblem: item.emblem ?? null,
    members: (item.members ?? []).map(mapTeamMember)
  };
}

function mapTeamMember(item: ApiTeamMember): TeamMember {
  return {
    userId: item.user_id,
    handle: item.handle ?? item.display_name ?? "Игрок",
    avatarUrl: item.avatar_url ?? null,
    rank: item.rank ?? null,
    subrank: item.subrank == null ? "" : romanSubrank(item.subrank),
    isCaptain: Boolean(item.is_captain),
    isSubstitute: Boolean(item.is_substitute)
  };
}

function mapMatch(item: ApiMatch): Match {
  const teamAId = item.team_a_id ?? teamIdFromMatchLabel(item.home_label);
  const teamBId = item.team_b_id ?? teamIdFromMatchLabel(item.away_label);
  const winnerTeamId = item.winner_team_id
    ?? (item.winner_side === "home" ? teamAId : item.winner_side === "away" ? teamBId : null);
  return {
    id: item.id,
    roundNumber: item.round_number ?? 1,
    matchOrder: item.match_order ?? item.sequence_number ?? 1,
    teamAId,
    teamBId,
    homeLabel: item.home_label ?? "TBD",
    awayLabel: item.away_label ?? "TBD",
    scoreA: item.score_a ?? item.home_score ?? null,
    scoreB: item.score_b ?? item.away_score ?? null,
    winnerTeamId,
    homeSourceMatchId: item.home_source_match_id ?? null,
    awaySourceMatchId: item.away_source_match_id ?? null,
    status: item.status ?? "scheduled",
    matchFormat: item.match_format ?? "bo1",
    ready: Boolean(item.ready ?? (teamAId && teamBId)),
    scheduledAt: item.scheduled_at ?? null
  };
}

function teamIdFromMatchLabel(label: string | null | undefined): string | null {
  const normalized = (label ?? "").trim();
  if (!normalized.startsWith("Team ")) {
    return null;
  }
  const teamId = normalized.slice("Team ".length).trim();
  return teamId || null;
}

function mapBracket(item: ApiBracket): Bracket {
  const tournamentStatus = mapStatus(item.tournament_status);
  const canManage = Boolean(item.capabilities?.can_manage ?? item.can_manage);
  const terminal = tournamentStatus === "completed" || tournamentStatus === "cancelled";
  const canScheduleMatches = Boolean(
    item.capabilities?.can_schedule_matches
      ?? (canManage && !terminal),
  );
  const canReportMatches = Boolean(
    item.capabilities?.can_report_matches
      ?? (canManage && !terminal && (
        tournamentStatus === "registration_closed" || tournamentStatus === "in_progress"
      )),
  );
  return {
    tournamentId: item.tournament_id ?? "",
    status: mapBracketStatus(item.status),
    tournamentStatus,
    revision: item.revision ?? 0,
    capabilities: {
      canManage,
      canScheduleMatches,
      canReportMatches,
    },
    canManage,
    teams: (item.teams ?? []).map(mapTeam),
    matches: (item.matches ?? []).map(mapMatch),
  };
}

function emptyBracket(tournamentId: string, tournamentStatus: TournamentStatus): Bracket {
  return {
    tournamentId,
    status: "pending",
    tournamentStatus,
    revision: 0,
    capabilities: {
      canManage: false,
      canScheduleMatches: false,
      canReportMatches: false,
    },
    canManage: false,
    teams: [],
    matches: [],
  };
}

function mapBracketStatus(status: string | null | undefined): Bracket["status"] {
  switch (status) {
    case "teams_ready":
    case "ready":
    case "pending":
      return status;
    default:
      return "pending";
  }
}

function mapStatus(status: string | null | undefined): TournamentStatus {
  switch (status) {
    case "registration_open":
    case "registration_closed":
    case "in_progress":
    case "completed":
    case "cancelled":
      return status;
    default:
      return "registration_closed";
  }
}

function formatStartsAt(value: string | null | undefined): string {
  if (!value) {
    return "Дата уточняется";
  }

  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "Дата уточняется";
  }

  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short"
  }).format(date);
}

function formatStartTimerLabel(value: string | null | undefined): string {
  if (!value) {
    return "Дата старта уточняется";
  }

  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "Дата старта уточняется";
  }

  return "Старт через";
}

function normalizeAllowedRanks(values: string[]): string[] {
  const normalized = values.map(normalizeRankCode).filter(Boolean);
  return normalized.length > 0
    ? sortRanksByStrengthDesc([...new Set(normalized)])
    : ranks.map((rank) => rank.code);
}

function normalizeRankCode(value: string): string {
  return ranks.find((rank) => rank.code === value || rank.label === value)?.code ?? "";
}

function mapCreatePayload(payload: TournamentCreatePayload) {
  const teamsFormAt = payload.schedule.teams_form_at || null;

  return {
    name: payload.title.trim(),
    description: payload.description.trim() || null,
    cover_url: payload.cover_url?.trim() || null,
    visibility: payload.visibility === "private" ? "invite_only" : "public",
    invite_code: payload.invite_code?.trim() || null,
    format_slug: "solo",
    allowed_ranks: sortRanksByStrengthDesc(payload.allowed_rank_codes).map(rankCodeToApiName),
    max_participants: payload.max_participants ?? null,
    registration_starts_at: payload.schedule.registration_starts_at || null,
    registration_closes_at: payload.schedule.registration_closes_at || null,
    ready_check_starts_at: payload.schedule.check_in_starts_at || payload.schedule.registration_closes_at || null,
    ready_check_ends_at: teamsFormAt,
    captain_selection_starts_at: teamsFormAt,
    starts_at: payload.starts_at || null,
    match_format: payload.match_format || "bo1",
    final_format: payload.final_format || "bo3",
    captain_response_deadline_minutes: 10,
    teams_count: payload.teams_count
  };
}

function rankCodeToApiName(value: string): string {
  return ranks.find((rank) => rank.code === value)?.label ?? value;
}
