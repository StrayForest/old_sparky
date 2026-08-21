from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import Tournament


def normalize_tournament_name(value: str) -> str:
    return value.strip().lower()


async def lock_tournament_name(
    db_session: AsyncSession,
    *,
    name: str,
) -> str:
    normalized_name = normalize_tournament_name(name)
    await db_session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(normalized_name, 0)))
    )
    return normalized_name


async def public_tournament_name_exists(
    db_session: AsyncSession,
    *,
    normalized_name: str,
    exclude_tournament_id: str | None = None,
) -> bool:
    stmt = select(Tournament.id).where(
        Tournament.visibility == "public",
        func.lower(func.btrim(Tournament.name)) == normalized_name,
    )
    if exclude_tournament_id is not None:
        stmt = stmt.where(Tournament.id != exclude_tournament_id)
    return await db_session.scalar(stmt.limit(1)) is not None
