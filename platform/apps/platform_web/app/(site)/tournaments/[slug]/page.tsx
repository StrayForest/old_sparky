import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cookies } from "next/headers";
import { Suspense } from "react";
import { Hero } from "@/components/layout/hero";
import { RouteLoadingShell } from "@/components/layout/loading-shells";
import { TournamentDetailView } from "@/components/tournaments/tournament-detail-view";
import { PlatformApiError } from "@/lib/platform-api";
import { getServerAuthBootstrap, platformSessionCookieName } from "@/lib/server-auth";
import { getServerTournamentWorkspace } from "@/lib/server-tournament-workspace";
import { measureSsrStage, recordSsrStage } from "@/lib/server-ssr-observability";
import { TournamentInviteGate } from "@/components/tournaments/tournament-invite-gate";

export const metadata: Metadata = {
  title: "Турнир"
};

type TournamentDetailPageProps = {
  params: Promise<{ slug: string }>;
  searchParams?: Promise<{ invite_code?: string }>;
};

export default function TournamentDetailPage(props: TournamentDetailPageProps) {
  return (
    <Suspense fallback={<RouteLoadingShell variant="tournament-detail" />}>
      <TournamentDetailContent {...props} />
    </Suspense>
  );
}

async function TournamentDetailContent({
  params,
  searchParams
}: TournamentDetailPageProps) {
  const startedAt = performance.now();
  const { slug } = await params;
  const resolvedSearchParams = await searchParams;
  const inviteCode = resolvedSearchParams?.invite_code?.trim().toUpperCase() || undefined;
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const actorUserIdPromise = requestCookies.has(platformSessionCookieName())
    ? measureSsrStage(
      "tournament_detail_auth_bootstrap",
      () => getServerAuthBootstrap(cookieHeader).then((snapshot) => snapshot.user?.id ?? null)
    )
    : Promise.resolve(null);

  let workspace: Awaited<ReturnType<typeof getServerTournamentWorkspace>>;
  let actorUserId: string | null;
  try {
    [workspace, actorUserId] = await Promise.all([
      measureSsrStage(
        "tournament_workspace",
        () => getServerTournamentWorkspace(slug, cookieHeader, inviteCode)
      ),
      actorUserIdPromise
    ]);
  } catch (error) {
    if (error instanceof PlatformApiError && (error.status === 401 || error.status === 403)) {
      return (
        <>
          <div className="page-noise" aria-hidden="true" />
          <main className="main">
            <TournamentInviteGate slug={slug} />
          </main>
        </>
      );
    }
    throw error;
  }

  if (!workspace) {
    notFound();
  }
  const { tournament } = workspace;

  await recordSsrStage("tournament_detail_data_ready", performance.now() - startedAt);
  const rendered = (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow={`Турниры / ${tournament.title}`}
        title={tournament.title}
        subtitle="Проверьте параметры турнира, расписание и текущий этап."
      />
      <main className="main">
        <TournamentDetailView
          tournament={tournament}
          actorUserId={actorUserId}
        />
      </main>
    </>
  );
  await recordSsrStage("tournament_detail_component_tree", performance.now() - startedAt);
  return rendered;
}
