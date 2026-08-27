from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping, Sequence

from python_packages.platform_domain.deadlock.constants import RANKS

TOURNAMENT_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "registration_open": ("registration_closed", "cancelled"),
    "registration_closed": ("registration_open", "in_progress", "cancelled"),
    "in_progress": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

MATCH_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "scheduled": ("live", "cancelled"),
    "live": ("scheduled", "cancelled"),
    "completed": (),
    "cancelled": ("scheduled",),
}

PARTICIPANT_STATUSES: tuple[str, ...] = (
    "registered",
    "confirmed",
    "checked_in",
    "withdrawn",
    "disqualified",
)

SELF_SERVICE_JOINABLE_TOURNAMENT_STATUSES = frozenset({"registration_open"})
SELF_SERVICE_LEAVEABLE_TOURNAMENT_STATUSES = frozenset(
    {"registration_open", "registration_closed"}
)
ORGANIZER_MANAGED_PARTICIPANT_STATUSES = frozenset(
    {"registration_open", "registration_closed"}
)
ORGANIZER_MODERATED_PARTICIPANT_STATUSES = frozenset(
    {"registration_open", "registration_closed", "in_progress"}
)
PARTICIPANT_RESTORATION_STATUSES = frozenset({"registration_open", "registration_closed"})
SOLO_TOURNAMENT_FORMAT = "solo"
# Ready Check admission proofs are bounded to this supported workflow window.
# The admission layer adds a small clock-skew allowance and the browser can
# refresh a multi-check stream proof before its bounded horizon expires.
READY_CHECK_MAX_DURATION_SECONDS = 24 * 60 * 60


class TournamentWorkflowError(ValueError):
    """Raised when a tournament or match workflow transition is invalid."""


@dataclass(frozen=True, slots=True)
class StrengthSeedTeam:
    team_id: str
    strength: float


def strength_seed_teams(
    rows: Sequence[Mapping[str, object] | StrengthSeedTeam],
) -> tuple[StrengthSeedTeam, ...]:
    teams = tuple(
        row
        if isinstance(row, StrengthSeedTeam)
        else StrengthSeedTeam(
            team_id=str(row.get("team_id") or "").strip(),
            strength=float(row.get("starter_strength") or 0.0),
        )
        for row in rows
    )
    if any(not team.team_id for team in teams):
        raise TournamentWorkflowError("Every locked team must have a stable team id.")
    if len({team.team_id for team in teams}) != len(teams):
        raise TournamentWorkflowError("Locked team ids must be unique.")
    return tuple(
        sorted(
            teams,
            key=lambda team: (
                -team.strength,
                _sortable_team_id(team.team_id),
            ),
        )
    )


def _sortable_team_id(team_id: str) -> tuple[int, int | str]:
    normalized = str(team_id).strip()
    if normalized.isdigit():
        return (0, int(normalized))
    return (1, normalized.casefold())


def bracket_seed_positions(team_count: int) -> tuple[int, ...]:
    if team_count < 2 or team_count & (team_count - 1):
        raise TournamentWorkflowError(
            "Bracket seeding requires a power-of-two team count of at least two."
        )
    positions = [1, 2]
    while len(positions) < team_count:
        mirror_seed = len(positions) * 2 + 1
        positions = [
            value
            for seed in positions
            for value in (seed, mirror_seed - seed)
        ]
    if team_count == 8:
        positions[4:] = positions[6:8] + positions[4:6]
    return tuple(positions)


def strength_seed_team_ids(
    rows: Sequence[Mapping[str, object] | StrengthSeedTeam],
) -> tuple[str, ...]:
    ordered = strength_seed_teams(rows)
    positions = bracket_seed_positions(len(ordered))
    return tuple(ordered[seed - 1].team_id for seed in positions)


def bracket_round_count(team_count: int) -> int:
    bracket_seed_positions(team_count)
    return team_count.bit_length() - 1


def match_title_for_round(
    *,
    round_number: int,
    total_rounds: int,
    sequence_number: int,
) -> str:
    if round_number == total_rounds:
        return "Grand Final"
    if round_number == total_rounds - 1:
        return f"Semifinal {sequence_number}"
    if round_number == total_rounds - 2:
        return f"Quarterfinal {sequence_number}"
    remaining_teams = 2 ** (total_rounds - round_number + 1)
    return f"Round of {remaining_teams} Match {sequence_number}"


def wins_required_for_format(match_format: str) -> int:
    wins_by_format = {"bo1": 1, "bo3": 2, "bo5": 3}
    try:
        return wins_by_format[match_format]
    except KeyError as exc:
        raise TournamentWorkflowError(f"Unknown match format: {match_format}.") from exc


def ensure_score_matches_format(
    *,
    match_format: str,
    home_score: int,
    away_score: int,
) -> None:
    required_wins = wins_required_for_format(match_format)
    high_score = max(home_score, away_score)
    low_score = min(home_score, away_score)
    if high_score != required_wins or low_score >= required_wins:
        raise TournamentWorkflowError(
            f"{match_format.upper()} result must end when the winner reaches {required_wins} win"
            f"{'s' if required_wins != 1 else ''}."
        )


def eliminated_team_id_for_single_elimination(
    *,
    home_team_id: str | None,
    away_team_id: str | None,
    winner_team_id: str | None,
) -> str:
    team_ids = tuple(
        team_id for team_id in (home_team_id, away_team_id) if team_id is not None
    )
    if len(team_ids) != 2 or len(set(team_ids)) != 2:
        raise TournamentWorkflowError(
            "A completed single-elimination match must contain two distinct teams."
        )
    if winner_team_id not in team_ids:
        raise TournamentWorkflowError(
            "A completed single-elimination match winner must be one of its teams."
        )
    return team_ids[1] if winner_team_id == team_ids[0] else team_ids[0]


def is_solo_tournament_format(format_slug: str | None) -> bool:
    return str(format_slug or "").strip() == SOLO_TOURNAMENT_FORMAT


def next_tournament_statuses(current_status: str) -> tuple[str, ...]:
    if current_status not in TOURNAMENT_STATUS_TRANSITIONS:
        raise TournamentWorkflowError(f"Unknown tournament status: {current_status}.")
    return TOURNAMENT_STATUS_TRANSITIONS[current_status]


def available_tournament_statuses(
    current_status: str,
    *,
    format_slug: str | None = None,
    has_locked_deadlock_roster: bool = False,
) -> tuple[str, ...]:
    allowed_statuses = list(next_tournament_statuses(current_status))
    if is_solo_tournament_format(format_slug) and current_status == "registration_closed":
        if has_locked_deadlock_roster:
            allowed_statuses = [status for status in allowed_statuses if status != "registration_open"]
        else:
            allowed_statuses = [status for status in allowed_statuses if status != "in_progress"]
    return tuple(allowed_statuses)


def transition_tournament_status(
    current_status: str,
    next_status: str,
    *,
    format_slug: str | None = None,
    has_locked_deadlock_roster: bool = False,
) -> str:
    allowed_statuses = available_tournament_statuses(
        current_status,
        format_slug=format_slug,
        has_locked_deadlock_roster=has_locked_deadlock_roster,
    )
    if next_status not in allowed_statuses:
        if (
            is_solo_tournament_format(format_slug)
            and current_status == "registration_closed"
            and next_status == "in_progress"
            and not has_locked_deadlock_roster
        ):
            raise TournamentWorkflowError(
                "Lock a Deadlock roster before moving the tournament in progress."
            )
        if (
            is_solo_tournament_format(format_slug)
            and current_status == "registration_closed"
            and next_status == "registration_open"
            and has_locked_deadlock_roster
        ):
            raise TournamentWorkflowError(
                "Registration cannot be reopened after a Deadlock roster is locked."
            )
        raise TournamentWorkflowError(
            f"Cannot move tournament from {current_status} to {next_status}."
        )
    return next_status


def can_self_join_tournament(current_status: str) -> bool:
    return current_status in SELF_SERVICE_JOINABLE_TOURNAMENT_STATUSES


def can_self_leave_tournament(current_status: str) -> bool:
    return current_status in SELF_SERVICE_LEAVEABLE_TOURNAMENT_STATUSES


def normalize_tournament_allowed_ranks(ranks: Sequence[str] | None) -> tuple[str, ...]:
    if not ranks:
        return ()
    normalized: list[str] = []
    for raw_rank in ranks:
        rank = str(raw_rank).strip()
        if rank not in RANKS:
            raise TournamentWorkflowError("Allowed ranks contain an unsupported rank.")
        if rank not in normalized:
            normalized.append(rank)
    return tuple(normalized)


def ensure_supported_tournament_format(format_slug: str) -> None:
    if not is_solo_tournament_format(format_slug):
        raise TournamentWorkflowError("Only solo tournaments are supported.")


def ensure_solo_entry(entry_type: str, team_name: str | None = None) -> None:
    if entry_type != "solo" or team_name:
        raise TournamentWorkflowError("Only solo registration is supported.")


def ensure_tournament_capacity_allows_join(
    *,
    max_participants: int | None,
    active_participant_count: int,
) -> None:
    if max_participants is not None and active_participant_count >= max_participants:
        raise TournamentWorkflowError("Tournament participant limit has been reached.")


def ensure_tournament_rank_allows_join(
    *,
    allowed_ranks: Sequence[str] | None,
    player_rank: str | None,
) -> None:
    normalized = normalize_tournament_allowed_ranks(allowed_ranks)
    if not normalized:
        return
    if player_rank is None:
        raise TournamentWorkflowError("A Deadlock profile is required before joining a rank-limited tournament.")
    if player_rank not in normalized:
        raise TournamentWorkflowError("Your Deadlock rank is outside this tournament's allowed rank range.")


def can_organizer_manage_participants(current_status: str) -> bool:
    return current_status in ORGANIZER_MANAGED_PARTICIPANT_STATUSES


def ensure_organizer_can_manage_participants(current_status: str) -> None:
    if not can_organizer_manage_participants(current_status):
        raise TournamentWorkflowError(
            "Participants can only be managed before the tournament starts."
        )


def ensure_deadlock_registration_changes_allowed(
    *,
    format_slug: str,
    has_locked_deadlock_roster: bool,
) -> None:
    if is_solo_tournament_format(format_slug) and has_locked_deadlock_roster:
        raise TournamentWorkflowError(
            "Tournament registrations can no longer be changed after a Deadlock roster is locked."
        )


def can_organizer_moderate_participants(current_status: str) -> bool:
    return current_status in ORGANIZER_MODERATED_PARTICIPANT_STATUSES


def ensure_organizer_can_moderate_participants(current_status: str) -> None:
    if not can_organizer_moderate_participants(current_status):
        raise TournamentWorkflowError(
            "Participant moderation is unavailable after the tournament ends."
        )


def ensure_participant_restoration_allowed(
    *,
    tournament_status: str,
    has_locked_deadlock_roster: bool,
) -> None:
    """Keep inactive-to-active restoration inside a rebuild-safe window.

    Exclusion has an explicit reconciliation path, but restoration would need
    to rebuild ready/captain eligibility and commitments from a canonical
    roster snapshot. Until that path exists, an organizer may only restore a
    retained participant before the Deadlock workflow starts and before a
    roster is locked.
    """

    if tournament_status not in PARTICIPANT_RESTORATION_STATUSES:
        raise TournamentWorkflowError(
            "Inactive participants can only be restored before the tournament starts."
        )
    if has_locked_deadlock_roster:
        raise TournamentWorkflowError(
            "Inactive participants cannot be restored after a Deadlock roster is locked."
        )


def ensure_match_team_ids_are_locked(
    *,
    home_team_id: str | None,
    away_team_id: str | None,
    locked_team_ids: set[str] | frozenset[str],
) -> tuple[str, str]:
    """Require manual bracket sides to use the locked roster identity."""

    normalized_home = str(home_team_id or "").strip()
    normalized_away = str(away_team_id or "").strip()
    if not normalized_home or not normalized_away:
        raise TournamentWorkflowError(
            "Manual matches must use canonical Team <id> labels from the locked roster."
        )
    if normalized_home == normalized_away:
        raise TournamentWorkflowError(
            "Manual matches must use two different locked teams."
        )
    unknown_team_ids = sorted(
        {normalized_home, normalized_away}.difference(
            {str(team_id).strip() for team_id in locked_team_ids}
        )
    )
    if unknown_team_ids:
        raise TournamentWorkflowError(
            "Manual matches may use only teams from the locked roster: "
            + ", ".join(unknown_team_ids)
            + "."
        )
    return normalized_home, normalized_away


def ensure_deadlock_roster_staging_allowed(
    *,
    format_slug: str,
    tournament_status: str,
    has_locked_deadlock_roster: bool,
    action_name: str,
) -> None:
    if not is_solo_tournament_format(format_slug):
        return
    if has_locked_deadlock_roster:
        raise TournamentWorkflowError(
            f"{action_name} is unavailable after a Deadlock roster is locked."
        )
    if tournament_status == "registration_closed":
        return
    if tournament_status == "registration_open":
        raise TournamentWorkflowError(
            f"{action_name} is available only after registration is closed."
        )
    raise TournamentWorkflowError(
        f"{action_name} is unavailable after the tournament has started."
    )


def ensure_deadlock_match_staging_allowed(
    *,
    format_slug: str,
    has_locked_deadlock_roster: bool,
) -> None:
    if is_solo_tournament_format(format_slug) and not has_locked_deadlock_roster:
        raise TournamentWorkflowError(
            "Lock a Deadlock roster before creating matches."
        )


@dataclass(frozen=True, slots=True)
class SeededOpeningRoundMatch:
    round_number: int
    sequence_number: int
    title: str | None
    home_label: str
    away_label: str


def build_seeded_opening_round_matches(team_labels: Sequence[str]) -> tuple[SeededOpeningRoundMatch, ...]:
    team_count = len(team_labels)
    if team_count < 2:
        raise TournamentWorkflowError(
            "At least two locked teams are required before seeding opening-round matches."
        )
    if team_count & (team_count - 1):
        raise TournamentWorkflowError(
            "Opening-round seeding currently requires a power-of-two locked roster size."
        )

    seed_order = bracket_seed_positions(team_count)
    ordered_labels = [str(team_labels[seed - 1]).strip() for seed in seed_order]
    match_count = team_count // 2
    return tuple(
        SeededOpeningRoundMatch(
            round_number=1,
            sequence_number=sequence_number,
            title=opening_round_match_title(match_count, sequence_number),
            home_label=ordered_labels[(sequence_number - 1) * 2],
            away_label=ordered_labels[(sequence_number - 1) * 2 + 1],
        )
        for sequence_number in range(1, match_count + 1)
    )


def opening_round_match_title(match_count: int, sequence_number: int) -> str:
    if match_count == 1:
        return "Grand Final"
    if match_count == 2:
        return f"Semifinal {sequence_number}"
    if match_count == 4:
        return f"Quarterfinal {sequence_number}"
    if match_count == 8:
        return f"Round of 16 Match {sequence_number}"
    return f"Round 1 Match {sequence_number}"


@dataclass(frozen=True, slots=True)
class ProgressedRoundMatch:
    round_number: int
    sequence_number: int
    title: str | None
    home_label: str
    away_label: str


@dataclass(frozen=True, slots=True)
class ExistingBracketMatchState:
    round_number: int
    status: str


def build_next_round_matches(
    source_round_number: int,
    winner_labels: Sequence[str],
) -> tuple[ProgressedRoundMatch, ...]:
    if source_round_number < 1:
        raise TournamentWorkflowError("Source round number must be positive.")

    winner_count = len(winner_labels)
    if winner_count < 2:
        raise TournamentWorkflowError(
            "At least two completed matches are required before generating the next round."
        )
    if winner_count % 2 != 0:
        raise TournamentWorkflowError(
            "Completed round winners cannot be paired evenly into the next round."
        )

    next_round_number = source_round_number + 1
    next_round_match_count = winner_count // 2
    progressed_matches: list[ProgressedRoundMatch] = []
    for index in range(next_round_match_count):
        sequence_number = index + 1
        home_label = str(winner_labels[index * 2]).strip()
        away_label = str(winner_labels[index * 2 + 1]).strip()
        if not home_label or not away_label:
            raise TournamentWorkflowError(
                "Completed round winners must have non-empty labels before generating the next round."
            )
        if home_label.casefold() == away_label.casefold():
            raise TournamentWorkflowError(
                "Next-round opponents must be distinct."
            )
        progressed_matches.append(
            ProgressedRoundMatch(
                round_number=next_round_number,
                sequence_number=sequence_number,
                title=opening_round_match_title(next_round_match_count, sequence_number),
                home_label=home_label,
                away_label=away_label,
            )
        )
    return tuple(progressed_matches)


def ensure_match_round_staging_allowed(
    requested_round_number: int,
    existing_matches: Sequence[ExistingBracketMatchState],
) -> None:
    if requested_round_number < 1:
        raise TournamentWorkflowError("Round number must be positive.")
    if not existing_matches:
        return

    latest_round_number = max(match.round_number for match in existing_matches)
    latest_round_matches = tuple(
        match
        for match in existing_matches
        if match.round_number == latest_round_number
    )

    if requested_round_number < latest_round_number:
        raise TournamentWorkflowError(
            f"Round {requested_round_number} can no longer be staged after round {latest_round_number} matches already exist."
        )
    if requested_round_number == latest_round_number:
        return
    if requested_round_number > latest_round_number + 1:
        raise TournamentWorkflowError(
            f"Create round {latest_round_number + 1} before staging round {requested_round_number}."
        )
    if any(match.status == "cancelled" for match in latest_round_matches):
        raise TournamentWorkflowError(
            f"Round {latest_round_number} has cancelled matches. Reset them to scheduled before staging round {requested_round_number}."
        )
    if any(match.status != "completed" for match in latest_round_matches):
        raise TournamentWorkflowError(
            f"Complete every match in round {latest_round_number} before staging round {requested_round_number}."
        )
    if len(latest_round_matches) == 1:
        raise TournamentWorkflowError(
            "The latest round already determines the tournament winner. No later round remains."
        )


def ensure_tournament_completion_has_final_result(
    existing_matches: Sequence[ExistingBracketMatchState],
) -> None:
    if not existing_matches:
        return

    latest_round_number = max(match.round_number for match in existing_matches)
    latest_round_matches = tuple(
        match
        for match in existing_matches
        if match.round_number == latest_round_number
    )
    if any(match.status == "cancelled" for match in latest_round_matches):
        raise TournamentWorkflowError(
            f"Round {latest_round_number} has cancelled matches. Reset them to scheduled before marking the tournament completed."
        )
    if len(latest_round_matches) == 1 and latest_round_matches[0].status == "completed":
        return

    raise TournamentWorkflowError(
        f"Finish the bracket with a single completed match in round {latest_round_number} before marking the tournament completed."
    )


def ensure_match_admin_actions_allowed(tournament_status: str) -> None:
    if tournament_status in {"completed", "cancelled"}:
        raise TournamentWorkflowError(
            "Match administration is unavailable after the tournament is completed or cancelled."
        )


def ensure_match_schedule_allowed(
    *,
    scheduled_at: datetime | None,
    now: datetime,
    source_scheduled_at: Sequence[datetime | None] = (),
    dependent_scheduled_at: Sequence[datetime | None] = (),
) -> None:
    if scheduled_at is None:
        return

    normalized_schedule = _normalize_timestamp(scheduled_at)
    if normalized_schedule <= _normalize_timestamp(now):
        raise TournamentWorkflowError("Match start time must be in the future.")

    source_times = [
        _normalize_timestamp(value)
        for value in source_scheduled_at
        if value is not None
    ]
    if source_times and normalized_schedule < max(source_times):
        raise TournamentWorkflowError(
            "A later-round match cannot start before its source matches."
        )

    dependent_times = [
        _normalize_timestamp(value)
        for value in dependent_scheduled_at
        if value is not None
    ]
    if dependent_times and normalized_schedule > min(dependent_times):
        raise TournamentWorkflowError(
            "A source match cannot start after a dependent match."
        )


def ensure_match_report_allowed(tournament_status: str) -> None:
    ensure_match_admin_actions_allowed(tournament_status)
    if tournament_status not in {"registration_closed", "in_progress"}:
        raise TournamentWorkflowError(
            "Match results can be reported after registration is closed and the bracket is ready."
        )


def available_match_statuses(
    current_status: str,
    *,
    tournament_status: str,
    current_round_number: int,
    latest_round_number: int,
) -> tuple[str, ...]:
    ensure_match_admin_actions_allowed(tournament_status)
    if current_status == "completed":
        if current_round_number == latest_round_number:
            return ("scheduled",)
        return ()
    return next_match_statuses(current_status)


def ensure_completed_match_reopen_allowed(
    *,
    current_round_number: int,
    latest_round_number: int,
) -> None:
    if current_round_number != latest_round_number:
        raise TournamentWorkflowError(
            f"Completed matches in round {current_round_number} cannot be reset while round {latest_round_number} matches already exist. Delete later-round matches first."
        )


def ensure_match_deletion_allowed(
    *,
    tournament_status: str,
    current_status: str,
    current_round_number: int,
    latest_round_number: int,
) -> None:
    ensure_match_admin_actions_allowed(tournament_status)
    if current_round_number != latest_round_number:
        raise TournamentWorkflowError(
            f"Only latest-round matches can be deleted for bracket recovery. Delete round {latest_round_number} matches before removing round {current_round_number}."
        )
    if current_status not in {"scheduled", "cancelled"}:
        raise TournamentWorkflowError(
            "Only scheduled or cancelled latest-round matches can be deleted for bracket recovery."
        )


def can_view_tournament_summary(
    *,
    tournament_visibility: str,
    has_authenticated_user: bool,
) -> bool:
    if tournament_visibility == "public":
        return True
    if tournament_visibility == "invite_only":
        return has_authenticated_user
    raise TournamentWorkflowError(f"Unknown tournament visibility: {tournament_visibility}.")


def can_view_tournament_workspace(
    *,
    tournament_visibility: str,
    is_participant: bool,
    is_organizer: bool,
    is_admin: bool,
) -> bool:
    if tournament_visibility == "public":
        return True
    if tournament_visibility == "invite_only":
        return is_participant or is_organizer or is_admin
    raise TournamentWorkflowError(f"Unknown tournament visibility: {tournament_visibility}.")


def next_participant_statuses(current_status: str) -> tuple[str, ...]:
    if current_status not in PARTICIPANT_STATUSES:
        raise TournamentWorkflowError(f"Unknown participant status: {current_status}.")
    return tuple(status for status in PARTICIPANT_STATUSES if status != current_status)


def transition_participant_status(current_status: str, next_status: str) -> str:
    if current_status not in PARTICIPANT_STATUSES:
        raise TournamentWorkflowError(f"Unknown participant status: {current_status}.")
    if next_status not in PARTICIPANT_STATUSES:
        raise TournamentWorkflowError(f"Unknown participant status: {next_status}.")
    return next_status


def next_match_statuses(current_status: str) -> tuple[str, ...]:
    if current_status not in MATCH_STATUS_TRANSITIONS:
        raise TournamentWorkflowError(f"Unknown match status: {current_status}.")
    return MATCH_STATUS_TRANSITIONS[current_status]


def transition_match_status(current_status: str, next_status: str) -> str:
    allowed_statuses = next_match_statuses(current_status)
    if next_status not in allowed_statuses:
        raise TournamentWorkflowError(f"Cannot move match from {current_status} to {next_status}.")
    return next_status


@dataclass(frozen=True, slots=True)
class MatchReportResult:
    status: str
    home_score: int
    away_score: int
    winner_side: str
    note: str | None


def resolve_match_report(
    *,
    current_status: str,
    home_score: int,
    away_score: int,
    note: str | None,
    match_format: str | None = None,
) -> MatchReportResult:
    if current_status not in {"scheduled", "live"}:
        raise TournamentWorkflowError(
            f"Cannot report a result while the match is {current_status}."
        )
    if home_score == away_score:
        raise TournamentWorkflowError("Draws are not supported for bracket-style match reports.")
    if match_format is not None:
        ensure_score_matches_format(
            match_format=match_format,
            home_score=home_score,
            away_score=away_score,
        )

    return MatchReportResult(
        status="completed",
        home_score=home_score,
        away_score=away_score,
        winner_side="home" if home_score > away_score else "away",
        note=note,
    )


def remaining_invite_uses(max_uses: int, use_count: int) -> int:
    return max(max_uses - use_count, 0)


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def invite_is_active(
    *,
    max_uses: int,
    use_count: int,
    revoked_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    if revoked_at is not None:
        return False
    if remaining_invite_uses(max_uses, use_count) <= 0:
        return False
    if expires_at is not None and _normalize_timestamp(expires_at) <= _normalize_timestamp(now):
        return False
    return True


def ensure_invite_claimable(
    *,
    tournament_visibility: str,
    tournament_status: str,
    max_uses: int,
    use_count: int,
    revoked_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> None:
    if tournament_visibility != "invite_only":
        raise TournamentWorkflowError(
            "Invite claims are available only for invite-only tournaments."
        )
    if not can_self_join_tournament(tournament_status):
        raise TournamentWorkflowError("Tournament registration is not open right now.")
    if revoked_at is not None:
        raise TournamentWorkflowError("This invite is no longer active.")
    if remaining_invite_uses(max_uses, use_count) <= 0:
        raise TournamentWorkflowError("This invite has already been fully claimed.")
    if expires_at is not None and _normalize_timestamp(expires_at) <= _normalize_timestamp(now):
        raise TournamentWorkflowError("This invite has expired.")
