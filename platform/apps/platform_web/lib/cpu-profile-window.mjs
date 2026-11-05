export async function mapAfterCpuWindowSnapshot(elapsedMs, snapshot, map) {
  await snapshot;
  return { elapsedMs, mapped: map() };
}

export function runCpuWindowSetup(resolveSnapshot, setup) {
  try {
    return setup();
  } catch (error) {
    resolveSnapshot();
    throw error;
  }
}
