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
from python_packages.platform_infra.invite_rate_limit import check_invite_rate_limit
from python_packages.platform_infra.security import (
    get_optional_authenticated_session,
)
from python_packages.platform_domain.tournaments import (
    INVITE_CODE_MAX_LENGTH,
    INVITE_CODE_MIN_LENGTH,
    normalize_strict_invite_code,
)

ACTIVE_PARTICIPANT_STATUSES = frozenset({"registered", "confirmed", "checked_in"})

# This is deliberately a closed set. A new private child GET must be added
# here, with an authorization/integration regression, before a bearer code can
# grant it. The route template is compared exactly after FastAPI has matched
# the request; a prefix/suffix check would make an accidentally added child a
# private bearer surface.
PRIVATE_TOURNAMENT_BEARER_ROUTE_PATHS = frozenset(
    {
        "/{slug}",
        "/{slug}/workspace",
        "/{slug}/participants",
        "/{slug}/matches",
        "/{slug}/bracket",
        "/api/v1/tournaments/{slug}",
        "/api/v1/tournaments/{slug}/workspace",
        "/api/v1/tournaments/{slug}/participants",
        "/api/v1/tournaments/{slug}/matches",
        "/api/v1/tournaments/{slug}/bracket",
    }
)
PRIVATE_TOURNAMENT_BEARER_ROUTE_SUFFIXES = frozenset(
    {"", "/workspace", "/participants", "/matches", "/bracket"}
)
# Compatibility names retained for service/unit consumers. The implementation
# is the domain validator shared by create, query, workspace and bearer reads.
BEARER_INVITE_CODE_MIN_LENGTH = INVITE_CODE_MIN_LENGTH
BEARER_INVITE_CODE_MAX_LENGTH = INVITE_CODE_MAX_LENGTH


def _matched_route_path(request: Request) -> str:
    return str(getattr(request.scope.get("route"), "path", "") or "")


def _request_tournament_slug(request: Request) -> str | None:
    """Read only FastAPI's parsed slug, never a raw-path substring."""

    slug = request.path_params.get("slug")
    if not isinstance(slug, str):
        return None
    return slug or None


def _route_suffix(route_path: str) -> str | None:
    """Return an exact tournament suffix for a matched route template."""

    for marker in (
        "/api/v1/tournaments/{slug}",
        "/tournaments/{slug}",
        "/{slug}",
    ):
        if route_path.startswith(marker):
            suffix = route_path[len(marker) :]
            if suffix and not suffix.startswith("/"):
                return None
            return suffix
    return None


# Keep the historical import name while ensuring all callers execute the one
# strict domain validator.
normalize_bearer_invite_code = normalize_strict_invite_code


def private_tournament_bearer_route_slug_from_request(request: Request) -> str | None:
    """Return a slug only for explicitly opted-in bearer read routes.

    The route template and parsed path parameter must both match. In
    particular, a hypothetical ``/{slug}/future-read`` child is not a bearer
    surface until this closed allowlist is deliberately extended.
    """

    if request.method.upper() != "GET":
        return None
    route_path = _matched_route_path(request)
    suffix = _route_suffix(route_path)
    if suffix is None:
        return None
    if route_path not in PRIVATE_TOURNAMENT_BEARER_ROUTE_PATHS:
        # Focused service tests may omit the API prefix, but still require the
        # exact registered tournament template and approved suffix.
        if not route_path.startswith(("/tournaments/{slug}", "/{slug}")):
            return None
        if suffix not in PRIVATE_TOURNAMENT_BEARER_ROUTE_SUFFIXES:
            return None
    return _request_tournament_slug(request)


def private_tournament_child_slug_from_request(request: Request) -> str | None:
    """Return the matched tournament slug for a private-read candidate route.

    The router dependency runs after FastAPI has matched the route, so use route
    metadata and parsed path params instead of a raw URL substring. This broad
    candidate detector is retained for the historical inactive-member deny
    guard; bearer authorization itself uses
    :func:`private_tournament_bearer_route_slug_from_request`, which is a
    closed allowlist.
    """

    if request.method.upper() != "GET":
        return None

    slug = _request_tournament_slug(request)
    route_path = _matched_route_path(request)
    marker = "{slug}"
    if marker not in route_path:
        return None

    suffix = route_path.split(marker, 1)[1].strip("/")
    if not suffix:
        return None
    return slug


def strict_invite_code_query(
    request: Request,
    *,
    parameter_name: str = "invite_code",
) -> str | None:
    """Parse one canonical query value without lossy normalization.

    Invite codes are generated/stored as ASCII uppercase alphanumerics. Lower
    case is accepted for the existing case-insensitive API contract, but
    punctuation, whitespace, duplicate parameters and out-of-range lengths
    fail closed before a route handler can apply its legacy normalizer.
    """

    values = request.query_params.getlist(parameter_name)
    if not values:
        return None
    if len(values) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{parameter_name} must be provided exactly once.",
        )
    raw_code = values[0]
    normalized_code = normalize_bearer_invite_code(raw_code)
    if normalized_code is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{parameter_name} must contain 10-24 ASCII letters or digits.",
        )
    return normalized_code


def _presented_invite_code(request: Request) -> str | None:
    """Parse the bearer ``invite_code`` query value."""

    return strict_invite_code_query(request)


async def enforce_tournament_bearer_read_rate_limit(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session),
) -> None:
    """Bound direct bearer-read attempts without logging or storing the code.

    The limiter is only entered when an opted-in route actually presents a
    syntactically valid code. Cookie-only internal reads therefore remain
    available when Redis is unavailable; bearer requests fail closed instead.
    """

    if private_tournament_bearer_route_slug_from_request(request) is None:
        return
    code = _presented_invite_code(request)
    if code is None:
        return
    # A platform admin is already independently authorized and should not be
    # made dependent on the anonymous bearer-attempt budget.
    if auth_session_has_admin_role(auth_session):
        return
    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id if auth_session is not None else "anonymous",
        operation="bearer_read",
        code=code,
    )


def auth_session_has_admin_role(auth_session) -> bool:
    if auth_session is None:
        return False
    return "admin" in auth_session.role_slugs or "superadmin" in auth_session.role_slugs


async def ensure_private_tournament_read_membership_is_active(
    request: Request,
    # Use the same dependency object as the route handler. FastAPI can then
    # reuse the authoritative optional-auth result instead of resolving a
    # wrapper that calls the same dependency a second time.
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Prevent retained participant rows from acting as private-read membership.

    Historical participant rows remain in the database for audit/history, but
    only explicitly active statuses grant participant membership for invite-only
    tournament child reads without a bearer code. A valid code bypasses that
    membership check only on the closed summary/workspace/participants/matches/
    bracket allowlist. Unknown future statuses fail closed until deliberately
    classified as active. Organizer and platform-admin authority remain
    independent of participant membership.

    The check applies to ordinary request-driven tournament reads; mutations
    continue through their existing authentication and workflow guards and do
    not inherit bearer authorization.
    """

    slug = private_tournament_child_slug_from_request(request)
    if slug is None:
        return
    bearer_slug = private_tournament_bearer_route_slug_from_request(request)
    invite_code = _presented_invite_code(request)
    if auth_session_has_admin_role(auth_session):
        return
    now = getattr(auth_session, "now", datetime.now(UTC))
    if invite_code and bearer_slug is not None:
        await check_invite_rate_limit(
            request,
            user_id=auth_session.user.id if auth_session is not None else "anonymous",
            operation="bearer_read",
            code=invite_code,
        )
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
        if visibility is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tournament not found.",
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
