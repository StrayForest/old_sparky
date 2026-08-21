import type { Metadata } from "next";
import { cookies } from "next/headers";
import { notFound } from "next/navigation";
import { AdminConsole } from "@/components/admin/admin-console";
import { AdminUserDeletion } from "@/components/admin/admin-user-deletion";
import { getServerCurrentUser, platformSessionCookieName } from "@/lib/server-auth";

export const metadata: Metadata = {
  title: "Operations",
  robots: {
    index: false,
    follow: false
  }
};

export default async function PlatformOperationsPage() {
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const authSnapshot = requestCookies.has(platformSessionCookieName())
    ? await getServerCurrentUser(cookieHeader)
    : null;
  const user = authSnapshot?.user ?? null;
  const smokeFallback = (process.env.PLATFORM_API_BASE_URL ?? "").includes("127.0.0.1:9");
  const hasAdminRole = user?.roles.some((role) => role === "admin" || role === "superadmin");

  if (!smokeFallback && !hasAdminRole) {
    notFound();
  }

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <main className="main admin-main">
        <AdminConsole />
        <AdminUserDeletion />
      </main>
    </>
  );
}
