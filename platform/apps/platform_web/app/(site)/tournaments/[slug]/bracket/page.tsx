import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { cookies } from "next/headers";
import { ArrowLeft } from "lucide-react";
import { BracketBoard } from "@/components/bracket/bracket-board";
import { Hero } from "@/components/layout/hero";
import { HistoryBackLink } from "@/components/layout/history-back-link";
import { getTournamentWorkspace, PlatformApiError } from "@/lib/platform-api";

export const metadata: Metadata = {
  title: "Сетка турнира"
};

export default async function TournamentBracketPage({
  params
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const cookieHeader = (await cookies()).toString();
  const requestHeaders: HeadersInit = cookieHeader ? { cookie: cookieHeader } : {};

  let workspace: Awaited<ReturnType<typeof getTournamentWorkspace>>;
  try {
    workspace = await getTournamentWorkspace(slug, requestHeaders, {
      participantsLimit: 0,
      workspaceView: "bracket_summary",
      includeCurrentUser: false
    });
  } catch (error) {
    if (error instanceof PlatformApiError && error.status === 401) {
      redirect(`/auth/login?returnTo=${encodeURIComponent(`/tournaments/${slug}/bracket`)}`);
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
      <Hero title={tournament.title} subtitle="Следите за командами и результатами по ходу турнира." />
      <main className="main">
        <HistoryBackLink className="outline-button tournament-profile-back" fallbackHref={`/tournaments/${encodeURIComponent(slug)}`}>
          <ArrowLeft aria-hidden="true" size={18} />
          Назад к турниру
        </HistoryBackLink>
        <BracketBoard initialBracket={tournament.bracket} slug={slug} />
      </main>
    </>
  );
}
