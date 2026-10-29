const JSON_FRAME_PATH = /(?:^|\/)(?:json|json_parse|json-parser)(?:[./-]|$)/u;

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
