import type {
  ParticipantLimit,
  Rank,
  TournamentStatus,
  TournamentSummary,
  TournamentVisibility
} from "@/lib/types";
import { rankOptions } from "@/lib/deadlock";

export const tournamentStatuses: Array<{ value: TournamentStatus; label: string }> = [
  { value: "registration_open", label: "Регистрация открыта" },
  { value: "registration_closed", label: "Регистрация закрыта" },
  { value: "in_progress", label: "Идет" },
  { value: "completed", label: "Завершен" },
  { value: "cancelled", label: "Отменен" }
];

export const ranks: Rank[] = rankOptions.map((label, index) => ({
  code: `r${rankOptions.length - index}`,
  label
}));

const rankOrder = new Map(ranks.map((rank, index) => [rank.code, index]));

export function sortRanksByStrengthDesc(rankCodes: string[]): string[] {
  return [...rankCodes].sort((left, right) => {
    const leftOrder = rankOrder.get(left) ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = rankOrder.get(right) ?? Number.MAX_SAFE_INTEGER;
    if (leftOrder !== rightOrder) {
      return leftOrder - rightOrder;
    }
    return left.localeCompare(right);
  });
}

export function statusLabel(status: TournamentStatus): string {
  return tournamentStatuses.find((item) => item.value === status)?.label ?? "Регистрация закрыта";
}

export function registrationTimerLabel(status: TournamentStatus): string {
  if (status === "registration_open") {
    return "Рег. открыта";
  }
  return "Регистрация закрыта";
}

export function participantLimit(tournament: TournamentSummary): ParticipantLimit {
  if (!tournament.maxParticipants || tournament.maxParticipants >= 999_999_999) {
    return { kind: "unlimited", current: tournament.participantCount };
  }

  return {
    kind: "limited",
    current: tournament.participantCount,
    max: tournament.maxParticipants,
    percent: Math.min(100, Math.round((tournament.participantCount / tournament.maxParticipants) * 100))
  };
}

export function isActiveParticipantStatus(
  status: string | null | undefined
): status is "registered" | "confirmed" | "checked_in" {
  return status === "registered" || status === "confirmed" || status === "checked_in";
}


export function normalizeVisibility(value: string | null | undefined): TournamentVisibility {
  return value === "private" || value === "invite_only" ? "private" : "public";
}
