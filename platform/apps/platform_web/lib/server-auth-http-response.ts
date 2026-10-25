import type { IncomingMessage } from "node:http";

export type ServerJsonResponse = {
  status: number;
  ok: boolean;
  json: () => Promise<unknown>;
};

export type ServerAuthJsonParseObserver = (
  durationMs: number,
  outcome: "ok" | "error",
) => void | Promise<void>;

export const SERVER_AUTH_RESPONSE_MAX_BYTES = 256 * 1024;

/** Consume one bounded auth response, rejecting and closing oversized bodies. */
export function readServerAuthJsonResponse(
  response: IncomingMessage,
  jsonParseObserver?: ServerAuthJsonParseObserver,
): Promise<ServerJsonResponse> {
  return new Promise<ServerJsonResponse>((resolve, reject) => {
    let settled = false;
    const chunks: Buffer[] = [];
    let totalBytes = 0;
    const settle = (operation: () => void) => {
      if (settled) return;
      settled = true;
      response.removeListener("aborted", responseAborted);
      response.removeListener("data", responseData);
      response.removeListener("end", responseEnd);
      operation();
    };
    const responseError = (error: Error) => settle(() => reject(error));
    function responseAborted(): void {
      settle(() => reject(new Error("Server auth response was aborted.")));
    }
    const responseClose = () => {
      if (!settled && !response.complete) {
        settle(() => reject(new Error("Server auth response closed before completion.")));
      }
      // Node can emit a socket error after `aborted` and `close`; keep the
      // listener for incomplete messages so that late resets stay contained.
      if (response.complete) response.removeListener("error", responseError);
    };

    // Attach before the declared-size check: destroying an oversized response
    // can emit an error asynchronously, and that must not escape as unhandled.
    response.on("error", responseError);
    response.once("close", responseClose);
    response.on("aborted", responseAborted);

    const contentLength = Number(response.headers["content-length"]);
    if (Number.isFinite(contentLength) && contentLength > SERVER_AUTH_RESPONSE_MAX_BYTES) {
      const error = new Error("Server auth response exceeded its size limit.");
      response.destroy();
      settle(() => reject(error));
      return;
    }

    function responseData(chunk: Buffer | string): void {
      if (settled) return;
      const bytes = Buffer.byteLength(chunk);
      totalBytes += bytes;
      if (totalBytes > SERVER_AUTH_RESPONSE_MAX_BYTES) {
        const error = new Error("Server auth response exceeded its size limit.");
        response.destroy();
        settle(() => reject(error));
        return;
      }
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    function responseEnd(): void {
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
    }
    response.on("data", responseData);
    response.on("end", responseEnd);
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
