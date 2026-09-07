import "server-only";

import { cache } from "react";
import { getTournamentWorkspace } from "@/lib/platform-api";

/**
 * Share the detail workspace request between the root layout and its page.
 *
 * The root layout must still await authoritative auth before rendering the
 * no-JavaScript header, but the workspace read can be started while that
 * request is in flight. Keep the cache key scalar so the layout and page use
 * the same request-local React cache entry.
 */
export const getServerTournamentWorkspace = cache(async (
  slug: string,
  cookieHeader: string,
  inviteCode?: string,
) => getTournamentWorkspace(
  slug,
  cookieHeader ? { cookie: cookieHeader } : {},
  {
    participantsLimit: 0,
    workspaceView: "detail",
    includeCurrentUser: false,
    inviteCode,
  },
));
