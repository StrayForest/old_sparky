from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from python_packages.platform_domain.deadlock.captain_selection import (
    assign_captain_team_numbers,
    sort_captain_candidates,
)

CAPTAIN_ENTRY_STATES = (
    "queued",
    "offered",
    "accepted",
    "declined",
    "cancelled",
    "assigned",
)
CAPTAIN_ROUND_RESPONSE_CHOICES = ("accept", "decline")
TEAM_COUNT_CHOICES = (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192)
DEFAULT_TEAM_COUNT_LIMIT = 128
DEADLOCK_PLAYERS_PER_TEAM = 7


class CaptainRoundError(ValueError):
    """Raised when the shared captain-round state receives invalid data."""


@dataclass(frozen=True, slots=True)
class CaptainRoundEntryState:
    user_id: str
    offer_order: int
    state: str = "queued"
    assigned_team_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in CAPTAIN_ENTRY_STATES:
            raise CaptainRoundError("Unknown captain-round entry state.")
        if self.offer_order < 1:
            raise CaptainRoundError("Captain-round offer order must be positive.")


@dataclass(frozen=True, slots=True)
class CaptainOfferResponseDecision:
    status: str
    user_id: str
    newly_offered_user_ids: tuple[str, ...] = ()
    cancelled_user_ids: tuple[str, ...] = ()
    accepted_count: int = 0
    offered_count: int = 0


@dataclass(frozen=True, slots=True)
class CaptainRoundState:
    round_id: int
    teams_count: int
    status: str
    entries: tuple[CaptainRoundEntryState, ...] = ()

    def __post_init__(self) -> None:
        if self.teams_count < 1:
            raise CaptainRoundError("Captain-round teams count must be positive.")
        if self.status not in ("active", "closed", "finalized"):
            raise CaptainRoundError("Unknown captain-round status.")

    @classmethod
    def active(
        cls,
        *,
        round_id: int,
        teams_count: int,
        entries: Sequence[Mapping[str, Any] | CaptainRoundEntryState] = (),
    ) -> "CaptainRoundState":
        return cls.from_entries(round_id=round_id, teams_count=teams_count, status="active", entries=entries)

    @classmethod
    def from_entries(
        cls,
        *,
        round_id: int,
        teams_count: int,
        status: str,
        entries: Sequence[Mapping[str, Any] | CaptainRoundEntryState] = (),
    ) -> "CaptainRoundState":
        prepared_entries = tuple(
            entry
            if isinstance(entry, CaptainRoundEntryState)
            else CaptainRoundEntryState(
                user_id=str(entry["user_id"]),
                offer_order=int(entry["offer_order"]),
                state=str(entry.get("state") or "queued"),
                assigned_team_id=(
                    str(entry["assigned_team_id"])
                    if entry.get("assigned_team_id") is not None
                    else None
                ),
            )
            for entry in entries
        )
        return cls(
            round_id=int(round_id),
            teams_count=int(teams_count),
            status=str(status),
            entries=tuple(sorted(prepared_entries, key=lambda item: (item.offer_order, item.user_id))),
        )

    @property
    def accepted_count(self) -> int:
        return sum(1 for entry in self.entries if entry.state in {"accepted", "assigned"})

    @property
    def offered_count(self) -> int:
        return sum(1 for entry in self.entries if entry.state == "offered")

    @property
    def declined_count(self) -> int:
        return sum(1 for entry in self.entries if entry.state == "declined")

    @property
    def assigned_count(self) -> int:
        return sum(1 for entry in self.entries if entry.state == "assigned")

    @property
    def queued_count(self) -> int:
        return sum(1 for entry in self.entries if entry.state == "queued")

    @property
    def candidate_count(self) -> int:
        return len(self.entries)

    @property
    def can_finalize(self) -> bool:
        return self.status == "active" and self.accepted_count == self.teams_count

    def respond(
        self,
        user_id: str | int,
        response: str,
    ) -> tuple["CaptainRoundState", CaptainOfferResponseDecision]:
        normalized_user_id = str(user_id)
        if self.status != "active":
            return self, CaptainOfferResponseDecision(
                status="closed",
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )

        normalized_response = str(response)
        if normalized_response not in CAPTAIN_ROUND_RESPONSE_CHOICES:
            raise CaptainRoundError("Unknown captain-round response choice.")

        current_index = next(
            (index for index, entry in enumerate(self.entries) if entry.user_id == normalized_user_id),
            None,
        )
        if current_index is None:
            return self, CaptainOfferResponseDecision(
                status="missing",
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )

        current_entry = self.entries[current_index]
        if current_entry.state == "accepted":
            return self, CaptainOfferResponseDecision(
                status="accepted",
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )
        if current_entry.state == "declined":
            return self, CaptainOfferResponseDecision(
                status="declined",
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )
        if current_entry.state != "offered":
            return self, CaptainOfferResponseDecision(
                status=current_entry.state,
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )
        if normalized_response == "accept" and self.accepted_count >= self.teams_count:
            return self, CaptainOfferResponseDecision(
                status="filled",
                user_id=normalized_user_id,
                accepted_count=self.accepted_count,
                offered_count=self.offered_count,
            )

        next_entries = list(self.entries)
        next_entries[current_index] = replace(
            current_entry,
            state="accepted" if normalized_response == "accept" else "declined",
        )
        next_state = replace(self, entries=tuple(next_entries)).reconcile()

        previous_by_user_id = {entry.user_id: entry.state for entry in self.entries}
        newly_offered_user_ids = tuple(
            entry.user_id
            for entry in next_state.entries
            if previous_by_user_id.get(entry.user_id) != "offered" and entry.state == "offered"
        )
        cancelled_user_ids = tuple(
            entry.user_id
            for entry in next_state.entries
            if previous_by_user_id.get(entry.user_id) != "cancelled" and entry.state == "cancelled"
        )

        return next_state, CaptainOfferResponseDecision(
            status="updated",
            user_id=normalized_user_id,
            newly_offered_user_ids=newly_offered_user_ids,
            cancelled_user_ids=cancelled_user_ids,
            accepted_count=next_state.accepted_count,
            offered_count=next_state.offered_count,
        )

    def reconcile(self) -> "CaptainRoundState":
        if self.status != "active":
            return self

        next_entries = list(self.entries)
        accepted_count = sum(1 for entry in next_entries if entry.state in {"accepted", "assigned"})
        if accepted_count >= self.teams_count:
            next_entries = [
                replace(entry, state="cancelled")
                if entry.state in {"queued", "offered"}
                else entry
                for entry in next_entries
            ]
            return replace(
                self,
                entries=tuple(sorted(next_entries, key=lambda item: (item.offer_order, item.user_id))),
            )

        offered_count = sum(1 for entry in next_entries if entry.state == "offered")
        open_slots = max(self.teams_count - accepted_count - offered_count, 0)
        if open_slots > 0:
            queued_indexes = [
                index
                for index, entry in enumerate(next_entries)
                if entry.state == "queued"
            ]
            for index in queued_indexes[:open_slots]:
                next_entries[index] = replace(next_entries[index], state="offered")

        return replace(
            self,
            entries=tuple(sorted(next_entries, key=lambda item: (item.offer_order, item.user_id))),
        )

    def close(self) -> "CaptainRoundState":
        if self.status != "active":
            return self
        return replace(
            self,
            status="closed",
            entries=tuple(
                replace(entry, state="cancelled")
                if entry.state in {"queued", "offered"}
                else entry
                for entry in self.entries
            ),
        )

    def exclude_user(self, user_id: str | int) -> "CaptainRoundState":
        normalized_user_id = str(user_id)
        next_entries = tuple(
            entry
            for entry in self.entries
            if entry.user_id != normalized_user_id
        )
        if len(next_entries) == len(self.entries):
            return self

        next_state = replace(
            self,
            entries=tuple(sorted(next_entries, key=lambda item: (item.offer_order, item.user_id))),
        )
        if next_state.status != "active":
            return next_state

        next_state = next_state.reconcile()
        if next_state.candidate_count < next_state.teams_count:
            return next_state.close()
        return next_state


def prepare_captain_round_entries(
    rows: Sequence[Mapping[str, Any]],
    teams_count: int,
    *,
    auto_assign: bool = False,
) -> tuple[CaptainRoundEntryState, ...]:
    if teams_count < 1:
        raise CaptainRoundError("Captain-round teams count must be positive.")

    ordered_candidates = sort_captain_candidates(rows)
    assignments_by_user_id = {
        assignment.user_id: assignment.team_id
        for assignment in assign_captain_team_numbers(ordered_candidates[:teams_count])
    }
    return tuple(
        CaptainRoundEntryState(
            user_id=candidate.user_id,
            offer_order=index,
            state="assigned" if auto_assign and index <= teams_count else "offered" if index <= teams_count else "queued",
            assigned_team_id=assignments_by_user_id.get(candidate.user_id) if auto_assign else None,
        )
        for index, candidate in enumerate(ordered_candidates, start=1)
    )


def normalize_requested_teams_count(teams_count: int | None) -> int | None:
    if teams_count is None:
        return None
    normalized = int(teams_count)
    if normalized < TEAM_COUNT_CHOICES[0] or normalized > TEAM_COUNT_CHOICES[-1]:
        raise CaptainRoundError("Teams count must be between 2 and 8192.")
    return next(count for count in TEAM_COUNT_CHOICES if count >= normalized)


def resolve_effective_teams_count(
    *,
    requested_teams_count: int | None,
    ready_player_count: int,
) -> int:
    max_supported = int(ready_player_count) // DEADLOCK_PLAYERS_PER_TEAM
    effective_choices = [count for count in TEAM_COUNT_CHOICES if count <= max_supported]
    if not effective_choices:
        raise CaptainRoundError("At least 14 ready players with Deadlock profiles are required to form 2 teams.")
    capped_requested = normalize_requested_teams_count(requested_teams_count) or DEFAULT_TEAM_COUNT_LIMIT
    return max(count for count in effective_choices if count <= capped_requested)
