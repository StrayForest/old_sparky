import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { safeAuthReturnPath, withSteamAuthStatus } from "@/lib/auth-navigation";

export const metadata: Metadata = {
  title: "Завершение входа через Steam",
  referrer: "no-referrer",
  robots: { index: false, follow: false }
};

export default async function SteamCompletePage({
  searchParams
}: {
  searchParams: Promise<{ flow?: string; returnTo?: string; steam_auth?: string }>;
}) {
  const { flow, returnTo, steam_auth: steamAuth } = await searchParams;
  const destination = safeAuthReturnPath(returnTo);
  if (flow === "link" && (
    destination === "/profile/me"
    || destination.startsWith("/profile/me?")
    || destination.startsWith("/profile/me#")
  )) {
    redirect(withSteamAuthStatus(destination, steamAuth === "success" ? "success" : "error"));
  }
  if (steamAuth === "success") {
    redirect(destination);
  }
  redirect(`/auth/login?steam_auth=error&returnTo=${encodeURIComponent(destination)}`);
}
