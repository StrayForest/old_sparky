from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.pagination import (
    PARTICIPANT_LIST_DEFAULT_LIMIT,
    PARTICIPANT_LIST_MAX_LIMIT,
    set_pagination_headers,
)
from apps.platform_api.app.api.schemas import TournamentParticipantManagementResponse
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    PlayerProfile,
    Tournament,
    TournamentParticipant,
    User,
)
from python_packages.platform_infra.security import get_authenticated_session

router = APIRouter()


def _serialize_participant(
    participant: TournamentParticipant,
    display_name: str,
) -> TournamentParticipantManagementResponse:
    return TournamentParticipantManagementResponse(
        id=participant.id,
        tournament_id=participant.tournament_id,
        user_id=participant.user_id,
        display_name=display_name,
        status=participant.status,
        entry_type=participant.entry_type,
        team_name=participant.team_name,
        moderation_note=participant.moderation_note,
        moderated_at=participant.moderated_at,
        moderated_by_user_id=participant.moderated_by_user_id,
        created_at=participant.created_at,
    )


@router.get(
    "/{slug}/participants/manage",
    response_model=list[TournamentParticipantManagementResponse],
)
async def list_tournament_participants_for_management(
    slug: str,
    response: Response,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(
        default=PARTICIPANT_LIST_DEFAULT_LIMIT,
        ge=1,
        le=PARTICIPANT_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentParticipantManagementResponse]:
    """Return the organizer's full roster, including inactive participant rows."""

    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.slug == slug)
    )
    if tournament is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tournament not found.",
        )
    if tournament.organizer_user_id != auth_session.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organizer can manage this tournament.",
        )

    filters = [TournamentParticipant.tournament_id == tournament.id]
    normalized_search = (search or "").strip().casefold()
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                func.lower(PlayerProfile.display_name).like(search_pattern),
                func.lower(PlayerProfile.handle).like(search_pattern),
                func.lower(User.display_name).like(search_pattern),
            )
        )

    total = int(
        await db_session.scalar(
            select(func.count())
            .select_from(TournamentParticipant)
            .join(User, User.id == TournamentParticipant.user_id)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == TournamentParticipant.user_id)
            .where(*filters)
        )
        or 0
    )
    rows = (
        await db_session.execute(
            select(
                TournamentParticipant,
                func.coalesce(PlayerProfile.display_name, User.display_name).label(
                    "display_name"
                ),
            )
            .join(User, User.id == TournamentParticipant.user_id)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == TournamentParticipant.user_id)
            .where(*filters)
            .order_by(
                TournamentParticipant.created_at.asc(),
                TournamentParticipant.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    set_pagination_headers(
        response,
        total=total,
        limit=limit,
        offset=offset,
        returned=len(rows),
    )
    return [
        _serialize_participant(participant, str(display_name or "Unknown"))
        for participant, display_name in rows
    ]
