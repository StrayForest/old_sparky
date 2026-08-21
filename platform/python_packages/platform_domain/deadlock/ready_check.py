from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

READY_CHECK_CHOICES = ("yes", "no")
READY_CHECK_CLOSED_STATUSES = {"closed", "stopped"}
READY_CHECK_VOTE_STATUSES = {
    "updated",
    "unchanged",
    "closed",
    "round_mismatch",
    "not_eligible",
}
READY_CHECK_START_STATUSES = {"created", "already_active", "empty"}


class ReadyCheckError(ValueError):
    """Raised when the shared ready-check state receives invalid data."""


@dataclass(frozen=True, slots=True)
class ReadyCheckVote:
    user_id: str
    choice: str

    def __post_init__(self) -> None:
        if self.choice not in READY_CHECK_CHOICES:
            raise ReadyCheckError("Unknown ready-check choice.")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any] | "ReadyCheckVote") -> "ReadyCheckVote":
        if isinstance(row, ReadyCheckVote):
            return row
        return cls(user_id=str(row["user_id"]), choice=str(row["choice"]))


@dataclass(frozen=True, slots=True)
class ReadyCheckStartDecision:
    status: str
    user_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.status not in READY_CHECK_START_STATUSES:
            raise ReadyCheckError("Unknown ready-check start status.")

    @property
    def should_create_round(self) -> bool:
        return self.status == "created"


@dataclass(frozen=True, slots=True)
class ReadyCheckVoteDecision:
    status: str
    choice: str | None
    ready_count: int
    declined_count: int

    def __post_init__(self) -> None:
        if self.status not in READY_CHECK_VOTE_STATUSES:
            raise ReadyCheckError("Unknown ready-check vote status.")


def _unique_user_ids(user_ids: Iterable[str | int]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for user_id in user_ids:
        normalized = str(user_id)
        if normalized in seen:
            continue
        ordered.append(normalized)
        seen.add(normalized)
    return tuple(ordered)


def prepare_ready_check_start(
    user_ids: Iterable[str | int],
    *,
    has_active_round: bool,
) -> ReadyCheckStartDecision:
    normalized_user_ids = _unique_user_ids(user_ids)
    if has_active_round:
        return ReadyCheckStartDecision(status="already_active", user_ids=normalized_user_ids)
    if not normalized_user_ids:
        return ReadyCheckStartDecision(status="empty", user_ids=normalized_user_ids)
    return ReadyCheckStartDecision(status="created", user_ids=normalized_user_ids)


@dataclass(frozen=True, slots=True)
class ReadyCheckRoundState:
    round_id: int | None
    status: str = "active"
    eligible_user_ids: tuple[str, ...] = ()
    votes: tuple[ReadyCheckVote, ...] = ()

    def __post_init__(self) -> None:
        if self.status != "active" and self.status not in READY_CHECK_CLOSED_STATUSES:
            raise ReadyCheckError("Unknown ready-check round status.")

    @classmethod
    def active(
        cls,
        round_id: int,
        *,
        eligible_user_ids: Iterable[str | int] = (),
        votes: Iterable[ReadyCheckVote | Mapping[str, Any]] = (),
    ) -> "ReadyCheckRoundState":
        return cls(
            round_id=int(round_id),
            status="active",
            eligible_user_ids=_unique_user_ids(eligible_user_ids),
            votes=tuple(ReadyCheckVote.from_mapping(vote) for vote in votes),
        )

    @property
    def ready_user_ids(self) -> tuple[str, ...]:
        return tuple(vote.user_id for vote in self.votes if vote.choice == "yes")

    @property
    def declined_user_ids(self) -> tuple[str, ...]:
        return tuple(vote.user_id for vote in self.votes if vote.choice == "no")

    def count_votes(self, choice: str) -> int:
        if choice not in READY_CHECK_CHOICES:
            raise ReadyCheckError("Unknown ready-check choice.")
        return sum(1 for vote in self.votes if vote.choice == choice)

    def close(self, status: str = "closed") -> "ReadyCheckRoundState":
        if status not in READY_CHECK_CLOSED_STATUSES:
            raise ReadyCheckError("Unknown ready-check close status.")
        return replace(self, status=status)

    def exclude_user(self, user_id: str | int) -> "ReadyCheckRoundState":
        normalized_user_id = str(user_id)
        next_eligible_user_ids = tuple(
            eligible_user_id
            for eligible_user_id in self.eligible_user_ids
            if eligible_user_id != normalized_user_id
        )
        next_votes = tuple(
            vote
            for vote in self.votes
            if vote.user_id != normalized_user_id
        )
        if next_eligible_user_ids == self.eligible_user_ids and next_votes == self.votes:
            return self

        next_state = replace(
            self,
            eligible_user_ids=next_eligible_user_ids,
            votes=next_votes,
        )
        if next_state.status == "active" and not next_state.eligible_user_ids:
            return next_state.close(status="stopped")
        return next_state

    def record_vote(
        self,
        user_id: str | int,
        choice: str,
        *,
        round_id: int | None = None,
    ) -> tuple["ReadyCheckRoundState", ReadyCheckVoteDecision]:
        if choice not in READY_CHECK_CHOICES:
            raise ReadyCheckError("Unknown ready-check choice.")

        if self.status != "active" or self.round_id is None:
            return self, ReadyCheckVoteDecision(
                status="closed",
                choice=choice,
                ready_count=self.count_votes("yes"),
                declined_count=self.count_votes("no"),
            )

        if round_id is not None and int(round_id) != int(self.round_id):
            return self, ReadyCheckVoteDecision(
                status="round_mismatch",
                choice=choice,
                ready_count=self.count_votes("yes"),
                declined_count=self.count_votes("no"),
            )

        normalized_user_id = str(user_id)
        if self.eligible_user_ids and normalized_user_id not in self.eligible_user_ids:
            return self, ReadyCheckVoteDecision(
                status="not_eligible",
                choice=choice,
                ready_count=self.count_votes("yes"),
                declined_count=self.count_votes("no"),
            )

        updated_votes = list(self.votes)
        for index, vote in enumerate(updated_votes):
            if vote.user_id != normalized_user_id:
                continue
            if vote.choice == choice:
                return self, ReadyCheckVoteDecision(
                    status="unchanged",
                    choice=choice,
                    ready_count=self.count_votes("yes"),
                    declined_count=self.count_votes("no"),
                )
            updated_votes[index] = ReadyCheckVote(user_id=normalized_user_id, choice=choice)
            next_state = replace(self, votes=tuple(updated_votes))
            return next_state, ReadyCheckVoteDecision(
                status="updated",
                choice=choice,
                ready_count=next_state.count_votes("yes"),
                declined_count=next_state.count_votes("no"),
            )

        updated_votes.append(ReadyCheckVote(user_id=normalized_user_id, choice=choice))
        next_state = replace(self, votes=tuple(updated_votes))
        return next_state, ReadyCheckVoteDecision(
            status="updated",
            choice=choice,
            ready_count=next_state.count_votes("yes"),
            declined_count=next_state.count_votes("no"),
        )
