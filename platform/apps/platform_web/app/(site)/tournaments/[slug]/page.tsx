import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { cookies } from "next/headers";
import { Hero } from "@/components/layout/hero";
import { TournamentDetailView } from "@/components/tournaments/tournament-detail-view";
import { getTournamentWorkspace, PlatformApiError } from "@/lib/platform-api";
import { getServerCurrentUser, platformSessionCookieName } from "@/lib/server-auth";

export const metadata: Metadata = {
  title: "Турнир"
};

export default async function TournamentDetailPage({
  params
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const requestHeaders: HeadersInit = cookieHeader ? { cookie: cookieHeader } : {};
  const actorUserIdPromise = requestCookies.has(platformSessionCookieName())
    ? getServerCurrentUser(cookieHeader).then((snapshot) => snapshot.user?.id ?? null)
    : Promise.resolve(null);

  let workspace: Awaited<ReturnType<typeof getTournamentWorkspace>>;
  let actorUserId: string | null;
  try {
    [workspace, actorUserId] = await Promise.all([
      getTournamentWorkspace(slug, requestHeaders, {
        participantsLimit: 0,
        workspaceView: "detail",
        includeCurrentUser: false
      }),
      actorUserIdPromise
    ]);
  } catch (error) {
    if (error instanceof PlatformApiError && error.status === 401) {
      redirect(`/auth/login?returnTo=${encodeURIComponent(`/tournaments/${slug}`)}`);
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