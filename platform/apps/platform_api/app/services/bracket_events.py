from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import deque
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
    SSE_LEASE_RENEW_INTERVAL_SECONDS,
    SSE_RECONNECT_JITTER_MS,
    SSE_RECONNECT_MIN_MS,
    SseConnectionLease,
    SseConnectionLeaseRenewalFailed,
)

logger = logging.getLogger(__name__)

SSE_ACCESS_REVALIDATION_SECONDS = 30.0
SSE_ACCESS_CHECK_COALESCE_SECONDS = 0.5
# Keep most of each API worker's DB pool for ordinary user requests. A bracket
# event can wake thousands of private streams at once; allowing every stream
# to revalidate concurrently turns that fan-out into a DB pool outage.
SSE_ACCESS_REVALIDATION_CONCURRENCY = 2
SSE_RELAY_QUEUE_MAXSIZE = 32

_access_check_registry_lock = asyncio.Lock()
_access_check_cache: dict[str, tuple[float, bool]] = {}
_access_check_locks: dict[str, asyncio.Lock] = {}
_access_check_concurrency = asyncio.Semaphore(SSE_ACCESS_REVALIDATION_CONCURRENCY)
_relay_registry_lock = asyncio.Lock()
_relays: dict[str, "_BracketEventRelay"] = {}
_RELAY_CLOSED = object()


class _BracketEventSubscription:
    def __init__(self, next_sequence: int) -> None:
        self.next_sequence = next_sequence


class _BracketEventRelay:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.client = None
        self.pubsub = None
        self.task: asyncio.Task[None] | None = None
        self.subscribers: set[_BracketEventSubscription] = set()
        self.messages: deque[tuple[int, str]] = deque(
            maxlen=SSE_RELAY_QUEUE_MAXSIZE
        )
        self.next_sequence = 0
        self.message_event = asyncio.Event()
        self.closed = False
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
                data = message.get("data") if isinstance(message, dict) else None
                if not isinstance(data, str):
                    continue
                self.next_sequence += 1
                self.messages.append(
                    (
                        self.next_sequence,
                        f"event: bracket\ndata: {data}\n\n",
                    )
                )
                self.message_event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bracket event relay failed for channel %s.", self.channel)
        finally:
            self.closed = True
            self.message_event.set()
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
) -> tuple[_BracketEventRelay, _BracketEventSubscription]:
    async with _relay_registry_lock:
        relay = _relays.get(channel)
        if relay is None or relay.closed:
            relay = _BracketEventRelay(channel)
            await relay.start()
            _relays[channel] = relay
        subscription = _BracketEventSubscription(relay.next_sequence + 1)
        relay.subscribers.add(subscription)
        return relay, subscription


async def _unsubscribe_from_bracket_relay(
    channel: str,
    relay: _BracketEventRelay,
    subscription: _BracketEventSubscription,
) -> None:
    task: asyncio.Task[None] | None = None
    should_close_resources = False
    async with _relay_registry_lock:
        relay.subscribers.discard(subscription)
        if not relay.subscribers and _relays.get(channel) is relay:
            _relays.pop(channel, None)
            task = relay.task
            should_close_resources = True
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if should_close_resources:
        await relay._close_resources()


async def _next_bracket_event(
    relay: _BracketEventRelay,
    subscription: _BracketEventSubscription,
) -> str | object:
    while True:
        if relay.closed:
            return _RELAY_CLOSED
        if relay.messages and subscription.next_sequence <= relay.next_sequence:
            first_sequence = relay.messages[0][0]
            if subscription.next_sequence < first_sequence:
                subscription.next_sequence = first_sequence
            message_index = subscription.next_sequence - first_sequence
            message = relay.messages[message_index][1]
            subscription.next_sequence += 1
            return message
        relay.message_event.clear()
        if relay.closed:
            return _RELAY_CLOSED
        await relay.message_event.wait()


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
    if context.decision == "public":
        # Public visibility does not depend on the viewer's session. Sharing
        # this short-lived check across viewers turns one tournament event
        # into one DB read instead of one read per connected public stream.
        return ":".join((tournament_id, context.decision, context.slug))
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
        async with _access_check_concurrency:
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
    connection_lease: SseConnectionLease | None = None,
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
    relay, subscription = await _subscribe_to_bracket_relay(channel)
    started_at = monotonic()
    last_access_check_at = started_at
    last_lease_renewal_at = started_at
    retry_ms = SSE_RECONNECT_MIN_MS + secrets.randbelow(SSE_RECONNECT_JITTER_MS + 1)
    try:
        yield f"retry: {retry_ms}\nevent: connected\ndata: {{}}\n\n"
        while True:
            try:
                message = await asyncio.wait_for(
                    _next_bracket_event(relay, subscription),
                    timeout=float(SSE_KEEPALIVE_SECONDS),
                )
            except TimeoutError:
                message = None
            if message is _RELAY_CLOSED:
                break
            now = monotonic()
            if (
                connection_lease is not None
                and now - last_lease_renewal_at >= SSE_LEASE_RENEW_INTERVAL_SECONDS
            ):
                try:
                    await connection_lease.renew()
                except SseConnectionLeaseRenewalFailed:
                    logger.error(
                        "Closing SSE stream because its connection lease could not be renewed."
                    )
                    break
                last_lease_renewal_at = now
            if message is not None or (
                now - last_access_check_at >= SSE_ACCESS_REVALIDATION_SECONDS
            ):
                if not await _coalesced_stream_access_check(tournament_id):
                    break
                last_access_check_at = now
            if message is None:
                yield ": keepalive\n\n"
                continue
            yield message
    finally:
        await _unsubscribe_from_bracket_relay(channel, relay, subscription)
