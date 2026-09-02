from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError

from apps.platform_api.app.services import tournament_catalog_cache as catalog_cache


class _FakeRedis:
    def __init__(self, store: dict[str, bytes], *, failure: Exception | None = None) -> None:
        self.store = store
        self.failure = failure
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        if self.failure is not None:
            raise self.failure
        return self.store.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> None:
        if self.failure is not None:
            raise self.failure
        self.store[key] = value
        self.store[f"{key}:ttl"] = str(ex).encode()

    async def aclose(self) -> None:
        self.closed = True


class TournamentCatalogCacheTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _key(**overrides: object) -> str:
        values: dict[str, object] = {
            "search": "Night Cup",
            "rank": ["Oracle", "Phantom"],
            "open_registration": False,
            "status_filter": None,
            "participants_sort": None,
            "date_sort": None,
            "limit": 9,
            "cursor": None,
        }
        values.update(overrides)
        return catalog_cache.public_tournament_list_cache_key(**values)

    async def test_key_normalizes_equivalent_public_queries(self) -> None:
        self.assertEqual(
            self._key(search=" Night Cup ", rank=["Phantom", "Oracle"]),
            self._key(search="night cup", rank=["oracle", "phantom"]),
        )

    async def test_write_then_read_round_trip_uses_compact_envelope(self) -> None:
        store: dict[str, bytes] = {}
        client = _FakeRedis(store)
        with (
            patch.object(catalog_cache, "_cache_enabled", return_value=True),
            patch.object(catalog_cache, "redis_client", return_value=client),
        ):
            await catalog_cache.set_public_tournament_list_cache(
                self._key(),
                body=b'[{"id":"tournament-1"}]',
                limit=9,
                has_more=True,
                next_cursor="cursor-1",
            )
            entry = await catalog_cache.get_public_tournament_list_cache(self._key())

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.body, b'[{"id":"tournament-1"}]')
        self.assertEqual(entry.limit, 9)
        self.assertTrue(entry.has_more)
        self.assertEqual(entry.next_cursor, "cursor-1")
        envelope = json.loads(store[self._key()])
        self.assertEqual(envelope["v"], 1)
        self.assertEqual(envelope["items"], [{"id": "tournament-1"}])
        self.assertEqual(envelope["limit"], 9)
        self.assertFalse(client.closed)

    async def test_redis_outage_is_a_cache_miss_and_does_not_fail_the_read(self) -> None:
        client = _FakeRedis({}, failure=RedisConnectionError("redis unavailable"))
        with (
            patch.object(catalog_cache, "_cache_enabled", return_value=True),
            patch.object(catalog_cache, "redis_client", return_value=client),
        ):
            entry = await catalog_cache.get_public_tournament_list_cache(self._key())

        self.assertIsNone(entry)
        self.assertFalse(client.closed)


if __name__ == "__main__":
    unittest.main()
