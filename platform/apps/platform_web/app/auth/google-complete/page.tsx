import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { safeAuthReturnPath } from "@/lib/auth-navigation";

export const metadata: Metadata = {
  title: "Завершение входа через Google",
  referrer: "no-referrer",
  robots: { index: false, follow: false }
};

export default async function GoogleCompletePage({
  searchParams
}: {
  searchParams: Promise<{ returnTo?: string; google_auth?: string }>;
}) {
  const { returnTo, google_auth: googleAuth } = await searchParams;
  const destination = safeAuthReturnPath(returnTo);
  if (googleAuth === "success") {
    redirect(destination);
  }
  redirect(`/auth/login?google_auth=error&returnTo=${encodeURIComponent(destination)}`);
}
