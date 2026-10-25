export type ServerInternalReadQueueFailure = "full" | "deadline";

export class ServerInternalReadQueueError extends Error {
  readonly failure: ServerInternalReadQueueFailure;

  constructor(failure: ServerInternalReadQueueFailure) {
    super(failure === "full"
      ? "The server read queue is full."
      : "The server read queue deadline expired.");
    this.name = "ServerInternalReadQueueError";
    this.failure = failure;
  }
}

type SchedulerOptions = {
  activeLimit: number;
  pendingLimit: number;
  queueDeadlineMs: number;
};

type QueuedPermit = {
  signal?: AbortSignal;
  resolve: (release: () => void) => void;
  reject: (error: Error) => void;
  deadlineAt: number;
  timer: ReturnType<typeof setTimeout>;
  onAbort?: () => void;
  settled: boolean;
};

export type ServerInternalReadAdmissionOutcome = "ok" | "error";
export type ServerInternalReadAdmissionObserver = (
  waitMs: number,
  outcome: ServerInternalReadAdmissionOutcome,
) => void | Promise<void>;

/** A process-local FIFO admission queue for internal, read-only SSR API calls. */
export class ServerInternalReadScheduler {
  private active = 0;
  private readonly pending: QueuedPermit[] = [];
  private readonly options: SchedulerOptions;

  constructor(options: SchedulerOptions) {
    if (
      !Number.isInteger(options.activeLimit) || options.activeLimit < 1
      || !Number.isInteger(options.pendingLimit) || options.pendingLimit < 0
      || !Number.isFinite(options.queueDeadlineMs) || options.queueDeadlineMs < 1
    ) {
      throw new Error("Invalid server internal read scheduler limits.");
    }
    this.options = options;
  }

  get pendingCount(): number {
    return this.pending.length;
  }

  get activeCount(): number {
    return this.active;
  }

  async run<T>(
    operation: () => Promise<T>,
    signal?: AbortSignal,
    admissionObserver?: ServerInternalReadAdmissionObserver,
  ): Promise<T> {
    const waitStartedAt = admissionObserver ? performance.now() : null;
    let release: () => void;
    try {
      release = await this.acquire(signal);
    } catch (error) {
      this.notifyAdmissionObserver(admissionObserver, waitStartedAt, "error");
      throw error;
    }
    try {
      this.notifyAdmissionObserver(admissionObserver, waitStartedAt, "ok");
      return await operation();
    } finally {
      release();
    }
  }

  private notifyAdmissionObserver(
    observer: ServerInternalReadAdmissionObserver | undefined,
    startedAt: number | null,
    outcome: ServerInternalReadAdmissionOutcome,
  ): void {
    if (!observer || startedAt === null) return;
    try {
      const notification = observer(Math.max(0, performance.now() - startedAt), outcome);
      if (notification !== undefined) {
        void Promise.resolve(notification).catch(() => {});
      }
    } catch {
      // Diagnostics must not change admission, cancellation, or read outcomes.
    }
  }

  private acquire(signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) {
      return Promise.reject(abortError(signal));
    }
    if (this.active < this.options.activeLimit && this.pending.length === 0) {
      this.active += 1;
      return Promise.resolve(this.createRelease());
    }
    if (this.pending.length >= this.options.pendingLimit) {
      return Promise.reject(new ServerInternalReadQueueError("full"));
    }

    return new Promise((resolve, reject) => {
      const deadlineAt = performance.now() + this.options.queueDeadlineMs;
      const waiter: QueuedPermit = {
        signal,
        resolve,
        reject,
        deadlineAt,
        timer: setTimeout(() => {
          this.rejectWaiter(waiter, new ServerInternalReadQueueError("deadline"));
        }, this.options.queueDeadlineMs),
        settled: false,
      };
      if (signal) {
        waiter.onAbort = () => this.rejectWaiter(waiter, abortError(signal));
        signal.addEventListener("abort", waiter.onAbort, { once: true });
      }
      this.pending.push(waiter);
    });
  }

  private rejectWaiter(waiter: QueuedPermit, error: Error): void {
    if (!this.settleWaiter(waiter, error, true)) return;
    this.drain();
  }

  private settleWaiter(
    waiter: QueuedPermit,
    error: Error,
    removeFromQueue: boolean,
  ): boolean {
    if (waiter.settled) return false;
    waiter.settled = true;
    if (removeFromQueue) {
      const index = this.pending.indexOf(waiter);
      if (index >= 0) this.pending.splice(index, 1);
    }
    clearTimeout(waiter.timer);
    if (waiter.signal && waiter.onAbort) {
      waiter.signal.removeEventListener("abort", waiter.onAbort);
    }
    waiter.reject(error);
    return true;
  }

  private drain(): void {
    while (this.active < this.options.activeLimit && this.pending.length > 0) {
      const waiter = this.pending.shift();
      if (!waiter || waiter.settled) continue;
      if (waiter.signal?.aborted) {
        this.settleWaiter(waiter, abortError(waiter.signal), false);
        continue;
      }
      if (performance.now() >= waiter.deadlineAt) {
        this.settleWaiter(
          waiter,
          new ServerInternalReadQueueError("deadline"),
          false,
        );
        continue;
      }
      waiter.settled = true;
      clearTimeout(waiter.timer);
      if (waiter.signal && waiter.onAbort) {
        waiter.signal.removeEventListener("abort", waiter.onAbort);
      }
      this.active += 1;
      waiter.resolve(this.createRelease());
    }
  }

  private createRelease(): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.active -= 1;
      this.drain();
    };
  }
}

function abortError(signal: AbortSignal): Error {
  if (signal.reason instanceof Error) return signal.reason;
  return new DOMException("The server read was aborted.", "AbortError");
}

const defaultSchedulerKey = "__oldsparkyServerInternalReadScheduler__";
type SchedulerGlobal = typeof globalThis & Record<string, unknown>;

function defaultScheduler(): ServerInternalReadScheduler {
  const sharedGlobal = globalThis as SchedulerGlobal;
  const existing = sharedGlobal[defaultSchedulerKey];
  if (existing instanceof ServerInternalReadScheduler) return existing;
  const scheduler = new ServerInternalReadScheduler({
    activeLimit: 24,
    pendingLimit: 128,
    queueDeadlineMs: 5_000,
  });
  sharedGlobal[defaultSchedulerKey] = scheduler;
  return scheduler;
}

export function runWithServerInternalRead<T>(
  operation: () => Promise<T>,
  signal?: AbortSignal,
  admissionObserver?: ServerInternalReadAdmissionObserver,
): Promise<T> {
  return defaultScheduler().run(operation, signal, admissionObserver);
}
