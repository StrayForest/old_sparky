export type PlatformUser = {
  id: string;
  email: string | null;
  display_name: string;
  status: string;
  created_at: string;
  roles: string[];
  can_create_public_tournaments: boolean;
  public_tournament_credits?: number;
  private_tournament_credits?: number;
  private_tournament_monthly_remaining?: number;
  private_tournament_monthly_limit?: number;
  avatar_url?: string | null;
  avatar_media?: PlatformMediaDescriptor | null;
  steam_id?: string | null;
  steam_linked?: boolean;
  has_password?: boolean;
  can_unlink_steam?: boolean;
};

export type PlatformAuthSessionResponse = {
  user: PlatformUser;
  expires_at: string;
};

export type PlatformAuthSecurityConfig = {
  public_registration_enabled: boolean;
  email_verification_required: boolean;
  turnstile_mode: "off" | "always" | "adaptive";
  turnstile_site_key: string | null;
  steam_login_enabled?: boolean;
};

export type PlatformMediaVariant = {
  name: string;
  width: number;
  height: number;
  byte_size: number;
  url: string;
};

export type PlatformMediaDescriptor = {
  asset_id: string;
  purpose: "profile_avatar" | "profile_banner" | "tournament_banner" | string;
  status: "pending" | "processing" | "ready" | "failed" | "replaced" | "deleted" | string;
  error_code: string | null;
  variants: PlatformMediaVariant[];
};

export type PlatformMediaAccepted = {
  asset_id: string;
  status: string;
  status_url: string;
};

export type PlatformMediaDeleteAccepted = {
  asset_id: string | null;
  status: "cleanup_pending" | "deleted";
};

export type PlatformAuthRegistrationResponse = {
  user: PlatformUser | null;
  expires_at: string | null;
  verification_required: boolean;
  retry_after_seconds?: number;
};

export type PlatformHomePatch = {
  id: string;
  title: string;
  excerpt: string;
  published_at: string;
  url: string;
};

export type PlatformHomeVideo = {
  id: string;
  title: string;
  published_at: string;
  url: string;
  thumbnail_url: string;
};

export type PlatformHomeContent = {
  patches: PlatformHomePatch[];
  videos: PlatformHomeVideo[];
  generated_at: string;
  patches_available: boolean;
  videos_available: boolean;
};

export type PlatformDeadlockGameAsset = {
  name: string;
  image_url: string;
  source_available: boolean;
};

export type PlatformDeadlockGameAssets = {
  heroes: PlatformDeadlockGameAsset[];
  ranks: PlatformDeadlockGameAsset[];
};

export type PlatformPatchAbility = {
  name: string;
  icon_url: string | null;
  changes: string[];
};

export type PlatformPatchSection = {
  kind: "general" | "objective" | "item" | "hero";
  title: string;
  hero_name: string | null;
  item_name?: string | null;
  item_category?: "weapon" | "vitality" | "spirit" | null;
  item_icon_url?: string | null;
  objective_key?: "urn" | "unstable_rift" | null;
  objective_icon_url?: string | null;
  changes: string[];
  abilities: PlatformPatchAbility[];
};

export type PlatformPatchDetail = {
  id: string;
  title: string;
  published_at: string;
  url: string;
  content: string;
  sections: PlatformPatchSection[];
};

export type PlatformProfile = {
  user_id: string;
  account_email?: string | null;
  display_name: string;
  handle: string | null;
  avatar_url: string | null;
  banner_url: string | null;
  avatar_media?: PlatformMediaDescriptor | null;
  banner_media?: PlatformMediaDescriptor | null;
  bio: string | null;
  contact_email: string | null;
  region: string | null;
  steam_id: string | null;
  discord_account: string | null;
  captain_team_name?: string | null;
  updated_at: string;
};

export type PlatformDeadlockProfile = {
  user_id: string;
  rank: string;
  subrank: number;
  playtime: string;
  roles: string[];
  pool: string[];
  captain_priority: string | null;
  updated_at: string;
};

export type PlatformAuditLog = {
  id: number;
  action: string;
  subject_type: string;
  subject_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  actor_display_name?: string | null;
  actor_email?: string | null;
};

export type PlatformAdminOverview = {
  users_total: number;
  tournaments_total: number;
  tournaments_attention_total?: number;
  audit_events_total: number;
  preprod_test_runs_total?: number;
  preprod_test_users_total?: number;
};

export type PlatformAdminPreprodTestRun = {
  marker: string;
  status: string;
  origin: string | null;
  requested_users: number;
  created_users: number;
  tournaments_created: number;
  active_participants: number;
  teams_count: number;
  matches_count: number;
  report_path: string | null;
  report: Record<string, unknown>;
  cleanup_state: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
};

export type PlatformAdminPreprodCleanupResult = {
  ok: boolean;
  runs_updated: number;
  tournaments_deleted: number;
  users_deleted: number;
  audit_logs_deleted: number;
  markers: string[];
  remaining_users: number;
  remaining_tournaments: number;
};

export type PlatformStatsRankBucket = {
  rank: string;
  count: number;
};

export type PlatformStatsOverview = {
  total_tournaments: number;
  completed_tournaments: number;
  active_upcoming_tournaments: number;
  registered_participants: number;
  completed_matches: number;
  deadlock_profiles_total: number;
  registered_participants_with_deadlock_profile: number;
  deadlock_profile_coverage_percent: number;
  deadlock_rank_distribution: PlatformStatsRankBucket[];
};

export type PlatformTournament = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  cover_url?: string | null;
  cover_media?: PlatformMediaDescriptor | null;
  visibility: string;
  status: string;
  format_slug: string;
  organizer_user_id: string;
  organizer_display_name: string | null;
  organizer_avatar_url?: string | null;
  organizer_avatar_media?: PlatformMediaDescriptor | null;
  participant_count: number;
  max_participants?: number | null;
  allowed_ranks: string[];
  has_locked_deadlock_roster: boolean;
  current_user_participant_status?: string | null;
  current_user_has_invite_access?: boolean;
  registration_starts_at?: string | null;
  registration_closes_at?: string | null;
  ready_check_starts_at?: string | null;
  ready_check_ends_at?: string | null;
  captain_selection_starts_at?: string | null;
  starts_at?: string | null;
  match_format?: string;
  final_format?: string;
  captain_response_deadline_minutes?: number | null;
  teams_count?: number | null;
  automation_ready_check_started_at?: string | null;
  automation_ready_check_closed_at?: string | null;
  automation_captain_round_started_at?: string | null;
  automation_captain_round_finalized_at?: string | null;
  automation_assignment_generated_at?: string | null;
  automation_last_error?: string | null;
  automation_failure_count?: number;
  automation_retry_after?: string | null;
  created_at: string;
  available_next_statuses: string[];
};

export type PlatformAdminTournament = PlatformTournament & {
  match_count: number;
  latest_round_number: number | null;
  unfinished_match_count: number;
  completed_match_count: number;
  cancelled_match_count: number;
  admin_override_warning: string | null;
  admin_recovery_hint: string | null;
};

export type PlatformTournamentParticipant = {
  id: string;
  tournament_id: string;
  user_id: string;
  display_name: string;
  status: string;
  entry_type: string;
  team_name: string | null;
  moderation_note: string | null;
  moderated_at: string | null;
  moderated_by_user_id: string | null;
  created_at: string;
};

export type PlatformTournamentDeadlockReadyRound = {
  id: number;
  tournament_id: string;
  status: string;
  eligible_participant_count: number;
  ready_count: number;
  declined_count: number;
  initiated_by_user_id: string | null;
  created_at: string;
  closed_at: string | null;
  current_user_choice: string | null;
};

export type PlatformTournamentDeadlockReadyVote = {
  round_id: number;
  tournament_id: string;
  status: string;
  eligible_participant_count: number;
  current_user_choice: string;
  changed: boolean;
  server_received_at: string;
};

export type PlatformTournamentDeadlockReadyCheckState = {
  active_round: PlatformTournamentDeadlockReadyRound | null;
  latest_round: PlatformTournamentDeadlockReadyRound | null;
  state_version?: number | null;
};

export type PlatformTournamentDeadlockCaptainPreviewCandidate = {
  user_id: string;
  display_name: string;
  rank: string;
  subrank: number;
  playtime: string;
  captain_priority: string | null;
  captain_priority_bucket: number;
  strength: number;
  projected_team_id: string | null;
};

export type PlatformTournamentDeadlockCaptainPreview = {
  teams_count: number;
  source_ready_round_id: number | null;
  ready_player_count: number;
  candidates: PlatformTournamentDeadlockCaptainPreviewCandidate[];
};

export type PlatformTournamentDeadlockCaptainEntry = {
  user_id: string;
  display_name: string;
  rank: string | null;
  subrank: number | null;
  playtime: string | null;
  captain_priority: string | null;
  captain_priority_bucket: number | null;
  strength: number | null;
  offer_order: number;
  state: string;
  assigned_team_id: string | null;
  responded_at: string | null;
  updated_at: string;
};

export type PlatformTournamentDeadlockCaptainRound = {
  id: number;
  tournament_id: string;
  source_ready_round_id: number;
  teams_count: number;
  status: string;
  candidate_count: number;
  accepted_count: number;
  offered_count: number;
  declined_count: number;
  queued_count: number;
  assigned_count: number;
  initiated_by_user_id: string | null;
  created_at: string;
  closed_at: string | null;
  finalized_at: string | null;
  can_finalize: boolean;
  current_user_entry: PlatformTournamentDeadlockCaptainEntry | null;
  entries: PlatformTournamentDeadlockCaptainEntry[];
};

export type PlatformTournamentDeadlockCaptainRoundState = {
  active_round: PlatformTournamentDeadlockCaptainRound | null;
  latest_round: PlatformTournamentDeadlockCaptainRound | null;
};

export type PlatformTournamentDeadlockAutoAssignmentPlayer = {
  user_id: string;
  username: string | null;
  rank: string;
  subrank: number;
  playtime: string | null;
  strength: number;
  pool: string[];
  roles: string[];
};

export type PlatformTournamentDeadlockAutoAssignmentCaptain =
  PlatformTournamentDeadlockAutoAssignmentPlayer & {
    assigned_role: string;
  };

export type PlatformTournamentDeadlockAutoAssignmentSlot = {
  slot_number: number;
  allowed_roles: string[];
  desired_heroes: string[];
  assigned_player: PlatformTournamentDeadlockAutoAssignmentPlayer;
  assigned_role: string;
  matched_desired_heroes: string[];
  desired_match_count: number;
  role_match: boolean;
};

export type PlatformTournamentDeadlockAutoAssignmentTeam = {
  team_id: string;
  starter_strength: number;
  starter_average_strength: number;
  captain: PlatformTournamentDeadlockAutoAssignmentCaptain;
  starter_slots: PlatformTournamentDeadlockAutoAssignmentSlot[];
  reserve_slot: PlatformTournamentDeadlockAutoAssignmentSlot | null;
};

export type PlatformTournamentDeadlockAutoAssignmentOptimizationSummary = {
  threshold: number;
  spread_percent: number;
  mad_percent: number;
  std_percent: number;
  candidate_pool_size: number;
  selected_player_count: number;
  source: string;
  pool_step: number | null;
  role_rescue_used: boolean;
  accepted_swap_moves: number;
  accepted_replacement_moves: number;
  accepted_hierarchy_moves: number;
  stage: number | null;
};

export type PlatformTournamentDeadlockAutoAssignmentPreferenceMetrics = {
  starter_slots_total: number;
  starter_preference_slots_total: number;
  starter_role_restricted_slots_total: number;
  starter_role_match_count: number;
  starter_role_match_rate_percent: number;
  starter_desired_slots_total: number;
  starter_desired_slots_with_any_match: number;
  starter_desired_slot_hit_rate_percent: number;
  starter_desired_heroes_requested_total: number;
  starter_desired_heroes_hit_total: number;
  starter_desired_hero_hit_rate_percent: number;
  starter_preference_slots_fully_honored: number;
  starter_preference_slots_fully_honored_rate_percent: number;
  reserve_slots_total: number;
  reserve_desired_slots_total: number;
  reserve_desired_slots_with_any_match: number;
};

export type PlatformTournamentDeadlockAutoAssignmentRun = {
  id: string;
  tournament_id: string;
  source_captain_round_id: number;
  source_ready_round_id: number;
  created_by_user_id: string | null;
  status: string;
  published_at: string | null;
  published_by_user_id: string | null;
  locked_at: string | null;
  locked_by_user_id: string | null;
  summary_text: string;
  teams: PlatformTournamentDeadlockAutoAssignmentTeam[];
  optimization_summary: PlatformTournamentDeadlockAutoAssignmentOptimizationSummary;
  preference_metrics: PlatformTournamentDeadlockAutoAssignmentPreferenceMetrics;
  candidate_pool_user_ids: string[];
  leftover_user_ids: string[];
  is_stale: boolean;
  stale_reasons: string[];
  created_at: string;
};

export type PlatformTournamentDeadlockAutoAssignmentState = {
  latest_run: PlatformTournamentDeadlockAutoAssignmentRun | null;
  published_run: PlatformTournamentDeadlockAutoAssignmentRun | null;
};

export type PlatformTournamentDeadlockAutoAssignmentJob = {
  task_id: string;
  status: string;
};

export type PlatformDeadlockDreamSlot = {
  user_id: string;
  slot_number: number;
  allowed_roles: string[];
  desired_heroes: string[];
  updated_at: string | null;
};

export type PlatformTournamentScopedProfile = {
  profile: PlatformProfile;
  deadlock_profile: PlatformDeadlockProfile | null;
  dream_slots: PlatformDeadlockDreamSlot[];
  stats: {
    tournaments_played: number;
    tournaments_organized: number;
    tournaments_won: number;
    recent_tournaments: string[];
  };
};

export type PlatformTournamentInvite = {
  id: string;
  tournament_id: string;
  code: string;
  note: string | null;
  max_uses: number;
  use_count: number;
  remaining_uses: number;
  expires_at: string | null;
  revoked_at: string | null;
  last_claimed_by_user_id: string | null;
  last_claimed_at: string | null;
  created_at: string;
  is_active: boolean;
};

export type PlatformTournamentMatch = {
  id: string;
  tournament_id: string;
  title: string | null;
  round_number: number;
  sequence_number: number;
  home_label: string;
  away_label: string;
  scheduled_at: string | null;
  status: string;
  home_score: number | null;
  away_score: number | null;
  winner_side: string | null;
  report_note: string | null;
  reported_by_user_id: string | null;
  reported_at: string | null;
  created_at: string;
  available_next_statuses: string[];
};
