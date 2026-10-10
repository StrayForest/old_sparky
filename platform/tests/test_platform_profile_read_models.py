from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from fastapi import HTTPException
from starlette.requests import Request

from apps.platform_api.app.api.routes import tournaments as tournament_routes
from apps.platform_api.app.services import (
    profile_read_models,
    tournament_profile_access,
)
from apps.platform_api.app.services.tournament_profile_access import (
    TournamentProfilePipelineResult,
)
from python_packages.platform_infra.media.r2_storage import R2Storage
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _Row(tuple):
    def __new__(cls, values: tuple[object, ...], **labels: object):
        instance = super().__new__(cls, values)
        for name, value in labels.items():
            setattr(instance, name, value)
        return instance


class _Pipeline:
    def __init__(self, values: list[object]):
        self.values = values
        self.commands: list[tuple[str, tuple[object, ...]]] = []

    def get(self, key: str) -> None:
        self.commands.append(("get", (key,)))

    def sismember(self, key: str, member: str) -> None:
        self.commands.append(("sismember", (key, member)))

    async def execute(self) -> list[object]:
        return self.values

    async def __aenter__(self) -> "_Pipeline":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Redis:
    def __init__(self, *, pipeline_values: list[object] | None = None) -> None:
        self.pipeline_values = pipeline_values or []
        self.pipeline_calls: list[bool] = []
        self.pipelines: list[_Pipeline] = []
        self.closed = False

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        self.pipeline_calls.append(transaction)
        pipeline = _Pipeline(self.pipeline_values)
        self.pipelines.append(pipeline)
        return pipeline

    async def aclose(self) -> None:
        self.closed = True


class _JoinDeltaRedis:
    def __init__(
        self,
        *,
        access: bytes,
        viewers: set[str] | None = None,
        roster: set[str] | None = None,
        fail_after_sadd: bool = False,
    ) -> None:
        self.access = access
        self.viewers = set(viewers or ())
        self.roster = set(roster or ())
        self.fail_after_sadd = fail_after_sadd
        self.eval_calls: list[tuple[object, ...]] = []
        self.closed = False
        self._lock = asyncio.Lock()

    async def eval(self, script: str, key_count: int, *args: object) -> int:
        async with self._lock:
            self.eval_calls.append((script, key_count, *args))
            self_key, viewers_key, roster_key, expected, incoming, user_id, _ttl = args
            self.assert_delta_keys(key_count, self_key, viewers_key, roster_key)
            current = json.loads(self.access)
            if current.get("profile_access_generation") != int(str(expected)):
                return 0
            viewer_count = current.get("viewer_count")
            roster_count = current.get("roster_count")
            if (
                type(viewer_count) is not int
                or type(roster_count) is not int
                or viewer_count != len(self.viewers)
                or roster_count != len(self.roster)
            ):
                return 0
            if str(user_id) in self.viewers:
                return 0
            self.viewers.add(str(user_id))
            if self.fail_after_sadd:
                raise ConnectionError("simulated Redis write failure")
            current["profile_access_generation"] = int(str(incoming))
            current["viewer_count"] = viewer_count + 1
            self.access = json.dumps(current, separators=(",", ":")).encode()
            return 1

    @staticmethod
    def assert_delta_keys(
        key_count: int,
        access_key: object,
        viewers_key: object,
        roster_key: object,
    ) -> None:
        if key_count != 3:
            raise AssertionError(f"unexpected Redis key count: {key_count}")
        if not str(access_key).startswith(tournament_profile_access.PROFILE_ACCESS_KEY_PREFIX):
            raise AssertionError("unexpected profile access key")
        if not str(viewers_key).startswith(tournament_profile_access.PROFILE_VIEWERS_KEY_PREFIX):
            raise AssertionError("unexpected profile viewers key")
        if not str(roster_key).startswith(tournament_profile_access.PROFILE_ROSTER_KEY_PREFIX):
            raise AssertionError("unexpected profile roster key")

    async def aclose(self) -> None:
        self.closed = True


class _ProfileAccessPurgeRedis:
    def __init__(self, keys: set[bytes], *, page_size: int = 2) -> None:
        self.keys = set(keys)
        self.page_size = page_size
        self.scan_calls: list[tuple[int | bytes | str, str, int]] = []
        self.unlink_calls: list[tuple[bytes, ...]] = []
        self.closed = False

    async def scan(
        self,
        *,
        cursor: int | bytes | str,
        match: str,
        count: int,
    ) -> tuple[int, list[bytes]]:
        self.scan_calls.append((cursor, match, count))
        prefix = match[:-1].encode("ascii")
        matching = sorted(key for key in self.keys if key.startswith(prefix))
        offset = int(cursor)
        page = matching[offset : offset + self.page_size]
        next_offset = offset + len(page)
        return (0 if next_offset >= len(matching) else next_offset), page

    async def unlink(self, *keys: bytes) -> int:
        self.unlink_calls.append(tuple(keys))
        removed = 0
        for key in keys:
            if key in self.keys:
                self.keys.remove(key)
                removed += 1
        return removed

    async def aclose(self) -> None:
        self.closed = True


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class _CasRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.closed = False

    async def eval(
        self,
        _script: str,
        _key_count: int,
        key: str,
        revision: str,
        payload: bytes,
        _ttl: str,
    ) -> int:
        current = profile_read_models._decode_envelope(self.store.get(key))
        if current is None or current.revision <= int(revision):
            self.store[key] = payload
            return 1
        return 0

    async def aclose(self) -> None:
        self.closed = True


class _SingleflightRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes | str] = {}
        self.set_calls: list[tuple[str, str | bytes, int | None, bool]] = []
        self.closed = False

    async def set(
        self,
        key: str,
        value: str | bytes,
        *,
        px: int | None = None,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self.set_calls.append((key, value, px if px is not None else ex, nx))
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def get(self, key: str) -> bytes | str | None:
        return self.store.get(key)

    async def eval(self, script: str, key_count: int, *args: object) -> int:
        self.assert_key_count(key_count)
        if "current_revision" in script:
            key, revision, payload, _ttl = args
            current = profile_read_models._decode_envelope(self.store.get(str(key)))
            if current is None or current.revision <= int(str(revision)):
                self.store[str(key)] = payload  # type: ignore[assignment]
                return 1
            return 0
        key, token = args
        if self.store.get(str(key)) == token:
            del self.store[str(key)]
            return 1
        return 0

    @staticmethod
    def assert_key_count(key_count: int) -> None:
        if key_count != 1:
            raise AssertionError(f"unexpected Redis key count: {key_count}")

    async def aclose(self) -> None:
        self.closed = True


class ProfileReadModelTests(PlatformIsolatedAsyncioTestCase):
    @staticmethod
    def _auth_session() -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(id="viewer-1"),
            role_slugs=frozenset(),
        )

    def _ready_access_raw(self) -> bytes:
        return json.dumps(
            {
                "tournament_id": "tournament-1",
                "organizer_user_id": "organizer-1",
                "roster_ready": True,
                "profile_access_generation": 7,
            }
        ).encode()

    @staticmethod
    def _profile_request(generation: int | None = 7) -> Request:
        state = {}
        if generation is not None:
            state["tournament_profile_access_generation"] = generation
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/api/v1/tournaments/night-veil/profiles/target-1",
                "raw_path": b"/api/v1/tournaments/night-veil/profiles/target-1",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
                "state": state,
            }
        )

    async def test_warm_route_returns_cached_bytes_without_database_session(self) -> None:
        payload = b'{"profile":{"user_id":"target-1"},"deadlock_profile":null}'
        pipeline = TournamentProfilePipelineResult(
            access_raw=self._ready_access_raw(),
            requester_is_viewer=True,
            target_is_roster_member=True,
            profile_raw=b"11\n" + payload,
            redis_available=True,
            pipeline_ms=0.3,
        )
        with (
            patch.object(
                tournament_routes,
                "read_tournament_profile_pipeline",
                AsyncMock(return_value=pipeline),
            ),
            patch.object(tournament_routes, "session_factory") as session_factory,
            patch.object(
                R2Storage,
                "head",
                side_effect=AssertionError("profile reads must not call R2"),
            ) as r2_head,
        ):
            response = await tournament_routes.get_tournament_scoped_profile(
                "night-veil",
                "target-1",
                self._profile_request(),
                self._auth_session(),
            )

        self.assertEqual(response.body, payload)
        session_factory.assert_not_called()
        r2_head.assert_not_called()

    async def test_profile_miss_builds_once_after_access_hit(self) -> None:
        payload = b'{"profile":{"user_id":"target-1"},"deadlock_profile":null}'
        pipeline = TournamentProfilePipelineResult(
            access_raw=self._ready_access_raw(),
            requester_is_viewer=True,
            target_is_roster_member=True,
            profile_raw=None,
            redis_available=True,
            pipeline_ms=0.3,
        )
        with (
            patch.object(
                tournament_routes,
                "read_tournament_profile_pipeline",
                AsyncMock(return_value=pipeline),
            ),
            patch.object(
                tournament_routes,
                "get_or_build_profile_read_model",
                AsyncMock(return_value=payload),
            ) as get_or_build,
        ):
            response = await tournament_routes.get_tournament_scoped_profile(
                "night-veil",
                "target-1",
                self._profile_request(),
                self._auth_session(),
            )

        self.assertEqual(response.body, payload)
        get_or_build.assert_awaited_once_with("target-1")

    async def test_full_cold_route_uses_only_access_and_profile_db_builds(self) -> None:
        payload = b'{"profile":{"user_id":"target-1"},"deadlock_profile":null}'
        pipeline = TournamentProfilePipelineResult(
            access_raw=None,
            requester_is_viewer=False,
            target_is_roster_member=False,
            profile_raw=None,
            redis_available=False,
            pipeline_ms=0.3,
        )
        state = tournament_profile_access.TournamentProfileAccessState(
            tournament_id="tournament-1",
            organizer_user_id="organizer-1",
            roster_ready=True,
            profile_access_generation=7,
            viewer_user_ids=frozenset({"viewer-1"}),
            roster_user_ids=frozenset({"target-1"}),
        )
        def session_factory():
            def make_session():
                return _SessionContext()

            return make_session

        with (
            patch.object(
                tournament_routes,
                "read_tournament_profile_pipeline",
                AsyncMock(return_value=pipeline),
            ),
            patch.object(
                tournament_routes,
                "refresh_tournament_profile_access_state",
                AsyncMock(return_value=state),
            ) as refresh_access,
            patch.object(
                tournament_routes,
                "session_factory",
                side_effect=session_factory,
            ) as sessions,
            patch.object(
                tournament_routes,
                "get_or_build_profile_read_model",
                AsyncMock(return_value=payload),
            ) as get_or_build,
        ):
            response = await tournament_routes.get_tournament_scoped_profile(
                "night-veil",
                "target-1",
                self._profile_request(),
                self._auth_session(),
            )

        self.assertEqual(response.body, payload)
        self.assertEqual(sessions.call_count, 1)
        refresh_access.assert_awaited_once()
        get_or_build.assert_awaited_once_with("target-1")

    async def test_access_semantics_are_checked_before_profile_build(self) -> None:
        cases = (
            (False, True, 403),
            (True, True, 409),
            (True, False, 404),
        )
        for requester_is_viewer, target_is_roster_member, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                pipeline = TournamentProfilePipelineResult(
                    access_raw=self._ready_access_raw()
                    if expected_status != 409
                    else self._ready_access_raw().replace(b"true", b"false", 1),
                    requester_is_viewer=requester_is_viewer,
                    target_is_roster_member=target_is_roster_member,
                    profile_raw=b"11\n{}",
                    redis_available=True,
                    pipeline_ms=0.3,
                )
                with patch.object(
                    tournament_routes,
                    "read_tournament_profile_pipeline",
                    AsyncMock(return_value=pipeline),
                ), patch.object(
                    tournament_routes,
                    "session_factory",
                ) as session_factory:
                    with self.assertRaises(HTTPException) as raised:
                        await tournament_routes.get_tournament_scoped_profile(
                            "night-veil",
                            "target-1",
                            self._profile_request(),
                            self._auth_session(),
                        )

                self.assertEqual(raised.exception.status_code, expected_status)
                session_factory.assert_not_called()

        stale_pipeline = TournamentProfilePipelineResult(
            access_raw=self._ready_access_raw(),
            requester_is_viewer=True,
            target_is_roster_member=True,
            profile_raw=b"11\n{}",
            redis_available=True,
            pipeline_ms=0.3,
        )
        current_state = tournament_profile_access.TournamentProfileAccessState(
            tournament_id="tournament-1",
            organizer_user_id="organizer-1",
            roster_ready=True,
            profile_access_generation=8,
            viewer_user_ids=frozenset(),
            roster_user_ids=frozenset({"target-1"}),
        )

        def session_factory():
            def make_session():
                return _SessionContext()

            return make_session

        with (
            patch.object(
                tournament_routes,
                "read_tournament_profile_pipeline",
                AsyncMock(return_value=stale_pipeline),
            ),
            patch.object(
                tournament_routes,
                "refresh_tournament_profile_access_state",
                AsyncMock(return_value=current_state),
            ) as refresh_access,
            patch.object(
                tournament_routes,
                "session_factory",
                side_effect=session_factory,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await tournament_routes.get_tournament_scoped_profile(
                    "night-veil",
                    "target-1",
                    self._profile_request(generation=8),
                    self._auth_session(),
                )

        self.assertEqual(raised.exception.status_code, 403)
        refresh_access.assert_awaited_once()

    async def test_redis_pipeline_failure_is_explicitly_a_fallback_signal(self) -> None:
        class _FailingRedis(_Redis):
            def pipeline(self, *, transaction: bool) -> _Pipeline:
                pipeline = super().pipeline(transaction=transaction)

                async def fail_execute() -> list[object]:
                    raise ConnectionError("redis unavailable")

                pipeline.execute = fail_execute
                return pipeline

        redis = _FailingRedis()
        with patch.object(tournament_profile_access, "redis_client", return_value=redis):
            result = await tournament_profile_access.read_tournament_profile_pipeline(
                slug="night-veil",
                current_user_id="viewer-1",
                target_user_id="target-1",
                profile_key="platform:profile:read-model:v1:target-1",
            )

        self.assertFalse(result.redis_available)
        self.assertIsNone(result.access_raw)
        self.assertTrue(redis.closed)

    async def test_profile_builder_uses_one_statement_and_exact_cached_contract(self) -> None:
        updated_at = datetime(2026, 8, 31, tzinfo=UTC)
        profile = SimpleNamespace(
            user_id="user-1",
            display_name="Player One",
            handle="player-one",
            avatar_url=None,
            banner_url=None,
            avatar_asset_id=None,
            banner_asset_id=None,
            bio="bio",
            region="EU",
            steam_id="76561198000000000",
            discord_account="player#1",
            captain_team_name=None,
            updated_at=updated_at,
        )
        deadlock = SimpleNamespace(
            user_id="user-1",
            rank="Oracle",
            subrank=3,
            playtime="1501-2000",
            roles=["Carry"],
            pool=["Abrams"],
            captain_priority="yes",
            updated_at=updated_at,
        )
        row = _Row(
            (profile, deadlock, None, None, [], []),
            avatar_variants=[],
            banner_variants=[],
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
        )

        model = await profile_read_models._build_profile_with_session(session, "user-1")

        session.execute.assert_awaited_once()
        self.assertIsNotNone(model)
        assert model is not None
        payload = json.loads(model.payload)
        self.assertEqual(set(payload), {"profile", "deadlock_profile"})
        self.assertEqual(payload["profile"]["user_id"], "user-1")
        self.assertEqual(payload["deadlock_profile"]["rank"], "Oracle")
        self.assertNotIn("dream_slots", payload)
        self.assertNotIn("stats", payload)

    async def test_auth_avatar_projection_selects_one_ready_variant_without_json_aggregate(self) -> None:
        row = _Row(
            ("ready", "avatars/user-1/avatar-256.webp"),
            avatar_status="ready",
            avatar_object_key="avatars/user-1/avatar-256.webp",
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: row))
        )

        with patch.object(
            profile_read_models,
            "public_media_url",
            return_value="https://cdn.old-sparky.com/avatars/user-1/avatar-256.webp",
        ) as public_url:
            projection = await profile_read_models.get_profile_avatar_read_model(
                session,
                "user-1",
            )

        statement = str(session.execute.await_args.args[0]).lower()
        self.assertNotIn("json_agg", statement)
        self.assertEqual(
            projection.avatar_url,
            "https://cdn.old-sparky.com/avatars/user-1/avatar-256.webp",
        )
        self.assertIsNone(projection.avatar_media)
        public_url.assert_called_once_with("avatars/user-1/avatar-256.webp")

    async def test_tournament_profile_pipeline_is_one_redis_round_trip(self) -> None:
        access = b'{"tournament_id":"tournament-1","organizer_user_id":"organizer","roster_ready":true,"profile_access_generation":7}'
        redis = _Redis(pipeline_values=[access, True, True, b"9\n{}"])
        with patch.object(
            tournament_profile_access,
            "redis_client",
            return_value=redis,
        ):
            result = await tournament_profile_access.read_tournament_profile_pipeline(
                slug="night-veil",
                current_user_id="viewer",
                target_user_id="target",
                profile_key="platform:profile:read-model:v1:target",
            )

        self.assertTrue(result.redis_available)
        self.assertEqual(redis.pipeline_calls, [False])
        self.assertEqual(
            [command[0] for command in redis.pipelines[0].commands],
            ["get", "sismember", "sismember", "get"],
        )
        self.assertEqual(result.access_raw, access)
        self.assertTrue(result.requester_is_viewer)
        self.assertTrue(result.target_is_roster_member)
        self.assertEqual(result.profile_raw, b"9\n{}")
        self.assertTrue(redis.closed)

    async def test_access_builder_uses_one_statement_and_materializes_sets(self) -> None:
        updated_at = datetime(2026, 8, 31, tzinfo=UTC)
        tournament = SimpleNamespace(
            id="tournament-1",
            organizer_user_id="organizer",
            profile_access_generation=12,
            updated_at=updated_at,
            created_at=updated_at,
            bracket_revision=4,
        )
        row = _Row(
            (tournament, ["viewer-1", "viewer-2"], ["target-1"], True),
            active_viewer_ids=["viewer-1", "viewer-2"],
            roster_user_ids=["target-1"],
            roster_ready=True,
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
        )

        state = await tournament_profile_access._build_state_with_session(session, "night-veil")

        session.execute.assert_awaited_once()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.viewer_user_ids, frozenset({"viewer-1", "viewer-2"}))
        self.assertEqual(state.roster_user_ids, frozenset({"target-1"}))
        self.assertTrue(state.roster_ready)
        self.assertEqual(state.profile_access_generation, 12)

    async def test_join_profile_delta_advances_warmed_empty_roster_cache_with_cas(self) -> None:
        access = json.dumps(
            {
                "tournament_id": "tournament-1",
                "organizer_user_id": "organizer-1",
                "roster_ready": False,
                "profile_access_generation": 5,
                "viewer_count": 0,
                "roster_count": 0,
            },
            separators=(",", ":"),
        ).encode()
        redis = _JoinDeltaRedis(access=access)
        with patch.object(tournament_profile_access, "redis_client", return_value=redis):
            updated = await tournament_profile_access.add_joined_tournament_profile_viewer(
                "night-veil",
                "new-viewer",
                expected_generation=5,
            )

        self.assertTrue(updated)
        stored = json.loads(redis.access)
        self.assertEqual(stored["profile_access_generation"], 6)
        self.assertEqual(stored["viewer_count"], 1)
        self.assertEqual(stored["roster_count"], 0)
        self.assertEqual(redis.viewers, {"new-viewer"})
        self.assertEqual(redis.roster, set())
        self.assertEqual(len(redis.eval_calls), 1)
        self.assertEqual(redis.eval_calls[0][1], 3)
        self.assertEqual(redis.eval_calls[0][-4:], ("5", "6", "new-viewer", "604800"))
        self.assertTrue(redis.closed)

        with (
            patch.object(
                tournament_routes,
                "add_joined_tournament_profile_viewer",
                AsyncMock(return_value=True),
            ) as fast_path,
            patch.object(
                tournament_routes,
                "refresh_tournament_profile_access_state",
                AsyncMock(),
            ) as full_refresh,
        ):
            await tournament_routes._refresh_tournament_profile_access_after_join(
                "night-veil",
                "new-viewer",
                expected_generation=5,
            )
        fast_path.assert_awaited_once_with(
            "night-veil",
            "new-viewer",
            expected_generation=5,
        )
        full_refresh.assert_not_awaited()

        with (
            patch.object(
                tournament_routes,
                "add_joined_tournament_profile_viewer",
                AsyncMock(return_value=False),
            ),
            patch.object(
                tournament_routes,
                "refresh_tournament_profile_access_state",
                AsyncMock(return_value=None),
            ) as full_refresh,
        ):
            await tournament_routes._refresh_tournament_profile_access_after_join(
                "night-veil",
                "new-viewer",
                expected_generation=5,
            )
        full_refresh.assert_awaited_once_with("night-veil")

        # Out-of-order joins or a full rebuild that already advanced the
        # cache cannot move it backward or bless a request twice.
        stale_cache = _JoinDeltaRedis(
            access=json.dumps(
                {**stored, "profile_access_generation": 4, "viewer_count": 1},
                separators=(",", ":"),
            ).encode(),
            viewers={"new-viewer"},
        )
        equal_incoming_cache = _JoinDeltaRedis(
            access=json.dumps(stored, separators=(",", ":")).encode(),
            viewers={"new-viewer"},
        )
        newer_cache = _JoinDeltaRedis(
            access=json.dumps(
                {**stored, "profile_access_generation": 8},
                separators=(",", ":"),
            ).encode(),
            viewers={"new-viewer"},
        )
        bad_cardinality = _JoinDeltaRedis(
            access=json.dumps(
                {**stored, "profile_access_generation": 6, "viewer_count": 3},
                separators=(",", ":"),
            ).encode(),
            viewers={"new-viewer"},
        )
        for rejected in (
            stale_cache,
            equal_incoming_cache,
            newer_cache,
            bad_cardinality,
        ):
            with self.subTest(cache_generation=json.loads(rejected.access)["profile_access_generation"]):
                with patch.object(
                    tournament_profile_access,
                    "redis_client",
                    return_value=rejected,
                ):
                    result = await tournament_profile_access.add_joined_tournament_profile_viewer(
                        "night-veil",
                        "another-viewer",
                        expected_generation=5,
                    )
                self.assertFalse(result)
                self.assertNotIn("another-viewer", rejected.viewers)

        concurrent = _JoinDeltaRedis(access=access)
        with patch.object(
            tournament_profile_access,
            "redis_client",
            return_value=concurrent,
        ):
            outcomes = await asyncio.gather(
                tournament_profile_access.add_joined_tournament_profile_viewer(
                    "night-veil", "join-a", expected_generation=5
                ),
                tournament_profile_access.add_joined_tournament_profile_viewer(
                    "night-veil", "join-b", expected_generation=5
                ),
            )
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(json.loads(concurrent.access)["profile_access_generation"], 6)
        self.assertEqual(len(concurrent.viewers), 1)

    async def test_join_delta_partial_write_is_rejected_and_db_state_controls_profile_access(self) -> None:
        access = json.dumps(
            {
                "tournament_id": "tournament-1",
                "organizer_user_id": "organizer-1",
                "roster_ready": True,
                "profile_access_generation": 5,
                "viewer_count": 0,
                "roster_count": 1,
            },
            separators=(",", ":"),
        ).encode()
        redis = _JoinDeltaRedis(
            access=access,
            roster={"target-1"},
            fail_after_sadd=True,
        )
        with patch.object(tournament_profile_access, "redis_client", return_value=redis):
            updated = await tournament_profile_access.add_joined_tournament_profile_viewer(
                "night-veil",
                "joined-viewer",
                expected_generation=5,
            )

        self.assertFalse(updated)
        self.assertEqual(json.loads(redis.access)["profile_access_generation"], 5)
        self.assertEqual(redis.viewers, {"joined-viewer"})

        # The cache's set changed but its generation did not. A request with
        # the post-commit DB generation must ignore those Redis booleans and
        # authorize from the complete DB-built state instead.
        stale_pipeline = TournamentProfilePipelineResult(
            access_raw=redis.access,
            requester_is_viewer=True,
            target_is_roster_member=True,
            profile_raw=b"11\n{}",
            redis_available=True,
            pipeline_ms=0.2,
        )
        current_state = tournament_profile_access.TournamentProfileAccessState(
            tournament_id="tournament-1",
            organizer_user_id="organizer-1",
            roster_ready=True,
            profile_access_generation=6,
            viewer_user_ids=frozenset({"joined-viewer"}),
            roster_user_ids=frozenset({"target-1"}),
        )
        with (
            patch.object(
                tournament_routes,
                "read_tournament_profile_pipeline",
                AsyncMock(return_value=stale_pipeline),
            ),
            patch.object(
                tournament_routes,
                "refresh_tournament_profile_access_state",
                AsyncMock(return_value=current_state),
            ) as refresh_access,
            patch.object(
                tournament_routes,
                "session_factory",
                return_value=lambda: _SessionContext(),
            ),
        ):
            response = await tournament_routes.get_tournament_scoped_profile(
                "night-veil",
                "target-1",
                self._profile_request(generation=6),
                SimpleNamespace(
                    user=SimpleNamespace(id="joined-viewer"),
                    role_slugs=frozenset(),
                ),
            )

        self.assertEqual(response.body, b"{}")
        refresh_access.assert_awaited_once()

    async def test_profile_access_cache_purge_is_fixed_bounded_and_verified(self) -> None:
        prefixes_v1 = (
            tournament_profile_access.LEGACY_PROFILE_ACCESS_KEY_PREFIX,
            tournament_profile_access.LEGACY_PROFILE_VIEWERS_KEY_PREFIX,
            tournament_profile_access.LEGACY_PROFILE_ROSTER_KEY_PREFIX,
        )
        prefixes_v2 = (
            tournament_profile_access.PROFILE_ACCESS_KEY_PREFIX,
            tournament_profile_access.PROFILE_VIEWERS_KEY_PREFIX,
            tournament_profile_access.PROFILE_ROSTER_KEY_PREFIX,
        )
        keys = {
            f"{prefix}:night-veil".encode()
            for prefix in (*prefixes_v1, *prefixes_v2)
        }
        keys.add(b"platform:tournament:profile-access:v10:must-remain")
        redis = _ProfileAccessPurgeRedis(keys, page_size=1)

        with patch.object(tournament_profile_access, "redis_client", return_value=redis):
            removed_legacy = (
                await tournament_profile_access.purge_legacy_profile_access_cache()
            )

        self.assertEqual(removed_legacy, 3)
        self.assertEqual(
            redis.keys,
            {
                *(f"{prefix}:night-veil".encode() for prefix in prefixes_v2),
                b"platform:tournament:profile-access:v10:must-remain",
            },
        )
        self.assertEqual(
            {match for _cursor, match, _count in redis.scan_calls},
            {f"{prefix}:*" for prefix in prefixes_v1},
        )
        self.assertTrue(all(
            count == tournament_profile_access.PROFILE_ACCESS_PURGE_SCAN_COUNT
            for _cursor, _match, count in redis.scan_calls
        ))
        self.assertTrue(redis.closed)

        redis = _ProfileAccessPurgeRedis(redis.keys, page_size=1)
        with patch.object(tournament_profile_access, "redis_client", return_value=redis):
            removed_all = (
                await tournament_profile_access.purge_all_tournament_profile_access_cache()
            )
        self.assertEqual(removed_all, 3)
        self.assertEqual(
            redis.keys,
            {b"platform:tournament:profile-access:v10:must-remain"},
        )
        self.assertEqual(
            {match for _cursor, match, _count in redis.scan_calls},
            {f"{prefix}:*" for prefix in (*prefixes_v1, *prefixes_v2)},
        )
        self.assertTrue(redis.closed)

        class _UnexpectedKeyRedis(_ProfileAccessPurgeRedis):
            async def scan(self, **kwargs: object) -> tuple[int, list[bytes]]:
                self.scan_calls.append(
                    (kwargs["cursor"], kwargs["match"], kwargs["count"])
                )
                return 0, [b"unrelated:key"]

        redis = _UnexpectedKeyRedis(set())
        with (
            patch.object(tournament_profile_access, "redis_client", return_value=redis),
            self.assertRaisesRegex(RuntimeError, "unexpected key"),
        ):
            await tournament_profile_access.purge_legacy_profile_access_cache()
        self.assertEqual(redis.unlink_calls, [])
        self.assertTrue(redis.closed)

        redis = _ProfileAccessPurgeRedis(
            {
                f"{prefix}:night-veil".encode()
                for prefix in prefixes_v1[:2]
            },
            page_size=2,
        )
        with (
            patch.object(tournament_profile_access, "redis_client", return_value=redis),
            patch.object(tournament_profile_access, "PROFILE_ACCESS_PURGE_MAX_KEYS", 1),
            self.assertRaisesRegex(RuntimeError, "key bound"),
        ):
            await tournament_profile_access.purge_legacy_profile_access_cache()
        self.assertEqual(redis.unlink_calls, [])
        self.assertTrue(redis.closed)

        class _StuckCursorRedis(_ProfileAccessPurgeRedis):
            async def scan(
                self,
                *,
                cursor: int | bytes | str,
                match: str,
                count: int,
            ) -> tuple[int, list[bytes]]:
                self.scan_calls.append((cursor, match, count))
                return 1, [f"{match[:-1]}night-veil".encode()]

        redis = _StuckCursorRedis(set())
        with (
            patch.object(tournament_profile_access, "redis_client", return_value=redis),
            patch.object(
                tournament_profile_access,
                "PROFILE_ACCESS_PURGE_MAX_SCAN_PAGES",
                1,
            ),
            self.assertRaisesRegex(RuntimeError, "scan bound"),
        ):
            await tournament_profile_access.purge_legacy_profile_access_cache()
        self.assertEqual(redis.unlink_calls, [])
        self.assertTrue(redis.closed)

    async def test_stale_profile_revision_cannot_replace_newer_json(self) -> None:
        redis = _CasRedis()
        with patch.object(profile_read_models, "redis_client", return_value=redis) as redis_factory:
            self.assertTrue(
                await profile_read_models.write_profile_read_model(
                    "user-1",
                    revision=20,
                    payload=b'{"profile":{"display_name":"new"}}',
                )
            )
            self.assertFalse(
                await profile_read_models.write_profile_read_model(
                    "user-1",
                    revision=19,
                    payload=b'{"profile":{"display_name":"old"}}',
                )
            )
        redis_factory.assert_has_calls(
            [
                call(decode_responses=False, shared=True),
                call(decode_responses=False, shared=True),
            ]
        )

        cached = profile_read_models._decode_envelope(
            redis.store[profile_read_models.profile_read_model_key("user-1")]
        )
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.revision, 20)
        self.assertIn(b'"new"', cached.payload)

    async def test_concurrent_profile_misses_share_one_builder(self) -> None:
        redis = _SingleflightRedis()
        model = profile_read_models.ProfileReadModel(
            revision=12,
            payload=b'{"profile":{"user_id":"user-1"},"deadlock_profile":null}',
        )
        build_calls = 0

        async def build(_user_id: str) -> profile_read_models.ProfileReadModel:
            nonlocal build_calls
            build_calls += 1
            await asyncio.sleep(0.04)
            return model

        with (
            patch.object(profile_read_models, "redis_client", return_value=redis),
            patch.object(
                profile_read_models,
                "build_profile_read_model",
                AsyncMock(side_effect=build),
            ),
        ):
            payloads = await asyncio.gather(
                *(
                    profile_read_models.get_or_build_profile_read_model("user-1")
                    for _ in range(10)
                )
            )

        self.assertEqual(build_calls, 1)
        self.assertEqual(payloads, [model.payload] * 10)
        self.assertEqual(
            redis.set_calls[0][2:],
            (
                profile_read_models.PROFILE_READ_MODEL_LOCK_TTL_MILLISECONDS,
                True,
            ),
        )
        self.assertNotIn(
            profile_read_models.profile_read_model_lock_key("user-1"),
            redis.store,
        )

    async def test_warm_profile_hit_reads_before_acquiring_singleflight_lock(self) -> None:
        redis = _SingleflightRedis()
        model = profile_read_models.ProfileReadModel(
            revision=12,
            payload=b'{"profile":{"user_id":"user-1"},"deadlock_profile":null}',
        )
        redis.store[profile_read_models.profile_read_model_key("user-1")] = (
            profile_read_models._encode_envelope(
                revision=model.revision,
                payload=model.payload,
            )
        )

        with (
            patch.object(profile_read_models, "redis_client", return_value=redis) as redis_factory,
            patch.object(
                profile_read_models,
                "build_profile_read_model",
                AsyncMock(side_effect=AssertionError("warm hit must not build")),
            ),
        ):
            payload = await profile_read_models.get_or_build_profile_read_model("user-1")

        self.assertEqual(payload, model.payload)
        self.assertEqual(redis.set_calls, [])
        redis_factory.assert_called_once_with(decode_responses=False, shared=True)

    async def test_absent_profile_is_negative_cached_and_does_not_rebuild(self) -> None:
        redis = _SingleflightRedis()
        with (
            patch.object(profile_read_models, "redis_client", return_value=redis),
            patch.object(
                profile_read_models,
                "build_profile_read_model",
                AsyncMock(return_value=None),
            ) as build,
        ):
            first = await profile_read_models.get_or_build_profile_read_model("user-1")
            second = await profile_read_models.get_or_build_profile_read_model("user-1")

        self.assertIsNone(first)
        self.assertIsNone(second)
        build.assert_awaited_once_with("user-1")
        raw = redis.store[profile_read_models.profile_read_model_key("user-1")]
        envelope = profile_read_models._decode_envelope(raw)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(
            envelope.payload,
            profile_read_models.PROFILE_READ_MODEL_NEGATIVE_SENTINEL,
        )
        self.assertEqual(
            redis.set_calls[-1][2],
            profile_read_models.PROFILE_READ_MODEL_NEGATIVE_TTL_SECONDS,
        )

    async def test_positive_profile_replaces_negative_sentinel(self) -> None:
        redis = _SingleflightRedis()
        redis.store[profile_read_models.profile_read_model_key("user-1")] = (
            profile_read_models._encode_envelope(
                revision=0,
                payload=profile_read_models.PROFILE_READ_MODEL_NEGATIVE_SENTINEL,
            )
        )

        with patch.object(profile_read_models, "redis_client", return_value=redis):
            stored = await profile_read_models.write_profile_read_model(
                "user-1",
                revision=12,
                payload=b'{"profile":{"user_id":"user-1"}}',
            )

        self.assertTrue(stored)
        self.assertEqual(
            profile_read_models.profile_read_model_payload(
                redis.store[profile_read_models.profile_read_model_key("user-1")]
            ),
            b'{"profile":{"user_id":"user-1"}}',
        )

    async def test_profile_read_model_unlock_only_deletes_owned_lock(self) -> None:
        redis = _SingleflightRedis()
        key = profile_read_models.profile_read_model_lock_key("user-1")
        await redis.set(
            key,
            "owner-token",
            px=profile_read_models.PROFILE_READ_MODEL_LOCK_TTL_MILLISECONDS,
            nx=True,
        )

        await profile_read_models._release_profile_read_model_lock(
            redis,
            "user-1",
            "other-token",
        )
        self.assertEqual(redis.store[key], "owner-token")
        await profile_read_models._release_profile_read_model_lock(
            redis,
            "user-1",
            "owner-token",
        )
        self.assertNotIn(key, redis.store)

    async def test_profile_singleflight_uses_one_db_fallback_when_redis_is_unavailable(
        self,
    ) -> None:
        class _UnavailableRedis(_SingleflightRedis):
            async def set(
                self,
                key: str,
                value: str,
                *,
                px: int,
                nx: bool,
            ) -> bool:
                raise ConnectionError("redis unavailable")

        redis = _UnavailableRedis()
        model = profile_read_models.ProfileReadModel(revision=12, payload=b"{}")
        with (
            patch.object(profile_read_models, "redis_client", return_value=redis),
            patch.object(
                profile_read_models,
                "build_profile_read_model",
                AsyncMock(return_value=model),
            ) as build,
        ):
            payload = await profile_read_models.get_or_build_profile_read_model("user-1")

        self.assertEqual(payload, model.payload)
        build.assert_awaited_once_with("user-1")


if __name__ == "__main__":
    unittest.main()
