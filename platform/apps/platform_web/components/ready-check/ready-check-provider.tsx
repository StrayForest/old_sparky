"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { usePathname } from "next/navigation";

import { useAuth } from "@/components/auth/auth-provider";
import {
  getReadyCheckAgenda,
  getReadyCheckStateProbe,
  platformApiUrl,
  type ReadyCheckAgendaItem,
  type ReadyCheckStateProbe,
} from "@/lib/platform-api";
import { sseRetryDelayMs } from "@/lib/sse-reconnect-policy";

const READY_CHECK_POLL_VISIBLE_MS = 1_500;
const READY_CHECK_HARD_TIMEOUT_MS = 5_000;
const READY_CHECK_SSE_PROOF_REFRESH_SKEW_MS = 60_000;
const configuredSseOpenTimeoutMs = Number(
  process.env.NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS ?? "",
);
const READY_CHECK_SSE_OPEN_TIMEOUT_MS = Number.isFinite(configuredSseOpenTimeoutMs)
  ? Math.min(30_000, Math.max(500, Math.round(configuredSseOpenTimeoutMs)))
  : 1_000;

function tournamentDetailSlug(pathname: string | null): string | null {
  const match = pathname?.match(/^\/tournaments\/([^/]+)\/?$/u);
  if (!match) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

type ReadyCheckLiveState = ReadyCheckStateProbe;

type ReadyCheckContextValue = {
  stateForTournament: (slug: string) => ReadyCheckLiveState | null;
  refreshAgenda: () => void;
};

const ReadyCheckContext = createContext<ReadyCheckContextValue | null>(null);

export function ReadyCheckProvider({ children }: { children: ReactNode }) {
  const { status: authStatus, user } = useAuth();
  const pathname = usePathname();
  const currentTournamentSlug = tournamentDetailSlug(pathname);
  const [agenda, setAgenda] = useState<ReadyCheckAgendaItem[]>([]);
  const [sseTicket, setSseTicket] = useState<string | null>(null);
  const [sseTicketExpiresAt, setSseTicketExpiresAt] = useState<string | null>(null);
  const [liveStates, setLiveStates] = useState<Record<string, ReadyCheckLiveState>>({});
  const [agendaRefreshVersion, setAgendaRefreshVersion] = useState(0);
  const [visibilityVersion, setVisibilityVersion] = useState(0);
  const agendaRef = useRef<ReadyCheckAgendaItem[]>([]);
  const liveStatesRef = useRef<Record<string, ReadyCheckLiveState>>({});

  const refreshAgenda = useCallback(() => {
    setAgendaRefreshVersion((value) => value + 1);
  }, []);

  const updateLiveState = useCallback((tournamentId: string, next: ReadyCheckLiveState) => {
    const updated = {
      ...liveStatesRef.current,
      [tournamentId]: next,
    };
    liveStatesRef.current = updated;
    setLiveStates(updated);
  }, []);

  useEffect(() => {
    agendaRef.current = agenda;
  }, [agenda]);

  useEffect(() => {
    liveStatesRef.current = liveStates;
  }, [liveStates]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      setVisibilityVersion((value) => value + 1);
      if (document.visibilityState === "visible") {
        refreshAgenda();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, [refreshAgenda]);

  useEffect(() => {
    if (authStatus !== "authenticated" || !user) {
      setAgenda([]);
      setSseTicket(null);
      setSseTicketExpiresAt(null);
      liveStatesRef.current = {};
      setLiveStates({});
      return;
    }

    const controller = new AbortController();
    const requestUserId = user.id;
    void getReadyCheckAgenda(controller.signal).then((nextAgenda) => {
      if (controller.signal.aborted || requestUserId !== user.id || !nextAgenda) {
        return;
      }
      const nextIds = new Set(nextAgenda.checks.map((item) => item.tournamentId));
      const retainedStates = Object.fromEntries(
        Object.entries(liveStatesRef.current).filter(([tournamentId]) => nextIds.has(tournamentId)),
      );
      liveStatesRef.current = retainedStates;
      setLiveStates(retainedStates);
      setAgenda(nextAgenda.checks);
      setSseTicket(nextAgenda.sseTicket);
      setSseTicketExpiresAt(nextAgenda.sseTicketExpiresAt);
    }).catch(() => {
      // A transient agenda failure must not tear down an already admitted
      // stream. The next navigation, visibility change, or explicit refresh
      // will retry the inexpensive agenda read.
    });
    return () => controller.abort();
  }, [authStatus, user, agendaRefreshVersion]);

  useEffect(() => {
    // The provider may stay mounted in the root layout so state consumers do
    // not remount across navigation, but a critical stream is a page-scoped
    // resource. Only the visible tournament detail can create its stream.
    const checks = currentTournamentSlug
      ? agendaRef.current.filter((item) => item.slug === currentTournamentSlug)
      : [];
    const streamTicket = sseTicket;
    const streamTicketExpiry = sseTicketExpiresAt;
    let pollingTimer: ReturnType<typeof setTimeout> | null = null;
    let agendaRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let active = true;
    let stream: EventSource | null = null;
    let pollController: AbortController | null = null;
    let streamConnected = false;
    let streamFallbackAvailable = false;
    let streamRetryAt = 0;
    let sseOpenTimer: ReturnType<typeof setTimeout> | null = null;

    const clearPollingTimer = () => {
      if (pollingTimer !== null) {
        clearTimeout(pollingTimer);
        pollingTimer = null;
      }
    };

    const clearSseOpenTimer = () => {
      if (sseOpenTimer !== null) {
        clearTimeout(sseOpenTimer);
        sseOpenTimer = null;
      }
    };

    const clearAgendaRefreshTimer = () => {
      if (agendaRefreshTimer !== null) {
        clearTimeout(agendaRefreshTimer);
        agendaRefreshTimer = null;
      }
    };

    const closeStream = () => {
      const currentStream = stream;
      stream = null;
      streamConnected = false;
      streamFallbackAvailable = false;
      clearSseOpenTimer();
      if (currentStream !== null) {
        currentStream.close();
      }
    };

    const liveStateFor = (item: ReadyCheckAgendaItem): ReadyCheckLiveState | undefined => (
      liveStatesRef.current[item.tournamentId]
    );

    const probe = async (item: ReadyCheckAgendaItem, signal: AbortSignal) => {
      try {
        const next = await getReadyCheckStateProbe(item.slug, item.stateTicket, signal);
        if (active && !signal.aborted && next) {
          updateLiveState(item.tournamentId, next);
          if (!hasPendingStreamDemand(Date.now())) {
            closeStream();
          }
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          // Missing/temporarily unavailable state remains waiting. The next
          // visible cadence retries without loading the tournament workspace.
        }
      }
    };

    const probeUnresolvedChecks = (items: ReadyCheckAgendaItem[]) => {
      for (const item of items) {
        if (liveStateFor(item)?.status === "active" || liveStateFor(item)?.status === "closed") {
          continue;
        }
        const controller = new AbortController();
        void probe(item, controller.signal).finally(() => controller.abort());
      }
    };

    const dueForPolling = (now: number) => checks.filter((item) => {
      const startsAt = Date.parse(item.readyCheckStartsAt);
      const endsAt = Date.parse(item.readyCheckEndsAt);
      return Number.isFinite(startsAt)
        && Number.isFinite(endsAt)
        && now >= startsAt
        && now < endsAt + READY_CHECK_HARD_TIMEOUT_MS
        && (liveStateFor(item)?.status ?? "waiting") === "waiting"
        && (item.admissionMode === "polling" || (!streamConnected && streamFallbackAvailable));
    });

    const shouldOpenStream = (now: number) => checks.some((item) => {
      const startsAt = Date.parse(item.readyCheckStartsAt);
      const openAt = Date.parse(item.admissionOpenAt);
      const endsAt = Date.parse(item.readyCheckEndsAt);
      if (
        !Number.isFinite(startsAt)
        || !Number.isFinite(openAt)
        || !Number.isFinite(endsAt)
        || now >= endsAt + READY_CHECK_HARD_TIMEOUT_MS
        || now >= startsAt
        || liveStateFor(item)?.status === "active"
        || liveStateFor(item)?.status === "closed"
        || item.admissionMode === "polling"
      ) {
        return false;
      }
      return now >= openAt;
    });

    const hasPendingStreamDemand = (now: number) => checks.some((item) => {
      if (
        item.admissionMode === "polling"
        || liveStateFor(item)?.status === "active"
        || liveStateFor(item)?.status === "closed"
      ) {
        return false;
      }
      const startsAt = Date.parse(item.readyCheckStartsAt);
      const openAt = Date.parse(item.admissionOpenAt);
      const endsAt = Date.parse(item.readyCheckEndsAt);
      if (
        !Number.isFinite(startsAt)
        || !Number.isFinite(openAt)
        || !Number.isFinite(endsAt)
        || now < openAt
        || now >= endsAt + READY_CHECK_HARD_TIMEOUT_MS
      ) {
        return false;
      }
      return true;
    });

    const scheduleAgendaRefreshBeforeStreamProofExpiry = () => {
      clearAgendaRefreshTimer();
      if (!streamTicket || !streamTicketExpiry) {
        return;
      }
      const expiresAt = Date.parse(streamTicketExpiry);
      if (!Number.isFinite(expiresAt)) {
        return;
      }
      const latestCheckEnd = Math.max(
        ...checks
          .filter((item) => item.admissionMode !== "polling")
          .map((item) => Date.parse(item.readyCheckEndsAt))
          .filter(Number.isFinite),
      );
      // A proof that naturally ends with its last Ready Check needs no
      // refresh. Refresh only when the bounded proof horizon ends before a
      // check still represented by the agenda.
      if (!Number.isFinite(latestCheckEnd) || latestCheckEnd <= expiresAt) {
        return;
      }
      const refreshAt = expiresAt - READY_CHECK_SSE_PROOF_REFRESH_SKEW_MS;
      const delay = Math.max(0, refreshAt - Date.now());
      agendaRefreshTimer = setTimeout(() => {
        agendaRefreshTimer = null;
        refreshAgenda();
      }, delay);
    };

    function scheduleNext() {
      clearPollingTimer();
      if (!active || document.visibilityState === "hidden") {
        return;
      }
      const now = Date.now();
      if (stream && !hasPendingStreamDemand(now)) {
        closeStream();
      }
      let nextAt = Number.POSITIVE_INFINITY;
      if (dueForPolling(now).length > 0) {
        nextAt = now + READY_CHECK_POLL_VISIBLE_MS;
      }
      for (const item of checks) {
        if (liveStateFor(item)?.status === "active" || liveStateFor(item)?.status === "closed") {
          continue;
        }
        const startsAt = Date.parse(item.readyCheckStartsAt);
        const openAt = Date.parse(item.admissionOpenAt);
        const endsAt = Date.parse(item.readyCheckEndsAt);
        if (!Number.isFinite(startsAt) || !Number.isFinite(openAt) || !Number.isFinite(endsAt)) {
          continue;
        }
        if (now < startsAt) {
          nextAt = Math.min(nextAt, startsAt);
        }
        if (item.admissionMode !== "polling" && now < openAt) {
          nextAt = Math.min(nextAt, openAt);
        }
        if (now < endsAt + READY_CHECK_HARD_TIMEOUT_MS) {
          nextAt = Math.min(nextAt, endsAt + READY_CHECK_HARD_TIMEOUT_MS);
        }
      }
      if (!stream && shouldOpenStream(now)) {
        nextAt = Math.min(nextAt, now < streamRetryAt ? streamRetryAt : now);
      }
      if (!Number.isFinite(nextAt)) {
        return;
      }
      pollingTimer = setTimeout(() => {
        pollingTimer = null;
        void runCycle().finally(scheduleNext);
      }, Math.max(0, nextAt - now));
    }

    const failStream = (source: EventSource) => {
      if (stream !== source) {
        return;
      }
      closeStream();
      const streamTicketExpired = Boolean(
        streamTicketExpiry
        && Number.isFinite(Date.parse(streamTicketExpiry))
        && Date.parse(streamTicketExpiry) <= Date.now()
      );
      // Keep fallback probes available immediately, but spread the next SSE
      // admission over the measured recovery window so mass disconnects do
      // not create a synchronized reconnect herd.
      streamRetryAt = streamTicketExpired
        ? Number.POSITIVE_INFINITY
        : Date.now() + sseRetryDelayMs();
      streamFallbackAvailable = true;
      if (streamTicketExpired) {
        refreshAgenda();
      }
      scheduleNext();
    };

    const openStream = () => {
      if (!streamTicket || stream || document.visibilityState === "hidden") {
        return;
      }
      const source = new EventSource(
        platformApiUrl(`/ready-check/events?ticket=${encodeURIComponent(streamTicket)}`),
        { withCredentials: true },
      );
      stream = source;
      source.onopen = () => {
        if (stream !== source) {
          return;
        }
        streamConnected = true;
        clearSseOpenTimer();
        streamRetryAt = 0;
        // If the stream was established around T, the connected frame may
        // legitimately follow the relay event. Catch up from the
        // user-scoped authoritative state instead of relying on relay history.
        probeUnresolvedChecks(
          checks.filter((item) => {
            const startsAt = Date.parse(item.readyCheckStartsAt);
            return Number.isFinite(startsAt) && startsAt <= Date.now();
          }),
        );
      };
      source.addEventListener("ready_check", (event) => {
        if (!(event instanceof MessageEvent) || typeof event.data !== "string") {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as {
            tournament_id?: string;
            revision?: number;
            status?: string;
          };
          const item = checks.find((candidate) => candidate.tournamentId === payload.tournament_id);
          if (!item || !payload.tournament_id || !["active", "closed"].includes(payload.status ?? "")) {
            return;
          }
          // The global event is only a wake-up hint. The user-scoped Redis
          // projection is authoritative so a participant revoked after the
          // agenda read cannot receive an active button from a stale stream.
          probeUnresolvedChecks([item]);
        } catch {
          // Ignore malformed relay data; the revision probe remains the
          // authoritative fallback for the current user's ticket.
        }
      });
      source.addEventListener("resync", () => {
        // A bounded relay can evict events before a slow subscriber consumes
        // them. Probe every unresolved visible check so a healthy connection
        // can recover without enabling periodic polling.
        probeUnresolvedChecks(checks);
      });
      source.onerror = () => {
        failStream(source);
      };
      sseOpenTimer = setTimeout(() => {
        if (stream === source && !streamConnected) {
          failStream(source);
        }
      }, READY_CHECK_SSE_OPEN_TIMEOUT_MS);
    };

    const runCycle = async () => {
      if (!active || document.visibilityState === "hidden") {
        return;
      }
      const now = Date.now();
      if (stream && !hasPendingStreamDemand(now)) {
        closeStream();
      }
      const due = dueForPolling(now).filter((item, index, items) => (
        items.findIndex((candidate) => candidate.tournamentId === item.tournamentId) === index
      ));
      if (due.length > 0) {
        pollController?.abort();
        pollController = new AbortController();
        await Promise.all(due.map((item) => probe(item, pollController!.signal)));
      }
      if (active && shouldOpenStream(Date.now()) && Date.now() >= streamRetryAt) {
        openStream();
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        clearPollingTimer();
        clearAgendaRefreshTimer();
        pollController?.abort();
        closeStream();
        return;
      }
      scheduleAgendaRefreshBeforeStreamProofExpiry();
      void runCycle().finally(scheduleNext);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    scheduleAgendaRefreshBeforeStreamProofExpiry();
    if (document.visibilityState === "visible") {
      void runCycle().finally(scheduleNext);
    }

    return () => {
      active = false;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearPollingTimer();
      clearAgendaRefreshTimer();
      pollController?.abort();
      closeStream();
    };
  }, [agenda, currentTournamentSlug, refreshAgenda, sseTicket, sseTicketExpiresAt, updateLiveState, visibilityVersion]);

  const stateForTournament = useCallback((slug: string) => {
    const item = agendaRef.current.find((candidate) => candidate.slug === slug);
    return item ? liveStates[item.tournamentId] ?? null : null;
  }, [liveStates]);

  const value = useMemo(
    () => ({ stateForTournament, refreshAgenda }),
    [refreshAgenda, stateForTournament],
  );

  return <ReadyCheckContext.Provider value={value}>{children}</ReadyCheckContext.Provider>;
}

export function useReadyCheckState(slug: string): ReadyCheckLiveState | null {
  const context = useContext(ReadyCheckContext);
  if (!context) {
    throw new Error("useReadyCheckState must be used inside ReadyCheckProvider.");
  }
  return context.stateForTournament(slug);
}

export function useReadyCheckAgendaRefresh(): () => void {
  const context = useContext(ReadyCheckContext);
  if (!context) {
    throw new Error("useReadyCheckAgendaRefresh must be used inside ReadyCheckProvider.");
  }
  return context.refreshAgenda;
}
