"""PostgreSQL-backed projection for tournament catalog cards."""

from __future__ import annotations

from collections.abc import Iterable
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import (
    PlayerProfile,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentListReadModel,
    TournamentParticipant,
    User,
)

logger = logging.getLogger(__name__)

INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")

_PROJECTION_COLUMNS = (
    "id",
    "slug",
    "name",
    "description",
    "cover_url",
    "banner_asset_id",
    "visibility",
    "status",
    "format_slug",
    "allowed_ranks",
    "max_participants",
    "registration_starts_at",
    "registration_closes_at",
    "ready_check_starts_at",
    "ready_check_ends_at",
    "captain_selection_starts_at",
    "starts_at",
    "match_format",
    "final_format",
    "captain_response_deadline_minutes",
    "teams_count",
    "automation_ready_check_started_at",
    "automation_ready_check_closed_at",
    "automation_captain_round_started_at",
    "automation_captain_round_finalized_at",
    "automation_assignment_generated_at",
    "automation_last_error",
    "automation_failure_count",
    "automation_retry_after",
    "organizer_user_id",
    "organizer_display_name",
    "organizer_avatar_asset_id",
    "participant_count",
    "has_locked_deadlock_roster",
    "bracket_revision",
    "created_at",
    "updated_at",
)


def _projection_source(
    *,
    tournament_id: str | None = None,
    organizer_user_id: str | None = None,
):
    participant_count = (
        select(func.count(TournamentParticipant.id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
    locked_roster = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .correlate(Tournament)
        .exists()
    )
    source = (
        select(
            Tournament.id.label("id"),
            Tournament.slug.label("slug"),
            Tournament.name.label("name"),
            Tournament.description.label("description"),
            Tournament.cover_url.label("cover_url"),
            Tournament.banner_asset_id.label("banner_asset_id"),
            Tournament.visibility.label("visibility"),
            Tournament.status.label("status"),
            Tournament.format_slug.label("format_slug"),
            Tournament.allowed_ranks.label("allowed_ranks"),
            Tournament.max_participants.label("max_participants"),
            Tournament.registration_starts_at.label("registration_starts_at"),
            Tournament.registration_closes_at.label("registration_closes_at"),
            Tournament.ready_check_starts_at.label("ready_check_starts_at"),
            Tournament.ready_check_ends_at.label("ready_check_ends_at"),
            Tournament.captain_selection_starts_at.label("captain_selection_starts_at"),
            Tournament.starts_at.label("starts_at"),
            Tournament.match_format.label("match_format"),
            Tournament.final_format.label("final_format"),
            Tournament.captain_response_deadline_minutes.label(
                "captain_response_deadline_minutes"
            ),
            Tournament.teams_count.label("teams_count"),
            Tournament.automation_ready_check_started_at.label(
                "automation_ready_check_started_at"
            ),
            Tournament.automation_ready_check_closed_at.label(
                "automation_ready_check_closed_at"
            ),
            Tournament.automation_captain_round_started_at.label(
                "automation_captain_round_started_at"
            ),
            Tournament.automation_captain_round_finalized_at.label(
                "automation_captain_round_finalized_at"
            ),
            Tournament.automation_assignment_generated_at.label(
                "automation_assignment_generated_at"
            ),
            Tournament.automation_last_error.label("automation_last_error"),
            Tournament.automation_failure_count.label("automation_failure_count"),
            Tournament.automation_retry_after.label("automation_retry_after"),
            Tournament.organizer_user_id.label("organizer_user_id"),
            User.display_name.label("organizer_display_name"),
            PlayerProfile.avatar_asset_id.label("organizer_avatar_asset_id"),
            func.coalesce(participant_count, 0).label("participant_count"),
            locked_roster.label("has_locked_deadlock_roster"),
            Tournament.bracket_revision.label("bracket_revision"),
            Tournament.created_at.label("created_at"),
            Tournament.updated_at.label("updated_at"),
        )
        .select_from(Tournament)
        .join(User, User.id == Tournament.organizer_user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == Tournament.organizer_user_id)
    )
    if tournament_id is not None:
        source = source.where(Tournament.id == str(tournament_id))
    if organizer_user_id is not None:
        source = source.where(Tournament.organizer_user_id == str(organizer_user_id))
    return source


async def _upsert_projection(
    db_session: AsyncSession,
    *,
    tournament_id: str | None = None,
    organizer_user_id: str | None = None,
) -> int:
    source = _projection_source(
        tournament_id=tournament_id,
        organizer_user_id=organizer_user_id,
    )
    statement = insert(TournamentListReadModel).from_select(
        list(_PROJECTION_COLUMNS),
        source,
    )
    excluded = statement.excluded
    update_values = {
        column: getattr(excluded, column)
        for column in _PROJECTION_COLUMNS
        if column != "id"
    }
    statement = statement.on_conflict_do_update(
        index_elements=[TournamentListReadModel.id],
        set_=update_values,
        # A post-commit refresh can race with a newer mutation. A committed
        # source timestamp prevents the older snapshot from replacing it.
        where=excluded.updated_at >= TournamentListReadModel.updated_at,
    )
    result = await db_session.execute(statement)
    return int(result.rowcount or 0)


async def refresh_tournament_list_read_model(
    tournament_id: str,
    *,
    db_session: AsyncSession | None = None,
) -> bool:
    """Refresh one card projection, or remove it when its source is gone."""

    owns_session = db_session is None
    if owns_session:
        db_session = session_factory()()
    assert db_session is not None
    try:
        source_exists = await db_session.scalar(
            select(Tournament.id).where(Tournament.id == str(tournament_id))
        )
        if source_exists is None:
            await db_session.execute(
                delete(TournamentListReadModel).where(
                    TournamentListReadModel.id == str(tournament_id)
                )
            )
            changed = False
        else:
            changed = bool(
                await _upsert_projection(
                    db_session,
                    tournament_id=str(tournament_id),
                )
            )
        if owns_session:
            await db_session.commit()
        return changed
    except Exception:
        if owns_session:
            await db_session.rollback()
        raise
    finally:
        if owns_session:
            await db_session.close()


async def refresh_tournament_list_read_models_for_organizer(
    organizer_user_id: str,
) -> int:
    """Refresh all cards owned by an organizer after profile metadata changes."""

    async with session_factory()() as db_session:
        changed = await _upsert_projection(
            db_session,
            organizer_user_id=str(organizer_user_id),
        )
        await db_session.commit()
        return changed


async def refresh_tournament_list_read_model_after_commit(
    tournament_id: str,
) -> bool:
    """Best-effort repair hook used after a committed tournament mutation."""

    try:
        return await refresh_tournament_list_read_model(str(tournament_id))
    except Exception:
        logger.exception(
            "Tournament catalog read-model refresh failed tournament_id=%s",
            tournament_id,
        )
        return False


async def refresh_tournament_list_read_models_for_organizer_after_commit(
    organizer_user_id: str,
) -> int:
    """Best-effort bulk repair hook for organizer profile mutations."""

    try:
        return await refresh_tournament_list_read_models_for_organizer(
            str(organizer_user_id)
        )
    except Exception:
        logger.exception(
            "Organizer tournament catalog read-model refresh failed organizer_user_id=%s",
            organizer_user_id,
        )
        return 0


async def refresh_tournament_list_read_models(
    tournament_ids: Iterable[str],
) -> int:
    """Repair a bounded set of cards in one transaction for operator tooling."""

    normalized_ids = tuple(dict.fromkeys(str(item) for item in tournament_ids))
    if not normalized_ids:
        return 0
    async with session_factory()() as db_session:
        changed = 0
        for tournament_id in normalized_ids:
            changed += int(
                await refresh_tournament_list_read_model(
                    tournament_id,
                    db_session=db_session,
                )
            )
        await db_session.commit()
        return changed
