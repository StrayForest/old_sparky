from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any

from apps.platform_api.app.services.tournament_workspace_access import (
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


def bracket_channel(tournament_id: str) -> str:
    return f"platform:bracket:{tournament_id}"


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

    client = redis_client()
    pubsub = client.pubsub()
    channel = bracket_channel(tournament_id)
    await pubsub.subscribe(channel)
    started_at = time.monotonic()
    last_access_check_at = started_at
    retry_ms = SSE_RECONNECT_MIN_MS + secrets.randbelow(SSE_RECONNECT_JITTER_MS + 1)
    try:
        yield f"retry: {retry_ms}\nevent: connected\ndata: {{}}\n\n"
        while True:
            remaining_seconds = (
                SSE_STREAM_MAX_LIFETIME_SECONDS - (time.monotonic() - started_at)
            )
            if remaining_seconds <= 0:
                break
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=min(float(SSE_KEEPALIVE_SECONDS), remaining_seconds),
            )
            now = time.monotonic()
            if message is not None or (
                now - last_access_check_at >= SSE_ACCESS_REVALIDATION_SECONDS
            ):
                if not await current_tournament_stream_access_is_valid(tournament_id):
                    break
                last_access_check_at = now
            if message is None:
                yield ": keepalive\n\n"
                continue
            yield f"event: bracket\ndata: {message['data']}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()
