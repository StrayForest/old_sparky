import { createServer } from "node:http";

const host = process.env.MOCK_PLATFORM_API_HOST ?? "127.0.0.1";
const port = Number(process.env.MOCK_PLATFORM_API_PORT ?? 3199);
const now = "2026-06-07T12:00:00Z";
const mockCsrfToken = `mock.${"c".repeat(64)}`;
let mockMediaSequence = 0;
const mockMediaAssets = new Map();
const proofRefreshAgendaCalls = new Map();
const deadlockHeroNames = [
  "Abrams", "Apollo", "Bebop", "Billy", "Calico", "Celeste", "The Doorman", "Drifter",
  "Dynamo", "Graves", "Grey Talon", "Haze", "Holliday", "Infernus", "Ivy", "Kelvin",
  "Lady Geist", "Lash", "McGinnis", "Mina", "Mirage", "Mo & Krill", "Paradox", "Paige",
  "Pocket", "Rem", "Seven", "Shiv", "Silver", "Sinclair", "Venator", "Victor", "Vindicta",
  "Viscous", "Vyper", "Warden", "Wraith", "Yamato", "Source New Hero"
];
const deadlockRankNames = [
  "Eternus", "Ascendant", "Phantom", "Oracle", "Emissary", "Ritualist", "Mystic",
  "Sentinel", "Acolyte", "Seeker", "Initiate"
];

const tournaments = [
  {
    id: "t_night_veil_5",
    slug: "night-veil-open-5",
    name: "Night Veil Open #5",
    description: "Night Veil Open #5 - турнир Deadlock для игроков платформы.\nРегистрируйтесь, подтверждайте участие и следите за сеткой.",
    cover_url: null,
    visibility: "public",
    status: "registration_open",
    format_slug: "solo",
    organizer_user_id: "u_night_veil",
    organizer_display_name: "Night Veil Esports",
    participant_count: 32,
    allowed_ranks: ["Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist", "Emissary", "Oracle", "Phantom", "Ascendant"],
    max_participants: 64,
    teams_count: 8,
    current_user_participant_status: null,
    current_user_has_invite_access: false,
    registration_starts_at: "2026-06-01T15:00:00Z",
    registration_closes_at: "2026-06-07T15:30:00Z",
    ready_check_starts_at: "2026-06-07T15:30:00Z",
    ready_check_ends_at: "2026-06-07T16:00:00Z",
    captain_selection_starts_at: "2026-06-07T16:00:00Z",
    starts_at: "2026-06-07T17:30:00Z",
    match_format: "bo3",
    final_format: "bo3",
    created_at: now,
    available_next_statuses: ["registration_closed", "cancelled"]
  },
  {
    id: "t_citadel",
    slug: "citadel-clash-3",
    name: "Citadel Clash #3",
    description: "Citadel Clash #3 - турнир Deadlock для игроков платформы.",
    cover_url: null,
    visibility: "public",
    status: "registration_closed",
    format_slug: "solo",
    organizer_user_id: "u_citadel",
    organizer_display_name: "Citadel Esports",
    participant_count: 128,
    allowed_ranks: ["Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist", "Emissary", "Oracle", "Ascendant"],
    max_participants: 128,
    teams_count: 16,
    current_user_participant_status: null,
    current_user_has_invite_access: false,
    registration_starts_at: "2026-05-10T14:00:00Z",
    registration_closes_at: "2026-05-20T12:00:00Z",
    ready_check_starts_at: "2026-05-20T12:00:00Z",
    ready_check_ends_at: "2026-05-20T12:30:00Z",
    captain_selection_starts_at: "2026-05-20T12:30:00Z",
    starts_at: "2026-05-20T14:00:00Z",
    match_format: "bo3",
    final_format: "bo3",
    created_at: now,
    available_next_statuses: ["registration_open", "in_progress", "cancelled"]
  }
];

const bracketManagerTournament = {
  ...tournaments[0],
  id: "t_bracket_manager_smoke",
  slug: "bracket-manager-smoke",
  name: "Bracket Manager Smoke",
  status: "registration_closed"
};

const teams = [
  {
    id: "1",
    name: "Aqua Wardens",
    seed: 1,
    starter_strength: 6827.0,
    starter_average_strength: 6827.0,
    captain_id: "u_lisalexy",
    color: null,
    emblem: null,
    members: [
      { user_id: "u_lisalexy", handle: "lisalexy", avatar_url: "/assets/heroes/Abrams.png", rank: "Eternus", subrank: 6, role: "Carry", is_captain: true, is_substitute: false },
      { user_id: "u_mid", handle: "Midnight", avatar_url: "/assets/heroes/Haze.png", rank: "Phantom", subrank: 4, role: "Semi-Carry", is_captain: false, is_substitute: false },
      { user_id: "u_void", handle: "VoidRider", avatar_url: "/assets/heroes/Seven.png", rank: "Oracle", subrank: 2, role: "Support", is_captain: false, is_substitute: true }
    ]
  },
  {
    id: "2",
    name: "Neon Revenants",
    seed: 8,
    starter_strength: 6316.0,
    starter_average_strength: 6316.0,
    captain_id: "u_shadow",
    color: null,
    emblem: null,
    members: [
      { user_id: "u_shadow", handle: "ShadowHawk", avatar_url: "/assets/heroes/Kelvin.png", rank: "Oracle", subrank: 5, role: "Carry", is_captain: true, is_substitute: false },
      { user_id: "u_echo", handle: "EchoBlade", avatar_url: null, rank: "Emissary", subrank: 3, role: "Semi-Support", is_captain: false, is_substitute: false }
    ]
  }
];

const profile = {
  user_id: "u_lisalexy",
  display_name: "lisalexy",
  handle: "lisalexy",
  avatar_url: null,
  banner_url: null,
  avatar_media: null,
  banner_media: null,
  bio: null,
  contact_email: "player@example.com",
  region: "Finland, Kannus",
  steam_id: "76561198000000000",
  discord_account: "lisalexy#4821",
  captain_team_name: "OldTeam",
  deadlock_profile: {
    user_id: "u_lisalexy",
    rank: "Eternus",
    subrank: 6,
    playtime: "3000+",
    roles: ["Carry", "Semi-Carry", "Support"],
    pool: ["Abrams", "Apollo", "Bebop"],
    captain_priority: "neutral",
    updated_at: now
  }
};

function json(response, status, payload, headers = {}) {
  response.writeHead(status, {
    "content-type": "application/json",
    ...headers
  });
  response.end(JSON.stringify(payload));
}

function authenticated(request) {
  return (request.headers.cookie ?? "").includes("deadlock_platform_session=");
}

function actorForRequest(request) {
  const cookie = request.headers.cookie ?? "";
  if (cookie.includes("steam-only-smoke=1")) {
    return {
      id: "u_steam_only",
      email: null,
      display_name: "SteamPlayer",
      roles: ["authenticated_user", "player"],
      steam_id: "76561198999999999",
      has_password: false
    };
  }
  if (cookie.includes("password-only-smoke=1")) {
    return {
      id: "u_password_only",
      email: "password@example.test",
      display_name: "PasswordPlayer",
      roles: ["authenticated_user", "player"],
      has_password: true
    };
  }
  if (cookie.includes("limited-creator-smoke=1")) {
    return {
      id: "u_limited_creator",
      email: "limited@example.test",
      display_name: "LimitedCreator",
      roles: ["authenticated_user", "player"]
    };
  }
  if (cookie.includes("team-unassigned-smoke=1")) {
    return {
      id: "u_unassigned",
      email: "bench@example.test",
      display_name: "BenchPlayer",
      roles: ["authenticated_user", "player"]
    };
  }
  if (cookie.includes("commitment-blocked-smoke=1")) {
    return {
      id: "u_commitment_blocked",
      email: "committed@example.test",
      display_name: "CommittedPlayer",
      roles: ["authenticated_user", "player"]
    };
  }
  if (authenticated(request)) {
    return {
      id: "u_lisalexy",
      email: "player@example.com",
      display_name: "lisalexy",
      roles: ["authenticated_user", "player", "admin", "superadmin"]
    };
  }
  return null;
}

function participantStatusForRequest(request) {
  const cookie = request.headers.cookie ?? "";
  if (cookie.includes("registration-smoke=1")) {
    return null;
  }
  return actorForRequest(request) ? "registered" : null;
}

function tournamentPage(url) {
  const status = url.searchParams.get("status");
  const search = (url.searchParams.get("search") ?? "").trim().toLowerCase();
  const ranks = url.searchParams.getAll("rank");
  const dateSort = url.searchParams.get("date_sort");
  const limit = Math.max(1, Number(url.searchParams.get("limit") ?? 9));
  const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0));
  let items = tournaments.filter((tournament) => (
    (!status || tournament.status === status)
    && (!search || `${tournament.name} ${tournament.organizer_display_name}`.toLowerCase().includes(search))
    && (!ranks.length || ranks.some((rank) => tournament.allowed_ranks.includes(rank)))
  ));
  if (dateSort === "nearest" || dateSort === "farthest") {
    items = [...items].sort((left, right) => {
      const order = Date.parse(left.starts_at) - Date.parse(right.starts_at);
      return dateSort === "nearest" ? order : -order;
    });
  }
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset };
}

function bracketPayload(slug, canManage = false, includeTeams = true) {
  const tournamentStatus = tournaments.find((item) => item.slug === slug)?.status
    ?? bracketManagerTournament.status;
  const terminal = tournamentStatus === "completed" || tournamentStatus === "cancelled";
  const matches = ["night-veil-open-5", bracketManagerTournament.slug].includes(slug) ? [{
    id: "m_night_final",
    round_number: 1,
    match_order: 1,
    sequence_number: 1,
    team_a_id: "1",
    team_b_id: "2",
    home_label: "Aqua Wardens",
    away_label: "Neon Revenants",
    score_a: 0,
    score_b: 1,
    home_score: 0,
    away_score: 1,
    winner_team_id: "2",
    winner_side: "away",
    home_source_match_id: null,
    away_source_match_id: null,
    status: "scheduled",
    match_format: "bo3",
    ready: true,
    scheduled_at: null
  }] : [];
  if (slug === bracketManagerTournament.slug) {
    matches.push(
      {
        ...matches[0],
        id: "m_night_second",
        match_order: 2,
        sequence_number: 2
      },
      {
        ...matches[0],
        id: "m_night_grand_final",
        round_number: 2,
        match_order: 1,
        sequence_number: 3,
        team_a_id: null,
        team_b_id: null,
        home_label: "Победитель четвертьфинала 1",
        away_label: "Победитель четвертьфинала 2",
        home_source_match_id: "m_night_final",
        away_source_match_id: "m_night_second",
        ready: false
      }
    );
  }
  return {
    tournament_id: slug === bracketManagerTournament.slug
      ? bracketManagerTournament.id
      : tournaments.find((item) => item.slug === slug)?.id ?? "",
    tournament_status: tournamentStatus,
    status: ["night-veil-open-5", bracketManagerTournament.slug].includes(slug) ? "ready" : "pending",
    revision: 0,
    can_manage: canManage,
    capabilities: {
      can_manage: canManage && !terminal,
      can_schedule_matches: canManage && !terminal,
      can_report_matches: canManage && !terminal
        && (tournamentStatus === "registration_closed" || tournamentStatus === "in_progress")
    },
    teams: includeTeams ? teams : [],
    matches,
    next_poll_after_ms: 3_000,
    state_version: 0,
    bracket_probe_ticket: "mock-bracket-probe"
  };
}

function readyPayload(tournament, request) {
  const suppressInitialActiveState = (request?.headers.cookie ?? "")
    .includes("ready-check-provider-resync-no-activation-smoke=1")
    || (request?.headers.cookie ?? "").includes("ready-check-provider-resync-recovery-smoke=1");
  return {
    active_round: tournament?.status === "registration_open" && !suppressInitialActiveState ? {
      id: 1,
      tournament_id: tournament.id,
      status: "active",
      eligible_participant_count: tournament.participant_count,
      ready_count: 0,
      declined_count: 0,
      initiated_by_user_id: null,
      created_at: now,
      closed_at: null,
      current_user_choice: null
    } : null,
    latest_round: null
  };
}

const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://${host}:${port}`);
  const path = url.pathname.replace(/\/$/, "") || "/";

  if (path === "/api/v1/health/live" || path === "/api/v1/health/ready") {
    json(response, 200, { status: "ok", service: "platform-api-mock" });
    return;
  }
  if (path === "/api/v1/auth/security-config") {
    json(response, 200, {
      public_registration_enabled: true,
      email_verification_required: true,
      turnstile_mode: "off",
      turnstile_site_key: null,
      steam_login_enabled: true
    }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/auth/csrf") {
    json(
      response,
      authenticated(request) ? 200 : 401,
      authenticated(request) ? { csrf_token: mockCsrfToken } : { detail: "Not authenticated." },
      { "cache-control": "no-store" }
    );
    return;
  }
  if (path === "/api/v1/ready-check/agenda" && request.method === "GET") {
    const cookie = request.headers.cookie ?? "";
    const hasProofRefreshCookie = Boolean(cookie.match(/(?:^|;\s*)ready-check-provider-proof-refresh-smoke=/));
    const mode = cookie.includes("ready-check-provider-sse-smoke=1")
      || cookie.includes("ready-check-provider-stalled-smoke=1")
      || cookie.includes("ready-check-provider-future-gap-smoke=1")
      || cookie.includes("ready-check-provider-resync-no-activation-smoke=1")
      || cookie.includes("ready-check-provider-resync-recovery-smoke=1")
      || hasProofRefreshCookie
      ? "scheduled_sse"
      : cookie.includes("ready-check-provider-polling-smoke=1")
        ? "polling"
        : null;
    const futureGap = cookie.includes("ready-check-provider-future-gap-smoke=1");
    const stalled = cookie.includes("ready-check-provider-stalled-smoke=1");
    const proofRefreshScope = cookie.match(/(?:^|;\s*)ready-check-provider-proof-refresh-smoke=([^;]+)/)?.[1] ?? "default";
    const proofRefresh = Boolean(cookie.match(/(?:^|;\s*)ready-check-provider-proof-refresh-smoke=/));
    if (proofRefresh) {
      proofRefreshAgendaCalls.set(
        proofRefreshScope,
        (proofRefreshAgendaCalls.get(proofRefreshScope) ?? 0) + 1,
      );
    }
    const checks = futureGap ? [
      {
        tournament_id: "t_night_veil_5",
        slug: "night-veil-open-5",
        ready_check_starts_at: "2026-06-07T15:30:00Z",
        ready_check_ends_at: "2026-06-07T15:36:00Z",
        admission_open_at: "2026-06-07T15:29:00Z",
        admission_priority: "scheduled",
        admission_mode: mode,
        state_ticket: "mock-ready-check-state-a"
      },
      {
        tournament_id: "t_citadel",
        slug: "citadel-clash-3",
        ready_check_starts_at: "2026-06-07T15:40:00Z",
        ready_check_ends_at: "2026-06-07T16:00:00Z",
        admission_open_at: "2026-06-07T15:39:00Z",
        admission_priority: "scheduled",
        admission_mode: mode,
        state_ticket: "mock-ready-check-state-b"
      }
    ] : mode ? [{
      tournament_id: "t_night_veil_5",
      slug: "night-veil-open-5",
      ready_check_starts_at: stalled ? "2026-06-07T17:00:00Z" : "2026-06-07T15:30:00Z",
      ready_check_ends_at: stalled ? "2026-06-07T17:30:00Z" : "2026-06-07T16:00:00Z",
      admission_open_at: "2026-06-07T15:29:00Z",
      admission_priority: "scheduled",
      admission_mode: mode,
      state_ticket: "mock-ready-check-state"
    }] : [];
    json(response, 200, {
      checks,
      sse_ticket: mode === "scheduled_sse" ? "mock-ready-check-stream" : null,
      sse_ticket_expires_at: proofRefresh
        ? (proofRefreshAgendaCalls.get(proofRefreshScope) ?? 0) > 1
          ? "2026-06-07T16:45:00Z"
          : "2026-06-07T15:45:00Z"
        : null
    }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/ready-check/state" && request.method === "GET") {
    const cookie = request.headers.cookie ?? "";
    if (cookie.includes("ready-check-provider-future-gap-smoke=1")) {
      const slug = url.searchParams.get("slug");
      json(response, 200, {
        revision: 1,
        status: slug === "night-veil-open-5" ? "active" : "waiting"
      });
      return;
    }
    if (cookie.includes("ready-check-provider-resync-no-activation-smoke=1")) {
      json(response, 200, { revision: 1, status: "waiting" });
      return;
    }
    if (cookie.includes("ready-check-provider-resync-recovery-smoke=1")) {
      json(response, 200, { revision: 2, status: "active" });
      return;
    }
    if (cookie.includes("ready-check-provider-")) {
      json(response, 503, { detail: "Mock Ready Check state unavailable." });
      return;
    }
  }
  if (path === "/api/v1/ready-check/events" && request.method === "GET") {
    const cookie = request.headers.cookie ?? "";
    if (cookie.includes("ready-check-provider-stalled-smoke=1")) {
      request.on("close", () => response.end());
      return;
    }
    if (
      cookie.includes("ready-check-provider-sse-smoke=1")
      || cookie.includes("ready-check-provider-proof-refresh-smoke=")
    ) {
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive"
      });
      response.write("retry: 5000\nevent: connected\ndata: {}\n\n");
      request.on("close", () => response.end());
      return;
    }
    if (
      cookie.includes("ready-check-provider-future-gap-smoke=1")
      || cookie.includes("ready-check-provider-resync-no-activation-smoke=1")
      || cookie.includes("ready-check-provider-resync-recovery-smoke=1")
    ) {
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive"
      });
      response.write("retry: 5000\nevent: connected\ndata: {}\n\n");
      if (cookie.includes("ready-check-provider-resync")) {
        response.write("event: resync\ndata: {\"reason\":\"relay_gap\"}\n\n");
        setTimeout(() => response.end(), 100);
      } else {
        response.write("event: ready_check\ndata: {\"tournament_id\":\"t_night_veil_5\",\"revision\":1,\"status\":\"active\"}\n\n");
      }
      return;
    }
  }
  if (path === "/api/v1/auth/password-reset/request" && request.method === "POST") {
    json(response, 202, { accepted: true }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/auth/password-reset/confirm" && request.method === "POST") {
    json(response, 200, { accepted: true }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/auth/email-verification/resend" && request.method === "POST") {
    json(response, 202, { accepted: true }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/auth/email-verification/confirm" && request.method === "POST") {
    json(response, 200, {
      user: {
        id: "u_lisalexy",
        email: "player@example.com",
        display_name: "lisalexy",
        status: "active",
        created_at: now,
        roles: ["authenticated_user", "player"],
        can_create_public_tournaments: false
      },
      expires_at: "2026-06-08T12:00:00Z"
    }, { "cache-control": "no-store" });
    return;
  }
  if (
    (path === "/api/v1/auth/email-link/request" || path === "/api/v1/auth/email-link/resend")
    && request.method === "POST"
  ) {
    json(response, 202, { accepted: true, retry_after_seconds: 60 }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/auth/email-link/confirm" && request.method === "POST") {
    json(response, 200, {
      id: "u_steam_only",
      email: "linked@example.test",
      display_name: "SteamPlayer",
      status: "active",
      created_at: now,
      roles: ["authenticated_user", "player"],
      can_create_public_tournaments: false,
      steam_id: "76561198999999999",
      steam_linked: true,
      has_password: false
    }, { "cache-control": "no-store" });
    return;
  }
  if (path === "/api/v1/content/game-assets") {
    json(response, 200, {
      heroes: deadlockHeroNames.map((name) => ({
        name,
        image_url: `/api/v1/content/game-assets/heroes/${encodeURIComponent(name)}.png`,
        source_available: true
      })),
      ranks: deadlockRankNames.map((name) => ({
        name,
        image_url: `/api/v1/content/game-assets/ranks/${encodeURIComponent(name)}.webp`,
        source_available: true
      }))
    });
    return;
  }
  const heroImageMatch = path.match(/^\/api\/v1\/content\/game-assets\/heroes\/(.+)\.png$/);
  if (heroImageMatch) {
    const heroName = decodeURIComponent(heroImageMatch[1]);
    const localName = heroName === "Source New Hero"
      ? "placeholder.svg"
      : `${heroName.replaceAll(" ", "_")}.png`;
    response.writeHead(307, { location: `/assets/heroes/${encodeURIComponent(localName)}` });
    response.end();
    return;
  }
  const rankImageMatch = path.match(/^\/api\/v1\/content\/game-assets\/ranks\/(.+)\.webp$/);
  if (rankImageMatch) {
    const rankName = decodeURIComponent(rankImageMatch[1]);
    const localName = deadlockRankNames.includes(rankName) ? rankName : "Initiate";
    response.writeHead(307, { location: `/assets/ranks/${localName}.webp` });
    response.end();
    return;
  }
  if (path === "/api/v1/content/home") {
    json(response, 200, {
      patches: Array.from({ length: 4 }, (_, index) => ({
        id: index === 0 ? "1836506165584438" : `183650616558443${index}`,
        title: `Minor Update - 07-${String(9 - index).padStart(2, "0")}-2026`,
        excerpt: "Обновлены игровые системы и баланс героев.",
        published_at: `2026-07-${String(9 - index).padStart(2, "0")}T12:00:00Z`,
        url: `https://store.steampowered.com/news/app/1422450/view/183650616558443${index}`
      })),
      videos: Array.from({ length: 4 }, (_, index) => ({
        id: `video00000${index}`,
        title: `Old Sparky Deadlock #${index + 1}`,
        published_at: `2026-07-${String(20 - index).padStart(2, "0")}T12:00:00Z`,
        url: `https://www.youtube.com/watch?v=video00000${index}`,
        thumbnail_url: `https://i${2 + (index % 2)}.ytimg.com/vi/video00000${index}/hqdefault.jpg`
      })),
      generated_at: now,
      patches_available: true,
      videos_available: true
    });
    return;
  }
  if (path === "/api/v1/content/patches/1836506165584438") {
    json(response, 200, {
      id: "1836506165584438",
      title: "Minor Update - 07-09-2026",
      published_at: "2026-07-09T12:00:00Z",
      url: "https://store.steampowered.com/news/app/1422450/view/1836506165584438",
      content: "Urn speed reduced",
      sections: [{
        kind: "general",
        title: "Общие изменения",
        hero_name: null,
        changes: [
          "Movement smoothing improved",
          "https://clan.fastly.steamstatic.com/images/45164767/f6a6d5724077ee5ea7b3b3701f4af907c9517df4.png"
        ],
        abilities: []
      }, {
        kind: "objective",
        title: "Urn",
        hero_name: null,
        objective_key: "urn",
        objective_icon_url: "https://assets-bucket.deadlock-api.com/assets-api-res/icons/minimap/soul_jar_marker_psd.png",
        changes: ["Speed reduced"],
        abilities: []
      }, {
        kind: "objective",
        title: "Unstable Rift",
        hero_name: null,
        objective_key: "unstable_rift",
        objective_icon_url: null,
        changes: ["Rift Troopers health increased"],
        abilities: []
      }, {
        kind: "item",
        title: "Crushing Fists",
        hero_name: null,
        item_name: "Crushing Fists",
        item_category: "weapon",
        item_icon_url: "https://deadlock.io/assets/game/panorama/images/items/weapon/crushing_fists_psd.png",
        changes: ["Stun duration increased"],
        abilities: []
      }, {
        kind: "hero",
        title: "The Doorman",
        hero_name: "The Doorman",
        changes: ["Doorway duration reduced"],
        abilities: []
      }]
    });
    return;
  }
  if ((path === "/api/v1/tournaments" || path === "/api/v1/tournaments/mine") && request.method === "GET") {
    const page = tournamentPage(url);
    json(response, 200, page.items, {
      "x-total-count": String(page.total),
      "x-limit": String(page.limit),
      "x-offset": String(page.offset),
      "x-has-more": String(page.offset + page.items.length < page.total)
    });
    return;
  }
  if (path === "/api/v1/tournaments/invites/suggest-code") {
    json(response, 200, { code: "SMOKE2026X", available: true });
    return;
  }
  if (path === "/api/v1/tournaments/invites/code-status") {
    json(response, 200, { code: url.searchParams.get("code") ?? "", available: true });
    return;
  }
  const workspaceMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/workspace$/);
  if (workspaceMatch && request.method === "GET") {
    const tournament = workspaceMatch[1] === bracketManagerTournament.slug
      ? bracketManagerTournament
      : tournaments.find((item) => item.slug === workspaceMatch[1]);
    const actor = actorForRequest(request);
    json(
      response,
      tournament ? 200 : 404,
      tournament ? {
        tournament: {
          ...tournament,
          current_user_participant_status: participantStatusForRequest(request)
        },
        current_user: actor ? {
          id: actor.id,
          email: actor.email,
          display_name: actor.display_name,
          status: "active",
          created_at: now,
          roles: actor.roles,
          can_create_public_tournaments: actor.roles.includes("admin") || actor.roles.includes("superadmin"),
          public_tournament_credits: 2,
          private_tournament_credits: 4
        } : null,
        current_user_active_commitment: (request.headers.cookie ?? "").includes("commitment-blocked-smoke=1")
          ? {
              id: "commitment_active",
              tournament_id: "t_active_cup",
              tournament_slug: "active-cup",
              tournament_name: "Active Cup",
              assignment_run_id: "assignment_active",
              team_id: "7",
              team_name: "Синие",
              activated_at: now
            }
          : null,
        participants: [],
        participants_total: 0,
        participants_limit: Number(url.searchParams.get("participants_limit") ?? 25),
        participants_offset: Number(url.searchParams.get("participants_offset") ?? 0),
        participants_has_more: false,
        participants_available: true,
        bracket: bracketPayload(
          tournament.slug,
          tournament.slug === bracketManagerTournament.slug
            || (request.headers.cookie ?? "").includes("bracket-manager-smoke=1"),
          !(request.headers.cookie ?? "").includes("teams-pending-smoke=1")
        ),
        ready_check: readyPayload(tournament, request),
        auto_assignment: { latest_run: null, published_run: null }
      } : { detail: "Tournament not found." }
    );
    return;
  }
  const participantMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/participants$/);
  if (participantMatch && request.method === "GET") {
    json(response, 200, [], {
      "x-total-count": "0",
      "x-limit": url.searchParams.get("limit") ?? "25",
      "x-offset": url.searchParams.get("offset") ?? "0",
      "x-has-more": "false"
    });
    return;
  }
  const bracketMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/bracket$/);
  if (bracketMatch && request.method === "GET") {
    const slug = bracketMatch[1];
    const etag = '"mock-bracket-revision-0"';
    const requestEtag = request.headers["if-none-match"] ?? "";
    if (requestEtag === etag || requestEtag === `W/${etag}`) {
      response.writeHead(304, {
        etag,
        "cache-control": "private, no-cache",
        vary: "Cookie, Accept-Encoding"
      });
      response.end();
      return;
    }
    json(
      response,
      200,
      bracketPayload(
        slug,
        slug === bracketManagerTournament.slug
          || (request.headers.cookie ?? "").includes("bracket-manager-smoke=1")
      ),
      {
        etag,
        "cache-control": "private, no-cache",
        vary: "Cookie, Accept-Encoding"
      }
    );
    return;
  }
  const bracketProbeMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/bracket\/probe$/);
  if (bracketProbeMatch && request.method === "GET") {
    json(response, 200, { revision: 0, status: "ready" });
    return;
  }
  const readyMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/deadlock\/ready-check$/);
  if (readyMatch) {
    const tournament = tournaments.find((item) => item.slug === readyMatch[1]);
    json(response, 200, readyPayload(tournament));
    return;
  }
  if (/^\/api\/v1\/tournaments\/[^/]+\/deadlock\/auto-assignment$/.test(path)) {
    json(response, 200, { latest_run: null, published_run: null, locked_run: null });
    return;
  }
  const tournamentMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)$/);
  if (tournamentMatch && request.method === "GET") {
    const tournament = tournaments.find((item) => item.slug === tournamentMatch[1]);
    json(
      response,
      tournament ? 200 : 404,
      tournament ? {
        ...tournament,
        current_user_participant_status: participantStatusForRequest(request)
      } : { detail: "Tournament not found." }
    );
    return;
  }
  if (path === "/api/v1/users/me") {
    if ((request.headers.cookie ?? "").includes("users-me-malformed-smoke=1")) {
      json(response, 200, {
        id: "u_malformed",
        email: null,
        display_name: "Malformed User",
        status: "active",
        created_at: now,
        roles: ["player"],
        can_create_public_tournaments: "yes",
        steam_id: 76561198000000000,
        steam_linked: "true",
        has_password: "false"
      });
      return;
    }
    if ((request.headers.cookie ?? "").includes("users-me-unavailable-smoke=1")) {
      json(response, 503, { detail: "Current-user service unavailable." });
      return;
    }
    const actor = actorForRequest(request);
    json(response, actor ? 200 : 401, actor ? {
      id: actor.id,
      email: actor.email,
      display_name: actor.display_name,
      status: "active",
      created_at: now,
      roles: actor.roles,
      can_create_public_tournaments: actor.roles.includes("admin") || actor.roles.includes("superadmin"),
      public_tournament_credits: actor.id === "u_limited_creator" ? 0 : 2,
      private_tournament_credits: (request.headers.cookie ?? "").includes("private-allowance-exhausted-smoke=1") ? 0 : 4,
      private_tournament_monthly_remaining: (request.headers.cookie ?? "").includes("private-monthly-used-smoke=1")
        || (request.headers.cookie ?? "").includes("private-allowance-exhausted-smoke=1") ? 0 : 1,
      private_tournament_monthly_limit: 1,
      steam_id: actor.steam_id ?? (actor.id === "u_lisalexy" ? "76561198000000000" : null),
      steam_linked: Boolean(actor.steam_id || actor.id === "u_lisalexy"),
      has_password: actor.has_password ?? true,
      can_unlink_steam: Boolean(actor.email && (actor.has_password ?? true))
    } : { detail: "Not authenticated." });
    return;
  }
  const profileMediaMatch = path.match(/^\/api\/v1\/profiles\/me\/(avatar|banner)$/u);
  const tournamentMediaMatch = path.match(/^\/api\/v1\/tournaments\/([^/]+)\/banner$/u);
  if ((profileMediaMatch || tournamentMediaMatch) && request.method === "POST") {
    request.resume();
    mockMediaSequence += 1;
    const purpose = profileMediaMatch
      ? profileMediaMatch[1] === "avatar" ? "profile_avatar" : "profile_banner"
      : "tournament_banner";
    const assetId = `00000000-0000-4000-8000-${String(mockMediaSequence).padStart(12, "0")}`;
    mockMediaAssets.set(assetId, purpose);
    json(response, 202, {
      asset_id: assetId,
      status: "pending",
      status_url: `/api/v1/media/${assetId}/status`
    });
    return;
  }
  if ((profileMediaMatch || tournamentMediaMatch) && request.method === "DELETE") {
    json(response, 202, { asset_id: null, status: "deleted" });
    return;
  }
  const mediaStatusMatch = path.match(/^\/api\/v1\/media\/([^/]+)\/status$/u);
  if (mediaStatusMatch) {
    const purpose = mockMediaAssets.get(mediaStatusMatch[1]);
    const variants = purpose === "profile_avatar"
      ? [128, 256, 512].map((width) => ({
          name: `avatar-${width}`,
          width,
          height: width,
          byte_size: 1024,
          url: `/assets/heroes/Abrams.png?prepared=${width}`
        }))
      : purpose === "profile_banner"
        ? [960, 1920].map((width) => ({
            name: `banner-${width}`,
            width,
            height: width / 4,
            byte_size: 2048,
            url: `/assets/tournament-covers/tournament-cover-template-2-v1.webp?prepared=${width}`
          }))
        : [560, 1120].map((width) => ({
            name: `banner-${width}`,
            width,
            height: width / 4,
            byte_size: 2048,
            url: `/assets/tournament-covers/tournament-cover-template-3-v1.webp?prepared=${width}`
          }));
    json(response, purpose ? 200 : 404, purpose ? {
      asset_id: mediaStatusMatch[1],
      purpose,
      status: "ready",
      error_code: null,
      variants
    } : { detail: "Media asset not found." });
    return;
  }
  if (path === "/api/v1/profiles/me") {
    if ((request.headers.cookie ?? "").includes("profile-unavailable-smoke=1")) {
      json(response, 503, { detail: "Profile service unavailable." });
      return;
    }
    const actor = actorForRequest(request);
    json(response, authenticated(request) ? 200 : 401, authenticated(request) ? {
      ...profile,
      id: actor?.id ?? profile.user_id,
      user_id: actor?.id ?? profile.user_id,
      account_email: actor?.email ?? null,
      display_name: actor?.display_name ?? "lisalexy",
      avatar_url: null,
      banner_url: null,
      avatar_media: null,
      banner_media: null,
      contact_email: actor?.email ?? null,
      region: "Finland, Kannus",
      steam_id: actor?.steam_id ?? (actor?.id === "u_lisalexy" ? "76561198000000000" : null),
      steam_linked: Boolean(actor?.steam_id || actor?.id === "u_lisalexy"),
      discord_account: "lisalexy#4821",
      captain_team_name: "OldTeam"
    } : { detail: "Not authenticated." });
    return;
  }
  if (path === "/api/v1/profiles/me/deadlock") {
    json(response, authenticated(request) ? 200 : 401, authenticated(request) ? profile.deadlock_profile : { detail: "Not authenticated." });
    return;
  }
  if (path === "/api/v1/profiles/public/lisalexy") {
    json(response, 200, profile);
    return;
  }
  const tournamentProfileMatch = path.match(/^\/api\/v1\/tournaments\/night-veil-open-5\/profiles\/(u_shadow|u_echo)$/);
  if (tournamentProfileMatch) {
    const userId = tournamentProfileMatch[1];
    const isShadow = userId === "u_shadow";
    json(response, 200, {
      profile: {
        user_id: userId,
        display_name: isShadow ? "ShadowHawk" : "EchoBlade",
        handle: isShadow ? "shadowhawk" : "echoblade",
        avatar_url: isShadow ? "/assets/heroes/Kelvin.png" : "/assets/heroes/Ivy.png",
        banner_url: null,
        bio: "Участник Night Veil Open #5.",
        contact_email: `${isShadow ? "shadowhawk" : "echoblade"}@example.test`,
        region: "Europe",
        steam_id: isShadow ? "76561198000000001" : "76561198000000002",
        discord_account: isShadow ? "shadowhawk" : "echoblade",
        captain_team_name: isShadow ? "Neon Revenants" : null,
        updated_at: now
      },
      deadlock_profile: {
        user_id: userId,
        rank: isShadow ? "Oracle" : "Emissary",
        subrank: isShadow ? 5 : 3,
        playtime: "1501-2000",
        roles: isShadow ? ["Carry", "Semi-Carry"] : ["Support", "Semi-Support"],
        pool: isShadow ? ["Kelvin", "Haze", "Warden"] : ["Ivy", "Dynamo", "Viscous"],
        captain_priority: isShadow ? "yes" : "neutral",
        updated_at: now
      },
      dream_slots: [],
      stats: {
        tournaments_played: 4,
        tournaments_organized: 0,
        tournaments_won: 1,
        recent_tournaments: ["Night Veil Open #5"]
      }
    });
    return;
  }
  if (path === "/api/v1/stats/overview") {
    json(response, 200, {
      total_tournaments: 2,
      completed_tournaments: 0,
      active_upcoming_tournaments: 2,
      registered_participants: 160,
      completed_matches: 0,
      deadlock_profiles_total: 1,
      registered_participants_with_deadlock_profile: 1,
      deadlock_profile_coverage_percent: 100,
      deadlock_rank_distribution: [{ rank: "Eternus", count: 1 }]
    });
    return;
  }

  json(response, 404, { detail: "Mock endpoint not found." });
});

server.listen(port, host, () => {
  process.stdout.write(`Mock platform API listening on http://${host}:${port}\n`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
