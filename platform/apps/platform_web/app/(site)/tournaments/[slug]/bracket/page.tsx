import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { cookies } from "next/headers";
import { ArrowLeft } from "lucide-react";
import { BracketBoard } from "@/components/bracket/bracket-board";
import { Hero } from "@/components/layout/hero";
import { getTournamentWorkspace } from "@/lib/platform-api";

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
  const workspace = await getTournamentWorkspace(slug, requestHeaders, {
    participantsLimit: 0,
    workspaceView: "bracket_summary",
    includeCurrentUser: false
  });
  if (!workspace) {
    notFound();
  }
  const { tournament } = workspace;

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero title={tournament.title} subtitle="Следите за командами и результатами по ходу турнира." />
      <main className="main">
        <Link className="outline-button tournament-profile-back" href={`/tournaments/${encodeURIComponent(slug)}`}>
          <ArrowLeft aria-hidden="true" size={18} />
          Назад к турниру
        </Link>
        <BracketBoard initialBracket={tournament.bracket} slug={slug} />
      </main>
    </>
  );
}
