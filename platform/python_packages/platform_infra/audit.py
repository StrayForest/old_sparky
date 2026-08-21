from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import AuditLog


async def write_audit_log(
    db_session: AsyncSession,
    *,
    actor_user_id: str | None,
    action: str,
    subject_type: str,
    subject_id: str | None,
    payload: dict,
) -> None:
    db_session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
        )
    )
