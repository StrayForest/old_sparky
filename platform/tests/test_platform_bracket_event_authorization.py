from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.platform_api.app.services import bracket_events


class _FakePubSub:
    def __init__(self, messages: list[dict[str, str] | None]) -> None:
        self.messages = list(messages)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        del ignore_subscribe_messages, timeout
        if self.messages:
            return self.messages.pop(0)
        return None

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedisClient:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


class PlatformBracketEventAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_stops_before_next_private_event_after_access_is_revoked(self) -> None:
        pubsub = _FakePubSub([{"data": '{"revision":2}'}])
        client = _FakeRedisClient(pubsub)
        access_check = AsyncMock(side_effect=(True, False))

        with (
            patch.object(bracket_events, "redis_client", MagicMock(return_value=client)),
            patch.object(
                bracket_events,
                "current_tournament_stream_access_is_valid",
                access_check,
            ),
        ):
            stream = bracket_events.stream_bracket_events("tournament-1")
            self.assertEqual(await anext(stream), "event: connected\ndata: {}\n\n")
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

        self.assertEqual(access_check.await_count, 2)
        self.assertEqual(pubsub.subscribed, ["platform:bracket:tournament-1"])
        self.assertEqual(pubsub.unsubscribed, ["platform:bracket:tournament-1"])
        self.assertTrue(pubsub.closed)
        self.assertTrue(client.closed)

    async def test_stream_does_not_subscribe_when_current_access_is_denied(self) -> None:
        redis_factory = MagicMock()
        access_check = AsyncMock(return_value=False)

        with (
            patch.object(bracket_events, "redis_client", redis_factory),
            patch.object(
                bracket_events,
                "current_tournament_stream_access_is_valid",
                access_check,
            ),
        ):
            stream = bracket_events.stream_bracket_events("tournament-2")
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

        access_check.assert_awaited_once_with("tournament-2")
        redis_factory.assert_not_called()

    async def test_stream_emits_event_when_access_remains_current(self) -> None:
        pubsub = _FakePubSub([{"data": '{"revision":3}'}])
        client = _FakeRedisClient(pubsub)
        access_check = AsyncMock(side_effect=(True, True))

        with (
            patch.object(bracket_events, "redis_client", MagicMock(return_value=client)),
            patch.object(
                bracket_events,
                "current_tournament_stream_access_is_valid",
                access_check,
            ),
        ):
            stream = bracket_events.stream_bracket_events("tournament-3")
            self.assertEqual(await anext(stream), "event: connected\ndata: {}\n\n")
            self.assertEqual(
                await anext(stream),
                'event: bracket\ndata: {"revision":3}\n\n',
            )
            await stream.aclose()

        self.assertEqual(access_check.await_count, 2)
        self.assertEqual(pubsub.unsubscribed, ["platform:bracket:tournament-3"])
        self.assertTrue(pubsub.closed)
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
