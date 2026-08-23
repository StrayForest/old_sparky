from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one exact match, found {count}")
    return text.replace(old, new, 1)


def sub_once(text: str, pattern: str, replacement: str, *, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one regex match, found {count}")
    return updated


WORKFLOW = "platform/apps/platform_api/app/services/tournament_workflow.py"
workflow = read(WORKFLOW)
workflow = replace_once(
    workflow,
    "    AutoAssignmentRunFreshness,\n",
    "    AutoAssignmentRunFreshness,\n    AutoAssignmentRunWorkflowError,\n",
    label="workflow assignment error import",
)
workflow = sub_once(
    workflow,
    r"async def prune_participant_from_active_ready_round\(.*?(?=async def prune_participant_from_active_captain_round)",
    '''async def prune_participant_from_active_ready_round(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    user_id: str,
    actor_user_id: str,
    now: datetime,
    participant_status: str,
) -> TournamentDeadlockReadyRound | None:
    active_round, latest_round = await deadlock_ready_state_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    round_row = active_round or latest_round
    if round_row is None:
        return None

    vote_rows = (
        await db_session.scalars(
            select(TournamentDeadlockReadyVote).where(
                TournamentDeadlockReadyVote.round_id == round_row.id
            )
        )
    ).all()
    round_state = ReadyCheckRoundState.active(
        round_id=round_row.id,
        eligible_user_ids=list(round_row.eligible_user_ids or []),
        votes=[
            {"user_id": row.user_id, "choice": row.choice}
            for row in vote_rows
        ],
    )
    if round_row.status != "active":
        round_state = round_state.close(status=round_row.status)
    next_state = round_state.exclude_user(user_id)
    if next_state == round_state:
        return None

    round_row.status = next_state.status
    round_row.eligible_user_ids = list(next_state.eligible_user_ids)
    if next_state.status != "active":
        round_row.closed_at = round_row.closed_at or now
    await db_session.execute(
        delete(TournamentDeadlockReadyVote).where(
            TournamentDeadlockReadyVote.round_id == round_row.id,
            TournamentDeadlockReadyVote.user_id == user_id,
        )
    )
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action="tournament.deadlock.ready_check.exclude_participant",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(round_row.id),
        payload={
            "tournament_slug": tournament.slug,
            "user_id": user_id,
            "participant_status": participant_status,
            "round_status": round_row.status,
            "eligible_participant_count": len(round_row.eligible_user_ids or []),
        },
    )
    return round_row

''',
    label="ready-round reconciliation",
)
workflow = replace_once(
    workflow,
    "\nasync def deadlock_auto_assignment_state_runs_for_tournament(\n",
    '''
async def supersede_published_deadlock_assignment_run_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    replacement_run_id: str | None = None,
) -> TournamentDeadlockAssignmentRun | None:
    current_published = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament_id,
    )
    if current_published is None or current_published.id == replacement_run_id:
        return current_published
    if current_published.status == "locked":
        raise TournamentWorkflowError(
            "The currently published roster is locked and cannot be replaced."
        )
    try:
        current_published.status = transition_auto_assignment_run_status(
            current_published.status,
            "superseded",
        )
    except AutoAssignmentRunWorkflowError as exc:
        raise TournamentWorkflowError(str(exc)) from exc
    return current_published


async def deadlock_auto_assignment_state_runs_for_tournament(
''',
    label="assignment supersede helper",
)
write(WORKFLOW, workflow)


AUTOMATION = "platform/apps/platform_api/app/services/deadlock_automation.py"
automation = read(AUTOMATION)
automation = replace_once(
    automation,
    "    reconcile_finalized_captain_round_for_availability,\n",
    "    reconcile_finalized_captain_round_for_availability,\n    supersede_published_deadlock_assignment_run_for_tournament,\n",
    label="automation supersede import",
)
automation = replace_once(
    automation,
    '''    changed = False
    if run_row.status == "generated":
        try:
            run_row.status = transition_auto_assignment_run_status(
''',
    '''    changed = False
    if run_row.status == "generated":
        await supersede_published_deadlock_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
            replacement_run_id=run_row.id,
        )
        try:
            run_row.status = transition_auto_assignment_run_status(
''',
    label="automation supersede before publish",
)
write(AUTOMATION, automation)


ADMIN = "platform/apps/platform_api/app/api/routes/admin.py"
admin = read(ADMIN)
admin = replace_once(
    admin,
    "from apps.platform_api.app.services.player_commitments import (\n",
    "from apps.platform_api.app.services.tournament_workflow import (\n    supersede_published_deadlock_assignment_run_for_tournament,\n)\nfrom apps.platform_api.app.services.player_commitments import (\n",
    label="admin supersede import",
)
admin = replace_once(
    admin,
    "PREPROD_CLEANUP_CHUNK_SIZE = 10_000\n",
    "PREPROD_CLEANUP_CHUNK_SIZE = 10_000\nINACTIVE_PARTICIPANT_STATUSES = (\"withdrawn\", \"disqualified\")\n",
    label="admin inactive statuses",
)
admin = replace_once(
    admin,
    '''    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
    )
''',
    '''    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
    ).where(
        TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES)
    )
''',
    label="admin active participant counts",
)
admin = replace_once(
    admin,
    '''            if int(locked_roster_count or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Registration cannot be opened while the Deadlock roster is locked.",
                )
            schedule_values = (
''',
    '''            if int(locked_roster_count or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Registration cannot be opened while the Deadlock roster is locked.",
                )
            await supersede_published_deadlock_assignment_run_for_tournament(
                db_session,
                tournament_id=tournament.id,
            )
            schedule_values = (
''',
    label="admin reopen supersedes published roster",
)
write(ADMIN, admin)


TOURNAMENTS = "platform/apps/platform_api/app/api/routes/tournaments.py"
tournaments = read(TOURNAMENTS)
tournaments = replace_once(
    tournaments,
    "    deadlock_published_auto_assignment_run_for_tournament,\n",
    "    deadlock_published_auto_assignment_run_for_tournament,\n    supersede_published_deadlock_assignment_run_for_tournament,\n",
    label="route supersede import",
)
tournaments = replace_once(
    tournaments,
    '''from apps.platform_api.app.services.tournament_runtime_cache import (
    register_tournament_runtime_cache_invalidator,
)
''',
    '''from apps.platform_api.app.services.tournament_runtime_cache import (
    invalidate_tournament_runtime_caches,
    register_tournament_runtime_cache_invalidator,
)
''',
    label="route runtime invalidator import",
)
tournaments = replace_once(
    tournaments,
    '''    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
    )
''',
    '''    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
    ).where(
        TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES)
    )
''',
    label="route active participant counts",
)
tournaments = replace_once(
    tournaments,
    '''    participant_count_for_sort = (
        select(func.count(TournamentParticipant.id))
        .where(TournamentParticipant.tournament_id == Tournament.id)
        .correlate(Tournament)
        .scalar_subquery()
    )
''',
    '''    participant_count_for_sort = (
        select(func.count(TournamentParticipant.id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
''',
    label="public participant sort count",
)
tournaments = sub_once(
    tournaments,
    r"async def participant_count_for_tournament\((?P<sig>.*?)\) -> int:\n.*?(?=\n\n(?:async )?def )",
    '''async def participant_count_for_tournament(\g<sig>) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(TournamentParticipant)
            .where(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
        )
        or 0
    )''',
    label="single tournament active count",
)

def replace_function_block(text: str, start_pattern: str, end_pattern: str, replacement: str, label: str) -> str:
    pattern = start_pattern + r".*?(?=" + end_pattern + r")"
    return sub_once(text, pattern, replacement, label=label)


tournaments = replace_function_block(
    tournaments,
    r'@router\.delete\("/\{slug\}/participants/\{participant_id\}".*?\nasync def organizer_remove_participant\(',
    r'@router\.patch\(',
    '''@router.delete("/{slug}/participants/{participant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def organizer_remove_participant(
    slug: str,
    participant_id: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_organizer_can_moderate_participants(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant = await get_participant_or_404(
        db_session,
        tournament_id=tournament.id,
        participant_id=participant_id,
    )
    await db_session.execute(
        select(User.id).where(User.id == participant.user_id).with_for_update()
    )
    previous_status = participant.status
    try:
        participant.status = transition_participant_status(
            participant.status,
            "disqualified",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant.moderation_note = "Removed by organizer."
    participant.moderated_at = auth_session.now
    participant.moderated_by_user_id = auth_session.user.id
    if (
        is_solo_tournament_format(tournament.format_slug)
        and not participant_status_is_inactive(previous_status)
    ):
        await prune_participant_from_active_ready_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await prune_participant_from_active_captain_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            user_ids=[participant.user_id],
            released_at=auth_session.now,
            release_reason="participant_disqualified",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.participant.manage_remove",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={
            "tournament_slug": tournament.slug,
            "user_id": participant.user_id,
            "from_status": previous_status,
            "to_status": participant.status,
            "retained_record": True,
        },
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


''',
    "canonical organizer removal handler",
)

moderation_match = re.search(
    r'@router\.patch\(\n    "/\{slug\}/participants/\{participant_id\}/moderation".*?(?=\n\n@router\.get\("/\{slug\}/invites")',
    tournaments,
    flags=re.S,
)
if moderation_match is None:
    raise RuntimeError("moderation cache invalidation: function block missing")
moderation_block = moderation_match.group(0)
moderation_block = replace_once(
    moderation_block,
    "    await db_session.commit()\n    _invalidate_participant_page_cache(tournament.id)\n",
    "    await db_session.commit()\n    invalidate_tournament_runtime_caches(tournament.id)\n",
    label="moderation cache invalidation",
)
tournaments = tournaments[:moderation_match.start()] + moderation_block + tournaments[moderation_match.end():]

# Historical yes votes must not block self-leave; reconciliation removes the
# current/latest round membership and vote before the retained registration row
# is physically removed by the self-service endpoint.
tournaments = replace_function_block(
    tournaments,
    r'@router\.delete\("/\{slug\}/join".*?\nasync def leave_tournament\(',
    r'@router\.get\("/\{slug\}/profiles/\{user_id\}"',
    '''@router.delete("/{slug}/join", status_code=status.HTTP_204_NO_CONTENT)
async def leave_tournament(
    slug: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    if not can_self_leave_tournament(tournament.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament registrations can no longer be changed.",
        )
    try:
        ensure_deadlock_registration_changes_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant = await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament.id,
            TournamentParticipant.user_id == auth_session.user.id,
        )
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not registered in this tournament.",
        )
    if participant.status in {"confirmed", "checked_in"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
        )
    active_ready_yes_vote_id = await db_session.scalar(
        select(TournamentDeadlockReadyVote.id)
        .join(
            TournamentDeadlockReadyRound,
            TournamentDeadlockReadyVote.round_id == TournamentDeadlockReadyRound.id,
        )
        .where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyRound.status == "active",
            TournamentDeadlockReadyVote.user_id == auth_session.user.id,
            TournamentDeadlockReadyVote.choice == "yes",
        )
    )
    if active_ready_yes_vote_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
        )

    await db_session.execute(
        select(User.id).where(User.id == participant.user_id).with_for_update()
    )
    if is_solo_tournament_format(tournament.format_slug):
        await prune_participant_from_active_ready_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status="withdrawn",
        )
        await prune_participant_from_active_captain_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status="withdrawn",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.leave",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={"tournament_slug": tournament.slug},
    )
    await db_session.execute(
        delete(TournamentParticipant).where(TournamentParticipant.id == participant.id)
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


''',
    "self-leave reconciliation",
)

# Current captain selection is automatic and creates a finalized round. Retain
# the old URLs only as explicit compatibility tombstones instead of unreachable
# active-round handlers.
tournaments = replace_function_block(
    tournaments,
    r'@router\.post\("/\{slug\}/deadlock/captain-round/respond".*?\nasync def respond_deadlock_captain_round\(',
    r'@router\.post\("/\{slug\}/deadlock/captain-round/close"',
    '''@router.post(
    "/{slug}/deadlock/captain-round/respond",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def respond_deadlock_captain_round(
    slug: str,
    payload: TournamentDeadlockCaptainRoundRespondRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Captain offer responses are retired; captain selection is automatic.",
    )


''',
    "captain respond tombstone",
)
tournaments = replace_function_block(
    tournaments,
    r'@router\.post\("/\{slug\}/deadlock/captain-round/close".*?\nasync def close_deadlock_captain_round\(',
    r'@router\.post\("/\{slug\}/deadlock/captain-round/finalize"',
    '''@router.post(
    "/{slug}/deadlock/captain-round/close",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def close_deadlock_captain_round(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Manual captain-round close is retired; captain selection is automatic.",
    )


''',
    "captain close tombstone",
)
tournaments = replace_function_block(
    tournaments,
    r'@router\.post\("/\{slug\}/deadlock/captain-round/finalize".*?\nasync def finalize_deadlock_captain_round\(',
    r'@router\.get\("/\{slug\}/deadlock/auto-assignment"',
    '''@router.post(
    "/{slug}/deadlock/captain-round/finalize",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def finalize_deadlock_captain_round(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Manual captain-round finalization is retired; captain selection is automatic.",
    )


''',
    "captain finalize tombstone",
)

old_publish = '''    current_published = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if current_published is not None and current_published.id != run_row.id and current_published.status == "locked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The currently published roster is locked and cannot be replaced.",
        )

    if current_published is not None and current_published.id != run_row.id and current_published.status == "published":
        try:
            current_published.status = transition_auto_assignment_run_status(
                current_published.status,
                "superseded",
            )
        except AutoAssignmentRunWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
'''
new_publish = '''    try:
        current_published = await supersede_published_deadlock_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
            replacement_run_id=run_row.id,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
'''
tournaments = replace_once(
    tournaments,
    old_publish,
    new_publish,
    label="manual publish shared supersede",
)
write(TOURNAMENTS, tournaments)


TEST = ROOT / "platform/tests/test_platform_backend_audit_remediation.py"
TEST.write_text(
    '''from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import tournament_workflow as workflow
from python_packages.platform_domain.tournaments import TournamentWorkflowError


class PlatformBackendAuditRemediationTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_assignment_is_superseded_before_replacement(self) -> None:
        published = SimpleNamespace(id="old-run", status="published")
        with patch.object(
            workflow,
            "deadlock_published_auto_assignment_run_for_tournament",
            AsyncMock(return_value=published),
        ):
            result = await workflow.supersede_published_deadlock_assignment_run_for_tournament(
                Mock(),
                tournament_id="tournament",
                replacement_run_id="new-run",
            )
        self.assertIs(result, published)
        self.assertEqual(published.status, "superseded")

    async def test_locked_assignment_cannot_be_superseded(self) -> None:
        locked = SimpleNamespace(id="locked-run", status="locked")
        with patch.object(
            workflow,
            "deadlock_published_auto_assignment_run_for_tournament",
            AsyncMock(return_value=locked),
        ):
            with self.assertRaises(TournamentWorkflowError):
                await workflow.supersede_published_deadlock_assignment_run_for_tournament(
                    Mock(),
                    tournament_id="tournament",
                    replacement_run_id="new-run",
                )

    async def test_closed_ready_round_exclusion_removes_vote_and_eligibility(self) -> None:
        closed_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        round_row = SimpleNamespace(
            id=77,
            status="closed",
            eligible_user_ids=["keep", "remove"],
            closed_at=closed_at,
        )
        vote = SimpleNamespace(user_id="remove", choice="yes")
        scalar_result = SimpleNamespace(all=lambda: [vote])
        db_session = Mock()
        db_session.scalars = AsyncMock(return_value=scalar_result)
        db_session.execute = AsyncMock()
        tournament = SimpleNamespace(id="tournament", slug="audit-remediation")
        with (
            patch.object(
                workflow,
                "deadlock_ready_state_round_for_tournament",
                AsyncMock(return_value=(None, round_row)),
            ),
            patch.object(workflow, "write_audit_log", AsyncMock()),
        ):
            result = await workflow.prune_participant_from_active_ready_round(
                db_session,
                tournament=tournament,
                user_id="remove",
                actor_user_id="organizer",
                now=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
                participant_status="disqualified",
            )
        self.assertIs(result, round_row)
        self.assertEqual(round_row.status, "closed")
        self.assertEqual(round_row.closed_at, closed_at)
        self.assertEqual(round_row.eligible_user_ids, ["keep"])
        db_session.execute.assert_awaited_once()

    def test_retired_captain_endpoints_are_hidden_from_openapi(self) -> None:
        schema = create_app().openapi()
        paths = schema["paths"]
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            paths,
        )
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/close",
            paths,
        )
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/finalize",
            paths,
        )


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)
