import "server-only";

import { cache } from "react";
import { cookies } from "next/headers";
import { getServerCurrentUser, platformSessionCookieName } from "@/lib/server-auth";

/**
 * Resolve the request-scoped operations access once for both metadata and the
 * page. Keeping the authorization decision in one cached server resolver
 * prevents metadata from drifting from the page's notFound behavior.
 */
export const resolvePlatformOperationsAccess = cache(async (): Promise<boolean> => {
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const authSnapshot = requestCookies.has(platformSessionCookieName())
    ? await getServerCurrentUser(cookieHeader)
    : null;
  const user = authSnapshot?.user ?? null;
  const hasAdminRole = user?.roles.some((role) => role === "admin" || role === "superadmin");

  // Endpoint configuration can never grant operations access. Missing or
  // unavailable session data therefore fails closed in every environment.
  return Boolean(hasAdminRole);
});
