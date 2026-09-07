import type { Metadata } from "next";
import { TournamentDetailClientPage } from "@/components/tournaments/tournament-detail-client-page";

export const metadata: Metadata = {
  title: "Турнир"
};

type TournamentDetailPageProps = {
  params: Promise<{ slug: string }>;
  searchParams?: Promise<{ invite_code?: string }>;
};

export default async function TournamentDetailPage({
  params,
  searchParams
}: TournamentDetailPageProps) {
  const { slug } = await params;
  const resolvedSearchParams = await searchParams;
  const inviteCode = resolvedSearchParams?.invite_code?.trim().toUpperCase() || undefined;

  return <TournamentDetailClientPage slug={slug} inviteCode={inviteCode} />;
}
