import type {
  PlatformMediaDescriptor,
  PlatformTournamentDeadlockAutoAssignmentRun,
  PlatformTournamentDeadlockReadyCheckState,
} from "@/lib/platform-types";

export type TournamentStatus =
  | "registration_open"
  | "registration_closed"
  | "in_progress"
  | "completed"
  | "cancelled";

export type TournamentVisibility = "public" | "private";

export type TournamentDateSort = "none" | "nearest" | "farthest";

export type TournamentScope = "all" | "mine" | "registered";

export type TournamentFormat = "single_elimination" | "double_elimination" | "round_robin";

export type Rank = {
  code: string;
  label: string;
};

export type ParticipantLimit =
  | { kind: "limited"; current: number; max: number; percent: number }
  | { kind: "unlimited"; current: number };

export type TournamentSummary = {
  id: string;
  slug: string;
  title: string;
  organizerUserId: string | null;
  organizerName: string;
  organizerAvatarUrl?: string | null;
  organizerAvatarMedia?: PlatformMediaDescriptor | null;
  coverUrl?: string | null;
  coverMedia?: PlatformMediaDescriptor | null;
  startsAtIso?: string | null;
  registrationClosesAtIso?: string | null;
  startsAtLabel: string;
  registrationTimerLabel: string;
  startTimerLabel: string;
  status: TournamentStatus;
  statusLabel: string;
  visibility: TournamentVisibility;
  bracketType: TournamentFormat;
  theme: string;
  allowedRanks: string[];
  participantCount: number;
  maxParticipants: number | null;
  teamsCount: number;
  currentUserParticipantStatus?: string | null;
  currentUserHasInviteAccess?: boolean;
  nextPollAfterMs?: number | null;
  stateVersion?: number | null;
};

export type TournamentPage = {
  items: TournamentSummary[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
};

export type TournamentListQuery = {
  search?: string;
  scope?: Exclude<TournamentScope, "all">;
  status?: TournamentStatus;
  rank?: string;
  dateSort?: Exclude<TournamentDateSort, "none">;
  limit?: number;
  offset?: number;
};

export type TournamentSchedule = {
  registrationStartsAt: string;
  registrationClosesAt: string;
  checkInStartsAt: string;
  checkInEndsAt: string;
  teamsFormAt: string;
  startsAt: string;
  timezone: string;
};

export type Registration = {
  id: string;
  userId: string;
  displayName?: string;
  entryType?: string;
  teamName?: string | null;
  status: string;
  checkInStatus: string;
  registeredAt: string;
  checkedInAt: string | null;
};

export type TeamMember = {
  userId: string;
  handle: string;
  avatarUrl: string | null;
  rank: string | null;
  subrank: string;
  isCaptain: boolean;
  isSubstitute: boolean;
};

export type Team = {
  id: string;
  name: string;
  seed: number | null;
  starterStrength: number | null;
  starterAverageStrength: number | null;
  captainId: string | null;
  color: string | null;
  emblem: string | null;
  members: TeamMember[];
};

export type Match = {
  id: string;
  roundNumber: number;
  matchOrder: number;
  teamAId: string | null;
  teamBId: string | null;
  homeLabel: string;
  awayLabel: string;
  scoreA: number | null;
  scoreB: number | null;
  winnerTeamId: string | null;
  homeSourceMatchId: string | null;
  awaySourceMatchId: string | null;
  status: string;
  matchFormat: string;
  ready: boolean;
  scheduledAt: string | null;
};

export type BracketStatus = "pending" | "teams_ready" | "ready";

export type TournamentBracketCapabilities = {
  canManage: boolean;
  canScheduleMatches: boolean;
  canReportMatches: boolean;
};

export type Bracket = {
  tournamentId: string;
  /** Structural graph state; never use this as a permission signal. */
  status: BracketStatus;
  tournamentStatus: TournamentStatus;
  revision: number;
  capabilities: TournamentBracketCapabilities;
  /** @deprecated Use capabilities.canManage. Kept for existing API consumers. */
  canManage: boolean;
  teams: Team[];
  matches: Match[];
  nextPollAfterMs?: number | null;
  stateVersion?: number | null;
  sseAdmissionTicket?: string | null;
  bracketProbeTicket?: string | null;
};

export type ActiveTournamentCommitment = {
  id: string;
  tournamentId: string;
  tournamentSlug: string;
  tournamentName: string;
  assignmentRunId: string;
  teamId: string;
  teamName: string;
  activatedAt: string;
};

export type TournamentDetail = TournamentSummary & {
  description: string;
  visibility: "public" | "private" | string;
  bracketType: TournamentFormat;
  matchFormat: string;
  finalFormat: string;
  participantMode: string;
  schedule: TournamentSchedule | null;
  registrations: Registration[];
  teams: Team[];
  bracket: Bracket;
  readyCheckState: PlatformTournamentDeadlockReadyCheckState | null;
  deadlockAssignment: PlatformTournamentDeadlockAutoAssignmentRun | null;
  activeCommitment: ActiveTournamentCommitment | null;
};

export type HeroOption = {
  name: string;
  theme: string;
};

export type ContactField = {
  label: string;
  value: string;
};

export type TeammatePreference = {
  slot: string;
  configured: boolean;
  roles: string[];
  heroes: string[];
};

export type PlayerProfile = {
  id: string;
  displayName: string;
  handle: string;
  avatarUrl: string | null;
  avatarMedia: PlatformMediaDescriptor | null;
  bannerUrl: string | null;
  bannerMedia: PlatformMediaDescriptor | null;
  accountEmail: string;
  rank: string;
  subrank: string;
  hoursRange: string;
  roles: string[];
  heroes: string[];
  heroPool: HeroOption[];
  completionPercent: number;
  teamName: string;
  teammatePreferences: TeammatePreference[];
  contacts: ContactField[];
};
