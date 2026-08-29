"use client";

import { useEffect, useMemo, useState } from "react";
import { NotebookPen, UserRound } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { PreparedMedia } from "@/components/media/prepared-media";
import { ProfileAccessState } from "@/components/profile/profile-access-state";
import { AccountProfileTab } from "@/components/profile/editor/account-profile-tab";
import { CaptainProfileTab } from "@/components/profile/editor/captain-profile-tab";
import {
  calculateProfileCompletion,
  cloneContacts,
  cloneDreamSlot,
  hasPreparedOrLegacyMedia,
  ProfileBanner,
} from "@/components/profile/editor/profile-editor-shared";
import { PublicProfileView } from "@/components/profile/public-profile-view";
import { TournamentProfileTab } from "@/components/profile/editor/tournament-profile-tab";
import {
  parseProfileTab,
  profileTabs,
  type CaptainPreference,
  type ProfileTabId,
} from "@/lib/profile-model";
import type { PlatformDeadlockDreamSlot } from "@/lib/platform-types";
import type { ContactField, PlayerProfile } from "@/lib/types";

export function ProfileEditor({
  initialTab = "tournament",
  profile,
  captainPreference,
  dreamSlots,
  heroNames,
  steamAuthStatus,
}: {
  initialTab?: ProfileTabId;
  profile: PlayerProfile;
  captainPreference: CaptainPreference;
  dreamSlots: PlatformDeadlockDreamSlot[];
  heroNames: string[];
  steamAuthStatus?: "error" | "success";
}) {
  const { status, user } = useAuth();
  const [activeTab, setActiveTab] = useState<ProfileTabId>(initialTab);
  const [summaryProfile, setSummaryProfile] = useState(profile);
  const [summaryDreamSlots, setSummaryDreamSlots] = useState(() =>
    dreamSlots.map(cloneDreamSlot)
  );
  const [summaryContacts, setSummaryContacts] = useState<ContactField[]>(() =>
    cloneContacts(profile.contacts)
  );

  const heroOptions = useMemo(() => {
    const merged = [...heroNames, ...profile.heroes];
    return merged
      .filter((hero, index) => hero && merged.indexOf(hero) === index)
      .map((name) => ({ name, theme: "h-blue" }));
  }, [heroNames, profile.heroes]);

  const completionPercent = calculateProfileCompletion(
    summaryProfile,
    summaryDreamSlots,
    summaryContacts
  );

  useEffect(() => {
    const handlePopState = () => {
      const params = new URLSearchParams(window.location.search);
      setActiveTab(parseProfileTab(params.get("tab")));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  if (status !== "authenticated" || !user) {
    return (
      <ProfileAccessState
        state={status === "anonymous" ? "anonymous" : "unavailable"}
      />
    );
  }

  function selectTab(tab: ProfileTabId) {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    window.history.pushState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function previewTournament(next: PlayerProfile) {
    setSummaryProfile((current) => ({
      ...current,
      rank: next.rank,
      subrank: next.subrank,
      hoursRange: next.hoursRange,
      roles: [...next.roles],
      heroes: [...next.heroes],
      heroPool: next.heroPool.map((hero) => ({ ...hero })),
    }));
  }

  function previewCaptain(
    teamName: string,
    nextDreamSlots: PlatformDeadlockDreamSlot[]
  ) {
    setSummaryProfile((current) => ({ ...current, teamName }));
    setSummaryDreamSlots(nextDreamSlots.map(cloneDreamSlot));
  }

  function previewAccount(
    next: PlayerProfile,
    contacts: ContactField[]
  ) {
    const nextContacts = cloneContacts(contacts);
    setSummaryProfile((current) => ({
      ...current,
      displayName: next.displayName,
      handle: next.handle || current.handle,
      avatarUrl: next.avatarUrl,
      avatarMedia: next.avatarMedia,
      bannerUrl: next.bannerUrl,
      bannerMedia: next.bannerMedia,
      accountEmail: next.accountEmail,
      contacts: nextContacts,
    }));
    setSummaryContacts(nextContacts);
  }

  return (
    <>
      <section className="panel profile-summary">
        <PreparedMedia
          alt=""
          className="profile-summary-banner"
          descriptor={summaryProfile.bannerMedia}
          fallbackUrl={summaryProfile.bannerUrl}
          sizes="(max-width: 820px) 100vw, 1200px"
        />
        <div className="identity">
          <div
            className={
              hasPreparedOrLegacyMedia(
                summaryProfile.avatarMedia,
                summaryProfile.avatarUrl
              )
                ? "avatar profile-avatar has-image"
                : "avatar profile-avatar profile-avatar-empty"
            }
          >
            {hasPreparedOrLegacyMedia(
              summaryProfile.avatarMedia,
              summaryProfile.avatarUrl
            ) ? (
              <PreparedMedia
                alt=""
                className="profile-avatar-image"
                descriptor={summaryProfile.avatarMedia}
                fallbackUrl={summaryProfile.avatarUrl}
                height={96}
                sizes="96px"
                width={96}
              />
            ) : (
              <UserRound aria-hidden="true" />
            )}
          </div>
          <div className="identity-name-wrap">
            <h2 className="profile-name">{summaryProfile.displayName}</h2>
          </div>
        </div>
        <div className="completion">
          <div
            className="panel-title mb-3"
            data-testid="profile-completion"
          >
            Профиль заполнен на {completionPercent}%
          </div>
          <div className="progress-row">
            <progress
              aria-label="Заполнение профиля"
              className="progress-track"
              max={100}
              value={completionPercent}
            />
          </div>
        </div>
      </section>

      <div className="tabs">
        {profileTabs.map(([id, label]) => (
          <button
            key={id}
            className={
              activeTab === id ? "tab-button active" : "tab-button"
            }
            onClick={() => selectTab(id)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>

      <div
        hidden={activeTab !== "tournament"}
        id="profile-panel-tournament"
      >
        <TournamentProfileTab
          heroOptions={heroOptions}
          initialCaptainPreference={captainPreference}
          initialProfile={profile}
          onPreview={previewTournament}
        />
      </div>

      <div
        hidden={activeTab !== "captain"}
        id="profile-panel-captain"
      >
        <CaptainProfileTab
          heroOptions={heroOptions}
          initialDreamSlots={dreamSlots}
          initialTeamName={profile.teamName}
          onPreview={previewCaptain}
        />
      </div>

      <div
        hidden={activeTab !== "account"}
        id="profile-panel-account"
      >
        <ProfileBanner
          icon={<NotebookPen size={38} />}
          title="Данные профиля"
          text="Управляйте отображением профиля, контактами и безопасностью аккаунта."
        />
        <PublicProfileView profile={summaryProfile} />
        <AccountProfileTab
          initialProfile={profile}
          onPreview={previewAccount}
          steamAuthStatus={steamAuthStatus}
        />
      </div>
    </>
  );
}
