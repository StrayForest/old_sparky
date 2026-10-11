/** Initiate cancellation for a terminal server fetch response without awaiting the body stream. */
export function cancelServerFetchResponse(
  response: Response,
  controller: AbortController,
): void {
  try {
    const cancellation = response.body?.cancel();
    if (cancellation) {
      void cancellation.catch(() => {});
    }
  } catch {
    // Preserve the terminal response while aborting only this server-owned fetch.
  } finally {
    controller.abort();
  }
}

export type ServerAuthTerminalSnapshot = {
  status: "anonymous" | "unavailable";
  user: null;
};

/** Preserve auth terminal mappings while releasing a fetch-owned response promptly. */
export function cancelServerAuthFetchTerminalResponse(
  response: Response,
  controller: AbortController,
): ServerAuthTerminalSnapshot | null {
  if (response.status === 401 || response.status === 403) {
    cancelServerFetchResponse(response, controller);
    return { status: "anonymous", user: null };
  }
  if (!response.ok) {
    cancelServerFetchResponse(response, controller);
    return { status: "unavailable", user: null };
  }
  return null;
}
