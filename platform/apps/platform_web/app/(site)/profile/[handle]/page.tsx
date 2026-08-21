import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Hero } from "@/components/layout/hero";
import { PublicProfileView } from "@/components/profile/public-profile-view";
import { getPublicPlayerProfile } from "@/lib/platform-api";

export const metadata: Metadata = {
  title: "Профиль игрока"
};

export default async function PublicProfilePage({
  params
}: {
  params: Promise<{ handle: string }>;
}) {
  const { handle } = await params;
  const profile = await getPublicPlayerProfile(handle);
  if (!profile) {
    notFound();
  }
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Профиль игрока"
        subtitle="Публичная карточка игрока с актуальными данными Deadlock."
      />
      <main className="main">
        <PublicProfileView profile={profile} />
      </main>
    </>
  );
}
