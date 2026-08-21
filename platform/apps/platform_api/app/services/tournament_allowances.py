from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import Tournament

PRIVATE_TOURNAMENT_MONTHLY_LIMIT = 1


def calendar_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)
    return month_start, next_month_start


async def private_tournament_monthly_remaining(
    db_session: AsyncSession,
    *,
    organizer_user_id: str,
    now: datetime,
) -> int:
    month_start, next_month_start = calendar_month_bounds(now)
    existing_count = await db_session.scalar(
        select(func.count()).select_from(Tournament).where(
            Tournament.organizer_user_id == organizer_user_id,
            Tournament.visibility == "invite_only",
            Tournament.created_at >= month_start,
            Tournament.created_at < next_month_start,
        )
    )
    return max(0, PRIVATE_TOURNAMENT_MONTHLY_LIMIT - int(existing_count or 0))
