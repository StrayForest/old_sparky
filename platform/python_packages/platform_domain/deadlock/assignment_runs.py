from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class AutoAssignmentRunWorkflowError(ValueError):
    """Raised when an auto-assignment run receives an invalid status transition."""


AUTO_ASSIGNMENT_RUN_STATUSES = (
    "generated",
    "published",
    "superseded",
    "locked",
)


@dataclass(frozen=True, slots=True)
class AutoAssignmentRunFreshness:
    is_stale: bool
    stale_reasons: tuple[str, ...]


def _normalize_player_input_row(
    row: Mapping[str, Any],
    *,
    include_team_id: bool,
) -> dict[str, Any]:
    normalized = {
        "user_id": str(row["user_id"]),
        "rank": str(row["rank"]),
        "subrank": int(row["subrank"]),
        "playtime": str(row["playtime"]),
        "pool": sorted({str(item) for item in list(row.get("pool") or [])}),
        "roles": sorted({str(item) for item in list(row.get("roles") or [])}),
    }
    if include_team_id:
        normalized["team_id"] = str(row["team_id"])
        normalized["team_name"] = str(row.get("team_name") or "")
    return normalized


def _normalize_dream_slot_input_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "team_id": str(row["team_id"]),
        "slot_number": int(row["slot_number"]),
        "allowed_roles": sorted({str(item) for item in list(row.get("allowed_roles") or [])}),
        "desired_heroes": sorted({str(item) for item in list(row.get("desired_heroes") or [])}),
    }


def build_auto_assignment_input_fingerprint(
    captain_rows: Sequence[Mapping[str, Any]],
    ready_player_rows: Sequence[Mapping[str, Any]],
    dream_slot_rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        "captains": sorted(
            [_normalize_player_input_row(row, include_team_id=True) for row in captain_rows],
            key=lambda row: (str(row["team_id"]), str(row["user_id"])),
        ),
        "ready_players": sorted(
            [_normalize_player_input_row(row, include_team_id=False) for row in ready_player_rows],
            key=lambda row: str(row["user_id"]),
        ),
        "dream_slots": sorted(
            [_normalize_dream_slot_input_row(row) for row in dream_slot_rows],
            key=lambda row: (str(row["team_id"]), int(row["slot_number"])),
        ),
    }


def evaluate_auto_assignment_run_freshness(
    *,
    run_source_captain_round_id: int,
    current_source_captain_round_id: int | None,
    run_source_ready_round_id: int,
    current_source_ready_round_id: int | None,
    stored_input_fingerprint: Mapping[str, Any] | None,
    current_input_fingerprint: Mapping[str, Any] | None,
) -> AutoAssignmentRunFreshness:
    stale_reasons: list[str] = []

    if current_source_captain_round_id is None:
        stale_reasons.append("captain_round_missing")
    elif int(run_source_captain_round_id) != int(current_source_captain_round_id):
        stale_reasons.append("captain_round_changed")

    if current_source_ready_round_id is None:
        stale_reasons.append("ready_round_missing")
    elif int(run_source_ready_round_id) != int(current_source_ready_round_id):
        stale_reasons.append("ready_round_changed")

    if stored_input_fingerprint is not None and current_input_fingerprint is not None:
        if list(stored_input_fingerprint.get("captains") or []) != list(current_input_fingerprint.get("captains") or []):
            stale_reasons.append("captains_changed")
        if list(stored_input_fingerprint.get("ready_players") or []) != list(
            current_input_fingerprint.get("ready_players") or []
        ):
            stale_reasons.append("ready_players_changed")
        if list(stored_input_fingerprint.get("dream_slots") or []) != list(
            current_input_fingerprint.get("dream_slots") or []
        ):
            stale_reasons.append("dream_slots_changed")

    return AutoAssignmentRunFreshness(
        is_stale=bool(stale_reasons),
        stale_reasons=tuple(stale_reasons),
    )


def next_auto_assignment_run_statuses(current_status: str) -> tuple[str, ...]:
    status = str(current_status)
    if status == "generated":
        return ("published",)
    if status == "published":
        return ("superseded", "locked")
    if status == "superseded":
        return ("published",)
    if status == "locked":
        return ()
    raise AutoAssignmentRunWorkflowError("Unknown auto-assignment run status.")


def transition_auto_assignment_run_status(current_status: str, next_status: str) -> str:
    normalized_current = str(current_status)
    normalized_next = str(next_status)
    allowed_statuses = next_auto_assignment_run_statuses(normalized_current)
    if normalized_next not in allowed_statuses:
        if normalized_current == normalized_next and normalized_current in AUTO_ASSIGNMENT_RUN_STATUSES:
            return normalized_current
        raise AutoAssignmentRunWorkflowError("Invalid auto-assignment run status transition.")
    return normalized_next
