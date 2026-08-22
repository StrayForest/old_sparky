from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.services.player_commitments import release_active_commitments
from apps.platform_api.app.services.tournament_runtime_cache import (
    invalidate_tournament_runtime_caches,
)
from apps.platform_api.app.services.tournament_workflow import (
    participant_status_is_inactive,
    prune_participant_from_active_captain_round,
    prune_participant_from_active_ready_round,
)
from python_packages.platform_domain.tournaments import (
    TournamentWorkflowError,
    ensure_organizer_can_moderate_participants,
    is_solo_tournament_format,
    transition_participant_status,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    Tournament,
    TournamentInvite,
    TournamentParticipant,
)
from python_packages.platform_infra.security import get_optional_authenticated_session


def _matched_route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or "")


def _is_organizer_remove_request(request: Request) -> bool:
    return (
        request.method.upper() == "DELETE"
        and _matched_route_path(request).endswith("/{slug}/participants/{participant_id}")
        and bool(str(request.path_params.get("slug") or "").strip())
        and bool(str(request.path_params.get("participant_id") or "").strip())
    )


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


async def _soft_exclude_participant(
    request: Request,
    *,
    auth_session,
    db_session: AsyncSession,
) -> None:
    slug = str(request.path_params.get("slug") or "").strip()
    participant_id = str(request.path_params.get("participant_id") or "").strip()
    if not slug or not participant_id:
        return

    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.slug == slug)
    )
    if tournament is None or tournament.organizer_user_id != auth_session.user.id:
        return

    participant = await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.id == participant_id,
            TournamentParticipant.tournament_id == tournament.id,
        )
    )
    if participant is None:
        return

    try:
        ensure_organizer_can_moderate_participants(tournament.status)
        next_status = transition_participant_status(
            participant.status,
            "disqualified",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if participant.status != next_status:
        previous_status = participant.status
        participant.status = next_status
        participant.moderated_at = auth_session.now
        participant.moderated_by_user_id = auth_session.user.id

        if (
            is_solo_tournament_format(tournament.format_slug)
            and not participant_status_is_inactive(previous_status)
        ):
            await prune_participant_from_active_ready_round(
                db_session,
                tournament=tournament,
                user_id=participant.user_id,
                actor_user_id=auth_session.user.id,
                now=auth_session.now,
                participant_status=participant.status,
            )
            await prune_participant_from_active_captain_round(
                db_session,
                tournament=tournament,
                user_id=participant.user_id,
                actor_user_id=auth_session.user.id,
                now=auth_session.now,
                participant_status=participant.status,
            )
            await release_active_commitments(
                db_session,
                tournament_id=tournament.id,
                user_ids=[participant.user_id],
                released_at=auth_session.now,
                release_reason="participant_disqualified",
            )

        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="tournament.participant.manage_remove",
            subject_type="tournament_participant",
            subject_id=participant.id,
            payload={
                "tournament_slug": tournament.slug,
                "user_id": participant.user_id,
                "from_status": previous_status,
                "to_status": participant.status,
                "retained_record": True,
            },
        )
        await db_session.commit()
        invalidate_tournament_runtime_caches(tournament.id)

    raise HTTPException(status_code=status.HTTP_204_NO_CONTENT)


async def enforce_tournament_participant_policy(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Enforce participant-lifecycle rules that apply across tournament routes.

    Organizer removal is a retained disqualification, never a physical deletion.
    Disqualified users cannot redeem another invite for the same tournament.
    """

    if auth_session is None:
        return

    if _is_invite_claim_request(request):
        await _reject_disqualified_invite_claim(
            request,
            user_id=auth_session.user.id,
            db_session=db_session,
        )
        return

    if _is_organizer_remove_request(request):
        await _soft_exclude_participant(
            request,
            auth_session=auth_session,
            db_session=db_session,
        )
