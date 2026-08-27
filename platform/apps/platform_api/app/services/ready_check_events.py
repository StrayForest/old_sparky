from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
import hashlib
import hmac
import json
import logging
from time import monotonic

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client
from python_packages.platform_infra.ready_check_admission import READY_CHECK_ADMISSION_TTL_SECONDS
from python_packages.platform_infra.sse_connection_limit import (
    SSE_KEEPALIVE_SECONDS,
    SseConnectionLease,
    SseConnectionLeaseRenewalFailed,
)

logger = logging.getLogger(__name__)

READY_CHECK_EVENT_CHANNEL = "platform:ready-check:events:v1"
READY_CHECK_STATE_KEY_PREFIX = "platform:ready-check:state:v1"
READY_CHECK_STATE_TTL_SECONDS = READY_CHECK_ADMISSION_TTL_SECONDS + 60
READY_CHECK_RELAY_QUEUE_MAXSIZE = 32

_relay_lock = asyncio.Lock()
_relay: "_ReadyCheckEventRelay | None" = None


def _state_key(tournament_id: str, user_id: str, ready_check_starts_at: int) -> str:
    # IDs are not secret, but keeping user IDs out of Redis key names makes
    # operational dumps less revealing and matches other platform hot paths.
    settings = get_settings()
    digest = hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        f"{tournament_id}:{user_id}:{ready_check_starts_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{READY_CHECK_STATE_KEY_PREFIX}:{digest}"


class _ReadyCheckSubscription:
    def __init__(self, tournament_ids: set[str]) -> None:
        self.tournament_ids = tournament_ids
        self.next_sequence = 1


class _ReadyCheckEventRelay:
    def __init__(self) -> None:
        self.client = None
        self.pubsub = None
        self.task: asyncio.Task[None] | None = None
        self.messages: deque[tuple[int, str, str]] = deque(maxlen=READY_CHECK_RELAY_QUEUE_MAXSIZE)
        self.next_sequence = 0
        self.message_event = asyncio.Event()
        self.closed = False
        self.resources_closed = False

    async def start(self) -> None:
        self.client = redis_client()
        try:
            self.pubsub = self.client.pubsub()
            await self.pubsub.subscribe(READY_CHECK_EVENT_CHANNEL)
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
                try:
                    payload = json.loads(data)
                    tournament_id = str(payload["tournament_id"])
                    event = f"event: ready_check\ndata: {data}\n\n"
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                self.next_sequence += 1
                self.messages.append((self.next_sequence, tournament_id, event))
                self.message_event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ready Check event relay failed.")
        finally:
            self.closed = True
            self.message_event.set()
            await self.close_resources()

    async def close_resources(self) -> None:
        if self.resources_closed:
            return
        self.resources_closed = True
        if self.pubsub is not None:
            try:
                await self.pubsub.unsubscribe(READY_CHECK_EVENT_CHANNEL)
            except Exception:
                pass
            try:
                await self.pubsub.aclose()
            except Exception:
                pass
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                pass


async def _get_relay() -> _ReadyCheckEventRelay:
    global _relay
    async with _relay_lock:
        if _relay is None or _relay.closed:
            _relay = _ReadyCheckEventRelay()
            await _relay.start()
        return _relay


async def publish_ready_check_event(
    *,
    tournament_id: str,
    round_id: int,
    status: str,
    eligible_user_ids: list[str] | tuple[str, ...],
    ready_check_starts_at: int,
) -> None:
    payload = {
        "tournament_id": str(tournament_id),
        "round_id": int(round_id),
        "status": status,
        "revision": int(round_id),
    }
    client = redis_client()
    try:
        encoded = json.dumps(payload, separators=(",", ":"))
        pipeline = client.pipeline(transaction=False)
        for user_id in eligible_user_ids:
            pipeline.set(
                _state_key(str(tournament_id), str(user_id), int(ready_check_starts_at)),
                encoded,
                ex=READY_CHECK_STATE_TTL_SECONDS,
            )
        pipeline.publish(READY_CHECK_EVENT_CHANNEL, encoded)
        await pipeline.execute()
    except Exception:
        # PostgreSQL remains authoritative. Redis projection failure is
        # observable and causes the browser to retry/fall back, never a false
        # authorization grant.
        logger.exception(
            "Failed to project Ready Check state: tournament=%s status=%s",
            tournament_id,
            status,
        )
    finally:
        await client.aclose()


async def revoke_ready_check_state(
    *,
    tournament_id: str,
    user_id: str,
    ready_check_starts_at: int,
) -> None:
    client = redis_client()
    try:
        await client.delete(
            _state_key(str(tournament_id), str(user_id), int(ready_check_starts_at))
        )
    except Exception:
        logger.warning(
            "Failed to revoke Ready Check state projection: tournament=%s user=%s",
            tournament_id,
            user_id,
            exc_info=True,
        )
    finally:
        await client.aclose()


async def read_ready_check_state(
    *,
    tournament_id: str,
    user_id: str,
    ready_check_starts_at: int,
) -> dict[str, object] | None:
    client = redis_client()
    try:
        value = await client.get(
            _state_key(str(tournament_id), str(user_id), int(ready_check_starts_at))
        )
        if not isinstance(value, str):
            return None
        try:
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning(
                "Ignoring malformed Ready Check state projection: tournament=%s user=%s",
                tournament_id,
                user_id,
            )
            return None
        return payload if isinstance(payload, dict) else None
    finally:
        await client.aclose()


async def stream_ready_check_events(
    tournament_ids: tuple[str, ...],
    *,
    connection_lease: SseConnectionLease | None = None,
) -> AsyncIterator[str]:
    relay = await _get_relay()
    subscription = _ReadyCheckSubscription(set(tournament_ids))
    last_lease_renewal_at = monotonic()

    async def renew_lease_if_due() -> bool:
        nonlocal last_lease_renewal_at
        if connection_lease is None:
            return True
        now = monotonic()
        if now - last_lease_renewal_at < SSE_KEEPALIVE_SECONDS:
            return True
        try:
            await connection_lease.renew()
        except SseConnectionLeaseRenewalFailed:
            logger.error(
                "Closing Ready Check SSE because its connection lease "
                "could not be renewed."
            )
            return False
        last_lease_renewal_at = now
        return True

    try:
        yield "retry: 5000\nevent: connected\ndata: {}\n\n"
        while True:
            if relay.closed:
                return
            message: str | object | None = None
            while relay.messages and subscription.next_sequence <= relay.next_sequence:
                first_sequence = relay.messages[0][0]
                if subscription.next_sequence < first_sequence:
                    subscription.next_sequence = first_sequence
                message_index = subscription.next_sequence - first_sequence
                if message_index >= len(relay.messages):
                    subscription.next_sequence = relay.messages[0][0]
                    continue
                sequence, tournament_id, candidate = relay.messages[message_index]
                subscription.next_sequence = sequence + 1
                if tournament_id in subscription.tournament_ids:
                    message = candidate
                    break
            if message is not None:
                if not await renew_lease_if_due():
                    return
                yield message
                continue
            relay.message_event.clear()
            if relay.closed:
                return
            try:
                await asyncio.wait_for(
                    relay.message_event.wait(),
                    timeout=float(SSE_KEEPALIVE_SECONDS),
                )
            except TimeoutError:
                if not await renew_lease_if_due():
                    return
                yield ": keepalive\n\n"
    finally:
        # The global relay is shared by every stream in this worker. It is
        # intentionally kept alive until worker shutdown to avoid the old
        # last-subscriber lifecycle race.
        pass


async def dispose_ready_check_event_relay() -> None:
    global _relay
    async with _relay_lock:
        relay = _relay
        _relay = None
    if relay is None:
        return
    if relay.task is not None and not relay.task.done():
        relay.task.cancel()
        await asyncio.gather(relay.task, return_exceptions=True)
    await relay.close_resources()
