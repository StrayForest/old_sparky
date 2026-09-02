import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cookies } from "next/headers";
import { Hero } from "@/components/layout/hero";
import { TournamentDetailView } from "@/components/tournaments/tournament-detail-view";
import { getTournamentWorkspace, PlatformApiError } from "@/lib/platform-api";
import { getServerAuthBootstrap, platformSessionCookieName } from "@/lib/server-auth";
import { TournamentInviteGate } from "@/components/tournaments/tournament-invite-gate";

export const metadata: Metadata = {
  title: "Турнир"
};

export default async function TournamentDetailPage({
  params,
  searchParams
}: {
  params: Promise<{ slug: string }>;
  searchParams?: Promise<{ invite_code?: string }>;
}) {
  const { slug } = await params;
  const resolvedSearchParams = await searchParams;
  const inviteCode = resolvedSearchParams?.invite_code?.trim().toUpperCase() || undefined;
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const requestHeaders: HeadersInit = cookieHeader ? { cookie: cookieHeader } : {};
  const actorUserIdPromise = requestCookies.has(platformSessionCookieName())
    ? getServerAuthBootstrap(cookieHeader).then((snapshot) => snapshot.user?.id ?? null)
    : Promise.resolve(null);

  let workspace: Awaited<ReturnType<typeof getTournamentWorkspace>>;
  let actorUserId: string | null;
  try {
    [workspace, actorUserId] = await Promise.all([
      getTournamentWorkspace(slug, requestHeaders, {
        participantsLimit: 0,
        workspaceView: "detail",
        includeCurrentUser: false,
        inviteCode
      }),
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

  return (
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
}
