from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import Tournament, TournamentParticipant
from python_packages.platform_infra.security import get_optional_authenticated_session

INACTIVE_PARTICIPANT_STATUSES = frozenset({"withdrawn", "disqualified"})
PRIVATE_WORKSPACE_READ_SUFFIXES = frozenset(
    {
        "workspace",
        "participants",
        "matches",
        "bracket",
        "bracket/events",
    }
)


def private_workspace_slug_from_request(request: Request) -> str | None:
    if request.method.upper() != "GET":
        return None

    parts = tuple(part for part in request.url.path.split("/") if part)
    try:
        tournaments_index = parts.index("tournaments")
    except ValueError:
        return None

    slug_index = tournaments_index + 1
    if slug_index >= len(parts):
        return None

    slug = parts[slug_index]
    suffix = "/".join(parts[slug_index + 1 :])
    if not slug or suffix not in PRIVATE_WORKSPACE_READ_SUFFIXES:
        return None
    return slug


def auth_session_has_admin_role(auth_session) -> bool:
    if auth_session is None:
        return False
    return "admin" in auth_session.role_slugs or "superadmin" in auth_session.role_slugs


async def ensure_inactive_participant_has_no_private_workspace_access(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Revoke private tournament workspace reads once participation is inactive.

    Existing route handlers historically use the existence of a participant row
    as their private-workspace membership signal. Participant rows are retained
    after withdrawal/disqualification for audit/history, so an inactive row must
    be rejected before those handlers are reached.
    """

    slug = private_workspace_slug_from_request(request)
    if slug is None or auth_session is None:
        return
    if auth_session_has_admin_role(auth_session):
        return

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

    tournament_visibility = row[0]
    organizer_user_id = row[1]
    participant_status = row[2]
    if tournament_visibility != "invite_only" or organizer_user_id == user_id:
        return
    if participant_status not in INACTIVE_PARTICIPANT_STATUSES:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Inactive tournament participants cannot access private tournament workspace data.",
    )
