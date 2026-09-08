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
import {
  isSsrDiagnosticsEnabled,
  measureSsrStage,
  recordSsrPoint,
  recordSsrRequestTimeline,
  recordSsrStage,
  runWithSsrTrace
} from "@/lib/server-ssr-observability";
import "./globals.css";
import "./theme-modern.css";
import "@/components/profile/account-identities.css";
import "@/components/tournaments/tournament-card.css";

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
  const diagnosticsEnabled = isSsrDiagnosticsEnabled();
  const startedAt = diagnosticsEnabled ? performance.now() : 0;
  const rootStartedAtMs = diagnosticsEnabled ? Date.now() : 0;
  await connection();
  const [requestHeaders, requestCookies] = await Promise.all([headers(), cookies()]);
  const renderRoot = (
    nonce: string | null,
    initialAuth: Awaited<ReturnType<typeof getServerAuthBootstrap>>,
    adsenseEnabled: boolean
  ) => (
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

  if (!diagnosticsEnabled) {
    const nonce = requestHeaders.get("x-nonce");
    const cookieHeader = requestCookies.toString();
    const initialAuth = requestCookies.has(platformSessionCookieName())
      ? await measureSsrStage("auth_bootstrap", () => getServerAuthBootstrap(cookieHeader))
      : { status: "anonymous" as const, user: null };
    const adsenseEnabled = process.env.PLATFORM_ADSENSE_ENABLED !== "false";
    return renderRoot(nonce, initialAuth, adsenseEnabled);
  }

  return runWithSsrTrace(startedAt, rootStartedAtMs, requestHeaders, async () => {
    await recordSsrRequestTimeline();
    await recordSsrPoint("root_layout_start", 0);
    const nonce = requestHeaders.get("x-nonce");
    const cookieHeader = requestCookies.toString();
    const initialAuth = requestCookies.has(platformSessionCookieName())
      ? await measureSsrStage("auth_bootstrap", () => getServerAuthBootstrap(cookieHeader))
      : { status: "anonymous" as const, user: null };
    const adsenseEnabled = process.env.PLATFORM_ADSENSE_ENABLED !== "false";
    const rendered = renderRoot(nonce, initialAuth, adsenseEnabled);
    await recordSsrStage("root_layout", performance.now() - startedAt);
    await recordSsrPoint("react_render_unattributed_start");
    return rendered;
  });
}
