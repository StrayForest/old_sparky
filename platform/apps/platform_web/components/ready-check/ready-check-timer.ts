"use client";

import { useEffect, useState } from "react";
import type {
  PlatformTournamentDeadlockReadyCheckState,
  PlatformTournamentDeadlockReadyRound,
} from "@/lib/platform-types";

export type ReadyCheckPhase = "unknown" | "waiting" | "active" | "finished";

/** Calculate the lifecycle phase on a server-relative timeline. */
export function readyCheckPhaseAt(
  estimatedServerNowMs: number,
  startsAtMs: number | null,
  endsAtMs: number | null,
): ReadyCheckPhase {
  if (
    !Number.isFinite(estimatedServerNowMs)
    || startsAtMs === null
    || endsAtMs === null
    || !Number.isFinite(startsAtMs)
    || !Number.isFinite(endsAtMs)
    || endsAtMs <= startsAtMs
  ) {
    return "unknown";
  }
  if (estimatedServerNowMs < startsAtMs) {
    return "waiting";
  }
  if (estimatedServerNowMs < endsAtMs) {
    return "active";
  }
  return "finished";
}

function timestampMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Select the durable round that belongs to the currently displayed schedule.
 * Historical closed rounds remain useful to organizers, but must not disable
 * or pre-check a newly scheduled Ready Check in the participant UI.
 */
export function currentReadyCheckRound(
  state: PlatformTournamentDeadlockReadyCheckState | null | undefined,
  startsAt: string | null | undefined,
): PlatformTournamentDeadlockReadyRound | null {
  if (!state) {
    return null;
  }
  if (state.active_round) {
    return state.active_round;
  }
  const latestRound = state.latest_round;
  if (!latestRound) {
    return null;
  }
  if (latestRound.status === "active") {
    return latestRound;
  }
  const startsAtMs = timestampMs(startsAt);
  const closedAtMs = timestampMs(latestRound.closed_at);
  if (startsAtMs === null || closedAtMs === null || closedAtMs >= startsAtMs) {
    return latestRound;
  }
  return null;
}

function phaseFromInitialPayload(
  serverTime: string | null | undefined,
  startsAt: string | null | undefined,
  endsAt: string | null | undefined,
): ReadyCheckPhase {
  return readyCheckPhaseAt(
    timestampMs(serverTime) ?? Number.NaN,
    timestampMs(startsAt),
    timestampMs(endsAt),
  );
}

export function useReadyCheckPhase(
  serverTime: string | null | undefined,
  startsAt: string | null | undefined,
  endsAt: string | null | undefined,
): ReadyCheckPhase {
  const [phase, setPhase] = useState<ReadyCheckPhase>(() => (
    phaseFromInitialPayload(serverTime, startsAt, endsAt)
  ));

  useEffect(() => {
    const serverTimeAtLoadMs = timestampMs(serverTime);
    const startsAtMs = timestampMs(startsAt);
    const endsAtMs = timestampMs(endsAt);
    if (serverTimeAtLoadMs === null || startsAtMs === null || endsAtMs === null) {
      setPhase("unknown");
      return;
    }

    const monotonicAtLoad = performance.now();
    let timerId: number | null = null;
    let disposed = false;

    const clearBoundaryTimer = () => {
      if (timerId !== null) {
        window.clearTimeout(timerId);
        timerId = null;
      }
    };

    const recomputeReadyCheckFromTime = () => {
      if (disposed) {
        return;
      }
      const estimatedServerNowMs = serverTimeAtLoadMs + (performance.now() - monotonicAtLoad);
      setPhase(readyCheckPhaseAt(estimatedServerNowMs, startsAtMs, endsAtMs));
      clearBoundaryTimer();

      const nextBoundaryMs = estimatedServerNowMs < startsAtMs
        ? startsAtMs
        : estimatedServerNowMs < endsAtMs
          ? endsAtMs
          : null;
      if (nextBoundaryMs !== null) {
        timerId = window.setTimeout(
          recomputeReadyCheckFromTime,
          Math.max(0, nextBoundaryMs - estimatedServerNowMs),
        );
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        recomputeReadyCheckFromTime();
      }
    };
    const handlePageShow = () => recomputeReadyCheckFromTime();

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pageshow", handlePageShow);
    recomputeReadyCheckFromTime();

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pageshow", handlePageShow);
      clearBoundaryTimer();
    };
  }, [endsAt, serverTime, startsAt]);

  return phase;
}
