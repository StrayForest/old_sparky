from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from python_packages.platform_domain.deadlock.strength import calculate_player_strength


@dataclass(frozen=True, slots=True)
class CaptainCandidate:
    user_id: str
    rank: str | None
    subrank: int
    playtime: str | None
    captain_priority_bucket: int = 0
    username: str | None = None
    strength: float = 0.0

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any] | "CaptainCandidate") -> "CaptainCandidate":
        if isinstance(row, CaptainCandidate):
            return row

        rank = row.get("rank")
        subrank = int((row.get("subrank") or 0))
        playtime = row.get("playtime")
        strength = 0.0
        if row.get("strength") is not None:
            strength = float(row["strength"])
        elif rank is not None:
            strength = calculate_player_strength(str(rank), subrank, playtime)
        return cls(
            user_id=str(row["user_id"]),
            rank=str(rank) if rank is not None else None,
            subrank=subrank,
            playtime=str(playtime) if playtime is not None else None,
            captain_priority_bucket=int(row.get("captain_priority_bucket") or 0),
            username=str(row["username"]) if row.get("username") is not None else None,
            strength=strength,
        )


@dataclass(frozen=True, slots=True)
class CaptainAssignment:
    user_id: str
    team_id: str


def sort_captain_candidates(
    rows: Sequence[Mapping[str, Any] | CaptainCandidate],
) -> list[CaptainCandidate]:
    candidates = [CaptainCandidate.from_mapping(row) for row in rows]
    return sorted(
        candidates,
        key=lambda row: (
            int(row.captain_priority_bucket),
            -float(row.strength),
            str(row.user_id),
        ),
    )


def sort_accepted_captains(
    rows: Sequence[Mapping[str, Any] | CaptainCandidate],
) -> list[CaptainCandidate]:
    candidates = [CaptainCandidate.from_mapping(row) for row in rows]
    return sorted(
        candidates,
        key=lambda row: (-float(row.strength), str(row.user_id)),
    )


def select_captain_offer_candidates(
    rows: Sequence[Mapping[str, Any] | CaptainCandidate],
    slots_to_fill: int,
    *,
    exclude_user_ids: Iterable[int] = (),
) -> tuple[CaptainCandidate, ...]:
    if slots_to_fill <= 0:
        return ()

    excluded = {str(user_id) for user_id in exclude_user_ids}
    selected = [
        candidate
        for candidate in sort_captain_candidates(rows)
        if candidate.user_id not in excluded
    ]
    return tuple(selected[:slots_to_fill])


def assign_captain_team_numbers(
    rows: Sequence[Mapping[str, Any] | CaptainCandidate],
) -> tuple[CaptainAssignment, ...]:
    return tuple(
        CaptainAssignment(user_id=candidate.user_id, team_id=str(index))
        for index, candidate in enumerate(sort_accepted_captains(rows), start=1)
    )
