type NodeRuntimeProcess = typeof process & {
  getBuiltinModule?: (id: string) => object | undefined;
};

type HeapMetricsRuntime = {
  setInterval?: typeof setInterval;
  clearInterval?: typeof clearInterval;
  writeLine?: (line: string) => void;
};

export function startHeapMetrics(
  nodeProcess: NodeRuntimeProcess,
  perfHooks: typeof import("node:perf_hooks"),
  runtime?: HeapMetricsRuntime,
): () => void;

export function isStableBoundedPseudoFileRead(input: {
  beforeSize: number;
  afterSize: number;
  beforeDevice: number;
  afterDevice: number;
  beforeInode: number;
  afterInode: number;
  beforeRegularFile: boolean;
  afterRegularFile: boolean;
  byteLength: number;
  limit: number;
}): boolean;

export function appendBoundedSample<T>(samples: T[], value: T, limit: number): boolean;

export function isTimelineEntryWithinWindow(startTime: number, windowStart: number, windowEnd: number): boolean;

export function timelineEntryDurationWithinWindow(
  startTime: number,
  duration: number,
  windowStart: number,
  windowEnd: number,
): number | null;

export function classifyCgroupReadFailure(code: string | null): "unavailable" | "invalid";

export function formatCpuDiagnosticMemoryEvent(tokens: string[]): string;
