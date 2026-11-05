export function mapAfterCpuWindowSnapshot<T>(
  elapsedMs: number,
  snapshot: Promise<unknown>,
  map: () => T,
): Promise<{ elapsedMs: number; mapped: T }>;

export function runCpuWindowSetup<T>(
  resolveSnapshot: () => void,
  setup: () => T,
): T;
