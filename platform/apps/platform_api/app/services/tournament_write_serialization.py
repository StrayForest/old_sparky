from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.tournaments import can_organizer_moderate_participants
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.invite_rate_limit import check_invite_rate_limit
from python_packages.platform_infra.models import (
    Tournament,
    TournamentInvite,
    TournamentParticipant,
)
from apps.platform_api.app.services.tournament_participant_capacity import (
    has_free_participant_slot,
)
from python_packages.platform_infra.security import (
    get_optional_authenticated_session,
)


ACTIVE_PARTICIPANT_STATUSES = ("registered", "confirmed", "checked_in")
INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")
PARTICIPANT_ADD_NOT_AVAILABLE = "Participant could not be added."


def _matched_route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or "")


def _is_invite_revoke_request(request: Request) -> bool:
    return (
        request.method.upper() == "DELETE"
        and _matched_route_path(request).endswith("/{slug}/invites/{invite_id}")
    )


def _is_organizer_participant_add_request(request: Request) -> bool:
    return (
        request.method.upper() == "POST"
        and _matched_route_path(request).endswith("/{slug}/participants/manage")
    )


def _is_self_join_request(request: Request) -> bool:
    return (
        request.method.upper() == "POST"
        and _matched_route_path(request).endswith("/{slug}/join")
    )


def _participant_mutation_kind(request: Request) -> str | None:
    method = request.method.upper()
    route_path = _matched_route_path(request)
    if route_path.endswith("/{slug}/join") and method in {"POST", "DELETE"}:
        return "self"
    if route_path.endswith("/{slug}/participants/manage") and method == "POST":
        return "organizer"
    if (
        route_path.endswith("/{slug}/participants/{participant_id}")
        and method == "DELETE"
    ):
        return "organizer"
    if (
        route_path.endswith("/{slug}/participants/{participant_id}/moderation")
        and method == "PATCH"
    ):
        return "moderation"
    return None


async def _request_json_object(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _tournament_owner_snapshot(
    db_session: AsyncSession,
    *,
    slug: str,
) -> tuple[str, str] | None:
    row = (
        await db_session.execute(
            select(Tournament.id, Tournament.organizer_user_id).where(
                Tournament.slug == slug
            )
        )
    ).first()
    if row is None:
        return None
    return str(row.id), str(row.organizer_user_id)


async def _lock_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str | None = None,
    slug: str | None = None,
) -> tuple[str, int | None, str] | None:
    stmt = select(
        Tournament.id,
        Tournament.max_participants,
        Tournament.status,
    )
    if tournament_id is not None:
        stmt = stmt.where(Tournament.id == tournament_id)
    elif slug is not None:
        stmt = stmt.where(Tournament.slug == slug)
    else:
        raise ValueError("tournament_id or slug is required")
    row = (await db_session.execute(stmt.with_for_update())).first()
    if row is None:
        return None
    return str(row.id), row.max_participants, str(row.status)


async def _lock_invite_revoke(
    request: Request,
    *,
    user_id: str,
    db_session: AsyncSession,
) -> None:
    slug = str(request.path_params.get("slug") or "").strip()
    invite_id = str(request.path_params.get("invite_id") or "").strip()
    if not slug or not invite_id:
        return

    owner_snapshot = await _tournament_owner_snapshot(db_session, slug=slug)
    if owner_snapshot is None:
        return
    tournament_id, organizer_user_id = owner_snapshot
    if organizer_user_id != user_id:
        return

    locked = await _lock_tournament(db_session, tournament_id=tournament_id)
    if locked is None:
        return
    await db_session.execute(
        select(TournamentInvite.id)
        .where(
            TournamentInvite.id == invite_id,
            TournamentInvite.tournament_id == tournament_id,
        )
        .with_for_update()
    )


async def _enforce_restore_capacity(
    request: Request,
    *,
    tournament_id: str,
    max_participants: int | None,
    tournament_status: str,
    db_session: AsyncSession,
) -> None:
    if max_participants is None or not can_organizer_moderate_participants(
        tournament_status
    ):
        return

    participant_id = str(request.path_params.get("participant_id") or "").strip()
    if not participant_id:
        return
    payload = await _request_json_object(request)
    target_status = str(payload.get("status") or "").strip()
    if target_status not in ACTIVE_PARTICIPANT_STATUSES:
        return

    current_status = await db_session.scalar(
        select(TournamentParticipant.status).where(
            TournamentParticipant.id == participant_id,
            TournamentParticipant.tournament_id == tournament_id,
        )
    )
    if current_status not in INACTIVE_PARTICIPANT_STATUSES:
        return

    if max_participants is not None and not await has_free_participant_slot(
        db_session,
        tournament_id=tournament_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament participant limit has been reached.",
        )


async def _lock_participant_mutation(
    request: Request,
    *,
    user_id: str,
    mutation_kind: str,
    db_session: AsyncSession,
) -> None:
    slug = str(request.path_params.get("slug") or "").strip()
    if not slug:
        return

    if mutation_kind == "self":
        if _is_self_join_request(request):
            return
        locked = await _lock_tournament(db_session, slug=slug)
    else:
        # Organizer moderation and removals retain the tournament-row lock so
        # lifecycle/workflow state and secondary rows keep their established
        # lock order.
        owner_snapshot = await _tournament_owner_snapshot(db_session, slug=slug)
        if owner_snapshot is None:
            return
        tournament_id, organizer_user_id = owner_snapshot
        if organizer_user_id != user_id:
            return
        locked = await _lock_tournament(db_session, tournament_id=tournament_id)

    if locked is None:
        return
    tournament_id, max_participants, tournament_status = locked
    if mutation_kind == "moderation":
        await _enforce_restore_capacity(
            request,
            tournament_id=tournament_id,
            max_participants=max_participants,
            tournament_status=tournament_status,
            db_session=db_session,
        )


async def serialize_tournament_write_invariants(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Serialize lifecycle/invite mutations while joins claim independent slots.

    Locks are acquired in tournament -> invite order and remain owned by the
    request's shared database transaction until the route commits, rolls back,
    or the dependency session closes. Organizer participant adds are rate
    limited and may resolve only accounts that already redeemed an invite for
    this tournament, so the endpoint cannot serve as a global account oracle.
    """

    if auth_session is None:
        return

    if _is_invite_revoke_request(request):
        await _lock_invite_revoke(
            request,
            user_id=auth_session.user.id,
            db_session=db_session,
        )
        return

    if _is_organizer_participant_add_request(request):
        await check_invite_rate_limit(
            request,
            user_id=auth_session.user.id,
            operation="manage",
        )

    mutation_kind = _participant_mutation_kind(request)
    if mutation_kind is not None:
        await _lock_participant_mutation(
            request,
            user_id=auth_session.user.id,
            mutation_kind=mutation_kind,
            db_session=db_session,
        )
