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
