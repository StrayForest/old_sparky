import type { Metadata } from "next";
import { cookies } from "next/headers";
import { notFound } from "next/navigation";
import { TournamentDetailClientPage } from "@/components/tournaments/tournament-detail-client-page";
import {
  isSsrTraceSampled,
  getServerRequestCorrelationHeaders,
  measureSsrStage,
  recordSsrPoint,
  recordSsrStage,
  recordSsrWorkspaceApiError
} from "@/lib/server-ssr-observability";
import { run_with_workspace_cpu_diagnostic } from "@/lib/performance-diagnostic-plan";
import {
  getTournamentWorkspace,
  normalizeTournamentInviteCode,
  PlatformApiError
} from "@/lib/platform-api";
import type { TournamentDetail } from "@/lib/types";

export const metadata: Metadata = {
  title: "Турнир"
};

type TournamentDetailPageProps = {
  params: Promise<{ slug: string }>;
  searchParams?: Promise<{ invite_code?: string | string[] }>;
};

export default async function TournamentDetailPage({
  params,
  searchParams
}: TournamentDetailPageProps) {
  // Intentional no-segment-loading contract: this segment contains nested
  // render-time notFound routes (for example tournament-scoped profiles).
  // A segment loading.tsx would flush a 200 shell before those routes can
  // return their real 404. The client detail component owns its safe API
  // progress state after this route has committed.
  const startedAt = performance.now();
  const { slug } = await params;
  const resolvedSearchParams = await searchParams;
  const inviteCode = normalizeTournamentInviteCode(resolvedSearchParams?.invite_code);
  const cookieHeader = (await cookies()).toString();
  let initialTournament: TournamentDetail | undefined;

  // Resolve existence before returning the client shell. A definitive API
  // 404 must become a document-level 404; otherwise an unknown slug commits
  // this route as HTTP 200 and only becomes an error after hydration. Private
  // tournaments intentionally remain client-owned when the server receives
  // 401/403, preserving the invite gate and its loading behavior.
  await run_with_workspace_cpu_diagnostic(async () => {
    const ssrTraceSampled = await isSsrTraceSampled();
    try {
      const workspaceHeaders: Record<string, string> = cookieHeader
        ? { cookie: cookieHeader }
        : {};
      if (ssrTraceSampled) {
        (await getServerRequestCorrelationHeaders()).forEach((value, name) => {
          workspaceHeaders[name] = value;
        });
      }
      const workspaceOptions = {
        participantsLimit: 0,
        workspaceView: "detail" as const,
        includeCurrentUser: false,
        inviteCode
      };
      const workspace = await (ssrTraceSampled
        ? measureSsrStage("tournament_workspace", () =>
          getTournamentWorkspace(slug, workspaceHeaders, workspaceOptions)
        )
        : getTournamentWorkspace(slug, workspaceHeaders, workspaceOptions));
      if (!workspace) {
        notFound();
      }
      initialTournament = workspace.tournament;
      if (ssrTraceSampled) {
        await recordSsrPoint("tournament_detail_data_ready");
      }
    } catch (error) {
      if (error instanceof PlatformApiError) {
        await recordSsrWorkspaceApiError(error.status);
      }
      if (!(error instanceof PlatformApiError && (error.status === 401 || error.status === 403))) {
        throw error;
      }
    }
    if (ssrTraceSampled) {
      await recordSsrStage("page_component", performance.now() - startedAt);
    }
  });

  const rendered = (
    <TournamentDetailClientPage
      slug={slug}
      inviteCode={inviteCode}
      initialTournament={initialTournament}
    />
  );
  return rendered;
}
