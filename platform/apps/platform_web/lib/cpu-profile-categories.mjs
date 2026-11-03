const JSON_FRAME_PATH = /(?:^|\/)(?:json|json_parse|json-parser)(?:[./-]|$)/u;

// Function identities are a reviewed, source-free projection. Paths and raw
// V8 symbols are never returned to logs or parent reports.
const FUNCTION_IDENTITIES = new Map([
  ["repo:getTournamentWorkspace", "web.workspace.api_fetch"],
  ["repo:loadTournamentWorkspace", "web.workspace.page_load"],
  ["repo:TournamentPage", "web.workspace.page_render"],
  ["react:renderToReadableStream", "web.react.render_stream"],
  ["react:renderToString", "web.react.render_string"],
  ["react:resolveModelToJSON", "web.react.serialize_model"],
  ["react:serializeRow", "web.react.serialize_row"],
  ["json:parse", "web.serialization.json_parse"],
  ["json:stringify", "web.serialization.json_stringify"],
  ["zod:parse", "web.serialization.schema_parse"],
  ["zod:safeParse", "web.serialization.schema_parse"],
  ["next:renderToHTMLOrFlight", "web.next.render_route"],
  ["next:renderToResponseWithComponentsImpl", "web.next.render_response"],
  ["next:generateFlight", "web.next.flight_payload"],
  ["next:generateMetadata", "web.next.metadata"],
  ["undici:fetch", "web.http.fetch"],
  ["undici:request", "web.http.request"],
  ["node:parse", "web.serialization.json_parse"],
  ["node:JSONParse", "web.serialization.json_parse"],
  ["node:JSONStringify", "web.serialization.json_stringify"],
  ["react:performWork", "web.react.render_work"],
  ["react:renderWithHooks", "web.react.render_work"],
  ["react:renderNodeDestructive", "web.react.render_work"],
]);

export const CPU_PROFILE_SAMPLING = Object.freeze({
  intervalUs: 10_000,
  maxSamples: 2_400,
  maxNodes: 10_000,
});

export function categoryForFrame(url, functionName = "") {
  const normalized = String(url).replaceAll("\\", "/");
  if (normalized.includes("/apps/platform_web/.next/server/app/(site)/tournaments/")) {
    if (["getTournamentWorkspace", "loadTournamentWorkspace", "TournamentPage"].includes(functionName)) {
      return `repo.${functionName === "getTournamentWorkspace" ? "workspace_api_fetch" : "workspace_page"}`;
    }
    return "repo.ssr_route_module";
  }
  if (normalized.includes("/node_modules/next/dist/compiled/react-server-dom-webpack/")
      || normalized.includes("/node_modules/react-server-dom-webpack/")
      || normalized.includes("/node_modules/react-dom/")) {
    return "react_server_render";
  }
  if (JSON_FRAME_PATH.test(normalized)) return "serialization_validation";
  if (normalized.includes("/node_modules/next/")) return "web_framework";
  if (normalized.includes("/node_modules/react/")) return "serialization_validation";
  if (normalized.includes("/node_modules/zod/")) return "serialization_validation";
  if (normalized.startsWith("node:internal/")) return "async_event_loop";
  if (normalized.includes("/node_modules/undici/")) return "http_client";
  if (normalized.startsWith("node:crypto") || normalized.includes("/node:crypto")) return "crypto";
  if (normalized.includes("/.next/server/app/") || normalized.includes("/.next/server/chunks/")) {
    return "compiled_server_chunk";
  }
  return "other";
}

export function functionIdForFrame(url, functionName = "") {
  const normalized = String(url).replaceAll("\\", "/");
  let family = "";
  if (normalized.includes("/apps/platform_web/.next/server/app/(site)/tournaments/")) {
    family = "repo";
  } else if (normalized.includes("/node_modules/next/dist/compiled/react-server-dom-webpack/")
      || normalized.includes("/node_modules/react-server-dom-webpack/")
      || normalized.includes("/node_modules/react-dom/")) {
    family = "react";
  } else if (JSON_FRAME_PATH.test(normalized)) {
    family = "json";
  } else if (normalized.includes("/node_modules/next/")) {
    family = "next";
  } else if (normalized.includes("/node_modules/zod/")) {
    family = "zod";
  } else if (normalized.includes("/node_modules/undici/")) {
    family = "undici";
  } else if (normalized.startsWith("node:internal/")) {
    family = "node";
  }
  return FUNCTION_IDENTITIES.get(`${family}:${String(functionName)}`) ?? null;
}

export function summarizeFunctionSamples(nodes, samples) {
  const counts = new Map();
  for (const nodeId of samples) {
    const frame = nodes.get(nodeId);
    if (!frame) continue;
    const functionId = functionIdForFrame(frame.url ?? "", frame.functionName ?? "");
    if (functionId !== null) counts.set(functionId, (counts.get(functionId) ?? 0) + 1);
  }
  const rows = [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, 20)
    .map(([function_id, count]) => ({ function_id, samples: count }));
  return {
    rows,
    mapped_sample_count: rows.reduce((total, row) => total + row.samples, 0),
  };
}
