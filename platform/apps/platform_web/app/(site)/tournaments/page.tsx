import type { Metadata } from "next";
import { Hero } from "@/components/layout/hero";
import { TournamentListClient } from "@/components/tournaments/tournament-list-client";
import { getTournamentSummaries } from "@/lib/platform-api";

export const metadata: Metadata = {
  title: "Турниры",
  description: "Турниры сообщества Old Sparky Arena по Deadlock."
};

export default async function TournamentsPage() {
  const initialPage = await getTournamentSummaries({ limit: 9, offset: 0 });

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Deadlock-турниры"
        subtitle="Находите турниры, регистрируйтесь и докажите своё превосходство в мире Deadlock."
      />
      <main className="main">
        <TournamentListClient initialPage={initialPage} />
      </main>
    </>
  );
}
