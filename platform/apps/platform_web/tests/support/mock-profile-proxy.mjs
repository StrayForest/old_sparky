import { createServer, request as httpRequest } from "node:http";

const host = process.env.MOCK_PROFILE_PROXY_HOST ?? "127.0.0.1";
const port = Number(process.env.MOCK_PROFILE_PROXY_PORT ?? 3199);
const upstreamHost = process.env.MOCK_PLATFORM_API_HOST ?? "127.0.0.1";
const upstreamPort = Number(process.env.MOCK_PLATFORM_API_UPSTREAM_PORT ?? 3198);
const upstreamOrigin = `http://${upstreamHost}:${upstreamPort}`;

function json(response, status, payload, headers = {}) {
  response.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
    ...headers,
  });
  response.end(JSON.stringify(payload));
}

async function upstreamJson(path, cookie) {
  const response = await fetch(`${upstreamOrigin}${path}`, {
    headers: {
      accept: "application/json",
      cookie,
    },
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({ detail: response.statusText }));
  return { status: response.status, payload };
}

function emptyDreamSlots(userId) {
  return Array.from({ length: 6 }, (_, index) => ({
    user_id: userId,
    slot_number: index + 1,
    allowed_roles: [],
    desired_heroes: [],
    updated_at: null,
  }));
}

async function profileWorkspace(request, response) {
  const cookie = request.headers.cookie ?? "";
  const [userResult, profileResult, deadlockResult] = await Promise.all([
    upstreamJson("/api/v1/users/me", cookie),
    upstreamJson("/api/v1/profiles/me", cookie),
    upstreamJson("/api/v1/profiles/me/deadlock", cookie),
  ]);

  if (userResult.status === 401 || profileResult.status === 401) {
    json(response, 401, { detail: "Not authenticated." });
    return;
  }
  if (userResult.status !== 200 || profileResult.status !== 200) {
    json(
      response,
      profileResult.status !== 200 ? profileResult.status : userResult.status,
      { detail: "Profile workspace unavailable." }
    );
    return;
  }

  const userId = profileResult.payload.user_id ?? userResult.payload.id ?? "";
  json(response, 200, {
    profile: profileResult.payload,
    deadlock_profile: deadlockResult.status === 200
      ? {
          ...deadlockResult.payload,
          captain_priority: cookie.includes("captain-priority-yes-smoke=1")
            ? "yes"
            : deadlockResult.payload.captain_priority,
        }
      : null,
    dream_slots: emptyDreamSlots(userId),
  });
}

async function captainUpdate(request, response) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  let payload = {};
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    json(response, 400, { detail: "Invalid JSON." });
    return;
  }

  const user = await upstreamJson(
    "/api/v1/users/me",
    request.headers.cookie ?? ""
  );
  if (user.status !== 200) {
    json(response, 401, { detail: "Not authenticated." });
    return;
  }

  const slotsByNumber = new Map(
    Array.isArray(payload.slots)
      ? payload.slots.map((slot) => [Number(slot.slot_number), slot])
      : []
  );
  json(response, 200, {
    captain_team_name: String(payload.captain_team_name ?? "").trim().slice(0, 15),
    dream_slots: Array.from({ length: 6 }, (_, index) => {
      const slotNumber = index + 1;
      const slot = slotsByNumber.get(slotNumber);
      return {
        user_id: user.payload.id,
        slot_number: slotNumber,
        allowed_roles: Array.isArray(slot?.allowed_roles) ? slot.allowed_roles : [],
        desired_heroes: Array.isArray(slot?.desired_heroes) ? slot.desired_heroes : [],
        updated_at: null,
      };
    }),
  });
}

function proxyRequest(request, response) {
  const headers = { ...request.headers };
  for (const header of [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ]) {
    delete headers[header];
  }
  headers.host = `${upstreamHost}:${upstreamPort}`;
  headers.connection = "close";
  const upstream = httpRequest(
    {
      hostname: upstreamHost,
      port: upstreamPort,
      method: request.method,
      path: request.url,
      headers,
      agent: false,
    },
    (upstreamResponse) => {
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.headers
      );
      upstreamResponse.pipe(response);
    }
  );
  upstream.on("error", () => {
    if (!response.headersSent) {
      json(response, 502, { detail: "Mock upstream unavailable." });
    } else {
      response.end();
    }
  });
  request.pipe(upstream);
}

const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://${host}:${port}`);
  const path = url.pathname.replace(/\/$/, "") || "/";

  if (
    path === "/api/v1/profiles/me/workspace" &&
    request.method === "GET"
  ) {
    void profileWorkspace(request, response).catch(() => {
      json(response, 503, { detail: "Profile workspace unavailable." });
    });
    return;
  }

  if (
    path === "/api/v1/profiles/me/captain" &&
    request.method === "PUT"
  ) {
    void captainUpdate(request, response).catch(() => {
      json(response, 503, { detail: "Captain profile unavailable." });
    });
    return;
  }

  proxyRequest(request, response);
});

server.listen(port, host, () => {
  process.stdout.write(
    `Mock profile proxy listening on http://${host}:${port} -> ${upstreamOrigin}\n`
  );
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
