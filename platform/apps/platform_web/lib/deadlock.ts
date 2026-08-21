import type { PlatformDeadlockDreamSlot } from "./platform-types";

export const deadlockHeroes = [
  "Abrams",
  "Apollo",
  "Bebop",
  "Billy",
  "Calico",
  "Celeste",
  "The Doorman",
  "Drifter",
  "Dynamo",
  "Graves",
  "Grey Talon",
  "Haze",
  "Holliday",
  "Infernus",
  "Ivy",
  "Kelvin",
  "Lady Geist",
  "Lash",
  "McGinnis",
  "Mina",
  "Mirage",
  "Mo & Krill",
  "Paradox",
  "Paige",
  "Pocket",
  "Rem",
  "Seven",
  "Shiv",
  "Silver",
  "Sinclair",
  "Venator",
  "Victor",
  "Vindicta",
  "Viscous",
  "Vyper",
  "Warden",
  "Wraith",
  "Yamato",
] as const;

export const rankOptions = [
  "Eternus",
  "Ascendant",
  "Phantom",
  "Oracle",
  "Emissary",
  "Ritualist",
  "Mystic",
  "Sentinel",
  "Acolyte",
  "Seeker",
  "Initiate",
] as const;

export const DEADLOCK_RANK_ASSET_VERSION = "20260731-1";

export function isSoloTournamentFormat(formatSlug: string | null | undefined): boolean {
  return formatSlug === "solo";
}

export const playtimeOptions = [
  "0-500",
  "501-1000",
  "1001-1500",
  "1501-2000",
  "2001-3000",
  "3000+",
] as const;

export const roleOptions = ["Carry", "Semi-Carry", "Support", "Semi-Support"] as const;
export const captainPriorityOptions = ["yes", "no", "neutral"] as const;

export function captainPriorityEligible(rank: string): boolean {
  void rank;
  return true;
}

export function deadlockHeroIconPath(heroName: string): string {
  return `/api/v1/content/game-assets/heroes/${encodeURIComponent(heroName)}.png`;
}

export const deadlockHeroPlaceholderPath = "/assets/heroes/placeholder.svg";

export function deadlockRankAssetSlug(rankName: string): string {
  const rankCode = /^r(\d+)$/.exec(rankName);
  const rankNumber = Number(rankCode?.[1] ?? 0);
  const resolvedRankName = rankOptions[rankOptions.length - rankNumber] ?? rankName;
  return resolvedRankName
    .trim()
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function versionDeadlockRankAsset(path: string): string {
  return `${path}?v=${DEADLOCK_RANK_ASSET_VERSION}`;
}

export function deadlockRankIconPath(rankName: string): string {
  const slug = deadlockRankAssetSlug(rankName);
  return versionDeadlockRankAsset(`/api/v1/content/game-assets/ranks/${slug}.webp`);
}

export const deadlockRankPlaceholderPath = versionDeadlockRankAsset("/assets/ranks/Initiate.webp");

export function toggleHeroSelection(current: string[], hero: string, maxSelected = 3): string[] {
  if (current.includes(hero)) {
    return current.filter((item) => item !== hero);
  }
  if (current.length >= maxSelected) {
    return current;
  }
  return [...current, hero];
}

export function emptyDreamSlot(slotNumber: number): PlatformDeadlockDreamSlot {
  return {
    user_id: "",
    slot_number: slotNumber,
    allowed_roles: [],
    desired_heroes: [],
    updated_at: null,
  };
}
