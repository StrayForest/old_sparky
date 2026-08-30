from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import unittest
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, update

from apps.platform_api.app.services.tournament_workflow import (
    generate_deadlock_auto_assignment_run_for_tournament,
)
from apps.platform_api.app.api.routes import tournaments as tournament_routes
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation
from python_packages.platform_infra import performance
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.csrf import csrf_cookie_name, generate_csrf_token
from python_packages.platform_infra.db import dispose_engine, engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentDeadlockReadyVoteCountShard,
    TournamentParticipant,
    UserSession,
    User,
)
from python_packages.platform_infra.security import session_token_digest


class PlatformDeadlockApiFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-deadlock-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.base_url = "http://testserver"
        self.app = create_app()
        self.clients = AsyncExitStack()
        await self._cleanup_test_data()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        await self._cleanup_test_data()
        await dispose_engine()

    async def _cleanup_test_data(self) -> None:
        async with session_factory()() as db_session:
            user_ids = list(
                (
                    await db_session.scalars(
                        select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
                    )
                ).all()
            )
            if user_ids:
                await db_session.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids)))
            await db_session.execute(delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%")))
            if user_ids:
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self.base_url,
            )
        )

    def _assert_status(self, response: httpx.Response, expected_status: int) -> dict:
        self.assertEqual(response.status_code, expected_status, response.text)
        if not response.content:
            return {}
        return response.json()

    async def _register_user(
        self,
        *,
        label: str,
        rank: str,
        subrank: int,
        captain_priority: str | None = None,
    ) -> dict[str, Any]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        display_name = f"test-{label}"[:15]
        register_payload = self._assert_status(
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": display_name,
                },
            ),
            201,
        )
        self._assert_status(
            await client.put(
                "/api/v1/profiles/me/deadlock",
                json={
                    "rank": rank,
                    "subrank": subrank,
                    "playtime": "1501-2000",
                    "roles": ["Carry", "Semi-Carry", "Support", "Semi-Support"],
                    "pool": ["Abrams", "Kelvin", "Seven"],
                    "captain_priority": captain_priority,
                },
            ),
            200,
        )
        return {
            "label": label,
            "email": email,
            "client": client,
            "user_id": register_payload["user"]["id"],
            "display_name": register_payload["user"]["display_name"],
        }

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user, f"User {user_id} is missing.")
            user.public_tournament_credits = 100
            await db_session.commit()

    async def _advance_deadlock_automation_for_slug(self, slug: str, *, now: datetime) -> dict[str, int]:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            result = await advance_deadlock_tournament_automation(
                db_session,
                tournament=tournament,
                now=now,
            )
            return result.as_dict()

    async def _generate_deadlock_auto_assignment_for_slug(
        self,
        slug: str,
        *,
        actor_user_id: str,
    ) -> str:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            run_row = await generate_deadlock_auto_assignment_run_for_tournament(
                db_session,
                tournament=tournament,
                actor_user_id=actor_user_id,
            )
            run_id = str(run_row.id)
            await db_session.commit()
            return run_id

    async def _prepare_ready_vote_fixture(
        self,
        *,
        label: str,
        extra_participant: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        organizer = await self._register_user(
            label=f"{label}-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        voter = await self._register_user(
            label=f"{label}-voter",
            rank="Ascendant",
            subrank=6,
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-{label}"[:25],
                    "description": "Ready vote instrumentation fixture.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        participants = (organizer, voter) + (
            (extra_participant,) if extra_participant is not None else ()
        )
        for user in participants:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"
            ),
            201,
        )
        if extra_participant is not None:
            async with session_factory()() as db_session:
                active_round = await db_session.scalar(
                    select(TournamentDeadlockReadyRound).where(
                        TournamentDeadlockReadyRound.tournament_id == tournament_payload["id"],
                        TournamentDeadlockReadyRound.status == "active",
                    )
                )
                self.assertIsNotNone(active_round)
                assert active_round is not None
                active_round.eligible_user_ids = [
                    str(organizer["user_id"]),
                    str(voter["user_id"]),
                ]
                await db_session.commit()
        return organizer, voter, slug

    async def test_ready_vote_checkout_is_one_for_each_success_and_duplicate(self) -> None:
        _organizer, voter, slug = await self._prepare_ready_vote_fixture(label="checkout")
        settings = get_settings()
        session_token = voter["client"].cookies.get(settings.platform_session_cookie_name)
        self.assertIsNotNone(session_token)
        async with session_factory()() as db_session:
            initial_last_seen_at = await db_session.scalar(
                select(UserSession.last_seen_at).where(
                    UserSession.token_digest == session_token_digest(str(session_token)),
                )
            )
        self.assertIsNotNone(initial_last_seen_at)
        checkout_scope: ContextVar[str | None] = ContextVar(
            "ready_vote_checkout_probe",
            default=None,
        )
        physical_pool = engine().sync_engine.pool
        checkout_counts: dict[str, int] = {}

        def count_checkout(_dbapi_connection: object, _connection_record: object, _connection_proxy: object) -> None:
            metrics = performance.current_request_metrics()
            request_id = metrics.request_id if metrics is not None else checkout_scope.get()
            if request_id is not None:
                checkout_counts[request_id] = checkout_counts.get(request_id, 0) + 1

        captured_metrics: list[performance.RequestPerformanceMetrics] = []
        original_log = performance.RequestPerformanceMiddleware._log_if_slow

        def capture_metrics(
            middleware: performance.RequestPerformanceMiddleware,
            scope: dict[str, Any],
            metrics: performance.RequestPerformanceMetrics,
            status_code: int,
        ) -> None:
            if str(scope.get("path") or "").endswith("/deadlock/ready-check/vote"):
                captured_metrics.append(metrics)
            original_log(middleware, scope, metrics, status_code)

        event.listen(physical_pool, "checkout", count_checkout)
        try:
            with patch.object(
                performance.RequestPerformanceMiddleware,
                "_log_if_slow",
                capture_metrics,
            ):
                for choice, expected_changed in (
                    ("yes", True),
                    ("yes", False),
                    ("no", True),
                ):
                    request_id = f"ready-vote-checkout-{uuid4().hex}"
                    token = checkout_scope.set(request_id)
                    try:
                        response = await voter["client"].post(
                            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                            json={"choice": choice},
                            headers={"X-Request-ID": request_id},
                        )
                    finally:
                        checkout_scope.reset(token)
                    payload = self._assert_status(response, 200)
                    self.assertEqual(payload["changed"], expected_changed)
                    self.assertEqual(checkout_counts.get(request_id, 0), 1)
                    metrics = next(
                        item for item in reversed(captured_metrics) if item.request_id == request_id
                    )
                    self.assertEqual(metrics.ready_vote_checkout_count, 1)
                    self.assertGreater(metrics.ready_vote_checkout_ms, 0.0)
        finally:
            event.remove(physical_pool, "checkout", count_checkout)

        self.assertEqual(len(captured_metrics), 3)
        async with session_factory()() as db_session:
            final_last_seen_at = await db_session.scalar(
                select(UserSession.last_seen_at).where(
                    UserSession.token_digest == session_token_digest(str(session_token)),
                )
            )
        self.assertEqual(final_last_seen_at, initial_last_seen_at)

    async def test_ready_vote_auth_and_preflight_failures_never_checkout_more_than_once(self) -> None:
        ineligible = await self._register_user(
            label="checkout-errors-ineligible",
            rank="Ascendant",
            subrank=6,
        )
        organizer, voter, slug = await self._prepare_ready_vote_fixture(
            label="checkout-errors",
            extra_participant=ineligible,
        )
        outsider = await self._register_user(
            label="checkout-errors-outsider",
            rank="Ascendant",
            subrank=6,
        )
        expired = await self._register_user(
            label="checkout-errors-expired",
            rank="Ascendant",
            subrank=6,
        )
        invalidated = await self._register_user(
            label="checkout-errors-invalidated",
            rank="Ascendant",
            subrank=6,
        )
        inactive = await self._register_user(
            label="checkout-errors-inactive",
            rank="Ascendant",
            subrank=6,
        )
        unverified = await self._register_user(
            label="checkout-errors-unverified",
            rank="Ascendant",
            subrank=6,
        )
        settings = get_settings()
        state_now = datetime.now(UTC)
        async with session_factory()() as db_session:
            for user, session_values in (
                (
                    expired,
                    {"expires_at": state_now - timedelta(seconds=1)},
                ),
                (
                    invalidated,
                    {"invalidated_at": state_now},
                ),
            ):
                token = user["client"].cookies.get(settings.platform_session_cookie_name)
                self.assertIsNotNone(token)
                await db_session.execute(
                    update(UserSession)
                    .where(UserSession.token_digest == session_token_digest(str(token)))
                    .values(**session_values)
                )
            inactive_token = inactive["client"].cookies.get(settings.platform_session_cookie_name)
            unverified_token = unverified["client"].cookies.get(settings.platform_session_cookie_name)
            self.assertIsNotNone(inactive_token)
            self.assertIsNotNone(unverified_token)
            await db_session.execute(
                update(User)
                .where(User.id == inactive["user_id"])
                .values(status="suspended")
            )
            await db_session.execute(
                update(User)
                .where(User.id == unverified["user_id"])
                .values(email_verified_at=None)
            )
            await db_session.commit()

        invalid_token_client = await self._new_client()
        invalid_token = f"invalid-ready-vote-{uuid4().hex}"
        invalid_csrf_token = generate_csrf_token(invalid_token, settings)
        invalid_token_client.cookies.set(settings.platform_session_cookie_name, invalid_token)
        invalid_token_client.cookies.set(csrf_cookie_name(settings), invalid_csrf_token)
        checkout_scope: ContextVar[str | None] = ContextVar(
            "ready_vote_error_checkout_probe",
            default=None,
        )
        physical_pool = engine().sync_engine.pool
        checkout_counts: dict[str, int] = {}

        def count_checkout(_dbapi_connection: object, _connection_record: object, _connection_proxy: object) -> None:
            metrics = performance.current_request_metrics()
            request_id = metrics.request_id if metrics is not None else checkout_scope.get()
            if request_id is not None:
                checkout_counts[request_id] = checkout_counts.get(request_id, 0) + 1

        event.listen(physical_pool, "checkout", count_checkout)

        async def assert_at_most_one_checkout(
            client: httpx.AsyncClient,
            *,
            expected_status: int,
        ) -> None:
            request_id = f"ready-vote-error-{uuid4().hex}"
            token = checkout_scope.set(request_id)
            try:
                headers = {"X-Request-ID": request_id}
                csrf_token = client.cookies.get(csrf_cookie_name(settings))
                if csrf_token:
                    headers["X-CSRF-Token"] = str(csrf_token)
                    headers["Origin"] = settings.platform_web_origin
                response = await client.post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                    headers=headers,
                )
            finally:
                checkout_scope.reset(token)
            self.assertEqual(response.status_code, expected_status, response.text)
            self.assertLessEqual(checkout_counts.get(request_id, 0), 1)

        try:
            anonymous = await self._new_client()
            await assert_at_most_one_checkout(anonymous, expected_status=401)
            await assert_at_most_one_checkout(invalid_token_client, expected_status=401)
            await assert_at_most_one_checkout(expired["client"], expected_status=401)
            await assert_at_most_one_checkout(invalidated["client"], expected_status=401)
            await assert_at_most_one_checkout(inactive["client"], expected_status=401)
            verification_settings = settings.model_copy(
                update={"platform_email_verification_required": True}
            )
            with patch(
                "python_packages.platform_infra.security.get_settings",
                return_value=verification_settings,
            ):
                await assert_at_most_one_checkout(unverified["client"], expected_status=401)
            await assert_at_most_one_checkout(outsider["client"], expected_status=403)
            await assert_at_most_one_checkout(ineligible["client"], expected_status=403)

            async with session_factory()() as db_session:
                tournament = await db_session.scalar(
                    select(Tournament).where(Tournament.slug == slug)
                )
                self.assertIsNotNone(tournament)
                assert tournament is not None
                now = datetime.now(UTC)
                tournament.ready_check_starts_at = now + timedelta(minutes=5)
                tournament.ready_check_ends_at = now + timedelta(minutes=15)
                await db_session.commit()
            await assert_at_most_one_checkout(voter["client"], expected_status=409)

            async with session_factory()() as db_session:
                tournament = await db_session.scalar(
                    select(Tournament).where(Tournament.slug == slug)
                )
                self.assertIsNotNone(tournament)
                assert tournament is not None
                now = datetime.now(UTC)
                tournament.ready_check_starts_at = now - timedelta(minutes=15)
                tournament.ready_check_ends_at = now - timedelta(minutes=5)
                await db_session.commit()
            await assert_at_most_one_checkout(voter["client"], expected_status=409)

            async with session_factory()() as db_session:
                tournament = await db_session.scalar(
                    select(Tournament).where(Tournament.slug == slug)
                )
                self.assertIsNotNone(tournament)
                assert tournament is not None
                now = datetime.now(UTC)
                tournament.ready_check_starts_at = now - timedelta(minutes=1)
                tournament.ready_check_ends_at = now + timedelta(minutes=5)
                active_round = await db_session.scalar(
                    select(TournamentDeadlockReadyRound).where(
                        TournamentDeadlockReadyRound.tournament_id == tournament.id,
                        TournamentDeadlockReadyRound.status == "active",
                    )
                )
                self.assertIsNotNone(active_round)
                assert active_round is not None
                active_round.status = "closed"
                active_round.closed_at = datetime.now(UTC)
                tournament.automation_ready_check_closed_at = datetime.now(UTC)
                await db_session.commit()
            await assert_at_most_one_checkout(voter["client"], expected_status=409)
        finally:
            event.remove(physical_pool, "checkout", count_checkout)

    async def test_ready_vote_withdrawal_race_rolls_back_vote_and_counter(self) -> None:
        organizer, voter, slug = await self._prepare_ready_vote_fixture(label="withdraw-race")
        participant_id: str
        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(
                select(Tournament.id).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament_id)
            participant_value = await db_session.scalar(
                select(TournamentParticipant.id).where(
                    TournamentParticipant.tournament_id == tournament_id,
                    TournamentParticipant.user_id == voter["user_id"],
                )
            )
            self.assertIsNotNone(participant_value)
            participant_id = str(participant_value)

        vote_at_upsert = asyncio.Event()
        release_vote = asyncio.Event()
        original_upsert = tournament_routes.upsert_deadlock_ready_vote

        async def gated_upsert(*args: Any, **kwargs: Any) -> bool:
            vote_at_upsert.set()
            await release_vote.wait()
            return await original_upsert(*args, **kwargs)

        vote_task: asyncio.Task[httpx.Response] | None = None
        exclusion_task: asyncio.Task[httpx.Response] | None = None
        vote_response: httpx.Response | None = None
        try:
            with patch.object(
                tournament_routes,
                "upsert_deadlock_ready_vote",
                side_effect=gated_upsert,
            ):
                vote_task = asyncio.create_task(
                    voter["client"].post(
                        f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                        json={"choice": "yes"},
                    )
                )
                await asyncio.wait_for(vote_at_upsert.wait(), timeout=10)
                exclusion_task = asyncio.create_task(
                    organizer["client"].patch(
                        f"/api/v1/tournaments/{slug}/participants/{participant_id}/moderation",
                        json={
                            "status": "withdrawn",
                            "moderation_note": "Withdrawn during ready vote race.",
                        },
                    )
                )
                exclusion_response = await asyncio.wait_for(exclusion_task, timeout=10)
                self.assertEqual(exclusion_response.status_code, 200, exclusion_response.text)
                release_vote.set()
                vote_response = await asyncio.wait_for(vote_task, timeout=10)
        finally:
            release_vote.set()
            pending_tasks = [
                task for task in (vote_task, exclusion_task) if task is not None
            ]
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        self.assertIsNotNone(vote_response)
        assert vote_response is not None
        self.assertEqual(vote_response.status_code, 409, vote_response.text)
        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(
                select(Tournament.id).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament_id)
            round_row = await db_session.scalar(
                select(TournamentDeadlockReadyRound).where(
                    TournamentDeadlockReadyRound.tournament_id == tournament_id
                )
            )
            self.assertIsNotNone(round_row)
            assert round_row is not None
            vote_count = await db_session.scalar(
                select(func.count(TournamentDeadlockReadyVote.id)).where(
                    TournamentDeadlockReadyVote.round_id == round_row.id,
                    TournamentDeadlockReadyVote.user_id == voter["user_id"],
                )
            )
            shard_count = await db_session.scalar(
                select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0)).where(
                    TournamentDeadlockReadyVoteCountShard.round_id == round_row.id,
                )
            )
        self.assertEqual(vote_count, 0)
        self.assertEqual(shard_count, 0)

    async def test_parallel_ready_vote_choices_keep_one_row_and_matching_shards(self) -> None:
        _organizer, voter, slug = await self._prepare_ready_vote_fixture(label="parallel-vote")

        with patch.object(tournament_routes, "_invalidate_ready_check_state_cache") as invalidate_cache:
            yes_response, no_response = await asyncio.gather(
                voter["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                voter["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "no"},
                ),
            )
        self.assertEqual(yes_response.status_code, 200, yes_response.text)
        self.assertEqual(no_response.status_code, 200, no_response.text)
        self.assertEqual(
            sorted(
                (
                    bool(yes_response.json()["changed"]),
                    bool(no_response.json()["changed"]),
                )
            ),
            [True, True],
        )
        self.assertEqual(invalidate_cache.call_count, 2)

        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(
                select(Tournament.id).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament_id)
            round_row = await db_session.scalar(
                select(TournamentDeadlockReadyRound).where(
                    TournamentDeadlockReadyRound.tournament_id == tournament_id
                )
            )
            self.assertIsNotNone(round_row)
            assert round_row is not None
            vote_row = await db_session.scalar(
                select(TournamentDeadlockReadyVote).where(
                    TournamentDeadlockReadyVote.round_id == round_row.id,
                    TournamentDeadlockReadyVote.user_id == voter["user_id"],
                )
            )
            self.assertIsNotNone(vote_row)
            assert vote_row is not None
            shard_totals = {
                choice: await db_session.scalar(
                    select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0)).where(
                        TournamentDeadlockReadyVoteCountShard.round_id == round_row.id,
                        TournamentDeadlockReadyVoteCountShard.choice == choice,
                    )
                )
                for choice in ("yes", "no")
            }
            vote_count = await db_session.scalar(
                select(func.count(TournamentDeadlockReadyVote.id)).where(
                    TournamentDeadlockReadyVote.round_id == round_row.id,
                    TournamentDeadlockReadyVote.user_id == voter["user_id"],
                )
            )

        self.assertEqual(vote_count, 1)
        self.assertIn(vote_row.choice, {"yes", "no"})
        self.assertEqual(shard_totals[vote_row.choice], 1)
        self.assertEqual(shard_totals["yes" if vote_row.choice == "no" else "no"], 0)

    async def test_ready_check_start_serializes_concurrent_organizer_requests(self) -> None:
        organizer = await self._register_user(
            label="concurrent-ready-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-cr",
                    "description": "Ready-check writer serialization.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        first_response, second_response = await asyncio.gather(
            organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
        )
        self.assertEqual(
            sorted((first_response.status_code, second_response.status_code)),
            [201, 409],
            {"first": first_response.text, "second": second_response.text},
        )

        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(select(Tournament.id).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament_id)
            active_round_count = await db_session.scalar(
                select(func.count(TournamentDeadlockReadyRound.id)).where(
                    TournamentDeadlockReadyRound.tournament_id == tournament_id,
                    TournamentDeadlockReadyRound.status == "active",
                )
            )
        self.assertEqual(active_round_count, 1)

    async def test_ready_vote_does_not_hold_tournament_lock_until_the_round_is_closed(self) -> None:
        organizer = await self._register_user(
            label="vote-lock-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        voter = await self._register_user(
            label="vote-lock-player",
            rank="Ascendant",
            subrank=6,
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-vl",
                    "description": "Ready vote close serialization.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        for user in (organizer, voter):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )

        vote_at_upsert = asyncio.Event()
        release_vote = asyncio.Event()
        original_upsert = tournament_routes.upsert_deadlock_ready_vote

        async def gated_upsert(*args: Any, **kwargs: Any) -> bool:
            vote_at_upsert.set()
            await release_vote.wait()
            return await original_upsert(*args, **kwargs)

        vote_task: asyncio.Task[httpx.Response] | None = None
        close_task: asyncio.Task[httpx.Response] | None = None
        try:
            with patch.object(
                tournament_routes,
                "upsert_deadlock_ready_vote",
                side_effect=gated_upsert,
            ):
                vote_task = asyncio.create_task(
                    voter["client"].post(
                        f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                        json={"choice": "yes"},
                    )
                )
                await asyncio.wait_for(vote_at_upsert.wait(), timeout=10)
                close_task = asyncio.create_task(
                    organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close")
                )
                await asyncio.wait_for(asyncio.shield(close_task), timeout=2)
                self.assertTrue(
                    close_task.done(),
                    "Ready-check closure must not wait for the ordinary vote writer.",
                )
                release_vote.set()
                vote_response, close_response = await asyncio.wait_for(
                    asyncio.gather(vote_task, close_task),
                    timeout=10,
                )
        finally:
            release_vote.set()
            pending_tasks = [task for task in (vote_task, close_task) if task is not None]
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        self.assertIn(vote_response.status_code, (200, 409), vote_response.text)
        self.assertEqual(close_response.status_code, 200, close_response.text)
        async with session_factory()() as db_session:
            round_row = await db_session.scalar(
                select(TournamentDeadlockReadyRound)
                .join(Tournament)
                .where(Tournament.slug == slug)
            )
            self.assertIsNotNone(round_row)
            vote_count = await db_session.scalar(
                select(func.count(TournamentDeadlockReadyVote.id)).where(
                    TournamentDeadlockReadyVote.round_id == round_row.id,
                    TournamentDeadlockReadyVote.user_id == voter["user_id"],
                )
            )
        self.assertEqual(round_row.status, "closed")
        self.assertEqual(vote_count, 1)

    async def test_deadlock_api_flow_covers_ready_check_assignment_lock_and_handoff(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label="player01", rank="Ascendant", subrank=6, captain_priority="yes"),
            await self._register_user(label="player02", rank="Phantom", subrank=6),
            await self._register_user(label="player03", rank="Phantom", subrank=5),
            await self._register_user(label="player04", rank="Phantom", subrank=4),
            await self._register_user(label="player05", rank="Oracle", subrank=6),
            await self._register_user(label="player06", rank="Oracle", subrank=5),
            await self._register_user(label="player07", rank="Emissary", subrank=6),
            await self._register_user(label="player08", rank="Emissary", subrank=5),
            await self._register_user(label="player09", rank="Ritualist", subrank=6),
            await self._register_user(label="player10", rank="Ritualist", subrank=5),
            await self._register_user(label="player11", rank="Mystic", subrank=6),
            await self._register_user(label="player12", rank="Mystic", subrank=5),
            await self._register_user(label="player13", rank="Acolyte", subrank=6),
        ]
        outsider = await self._register_user(label="profile-outsider", rank="Oracle", subrank=3)
        all_players = [organizer, *players]
        avatar_url = "/assets/main_logo/old-sparky-arena-logo-v3.webp"
        async with session_factory()() as db_session:
            fallback_profile = await db_session.get(PlayerProfile, players[0]["user_id"])
            self.assertIsNotNone(fallback_profile)
            fallback_profile.avatar_url = avatar_url
            fallback_profile.handle = None
            await db_session.commit()

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock API integration flow",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(participants_payload), 14)

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        blocked_match_creation = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Premature semifinal",
                "round_number": 1,
                "sequence_number": 1,
                "home_label": "Team 1",
                "away_label": "Team 2",
                "scheduled_at": None,
            },
        )
        self.assertEqual(blocked_match_creation.status_code, 409, blocked_match_creation.text)
        self.assertIn("Lock a Deadlock roster before creating matches.", blocked_match_creation.json()["detail"])

        blocked_seed_creation = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
        )
        self.assertEqual(blocked_seed_creation.status_code, 409, blocked_seed_creation.text)
        self.assertIn("Lock a Deadlock roster before creating matches.", blocked_seed_creation.json()["detail"])

        blocked_in_progress = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "in_progress"},
        )
        self.assertEqual(blocked_in_progress.status_code, 422, blocked_in_progress.text)
        self.assertIn("Lock a Deadlock roster", blocked_in_progress.json()["detail"])

        ready_start_payload = self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        self.assertEqual(ready_start_payload["eligible_participant_count"], 14)

        for user in all_players:
            vote_payload = self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
            self.assertEqual(vote_payload["status"], "active")

        ready_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_payload["active_round"]["ready_count"], 14)

        preview_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-preview",
                params={"teams_count": 2},
            ),
            200,
        )
        self.assertEqual(preview_payload["ready_player_count"], 14)
        self.assertEqual(
            [candidate["user_id"] for candidate in preview_payload["candidates"][:2]],
            [organizer["user_id"], players[0]["user_id"]],
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        captain_start_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/captain-round/start",
                json={"teams_count": 2},
            ),
            201,
        )
        self.assertEqual(captain_start_payload["status"], "finalized")
        self.assertEqual(captain_start_payload["offered_count"], 0)
        self.assertEqual(captain_start_payload["assigned_count"], 2)
        self.assertEqual(captain_start_payload["declined_count"], 0)

        captain_retry = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/start",
            json={"teams_count": 2},
        )
        self.assertEqual(captain_retry.status_code, 409, captain_retry.text)
        self.assertIn(
            "already exists for this ready-check round",
            captain_retry.json()["detail"],
        )

        captain_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state_payload["active_round"])
        latest_captain_payload = captain_state_payload["latest_round"]
        self.assertEqual(latest_captain_payload["status"], "finalized")
        self.assertEqual(latest_captain_payload["assigned_count"], 2)
        self.assertEqual(
            {
                entry["user_id"]
                for entry in latest_captain_payload["entries"]
                if entry["state"] == "assigned"
            },
            {organizer["user_id"], players[0]["user_id"]},
        )

        dream_slots_payload = self._assert_status(
            await organizer["client"].put(
                "/api/v1/profiles/me/deadlock/dream-slots",
                json={
                    "slots": [
                        {
                            "slot_number": 1,
                            "allowed_roles": ["Carry"],
                            "desired_heroes": ["Abrams"],
                        }
                    ]
                },
            ),
            200,
        )
        self.assertEqual(dream_slots_payload[0]["allowed_roles"], ["Carry"])
        self.assertEqual(dream_slots_payload[0]["desired_heroes"], ["Abrams"])

        generated_run_id = await self._generate_deadlock_auto_assignment_for_slug(
            slug,
            actor_user_id=str(organizer["user_id"]),
        )
        generated_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        generated_run_payload = generated_state_payload["latest_run"]
        self.assertEqual(generated_run_payload["id"], generated_run_id)
        self.assertEqual(generated_run_payload["status"], "generated")
        self.assertEqual(len(generated_run_payload["teams"]), 2)
        run_id = generated_run_payload["id"]
        target_profile_user_id = generated_run_payload["teams"][0]["captain"]["user_id"]

        organizer_profile_before_publish = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
            ),
            200,
        )
        self.assertEqual(organizer_profile_before_publish["profile"]["user_id"], target_profile_user_id)

        participant_profile_before_publish = await players[1]["client"].get(
            f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
        )
        self.assertEqual(participant_profile_before_publish.status_code, 409, participant_profile_before_publish.text)

        legacy_run_response = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/run"
        )
        self.assertEqual(legacy_run_response.status_code, 404, legacy_run_response.text)
        with self.assertRaises(HTTPException) as duplicate_run_error:
            await self._generate_deadlock_auto_assignment_for_slug(
                slug,
                actor_user_id=str(organizer["user_id"]),
            )
        self.assertEqual(duplicate_run_error.exception.status_code, 409)
        self.assertIn("already matches the current captain", str(duplicate_run_error.exception.detail))

        published_run_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/{run_id}/publish"
            ),
            200,
        )
        self.assertEqual(published_run_payload["status"], "published")

        participant_state_payload = self._assert_status(
            await players[1]["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        self.assertIsNone(participant_state_payload["latest_run"])
        self.assertEqual(participant_state_payload["published_run"]["id"], run_id)
        self.assertEqual(participant_state_payload["published_run"]["status"], "published")

        outsider_profile_response = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
        )
        self.assertEqual(outsider_profile_response.status_code, 403, outsider_profile_response.text)

        async with session_factory()() as db_session:
            profile = await db_session.scalar(
                select(PlayerProfile).where(
                    PlayerProfile.user_id == target_profile_user_id
                )
            )
            self.assertIsNotNone(profile)
            profile.steam_id = "76561198000000000"
            await db_session.commit()

        participant_profile_payload = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
            ),
            200,
        )
        self.assertEqual(participant_profile_payload["profile"]["user_id"], target_profile_user_id)
        self.assertNotIn("contact_email", participant_profile_payload["profile"])
        self.assertEqual(participant_profile_payload["profile"]["steam_id"], "76561198000000000")
        self.assertIn("deadlock_profile", participant_profile_payload)
        self.assertEqual(len(participant_profile_payload["dream_slots"]), 6)

        locked_run_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/{run_id}/lock"
            ),
            200,
        )
        self.assertEqual(locked_run_payload["status"], "locked")
        self.assertIsNotNone(locked_run_payload["locked_at"])

        locked_tournament_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertTrue(locked_tournament_payload["has_locked_deadlock_roster"])

        async with session_factory()() as db_session:
            active_commitments = (
                await db_session.scalars(
                    select(PlayerTournamentCommitment).where(
                        PlayerTournamentCommitment.tournament_id == locked_tournament_payload["id"],
                        PlayerTournamentCommitment.released_at.is_(None)
                    )
                )
            ).all()
            self.assertEqual(len(active_commitments), 14)
            self.assertEqual(
                len({commitment.user_id for commitment in active_commitments}),
                14,
            )

        waiting_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-w",
                    "description": "Registration remains available while committed elsewhere.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        waiting_slug = waiting_tournament["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{waiting_slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await players[1]["client"].post(
                f"/api/v1/tournaments/{waiting_slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        waiting_workspace = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{waiting_slug}/workspace",
                params={"participants_limit": 0, "workspace_view": "detail"},
            ),
            200,
        )
        self.assertEqual(
            waiting_workspace["current_user_active_commitment"]["tournament_id"],
            locked_tournament_payload["id"],
        )

        seeded_matches_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
            ),
            201,
        )
        self.assertEqual(len(seeded_matches_payload), 1)
        self.assertEqual(seeded_matches_payload[0]["title"], "Grand Final")
        self.assertEqual(seeded_matches_payload[0]["home_label"], "Team 1")
        self.assertEqual(seeded_matches_payload[0]["away_label"], "Team 2")
        match_id = seeded_matches_payload[0]["id"]
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertGreater(len(bracket_payload["teams"][0]["members"]), 0)
        roster_members = [
            member
            for team in bracket_payload["teams"]
            for member in team["members"]
        ]
        avatar_member = next(
            member
            for member in roster_members
            if member["user_id"] == players[0]["user_id"]
        )
        self.assertIsNone(avatar_member["avatar_url"])
        self.assertEqual(avatar_member["handle"], players[0]["display_name"])
        self.assertEqual(avatar_member["rank"], "Ascendant")
        self.assertEqual(avatar_member["subrank"], 6)
        self.assertEqual(len(bracket_payload["matches"]), 1)
        self.assertEqual(bracket_payload["matches"][0]["team_a_id"], "1")
        self.assertEqual(bracket_payload["matches"][0]["team_b_id"], "2")
        summary_bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket?teams_view=summary"),
            200,
        )
        self.assertEqual(summary_bracket_payload["status"], "ready")
        self.assertEqual(len(summary_bracket_payload["teams"]), 2)
        self.assertEqual(summary_bracket_payload["teams"][0]["members"], [])
        self.assertEqual(
            summary_bracket_payload["teams"][0]["starter_strength"],
            bracket_payload["teams"][0]["starter_strength"],
        )
        self.assertEqual(len(summary_bracket_payload["matches"]), 1)

        duplicate_seed_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
        )
        self.assertEqual(duplicate_seed_attempt.status_code, 409, duplicate_seed_attempt.text)
        self.assertIn("Matches already exist for this tournament.", duplicate_seed_attempt.json()["detail"])

        blocked_live_before_start = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{match_id}/status",
            json={"status": "live"},
        )
        self.assertEqual(blocked_live_before_start.status_code, 409, blocked_live_before_start.text)
        self.assertIn("Tournament must be in progress", blocked_live_before_start.json()["detail"])

        blocked_reopen = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "registration_open"},
        )
        self.assertEqual(blocked_reopen.status_code, 422, blocked_reopen.text)
        self.assertIn("Registration cannot be reopened", blocked_reopen.json()["detail"])

        in_progress_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )
        self.assertEqual(in_progress_payload["status"], "in_progress")

        live_match_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{match_id}/status",
                json={"status": "live"},
            ),
            200,
        )
        self.assertEqual(live_match_payload["status"], "live")

        completed_match_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{match_id}/report",
                json={
                    "home_score": 2,
                    "away_score": 1,
                    "note": "Locked-roster handoff match completed.",
                },
            ),
            200,
        )
        self.assertEqual(completed_match_payload["status"], "completed")
        self.assertEqual(completed_match_payload["winner_side"], "home")
        completed_tournament_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(completed_tournament_payload["status"], "completed")
        async with session_factory()() as db_session:
            active_commitment_count = await db_session.scalar(
                select(func.count())
                .select_from(PlayerTournamentCommitment)
                .where(
                    PlayerTournamentCommitment.tournament_id == completed_tournament_payload["id"],
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            self.assertEqual(int(active_commitment_count or 0), 0)
        released_workspace = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{waiting_slug}/workspace",
                params={"participants_limit": 0, "workspace_view": "detail"},
            ),
            200,
        )
        self.assertIsNone(released_workspace["current_user_active_commitment"])

    async def test_deadlock_permissions_require_joined_participant_or_organizer(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        participant = await self._register_user(
            label="participant",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        outsider = await self._register_user(
            label="outsider",
            rank="Phantom",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock permission coverage",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await participant["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        participant_start_attempt = await participant["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"
        )
        self.assertEqual(participant_start_attempt.status_code, 403, participant_start_attempt.text)
        self.assertIn("Only the organizer can manage this tournament.", participant_start_attempt.json()["detail"])

        outsider_ready_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check"
        )
        self.assertEqual(outsider_ready_state.status_code, 403, outsider_ready_state.text)
        self.assertIn("Join the tournament before viewing ready-check state.", outsider_ready_state.json()["detail"])

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"
            ),
            201,
        )

        participant_ready_state = self._assert_status(
            await participant["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check"
            ),
            200,
        )
        self.assertEqual(participant_ready_state["active_round"]["status"], "active")

        outsider_captain_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round"
        )
        self.assertEqual(outsider_captain_state.status_code, 403, outsider_captain_state.text)
        self.assertIn("Join the tournament before viewing captain-round state.", outsider_captain_state.json()["detail"])

        outsider_auto_assignment_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"
        )
        self.assertEqual(
            outsider_auto_assignment_state.status_code,
            403,
            outsider_auto_assignment_state.text,
        )
        self.assertIn(
            "Join the tournament before viewing Deadlock auto-assignment state.",
            outsider_auto_assignment_state.json()["detail"],
        )

    async def test_deadlock_ready_check_state_get_does_not_start_scheduled_round(self) -> None:
        organizer = await self._register_user(
            label="readonly",
            rank="Phantom",
            subrank=5,
        )
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-r",
                    "description": "Ready-check GET must not advance workflow state.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament.ready_check_starts_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
            await db_session.commit()

        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertIsNone(ready_state["active_round"])
        self.assertIsNone(ready_state["latest_round"])

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            self.assertEqual(tournament.status, "registration_open")
            self.assertIsNone(tournament.automation_ready_check_started_at)

    async def test_ready_vote_works_before_delayed_worker_and_duplicate_tabs_are_idempotent(self) -> None:
        organizer = await self._register_user(
            label="delayed-worker-voter",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-d",
                    "description": "Ready vote must not wait for automation.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        now = datetime.now(UTC)
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament_id = str(tournament.id)
            tournament.ready_check_starts_at = now + timedelta(minutes=1)
            tournament.ready_check_ends_at = now + timedelta(minutes=10)
            tournament.automation_ready_check_started_at = None
            tournament.automation_ready_check_closed_at = None
            await db_session.commit()

        before_start_response = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(before_start_response.status_code, 409, before_start_response.text)
        self.assertIn("has not started", before_start_response.json()["detail"])
        async with session_factory()() as db_session:
            self.assertIsNone(
                await db_session.scalar(
                    select(TournamentDeadlockReadyRound.id).where(
                        TournamentDeadlockReadyRound.tournament_id == tournament_id,
                    )
                )
            )
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament.ready_check_starts_at = now - timedelta(seconds=1)
            await db_session.commit()

        with patch.object(tournament_routes, "_invalidate_ready_check_state_cache") as invalidate_cache:
            first_response, second_response = await asyncio.gather(
                organizer["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                organizer["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
            )
        self.assertEqual(
            sorted((first_response.status_code, second_response.status_code)),
            [200, 200],
            {"first": first_response.text, "second": second_response.text},
        )
        self.assertEqual(invalidate_cache.call_count, 1)
        invalidate_cache.assert_called_once_with(tournament_id)
        changed_values = sorted(
            (
                bool(first_response.json()["changed"]),
                bool(second_response.json()["changed"]),
            )
        )
        self.assertEqual(changed_values, [False, True])

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            round_rows = list(
                (
                    await db_session.scalars(
                        select(TournamentDeadlockReadyRound).where(
                            TournamentDeadlockReadyRound.tournament_id == tournament.id,
                        )
                    )
                ).all()
            )
            self.assertEqual(len(round_rows), 1)
            vote_count = await db_session.scalar(
                select(func.count(TournamentDeadlockReadyVote.id)).where(
                    TournamentDeadlockReadyVote.round_id == round_rows[0].id,
                    TournamentDeadlockReadyVote.user_id == organizer["user_id"],
                )
            )
            yes_count = await db_session.scalar(
                select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0)).where(
                    TournamentDeadlockReadyVoteCountShard.round_id == round_rows[0].id,
                    TournamentDeadlockReadyVoteCountShard.choice == "yes",
                )
            )
        self.assertEqual(round_rows[0].status, "active")
        self.assertEqual(vote_count, 1)
        self.assertEqual(yes_count, 1)
        self.assertIsNotNone(tournament.automation_ready_check_started_at)

        close_result = await self._advance_deadlock_automation_for_slug(
            slug,
            now=now + timedelta(minutes=11),
        )
        self.assertEqual(close_result["ready_closed"], 1)
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            closed_round = await db_session.scalar(
                select(TournamentDeadlockReadyRound).where(
                    TournamentDeadlockReadyRound.tournament_id == tournament_id,
                )
            )
        self.assertEqual(closed_round.status, "closed")
        self.assertIsNotNone(tournament.automation_ready_check_closed_at)

    async def test_deadlock_moderation_prunes_active_ready_check_state(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        active_one = await self._register_user(
            label="active-one",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        active_two = await self._register_user(
            label="active-two",
            rank="Phantom",
            subrank=6,
        )
        removed_before_start = await self._register_user(
            label="removed-pre",
            rank="Oracle",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock moderation hardening",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )

        for user in (organizer, active_one, active_two, removed_before_start):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        participants_by_user_id = {
            participant["user_id"]: participant
            for participant in participants_payload
        }

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        withdrawn_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[removed_before_start['user_id']]['id']}/moderation",
                json={
                    "status": "withdrawn",
                    "moderation_note": "Removed before ready-check.",
                },
            ),
            200,
        )
        self.assertEqual(withdrawn_payload["status"], "withdrawn")

        ready_start_payload = self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        self.assertEqual(ready_start_payload["eligible_participant_count"], 3)

        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        withdrawn_vote_attempt = await removed_before_start["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(withdrawn_vote_attempt.status_code, 403, withdrawn_vote_attempt.text)
        self.assertIn(
            "Only joined participants can vote in deadlock ready-check.",
            withdrawn_vote_attempt.json()["detail"],
        )

        disqualified_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[active_two['user_id']]['id']}/moderation",
                json={
                    "status": "disqualified",
                    "moderation_note": "Removed after ready-check vote.",
                },
            ),
            200,
        )
        self.assertEqual(disqualified_payload["status"], "disqualified")

        organizer_ready_state = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check"
            ),
            200,
        )
        self.assertEqual(organizer_ready_state["active_round"]["eligible_participant_count"], 2)
        self.assertEqual(organizer_ready_state["active_round"]["ready_count"], 2)
        self.assertEqual(organizer_ready_state["active_round"]["declined_count"], 0)

        disqualified_ready_state = await active_two["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check"
        )
        self.assertEqual(disqualified_ready_state.status_code, 403, disqualified_ready_state.text)
        self.assertIn(
            "Join the tournament before viewing ready-check state.",
            disqualified_ready_state.json()["detail"],
        )

        disqualified_vote_attempt = await active_two["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(disqualified_vote_attempt.status_code, 403, disqualified_vote_attempt.text)
        self.assertIn(
            "Only joined participants can vote in deadlock ready-check.",
            disqualified_vote_attempt.json()["detail"],
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        closed_vote_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(closed_vote_attempt.status_code, 409, closed_vote_attempt.text)
        self.assertIn("no longer active", closed_vote_attempt.json()["detail"])

        preview_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-preview",
                params={"teams_count": 2},
            ),
            200,
        )
        self.assertEqual(preview_payload["ready_player_count"], 2)
        candidate_user_ids = [candidate["user_id"] for candidate in preview_payload["candidates"]]
        self.assertEqual(candidate_user_ids, [])
        self.assertNotIn(active_two["user_id"], candidate_user_ids)
        self.assertNotIn(removed_before_start["user_id"], candidate_user_ids)

    async def test_deadlock_moderation_reconciles_active_captain_round(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        active_one = await self._register_user(
            label="active-one",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        active_two = await self._register_user(
            label="active-two",
            rank="Phantom",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock captain moderation hardening",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )

        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        participants_by_user_id = {
            participant["user_id"]: participant
            for participant in participants_payload
        }

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        captain_round_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/start",
            json={"teams_count": 2},
        )
        self.assertEqual(captain_round_attempt.status_code, 409, captain_round_attempt.text)
        self.assertIn("At least 14 ready players", captain_round_attempt.json()["detail"])

        disqualified_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[active_one['user_id']]['id']}/moderation",
                json={
                    "status": "disqualified",
                    "moderation_note": "Removed during captain round.",
                },
            ),
            200,
        )
        self.assertEqual(disqualified_payload["status"], "disqualified")

        disqualified_response_attempt = await active_one["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            json={"decision": "accept"},
        )
        self.assertEqual(
            disqualified_response_attempt.status_code,
            403,
            disqualified_response_attempt.text,
        )
        self.assertIn(
            "Only joined participants can respond to captain offers.",
            disqualified_response_attempt.json()["detail"],
        )

        captain_state_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-round"
            ),
            200,
        )
        self.assertIsNone(captain_state_payload["active_round"])
        self.assertIsNone(captain_state_payload["latest_round"])

    async def test_deadlock_captain_decline_is_disabled(self) -> None:
        organizer = await self._register_user(
            label="decline-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-d",
                    "description": "Captain decline disabled.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )

        decline_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            json={"decision": "decline"},
        )
        self.assertEqual(decline_attempt.status_code, 409, decline_attempt.text)
        self.assertIn(
            "Captain decline is disabled",
            decline_attempt.json()["detail"],
        )

    async def test_tournament_deadlock_automation_schedule_drives_ready_captains_and_assignment(self) -> None:
        organizer = await self._register_user(
            label="auto-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label=f"auto-player-{index:02}", rank="Phantom", subrank=6)
            for index in range(1, 14)
        ]
        all_players = [organizer, *players]
        base_time = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        ready_start = base_time
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end

        invalid_payload = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-a",
                "description": "Invalid automation schedule",
                "visibility": "public",
                "format_slug": "solo",
                "ready_check_starts_at": ready_start.isoformat(),
                "captain_selection_starts_at": (ready_start + timedelta(minutes=5)).isoformat(),
                "teams_count": 2,
            },
        )
        self.assertEqual(invalid_payload.status_code, 422, invalid_payload.text)

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "description": "Automated Deadlock flow",
                    "visibility": "public",
                    "format_slug": "solo",
                    "ready_check_starts_at": ready_start.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "teams_count": 2,
                },
            ),
            201,
        )
        self.assertEqual(tournament_payload["status"], "registration_open")
        slug = tournament_payload["slug"]

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        ready_start = datetime.now(UTC) - timedelta(seconds=1)
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament.registration_closes_at = ready_start
            tournament.ready_check_starts_at = ready_start
            tournament.ready_check_ends_at = ready_end
            tournament.captain_selection_starts_at = captain_start
            await db_session.commit()

        ready_start_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_start)
        self.assertEqual(ready_start_result["ready_started"], 1)
        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state["active_round"]["eligible_participant_count"], 14)

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        ready_close_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_end)
        self.assertEqual(ready_close_result["ready_closed"], 1)
        captain_start_result = await self._advance_deadlock_automation_for_slug(slug, now=captain_start)
        self.assertEqual(
            ready_close_result["captain_started"] + captain_start_result["captain_started"],
            1,
            {"ready_close_result": ready_close_result, "captain_start_result": captain_start_result},
        )

        captain_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state["active_round"])
        self.assertEqual(captain_state["latest_round"]["status"], "finalized")
        assignment_result = await self._advance_deadlock_automation_for_slug(
            slug,
            now=captain_start + timedelta(minutes=1),
        )
        self.assertEqual(
            ready_close_result["assignment_generated"]
            + captain_start_result["assignment_generated"]
            + assignment_result["assignment_generated"],
            1,
        )
        assignment_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        self.assertEqual(assignment_state["latest_run"]["status"], "locked")
        self.assertEqual(assignment_state["published_run"]["status"], "locked")
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertEqual(len(bracket_payload["matches"]), 1)
        refreshed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertIsNotNone(refreshed_tournament["automation_assignment_generated_at"])

    async def test_deadlock_tournament_full_player_flow_auto_selects_captains_and_generates_teams(
        self,
    ) -> None:
        organizer = await self._register_user(
            label="full-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label="full-player-01", rank="Phantom", subrank=6, captain_priority="yes"),
            await self._register_user(label="full-player-02", rank="Ascendant", subrank=5, captain_priority="neutral"),
            await self._register_user(label="full-player-03", rank="Oracle", subrank=6, captain_priority="no"),
            await self._register_user(label="full-player-04", rank="Phantom", subrank=5),
            await self._register_user(label="full-player-05", rank="Oracle", subrank=5),
            await self._register_user(label="full-player-06", rank="Emissary", subrank=6),
            await self._register_user(label="full-player-07", rank="Emissary", subrank=5),
            await self._register_user(label="full-player-08", rank="Ritualist", subrank=6),
            await self._register_user(label="full-player-09", rank="Ritualist", subrank=5),
            await self._register_user(label="full-player-10", rank="Mystic", subrank=6),
            await self._register_user(label="full-player-11", rank="Mystic", subrank=5),
            await self._register_user(label="full-player-12", rank="Acolyte", subrank=6),
            await self._register_user(label="full-player-13", rank="Acolyte", subrank=5),
        ]
        all_players = [organizer, *players]

        base_time = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        ready_start = base_time
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Full Deadlock tournament workflow with automatic captains.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "ready_check_starts_at": ready_start.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "teams_count": 2,
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self.assertEqual(tournament_payload["status"], "registration_open")

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        ready_start = datetime.now(UTC) - timedelta(seconds=1)
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament.registration_closes_at = ready_start
            tournament.ready_check_starts_at = ready_start
            tournament.ready_check_ends_at = ready_end
            tournament.captain_selection_starts_at = captain_start
            await db_session.commit()

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(participants_payload), 14)

        ready_start_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_start)
        self.assertEqual(ready_start_result["ready_started"], 1)
        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state["active_round"]["eligible_participant_count"], 14)
        self.assertEqual(ready_state["active_round"]["ready_count"], 0)

        first_ready_user = all_players[0]
        first_yes_payload = self._assert_status(
            await first_ready_user["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "yes"},
            ),
            200,
        )
        self.assertEqual(first_yes_payload["round_id"], ready_state["active_round"]["id"])
        self.assertEqual(first_yes_payload["current_user_choice"], "yes")
        self.assertTrue(first_yes_payload["changed"])
        self.assertNotIn("ready_count", first_yes_payload)
        self.assertNotIn("declined_count", first_yes_payload)
        async with session_factory()() as db_session:
            first_vote_row = await db_session.scalar(
                select(TournamentDeadlockReadyVote).where(
                    TournamentDeadlockReadyVote.round_id == ready_state["active_round"]["id"],
                    TournamentDeadlockReadyVote.user_id == first_ready_user["user_id"],
                )
            )
            self.assertIsNotNone(first_vote_row)
            assert first_vote_row is not None
            first_yes_count = await db_session.scalar(
                select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0)).where(
                    TournamentDeadlockReadyVoteCountShard.round_id == first_vote_row.round_id,
                    TournamentDeadlockReadyVoteCountShard.choice == "yes",
                )
            )
        ready_state_after_first_yes = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_first_yes["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_first_yes["active_round"]["declined_count"], 0)

        with patch.object(tournament_routes, "_invalidate_ready_check_state_cache") as invalidate_cache:
            repeated_yes_payload = self._assert_status(
                await first_ready_user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
        invalidate_cache.assert_not_called()
        self.assertEqual(repeated_yes_payload["current_user_choice"], "yes")
        self.assertFalse(repeated_yes_payload["changed"])
        async with session_factory()() as db_session:
            repeated_vote_row = await db_session.scalar(
                select(TournamentDeadlockReadyVote).where(
                    TournamentDeadlockReadyVote.round_id == first_vote_row.round_id,
                    TournamentDeadlockReadyVote.user_id == first_ready_user["user_id"],
                )
            )
            repeated_yes_count = await db_session.scalar(
                select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0)).where(
                    TournamentDeadlockReadyVoteCountShard.round_id == first_vote_row.round_id,
                    TournamentDeadlockReadyVoteCountShard.choice == "yes",
                )
            )
        self.assertIsNotNone(repeated_vote_row)
        self.assertEqual(repeated_vote_row.id, first_vote_row.id)
        self.assertEqual(repeated_vote_row.choice, first_vote_row.choice)
        self.assertEqual(repeated_vote_row.created_at, first_vote_row.created_at)
        self.assertEqual(repeated_vote_row.updated_at, first_vote_row.updated_at)
        self.assertEqual(repeated_vote_row.responded_at, first_vote_row.responded_at)
        self.assertEqual(first_yes_count, 1)
        self.assertEqual(repeated_yes_count, 1)
        ready_state_after_repeat = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_repeat["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_repeat["active_round"]["declined_count"], 0)

        with patch.object(tournament_routes, "_invalidate_ready_check_state_cache") as invalidate_cache:
            declined_payload = self._assert_status(
                await first_ready_user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "no"},
                ),
                200,
            )
        invalidate_cache.assert_called_once_with(tournament_payload["id"])
        self.assertEqual(declined_payload["current_user_choice"], "no")
        self.assertTrue(declined_payload["changed"])
        ready_state_after_decline = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_decline["active_round"]["ready_count"], 0)
        self.assertEqual(ready_state_after_decline["active_round"]["declined_count"], 1)

        with patch.object(tournament_routes, "_invalidate_ready_check_state_cache") as invalidate_cache:
            restored_yes_payload = self._assert_status(
                await first_ready_user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
        invalidate_cache.assert_called_once_with(tournament_payload["id"])
        self.assertEqual(restored_yes_payload["current_user_choice"], "yes")
        self.assertTrue(restored_yes_payload["changed"])
        ready_state_after_restore = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_restore["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_restore["active_round"]["declined_count"], 0)

        for user in all_players[1:]:
            vote_payload = self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
            self.assertEqual(vote_payload["current_user_choice"], "yes")
            self.assertTrue(vote_payload["changed"])
        ready_state_after_all_votes = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_all_votes["active_round"]["ready_count"], 14)
        self.assertEqual(ready_state_after_all_votes["active_round"]["declined_count"], 0)

        ready_close_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_end)
        captain_start_result = await self._advance_deadlock_automation_for_slug(slug, now=captain_start)
        self.assertEqual(
            ready_close_result["captain_started"] + captain_start_result["captain_started"],
            1,
            {"ready_close_result": ready_close_result, "captain_start_result": captain_start_result},
        )

        captain_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state["active_round"])
        self.assertEqual(captain_state["latest_round"]["status"], "finalized")
        captain_user_ids_to_assign = [
            entry["user_id"]
            for entry in captain_state["latest_round"]["entries"]
            if entry["state"] == "assigned"
        ]
        self.assertEqual(len(captain_user_ids_to_assign), 2)
        self.assertEqual(
            set(captain_user_ids_to_assign),
            {organizer["user_id"], players[0]["user_id"]},
        )

        assignment_result = await self._advance_deadlock_automation_for_slug(
            slug,
            now=captain_start + timedelta(minutes=1),
        )
        self.assertEqual(
            ready_close_result["assignment_generated"]
            + captain_start_result["assignment_generated"]
            + assignment_result["assignment_generated"],
            1,
        )

        captain_final_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertEqual(captain_final_state["latest_round"]["status"], "finalized")
        assigned_captain_ids = {
            entry["user_id"]
            for entry in captain_final_state["latest_round"]["entries"]
            if entry["state"] == "assigned"
        }
        self.assertEqual(assigned_captain_ids, set(captain_user_ids_to_assign))

        assignment_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        latest_run = assignment_state["latest_run"]
        self.assertEqual(latest_run["status"], "locked")
        self.assertEqual(len(latest_run["teams"]), 2)
        self.assertEqual(
            {team["captain"]["user_id"] for team in latest_run["teams"]},
            set(captain_user_ids_to_assign),
        )
        assigned_user_ids = {
            team["captain"]["user_id"]
            for team in latest_run["teams"]
        }
        for team in latest_run["teams"]:
            assigned_user_ids.update(
                slot["assigned_player"]["user_id"]
                for slot in team["starter_slots"]
            )
            if team["reserve_slot"] is not None:
                assigned_user_ids.add(team["reserve_slot"]["assigned_player"]["user_id"])
        self.assertGreaterEqual(len(assigned_user_ids), 13)

        refreshed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertIsNotNone(refreshed_tournament["automation_assignment_generated_at"])
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertEqual(len(bracket_payload["matches"]), 1)
