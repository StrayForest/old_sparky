import "server-only";

import type { ReactNode } from "react";
import { measureSsrStage } from "@/lib/server-ssr-observability";

/**
 * Marks a server-component boundary without changing its rendered output.
 * Client components below the boundary are measured as boundary assembly;
 * their browser execution is outside the server trace.
 */
export async function SsrDiagnosticBoundary({
  children,
  stage
}: {
  children: ReactNode;
  stage: string;
}) {
  return measureSsrStage(stage, async () => children);
}
