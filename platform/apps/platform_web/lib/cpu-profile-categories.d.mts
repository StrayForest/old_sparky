export function categoryForFrame(url: string, functionName?: string): string;
export function functionIdForFrame(url: string, functionName?: string): string | null;
export const CPU_PROFILE_SAMPLING: Readonly<{
  intervalUs: number;
  maxSamples: number;
  maxNodes: number;
}>;
export function summarizeFunctionSamples(
  nodes: ReadonlyMap<number, { url?: string; functionName?: string }>,
  samples: readonly number[],
): {
  rows: Array<{ function_id: string; samples: number }>;
  mapped_sample_count: number;
};
