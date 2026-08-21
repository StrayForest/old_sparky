from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    PlatformStatsOverviewResponse,
    StatsRankDistributionItemResponse,
)
from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    DeadlockProfile,
    Tournament,
    TournamentMatch,
    TournamentParticipant,
)

router = APIRouter()

ACTIVE_UPCOMING_TOURNAMENT_STATUSES = (
    "registration_open",
    "registration_closed",
    "in_progress",
)
ACTIVE_PARTICIPANT_STATUSES = ("registered", "confirmed", "checked_in")


@router.get("/overview", response_model=PlatformStatsOverviewResponse)
async def stats_overview(
    db_session: AsyncSession = Depends(get_db_session),
) -> PlatformStatsOverviewResponse:
    metric_row = (
        await db_session.execute(
            select(
                select(func.count())
                .select_from(Tournament)
                .scalar_subquery()
                .label("total_tournaments"),
                select(func.count())
                .select_from(Tournament)
                .where(Tournament.status == "completed")
                .scalar_subquery()
                .label("completed_tournaments"),
                select(func.count())
                .select_from(Tournament)
                .where(Tournament.status.in_(ACTIVE_UPCOMING_TOURNAMENT_STATUSES))
                .scalar_subquery()
                .label("active_upcoming_tournaments"),
                select(func.count())
                .select_from(TournamentParticipant)
                .where(TournamentParticipant.status.in_(ACTIVE_PARTICIPANT_STATUSES))
                .scalar_subquery()
                .label("registered_participants"),
                select(func.count())
                .select_from(TournamentMatch)
                .where(TournamentMatch.status == "completed")
                .scalar_subquery()
                .label("completed_matches"),
                select(func.count())
                .select_from(DeadlockProfile)
                .scalar_subquery()
                .label("deadlock_profiles_total"),
                select(func.count())
                .select_from(TournamentParticipant)
                .join(DeadlockProfile, DeadlockProfile.user_id == TournamentParticipant.user_id)
                .where(TournamentParticipant.status.in_(ACTIVE_PARTICIPANT_STATUSES))
                .scalar_subquery()
                .label("registered_participants_with_deadlock_profile"),
            )
        )
    ).mappings().one()

    total_tournaments = int(metric_row["total_tournaments"])
    completed_tournaments = int(metric_row["completed_tournaments"])
    active_upcoming_tournaments = int(metric_row["active_upcoming_tournaments"])
    registered_participants = int(metric_row["registered_participants"])
    completed_matches = int(metric_row["completed_matches"])
    deadlock_profiles_total = int(metric_row["deadlock_profiles_total"])
    registered_participants_with_deadlock_profile = int(
        metric_row["registered_participants_with_deadlock_profile"]
    )
    coverage_percent = (
        round((registered_participants_with_deadlock_profile / registered_participants) * 100, 2)
        if registered_participants
        else 0.0
    )

    rank_rows = (
        await db_session.execute(
            select(DeadlockProfile.rank, func.count(DeadlockProfile.user_id))
            .group_by(DeadlockProfile.rank)
        )
    ).all()
    rank_counts = {str(rank): int(count) for rank, count in rank_rows}
    rank_distribution = [
        StatsRankDistributionItemResponse(rank=rank, count=rank_counts[rank])
        for rank in RANKS
        if rank_counts.get(rank, 0) > 0
    ]

    return PlatformStatsOverviewResponse(
        total_tournaments=total_tournaments,
        completed_tournaments=completed_tournaments,
        active_upcoming_tournaments=active_upcoming_tournaments,
        registered_participants=registered_participants,
        completed_matches=completed_matches,
        deadlock_profiles_total=deadlock_profiles_total,
        registered_participants_with_deadlock_profile=registered_participants_with_deadlock_profile,
        deadlock_profile_coverage_percent=coverage_percent,
        deadlock_rank_distribution=rank_distribution,
    )
