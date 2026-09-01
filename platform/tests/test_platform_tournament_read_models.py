from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from redis.exceptions import ConnectionError as RedisConnectionError
from pydantic import BaseModel

from apps.platform_api.app.services import tournament_read_models as read_models


class _FakeRedis:
    def __init__(self, store: dict[str, bytes], *, failure: Exception | None = None) -> None:
        self.store = store
        self.failure = failure
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        if self.failure is not None:
            raise self.failure
        return self.store.get(key)

    async def eval(
        self,
        _script: str,
        _key_count: int,
        key: str,
        revision: str,
        payload: bytes,
        _ttl: str,
    ) -> int:
        if self.failure is not None:
            raise self.failure
        current = read_models._decode_envelope(self.store.get(key))
        if current is None or current.revision <= int(revision):
            self.store[key] = payload
            return 1
        return 0

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def aclose(self) -> None:
        self.closed = True


class TournamentReadModelTests(unittest.IsolatedAsyncioTestCase):
    def test_serializes_pydantic_lists_as_json_objects(self) -> None:
        class Team(BaseModel):
            id: str

        self.assertEqual(
            read_models._serialize_payload([Team(id="team-1")]),
            b'[{"id":"team-1"}]',
        )

    async def test_revision_matching_hit_does_not_rebuild(self) -> None:
        store: dict[str, bytes] = {}
        clients: list[_FakeRedis] = []

        def client_factory(*, decode_responses: bool) -> _FakeRedis:
            self.assertFalse(decode_responses)
            client = _FakeRedis(store)
            clients.append(client)
            return client

        builder = AsyncMock(return_value=b'{"teams":[]}')
        with patch.object(read_models, "redis_client", side_effect=client_factory):
            first = await read_models.read_model_read_or_build(
                tournament_id="tournament-1",
                model="teams",
                revision=7,
                builder=builder,
            )
            second = await read_models.read_model_read_or_build(
                tournament_id="tournament-1",
                model="teams",
                revision=7,
                builder=builder,
            )

        self.assertEqual(first, second)
        builder.assert_awaited_once()
        self.assertTrue(all(client.closed for client in clients))

    async def test_stale_revision_cannot_overwrite_newer_projection(self) -> None:
        store: dict[str, bytes] = {}

        with patch.object(
            read_models,
            "redis_client",
            side_effect=lambda **_: _FakeRedis(store),
        ):
            newer, _ = await read_models._redis_set_if_newer(
                tournament_id="tournament-1",
                model="bracket_full",
                revision=20,
                payload=b"new",
            )
            older, _ = await read_models._redis_set_if_newer(
                tournament_id="tournament-1",
                model="bracket_full",
                revision=19,
                payload=b"old",
            )

        self.assertTrue(newer)
        self.assertFalse(older)
        envelope = read_models._decode_envelope(
            store[read_models.read_model_key("tournament-1", "bracket_full")]
        )
        assert envelope is not None
        self.assertEqual(envelope.revision, 20)
        self.assertEqual(envelope.payload, b"new")

    async def test_delete_removes_all_selected_projections(self) -> None:
        tournament_id = "tournament-1"
        models = ("teams", "workspace_detail", "bracket_summary", "bracket_full")
        store = {
            read_models.read_model_key(tournament_id, model): b"projection"
            for model in models
        }
        client = _FakeRedis(store)

        with patch.object(read_models, "redis_client", return_value=client):
            await read_models.delete_tournament_read_models(tournament_id, models)

        self.assertEqual(store, {})
        self.assertTrue(client.closed)

    async def test_redis_outage_returns_authoritative_builder_payload(self) -> None:
        client = _FakeRedis({}, failure=RedisConnectionError("redis unavailable"))
        builder = AsyncMock(return_value=b'{"status":"ready"}')

        with patch.object(read_models, "redis_client", return_value=client):
            payload = await read_models.read_model_read_or_build(
                tournament_id="tournament-1",
                model="workspace_detail",
                revision=3,
                builder=builder,
            )

        self.assertEqual(payload, b'{"status":"ready"}')
        builder.assert_awaited_once()
        self.assertTrue(client.closed)

    async def test_summary_refresh_does_not_load_members_or_profiles(self) -> None:
        tournament = SimpleNamespace(
            id="tournament-1",
            status="in_progress",
            bracket_revision=4,
        )
        team_loader = AsyncMock(return_value=([], []))
        match_loader = AsyncMock(return_value=[])
        profile_loader = AsyncMock()

        with (
            patch(
                "apps.platform_api.app.services.tournament_teams.load_tournament_team_state",
                team_loader,
            ),
            patch(
                "apps.platform_api.app.api.routes.tournaments.tournament_matches_in_order",
                match_loader,
            ),
            patch(
                "apps.platform_api.app.api.routes.tournaments.deadlock_assignment_member_profiles",
                profile_loader,
            ),
        ):
            values = await read_models._build_selected_read_models_from_authoritative_db(
                AsyncMock(),
                tournament=tournament,
                selected=("bracket_summary",),
            )

        team_loader.assert_awaited_once_with(
            ANY,
            tournament_id="tournament-1",
            include_members=False,
        )
        match_loader.assert_awaited_once_with(
            ANY,
            tournament_id="tournament-1",
        )
        profile_loader.assert_not_awaited()
        self.assertEqual(values["bracket_summary"].teams, [])


if __name__ == "__main__":
    unittest.main()
