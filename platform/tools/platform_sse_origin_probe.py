#!/usr/bin/env python3
"""Run an isolated loopback-only SSE origin capacity probe.

This intentionally skips the retained tournament workflow setup. It creates
one public test tournament in the isolated test database, opens anonymous
streams against the direct API origin, samples host sockets/load, and removes
that exact tournament and organizer before returning.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import resource
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from redis.asyncio import from_url
from sqlalchemy import delete

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from apps.platform_api.app.services.bracket_events import bracket_channel
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import Tournament, User
from python_packages.platform_infra.sse_connection_limit import (
    SSE_GLOBAL_LIMIT,
    SSE_LOAD_TEST_BYPASS_HEADER,
    sse_load_test_bypass_token,
)
from tools.platform_production_qa import load_env_file, percentile


DEFAULT_ORIGIN = "http://127.0.0.1:8010"
TEST_SSE_GLOBAL_KEY = "platform:sse-limit:v1:global"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--connections", type=int, default=1_000)
    parser.add_argument("--hold-seconds", type=float, default=5.0)
    parser.add_argument("--open-concurrency", type=int, default=32)
    parser.add_argument("--probe-delay", type=float, default=1.5)
    parser.add_argument("--post-open-settle-seconds", type=float, default=5.0)
    parser.add_argument("--event-count", type=int, default=1)
    parser.add_argument("--event-interval", type=float, default=0.2)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    parsed = urlsplit(args.origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("origin probe is restricted to an HTTP loopback origin")
    if not 1 <= args.connections <= SSE_GLOBAL_LIMIT:
        raise SystemExit(f"connections must be between 1 and {SSE_GLOBAL_LIMIT}")
    if not 0.1 <= args.hold_seconds <= 900:
        raise SystemExit("hold-seconds must be between 0.1 and 900")
    if not 1 <= args.open_concurrency <= args.connections:
        raise SystemExit("open-concurrency must be between 1 and connections")
    if not 0 <= args.probe_delay <= 60:
        raise SystemExit("probe-delay must be between 0 and 60")
    if not 0 <= args.post_open_settle_seconds <= 60:
        raise SystemExit("post-open-settle-seconds must be between 0 and 60")
    if not 0 <= args.event_count <= 100:
        raise SystemExit("event-count must be between 0 and 100")
    if not 0 <= args.event_interval <= 60:
        raise SystemExit("event-interval must be between 0 and 60")
    if not 1 <= args.http_timeout <= 600:
        raise SystemExit("http-timeout must be between 1 and 600")
    if not 0.1 <= args.sample_interval <= 10:
        raise SystemExit("sample-interval must be between 0.1 and 10")


def tcp_established(port: int) -> int:
    port_hex = f"{port:04X}"
    total = 0
    for proc_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        with suppress(OSError):
            for line in proc_path.read_text(encoding="utf-8").splitlines()[1:]:
                columns = line.split()
                if (
                    len(columns) >= 4
                    and columns[1].rsplit(":", 1)[-1].upper() == port_hex
                    and columns[3] == "01"
                ):
                    total += 1
    return total


def server_snapshot(*, api_port: int) -> dict[str, int | float]:
    load_1m, load_5m, load_15m = os.getloadavg()
    return {
        "api_established": tcp_established(api_port),
        "redis_established": tcp_established(6379),
        "postgres_established": tcp_established(5432),
        "load_1m": round(load_1m, 2),
        "load_5m": round(load_5m, 2),
        "load_15m": round(load_15m, 2),
    }


def client_resource_limits() -> dict[str, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {"nofile_soft": int(soft), "nofile_hard": int(hard)}


def raise_client_descriptor_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(32_768, int(hard))
    if int(soft) < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


async def run_probe(args: argparse.Namespace) -> dict[str, object]:
    validate_args(args)
    raise_client_descriptor_limit()
    env_file = args.env_file
    if env_file is None:
        configured = os.environ.get("PLATFORM_ENV_FILE", "").strip()
        env_file = Path(configured) if configured else PLATFORM_ROOT / ".env.platform"
    load_env_file(env_file)
    settings = get_settings()
    if settings.platform_environment != "test":
        raise RuntimeError("origin probe requires PLATFORM_ENVIRONMENT=test")
    if "platformdb_test" not in settings.platform_database_url:
        raise RuntimeError("origin probe requires platformdb_test")
    if not settings.platform_redis_url.rstrip("/").endswith("/15"):
        raise RuntimeError("origin probe requires Redis database 15")

    admission_store = from_url(settings.platform_redis_url)
    try:
        await admission_store.delete(TEST_SSE_GLOBAL_KEY)
    finally:
        await admission_store.aclose()

    parsed_origin = urlsplit(args.origin)
    api_port = parsed_origin.port or 80
    slug = f"local-sse-{uuid4().hex[:12]}"
    tournament_id = str(uuid4())
    organizer_id = str(uuid4())
    async with session_factory()() as db_session:
        db_session.add(
            User(
                id=organizer_id,
                email=f"{slug}@example.test",
                display_name="Local SSE",
            )
        )
        await db_session.flush()
        db_session.add(
            Tournament(
                id=tournament_id,
                slug=slug,
                name=f"Local SSE {slug[-8:]}",
                format_slug="single_elimination",
                allowed_ranks=[],
                max_participants=10_000,
                visibility="public",
                status="registration_open",
                organizer_user_id=organizer_id,
                bracket_revision=0,
            )
        )
        await db_session.commit()

    statuses: Counter[str] = Counter()
    errors: list[dict[str, object]] = []
    connect_latencies: list[float] = []
    active_connections = 0
    max_active_connections = 0
    completed = 0
    events = 0
    bytes_received = 0
    active_lock = asyncio.Lock()
    attempts_finished = 0
    attempts_lock = asyncio.Lock()
    all_attempts_done = asyncio.Event()
    open_gate = asyncio.Semaphore(args.open_concurrency)
    samples: list[dict[str, int | float]] = []
    stop_sampler = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def sample_server() -> None:
        while not stop_sampler.is_set():
            samples.append(server_snapshot(api_port=api_port))
            try:
                await asyncio.wait_for(
                    stop_sampler.wait(), timeout=args.sample_interval
                )
            except TimeoutError:
                pass
        samples.append(server_snapshot(api_port=api_port))

    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        SSE_LOAD_TEST_BYPASS_HEADER: sse_load_test_bypass_token(settings),
    }
    sampler_task = asyncio.create_task(sample_server())
    started_at = time.monotonic()
    try:
        limits = httpx.Limits(
            max_connections=args.connections,
            max_keepalive_connections=args.connections,
        )
        timeout = httpx.Timeout(
            connect=min(10.0, args.http_timeout),
            read=None,
            write=min(10.0, args.http_timeout),
            pool=min(10.0, args.http_timeout),
        )
        async with httpx.AsyncClient(
            base_url=f"{args.origin.rstrip('/')}/api/v1",
            limits=limits,
            timeout=timeout,
        ) as client:

            async def consume(index: int) -> None:
                nonlocal active_connections
                nonlocal max_active_connections
                nonlocal completed, events, bytes_received
                nonlocal attempts_finished
                request_started = time.monotonic()
                response_context = None
                opened = False
                attempt_marked = False

                async def mark_attempt_finished() -> None:
                    nonlocal attempts_finished, attempt_marked
                    if attempt_marked:
                        return
                    attempt_marked = True
                    async with attempts_lock:
                        attempts_finished += 1
                        if attempts_finished >= args.connections:
                            all_attempts_done.set()

                try:
                    async with open_gate:
                        response_context = client.stream(
                            "GET",
                            f"/tournaments/{slug}/bracket/events",
                            headers={
                                **headers,
                                "X-Request-ID": f"local-sse-{index}",
                            },
                        )
                        response = await response_context.__aenter__()
                    statuses[str(response.status_code)] += 1
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")
                        if len(errors) < 25:
                            errors.append(
                                {
                                    "type": "HTTPStatus",
                                    "status": response.status_code,
                                    "body": body[:300],
                                    "request_id": response.headers.get(
                                        "x-request-id"
                                    ),
                                    "index": index,
                                }
                            )
                        await mark_attempt_finished()
                        return
                    opened = True
                    connect_latencies.append(
                        (time.monotonic() - request_started) * 1000
                    )
                    async with active_lock:
                        active_connections += 1
                        max_active_connections = max(
                            max_active_connections, active_connections
                        )
                    await mark_attempt_finished()
                    current_event = False
                    try:
                        await all_attempts_done.wait()
                        async with asyncio.timeout(args.hold_seconds):
                            async for line in response.aiter_lines():
                                bytes_received += len(line.encode("utf-8")) + 1
                                if line == "event: bracket":
                                    current_event = True
                                elif current_event and line.startswith("data: "):
                                    events += 1
                                    current_event = False
                    except TimeoutError:
                        completed += 1
                except Exception as exc:
                    await mark_attempt_finished()
                    if len(errors) < 25:
                        errors.append(
                            {
                                "type": type(exc).__name__,
                                "message": str(exc)[:300],
                                "index": index,
                                "elapsed_ms": round(
                                    (time.monotonic() - request_started) * 1000,
                                    2,
                                ),
                            }
                        )
                finally:
                    if response_context is not None:
                        with suppress(Exception):
                            await response_context.__aexit__(None, None, None)
                    if opened:
                        async with active_lock:
                            active_connections = max(0, active_connections - 1)

            tasks = [
                asyncio.create_task(consume(index))
                for index in range(args.connections)
            ]
            publisher = from_url(settings.platform_redis_url, decode_responses=True)
            try:
                await publisher.ping()
                await asyncio.sleep(args.probe_delay)
                await all_attempts_done.wait()
                await asyncio.sleep(args.post_open_settle_seconds)
                for event_index in range(args.event_count):
                    await publisher.publish(
                        bracket_channel(tournament_id),
                        json.dumps(
                            {
                                "type": "qa_sse_probe",
                                "qa_published_at_ms": int(time.time() * 1000),
                                "event_index": event_index,
                            },
                            separators=(",", ":"),
                        ),
                    )
                    if event_index + 1 < args.event_count:
                        await asyncio.sleep(args.event_interval)
            except Exception as exc:
                if len(errors) < 25:
                    errors.append(
                        {
                            "type": type(exc).__name__,
                            "message": str(exc)[:300],
                            "component": "redis_publisher",
                        }
                    )
            finally:
                await publisher.aclose()
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        stop_sampler.set()
        await sampler_task
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(Tournament).where(Tournament.id == tournament_id)
            )
            await db_session.execute(delete(User).where(User.id == organizer_id))
            await db_session.commit()
        await dispose_engine()
        admission_store = from_url(settings.platform_redis_url)
        try:
            await admission_store.delete(TEST_SSE_GLOBAL_KEY)
        finally:
            await admission_store.aclose()

    return {
        "origin": args.origin,
        "target_connections": args.connections,
        "hold_seconds": args.hold_seconds,
        "open_concurrency": args.open_concurrency,
        "post_open_settle_seconds": args.post_open_settle_seconds,
        "client_resource_limits": client_resource_limits(),
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "statuses": dict(sorted(statuses.items())),
        "connected": int(statuses.get("200", 0)),
        "max_active_connections": max_active_connections,
        "completed": completed,
        "errors": len(errors),
        "error_samples": errors,
        "events": events,
        "expected_events": int(statuses.get("200", 0)) * args.event_count,
        "bytes_received": bytes_received,
        "connect_latency_ms": {
            "count": len(connect_latencies),
            "p50": percentile(connect_latencies, 50),
            "p95": percentile(connect_latencies, 95),
            "p99": percentile(connect_latencies, 99),
        },
        "server_peak": {
            key: max(sample[key] for sample in samples)
            for key in samples[0]
        },
        "server_last": samples[-1],
        "server_samples": len(samples),
        "passed": (
            statuses.get("200", 0) == args.connections
            and not errors
            and events >= statuses.get("200", 0) * args.event_count
            and max_active_connections <= SSE_GLOBAL_LIMIT
        ),
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def async_main() -> int:
    args = parse_args()
    result = await run_probe(args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
