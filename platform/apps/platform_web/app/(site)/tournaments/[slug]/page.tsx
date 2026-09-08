import type { Metadata } from "next";
import { TournamentDetailClientPage } from "@/components/tournaments/tournament-detail-client-page";
import { recordSsrStage } from "@/lib/server-ssr-observability";

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
  const startedAt = performance.now();
  const { slug } = await params;
  const resolvedSearchParams = await searchParams;
  const inviteCode = resolvedSearchParams?.invite_code?.trim().toUpperCase() || undefined;

  const rendered = <TournamentDetailClientPage slug={slug} inviteCode={inviteCode} />;
  await recordSsrStage("page_component", performance.now() - startedAt);
  return rendered;
}
