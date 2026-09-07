import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { connection } from "next/server";
import { Suspense, type ReactNode } from "react";
import { AuthProvider } from "@/components/auth/auth-provider";
import { I18nProvider } from "@/components/i18n-provider";
import { SiteFooter } from "@/components/layout/site-footer";
import { SiteHeader } from "@/components/layout/site-header";
import { CspNonceProvider } from "@/components/security/csp-nonce-provider";
import { CspRouteAnnouncer } from "@/components/security/csp-route-announcer";
import { getServerAuthBootstrap, platformSessionCookieName } from "@/lib/server-auth";
import { getServerTournamentWorkspace } from "@/lib/server-tournament-workspace";
import { measureSsrStage, recordSsrStage } from "@/lib/server-ssr-observability";
import "./globals.css";
import "./theme-modern.css";
import "@/components/profile/account-identities.css";
import "@/components/tournaments/tournament-card.css";

const SERVER_RENDER_PATHNAME_HEADER = "x-platform-render-pathname";
const SERVER_RENDER_SEARCH_HEADER = "x-platform-render-search";

function tournamentDetailSlugFromRequest(requestHeaders: Headers): string | null {
  if (requestHeaders.get(SERVER_RENDER_SEARCH_HEADER)) {
    return null;
  }
  const pathname = requestHeaders.get(SERVER_RENDER_PATHNAME_HEADER);
  const match = pathname?.match(/^\/tournaments\/([^/]+)$/u);
  if (!match) {
    return null;
  }
  try {
    const slug = decodeURIComponent(match[1]);
    return slug && !slug.includes("/") ? slug : null;
  } catch {
    return null;
  }
}

export const metadata: Metadata = {
  title: {
    default: "Old Sparky Arena",
    template: "%s | Old Sparky Arena"
  },
  description: "Турнирная арена сообщества Old Sparky"
};

export default async function RootLayout({
  children
}: Readonly<{
  children: ReactNode;
}>) {
  const startedAt = performance.now();
  await connection();
  const [requestHeaders, requestCookies] = await Promise.all([headers(), cookies()]);
  const nonce = requestHeaders.get("x-nonce");
  const cookieHeader = requestCookies.toString();
  const tournamentDetailSlug = tournamentDetailSlugFromRequest(requestHeaders);
  if (tournamentDetailSlug) {
    const workspacePrefetch = measureSsrStage(
      "root_layout_tournament_workspace_prefetch",
      () => getServerTournamentWorkspace(tournamentDetailSlug, cookieHeader, undefined),
    );
    // The detail page consumes this same request-local promise. Attach a
    // rejection handler here as a safety net for an interrupted render.
    void workspacePrefetch.catch(() => undefined);
  }
  const initialAuth = requestCookies.has(platformSessionCookieName())
    ? await measureSsrStage("root_layout_auth_bootstrap", () => getServerAuthBootstrap(cookieHeader))
    : { status: "anonymous" as const, user: null };
  const adsenseEnabled = process.env.PLATFORM_ADSENSE_ENABLED !== "false";

  const rendered = (
    <html lang="ru">
      <head>
        {adsenseEnabled ? (
          <script
            async
            crossOrigin="anonymous"
            nonce={nonce ?? undefined}
            src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7185165276065459"
          />
        ) : null}
      </head>
      <body>
        <CspNonceProvider nonce={nonce}>
          <CspRouteAnnouncer />
          <AuthProvider initialStatus={initialAuth.status} initialUser={initialAuth.user}>
            <I18nProvider>
              <SiteHeader />
              <Suspense fallback={<div className="page-noise" aria-hidden="true" />}>
                {children}
              </Suspense>
              <SiteFooter />
            </I18nProvider>
          </AuthProvider>
        </CspNonceProvider>
      </body>
    </html>
  );
  await recordSsrStage("root_layout_component_tree", performance.now() - startedAt);
  return rendered;
}
