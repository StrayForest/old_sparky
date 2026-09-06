from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    Tournament,
    TournamentInvite,
    TournamentParticipant,
)
from python_packages.platform_infra.security import (
    get_optional_authenticated_session_for_tournament_policy,
)

ACTIVE_PARTICIPANT_STATUSES = frozenset({"registered", "confirmed", "checked_in"})


def private_tournament_child_slug_from_request(request: Request) -> str | None:
    """Return the matched tournament slug for a private-read candidate route.

    The router dependency runs after FastAPI has matched the route, so use route
    metadata and parsed path params instead of maintaining a suffix allowlist.
    The tournament summary itself (``/{slug}``) is intentionally excluded;
    every routed GET child of ``/{slug}`` is covered automatically, including
    future child endpoints added to the tournament router.
    """

    if request.method.upper() != "GET":
        return None

    slug = str(request.path_params.get("slug") or "").strip()
    if not slug:
        return None

    route = request.scope.get("route")
    route_path = str(getattr(route, "path", "") or "")
    marker = "{slug}"
    if marker not in route_path:
        return None

    suffix = route_path.split(marker, 1)[1].strip("/")
    if not suffix:
        return None
    return slug


def auth_session_has_admin_role(auth_session) -> bool:
    if auth_session is None:
        return False
    return "admin" in auth_session.role_slugs or "superadmin" in auth_session.role_slugs


async def ensure_private_tournament_read_membership_is_active(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session_for_tournament_policy),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Prevent retained participant rows from acting as private-read membership.

    Historical participant rows remain in the database for audit/history, but
    only explicitly active statuses grant participant membership for invite-only
    tournament child reads. Unknown future statuses fail closed until they are
    deliberately classified as active. Organizer and platform-admin authority
    remain independent of participant membership.

    The check applies to ordinary request-driven tournament reads; mutations
    continue through their existing authentication and workflow guards.
    """

    slug = private_tournament_child_slug_from_request(request)
    if slug is None:
        return
    if auth_session_has_admin_role(auth_session):
        return
    invite_code = "".join(
        char
        for char in str(request.query_params.get("invite_code") or "").upper()
        if char.isalnum()
    )
    now = getattr(auth_session, "now", datetime.now(UTC))
    if invite_code:
        valid_invite = await db_session.scalar(
            select(TournamentInvite.id)
            .join(Tournament, Tournament.id == TournamentInvite.tournament_id)
            .where(
                Tournament.slug == slug,
                TournamentInvite.code == invite_code,
                TournamentInvite.revoked_at.is_(None),
                (TournamentInvite.expires_at.is_(None) | (TournamentInvite.expires_at > now)),
            )
        )
        if valid_invite is not None:
            return

    if auth_session is None:
        visibility = await db_session.scalar(
            select(Tournament.visibility).where(Tournament.slug == slug)
        )
        if visibility == "public":
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid invite code is required to view this private tournament.",
        )

    user_id = auth_session.user.id
    row = (
        await db_session.execute(
            select(
                Tournament.visibility,
                Tournament.organizer_user_id,
                TournamentParticipant.status,
            )
            .outerjoin(
                TournamentParticipant,
                (TournamentParticipant.tournament_id == Tournament.id)
                & (TournamentParticipant.user_id == user_id),
            )
            .where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        return

    visibility, organizer_user_id, participant_status = row
    if (
        visibility != "invite_only"
        or organizer_user_id == auth_session.user.id
        or participant_status is None
        or participant_status in ACTIVE_PARTICIPANT_STATUSES
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Inactive tournament participants cannot access private tournament workspace data.",
    )
