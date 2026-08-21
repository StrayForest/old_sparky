from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from python_packages.platform_domain.deadlock.captain_selection import (
    assign_captain_team_numbers,
    select_captain_offer_candidates,
)
from python_packages.platform_domain.deadlock.dream_slots import normalize_slot_payload
from python_packages.platform_domain.deadlock.strength import calculate_player_strength


def captain_priority_bucket(rank: str | None, captain_priority: str | None) -> int:
    normalized_priority = str(captain_priority or "neutral")
    if normalized_priority == "yes":
        return 0
    if normalized_priority == "neutral":
        return 1
    if normalized_priority == "no":
        return 2
    return 3


@dataclass(frozen=True, slots=True)
class DeadlockCaptainPreviewCandidate:
    user_id: str
    display_name: str
    rank: str
    subrank: int
    playtime: str
    captain_priority: str | None
    captain_priority_bucket: int
    strength: float
    projected_team_id: str | None


def build_captain_team_dream_slot_rows(
    captain_rows: Sequence[Mapping[str, Any]],
    slot_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    team_ids_by_user_id = {
        str(row["user_id"]): str(row["team_id"])
        for row in captain_rows
        if row.get("user_id") is not None and row.get("team_id") is not None
    }
    prepared_rows: list[dict[str, Any]] = []
    for row in slot_rows:
        user_id = str(row["user_id"])
        team_id = team_ids_by_user_id.get(user_id)
        if team_id is None:
            continue
        slot_number = int(row.get("slot_number") or 1)
        normalized = normalize_slot_payload(
            {
                "team_id": team_id,
                "slot_number": slot_number,
                "allowed_roles": row.get("allowed_roles") or [],
                "desired_heroes": row.get("desired_heroes") or [],
            },
            default_slot_number=slot_number,
        )
        prepared_rows.append(normalized)
    return tuple(
        sorted(
            prepared_rows,
            key=lambda row: (str(row.get("team_id") or ""), int(row.get("slot_number") or 0)),
        )
    )


def build_captain_preview(
    rows: Sequence[Mapping[str, Any]],
    teams_count: int,
    *,
    rank_power: Mapping[str, int] | None = None,
) -> tuple[DeadlockCaptainPreviewCandidate, ...]:
    if teams_count < 1:
        return ()

    prepared_rows: list[dict[str, Any]] = []
    for row in rows:
        rank = str(row["rank"])
        subrank = int(row["subrank"])
        playtime = str(row["playtime"])
        strength = (
            float(row["strength"])
            if row.get("strength") is not None
            else calculate_player_strength(rank, subrank, playtime, rank_power=rank_power)
        )
        prepared_rows.append(
            {
                "user_id": str(row["user_id"]),
                "display_name": str(row["display_name"]),
                "rank": rank,
                "subrank": subrank,
                "playtime": playtime,
                "captain_priority": row.get("captain_priority"),
                "captain_priority_bucket": row.get("captain_priority_bucket")
                if row.get("captain_priority_bucket") is not None
                else captain_priority_bucket(rank, row.get("captain_priority")),
                "strength": strength,
            }
        )

    selected = select_captain_offer_candidates(prepared_rows, teams_count)
    assignments = assign_captain_team_numbers(selected)
    team_ids_by_user_id = {assignment.user_id: assignment.team_id for assignment in assignments}

    return tuple(
        DeadlockCaptainPreviewCandidate(
            user_id=str(candidate.user_id),
            display_name=str(next(row["display_name"] for row in prepared_rows if row["user_id"] == str(candidate.user_id))),
            rank=str(candidate.rank),
            subrank=int(candidate.subrank),
            playtime=str(candidate.playtime),
            captain_priority=next(
                (row.get("captain_priority") for row in prepared_rows if row["user_id"] == str(candidate.user_id)),
                None,
            ),
            captain_priority_bucket=int(candidate.captain_priority_bucket),
            strength=float(candidate.strength),
            projected_team_id=team_ids_by_user_id.get(candidate.user_id),
        )
        for candidate in selected
    )
