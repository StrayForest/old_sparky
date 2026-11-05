from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from apps.platform_api.app.api.routes.users import get_me
from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.current_user import (
    serialize_current_user_from_account_read_model,
)
from apps.platform_api.app.services.user_account_read_models import (
    UserAccountReadModelLookup,
    _RESERVE_GENERATION_SCRIPT,
    _SET_IF_CURRENT_GENERATION_SCRIPT,
    _decode,
    _encode,
    _encode_pending,
    _write,
    get_or_build_user_account_read_model,
    user_account_read_model_key,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


def _user(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "user-1",
        "email": "user@example.com",
        "display_name": "Player",
        "status": "active",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "email_verified_at": datetime(2026, 1, 1, tzinfo=UTC),
        "public_tournament_credits": 2,
        "private_tournament_credits": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _account() -> dict[str, object]:
    return {
        "avatar_url": None,
        "avatar_media": None,
        "steam_id": None,
        "steam_linked": False,
        "has_password": False,
        "can_unlink_steam": False,
        "private_tournament_monthly_remaining": 1,
        "private_tournament_monthly_limit": 1,
    }


class UserAccountReadModelTests(PlatformIsolatedAsyncioTestCase):
    def test_serializer_keeps_auth_fields_outside_cached_account_payload(self) -> None:
        response = serialize_current_user_from_account_read_model(
            _user(),
            role_slugs=frozenset({"player"}),
            account={
                **_account(),
                "steam_id": "76561198000000000",
                "steam_linked": True,
                "has_password": True,
                "can_unlink_steam": True,
                "private_tournament_monthly_remaining": 0,
            },
        )

        self.assertIsInstance(response, UserResponse)
        self.assertEqual(response.email, "user@example.com")
        self.assertEqual(response.roles, ["player"])
        self.assertEqual(response.steam_id, "76561198000000000")
        self.assertEqual(response.private_tournament_monthly_remaining, 0)

    async def test_cache_hit_refreshes_quota_before_lookup_returns(self) -> None:
        user = SimpleNamespace(id="user-1")
        client = SimpleNamespace(
            eval=AsyncMock(return_value=_encode(
                revision=10,
                generation="generation-one",
                payload=_account(),
            )),
            aclose=AsyncMock(),
        )
        db_session = SimpleNamespace(execute=AsyncMock())
        quota = AsyncMock(return_value=0)

        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.private_tournament_monthly_remaining",
            new=quota,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            lookup = await get_or_build_user_account_read_model(
                db_session,
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )
            self.assertEqual(lookup.account["private_tournament_monthly_remaining"], 0)
            quota.assert_awaited_once()
            await lookup.write_after_db_release(user_id=user.id)

        client.eval.assert_awaited_once()
        self.assertIs(client.eval.call_args.args[0], _RESERVE_GENERATION_SCRIPT)
        self.assertEqual(client.eval.call_args.args[2], user_account_read_model_key("user-1"))
        db_session.execute.assert_not_awaited()
        event = record_event.call_args.kwargs
        self.assertEqual(event["outcome"], "hit")
        self.assertGreaterEqual(event["get_ms"], 0)
        self.assertEqual(event["build_ms"], 0)
        self.assertEqual(event["set_ms"], 0)
        self.assertEqual(
            set(event),
            {"model", "outcome", "get_ms", "build_ms", "set_ms", "include_payload_metadata"},
        )

    async def test_cache_miss_defers_write_and_records_after_it(self) -> None:
        user = SimpleNamespace(id="private-user-id")
        payload = _account()
        client = SimpleNamespace(
            eval=AsyncMock(side_effect=lambda _script, _keys, _key, pending, _ttl: pending),
            aclose=AsyncMock(),
        )
        write = AsyncMock(return_value=True)

        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.build_user_account_read_model",
            new=AsyncMock(return_value=(10, payload)),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=write,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            lookup = await get_or_build_user_account_read_model(
                SimpleNamespace(),
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )
            write.assert_not_awaited()
            record_event.assert_not_called()
            await lookup.write_after_db_release(user_id=user.id)

        self.assertEqual(lookup.account, payload)
        write.assert_awaited_once_with(
            user.id,
            generation=lookup.pending_generation,
            revision=10,
            payload=payload,
        )
        self.assertEqual(record_event.call_args.kwargs["outcome"], "miss")
        self.assertGreaterEqual(record_event.call_args.kwargs["set_ms"], 0)
        self.assertEqual(
            set(record_event.call_args.kwargs),
            {"model", "outcome", "get_ms", "build_ms", "set_ms", "include_payload_metadata"},
        )
        self.assertNotIn(user.id, repr(record_event.call_args.kwargs))
        self.assertNotIn("private_tournament_monthly_remaining", repr(record_event.call_args.kwargs))

    async def test_redis_reservation_failure_skips_warm_and_returns_db_payload(self) -> None:
        user = SimpleNamespace(id="user-1")
        payload = _account()
        client = SimpleNamespace(
            eval=AsyncMock(side_effect=OSError("private cache detail")),
            aclose=AsyncMock(),
        )
        write = AsyncMock()

        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.build_user_account_read_model",
            new=AsyncMock(return_value=(10, payload)),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=write,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            lookup = await get_or_build_user_account_read_model(
                SimpleNamespace(),
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )
            await lookup.write_after_db_release(user_id=user.id)

        self.assertEqual(lookup.account, payload)
        self.assertIsNone(lookup.pending_generation)
        write.assert_not_awaited()
        self.assertEqual(record_event.call_args.kwargs["outcome"], "read_error")
        self.assertEqual(record_event.call_args.kwargs["set_ms"], 0)
        self.assertNotIn("private cache detail", repr(record_event.call_args.kwargs))

    async def test_writer_reports_lua_generation_rejection_as_not_written(self) -> None:
        client = SimpleNamespace(
            eval=AsyncMock(return_value=0),
            aclose=AsyncMock(),
        )
        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ):
            result = await _write(
                "user-1",
                generation="generation-one",
                revision=10,
                payload=_account(),
            )

        self.assertIs(result, False)
        client.eval.assert_awaited_once()
        client.aclose.assert_awaited_once()

    async def test_guard_rejection_keeps_miss_outcome_and_db_account(self) -> None:
        payload = _account()
        lookup = UserAccountReadModelLookup(
            account=payload,
            get_ms=1.0,
            build_ms=2.0,
            outcome="miss",
            pending_generation="generation-one",
            pending_revision=10,
            pending_payload=payload,
        )
        with patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=AsyncMock(return_value=False),
        ) as write, patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            await lookup.write_after_db_release(user_id="user-1")

        self.assertEqual(lookup.account, payload)
        write.assert_awaited_once()
        self.assertEqual(record_event.call_args.kwargs["outcome"], "miss")

    async def test_build_failure_records_timing_without_cache_write(self) -> None:
        client = SimpleNamespace(
            eval=AsyncMock(side_effect=lambda _script, _keys, _key, pending, _ttl: pending),
            aclose=AsyncMock(),
        )
        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.build_user_account_read_model",
            new=AsyncMock(side_effect=RuntimeError("build failure")),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            with self.assertRaisesRegex(RuntimeError, "build failure"):
                await get_or_build_user_account_read_model(
                    SimpleNamespace(),
                    user=SimpleNamespace(id="user-1"),
                    now=datetime(2026, 1, 2, tzinfo=UTC),
                )

        self.assertEqual(record_event.call_args.kwargs["outcome"], "build_error")
        self.assertEqual(record_event.call_args.kwargs["set_ms"], 0)

    async def test_route_releases_db_after_materializing_response_before_blocked_set(self) -> None:
        entered_write = asyncio.Event()
        continue_write = asyncio.Event()
        released = False
        account = _account()
        lookup = UserAccountReadModelLookup(
            account=account,
            get_ms=1.0,
            build_ms=2.0,
            outcome="miss",
            pending_generation="generation-one",
            pending_revision=10,
            pending_payload=account,
        )
        db_session = SimpleNamespace(execute=AsyncMock())

        async def release(_session: object) -> None:
            nonlocal released
            released = True

        async def blocked_write(*_args: object, **_kwargs: object) -> bool:
            self.assertTrue(released)
            entered_write.set()
            await continue_write.wait()
            return False

        with patch(
            "apps.platform_api.app.api.routes.users.get_or_build_user_account_read_model",
            new=AsyncMock(return_value=lookup),
        ), patch(
            "apps.platform_api.app.api.routes.users.release_db_connection",
            new=AsyncMock(side_effect=release),
        ) as release_mock, patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=blocked_write,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            task = asyncio.create_task(
                get_me(
                    SimpleNamespace(
                        user=_user(),
                        role_slugs=frozenset({"player"}),
                        now=datetime(2026, 1, 2, tzinfo=UTC),
                    ),
                    db_session,
                )
            )
            await asyncio.wait_for(entered_write.wait(), timeout=2)
            release_mock.assert_awaited_once()
            db_session.execute.assert_not_awaited()
            continue_write.set()
            response = await task

        self.assertIsInstance(response, UserResponse)
        self.assertEqual(response.email, "user@example.com")
        self.assertEqual(response.roles, ["player"])
        self.assertEqual(release_mock.await_count, 2)
        self.assertEqual(record_event.call_args.kwargs["outcome"], "miss")

    async def test_route_records_failed_set_duration_once_when_final_release_raises(self) -> None:
        account = _account()
        lookup = UserAccountReadModelLookup(
            account=account,
            get_ms=1.0,
            build_ms=2.0,
            outcome="miss",
            pending_generation="generation-one",
            pending_revision=10,
            pending_payload=account,
        )
        release_count = 0

        async def release(_session: object) -> None:
            nonlocal release_count
            release_count += 1
            if release_count == 2:
                raise RuntimeError("session close failed")

        with patch(
            "apps.platform_api.app.api.routes.users.get_or_build_user_account_read_model",
            new=AsyncMock(return_value=lookup),
        ), patch(
            "apps.platform_api.app.api.routes.users.release_db_connection",
            new=AsyncMock(side_effect=release),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=AsyncMock(side_effect=RuntimeError("unexpected cache write failure")),
        ) as write, patch(
            "apps.platform_api.app.services.user_account_read_models.perf_counter",
            side_effect=(10.0, 10.25),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            with self.assertRaisesRegex(RuntimeError, "session close failed"):
                await get_me(
                    SimpleNamespace(
                        user=_user(),
                        role_slugs=frozenset({"player"}),
                        now=datetime(2026, 1, 2, tzinfo=UTC),
                    ),
                    SimpleNamespace(),
                )

        write.assert_awaited_once()
        self.assertEqual(release_count, 2)
        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.kwargs["outcome"], "write_error")
        self.assertEqual(record_event.call_args.kwargs["set_ms"], 250.0)

    async def test_route_records_cancelled_set_timing_once_and_propagates_cancel(self) -> None:
        account = _account()
        lookup = UserAccountReadModelLookup(
            account=account,
            get_ms=1.0,
            build_ms=2.0,
            outcome="miss",
            pending_generation="generation-one",
            pending_revision=10,
            pending_payload=account,
        )
        with patch(
            "apps.platform_api.app.api.routes.users.get_or_build_user_account_read_model",
            new=AsyncMock(return_value=lookup),
        ), patch(
            "apps.platform_api.app.api.routes.users.release_db_connection",
            new=AsyncMock(),
        ) as release, patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ) as write, patch(
            "apps.platform_api.app.services.user_account_read_models.perf_counter",
            side_effect=(20.0, 20.5),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            with self.assertRaises(asyncio.CancelledError):
                await get_me(
                    SimpleNamespace(
                        user=_user(),
                        role_slugs=frozenset({"player"}),
                        now=datetime(2026, 1, 2, tzinfo=UTC),
                    ),
                    SimpleNamespace(),
                )

        write.assert_awaited_once()
        self.assertEqual(release.await_count, 2)
        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.kwargs["outcome"], "write_error")
        self.assertEqual(record_event.call_args.kwargs["set_ms"], 500.0)


class UserAccountReadModelRedisIntegrationTests(PlatformIsolatedAsyncioTestCase):
    async def test_real_lua_generation_guard_rejects_stale_and_expired_writers(self) -> None:
        from redis.asyncio import from_url

        from python_packages.platform_infra.config import get_settings

        user_id = f"integration-{uuid4().hex}"
        key = user_account_read_model_key(user_id)
        client = from_url(get_settings().platform_redis_url, decode_responses=False)
        stale_generation = "stale-generation"
        fresh_generation = "fresh-generation"
        payload = _account()

        async def reserve(generation: str) -> bytes:
            return await client.eval(
                _RESERVE_GENERATION_SCRIPT,
                1,
                key,
                _encode_pending(generation=generation),
                "60",
            )

        async def write(generation: str, revision: int, value: dict[str, object]) -> int:
            return await client.eval(
                _SET_IF_CURRENT_GENERATION_SCRIPT,
                1,
                key,
                generation,
                str(revision),
                _encode(revision=revision, generation=generation, payload=value),
                "60",
            )

        try:
            await reserve(stale_generation)
            self.assertEqual(await write(stale_generation, 10, {**payload, "steam_linked": True}), 1)

            # The existing invalidation helper deletes the same key; an old
            # in-flight writer must not repopulate the new generation.
            from apps.platform_api.app.services.user_account_read_models import (
                delete_user_account_read_model,
            )

            await delete_user_account_read_model(user_id)
            await reserve(fresh_generation)
            self.assertEqual(await write(fresh_generation, 20, payload), 1)
            self.assertEqual(await write(fresh_generation, 19, {**payload, "steam_linked": True}), 0)
            self.assertEqual(await write(stale_generation, 30, {**payload, "steam_linked": True}), 0)
            current = _decode(await client.get(key))
            self.assertEqual(current, ("ready", fresh_generation, 20, payload))

            expired_generation = "expired-generation"
            await delete_user_account_read_model(user_id)
            await reserve(expired_generation)
            await client.pexpire(key, 0)
            self.assertEqual(await write(expired_generation, 40, payload), 0)

            legacy_payload = b'10\n{"steam_linked":true}'
            await client.set(key, legacy_payload, ex=60)
            replacement = await reserve("legacy-replacement")
            self.assertEqual(_decode(replacement), ("pending", "legacy-replacement"))
            self.assertNotEqual(replacement, legacy_payload)
        finally:
            try:
                await client.unlink(key)
            finally:
                await client.aclose()


if __name__ == "__main__":
    unittest.main()
