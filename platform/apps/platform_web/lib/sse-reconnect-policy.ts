// The production public contour has demonstrated 50 safe new SSE opens/sec
// at the 3,000-stream application cap. A complete population loss therefore
// needs at least 60 seconds to refill. Keep a 90-second full-jitter window on
// top of that minimum so a mass disconnect cannot create a synchronized burst.
export const SSE_RETRY_MIN_DELAY_MS = 60_000;
export const SSE_RETRY_JITTER_WINDOW_MS = 90_000;

export function sseRetryDelayMs(randomValue = Math.random()): number {
  const normalized = Number.isFinite(randomValue)
    ? Math.min(1, Math.max(0, randomValue))
    : 0;
  return SSE_RETRY_MIN_DELAY_MS
    + Math.floor(normalized * SSE_RETRY_JITTER_WINDOW_MS);
}
