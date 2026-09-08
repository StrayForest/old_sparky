import type { ReactNode } from "react";
import { SsrDiagnosticBoundary } from "@/components/observability/ssr-diagnostic-boundary";

export default function TournamentDetailLayout({
  children
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <SsrDiagnosticBoundary stage="route_layout">
      {children}
    </SsrDiagnosticBoundary>
  );
}
