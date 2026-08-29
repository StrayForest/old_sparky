from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.api.routes import profiles, tournaments
from apps.platform_api.app.services import deadlock_automation, player_commitments
from apps.platform_api.app.services.mutation_idempotency import (
    mutation_payload_fingerprint,
    request_idempotency_key,
)
from python_packages.platform_infra import security
from python_packages.platform_infra.models import (
    TournamentDeadlockReadyVoteCountShard,
    TournamentMatch,
    TournamentParticipant,
)


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class PersistenceConcurrencyRemediationTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_read_auth_does_not_open_last_seen_transaction(self) -> None:
        request = Mock()
        db_session = Mock()
        resolved = SimpleNamespace()
        with patch.object(
            security,
            "_resolve_optional_authenticated_session",
            AsyncMock(return_value=resolved),
        ) as resolve:
            result = await security.get_optional_authenticated_session(request, db_session)

        self.assertIs(result, resolved)
        resolve.assert_awaited_once_with(
            request,
            db_session,
            touch_session=False,
        )

    async def test_ready_vote_auth_skips_roles_and_last_seen_touch(self) -> None:
        request = Mock()
        db_session = Mock()
        resolved = SimpleNamespace()
        with patch.object(
            security,
            "_get_authenticated_session",
            AsyncMock(return_value=resolved),
        ) as resolve:
            result = await security.get_authenticated_session_for_ready_vote(
                request,
                db_session,
            )

        self.assertIs(result, resolved)
        resolve.assert_awaited_once_with(
            request,
            db_session,
            load_roles=False,
            touch_session=False,
        )

    async def test_tournament_policy_auth_skips_ready_vote_route(self) -> None:
        request = Mock()
        request.method = "POST"
        request.scope = {"route": SimpleNamespace(path="/{slug}/deadlock/ready-check/vote")}

        result = await security.get_optional_authenticated_session_for_tournament_policy(
            request,
            Mock(),
        )

        self.assertIsNone(result)

    async def test_auth_touch_uses_isolated_session_transaction(self) -> None:
        touch_session = Mock()
        touch_session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
        touch_session.commit = AsyncMock()
        factory = Mock(return_value=_AsyncContext(touch_session))
        auth_session = SimpleNamespace(
            now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            session=SimpleNamespace(
                id="session",
                last_seen_at=datetime(2026, 8, 23, 11, 0, tzinfo=UTC),
            ),
        )
        settings = SimpleNamespace(platform_session_touch_interval_seconds=300)
        with (
            patch.object(security, "get_settings", return_value=settings),
            patch.object(security, "session_factory", return_value=factory),
        ):
            await security._touch_authenticated_session(auth_session)
        touch_session.commit.assert_awaited_once()
        self.assertEqual(auth_session.session.last_seen_at, auth_session.now)

    async def test_profile_owner_lock_uses_for_update_query(self) -> None:
        db_session = Mock()
        db_session.scalar = AsyncMock(return_value="user")
        await profiles.lock_profile_owner(db_session, "user")
        statement = db_session.scalar.await_args.args[0]
        self.assertIsNotNone(statement._for_update_arg)

    async def test_commitment_reconciliation_locks_parent_tournaments_first(self) -> None:
        db_session = Mock()
        db_session.scalars = AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: ["t2", "t1", "t1"]),
                SimpleNamespace(all=lambda: ["t1", "t2"]),
            ]
        )
        locked = await player_commitments.lock_active_commitment_tournaments(db_session)
        self.assertEqual(locked, ("t1", "t2"))
        lock_statement = db_session.scalars.await_args_list[1].args[0]
        self.assertIsNotNone(lock_statement._for_update_arg)

    def test_reconciliation_scopes_mutations_to_locked_tournaments(self) -> None:
        source = Path(player_commitments.__file__).read_text(encoding="utf-8")
        block = source.split("async def reconcile_player_commitments(", 1)[1]
        self.assertGreaterEqual(
            block.count("tournament_id.in_(locked_tournament_ids)"),
            3,
        )

    async def test_automation_failure_reload_reacquires_workflow_lock(self) -> None:
        tournament = SimpleNamespace(id="tournament")
        with patch.object(
            deadlock_automation,
            "lock_tournament_for_workflow",
            AsyncMock(return_value=tournament),
        ) as locked:
            result = await deadlock_automation._lock_tournament_for_failure_state(
                Mock(), "tournament"
            )
        self.assertIs(result, tournament)
        locked.assert_awaited_once()

    def test_persistent_domain_constraints_are_present_in_metadata(self) -> None:
        participant_constraints = {
            constraint.name for constraint in TournamentParticipant.__table__.constraints
        }
        self.assertIn("ck_tournament_participants_entry_type_solo", participant_constraints)
        self.assertIn("ck_tournament_participants_status_allowed", participant_constraints)

        match_constraints = {
            constraint.name for constraint in TournamentMatch.__table__.constraints
        }
        self.assertIn("ck_tournament_matches_status_allowed", match_constraints)
        self.assertIn(
            "ck_tournament_matches_completed_result_consistent", match_constraints
        )

        shard_constraints = {
            constraint.name
            for constraint in TournamentDeadlockReadyVoteCountShard.__table__.constraints
        }
        self.assertIn(
            "ck_tournament_deadlock_ready_vote_count_shards_vote_count_nonnegative",
            shard_constraints,
        )
        self.assertIn(
            "ck_tournament_deadlock_ready_vote_count_shards_shard_in_range",
            shard_constraints,
        )

    def test_idempotency_key_and_payload_fingerprint_are_stable(self) -> None:
        request = SimpleNamespace(headers={"Idempotency-Key": "retry-123"})
        self.assertEqual(request_idempotency_key(request), "retry-123")
        self.assertEqual(
            mutation_payload_fingerprint({"b": 2, "a": 1}),
            mutation_payload_fingerprint({"a": 1, "b": 2}),
        )

    def test_disqualified_self_leave_has_explicit_retention_guard(self) -> None:
        source = Path(tournaments.__file__).read_text(encoding="utf-8")
        leave_block = source.split('async def leave_tournament(', 1)[1].split(
            '@router.get("/{slug}/profiles/{user_id}"', 1
        )[0]
        self.assertIn('participant.status == "disqualified"', leave_block)
        self.assertIn("retained until the organizer", leave_block)

    def test_historical_migration_no_longer_deletes_tournaments(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260429_0011_drop_tournament_dream_slots.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM platform.tournaments", migration)
        self.assertIn("will not delete tournament data", migration)


if __name__ == "__main__":
    unittest.main()
