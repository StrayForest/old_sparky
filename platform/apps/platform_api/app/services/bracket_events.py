from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import suppress
from time import monotonic
from typing import Any

from apps.platform_api.app.services.tournament_workspace_access import (
    current_tournament_stream_access_context,
    current_tournament_stream_access_is_valid,
)
from python_packages.platform_infra.redis import redis_client
from python_packages.platform_infra.sse_connection_limit import (
    SSE_KEEPALIVE_SECONDS,
    SSE_RECONNECT_JITTER_MS,
    SSE_RECONNECT_MIN_MS,
    SSE_STREAM_MAX_LIFETIME_SECONDS,
)

logger = logging.getLogger(__name__)

SSE_ACCESS_REVALIDATION_SECONDS = 30.0
SSE_ACCESS_CHECK_COALESCE_SECONDS = 0.5
SSE_RELAY_QUEUE_MAXSIZE = 32

_access_check_registry_lock = asyncio.Lock()
_access_check_cache: dict[str, tuple[float, bool]] = {}
_access_check_locks: dict[str, asyncio.Lock] = {}
_relay_registry_lock = asyncio.Lock()
_relays: dict[str, "_BracketEventRelay"] = {}
_RELAY_CLOSED = object()


class _BracketEventRelay:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.client = None
        self.pubsub = None
        self.task: asyncio.Task[None] | None = None
        self.subscribers: set[asyncio.Queue[object]] = set()
        self.resources_closed = False

    async def start(self) -> None:
        self.client = redis_client()
        try:
            self.pubsub = self.client.pubsub()
            await self.pubsub.subscribe(self.channel)
            self.task = asyncio.create_task(self._run())
        except BaseException:
            if self.client is not None:
                await self.client.aclose()
            self.client = None
            self.pubsub = None
            raise

    async def _run(self) -> None:
        assert self.pubsub is not None
        try:
            while True:
                message = await self.pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    await asyncio.sleep(0.01)
                    continue
                for queue in tuple(self.subscribers):
                    if queue.full():
                        with suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    with suppress(asyncio.QueueFull):
                        queue.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bracket event relay failed for channel %s.", self.channel)
        finally:
            for queue in tuple(self.subscribers):
                if queue.full():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(_RELAY_CLOSED)
            await self._close_resources()

    async def _close_resources(self) -> None:
        if self.resources_closed:
            return
        self.resources_closed = True
        if self.pubsub is not None:
            with suppress(Exception):
                await self.pubsub.unsubscribe(self.channel)
            with suppress(Exception):
                await self.pubsub.aclose()
        if self.client is not None:
            with suppress(Exception):
                await self.client.aclose()


async def _subscribe_to_bracket_relay(
    channel: str,
) -> tuple[_BracketEventRelay, asyncio.Queue[object]]:
    async with _relay_registry_lock:
        relay = _relays.get(channel)
        if relay is None:
            relay = _BracketEventRelay(channel)
            await relay.start()
            _relays[channel] = relay
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=SSE_RELAY_QUEUE_MAXSIZE)
        relay.subscribers.add(queue)
        return relay, queue


async def _unsubscribe_from_bracket_relay(
    channel: str,
    relay: _BracketEventRelay,
    queue: asyncio.Queue[object],
) -> None:
    task: asyncio.Task[None] | None = None
    async with _relay_registry_lock:
        relay.subscribers.discard(queue)
        if not relay.subscribers and _relays.get(channel) is relay:
            _relays.pop(channel, None)
            task = relay.task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await relay._close_resources()


async def dispose_bracket_event_relays() -> None:
    async with _relay_registry_lock:
        relays = tuple(_relays.values())
        _relays.clear()
    tasks = [relay.task for relay in relays if relay.task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for relay in relays:
        await relay._close_resources()


async def _reset_bracket_relays_for_tests() -> None:
    await dispose_bracket_event_relays()


def bracket_channel(tournament_id: str) -> str:
    return f"platform:bracket:{tournament_id}"


def _access_check_cache_key(tournament_id: str) -> str:
    context = current_tournament_stream_access_context()
    if context is None:
        return f"{tournament_id}:anonymous"
    return ":".join(
        (
            tournament_id,
            context.decision,
            context.slug,
            context.user_id or "",
            context.session_id or "",
        )
    )


async def _coalesced_stream_access_check(tournament_id: str) -> bool:
    """Share a short-lived authoritative check across identical streams.

    A tournament event is delivered to every subscriber at once. Holding a
    per-access-key lock around one short database check prevents thousands of
    identical subscribers from exhausting the API pool on the same event,
    while independent tournaments and users can revalidate concurrently.
    """

    key = _access_check_cache_key(tournament_id)
    async with _access_check_registry_lock:
        key_lock = _access_check_locks.setdefault(key, asyncio.Lock())
    async with key_lock:
        async with _access_check_registry_lock:
            cached = _access_check_cache.get(key)
            if (
                cached is not None
                and monotonic() - cached[0] < SSE_ACCESS_CHECK_COALESCE_SECONDS
            ):
                return cached[1]
        allowed = await current_tournament_stream_access_is_valid(tournament_id)
        async with _access_check_registry_lock:
            _access_check_cache[key] = (monotonic(), allowed)
            if len(_access_check_cache) > 4096:
                cutoff = monotonic() - SSE_ACCESS_CHECK_COALESCE_SECONDS
                for cache_key, (checked_at, _) in tuple(_access_check_cache.items()):
                    if checked_at < cutoff:
                        _access_check_cache.pop(cache_key, None)
                        stale_lock = _access_check_locks.get(cache_key)
                        if stale_lock is not None and not stale_lock.locked():
                            _access_check_locks.pop(cache_key, None)
        return allowed


async def publish_bracket_event(
    tournament_id: str,
    payload: dict[str, Any],
) -> None:
    client = redis_client()
    try:
        await client.publish(
            bracket_channel(tournament_id),
            json.dumps(payload, separators=(",", ":")),
        )
    except Exception:
        logger.exception(
            "Failed to publish bracket event for tournament %s.",
            tournament_id,
        )
    finally:
        await client.aclose()


async def stream_bracket_events(
    tournament_id: str,
    *,
    admission_verified: bool = False,
) -> AsyncIterator[str]:
    """Stream bracket events after endpoint admission and on every checkpoint.

    The HTTP endpoint has already completed the authoritative admission check
    before creating the stream. Skipping that duplicate check avoids a second
    database round trip before Redis subscription; every bracket event and the
    periodic idle checkpoint still revalidate access.
    """

    if not admission_verified and not await current_tournament_stream_access_is_valid(
        tournament_id
    ):
        return

    channel = bracket_channel(tournament_id)
    relay, event_queue = await _subscribe_to_bracket_relay(channel)
    started_at = monotonic()
    last_access_check_at = started_at
    retry_ms = SSE_RECONNECT_MIN_MS + secrets.randbelow(SSE_RECONNECT_JITTER_MS + 1)
    try:
        yield f"retry: {retry_ms}\nevent: connected\ndata: {{}}\n\n"
        while True:
            remaining_seconds = (
                SSE_STREAM_MAX_LIFETIME_SECONDS - (monotonic() - started_at)
            )
            if remaining_seconds <= 0:
                break
            try:
                message = await asyncio.wait_for(
                    event_queue.get(),
                    timeout=min(float(SSE_KEEPALIVE_SECONDS), remaining_seconds),
                )
            except TimeoutError:
                message = None
            if message is _RELAY_CLOSED:
                break
            now = monotonic()
            if message is not None or (
                now - last_access_check_at >= SSE_ACCESS_REVALIDATION_SECONDS
            ):
                if not await _coalesced_stream_access_check(tournament_id):
                    break
                last_access_check_at = now
            if message is None:
                yield ": keepalive\n\n"
                continue
            yield f"event: bracket\ndata: {message['data']}\n\n"
    finally:
        await _unsubscribe_from_bracket_relay(channel, relay, event_queue)
