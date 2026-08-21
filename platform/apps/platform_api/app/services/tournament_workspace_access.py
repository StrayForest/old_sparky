from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import get_db_session, session_factory
from python_packages.platform_infra.models import Tournament, TournamentParticipant
from python_packages.platform_infra.security import get_optional_authenticated_session

ACTIVE_PARTICIPANT_STATUSES = frozenset({"registered", "confirmed", "checked_in"})


@dataclass(frozen=True, slots=True)
class TournamentStreamAccessContext:
    decision: Literal["allow", "deny", "active_participant"]
    slug: str
    user_id: str | None = None


_tournament_stream_access_context: ContextVar[TournamentStreamAccessContext | None] = ContextVar(
    "tournament_stream_access_context",
    default=None,
)


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


def current_tournament_stream_access_context() -> TournamentStreamAccessContext | None:
    return _tournament_stream_access_context.get()


async def current_tournament_stream_access_is_valid(tournament_id: str) -> bool:
    """Revalidate long-lived tournament stream access before emitting data.

    Private participant streams are checked against the current participant row
    on every emission. Missing request context fails closed for existing private
    tournaments while anonymous public streams are allowed after a visibility
    lookup. A missing tournament is tolerated for the low-level Redis stream
    helper, which is also exercised independently of HTTP routing in tests.
    """

    access_context = current_tournament_stream_access_context()
    if access_context is not None:
        if access_context.decision == "allow":
            return True
        if access_context.decision == "deny":
            return False
        if not access_context.user_id:
            return False

        async with session_factory()() as db_session:
            row = (
                await db_session.execute(
                    select(
                        Tournament.visibility,
                        TournamentParticipant.status,
                    )
                    .outerjoin(
                        TournamentParticipant,
                        (TournamentParticipant.tournament_id == Tournament.id)
                        & (TournamentParticipant.user_id == access_context.user_id),
                    )
                    .where(
                        Tournament.id == tournament_id,
                        Tournament.slug == access_context.slug,
                    )
                )
            ).first()
        if row is None:
            return False
        if row[0] != "invite_only":
            return True
        return row[1] in ACTIVE_PARTICIPANT_STATUSES

    async with session_factory()() as db_session:
        row = (
            await db_session.execute(
                select(Tournament.slug, Tournament.visibility).where(
                    Tournament.id == tournament_id
                )
            )
        ).first()
    if row is None:
        return True
    if row[1] != "public":
        return False
    _tournament_stream_access_context.set(
        TournamentStreamAccessContext(decision="allow", slug=str(row[0]))
    )
    return True


async def ensure_private_tournament_read_membership_is_active(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Prevent retained participant rows from acting as private-read membership.

    Historical participant rows remain in the database for audit/history, but
    only explicitly active statuses grant participant membership for invite-only
    tournament child reads. Unknown future statuses fail closed until they are
    deliberately classified as active. Organizer and platform-admin authority
    remain independent of participant membership.

    The dependency also records a request-local stream decision. Long-lived
    private participant streams revalidate the current participant status before
    every emitted event or keepalive, so access is revoked after a withdrawal or
    disqualification even when the SSE connection was opened earlier.
    """

    _tournament_stream_access_context.set(None)
    slug = private_tournament_child_slug_from_request(request)
    if slug is None:
        return
    if auth_session_has_admin_role(auth_session):
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(decision="allow", slug=slug)
        )
        return
    if auth_session is None:
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
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(decision="deny", slug=slug)
        )
        return

    tournament_visibility = row[0]
    organizer_user_id = row[1]
    participant_status = row[2]
    if tournament_visibility != "invite_only" or organizer_user_id == user_id:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(decision="allow", slug=slug)
        )
        return
    if participant_status is None:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(decision="deny", slug=slug, user_id=user_id)
        )
        return
    if participant_status in ACTIVE_PARTICIPANT_STATUSES:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="active_participant",
                slug=slug,
                user_id=user_id,
            )
        )
        return

    _tournament_stream_access_context.set(
        TournamentStreamAccessContext(decision="deny", slug=slug, user_id=user_id)
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Inactive tournament participants cannot access private tournament workspace data.",
    )
