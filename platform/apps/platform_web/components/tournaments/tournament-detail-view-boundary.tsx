"use client";

import dynamic from "next/dynamic";
import { RouteLoadingPanel } from "@/components/layout/loading-shells";
import type { TournamentDetail } from "@/lib/types";

type TournamentDetailViewBoundaryProps = {
  tournament: TournamentDetail;
  actorUserId: string | null;
};

const ClientTournamentDetailView = dynamic(
  () => import("@/components/tournaments/tournament-detail-view").then((module) => module.TournamentDetailView),
  {
    ssr: false,
    loading: () => <RouteLoadingPanel />
  }
);

export function TournamentDetailViewBoundary({
  tournament,
  actorUserId
}: TournamentDetailViewBoundaryProps) {
  return <ClientTournamentDetailView tournament={tournament} actorUserId={actorUserId} />;
}
