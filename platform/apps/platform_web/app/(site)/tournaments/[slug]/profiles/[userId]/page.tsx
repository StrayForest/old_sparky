import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { Hero } from "@/components/layout/hero";
import { PublicProfileView } from "@/components/profile/public-profile-view";
import { getTournamentPlayerProfile } from "@/lib/platform-api";

export const metadata: Metadata = {
  title: "Профиль участника"
};

export default async function TournamentPlayerProfilePage({
  params
}: {
  params: Promise<{ slug: string; userId: string }>;
}) {
  const { slug, userId } = await params;
  const cookieHeader = (await cookies()).toString();
  if (!cookieHeader) {
    notFound();
  }
  const profile = await getTournamentPlayerProfile(slug, userId, { cookie: cookieHeader });
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
        <Link className="outline-button tournament-profile-back" href={`/tournaments/${encodeURIComponent(slug)}`}>
          <ArrowLeft aria-hidden="true" size={16} />
          Назад к турниру
        </Link>
        <PublicProfileView profile={profile} />
      </main>
    </>
  );
}
