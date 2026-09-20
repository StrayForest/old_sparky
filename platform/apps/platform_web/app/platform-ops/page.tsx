import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { AdminConsole } from "@/components/admin/admin-console";
import { resolvePlatformOperationsAccess } from "@/lib/platform-ops-access";

export async function generateMetadata(): Promise<Metadata> {
  const hasOperationsAccess = await resolvePlatformOperationsAccess();
  return hasOperationsAccess
    ? { title: "Operations", robots: { index: false, follow: false } }
    : { title: "Operations" };
}

export default async function PlatformOperationsPage() {
  // Intentional no-segment-loading contract: non-admin requests terminate in
  // render-time notFound. A loading.tsx here could stream a 200 shell before
  // the access decision is complete.
  if (!(await resolvePlatformOperationsAccess())) {
    notFound();
  }

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <main className="main admin-main">
        <AdminConsole />
      </main>
    </>
  );
}
