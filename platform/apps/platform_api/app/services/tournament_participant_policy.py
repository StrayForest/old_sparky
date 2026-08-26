from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import TournamentInvite, TournamentParticipant
from python_packages.platform_infra.security import (
    get_optional_authenticated_session,
)


def _matched_route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or "")


def _is_invite_claim_request(request: Request) -> bool:
    return (
        request.method.upper() == "POST"
        and _matched_route_path(request).endswith("/invites/claim")
    )


async def _reject_disqualified_invite_claim(
    request: Request,
    *,
    user_id: str,
    db_session: AsyncSession,
) -> None:
    try:
        payload = await request.json()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return

    code = "".join(
        char
        for char in str(payload.get("code") or "").upper()
        if char.isalnum()
    )
    if not code:
        return

    participant_status = await db_session.scalar(
        select(TournamentParticipant.status)
        .join(
            TournamentInvite,
            TournamentInvite.tournament_id == TournamentParticipant.tournament_id,
        )
        .where(
            TournamentInvite.code == code,
            TournamentParticipant.user_id == user_id,
        )
    )
    if participant_status == "disqualified":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Disqualified participants cannot redeem invites for this tournament.",
        )


async def enforce_tournament_participant_policy(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Reject invite redemption for retained tournament disqualifications."""

    if auth_session is None or not _is_invite_claim_request(request):
        return

    await _reject_disqualified_invite_claim(
        request,
        user_id=auth_session.user.id,
        db_session=db_session,
    )
