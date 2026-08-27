from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apps.platform_api.app.api.routes import ready_check
from apps.platform_api.app.services import ready_check_events
from python_packages.platform_infra.sse_connection_limit import (
    READY_CHECK_SSE_USER_LIMIT,
    SSE_CONNECTION_LEASE_SCOPE,
    SseConnectionLease,
)


class PlatformReadyCheckStateProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_probe_is_one_redis_get_and_no_database_dependency(self) -> None:
        client = MagicMock()
        client.get = AsyncMock(return_value='{"revision":184,"status":"waiting"}')
        client.aclose = AsyncMock()
        with (
            patch.object(ready_check_events, "redis_client", return_value=client),
            patch.object(ready_check_events, "_state_key", return_value="state-key"),
        ):
            state = await ready_check_events.read_ready_check_state(
                tournament_id="tournament-1",
                user_id="user-1",
                ready_check_starts_at=1_700_000_000,
            )

        self.assertEqual(state, {"revision": 184, "status": "waiting"})
        client.get.assert_awaited_once_with("state-key")
        client.aclose.assert_awaited_once_with()


class PlatformReadyCheckEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_emits_resync_when_relay_window_evicts_unconsumed_event(self) -> None:
        relay = SimpleNamespace(
            closed=False,
            messages=deque(maxlen=ready_check_events.READY_CHECK_RELAY_QUEUE_MAXSIZE),
            next_sequence=0,
            message_event=asyncio.Event(),
        )
        with patch.object(ready_check_events, "_get_relay", AsyncMock(return_value=relay)):
            stream = ready_check_events.stream_ready_check_events(("tournament-1",))
            self.assertIn("event: connected", await anext(stream))

            relay.messages.append((1, "tournament-1", "event: ready_check\ndata: {\"status\":\"active\"}\n\n"))
            relay.next_sequence = 1
            for sequence in range(2, ready_check_events.READY_CHECK_RELAY_QUEUE_MAXSIZE + 2):
                relay.messages.append((sequence, f"unrelated-{sequence}", "event: ready_check\ndata: {}\n\n"))
                relay.next_sequence = sequence

            self.assertEqual(
                await anext(stream),
                ready_check_events.READY_CHECK_RELAY_RESYNC_EVENT,
            )
            await stream.aclose()

    async def test_stream_delivers_retained_relevant_event_without_resync(self) -> None:
        relay = SimpleNamespace(
            closed=False,
            messages=deque(maxlen=ready_check_events.READY_CHECK_RELAY_QUEUE_MAXSIZE),
            next_sequence=0,
            message_event=asyncio.Event(),
        )
        with patch.object(ready_check_events, "_get_relay", AsyncMock(return_value=relay)):
            stream = ready_check_events.stream_ready_check_events(("tournament-1",))
            self.assertIn("event: connected", await anext(stream))
            relay.next_sequence = 1
            relay.messages.append(
                (1, "tournament-1", "event: ready_check\ndata: {\"status\":\"active\"}\n\n")
            )

            self.assertIn("event: ready_check", await anext(stream))
            await stream.aclose()

    async def test_idle_stream_renews_lease_past_120_seconds(self) -> None:
        relay = SimpleNamespace(
            closed=False,
            messages=deque(),
            next_sequence=0,
            message_event=asyncio.Event(),
        )
        lease = MagicMock(spec=SseConnectionLease)
        lease.renew = AsyncMock()
        virtual_time = 0.0

        async def timeout_without_waiting(awaitable, *, timeout):
            del timeout
            awaitable.close()
            nonlocal virtual_time
            virtual_time += 15.0
            raise TimeoutError

        with (
            patch.object(ready_check_events, "_get_relay", AsyncMock(return_value=relay)),
            patch.object(ready_check_events, "monotonic", side_effect=lambda: virtual_time),
            patch.object(ready_check_events.asyncio, "wait_for", new=timeout_without_waiting),
        ):
            stream = ready_check_events.stream_ready_check_events(
                ("tournament-1",),
                connection_lease=lease,
            )
            self.assertIn("event: connected", await anext(stream))
            for _ in range(9):
                self.assertEqual(await anext(stream), ": keepalive\n\n")
            await stream.aclose()

        self.assertGreater(virtual_time, 120.0)
        # The stream calls renew at each keepalive checkpoint; the lease's
        # own renewal interval suppresses redundant Redis writes.
        self.assertEqual(lease.renew.await_count, 9)

    async def test_stream_closes_when_ready_check_lease_renewal_fails(self) -> None:
        relay = SimpleNamespace(
            closed=False,
            messages=deque(),
            next_sequence=0,
            message_event=asyncio.Event(),
        )
        lease = MagicMock(spec=SseConnectionLease)
        lease.renew = AsyncMock(
            side_effect=ready_check_events.SseConnectionLeaseRenewalFailed("expired")
        )
        virtual_time = 0.0

        async def timeout_without_waiting(awaitable, *, timeout):
            del timeout
            awaitable.close()
            nonlocal virtual_time
            virtual_time += 15.0
            raise TimeoutError

        with (
            patch.object(ready_check_events, "_get_relay", AsyncMock(return_value=relay)),
            patch.object(ready_check_events, "monotonic", side_effect=lambda: virtual_time),
            patch.object(ready_check_events.asyncio, "wait_for", new=timeout_without_waiting),
        ):
            stream = ready_check_events.stream_ready_check_events(
                ("tournament-1",),
                connection_lease=lease,
            )
            await anext(stream)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

        lease.renew.assert_awaited_once_with()


class PlatformReadyCheckRouteGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_round_does_not_reintroduce_post_start_late_sse(self) -> None:
        starts_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        tournament = SimpleNamespace(
            id="tournament-a",
            slug="a",
            ready_check_starts_at=starts_at,
            ready_check_ends_at=starts_at + timedelta(minutes=20),
        )
        active_round = SimpleNamespace(
            tournament_id="tournament-a",
            eligible_user_ids=["user-1"],
        )

        class FakeResult:
            def __init__(self, rows):
                self.rows = rows

            def scalars(self):
                return self

            def all(self):
                return self.rows

        class FakeDbSession:
            def __init__(self):
                self.execute_calls = 0

            async def execute(self, _statement):
                self.execute_calls += 1
                if self.execute_calls == 1:
                    return FakeResult([tournament])
                if self.execute_calls == 2:
                    return FakeResult([tournament])
                return FakeResult([("tournament-a", 1)])

            async def scalars(self, _statement):
                return FakeResult([active_round])

        auth_session = SimpleNamespace(
            now=starts_at + timedelta(seconds=1),
            user=SimpleNamespace(id="user-1"),
            session=SimpleNamespace(id="session-1"),
        )

        with (
            patch.object(ready_check, "_stream_cookie", return_value="session-token"),
            patch.object(ready_check, "current_ready_check_sse_connection_count", new=AsyncMock(return_value=0)),
            patch.object(ready_check, "qa_sse_capacity_limit", return_value=3_000),
            patch.object(ready_check, "issue_ready_check_state_proof", return_value="state-proof"),
            patch.object(ready_check, "ready_check_user_admission", return_value=(
                starts_at,
                "polling",
                "polling",
            )),
        ):
            agenda = await ready_check.get_ready_check_agenda(
                SimpleNamespace(),
                response=MagicMock(),
                auth_session=auth_session,
                db_session=FakeDbSession(),
            )

        self.assertEqual(agenda.checks[0].admission_mode, "polling")

    async def test_agenda_quotas_use_the_global_simultaneous_tournament_demand(self) -> None:
        starts_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        tournament_a = SimpleNamespace(
            id="tournament-a",
            slug="a",
            ready_check_starts_at=starts_at,
            ready_check_ends_at=starts_at + timedelta(minutes=20),
        )
        tournament_b = SimpleNamespace(
            id="tournament-b",
            slug="b",
            ready_check_starts_at=starts_at,
            ready_check_ends_at=starts_at + timedelta(minutes=20),
        )

        class FakeResult:
            def __init__(self, rows):
                self.rows = rows

            def scalars(self):
                return self

            def all(self):
                return self.rows

        class FakeDbSession:
            def __init__(self):
                self.execute_calls = 0

            async def execute(self, _statement):
                self.execute_calls += 1
                if self.execute_calls == 1:
                    return FakeResult([tournament_a])
                if self.execute_calls == 2:
                    return FakeResult([tournament_a, tournament_b])
                return FakeResult([("tournament-a", 7_000), ("tournament-b", 5_000)])

            async def scalars(self, _statement):
                return FakeResult([])

        auth_session = SimpleNamespace(
            now=starts_at - timedelta(minutes=1),
            user=SimpleNamespace(id="user-1"),
            session=SimpleNamespace(id="session-1"),
        )
        request = SimpleNamespace()
        db_session = FakeDbSession()

        with (
            patch.object(ready_check, "_stream_cookie", return_value="session-token"),
            patch.object(ready_check, "current_ready_check_sse_connection_count", new=AsyncMock(return_value=0)),
            patch.object(ready_check, "qa_sse_capacity_limit", return_value=10_000),
            patch.object(ready_check, "issue_ready_check_state_proof", return_value="state-proof"),
            patch.object(ready_check, "ready_check_user_admission", return_value=(
                starts_at,
                "polling",
                "polling",
            )) as user_admission,
        ):
            agenda = await ready_check.get_ready_check_agenda(
                request,
                response=MagicMock(),
                auth_session=auth_session,
                db_session=db_session,
            )

        self.assertEqual(agenda.checks[0].tournament_id, "tournament-a")
        self.assertEqual(user_admission.call_args.args[0].expected_demand, 12_000)
        self.assertEqual(user_admission.call_args.kwargs["sse_quota"], 5_833)

    async def test_ready_check_stream_uses_one_connection_per_authenticated_user(self) -> None:
        request = SimpleNamespace(scope={})
        proof = SimpleNamespace(
            admission_open_at=0,
            tournament_ids=("tournament-1",),
            user_id="user-1",
        )
        lease = MagicMock(spec=SseConnectionLease)
        request.scope[SSE_CONNECTION_LEASE_SCOPE] = lease

        async def empty_stream(*_args, **_kwargs):
            if False:
                yield ""

        with (
            patch.object(ready_check, "verify_ready_check_stream_proof", return_value=proof),
            patch.object(ready_check, "_stream_cookie", return_value="session-token"),
            patch.object(ready_check, "add_sse_authenticated_user_scope", new=AsyncMock()) as add_user,
            patch.object(ready_check, "stream_ready_check_events", side_effect=empty_stream) as stream_events,
        ):
            response = await ready_check.get_ready_check_events(request, "proof")

        self.assertEqual(response.media_type, "text/event-stream")
        add_user.assert_awaited_once_with(
            request,
            "user-1",
            user_limit=READY_CHECK_SSE_USER_LIMIT,
            user_scope="ready_check",
        )
        stream_events.assert_called_once_with(
            ("tournament-1",),
            connection_lease=lease,
        )
