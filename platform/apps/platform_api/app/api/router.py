from fastapi import APIRouter, Depends

from apps.platform_api.app.api.routes import (
    admin,
    admin_user_delete,
    audit,
    auth,
    auth_identities,
    content,
    health,
    media,
    profile_workspace,
    profiles,
    registration,
    security_reports,
    stats,
    tournament_participants,
    tournaments,
    users,
)
from apps.platform_api.app.services.tournament_participant_policy import (
    enforce_tournament_participant_policy,
)
from apps.platform_api.app.services.tournament_workspace_access import (
    ensure_private_tournament_read_membership_is_active,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
# Registration is intentionally mounted before the legacy auth router so the
# pending-account restart flow owns POST /auth/register without disturbing the
# rest of the authentication endpoints.
api_router.include_router(registration.router, prefix="/auth", tags=["auth"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(auth_identities.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(profiles.router, prefix="/profiles", tags=["profiles"])
api_router.include_router(profile_workspace.router, prefix="/profiles", tags=["profiles"])
api_router.include_router(media.router, prefix="/media", tags=["media"])

tournament_dependencies = [
    Depends(ensure_private_tournament_read_membership_is_active),
    Depends(enforce_tournament_participant_policy),
]
api_router.include_router(
    tournaments.router,
    prefix="/tournaments",
    tags=["tournaments"],
    dependencies=tournament_dependencies,
)
api_router.include_router(
    tournament_participants.router,
    prefix="/tournaments",
    tags=["tournaments"],
    dependencies=tournament_dependencies,
)
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_user_delete.router, prefix="/admin", tags=["admin"])
api_router.include_router(audit.router, prefix="/audit", tags=["audit"])
api_router.include_router(content.router, prefix="/content", tags=["content"])
api_router.include_router(security_reports.router, prefix="/security", tags=["security"])
