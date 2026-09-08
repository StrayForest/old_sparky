import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { connection } from "next/server";
import { Suspense, type ReactNode } from "react";
import { AuthProvider } from "@/components/auth/auth-provider";
import { I18nProvider } from "@/components/i18n-provider";
import { SiteFooter } from "@/components/layout/site-footer";
import { SiteHeader } from "@/components/layout/site-header";
import { SsrDiagnosticBoundary } from "@/components/observability/ssr-diagnostic-boundary";
import { CspNonceProvider } from "@/components/security/csp-nonce-provider";
import { CspRouteAnnouncer } from "@/components/security/csp-route-announcer";
import { getServerAuthBootstrap, platformSessionCookieName } from "@/lib/server-auth";
import {
  measureSsrStage,
  recordSsrPoint,
  recordSsrProxyToRootStart,
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
  const startedAt = performance.now();
  await connection();
  const requestHeaders = await headers();
  return runWithSsrTrace(startedAt, requestHeaders, async () => {
    await recordSsrPoint("root_layout_start", 0);
    await recordSsrProxyToRootStart();
    const requestCookies = await cookies();
    const nonce = requestHeaders.get("x-nonce");
    const cookieHeader = requestCookies.toString();
    const initialAuth = requestCookies.has(platformSessionCookieName())
      ? await measureSsrStage("auth_bootstrap", () => getServerAuthBootstrap(cookieHeader))
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
            <SsrDiagnosticBoundary stage="authenticated_provider">
              <AuthProvider initialStatus={initialAuth.status} initialUser={initialAuth.user}>
                <I18nProvider>
                  <SsrDiagnosticBoundary stage="global_chrome_header">
                    <SiteHeader />
                  </SsrDiagnosticBoundary>
                  <Suspense fallback={<div className="page-noise" aria-hidden="true" />}>
                    {children}
                  </Suspense>
                  <SsrDiagnosticBoundary stage="global_chrome_footer">
                    <SiteFooter />
                  </SsrDiagnosticBoundary>
                </I18nProvider>
              </AuthProvider>
            </SsrDiagnosticBoundary>
          </CspNonceProvider>
        </body>
      </html>
    );
    await recordSsrStage("root_layout", performance.now() - startedAt);
    return rendered;
  });
}
