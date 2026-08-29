const newPatchWindowMs = 3 * 24 * 60 * 60 * 1000;

export function isNewPatch(publishedAt: string, now = Date.now()): boolean {
  const publishedTime = Date.parse(publishedAt);
  const age = now - publishedTime;
  return Number.isFinite(publishedTime) && age >= 0 && age < newPatchWindowMs;
}
