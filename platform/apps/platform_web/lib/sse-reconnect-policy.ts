// The production public contour has demonstrated a clean 25 new SSE opens/sec
// at the 3,000-stream application cap. A complete population loss therefore
// needs a 120-second refill window. Keep the minimum at 60 seconds and use a
// 120-second full-jitter window so mass recovery averages no more than that
// proven rate without synchronizing clients.
export const SSE_RETRY_MIN_DELAY_MS = 60_000;
export const SSE_RETRY_JITTER_WINDOW_MS = 120_000;

export function sseRetryDelayMs(randomValue = Math.random()): number {
  const normalized = Number.isFinite(randomValue)
    ? Math.min(1, Math.max(0, randomValue))
    : 0;
  return SSE_RETRY_MIN_DELAY_MS
    + Math.floor(normalized * SSE_RETRY_JITTER_WINDOW_MS);
}
