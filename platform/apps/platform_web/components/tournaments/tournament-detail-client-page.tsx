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
};

type DetailState =
  | { status: "loading" }
  | { status: "ready"; tournament: TournamentDetail }
  | { status: "invite" }
  | { status: "not-found" }
  | { status: "error" };

export function TournamentDetailClientPage({
  slug,
  inviteCode
}: TournamentDetailClientPageProps) {
  const { status: authStatus, user } = useAuth();
  const { t } = useI18n();
  const [state, setState] = useState<DetailState>({ status: "loading" });
  const [retryGeneration, setRetryGeneration] = useState(0);
  const requestGeneration = useRef(0);
  const actorUserId = authStatus === "authenticated" ? user?.id ?? null : null;

  useEffect(() => {
    const controller = new AbortController();
    const generation = ++requestGeneration.current;
    setState({ status: "loading" });

    void getTournamentWorkspace(slug, {}, {
      participantsLimit: 0,
      workspaceView: "detail",
      includeCurrentUser: false,
      inviteCode,
      signal: controller.signal
    })
      .then((workspace) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) {
          return;
        }
        setState(workspace
          ? { status: "ready", tournament: workspace.tournament }
          : { status: "not-found" });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) {
          return;
        }
        if (error instanceof PlatformApiError && (error.status === 401 || error.status === 403)) {
          setState({ status: "invite" });
          return;
        }
        setState({ status: "error" });
      });

    return () => controller.abort();
  }, [inviteCode, retryGeneration, slug]);

  if (state.status === "loading") {
    return <RouteLoadingShell variant="tournament-detail" />;
  }

  if (state.status === "invite") {
    return (
      <>
        <div className="page-noise" aria-hidden="true" />
        <main className="main">
          <TournamentInviteGate slug={slug} />
        </main>
      </>
    );
  }

  if (state.status === "not-found") {
    return <DetailErrorShell title={t("tournament.notFoundTitle")} copy={t("tournament.notFoundCopy")} />;
  }

  if (state.status === "error") {
    return (
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
    );
  }

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow={`Турниры / ${state.tournament.title}`}
        title={state.tournament.title}
        subtitle="Проверьте параметры турнира, расписание и текущий этап."
      />
      <main className="main">
        <TournamentDetailViewBoundary
          tournament={state.tournament}
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
