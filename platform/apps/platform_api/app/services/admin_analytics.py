from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, and_, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockProfile,
    ExternalIdentity,
    PlayerProfile,
    PreprodTestRun,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentMatch,
    TournamentParticipant,
    TournamentTeam,
    TournamentTeamMember,
    User,
)


ACTIVE_TOURNAMENT_STATUSES = ("registration_open", "registration_closed", "in_progress")
ACTIVE_PARTICIPANT_STATUSES = ("registered", "confirmed", "checked_in")
CURRENT_ASSIGNMENT_STATUSES = ("published", "locked")
ANALYTICS_DAYS = 14


def _attention_filter():
    has_unfinished_match = exists(
        select(1).where(
            TournamentMatch.tournament_id == Tournament.id,
            TournamentMatch.status.not_in(("completed", "cancelled")),
        )
    )
    has_locked_roster = exists(
        select(1).where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
    )
    return or_(
        Tournament.visibility == "invite_only",
        has_unfinished_match,
        and_(Tournament.status == "registration_open", has_locked_roster),
    )


def _bucket_rows(
    category: str,
    rows: list[tuple[str, int]],
    *,
    order: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    counts = {str(key): int(count) for key, count in rows if key is not None}
    keys = list(order) + sorted(key for key in counts if key not in order)
    total = sum(counts.values())
    return [
        {
            "key": key,
            "count": counts[key],
            "percentage": round((counts[key] / total) * 100, 1) if total else 0.0,
        }
        for key in keys
        if counts.get(key, 0) > 0
    ]


def _activity_point_rows(
    rows: list[tuple[str, date, int]],
    start_day: date,
    end_day: date,
) -> list[dict[str, Any]]:
    values: dict[date, dict[str, Any]] = defaultdict(
        lambda: {
            "date": None,
            "users": 0,
            "tournaments": 0,
            "participants": 0,
            "matches": 0,
            "audit_events": 0,
        }
    )
    current_day = start_day
    while current_day <= end_day:
        values[current_day]["date"] = current_day
        current_day += timedelta(days=1)
    for category, point_day, count in rows:
        if point_day in values:
            values[point_day][category] = int(count)
    return [values[key] for key in sorted(values)]


async def load_admin_analytics(db_session: AsyncSession) -> dict[str, Any]:
    """Load the complete protected operations snapshot with bounded SQL work.

    The endpoint is intentionally read-only. PostgreSQL remains the source of
    truth; no analytics rows, cache keys or workflow state are created here.
    The three queries are one scalar metric query, one grouped-distribution
    query and one bounded fourteen-day activity query.
    """

    now = datetime.now(UTC)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    start_day = (now - timedelta(days=ANALYTICS_DAYS - 1)).date()
    end_day = now.date()

    active_participant = TournamentParticipant.status.in_(ACTIVE_PARTICIPANT_STATUSES)
    assigned_participant = exists(
        select(1).where(
            TournamentTeamMember.tournament_id == TournamentParticipant.tournament_id,
            TournamentTeamMember.user_id == TournamentParticipant.user_id,
        )
    )
    active_participant_counts = (
        select(
            TournamentParticipant.tournament_id,
            func.count(TournamentParticipant.id).label("participant_count"),
        )
        .where(active_participant)
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )

    metric_row = (
        await db_session.execute(
            select(
                select(func.count()).select_from(User).scalar_subquery().label("users_total"),
                select(func.count()).select_from(User).where(User.status == "active").scalar_subquery().label("active_users"),
                select(func.count()).select_from(User).where(User.email_verified_at.is_not(None)).scalar_subquery().label("verified_users"),
                select(func.count(func.distinct(ExternalIdentity.user_id))).select_from(ExternalIdentity).scalar_subquery().label("steam_linked_users"),
                select(func.count()).select_from(PlayerProfile).scalar_subquery().label("player_profiles_total"),
                select(func.count()).select_from(DeadlockProfile).scalar_subquery().label("deadlock_profiles_total"),
                select(func.count()).select_from(Tournament).scalar_subquery().label("tournaments_total"),
                select(func.count()).select_from(Tournament).where(Tournament.status.in_(ACTIVE_TOURNAMENT_STATUSES)).scalar_subquery().label("active_tournaments"),
                select(func.count()).select_from(Tournament).where(Tournament.status == "completed").scalar_subquery().label("completed_tournaments"),
                select(func.count()).select_from(Tournament).where(_attention_filter()).scalar_subquery().label("tournaments_attention_total"),
                select(func.count()).select_from(Tournament).where(Tournament.visibility == "public").scalar_subquery().label("public_tournaments"),
                select(func.count()).select_from(Tournament).where(Tournament.visibility == "invite_only").scalar_subquery().label("invite_only_tournaments"),
                select(func.coalesce(func.avg(active_participant_counts.c.participant_count), 0.0)).select_from(active_participant_counts).scalar_subquery().label("average_active_participants_per_tournament"),
                select(func.count()).select_from(TournamentParticipant).scalar_subquery().label("participants_total"),
                select(func.count()).select_from(TournamentParticipant).where(active_participant).scalar_subquery().label("active_participants"),
                select(func.count()).select_from(TournamentParticipant).where(active_participant, assigned_participant).scalar_subquery().label("assigned_participants"),
                select(func.count()).select_from(TournamentParticipant).where(active_participant, ~assigned_participant).scalar_subquery().label("unassigned_participants"),
                select(func.count()).select_from(TournamentTeam).scalar_subquery().label("teams_total"),
                select(func.count()).select_from(TournamentTeamMember).scalar_subquery().label("rostered_members_total"),
                select(func.count()).select_from(TournamentDeadlockAssignmentRun).where(TournamentDeadlockAssignmentRun.status == "locked").scalar_subquery().label("locked_rosters"),
                select(func.count()).select_from(TournamentMatch).scalar_subquery().label("matches_total"),
                select(func.count()).select_from(TournamentMatch).where(TournamentMatch.status == "scheduled").scalar_subquery().label("scheduled_matches"),
                select(func.count()).select_from(TournamentMatch).where(TournamentMatch.status == "live").scalar_subquery().label("live_matches"),
                select(func.count()).select_from(TournamentMatch).where(TournamentMatch.status == "completed").scalar_subquery().label("completed_matches"),
                select(func.count()).select_from(TournamentMatch).where(TournamentMatch.status == "cancelled").scalar_subquery().label("cancelled_matches"),
                select(func.count()).select_from(TournamentDeadlockAssignmentRun).scalar_subquery().label("assignment_runs_total"),
                select(func.count()).select_from(TournamentDeadlockAssignmentRun).where(TournamentDeadlockAssignmentRun.status.in_(CURRENT_ASSIGNMENT_STATUSES)).scalar_subquery().label("current_assignment_runs"),
                select(func.count()).select_from(TournamentDeadlockReadyRound).scalar_subquery().label("ready_rounds_total"),
                select(func.count()).select_from(TournamentDeadlockReadyRound).where(TournamentDeadlockReadyRound.status == "active").scalar_subquery().label("active_ready_rounds"),
                select(func.count()).select_from(TournamentDeadlockCaptainRound).scalar_subquery().label("captain_rounds_total"),
                select(func.count()).select_from(TournamentDeadlockCaptainRound).where(TournamentDeadlockCaptainRound.status == "active").scalar_subquery().label("active_captain_rounds"),
                select(func.coalesce(func.sum(Tournament.automation_failure_count), 0)).select_from(Tournament).scalar_subquery().label("automation_failures_total"),
                select(func.count()).select_from(Tournament).where(Tournament.automation_failure_count > 0).scalar_subquery().label("tournaments_with_automation_failures"),
                select(func.count()).select_from(AuditLog).scalar_subquery().label("audit_events_total"),
                select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= cutoff_24h).scalar_subquery().label("audit_events_24h"),
                select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= cutoff_7d).scalar_subquery().label("audit_events_7d"),
                select(func.count()).select_from(PreprodTestRun).scalar_subquery().label("preprod_test_runs_total"),
                select(func.coalesce(func.sum(PreprodTestRun.created_users), 0)).where(PreprodTestRun.status != "cleaned").scalar_subquery().label("preprod_test_users_total"),
            )
        )
    ).mappings().one()

    distribution_statements = [
        select(literal("user_status").label("category"), User.status.label("key"), func.count().label("count")).group_by(User.status),
        select(literal("tournament_status").label("category"), Tournament.status.label("key"), func.count().label("count")).group_by(Tournament.status),
        select(literal("tournament_visibility").label("category"), Tournament.visibility.label("key"), func.count().label("count")).group_by(Tournament.visibility),
        select(literal("participant_status").label("category"), TournamentParticipant.status.label("key"), func.count().label("count")).group_by(TournamentParticipant.status),
        select(literal("match_status").label("category"), TournamentMatch.status.label("key"), func.count().label("count")).group_by(TournamentMatch.status),
        select(literal("assignment_status").label("category"), TournamentDeadlockAssignmentRun.status.label("key"), func.count().label("count")).group_by(TournamentDeadlockAssignmentRun.status),
        select(literal("ready_round_status").label("category"), TournamentDeadlockReadyRound.status.label("key"), func.count().label("count")).group_by(TournamentDeadlockReadyRound.status),
        select(literal("captain_round_status").label("category"), TournamentDeadlockCaptainRound.status.label("key"), func.count().label("count")).group_by(TournamentDeadlockCaptainRound.status),
        select(literal("rank").label("category"), DeadlockProfile.rank.label("key"), func.count().label("count")).group_by(DeadlockProfile.rank),
        select(
            literal("active_participant_rank").label("category"),
            DeadlockProfile.rank.label("key"),
            func.count().label("count"),
        )
        .select_from(TournamentParticipant)
        .join(DeadlockProfile, DeadlockProfile.user_id == TournamentParticipant.user_id)
        .where(active_participant)
        .group_by(DeadlockProfile.rank),
    ]
    distribution_rows = (
        await db_session.execute(union_all(*distribution_statements))
    ).all()
    distributions: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for category, key, count in distribution_rows:
        distributions[str(category)].append((str(key), int(count)))

    activity_statements = []
    for category, model in (
        ("users", User),
        ("tournaments", Tournament),
        ("participants", TournamentParticipant),
        ("matches", TournamentMatch),
        ("audit_events", AuditLog),
    ):
        day_expression = cast(func.date_trunc("day", model.created_at), Date)
        activity_statements.append(
            select(
                literal(category).label("category"),
                day_expression.label("day"),
                func.count().label("count"),
            )
            .where(model.created_at >= datetime.combine(start_day, datetime.min.time(), tzinfo=UTC))
            .group_by(day_expression)
        )
    activity_rows = (
        await db_session.execute(union_all(*activity_statements))
    ).all()

    def metric(name: str) -> int:
        return int(metric_row[name] or 0)

    active_participants = metric("active_participants")
    participants_with_profile = sum(
        count
        for key, count in distributions.get("active_participant_rank", [])
        if key
    )

    return {
        "generated_at": now,
        "users_total": metric("users_total"),
        "active_users": metric("active_users"),
        "verified_users": metric("verified_users"),
        "steam_linked_users": metric("steam_linked_users"),
        "player_profiles_total": metric("player_profiles_total"),
        "deadlock_profiles_total": metric("deadlock_profiles_total"),
        "tournaments_total": metric("tournaments_total"),
        "active_tournaments": metric("active_tournaments"),
        "completed_tournaments": metric("completed_tournaments"),
        "tournaments_attention_total": metric("tournaments_attention_total"),
        "public_tournaments": metric("public_tournaments"),
        "invite_only_tournaments": metric("invite_only_tournaments"),
        "average_active_participants_per_tournament": round(float(metric_row["average_active_participants_per_tournament"] or 0), 1),
        "participants_total": metric("participants_total"),
        "active_participants": active_participants,
        "assigned_participants": metric("assigned_participants"),
        "unassigned_participants": metric("unassigned_participants"),
        "participant_profile_coverage_percent": round((participants_with_profile / active_participants) * 100, 1) if active_participants else 0.0,
        "teams_total": metric("teams_total"),
        "rostered_members_total": metric("rostered_members_total"),
        "locked_rosters": metric("locked_rosters"),
        "matches_total": metric("matches_total"),
        "scheduled_matches": metric("scheduled_matches"),
        "live_matches": metric("live_matches"),
        "completed_matches": metric("completed_matches"),
        "cancelled_matches": metric("cancelled_matches"),
        "assignment_runs_total": metric("assignment_runs_total"),
        "current_assignment_runs": metric("current_assignment_runs"),
        "ready_rounds_total": metric("ready_rounds_total"),
        "active_ready_rounds": metric("active_ready_rounds"),
        "captain_rounds_total": metric("captain_rounds_total"),
        "active_captain_rounds": metric("active_captain_rounds"),
        "automation_failures_total": metric("automation_failures_total"),
        "tournaments_with_automation_failures": metric("tournaments_with_automation_failures"),
        "audit_events_total": metric("audit_events_total"),
        "audit_events_24h": metric("audit_events_24h"),
        "audit_events_7d": metric("audit_events_7d"),
        "preprod_test_runs_total": metric("preprod_test_runs_total"),
        "preprod_test_users_total": metric("preprod_test_users_total"),
        "user_status_distribution": _bucket_rows("user_status", distributions["user_status"], order=("active", "disabled")),
        "tournament_status_distribution": _bucket_rows("tournament_status", distributions["tournament_status"], order=ACTIVE_TOURNAMENT_STATUSES + ("completed", "cancelled")),
        "tournament_visibility_distribution": _bucket_rows("tournament_visibility", distributions["tournament_visibility"], order=("public", "invite_only")),
        "participant_status_distribution": _bucket_rows("participant_status", distributions["participant_status"], order=ACTIVE_PARTICIPANT_STATUSES + ("withdrawn", "disqualified")),
        "match_status_distribution": _bucket_rows("match_status", distributions["match_status"], order=("scheduled", "live", "completed", "cancelled")),
        "assignment_status_distribution": _bucket_rows("assignment_status", distributions["assignment_status"], order=("generated", "published", "locked", "superseded")),
        "ready_round_status_distribution": _bucket_rows("ready_round_status", distributions["ready_round_status"], order=("active", "closed", "stopped")),
        "captain_round_status_distribution": _bucket_rows("captain_round_status", distributions["captain_round_status"], order=("active", "closed", "finalized")),
        "rank_distribution": _bucket_rows("rank", distributions["rank"], order=tuple(RANKS)),
        "active_participant_rank_distribution": _bucket_rows("active_participant_rank", distributions["active_participant_rank"], order=tuple(RANKS)),
        "activity": _activity_point_rows(
            [(str(category), point_day, int(count)) for category, point_day, count in activity_rows],
            start_day,
            end_day,
        ),
    }
