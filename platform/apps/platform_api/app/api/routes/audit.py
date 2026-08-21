from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import AuditLogResponse
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import AuditLog
from python_packages.platform_infra.security import get_authenticated_session

router = APIRouter()


@router.get("/me", response_model=list[AuditLogResponse])
async def my_audit_events(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AuditLogResponse]:
    rows = await db_session.scalars(
        select(AuditLog)
        .where(AuditLog.actor_user_id == auth_session.user.id)
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    return [
        AuditLogResponse(
            id=row.id,
            action=row.action,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            payload=row.payload,
            created_at=row.created_at,
        )
        for row in rows
    ]
