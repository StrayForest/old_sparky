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

const NS_PER_MS = BigInt(1_000_000);
const NS_PER_US = BigInt(1_000);
const MAX_CLOCK_OFFSET_WIDTH_NS = BigInt(10_000_000);
const MAX_EDGE_LAG_NS = BigInt(250_000_000);
const MAX_SAMPLE_GAP_US = 250_000;

/** Read a wall-clock anchor bracketed by the process monotonic clock. */
export function readCpuProfileClockAnchor() {
  const beforeNs = process.hrtime.bigint();
  const wallMs = Date.now();
  const afterNs = process.hrtime.bigint();
  return { beforeNs, wallMs, afterNs };
}

/** Intersect Date.now/monotonic offset ranges, accounting for millisecond quantization. */
export function intersectCpuProfileClockAnchors(anchors) {
  if (!Array.isArray(anchors) || anchors.length < 2) return null;
  let offsetMinNs = null;
  let offsetMaxNs = null;
  for (const anchor of anchors) {
    if (!anchor || typeof anchor.beforeNs !== "bigint" || typeof anchor.afterNs !== "bigint"
        || !Number.isSafeInteger(anchor.wallMs) || anchor.beforeNs < BigInt(0)
        || anchor.afterNs < anchor.beforeNs) return null;
    const wallNs = BigInt(anchor.wallMs) * NS_PER_MS;
    const minimum = wallNs - anchor.afterNs;
    const maximum = (BigInt(anchor.wallMs) + BigInt(1)) * NS_PER_MS - anchor.beforeNs;
    offsetMinNs = offsetMinNs === null || minimum > offsetMinNs ? minimum : offsetMinNs;
    offsetMaxNs = offsetMaxNs === null || maximum < offsetMaxNs ? maximum : offsetMaxNs;
  }
  if (offsetMinNs === null || offsetMaxNs === null || offsetMinNs > offsetMaxNs
      || offsetMaxNs - offsetMinNs > MAX_CLOCK_OFFSET_WIDTH_NS) return null;
  return { offsetMinNs, offsetMaxNs };
}

/** Validate a bounded V8 profile and return only whole sample intervals inside the planned window. */
export function clipCpuProfileToPlannedWindow(
  profile,
  {
    offsetMinNs,
    offsetMaxNs,
    startWallMs,
    endWallMs,
    startBracket,
    stopBracket,
    maxSamples,
    maxNodes,
    maxCpuUs,
  },
) {
  const invalid = () => null;
  if (!profile || !Array.isArray(profile.nodes) || !Array.isArray(profile.samples)
      || !Array.isArray(profile.timeDeltas) || !Number.isSafeInteger(profile.startTime)
      || !Number.isSafeInteger(profile.endTime) || profile.startTime < 0
      || profile.endTime <= profile.startTime || profile.samples.length === 0
      || profile.samples.length > maxSamples || profile.samples.length !== profile.timeDeltas.length
      || profile.nodes.length === 0 || profile.nodes.length > maxNodes
      || !Number.isSafeInteger(startWallMs) || !Number.isSafeInteger(endWallMs)
      || endWallMs <= startWallMs || typeof offsetMinNs !== "bigint"
      || typeof offsetMaxNs !== "bigint" || offsetMinNs > offsetMaxNs
      || !startBracket || !stopBracket
      || typeof startBracket.beforeNs !== "bigint" || typeof startBracket.afterNs !== "bigint"
      || typeof stopBracket.beforeNs !== "bigint" || typeof stopBracket.afterNs !== "bigint"
      || startBracket.afterNs < startBracket.beforeNs
      || stopBracket.afterNs < stopBracket.beforeNs) return invalid();

  const profileStartNs = BigInt(profile.startTime) * NS_PER_US;
  const profileEndNs = BigInt(profile.endTime) * NS_PER_US;
  const bracketMarginNs = NS_PER_US;
  if (profileStartNs < startBracket.beforeNs - bracketMarginNs
      || profileStartNs > startBracket.afterNs + bracketMarginNs
      || profileEndNs < stopBracket.beforeNs - bracketMarginNs
      || profileEndNs > stopBracket.afterNs + bracketMarginNs) return invalid();

  const nodeIds = new Set();
  for (const node of profile.nodes) {
    if (!node || !Number.isSafeInteger(node.id) || node.id < 0 || nodeIds.has(node.id)) return invalid();
    nodeIds.add(node.id);
  }
  if (profile.timeDeltas.some((delta) => !Number.isSafeInteger(delta) || delta <= 0)
      || profile.timeDeltas.reduce((total, delta) => total + delta, 0) > maxCpuUs
      || profile.samples.some((sample) => !Number.isSafeInteger(sample) || !nodeIds.has(sample))) {
    return invalid();
  }

  const startWallNs = BigInt(startWallMs) * NS_PER_MS;
  const endWallNs = BigInt(endWallMs) * NS_PER_MS;
  const earliestStartNs = startWallNs - offsetMaxNs;
  const latestStartNs = startWallNs - offsetMinNs;
  const earliestEndNs = endWallNs - offsetMaxNs;
  const latestEndNs = endWallNs - offsetMinNs;
  if (latestStartNs >= earliestEndNs) return invalid();

  let sampleAtUs = BigInt(profile.startTime);
  let previousAtUs = sampleAtUs;
  let totalCpuUs = BigInt(0);
  const samples = [];
  const timeDeltas = [];
  let firstIntervalStartUs = null;
  let lastIntervalEndUs = null;
  for (let index = 0; index < profile.samples.length; index += 1) {
    const deltaUs = profile.timeDeltas[index];
    sampleAtUs += BigInt(deltaUs);
    if (sampleAtUs <= previousAtUs || sampleAtUs * NS_PER_US > profileEndNs) return invalid();
    const intervalStartNs = previousAtUs * NS_PER_US;
    const intervalEndNs = sampleAtUs * NS_PER_US;
    if (intervalEndNs > latestStartNs && intervalStartNs < earliestEndNs
        && deltaUs > MAX_SAMPLE_GAP_US) return invalid();
    if (intervalStartNs >= latestStartNs && intervalEndNs <= earliestEndNs) {
      if (firstIntervalStartUs === null) firstIntervalStartUs = previousAtUs;
      lastIntervalEndUs = sampleAtUs;
      samples.push(profile.samples[index]);
      timeDeltas.push(deltaUs);
      totalCpuUs += BigInt(deltaUs);
    }
    previousAtUs = sampleAtUs;
  }
  if (firstIntervalStartUs === null || lastIntervalEndUs === null || totalCpuUs <= BigInt(0)) return invalid();

  const firstStartNs = firstIntervalStartUs * NS_PER_US;
  const lastEndNs = lastIntervalEndUs * NS_PER_US;
  const durationNs = BigInt(endWallMs - startWallMs) * NS_PER_MS;
  const startLagNs = firstStartNs - earliestStartNs;
  const earliestEndLagNs = lastEndNs - earliestEndNs;
  const latestEndLagNs = lastEndNs - latestEndNs;
  const elapsedNs = totalCpuUs * NS_PER_US;
  const startLagMs = Number((startLagNs + NS_PER_MS - BigInt(1)) / NS_PER_MS);
  const elapsedMs = Number(totalCpuUs / BigInt(1_000));
  const parserEndLagMs = startLagMs + elapsedMs - (endWallMs - startWallMs);
  if (startLagNs < BigInt(0) || startLagNs > MAX_EDGE_LAG_NS
      || earliestEndLagNs > MAX_EDGE_LAG_NS || latestEndLagNs < -MAX_EDGE_LAG_NS
      || elapsedNs < durationNs - MAX_EDGE_LAG_NS
      || elapsedNs > durationNs + MAX_EDGE_LAG_NS
      || parserEndLagMs < -250 || parserEndLagMs > 250) return invalid();

  return {
    samples,
    timeDeltas,
    elapsedMs,
    startLagMs,
  };
}
