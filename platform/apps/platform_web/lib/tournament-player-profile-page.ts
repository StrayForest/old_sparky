import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";
import { getTournamentPlayerProfile } from "@/lib/platform-api";
import type { PlayerProfile } from "@/lib/types";

/**
 * The route metadata and page render must observe the same authenticated
 * read-model. React cache deduplicates this request within one render while
 * retaining the request cookie boundary.
 */
export const resolveTournamentPlayerProfile = cache(async (
  slug: string,
  userId: string,
): Promise<PlayerProfile | null> => {
  const cookieHeader = (await cookies()).toString();
  if (!cookieHeader) {
    return null;
  }
  return getTournamentPlayerProfile(slug, userId, { cookie: cookieHeader });
});
