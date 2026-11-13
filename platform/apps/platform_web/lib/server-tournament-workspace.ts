import "server-only";

import {
  getTournamentWorkspace,
  PlatformApiError,
  type TournamentWorkspace,
  type TournamentWorkspaceRequestOptions,
} from "@/lib/platform-api";
import {
  runWithServerInternalRead,
  ServerInternalReadQueueError,
} from "@/lib/server-internal-read-scheduler";
import type { ServerInternalReadAdmissionObserver } from "@/lib/server-internal-read-scheduler";

type WorkspaceRequestOptions = Omit<TournamentWorkspaceRequestOptions, "ssrDiagnostics"> & {
  ssrDiagnostics?: NonNullable<TournamentWorkspaceRequestOptions["ssrDiagnostics"]> & {
    admissionObserver?: ServerInternalReadAdmissionObserver;
  };
};

export function getServerTournamentWorkspace(
  slug: string,
  requestHeaders: HeadersInit = {},
  options: WorkspaceRequestOptions = {},
): Promise<TournamentWorkspace | null> {
  return runWithServerInternalRead(
    () => getTournamentWorkspace(slug, requestHeaders, options),
    options.signal,
    options.ssrDiagnostics?.admissionObserver,
  ).catch((error: unknown) => {
    if (error instanceof ServerInternalReadQueueError) {
      throw new PlatformApiError(
        "Server read capacity is temporarily unavailable.",
        503,
        null,
        { code: "server_read_capacity" },
      );
    }
    throw error;
  });
}
