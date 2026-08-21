import { deadlockHeroes, emptyDreamSlot } from "@/lib/deadlock";
import type {
  PlatformDeadlockDreamSlot,
  PlatformDeadlockProfile,
  PlatformProfile,
} from "@/lib/platform-types";
import type { PlayerProfile } from "@/lib/types";

export const profileTabs = [
  ["tournament", "Турнирный профиль"],
  ["captain", "Профиль капитана"],
  ["account", "Аккаунт"],
] as const;

export type ProfileTabId = (typeof profileTabs)[number][0];
export type CaptainPreference = "Повысить" | "Нейтрально" | "Понизить";

export type ProfileWorkspacePayload = {
  profile: PlatformProfile;
  deadlock_profile: PlatformDeadlockProfile | null;
  dream_slots: PlatformDeadlockDreamSlot[];
};

export type ProfileWorkspace = {
  profile: PlayerProfile;
  captainPreference: CaptainPreference;
  dreamSlots: PlatformDeadlockDreamSlot[];
};

export function parseProfileTab(value: string | null | undefined): ProfileTabId {
  return value === "captain" || value === "account" ? value : "tournament";
}

export function captainPreferenceFromApi(
  value: string | null | undefined
): CaptainPreference {
  if (value === "yes") {
    return "Повысить";
  }
  if (value === "no") {
    return "Понизить";
  }
  return "Нейтрально";
}

export function captainPreferenceToApi(value: CaptainPreference): "yes" | "no" | "neutral" {
  if (value === "Повысить") {
    return "yes";
  }
  if (value === "Понизить") {
    return "no";
  }
  return "neutral";
}

export function normalizeDreamSlots(
  slots: readonly PlatformDeadlockDreamSlot[]
): PlatformDeadlockDreamSlot[] {
  const bySlot = new Map(slots.map((slot) => [slot.slot_number, slot]));
  return Array.from({ length: 6 }, (_, index) => {
    const slot = bySlot.get(index + 1);
    return slot
      ? {
          ...slot,
          allowed_roles: [...slot.allowed_roles],
          desired_heroes: [...slot.desired_heroes],
        }
      : emptyDreamSlot(index + 1);
  });
}

export function mapProfileWorkspacePayload(
  payload: ProfileWorkspacePayload
): ProfileWorkspace {
  const item = payload.profile;
  const deadlock = payload.deadlock_profile;
  const id = item.user_id;
  const displayName = item.display_name || item.handle || id;
  const handle = item.handle || displayName;
  const heroes = deadlock?.pool ?? [];
  const availableHeroNames = [
    ...deadlockHeroes,
    ...heroes.filter(
      (hero) => !deadlockHeroes.includes(hero as (typeof deadlockHeroes)[number])
    ),
  ];
  const rank = deadlock?.rank ?? "";
  const subrank = deadlock?.subrank ?? null;
  const hoursRange = deadlock?.playtime ?? "";
  const roles = deadlock?.roles ?? [];

  const profile: PlayerProfile = {
    id,
    displayName,
    handle,
    avatarUrl: item.avatar_url ?? null,
    avatarMedia: item.avatar_media ?? null,
    bannerUrl: item.banner_url ?? null,
    bannerMedia: item.banner_media ?? null,
    accountEmail: item.account_email ?? "",
    rank,
    subrank: subrank == null ? "" : romanSubrank(subrank),
    hoursRange,
    roles: [...roles],
    heroes: [...heroes],
    heroPool: availableHeroNames.map((hero) => ({ name: hero, theme: "h-blue" })),
    completionPercent: calculateBaseCompletion({
      handle,
      rank,
      subrank,
      hoursRange,
      roles,
      heroes,
    }),
    teamName: item.captain_team_name ?? "",
    teammatePreferences: [],
    contacts: [
      { label: "Почта", value: item.contact_email ?? item.account_email ?? "" },
      { label: "Discord", value: item.discord_account ?? "" },
      { label: "Steam ID", value: item.steam_id ?? "" },
      { label: "Регион", value: item.region ?? "" },
    ],
  };

  return {
    profile,
    captainPreference: captainPreferenceFromApi(deadlock?.captain_priority),
    dreamSlots: normalizeDreamSlots(payload.dream_slots),
  };
}

function romanSubrank(subrank: number): string {
  return ["", "I", "II", "III", "IV", "V", "VI"][subrank] ?? String(subrank);
}

function calculateBaseCompletion(profile: {
  handle: string;
  rank: string;
  subrank: number | null;
  hoursRange: string;
  roles: string[];
  heroes: string[];
}): number {
  const checks = [
    Boolean(profile.handle),
    Boolean(profile.rank),
    Boolean(profile.subrank),
    Boolean(profile.hoursRange),
    profile.roles.length > 0,
    profile.heroes.length > 0,
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}
