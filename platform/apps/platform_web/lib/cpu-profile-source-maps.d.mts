export type CpuProfileFrame = {
  url?: string;
  functionName?: string;
  lineNumber?: number;
  columnNumber?: number;
};

export type CpuProfileSourceMapLimits = Readonly<{
  files: number;
  mapBytes: number;
  totalBytes: number;
  generatedBytes: number;
}>;

export const CPU_PROFILE_SOURCE_MAP_LIMITS: CpuProfileSourceMapLimits;
export function mapSampledFrames(
  nodes: ReadonlyMap<number, CpuProfileFrame>,
  sampledNodeIds: readonly number[],
  runtimeCwd: string,
  limits?: Partial<CpuProfileSourceMapLimits>,
): Map<number, CpuProfileFrame>;
