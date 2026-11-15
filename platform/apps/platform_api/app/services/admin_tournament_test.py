"""Business rules for admin-owned manual tournament test fixtures."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    AdminTournamentTestBlockerResponse,
    AdminTournamentTestCapabilitiesResponse,
    AdminTournamentTestCountsResponse,
    AdminTournamentTestResponse,
)
from python_packages.platform_domain.deadlock.captain_round import TEAM_COUNT_CHOICES
from python_packages.platform_infra.db import Base
from python_packages.platform_infra.models import (
    AdminTournamentTestFixture,
    AdminTournamentTestPlayer,
    DeadlockProfile,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentInvite,
    TournamentParticipant,
    TournamentTeamMember,
)

MAX_FAKE_PLAYERS = 50
PLAYERS_PER_TEAM = 7


async def active_human_user_ids(db: AsyncSession, tournament_id: str) -> list[str]:
    return list((await db.scalars(
        select(TournamentParticipant.user_id)
        .where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
            ~exists(select(1).where(AdminTournamentTestPlayer.user_id == TournamentParticipant.user_id)),
        )
        .order_by(TournamentParticipant.user_id)
    )).all())


async def fake_users_have_external_references(
    db: AsyncSession,
    fake_ids: list[str],
    tournament_id: str,
) -> bool:
    if not fake_ids:
        return False
    owned_profile_tables = {
        "player_profiles", "deadlock_profiles", "deadlock_dream_slots",
        "admin_tournament_test_players",
    }
    parent_tournament_tables = {
        "tournament_deadlock_ready_votes": ("tournament_deadlock_ready_rounds", "round_id"),
        "tournament_deadlock_captain_entries": ("tournament_deadlock_captain_rounds", "round_id"),
    }
    for table in Base.metadata.tables.values():
        if table.name in owned_profile_tables:
            continue
        for column in table.columns:
            if not any(fk.target_fullname == "platform.users.id" for fk in column.foreign_keys):
                continue
            predicates = [column.in_(fake_ids)]
            if "tournament_id" in table.c:
                predicates.append(table.c.tournament_id != tournament_id)
            elif table.name in parent_tournament_tables:
                parent_name, parent_fk = parent_tournament_tables[table.name]
                parent = Base.metadata.tables[f"platform.{parent_name}"]
                predicates.append(
                    table.c[parent_fk].in_(select(parent.c.id).where(parent.c.tournament_id != tournament_id))
                )
            if bool(await db.scalar(select(exists().where(and_(*predicates))))):
                return True
    return False


async def summarize_admin_tournament_test(
    db: AsyncSession,
    fixture: AdminTournamentTestFixture,
    tournament: Tournament,
) -> AdminTournamentTestResponse:
    fake_ids = list((await db.scalars(
        select(AdminTournamentTestPlayer.user_id).where(AdminTournamentTestPlayer.fixture_id == fixture.id)
    )).all())
    fake_total = len(fake_ids)
    fake_ready = int(await db.scalar(
        select(func.count()).select_from(AdminTournamentTestPlayer)
        .where(AdminTournamentTestPlayer.fixture_id == fixture.id, AdminTournamentTestPlayer.prepared_ready.is_(True))
    ) or 0)
    human_ids = await active_human_user_ids(db, tournament.id)
    missing_profile_count = int(await db.scalar(
        select(func.count()).select_from(TournamentParticipant)
        .where(
            TournamentParticipant.tournament_id == tournament.id,
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
            ~exists(select(1).where(AdminTournamentTestPlayer.user_id == TournamentParticipant.user_id)),
            ~exists(select(1).where(DeadlockProfile.user_id == TournamentParticipant.user_id)),
        )
    ) or 0)
    latest_round = await db.scalar(
        select(TournamentDeadlockReadyRound)
        .where(TournamentDeadlockReadyRound.tournament_id == tournament.id)
        .order_by(TournamentDeadlockReadyRound.created_at.desc(), TournamentDeadlockReadyRound.id.desc())
        .limit(1)
    )
    human_yes = 0
    if latest_round is not None and human_ids:
        human_yes = int(await db.scalar(
            select(func.count()).select_from(TournamentDeadlockReadyVote)
            .where(
                TournamentDeadlockReadyVote.round_id == latest_round.id,
                TournamentDeadlockReadyVote.user_id.in_(human_ids),
                TournamentDeadlockReadyVote.choice == "yes",
            )
        ) or 0)
    minimum_teams_count = next((count for count in TEAM_COUNT_CHOICES if count * PLAYERS_PER_TEAM >= max(1, len(human_ids))), 2)
    feasible_team_counts = [
        count for count in TEAM_COUNT_CHOICES
        if count >= minimum_teams_count and count * PLAYERS_PER_TEAM <= len(human_ids) + fake_ready
    ]
    teams_count = max(feasible_team_counts) if feasible_team_counts else minimum_teams_count
    required_slots = teams_count * PLAYERS_PER_TEAM
    filler_needed = max(0, required_slots - len(human_ids) - fake_ready)
    locked_run = await db.scalar(
        select(TournamentDeadlockAssignmentRun).where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        ).order_by(TournamentDeadlockAssignmentRun.created_at.desc()).limit(1)
    )
    teams_locked = int(await db.scalar(
        select(func.count(func.distinct(TournamentTeamMember.team_id))).where(
            TournamentTeamMember.tournament_id == tournament.id
        )
    ) or 0)
    stage = (
        "teams_locked" if locked_run is not None else
        "ready_check_active" if latest_round is not None and latest_round.status == "active" else
        "ready_check_closed" if latest_round is not None and latest_round.status == "closed" else
        "registration_open" if tournament.status == "registration_open" else
        "registration_closed"
    )
    blockers = []
    if stage == "ready_check_closed" and filler_needed:
        blockers.append(AdminTournamentTestBlockerResponse(
            code="insufficient_fillers", required=required_slots,
            available=len(human_ids) + fake_ready, filler_needed=filler_needed,
        ))
    if stage == "ready_check_closed" and missing_profile_count:
        blockers.append(AdminTournamentTestBlockerResponse(code="human_profiles_missing"))
    fake_capacity_remaining = MAX_FAKE_PLAYERS - fake_total
    unfillable_by_fake_cap = filler_needed > fake_capacity_remaining
    if stage == "ready_check_closed" and unfillable_by_fake_cap:
        blockers.append(AdminTournamentTestBlockerResponse(
            code="too_many_humans", required=required_slots,
            available=len(human_ids) + fake_ready, filler_needed=filler_needed,
        ))
    cleanup_blocked = await fake_users_have_external_references(db, fake_ids, tournament.id)
    if cleanup_blocked:
        blockers.append(AdminTournamentTestBlockerResponse(code="fake_users_have_external_references"))
    code = await db.scalar(
        select(TournamentInvite.code).where(TournamentInvite.tournament_id == tournament.id).limit(1)
    )
    return AdminTournamentTestResponse(
        id=fixture.id, slug=tournament.slug, name=tournament.name,
        visibility=tournament.visibility, status=tournament.status, stage=stage,
        join_code=code or "", counts=AdminTournamentTestCountsResponse(
            human_active=len(human_ids), human_ready_yes=human_yes,
            fake_total=fake_total, fake_ready_yes=fake_ready,
            required_slots=required_slots, teams_count=teams_count,
            teams_locked=teams_locked,
            assigned_humans=(len(human_ids) if locked_run else 0),
            filler_needed=filler_needed,
        ),
        capabilities=AdminTournamentTestCapabilitiesResponse(
            can_add_fake_players=(stage == "ready_check_closed" and fake_capacity_remaining > 0 and not unfillable_by_fake_cap),
            can_start_ready_check=stage in ("registration_open", "registration_closed") and bool(human_ids or fake_total),
            can_close_ready_check=stage == "ready_check_active",
            can_generate_teams=(
                stage == "ready_check_closed"
                and filler_needed == 0
                and missing_profile_count == 0
            ),
            can_delete=not cleanup_blocked,
        ),
        blockers=blockers,
        cleanup_blocked_reason="fake_users_have_external_references" if cleanup_blocked else None,
    )
