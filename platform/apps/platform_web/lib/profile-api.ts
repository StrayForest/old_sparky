import { platformApiRequest } from "@/lib/platform-api";
import {
  captainPreferenceFromApi,
  captainPreferenceToApi,
  normalizeDreamSlots,
  type CaptainPreference,
} from "@/lib/profile-model";
import type {
  PlatformDeadlockDreamSlot,
  PlatformDeadlockProfile,
} from "@/lib/platform-types";

export type TournamentProfileUpdate = {
  rank: string;
  subrank: string;
  hoursRange: string;
  roles: string[];
  heroes: string[];
  captainPreference: CaptainPreference;
};

export type TournamentProfileUpdateResult = TournamentProfileUpdate;

type CaptainProfileResponse = {
  captain_team_name: string;
  dream_slots: PlatformDeadlockDreamSlot[];
};

export async function updateTournamentProfile(
  payload: TournamentProfileUpdate
): Promise<TournamentProfileUpdateResult> {
  const updated = await platformApiRequest<PlatformDeadlockProfile>(
    "/profiles/me/deadlock",
    {
      method: "PUT",
      body: JSON.stringify({
        rank: payload.rank,
        subrank: numericSubrank(payload.subrank),
        playtime: payload.hoursRange,
        roles: payload.roles,
        pool: payload.heroes,
        captain_priority: captainPreferenceToApi(payload.captainPreference),
      }),
    }
  );

  return {
    rank: updated.rank,
    subrank: romanSubrank(updated.subrank),
    hoursRange: updated.playtime,
    roles: [...updated.roles],
    heroes: [...updated.pool],
    captainPreference: captainPreferenceFromApi(updated.captain_priority),
  };
}

export async function updateCaptainProfile(
  teamName: string,
  slots: PlatformDeadlockDreamSlot[]
): Promise<{
  teamName: string;
  dreamSlots: PlatformDeadlockDreamSlot[];
}> {
  const updated = await platformApiRequest<CaptainProfileResponse>(
    "/profiles/me/captain",
    {
      method: "PUT",
      body: JSON.stringify({
        captain_team_name: teamName,
        slots: slots.map((slot) => ({
          slot_number: slot.slot_number,
          allowed_roles: slot.allowed_roles,
          desired_heroes: slot.desired_heroes,
        })),
      }),
    }
  );

  return {
    teamName: updated.captain_team_name,
    dreamSlots: normalizeDreamSlots(updated.dream_slots),
  };
}

function numericSubrank(subrank: string): number {
  const roman: Record<string, number> = {
    I: 1,
    II: 2,
    III: 3,
    IV: 4,
    V: 5,
    VI: 6,
  };
  return roman[subrank.trim()] ?? (Number(subrank) || 1);
}

function romanSubrank(subrank: number): string {
  return ["", "I", "II", "III", "IV", "V", "VI"][subrank] ?? String(subrank);
}
