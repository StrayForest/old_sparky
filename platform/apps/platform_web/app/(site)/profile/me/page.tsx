import type { Metadata } from "next";
import { cookies } from "next/headers";
import { Hero } from "@/components/layout/hero";
import { ProfileAccessState } from "@/components/profile/profile-access-state";
import { ProfileEditor } from "@/components/profile/editor/profile-editor";
import { parseProfileTab } from "@/lib/profile-model";
import {
  getServerProfileHeroNames,
  getServerProfileWorkspace,
} from "@/lib/server-profile-workspace";
import {
  getServerCurrentUser,
  platformSessionCookieName,
} from "@/lib/server-auth";

export const metadata: Metadata = {
  title: "Мой профиль",
};

export default async function MyProfilePage({
  searchParams,
}: {
  searchParams: Promise<{ steam_auth?: string; tab?: string }>;
}) {
  const { steam_auth: steamAuth, tab } = await searchParams;
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const auth = requestCookies.has(platformSessionCookieName())
    ? await getServerCurrentUser(cookieHeader)
    : { status: "anonymous" as const, user: null };

  let workspace = null;
  let heroNames: string[] = [];
  let profileUnavailable = false;

  if (auth.status === "authenticated") {
    try {
      [workspace, heroNames] = await Promise.all([
        getServerProfileWorkspace(cookieHeader),
        getServerProfileHeroNames(),
      ]);
    } catch {
      profileUnavailable = true;
    }
  }

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Профиль игрока"
        subtitle="Турнирные данные, капитанские предпочтения и данные профиля."
      />
      <main className="main">
        {auth.status === "authenticated" && workspace ? (
          <ProfileEditor
            captainPreference={workspace.captainPreference}
            dreamSlots={workspace.dreamSlots}
            heroNames={heroNames}
            initialTab={parseProfileTab(tab)}
            profile={workspace.profile}
            steamAuthStatus={
              steamAuth === "error" || steamAuth === "success"
                ? steamAuth
                : undefined
            }
          />
        ) : auth.status === "anonymous" ||
          (auth.status === "authenticated" &&
            !workspace &&
            !profileUnavailable) ? (
          <ProfileAccessState state="anonymous" />
        ) : (
          <ProfileAccessState state="unavailable" />
        )}
      </main>
    </>
  );
}
