from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.tournaments import TournamentWorkflowError
from python_packages.platform_infra.models import (
    TournamentParticipant,
    TournamentParticipantSlot,
)


async def has_free_participant_slot(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> bool:
    return (
        await db_session.scalar(
            select(TournamentParticipantSlot.id)
            .where(
                TournamentParticipantSlot.tournament_id == tournament_id,
                TournamentParticipantSlot.participant_id.is_(None),
            )
            .limit(1)
        )
    ) is not None


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
        raise TournamentWorkflowError("Tournament participant limit has been reached.")
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
