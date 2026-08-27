"use client";

import { ZoomIn, ZoomOut } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

import { useI18n } from "@/components/i18n-provider";
import { useCspNonce } from "@/components/security/csp-nonce-provider";
import {
  getTournamentBracket,
  PlatformApiError,
  platformApiMessage,
  platformApiRequest,
  platformApiUrl,
} from "@/lib/platform-api";
import type { Bracket, Match, Team } from "@/lib/types";
import { sseRetryDelayMs } from "@/lib/sse-reconnect-policy";

const MATCH_W = 272;
const MATCH_H_VIEW = 146;
const MATCH_H_MANAGE = MATCH_H_VIEW + 58;
const GAP_X = 70;
const GAP_Y = 26;
const MATCH_FRAME_CENTER_Y = 92;
const BRACKET_INITIAL_LOAD_JITTER_MS = 3000;
const BRACKET_EVENT_JITTER_MS = 3000;
const BRACKET_POLL_BASE_MS = 10_000;
const BRACKET_POLL_JITTER_MS = 3000;
const BRACKET_POLL_MIN_MS = 3_000;
const BRACKET_POLL_MAX_MS = 60_000;
const SSE_OPEN_JITTER_MS = 500;
const SSE_FALLBACK_POLL_JITTER_MS = 500;
const configuredSseOpenTimeoutMs = Number(
  process.env.NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS ?? "",
);
const SSE_OPEN_TIMEOUT_MS = Number.isFinite(configuredSseOpenTimeoutMs)
  ? Math.min(30_000, Math.max(500, Math.round(configuredSseOpenTimeoutMs)))
  : 1_000;
const PANNING_IGNORE_SELECTOR = "button,input,select,textarea,a,[role='button']";

type LayoutMatch = {
  match: Match;
  x: number;
  y: number;
};

type RoundPosition = {
  cxLeft: number;
  cxRight: number;
  cy: number;
};

type ScoreDraft = {
  home: string;
  away: string;
};

type ScheduleDraft = {
  date: string;
  time: string;
};

type ScheduleBounds = {
  min: string;
  max: string | null;
};

const moscowScheduleFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Europe/Moscow",
});

type PanState = {
  pointerId: number;
  x: number;
  y: number;
  scrollLeft: number;
  scrollTop: number;
} | null;

function bracketPollDelayMs(bracket: Bracket | null): number | null {
  const serverDelayMs = bracket?.nextPollAfterMs;
  if (serverDelayMs === 0) {
    return null;
  }
  const baseDelayMs = typeof serverDelayMs === "number" && Number.isFinite(serverDelayMs)
    ? serverDelayMs
    : BRACKET_POLL_BASE_MS;
  const clampedDelayMs = Math.min(
    BRACKET_POLL_MAX_MS,
    Math.max(BRACKET_POLL_MIN_MS, Math.round(baseDelayMs)),
  );
  return clampedDelayMs + Math.floor(Math.random() * BRACKET_POLL_JITTER_MS);
}

export function BracketBoard({
  initialBracket,
  slug,
}: {
  initialBracket?: Bracket | null;
  slug: string;
}) {
  const { t } = useI18n();
  const nonce = useCspNonce();
  const [bracket, setBracket] = useState<Bracket | null>(initialBracket ?? null);
  const [zoom, setZoom] = useState(90);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [panning, setPanning] = useState(false);
  const initialRefreshRequested = useRef(false);
  const shellRef = useRef<HTMLElement | null>(null);
  const panState = useRef<PanState>(null);
  const bracketRef = useRef<Bracket | null>(initialBracket ?? null);
  const activeRefreshController = useRef<AbortController | null>(null);
  const refreshInFlight = useRef<Promise<Bracket | null> | null>(null);
  const refreshGeneration = useRef(0);
  const bracketEtag = useRef<string | null>(null);

  useEffect(() => {
    bracketRef.current = bracket;
  }, [bracket]);

  const abortRefresh = useCallback(() => {
    refreshGeneration.current += 1;
    activeRefreshController.current?.abort();
    activeRefreshController.current = null;
    refreshInFlight.current = null;
  }, []);

  const refresh = useCallback(async (): Promise<Bracket | null> => {
    if (refreshInFlight.current) {
      return refreshInFlight.current;
    }
    const controller = new AbortController();
    activeRefreshController.current = controller;
    const requestGeneration = ++refreshGeneration.current;
    const request = getTournamentBracket(slug, {}, {
      teamsView: "summary",
      signal: controller.signal,
      ifNoneMatch: bracketEtag.current,
      cachedBracket: bracketRef.current,
      onResponse: (response) => {
        const etag = response.headers.get("etag");
        if (etag) {
          bracketEtag.current = etag;
        }
      },
    })
      .then((next) => {
        if (controller.signal.aborted || requestGeneration !== refreshGeneration.current) {
          return next;
        }
        if (!next) {
          setError(t("bracket.loadFailed"));
          return null;
        }
        setError(null);
        setBracket((current) => {
          if (!current || next.revision >= current.revision) {
            bracketRef.current = next;
            return next;
          }
          return current;
        });
        return next;
      })
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(t("bracket.loadFailed"));
        }
        return null;
      })
      .finally(() => {
        if (refreshInFlight.current === request) {
          refreshInFlight.current = null;
        }
        if (activeRefreshController.current === controller) {
          activeRefreshController.current = null;
        }
      });
    refreshInFlight.current = request;
    return request;
  }, [slug, t]);

  useEffect(() => {
    bracketEtag.current = null;
  }, [slug]);

  useEffect(() => {
    const shouldLoadBracketMatches = !bracket || (
      bracket.matches.length === 0
      && (bracket.status === "ready" || bracket.revision > 0)
    );
    if (!shouldLoadBracketMatches || initialRefreshRequested.current) {
      return;
    }
    initialRefreshRequested.current = true;
    const timer = setTimeout(
      () => {
        if (document.visibilityState !== "hidden") {
          void refresh();
        }
      },
      Math.floor(Math.random() * BRACKET_INITIAL_LOAD_JITTER_MS),
    );
    return () => clearTimeout(timer);
  }, [bracket, refresh]);

  useEffect(() => {
    let pollingTimer: ReturnType<typeof setTimeout> | null = null;
    let pollingDelayTimer: ReturnType<typeof setTimeout> | null = null;
    let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let pollingActive = false;
    const refreshIfVisible = async () => {
      if (document.visibilityState === "hidden") {
        abortRefresh();
        return null;
      }
      return refresh();
    };
    const clearEventRefresh = () => {
      if (eventRefreshTimer !== null) {
        clearTimeout(eventRefreshTimer);
        eventRefreshTimer = null;
      }
    };
    const scheduleEventRefresh = () => {
      clearEventRefresh();
      eventRefreshTimer = setTimeout(
        () => void refreshIfVisible(),
        Math.floor(Math.random() * BRACKET_EVENT_JITTER_MS),
      );
    };
    const schedulePollingTick = (sourceBracket: Bracket | null = bracketRef.current) => {
      if (!pollingActive) {
        return;
      }
      const delayMs = bracketPollDelayMs(sourceBracket);
      if (delayMs === null) {
        stopPolling();
        return;
      }
      pollingTimer = setTimeout(async () => {
        pollingTimer = null;
        const next = await refreshIfVisible();
        if (!pollingActive) {
          return;
        }
        schedulePollingTick(next ?? bracketRef.current);
      }, delayMs);
    };
    const startPolling = (immediate = false) => {
      if (pollingActive || pollingTimer !== null || pollingDelayTimer !== null) {
        return;
      }
      if (bracketPollDelayMs(bracketRef.current) === null) {
        return;
      }
      pollingActive = true;
      const delayMs = immediate
        ? Math.floor(Math.random() * SSE_FALLBACK_POLL_JITTER_MS)
        : Math.floor(Math.random() * BRACKET_POLL_JITTER_MS);
      pollingDelayTimer = setTimeout(async () => {
        pollingDelayTimer = null;
        const next = await refreshIfVisible();
        if (!pollingActive) {
          return;
        }
        schedulePollingTick(next ?? bracketRef.current);
      }, delayMs);
    };
    const stopPolling = () => {
      pollingActive = false;
      if (pollingDelayTimer !== null) {
        clearTimeout(pollingDelayTimer);
        pollingDelayTimer = null;
      }
      if (pollingTimer !== null) {
        clearTimeout(pollingTimer);
        pollingTimer = null;
      }
    };
    let source: EventSource | null = null;
    let sharedPort: MessagePort | null = null;
    let sseActive = false;
    let sseOpenDelayTimer: ReturnType<typeof setTimeout> | null = null;
    let sseOpenTimer: ReturnType<typeof setTimeout> | null = null;
    let sseRetryTimer: ReturnType<typeof setTimeout> | null = null;
    let sseRetryNotBefore = 0;

    const clearSseOpenTimer = () => {
      if (sseOpenTimer !== null) {
        clearTimeout(sseOpenTimer);
        sseOpenTimer = null;
      }
    };

    const closeSource = () => {
      if (sseOpenDelayTimer !== null) {
        clearTimeout(sseOpenDelayTimer);
        sseOpenDelayTimer = null;
      }
      clearSseOpenTimer();
      if (sharedPort !== null) {
        sharedPort.postMessage({ type: "unsubscribe", key: slug });
        sharedPort.onmessage = null;
        sharedPort.close();
        sharedPort = null;
      }
      if (source !== null) {
        source.close();
        source = null;
      }
      sseActive = false;
    };

    const fallBackToPolling = () => {
      closeSource();
      // Keep polling available immediately, but spread the next admission
      // attempt across the measured safe establishment window. This applies
      // equally to a timeout, 429/503, network failure and mass disconnect.
      sseRetryNotBefore = Date.now() + sseRetryDelayMs();
      startPolling(true);
      scheduleSseRetry();
    };

    const scheduleSseRetry = () => {
      if (sseRetryTimer !== null) {
        clearTimeout(sseRetryTimer);
      }
      const delayMs = Math.max(0, sseRetryNotBefore - Date.now());
      sseRetryTimer = setTimeout(() => {
        sseRetryTimer = null;
        if (document.visibilityState === "visible") {
          void refreshIfVisible().finally(openSse);
        }
      }, delayMs);
    };

    const openSse = () => {
      if (
        sseActive
        || document.visibilityState !== "visible"
        || Date.now() < sseRetryNotBefore
      ) {
        return;
      }
      sseOpenDelayTimer = setTimeout(() => {
        sseOpenDelayTimer = null;
        if (
          document.visibilityState !== "visible"
          || Date.now() < sseRetryNotBefore
          || sseActive
        ) {
          return;
        }
        const ticket = bracketRef.current?.sseAdmissionTicket;
        const streamUrl = platformApiUrl(`/tournaments/${slug}/bracket/events`);
        const url = ticket
          ? `${streamUrl}?ticket=${encodeURIComponent(ticket)}`
          : streamUrl;
        sseActive = true;
        const onOpen = () => {
          clearSseOpenTimer();
          stopPolling();
        };
        const onBracket = () => {
          scheduleEventRefresh();
        };
        const onError = () => {
          // Close the source and use revision polling during cooldown. This
          // also stops a shared worker from reconnecting every browser tab.
          fallBackToPolling();
        };
        sseOpenTimer = setTimeout(() => {
          // A slow edge handshake is not useful to a visitor. Abort it before
          // an upstream queue can turn a normal fallback into a multi-minute
          // wait, then retry only after the cooldown.
          if (sseActive) {
            fallBackToPolling();
          }
        }, SSE_OPEN_TIMEOUT_MS);
        try {
          if (typeof SharedWorker !== "undefined") {
            const worker = new SharedWorker("/sse-shared-worker.js");
            worker.onerror = onError;
            sharedPort = worker.port;
            sharedPort.onmessage = (event: MessageEvent<{
              key?: string;
              type?: string;
            }>) => {
              if (event.data.key !== slug) {
                return;
              }
              if (event.data.type === "open") {
                onOpen();
              } else if (event.data.type === "bracket") {
                onBracket();
              } else if (event.data.type === "error") {
                onError();
              }
            };
            sharedPort.start();
            sharedPort.postMessage({ type: "subscribe", key: slug, url });
          } else {
            source = new EventSource(url, { withCredentials: true });
            source.onopen = onOpen;
            source.addEventListener("bracket", onBracket);
            source.onerror = onError;
          }
        } catch {
          onError();
        }
      }, Math.floor(Math.random() * SSE_OPEN_JITTER_MS));
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void refreshIfVisible();
        openSse();
      } else {
        abortRefresh();
        closeSource();
        stopPolling();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    openSse();

    return () => {
      closeSource();
      if (sseRetryTimer !== null) {
        clearTimeout(sseRetryTimer);
      }
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearEventRefresh();
      stopPolling();
      abortRefresh();
    };
  }, [abortRefresh, refresh, slug]);

  const mutate = useCallback(async (
    path: string,
    init: RequestInit,
  ) => {
    setBusy(true);
    setError(null);
    try {
      await platformApiRequest(path, init);
      await refresh();
      return true;
    } catch (caught) {
      if (caught instanceof PlatformApiError && caught.status === 409) {
        await refresh();
      }
      setError(platformApiMessage(caught, t("bracket.actionFailed")));
      return false;
    } finally {
      setBusy(false);
    }
  }, [refresh, t]);

  const beginPan = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) {
      return;
    }
    const target = event.target as HTMLElement | null;
    if (target?.closest(PANNING_IGNORE_SELECTOR)) {
      return;
    }
    const shell = shellRef.current;
    if (!shell) {
      return;
    }
    panState.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      scrollLeft: shell.scrollLeft,
      scrollTop: shell.scrollTop,
    };
    shell.setPointerCapture(event.pointerId);
    setPanning(true);
  }, []);

  const movePan = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const state = panState.current;
    const shell = shellRef.current;
    if (!state || !shell || state.pointerId !== event.pointerId) {
      return;
    }
    event.preventDefault();
    shell.scrollLeft = state.scrollLeft - (event.clientX - state.x);
    shell.scrollTop = state.scrollTop - (event.clientY - state.y);
  }, []);

  const endPan = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const shell = shellRef.current;
    if (panState.current?.pointerId === event.pointerId) {
      panState.current = null;
      setPanning(false);
      if (shell?.hasPointerCapture(event.pointerId)) {
        shell.releasePointerCapture(event.pointerId);
      }
    }
  }, []);

  if (!bracket || bracket.matches.length === 0) {
    const loadingBracketMatches = !error && (
      !bracket || bracket.status === "ready" || bracket.revision > 0
    );
    return (
      <section className="bracket-empty-state" data-testid="bracket-empty">
        <strong>{loadingBracketMatches ? t("deadlock.refreshing") : t("tournament.bracketEmpty")}</strong>
        <span>{error ?? t("bracket.noFabricatedData")}</span>
      </section>
    );
  }

  const terminalTournament = bracket.tournamentStatus === "completed"
    || bracket.tournamentStatus === "cancelled";
  const canManageMatches = bracket.capabilities.canManage && !terminalTournament;
  const canScheduleMatches = bracket.capabilities.canScheduleMatches && !terminalTournament;
  const canReportMatches = bracket.capabilities.canReportMatches && !terminalTournament;
  const resolvedMatchHeight = canManageMatches ? MATCH_H_MANAGE : MATCH_H_VIEW;
  const model = buildLayout(bracket, resolvedMatchHeight);
  const scale = zoom / 100;
  const layoutScope = `bracket-layout-${stableNumericId(bracket.tournamentId)}`;
  const layoutCss = bracketLayoutCss(layoutScope, model, scale, resolvedMatchHeight);

  return (
    <div className="bracket-wrap" id={layoutScope}>
      <style nonce={nonce ?? undefined}>{layoutCss}</style>
      <div className="floating-zoom" aria-label={t("bracket.zoomControls")}>
        <button
          className="floating-zoom-button"
          type="button"
          onClick={() => setZoom((value) => Math.min(210, value + 20))}
          aria-label={t("tournament.bracketZoomIn")}
        >
          <ZoomIn size={20} />
        </button>
        <button
          className="floating-zoom-button"
          type="button"
          onClick={() => setZoom((value) => Math.max(30, value - 20))}
          aria-label={t("tournament.bracketZoomOut")}
        >
          <ZoomOut size={20} />
        </button>
      </div>

      {error ? <div className="bracket-error" role="alert">{error}</div> : null}

      <section
        className={["bracket-shell", panning ? "is-panning" : ""].filter(Boolean).join(" ")}
        aria-label={t("tournament.bracketTitle")}
        data-testid="bracket-shell"
        onPointerCancel={endPan}
        onPointerDown={beginPan}
        onPointerLeave={endPan}
        onPointerMove={movePan}
        onPointerUp={endPan}
        ref={shellRef}
      >
        <div
          className="bracket-viewport"
          data-testid="bracket-viewport"
        >
          <div className="bracket-canvas">
            <svg
              className="bracket-lines"
              width={model.width}
              height={model.height}
              viewBox={`0 0 ${model.width} ${model.height}`}
              aria-hidden="true"
            >
              {model.paths.map((path) => <path d={path} key={path} />)}
            </svg>
            {model.matches.map(({ match }, matchIndex) => (
              <MatchCard
                bracket={bracket}
                busy={busy}
                canReportMatches={canReportMatches}
                canScheduleMatches={canScheduleMatches}
                layoutIndex={matchIndex}
                key={match.id}
                match={match}
                mutate={mutate}
                slug={slug}
                t={t}
              />
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

function MatchCard({
  bracket,
  busy,
  canReportMatches,
  canScheduleMatches,
  layoutIndex,
  match,
  mutate,
  slug,
  t,
}: {
  bracket: Bracket;
  busy: boolean;
  canReportMatches: boolean;
  canScheduleMatches: boolean;
  layoutIndex: number;
  match: Match;
  mutate: (path: string, init: RequestInit) => Promise<boolean>;
  slug: string;
  t: (key: string, params?: Record<string, string | number | null | undefined>) => string;
}) {
  const [score, setScore] = useState<ScoreDraft>({
    home: match.scoreA === null ? "" : String(match.scoreA),
    away: match.scoreB === null ? "" : String(match.scoreB),
  });
  const [schedule, setSchedule] = useState<ScheduleDraft>(() => scheduleDraftFromIso(match.scheduledAt));
  const scheduleEditedRef = useRef(false);
  const scheduleMatchIdRef = useRef(match.id);

  useEffect(() => {
    setScore({
      home: match.scoreA === null ? "" : String(match.scoreA),
      away: match.scoreB === null ? "" : String(match.scoreB),
    });
  }, [match.scoreA, match.scoreB, match.id]);

  useEffect(() => {
    if (scheduleMatchIdRef.current !== match.id) {
      scheduleMatchIdRef.current = match.id;
      scheduleEditedRef.current = false;
    }
    const serverSchedule = scheduleDraftFromIso(match.scheduledAt);
    setSchedule((current) => {
      const currentIso = scheduleDraftToIso(current);
      const matchesServerSchedule = Boolean(
        currentIso
        && match.scheduledAt
        && Date.parse(currentIso) === Date.parse(match.scheduledAt)
      );
      if (matchesServerSchedule) {
        scheduleEditedRef.current = false;
        return serverSchedule;
      }
      if (scheduleEditedRef.current) {
        return current;
      }
      return serverSchedule;
    });
  }, [match.id, match.scheduledAt]);

  const slots = [
    { teamId: match.teamAId, label: match.homeLabel },
    { teamId: match.teamBId, label: match.awayLabel },
  ];
  const reportStatusAllowed = match.status === "scheduled" || match.status === "live";
  const scoreValid = scoreMatchesFormat(score, match.matchFormat);
  const scheduleBounds = matchScheduleBounds(bracket.matches, match);
  const scheduleIso = scheduleDraftToIso(schedule);
  const scheduleChanged = Boolean(
    scheduleIso
    && (!match.scheduledAt || Date.parse(scheduleIso) !== Date.parse(match.scheduledAt))
  );
  const scheduleValid = Boolean(
    scheduleIso
    && scheduleIso >= scheduleBounds.min
    && (!scheduleBounds.max || scheduleIso <= scheduleBounds.max)
  );

  const report = async () => {
    if (!canReportMatches || !scoreValid) {
      return;
    }
    await mutate(`/tournaments/${slug}/matches/${match.id}/report`, {
      method: "POST",
      body: JSON.stringify({
        home_score: Number(score.home),
        away_score: Number(score.away),
        expected_revision: bracket.revision,
      }),
    });
  };

  const saveSchedule = async () => {
    if (!canScheduleMatches || !scheduleIso || !scheduleValid) {
      return;
    }
    await mutate(`/tournaments/${slug}/matches/${match.id}/schedule`, {
      method: "PATCH",
      body: JSON.stringify({
        scheduled_at: scheduleIso,
        expected_revision: bracket.revision,
      }),
    });
  };

  const selectedDate = schedule.date;
  const minDraft = scheduleDraftFromIso(scheduleBounds.min);
  const maxDraft = scheduleBounds.max ? scheduleDraftFromIso(scheduleBounds.max) : null;

  return (
    <article
      className="match"
      data-bracket-match-index={layoutIndex}
      data-testid="bracket-match"
    >
      <div className="match-schedule">
        {canScheduleMatches ? (
          <div className="match-schedule-controls">
            <input
              aria-label={`${t("bracket.matchDate")} ${match.roundNumber}-${match.matchOrder}`}
              disabled={busy}
              max={maxDraft?.date}
              min={minDraft.date}
              onInput={(event) => {
                const date = event.currentTarget.value;
                scheduleEditedRef.current = true;
                setSchedule((current) => ({ ...current, date }));
              }}
              type="date"
              value={schedule.date}
            />
            <input
              aria-label={`${t("bracket.matchTime")} ${match.roundNumber}-${match.matchOrder}`}
              disabled={busy}
              max={maxDraft && selectedDate === maxDraft.date ? maxDraft.time : undefined}
              min={selectedDate === minDraft.date ? minDraft.time : undefined}
              onInput={(event) => {
                const time = event.currentTarget.value;
                scheduleEditedRef.current = true;
                setSchedule((current) => ({ ...current, time }));
              }}
              step="600"
              type="time"
              value={schedule.time}
            />
            <button
              aria-label={t("bracket.saveSchedule")}
              disabled={busy || !scheduleValid || !scheduleChanged}
              onClick={() => void saveSchedule()}
              type="button"
            >
              ОК
            </button>
          </div>
        ) : match.scheduledAt ? (
          <time dateTime={match.scheduledAt}>{formatMatchSchedule(match.scheduledAt)}</time>
        ) : null}
      </div>

      <div className="match-frame">
        <header className="match-meta">
          <strong>{match.matchFormat.toUpperCase()}</strong>
        </header>
        {slots.map((slot, sideIndex) => {
          const team = slot.teamId
            ? bracket.teams.find((item) => item.id === slot.teamId) ?? null
            : null;
          const scoreValue = sideIndex === 0 ? match.scoreA : match.scoreB;
          return (
            <TeamSlot
              isWinner={Boolean(team && match.winnerTeamId === team.id)}
              key={`${match.id}-${sideIndex}`}
              label={team?.name ?? (slot.teamId ? slot.label : "")}
              score={scoreValue}
              team={team}
            />
          );
        })}
      </div>

      {canReportMatches ? (
        <div className="match-controls">
          <div className="match-score-controls">
            <input
              aria-label={t("tournament.homeScore")}
              disabled={busy || !match.ready || !reportStatusAllowed}
              max="99"
              min="0"
              onChange={(event) => setScore((current) => ({ ...current, home: event.target.value }))}
              type="number"
              value={score.home}
            />
            <span>:</span>
            <input
              aria-label={t("tournament.awayScore")}
              disabled={busy || !match.ready || !reportStatusAllowed}
              max="99"
              min="0"
              onChange={(event) => setScore((current) => ({ ...current, away: event.target.value }))}
              type="number"
              value={score.away}
            />
            <button
              type="button"
              disabled={
                busy
                || !match.ready
                || !reportStatusAllowed
                || !scoreValid
              }
              onClick={() => void report()}
            >
              {t("bracket.report")}
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

function formatMatchSchedule(value: string): string {
  return `${moscowScheduleFormatter.format(new Date(value))} МСК`;
}

function scheduleDraftFromIso(value: string | null): ScheduleDraft {
  if (!value) {
    return { date: "", time: "" };
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
    month: "2-digit",
    timeZone: "Europe/Moscow",
    year: "numeric",
  }).formatToParts(new Date(value));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return {
    date: `${part("year")}-${part("month")}-${part("day")}`,
    time: `${part("hour")}:${part("minute")}`,
  };
}

function scheduleDraftToIso(value: ScheduleDraft): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(value.date) || !/^\d{2}:\d{2}$/u.test(value.time)) {
    return null;
  }
  const parsed = new Date(`${value.date}T${value.time}:00+03:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function matchScheduleBounds(matches: Match[], match: Match): ScheduleBounds {
  const sourceIds = new Set(
    [match.homeSourceMatchId, match.awaySourceMatchId].filter((value): value is string => Boolean(value))
  );
  const sourceTimes = matches
    .filter((item) => sourceIds.has(item.id) && item.scheduledAt)
    .map((item) => new Date(item.scheduledAt as string).toISOString());
  const dependentTimes = matches
    .filter((item) => (
      (item.homeSourceMatchId === match.id || item.awaySourceMatchId === match.id)
      && item.scheduledAt
    ))
    .map((item) => new Date(item.scheduledAt as string).toISOString());
  const now = new Date(Date.now() + 60_000);
  now.setUTCSeconds(0, 0);
  const minimum = [now.toISOString(), ...sourceTimes].sort().at(-1) as string;
  const maximum = dependentTimes.length ? dependentTimes.sort().at(0) ?? null : null;
  return { min: minimum, max: maximum };
}

function TeamSlot({
  isWinner,
  label,
  score,
  team,
}: {
  isWinner: boolean;
  label: string;
  score: number | null;
  team: Team | null;
}) {
  return (
    <div
      className={[
        "team",
        isWinner ? "winner" : "",
      ].filter(Boolean).join(" ")}
    >
      <div className="team-slot-content">
        <span className="seed"><span className="seed-value">{team?.seed ?? ""}</span></span>
        <span className="team-name" data-testid="bracket-team-name">
          <span className="team-name-copy">
            <span className="team-name-label">{label}</span>
            {team?.starterStrength !== null && team?.starterStrength !== undefined ? (
              <small>{team.starterStrength.toFixed(1)}</small>
            ) : null}
          </span>
        </span>
        <span className="score"><span className="score-value">{team ? score ?? "-" : ""}</span></span>
      </div>
    </div>
  );
}

function buildLayout(bracket: Bracket, matchHeight: number) {
  const rounds = roundNumbers(bracket);
  const firstRoundCount = Math.max(
    1,
    bracket.matches.filter((match) => match.roundNumber === rounds[0]).length,
  );
  const firstRoundStep = matchHeight + GAP_Y;
  const height = firstRoundCount * matchHeight + (firstRoundCount - 1) * GAP_Y;
  const width = rounds.length * MATCH_W + Math.max(0, rounds.length - 1) * GAP_X;
  const positions = new Map<number, RoundPosition[]>();
  const matches: LayoutMatch[] = [];
  const paths: string[] = [];

  rounds.forEach((roundNumber, roundIndex) => {
    const roundMatches = bracket.matches
      .filter((match) => match.roundNumber === roundNumber)
      .sort((left, right) => left.matchOrder - right.matchOrder);
    const x = roundIndex * (MATCH_W + GAP_X);
    const step = firstRoundStep * Math.pow(2, roundIndex);
    const top = ((Math.pow(2, roundIndex) - 1) * firstRoundStep) / 2;
    const roundPositions: RoundPosition[] = [];
    roundMatches.forEach((match, matchIndex) => {
      const y = top + matchIndex * step;
      roundPositions.push({
        cxLeft: x,
        cxRight: x + MATCH_W,
        cy: y + MATCH_FRAME_CENTER_Y,
      });
      matches.push({ match, x, y });
    });
    positions.set(roundNumber, roundPositions);
  });

  for (let roundIndex = 0; roundIndex < rounds.length - 1; roundIndex += 1) {
    const current = positions.get(rounds[roundIndex]) ?? [];
    const next = positions.get(rounds[roundIndex + 1]) ?? [];
    next.forEach((target, index) => {
      const first = current[index * 2];
      const second = current[index * 2 + 1];
      if (!first || !second) {
        return;
      }
      const joinX = first.cxRight + GAP_X / 2;
      paths.push(`M${first.cxRight} ${first.cy} H${joinX}`);
      paths.push(`M${second.cxRight} ${second.cy} H${joinX}`);
      paths.push(`M${joinX} ${first.cy} V${second.cy}`);
      paths.push(`M${joinX} ${target.cy} H${target.cxLeft}`);
    });
  }

  return { width, height, matches, paths };
}

function bracketLayoutCss(
  scope: string,
  model: ReturnType<typeof buildLayout>,
  scale: number,
  matchHeight: number,
): string {
  const geometry = [
    model.width,
    model.height,
    scale,
    matchHeight,
    ...model.matches.flatMap(({ x, y }) => [x, y]),
  ];
  if (geometry.some((value) => !Number.isFinite(value) || value < 0)) {
    return "";
  }

  const cssNumber = (value: number) => String(Math.round(value * 1000) / 1000);
  const scaledWidth = model.width * scale;
  const scaledHeight = model.height * scale;
  const rules = [
    `#${scope} .bracket-viewport{width:100%;min-width:${cssNumber(scaledWidth + 120)}px;height:${cssNumber(scaledHeight + 120)}px}`,
    `#${scope} .bracket-canvas{width:${cssNumber(model.width)}px;height:${cssNumber(model.height)}px;transform:scale(${cssNumber(scale)});transform-origin:top left;margin-left:max(36px,calc((100% - ${cssNumber(scaledWidth)}px)/2));margin-top:56px}`,
  ];
  model.matches.forEach(({ x, y }, index) => {
    rules.push(
      `#${scope} [data-bracket-match-index="${index}"]{left:${cssNumber(x)}px;top:${cssNumber(y)}px;height:${cssNumber(matchHeight)}px}`,
    );
  });
  return rules.join("\n");
}

function stableNumericId(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function roundNumbers(bracket: Bracket): number[] {
  return [...new Set(bracket.matches.map((match) => match.roundNumber))]
    .sort((left, right) => left - right);
}

function scoreMatchesFormat(score: ScoreDraft, matchFormat: string): boolean {
  if (score.home === "" || score.away === "") {
    return false;
  }
  const home = Number(score.home);
  const away = Number(score.away);
  const winsRequired = winsRequiredForFormat(matchFormat);
  if (!Number.isInteger(home) || !Number.isInteger(away) || home < 0 || away < 0) {
    return false;
  }
  if (home === away) {
    return false;
  }
  return Math.max(home, away) === winsRequired && Math.min(home, away) < winsRequired;
}

function winsRequiredForFormat(matchFormat: string): number {
  if (matchFormat === "bo5") {
    return 3;
  }
  if (matchFormat === "bo3") {
    return 2;
  }
  return 1;
}
