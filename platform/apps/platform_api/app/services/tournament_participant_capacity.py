from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.tournaments import TournamentWorkflowError
from python_packages.platform_infra.models import (
    Tournament,
    TournamentParticipant,
    TournamentParticipantSlot,
    new_uuid,
)


# Keep a small durable free-slot inventory for ordinary tournaments. Larger
# advertised capacities use sparse slot rows allocated on demand, so creating
# a tournament with the API's upper-bound capacity never expands into millions
# of database rows.
PARTICIPANT_SLOT_MATERIALIZATION_LIMIT = 1024


async def has_free_participant_slot(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> bool:
    free_slot = await db_session.scalar(
        select(TournamentParticipantSlot.id)
        .where(
            TournamentParticipantSlot.tournament_id == tournament_id,
            TournamentParticipantSlot.participant_id.is_(None),
        )
        .limit(1)
    )
    if free_slot is not None:
        return True

    capacity = await db_session.scalar(
        select(Tournament.max_participants).where(Tournament.id == tournament_id)
    )
    if capacity is None:
        return True
    if capacity <= PARTICIPANT_SLOT_MATERIALIZATION_LIMIT:
        return False
    active_count = await db_session.scalar(
        select(func.count(TournamentParticipant.id)).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
        )
    )
    return int(active_count or 0) < capacity


async def _claim_sparse_participant_slot(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    max_participants: int,
    participant_id: str,
    claimed_at: datetime | None,
) -> TournamentParticipantSlot:
    """Allocate a slot above the bounded inventory without a tournament lock."""

    for _ in range(32):
        next_slot = await db_session.scalar(
            select(func.coalesce(func.max(TournamentParticipantSlot.slot_number), 0) + 1)
            .where(TournamentParticipantSlot.tournament_id == tournament_id)
        )
        if next_slot is None or int(next_slot) > max_participants:
            raise TournamentWorkflowError("Tournament participant limit has been reached.")
        slot_id = new_uuid()
        inserted_id = await db_session.scalar(
            postgresql.insert(TournamentParticipantSlot)
            .values(
                id=slot_id,
                tournament_id=tournament_id,
                slot_number=int(next_slot),
                participant_id=participant_id,
                claimed_at=claimed_at,
            )
            .on_conflict_do_nothing()
            .returning(TournamentParticipantSlot.id)
        )
        if inserted_id is not None:
            slot = await db_session.scalar(
                select(TournamentParticipantSlot).where(
                    TournamentParticipantSlot.id == inserted_id
                )
            )
            if slot is not None:
                return slot
    raise TournamentWorkflowError("Tournament participant capacity is contended; retry.")


async def claim_participant_slot(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    max_participants: int | None,
    participant_id: str | None = None,
    claimed_at: datetime | None = None,
) -> TournamentParticipantSlot | None:
    """Claim one capacity slot in the caller's transaction.

    `SKIP LOCKED` is deliberately scoped to this queue-like slot table. It is
    never used to read or lock the authoritative tournament row.
    """

    if max_participants is None:
        return None
    slot = await db_session.scalar(
        select(TournamentParticipantSlot)
        .where(
            TournamentParticipantSlot.tournament_id == tournament_id,
            TournamentParticipantSlot.participant_id.is_(None),
        )
        .order_by(TournamentParticipantSlot.slot_number.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if slot is None:
        if (
            participant_id is None
            or max_participants <= PARTICIPANT_SLOT_MATERIALIZATION_LIMIT
        ):
            raise TournamentWorkflowError("Tournament participant limit has been reached.")
        return await _claim_sparse_participant_slot(
            db_session,
            tournament_id=tournament_id,
            max_participants=max_participants,
            participant_id=participant_id,
            claimed_at=claimed_at,
        )
    slot.participant_id = participant_id
    slot.claimed_at = claimed_at
    slot.updated_at = claimed_at or datetime.now(UTC)
    return slot


async def release_participant_slot(
    db_session: AsyncSession,
    *,
    participant_id: str,
) -> None:
    slot = await db_session.scalar(
        select(TournamentParticipantSlot)
        .where(TournamentParticipantSlot.participant_id == participant_id)
        .with_for_update()
    )
    if slot is not None:
        slot.participant_id = None
        slot.claimed_at = None
        slot.updated_at = datetime.now(UTC)


async def claim_slot_for_existing_participant(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    participant: TournamentParticipant,
    max_participants: int | None,
    claimed_at: datetime | None = None,
) -> TournamentParticipantSlot | None:
    return await claim_participant_slot(
        db_session,
        tournament_id=tournament_id,
        max_participants=max_participants,
        participant_id=participant.id,
        claimed_at=claimed_at,
    )


async def ensure_participant_slot_claimed(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    max_participants: int | None,
    participant_id: str,
    claimed_at: datetime | None = None,
) -> TournamentParticipantSlot | None:
    """Repair/seed a slot for a participant created outside the API route."""

    if max_participants is None:
        return None
    existing = await db_session.scalar(
        select(TournamentParticipantSlot).where(
            TournamentParticipantSlot.participant_id == participant_id,
        )
    )
    if existing is not None:
        return existing
    return await claim_participant_slot(
        db_session,
        tournament_id=tournament_id,
        max_participants=max_participants,
        participant_id=participant_id,
        claimed_at=claimed_at,
    )
