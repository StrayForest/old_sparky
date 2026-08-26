from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import (
    SseStreamDbAdmissionUnavailable,
    get_db_session,
    get_stream_db_session,
    stream_db_session,
)
from python_packages.platform_infra.models import (
    Role,
    Tournament,
    TournamentParticipant,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import (
    get_optional_authenticated_session,
    get_optional_authenticated_session_for_stream,
)
from python_packages.platform_infra.sse_admission import (
    SseAdmissionTicketInvalid,
    verify_sse_admission_ticket,
)
from python_packages.platform_infra.sse_connection_limit import (
    add_sse_authenticated_user_scope,
)

ACTIVE_PARTICIPANT_STATUSES = frozenset({"registered", "confirmed", "checked_in"})
ADMIN_ROLE_SLUGS = ("admin", "superadmin")


@dataclass(frozen=True, slots=True)
class TournamentStreamAccessContext:
    decision: Literal["public", "deny", "organizer", "admin", "active_participant"]
    slug: str
    user_id: str | None = None
    session_id: str | None = None
    tournament: Tournament | None = None
    tournament_id: str | None = None


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


def _auth_session_id(auth_session) -> str | None:
    session = getattr(auth_session, "session", None)
    value = str(getattr(session, "id", "") or "").strip()
    return value or None


async def _authenticated_stream_session_is_current(
    db_session: AsyncSession,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    now = datetime.now(UTC)
    session_user_id = await db_session.scalar(
        select(UserSession.user_id)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.invalidated_at.is_(None),
            UserSession.expires_at > now,
            User.status == "active",
        )
    )
    return session_user_id is not None


async def _user_still_has_admin_role(
    db_session: AsyncSession,
    *,
    user_id: str,
) -> bool:
    role_slug = await db_session.scalar(
        select(Role.slug)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user_id,
            Role.slug.in_(ADMIN_ROLE_SLUGS),
        )
        .limit(1)
    )
    return role_slug is not None


async def current_tournament_stream_access_is_valid(tournament_id: str) -> bool:
    """Revalidate long-lived tournament stream access before emitting data.

    Public streams remain public-only. Every authenticated private stream
    revalidates the exact server-side session plus the authority that admitted
    it (organizer, platform admin, or active participant) before every event
    and at periodic idle checkpoints. Session logout/revocation and role
    removal therefore take effect before any later private event, without
    waiting for the bounded SSE lifetime to expire.
    """

    try:
        access_context = current_tournament_stream_access_context()
        if access_context is not None:
            if access_context.decision == "deny":
                return False

            async with stream_db_session() as db_session:
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
                            & (
                                TournamentParticipant.user_id
                                == (access_context.user_id or "")
                            ),
                        )
                        .where(
                            Tournament.id == tournament_id,
                            Tournament.slug == access_context.slug,
                        )
                    )
                ).first()
                if row is None:
                    return False

                tournament_visibility = str(row[0])
                organizer_user_id = str(row[1])
                participant_status = row[2]

                if access_context.decision == "public":
                    if access_context.user_id and access_context.session_id:
                        if not await _authenticated_stream_session_is_current(
                            db_session,
                            user_id=access_context.user_id,
                            session_id=access_context.session_id,
                        ):
                            return False
                    return tournament_visibility == "public"
                if tournament_visibility != "invite_only":
                    return True
                if not access_context.user_id or not access_context.session_id:
                    return False
                if not await _authenticated_stream_session_is_current(
                    db_session,
                    user_id=access_context.user_id,
                    session_id=access_context.session_id,
                ):
                    return False

                if access_context.decision == "admin":
                    return await _user_still_has_admin_role(
                        db_session,
                        user_id=access_context.user_id,
                    )
                if access_context.decision == "organizer":
                    return organizer_user_id == access_context.user_id
                if access_context.decision == "active_participant":
                    return participant_status in ACTIVE_PARTICIPANT_STATUSES
                return False

        async with stream_db_session() as db_session:
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
            TournamentStreamAccessContext(decision="public", slug=str(row[0]))
        )
        return True
    except SseStreamDbAdmissionUnavailable:
        # Revalidation runs after the response has started.  A 503 cannot be
        # sent safely there; closing the stream makes the client use polling
        # or reconnect with the normal backoff instead.
        return False


def _apply_stream_access_context(
    *,
    slug: str,
    auth_session,
    tournament_visibility: str,
    organizer_user_id: str | None,
    participant_status: str | None,
    tournament: Tournament | None = None,
) -> None:
    if auth_session_has_admin_role(auth_session):
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="admin",
                slug=slug,
                user_id=auth_session.user.id,
                session_id=_auth_session_id(auth_session),
                tournament=tournament,
            )
        )
        return
    if auth_session is None:
        return

    user_id = auth_session.user.id
    session_id = _auth_session_id(auth_session)
    if tournament_visibility != "invite_only":
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="public",
                slug=slug,
                tournament=tournament,
            )
        )
        return
    if organizer_user_id == user_id:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="organizer",
                slug=slug,
                user_id=user_id,
                session_id=session_id,
                tournament=tournament,
            )
        )
        return
    if participant_status is None:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="deny",
                slug=slug,
                user_id=user_id,
                session_id=session_id,
                tournament=tournament,
            )
        )
        return
    if participant_status in ACTIVE_PARTICIPANT_STATUSES:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="active_participant",
                slug=slug,
                user_id=user_id,
                session_id=session_id,
                tournament=tournament,
            )
        )
        return

    _tournament_stream_access_context.set(
        TournamentStreamAccessContext(
            decision="deny",
            slug=slug,
            user_id=user_id,
            session_id=session_id,
            tournament=tournament,
        )
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Inactive tournament participants cannot access private tournament workspace data.",
    )


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
    private streams revalidate the current server-side session and the authority
    used at admission before every emitted event or keepalive.
    """

    _tournament_stream_access_context.set(None)
    slug = private_tournament_child_slug_from_request(request)
    if slug is None:
        return
    if auth_session_has_admin_role(auth_session):
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision="admin",
                slug=slug,
                user_id=auth_session.user.id,
                session_id=_auth_session_id(auth_session),
            )
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

    _apply_stream_access_context(
        slug=slug,
        auth_session=auth_session,
        tournament_visibility=row[0],
        organizer_user_id=row[1],
        participant_status=row[2],
    )


async def ensure_private_tournament_read_membership_is_active_for_stream(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session_for_stream),
    db_session: AsyncSession = Depends(get_stream_db_session, scope="function"),
) -> None:
    """Apply private-read authorization with endpoint-scoped DB access."""

    _tournament_stream_access_context.set(None)
    slug = private_tournament_child_slug_from_request(request)
    if slug is None or auth_session is None:
        return

    row = (
        await db_session.execute(
            select(Tournament, TournamentParticipant.status)
            .outerjoin(
                TournamentParticipant,
                (TournamentParticipant.tournament_id == Tournament.id)
                & (TournamentParticipant.user_id == auth_session.user.id),
            )
            .where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(decision="deny", slug=slug)
        )
        return

    tournament = row[0]
    _apply_stream_access_context(
        slug=slug,
        auth_session=auth_session,
        tournament_visibility=tournament.visibility,
        organizer_user_id=tournament.organizer_user_id,
        participant_status=row[1],
        tournament=tournament,
    )


def _stream_db_unavailable_http_exception() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Live-update admission is temporarily full. Use polling.",
        headers={
            "Retry-After": "1",
            "Cache-Control": "no-store",
        },
    )


async def admit_tournament_bracket_stream(
    request: Request,
    slug: str,
    ticket: str | None = Query(default=None, alias="ticket"),
) -> None:
    """Admit an SSE stream with a ticket fast path and DB fallback.

    A ticket is issued only after the normal workspace authorization path has
    proved visibility. Private tickets remain bound to the same session
    cookie and are revalidated against PostgreSQL while the stream is open.
    Unticketed clients retain the authoritative bounded-DB admission path.
    """

    _tournament_stream_access_context.set(None)
    if ticket is not None:
        settings = get_settings()
        session_token = request.cookies.get(settings.platform_session_cookie_name)
        try:
            admitted = verify_sse_admission_ticket(
                ticket,
                expected_slug=slug,
                session_token=session_token,
            )
        except SseAdmissionTicketInvalid as exc:
            raise HTTPException(
                status_code=401,
                detail="SSE admission ticket is invalid or expired.",
                headers={"Cache-Control": "no-store"},
            ) from exc
        _tournament_stream_access_context.set(
            TournamentStreamAccessContext(
                decision=admitted.access,
                slug=admitted.slug,
                user_id=admitted.user_id,
                session_id=admitted.session_id,
                tournament_id=admitted.tournament_id,
            )
        )
        if admitted.user_id is not None:
            await add_sse_authenticated_user_scope(request, admitted.user_id)
        return

    try:
        async with stream_db_session() as db_session:
            auth_session = await get_optional_authenticated_session_for_stream(
                request,
                db_session,
            )
            await ensure_private_tournament_read_membership_is_active_for_stream(
                request,
                auth_session,
                db_session,
            )
            context = current_tournament_stream_access_context()
            if context is not None and context.decision == "deny" and auth_session is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Tournament roster and bracket data are visible only to joined "
                        "participants, the organizer, or platform admins."
                    ),
                )
            if context is None or context.decision == "deny":
                tournament = await db_session.scalar(
                    select(Tournament).where(Tournament.slug == slug)
                )
                if tournament is None:
                    raise HTTPException(status_code=404, detail="Tournament not found.")
                if tournament.visibility == "invite_only":
                    raise HTTPException(
                        status_code=401,
                        detail="Authentication required to view invite-only tournament bracket data.",
                    )
                context = TournamentStreamAccessContext(
                    decision="public",
                    slug=slug,
                    tournament=tournament,
                    tournament_id=tournament.id,
                )
                _tournament_stream_access_context.set(context)
            elif context.tournament is None:
                tournament = await db_session.scalar(
                    select(Tournament).where(Tournament.slug == slug)
                )
                if tournament is None:
                    raise HTTPException(status_code=404, detail="Tournament not found.")
                _tournament_stream_access_context.set(
                    TournamentStreamAccessContext(
                        decision=context.decision,
                        slug=context.slug,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        tournament=tournament,
                        tournament_id=tournament.id,
                    )
                )
            if auth_session is not None:
                await add_sse_authenticated_user_scope(
                    request,
                    str(auth_session.user.id),
                )
    except SseStreamDbAdmissionUnavailable as exc:
        raise _stream_db_unavailable_http_exception() from exc
