"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Hero } from "@/components/layout/hero";
import { RouteLoadingShell } from "@/components/layout/loading-shells";
import { useAuth } from "@/components/auth/auth-provider";
import { TournamentDetailViewBoundary } from "@/components/tournaments/tournament-detail-view-boundary";
import { TournamentInviteGate } from "@/components/tournaments/tournament-invite-gate";
import { useI18n } from "@/components/i18n-provider";
import { getTournamentWorkspace, PlatformApiError } from "@/lib/platform-api";
import type { TournamentDetail } from "@/lib/types";

type TournamentDetailClientPageProps = {
  slug: string;
  inviteCode?: string;
  initialTournament?: TournamentDetail;
};

type DetailState =
  | { status: "loading" }
  | { status: "ready"; tournament: TournamentDetail }
  | { status: "invite" }
  | { status: "not-found" }
  | { status: "error" };

type InitialRequest = {
  slug: string;
  inviteCode: string | null;
  sessionIdentity: string;
  payload: TournamentDetail;
  version: number;
  retryGeneration: number;
};

type ServerSeed = {
  slug: string;
  inviteCode: string | null;
  payload: TournamentDetail | undefined;
};

type DetailContext = {
  slug: string;
  inviteCode: string | null;
  sessionIdentity: string;
  serverSeedVersion: number;
  retryGeneration: number;
};

const MAX_LIFECYCLE_GENERATION = 1_000_000;

function sameDetailContext(left: DetailContext | null, right: DetailContext): boolean {
  return Boolean(
    left
    && left.slug === right.slug
    && left.inviteCode === right.inviteCode
    && left.sessionIdentity === right.sessionIdentity
    && left.serverSeedVersion === right.serverSeedVersion
    && left.retryGeneration === right.retryGeneration
  );
}

function nextLifecycleGeneration(current: number): number {
  return current >= MAX_LIFECYCLE_GENERATION ? 1 : current + 1;
}

function TournamentDetailLifecycleMarker({
  generation,
  settled
}: {
  generation: number;
  settled: boolean;
}) {
  return (
    <span
      aria-hidden="true"
      data-generation={generation}
      data-settled={settled ? "true" : "false"}
      data-testid="tournament-detail-lifecycle"
      hidden
    />
  );
}

export function TournamentDetailClientPage({
  slug,
  inviteCode,
  initialTournament
}: TournamentDetailClientPageProps) {
  const { status: authStatus, user } = useAuth();
  const { t } = useI18n();
  const sessionIdentity = `${authStatus}:${user?.id ?? "anonymous"}`;
  const normalizedInviteCode = inviteCode ?? null;
  const serverSeedVersionRef = useRef(1);
  const serverSeedRef = useRef<ServerSeed>({
    slug,
    inviteCode: normalizedInviteCode,
    payload: initialTournament
  });
  const [retryGeneration, setRetryGeneration] = useState(0);
  const [state, setState] = useState<DetailState>(() => initialTournament
    ? { status: "ready", tournament: initialTournament }
    : { status: "loading" });
  const initialRequestRef = useRef<InitialRequest | null>(initialTournament
    ? {
        slug,
        inviteCode: normalizedInviteCode,
        sessionIdentity,
        payload: initialTournament,
        version: serverSeedVersionRef.current,
        retryGeneration
      }
    : null);
  const requestGeneration = useRef(0);
  const stateContextRef = useRef<DetailContext>({
    slug,
    inviteCode: normalizedInviteCode,
    sessionIdentity,
    serverSeedVersion: serverSeedVersionRef.current,
    retryGeneration
  });
  const lifecycleGenerationRef = useRef(0);
  const lifecycleObservedContextRef = useRef<DetailContext | null>(null);
  const lifecycleSettledContextRef = useRef<DetailContext | null>(null);
  const lifecycleSettledRef = useRef(false);
  const actorUserId = authStatus === "authenticated" ? user?.id ?? null : null;

  const serverSeedChanged = (
    serverSeedRef.current.slug !== slug
    || serverSeedRef.current.inviteCode !== normalizedInviteCode
    || serverSeedRef.current.payload !== initialTournament
  );
  if (serverSeedChanged) {
    serverSeedRef.current = {
      slug,
      inviteCode: normalizedInviteCode,
      payload: initialTournament
    };
    const version = ++serverSeedVersionRef.current;
    requestGeneration.current += 1;
    initialRequestRef.current = initialTournament
      ? {
          slug,
          inviteCode: normalizedInviteCode,
          sessionIdentity,
          payload: initialTournament,
          version,
          retryGeneration
        }
      : null;
  }
  const serverSeedVersion = serverSeedVersionRef.current;
  const currentContext: DetailContext = {
    slug,
    inviteCode: normalizedInviteCode,
    sessionIdentity,
    serverSeedVersion,
    retryGeneration
  };
  if (!sameDetailContext(lifecycleObservedContextRef.current, currentContext)) {
    lifecycleObservedContextRef.current = currentContext;
    lifecycleGenerationRef.current = nextLifecycleGeneration(lifecycleGenerationRef.current);
    lifecycleSettledContextRef.current = null;
    lifecycleSettledRef.current = false;
  }
  const lifecycleSettled = lifecycleSettledRef.current
    && sameDetailContext(lifecycleSettledContextRef.current, currentContext);
  const lifecycleGeneration = lifecycleGenerationRef.current;
  const displayState = serverSeedChanged
    ? initialTournament
      ? { status: "ready" as const, tournament: initialTournament }
      : { status: "loading" as const }
    : sameDetailContext(stateContextRef.current, currentContext)
      ? state
      : { status: "loading" as const };
  const settleLifecycle = (context: DetailContext) => {
    if (sameDetailContext(lifecycleObservedContextRef.current, context)) {
      lifecycleSettledContextRef.current = context;
      lifecycleSettledRef.current = true;
    }
  };

  useEffect(() => {
    const initialRequest = initialRequestRef.current;
    if (
      initialRequest
      && initialRequest.slug === slug
      && initialRequest.inviteCode === normalizedInviteCode
      && initialRequest.sessionIdentity === sessionIdentity
      && initialRequest.payload === initialTournament
      && initialRequest.version === serverSeedVersion
      && initialRequest.retryGeneration === retryGeneration
    ) {
      settleLifecycle(currentContext);
      stateContextRef.current = currentContext;
      setState(initialTournament
        ? { status: "ready", tournament: initialTournament }
        : { status: "loading" });
      return;
    }
    initialRequestRef.current = null;
    const controller = new AbortController();
    const generation = ++requestGeneration.current;
    const requestSlug = slug;
    const requestInviteCode = normalizedInviteCode;
    const requestSessionIdentity = sessionIdentity;
    const requestContext = currentContext;
    lifecycleSettledContextRef.current = null;
    lifecycleSettledRef.current = false;
    stateContextRef.current = requestContext;
    setState({ status: "loading" });

    void getTournamentWorkspace(requestSlug, {}, {
      participantsLimit: 0,
      workspaceView: "detail",
      includeCurrentUser: false,
      inviteCode: normalizedInviteCode ?? undefined,
      signal: controller.signal
    })
      .then((workspace) => {
        if (
          controller.signal.aborted
            || requestGeneration.current !== generation
            || requestSlug !== slug
            || requestInviteCode !== normalizedInviteCode
            || requestSessionIdentity !== sessionIdentity
            || requestContext.serverSeedVersion !== serverSeedVersion
            || requestContext.retryGeneration !== retryGeneration
          ) {
          return;
        }
        settleLifecycle(requestContext);
        stateContextRef.current = requestContext;
        setState(workspace
          ? { status: "ready", tournament: workspace.tournament }
          : { status: "not-found" });
      })
      .catch((error: unknown) => {
        if (
          controller.signal.aborted
            || requestGeneration.current !== generation
            || requestSlug !== slug
            || requestInviteCode !== normalizedInviteCode
            || requestSessionIdentity !== sessionIdentity
            || requestContext.serverSeedVersion !== serverSeedVersion
            || requestContext.retryGeneration !== retryGeneration
          ) {
          return;
        }
        settleLifecycle(requestContext);
        stateContextRef.current = requestContext;
        if (error instanceof PlatformApiError && (error.status === 401 || error.status === 403)) {
          setState({ status: "invite" });
          return;
        }
        setState({ status: "error" });
      })
      .finally(() => {
        if (
          requestGeneration.current === generation
          && sameDetailContext(lifecycleObservedContextRef.current, requestContext)
        ) {
          settleLifecycle(requestContext);
        }
      });

    return () => controller.abort();
  }, [
    actorUserId,
    authStatus,
    initialTournament,
    normalizedInviteCode,
    retryGeneration,
    serverSeedVersion,
    sessionIdentity,
    slug
  ]);

  if (displayState.status === "loading") {
    return (
      <>
        <TournamentDetailLifecycleMarker generation={lifecycleGeneration} settled={lifecycleSettled} />
        <RouteLoadingShell variant="tournament-detail" />
      </>
    );
  }

  if (displayState.status === "invite") {
    return (
      <>
        <TournamentDetailLifecycleMarker generation={lifecycleGeneration} settled={lifecycleSettled} />
        <div className="page-noise" aria-hidden="true" />
        <main className="main">
          <TournamentInviteGate slug={slug} />
        </main>
      </>
    );
  }

  if (displayState.status === "not-found") {
    return (
      <>
        <TournamentDetailLifecycleMarker generation={lifecycleGeneration} settled={lifecycleSettled} />
        <DetailErrorShell title={t("tournament.notFoundTitle")} copy={t("tournament.notFoundCopy")} />
      </>
    );
  }

  if (displayState.status === "error") {
    return (
      <>
        <TournamentDetailLifecycleMarker generation={lifecycleGeneration} settled={lifecycleSettled} />
        <DetailErrorShell
          title={t("tournament.loadFailedTitle")}
          copy={t("tournament.loadFailedCopy")}
          action={(
            <button
              className="primary-action"
              onClick={() => setRetryGeneration((current) => current + 1)}
              type="button"
            >
              {t("common.retry")}
            </button>
          )}
        />
      </>
    );
  }

  return (
    <>
      <TournamentDetailLifecycleMarker generation={lifecycleGeneration} settled={lifecycleSettled} />
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow={`Турниры / ${displayState.tournament.title}`}
        title={displayState.tournament.title}
        subtitle="Проверьте параметры турнира, расписание и текущий этап."
      />
      <main className="main">
        <TournamentDetailViewBoundary
          key={serverSeedVersion}
          tournament={displayState.tournament}
          actorUserId={actorUserId}
        />
      </main>
    </>
  );
}

function DetailErrorShell({
  title,
  copy,
  action
}: {
  title: string;
  copy: string;
  action?: ReactNode;
}) {
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero eyebrow="Турниры" title={title} subtitle={copy} />
      <main className="main">
        <section className="panel panel-pad auth-panel" role="alert">
          <h2 className="panel-title">{title}</h2>
          <p className="description-text">{copy}</p>
          {action ? <div className="auth-actions">{action}</div> : null}
        </section>
      </main>
    </>
  );
}
