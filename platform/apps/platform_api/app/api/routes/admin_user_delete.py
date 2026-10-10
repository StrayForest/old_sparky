from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.media.hard_delete import (
    MediaCleanupRequired,
    purge_deleted_media_metadata,
)
from python_packages.platform_infra.models import (
    Role,
    Tournament,
    TournamentParticipant,
    TournamentTeamMember,
    User,
    UserRole,
)
from python_packages.platform_infra.security import (
    get_authenticated_session,
    invalidate_user_session_cache,
)

router = APIRouter()


class AdminUserDeleteRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=320)
    note: str = Field(min_length=3, max_length=1000)


def ensure_superadmin_role(auth_session) -> None:
    if "superadmin" not in auth_session.role_slugs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin role is required.",
        )


async def role_slugs_for_user(db_session: AsyncSession, user_id: str) -> list[str]:
    rows = (
        await db_session.scalars(
            select(Role.slug)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
    ).all()
    return sorted(str(role) for role in rows)


async def tournament_ids_for_profile_access(
    db_session: AsyncSession, user_id: str
) -> set[str]:
    participant_ids = await db_session.scalars(
        select(TournamentParticipant.tournament_id).where(
            TournamentParticipant.user_id == user_id
        )
    )
    roster_ids = await db_session.scalars(
        select(TournamentTeamMember.tournament_id).where(
            TournamentTeamMember.user_id == user_id
        )
    )
    return {str(value) for value in (*participant_ids.all(), *roster_ids.all())}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_user(
    user_id: str,
    payload: AdminUserDeleteRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Permanently delete a normal platform account and its cascading user data.

    Destructive deletion is deliberately superadmin-only. Accounts that own
    tournaments or still have durable media are blocked so the existing
    tournament/media cleanup lifecycles remain the single source of truth.
    """

    ensure_superadmin_role(auth_session)

    if user_id == auth_session.user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить собственный аккаунт из админ-панели.",
        )

    # Deleting a user cascades into participant and roster rows. Acquire every
    # affected tournament first, in stable order, so each FK cascade and its
    # profile-access generation trigger follows the platform T -> User order.
    initial_tournament_ids = await tournament_ids_for_profile_access(
        db_session, user_id
    )
    locked_tournament_ids: set[str] = set()
    if initial_tournament_ids:
        locked_tournament_ids = {
            str(value)
            for value in await db_session.scalars(
                select(Tournament.id)
                .where(Tournament.id.in_(initial_tournament_ids))
                .order_by(Tournament.id.asc())
                .with_for_update()
            )
        }

    user = await db_session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    # A concurrent membership writer may have committed while the tournament
    # locks were being acquired. Recheck after the User lock; if it introduced
    # an unseen tournament, abort and let the caller retry under the full lock
    # set instead of taking User -> Tournament locks.
    current_tournament_ids = await tournament_ids_for_profile_access(
        db_session, user_id
    )
    if not current_tournament_ids.issubset(locked_tournament_ids):
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Удаление заблокировано конкурентным изменением. Повторите попытку.",
        )

    target_roles = await role_slugs_for_user(db_session, user.id)
    if "superadmin" in target_roles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Аккаунт superadmin нельзя удалить через эту операцию.",
        )

    expected_confirmation = (user.email or user.id).strip()
    if payload.confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Для подтверждения введите точно: {expected_confirmation}",
        )

    owned_tournaments = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .where(Tournament.organizer_user_id == user.id)
        )
        or 0
    )
    if owned_tournaments:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "user_owns_tournaments",
                "message": (
                    "Сначала удалите турниры этого пользователя. "
                    f"Найдено турниров: {owned_tournaments}."
                ),
                "tournaments": owned_tournaments,
            },
        )

    try:
        media_metadata_deleted = await purge_deleted_media_metadata(
            db_session,
            owner_user_ids=(user.id,),
        )
    except MediaCleanupRequired as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "user_media_cleanup_required",
                "message": (
                    "У пользователя есть активные медиа-файлы. Сначала удалите "
                    "аватар/баннер и дождитесь завершения очистки хранилища."
                ),
                "statuses": exc.status_counts,
            },
        ) from exc

    deleted_snapshot = {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
        "roles": target_roles,
    }

    try:
        result = await db_session.execute(delete(User).where(User.id == user.id))
        if int(result.rowcount or 0) != 1:
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Не удалось удалить пользователя из-за конкурентного изменения.",
            )

        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="admin.user.delete",
            subject_type="user",
            subject_id=user_id,
            payload={
                **deleted_snapshot,
                "media_metadata_deleted": media_metadata_deleted,
                "note": payload.note.strip(),
            },
        )
        await db_session.commit()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Удаление заблокировано связанными данными. "
                "Удалите зависимые объекты пользователя и повторите попытку."
            ),
        ) from exc

    invalidate_user_session_cache(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
