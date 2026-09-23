import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { HistoryBackLink } from "@/components/layout/history-back-link";
import { Hero } from "@/components/layout/hero";
import { PublicProfileView } from "@/components/profile/public-profile-view";
import { resolveTournamentPlayerProfile } from "@/lib/tournament-player-profile-page";

type TournamentPlayerProfilePageProps = {
  params: Promise<{ slug: string; userId: string }>;
};

export async function generateMetadata({
  params
}: TournamentPlayerProfilePageProps): Promise<Metadata> {
  const { slug, userId } = await params;
  const profile = await resolveTournamentPlayerProfile(slug, userId);
  return profile
    ? { title: "Профиль участника", robots: { index: false, follow: false } }
    : { title: "Профиль участника" };
}

export default async function TournamentPlayerProfilePage({
  params
}: TournamentPlayerProfilePageProps) {
  const { slug, userId } = await params;
  const profile = await resolveTournamentPlayerProfile(slug, userId);
  if (!profile) {
    notFound();
  }

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow="Состав турнира"
        title="Профиль игрока"
        subtitle="Турнирные данные участника сформированного состава."
      />
      <main className="main">
        <HistoryBackLink className="outline-button tournament-profile-back" fallbackHref={`/tournaments/${encodeURIComponent(slug)}`}>
          <ArrowLeft aria-hidden="true" size={16} />
          Назад к турниру
        </HistoryBackLink>
        <PublicProfileView profile={profile} />
      </main>
    </>
  );
}
