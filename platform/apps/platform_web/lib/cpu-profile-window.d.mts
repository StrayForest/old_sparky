export function mapAfterCpuWindowSnapshot<T>(
  elapsedMs: number,
  snapshot: Promise<unknown>,
  map: () => T,
): Promise<{ elapsedMs: number; mapped: T }>;

export function runCpuWindowSetup<T>(
  resolveSnapshot: () => void,
  setup: () => T,
): T;

export type CpuProfileClockAnchor = {
  readonly beforeNs: bigint;
  readonly wallMs: number;
  readonly afterNs: bigint;
};

export type CpuProfileClockBracket = {
  readonly beforeNs: bigint;
  readonly afterNs: bigint;
};

export type BoundedV8CpuProfile = {
  readonly startTime: number;
  readonly endTime: number;
  readonly nodes: ReadonlyArray<{ readonly id: number }>;
  readonly samples: ReadonlyArray<number>;
  readonly timeDeltas: ReadonlyArray<number>;
};

export type CpuProfileClockOffsetRange = {
  readonly offsetMinNs: bigint;
  readonly offsetMaxNs: bigint;
};

export function readCpuProfileClockAnchor(): CpuProfileClockAnchor;

export function intersectCpuProfileClockAnchors(
  anchors: ReadonlyArray<CpuProfileClockAnchor>,
): CpuProfileClockOffsetRange | null;

export function clipCpuProfileToPlannedWindow(
  profile: BoundedV8CpuProfile,
  options: CpuProfileClockOffsetRange & {
    readonly startWallMs: number;
    readonly endWallMs: number;
    readonly startBracket: CpuProfileClockBracket;
    readonly stopBracket: CpuProfileClockBracket;
    readonly maxSamples: number;
    readonly maxNodes: number;
    readonly maxCpuUs: number;
  },
): { readonly samples: number[]; readonly timeDeltas: number[]; readonly elapsedMs: number; readonly startLagMs: number } | null;
