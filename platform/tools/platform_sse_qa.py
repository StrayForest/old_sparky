#!/usr/bin/env python3
"""Measure retained SSE connection admission, fan-out and reconnect behavior."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import resource
import secrets
import ssl
import sys
import time
import traceback
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from redis.asyncio import Redis, from_url

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from apps.platform_api.app.services.bracket_events import bracket_channel
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine
from python_packages.platform_infra.sse_connection_limit import (
    SSE_GLOBAL_LIMIT,
    SSE_LOAD_TEST_CAPACITY_HEADER,
    SSE_LOAD_TEST_BYPASS_HEADER,
    SSE_QA_GLOBAL_LIMIT_MAX,
    sse_load_test_capacity_token,
    sse_load_test_bypass_token,
)
from tools.platform_production_qa import (
    BROWSER_POLLING_TOURNAMENT_PLAN,
    ProductionQa,
    VALID_RANKS,
    load_env_file,
    percentile,
)

DEFAULT_REPORT_PATH = Path("/tmp/platform-sse-qa-report.json")
DEFAULT_ORIGIN = "http://127.0.0.1"
SSE_EVENT_TYPE = "qa_sse_probe"
COMBINED_TIMEOUT_GRACE_SECONDS = 15.0
SSE_STREAM_CLOSE_TIMEOUT_SECONDS = 0.25
SSE_FIXTURE_TIMEOUT_SECONDS = 90.0
READY_CHECK_FIXTURE_TIMEOUT_SECONDS = 900.0
# This is a bounded QA hold, not a production stream lifetime. It is long
# enough to prove that a healthy stream survives the old 600-second rotation
# boundary while keeping an operator mistake bounded to fifteen minutes.
SSE_HOLD_MAX_SECONDS = 900.0
READY_CHECK_AGENDA_OPEN_RATE_PER_SECOND = 25.0
READY_CHECK_FIXTURE_MIN_LEAD_SECONDS = 900.0
READY_CHECK_FIXTURE_SETUP_GRACE_SECONDS = 600.0
SSE_PLATEAU_PROBE_CONNECTIONS = 10
NORMAL_TRAFFIC_USER_LIMIT = 32
NORMAL_TRAFFIC_REQUEST_CONCURRENCY = 8
NORMAL_TRAFFIC_INTERVAL_SECONDS = 15.0
NORMAL_TRAFFIC_MUTATION_INTERVAL_SECONDS = 30.0
# The browser keeps its own immediate-polling/full-jitter recovery policy. This
# is only the retained-load diagnostic ceiling: public edge queueing must be
# observable for a full minute before the harness classifies an attempt as
# fallback-eligible.
SSE_OPEN_TIMEOUT_MAX_PUBLIC_SECONDS = 60.0
SSE_OPEN_TIMEOUT_MAX_ORIGIN_LOCAL_SECONDS = 60.0
SSE_RECONNECT_MAX_BACKOFF_SECONDS = 5.0


def sse_open_delay_seconds(
    *,
    index: int,
    open_rate_per_second: float,
    scheduled_open_at: float | None = None,
    now_epoch: float | None = None,
) -> float:
    """Honor a signed per-user admission slot while retaining open-rate pacing."""

    rate_delay = (
        max(0, index) / open_rate_per_second
        if open_rate_per_second > 0
        else 0.0
    )
    scheduled_delay = 0.0
    if scheduled_open_at is not None:
        reference_epoch = now_epoch if now_epoch is not None else time.time()
        scheduled_delay = max(0.0, scheduled_open_at - reference_epoch)
    return max(rate_delay, scheduled_delay)


def ready_check_fixture_schedule(
    *,
    now: datetime,
    user_count: int,
    agenda_open_rate_per_second: float = READY_CHECK_AGENDA_OPEN_RATE_PER_SECOND,
) -> dict[str, datetime]:
    """Leave enough time for paced public agenda setup before the Ready Check."""

    agenda_rate = max(0.1, agenda_open_rate_per_second)
    agenda_span_seconds = max(0, user_count) / agenda_rate
    ready_check_lead_seconds = max(
        READY_CHECK_FIXTURE_MIN_LEAD_SECONDS,
        agenda_span_seconds + READY_CHECK_FIXTURE_SETUP_GRACE_SECONDS,
    )
    ready_check_starts_at = now + timedelta(seconds=ready_check_lead_seconds)
    return {
        "registration_starts_at": now + timedelta(minutes=1),
        "registration_closes_at": ready_check_starts_at - timedelta(minutes=1),
        "ready_check_starts_at": ready_check_starts_at,
        "ready_check_ends_at": ready_check_starts_at + timedelta(minutes=10),
        "captain_selection_starts_at": ready_check_starts_at + timedelta(minutes=11),
        "starts_at": ready_check_starts_at + timedelta(minutes=12),
    }


async def _close_sse_stream_context(stream_context: Any | None) -> None:
    """Close a timed-out httpx stream without extending the open budget."""

    if stream_context is None:
        return
    with suppress(Exception):
        await asyncio.wait_for(
            stream_context.__aexit__(None, None, None),
            timeout=SSE_STREAM_CLOSE_TIMEOUT_SECONDS,
        )


class _RawSseStream:
    """Small HTTP/1.1 SSE client for high-volume loopback measurements.

    httpx is intentionally retained for the public/Cloudflare contour, where
    its normal transport gives us the production-facing response metadata. A
    loopback capacity run is different: it creates thousands of long-lived
    sockets on the same two-core VPS. The standard client and per-line decoder
    can consume a full core before the API is saturated, which makes the load
    generator—not the origin—the measured bottleneck. This client keeps the
    same HTTP/1.1 request and SSE parsing contract with much less overhead.
    """

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.request_headers = headers
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.status_code = 0
        self.headers_received: dict[str, str] = {}
        self._error_body: bytes | None = None

    async def __aenter__(self) -> "_RawSseStream":
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SSE origin must use http or https with a hostname.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ssl_context = None
        server_hostname = None
        if parsed.scheme == "https":
            ssl_context = ssl.create_default_context()
            server_hostname = parsed.hostname
        self.reader, self.writer = await asyncio.open_connection(
            parsed.hostname,
            port,
            ssl=ssl_context,
            server_hostname=server_hostname,
            limit=128 * 1024,
        )
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = (parsed.scheme == "http" and port == 80) or (
            parsed.scheme == "https" and port == 443
        )
        host_header = host if default_port else f"{host}:{port}"
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        request_lines = [
            f"GET {request_target} HTTP/1.1",
            f"Host: {host_header}",
            "Connection: keep-alive",
        ]
        request_lines.extend(
            f"{name}: {value}" for name, value in self.request_headers.items()
        )
        self.writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode("utf-8"))
        await self.writer.drain()
        header_block = await self.reader.readuntil(b"\r\n\r\n")
        header_lines = header_block[:-4].split(b"\r\n")
        if not header_lines:
            raise OSError("SSE origin returned an empty HTTP response.")
        status_parts = header_lines[0].decode("latin-1").split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise OSError("SSE origin returned an invalid HTTP status line.")
        self.status_code = int(status_parts[1])
        for raw_line in header_lines[1:]:
            name, separator, value = raw_line.decode("latin-1").partition(":")
            if separator:
                self.headers_received[name.strip().lower()] = value.strip()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    @property
    def headers(self) -> httpx.Headers:
        return httpx.Headers(self.headers_received)

    async def aread(self) -> bytes:
        if self._error_body is not None:
            return self._error_body
        if self.reader is None or self.status_code == 200:
            self._error_body = b""
            return self._error_body
        raw_length = self.headers_received.get("content-length", "0")
        try:
            length = min(4096, max(0, int(raw_length)))
        except ValueError:
            length = 4096
        body = bytearray()
        async for chunk in self._body_chunks():
            body.extend(chunk)
            if len(body) >= length:
                break
        self._error_body = bytes(body[:length])
        return self._error_body

    async def _body_chunks(self):
        if self.reader is None:
            return
        if "chunked" not in self.headers_received.get("transfer-encoding", "").lower():
            content_length = self.headers_received.get("content-length")
            try:
                remaining = max(0, int(content_length)) if content_length is not None else None
            except ValueError:
                remaining = None
            if remaining is not None:
                while remaining:
                    chunk = await self.reader.read(min(64 * 1024, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                    yield chunk
                return
            while True:
                chunk = await self.reader.read(64 * 1024)
                if not chunk:
                    return
                yield chunk
        else:
            while True:
                size_line = await self.reader.readline()
                if not size_line:
                    return
                raw_size = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(raw_size, 16)
                except ValueError as exc:
                    raise OSError("SSE origin returned an invalid chunk size.") from exc
                if size == 0:
                    # Consume the optional trailer block before returning.
                    while await self.reader.readline() not in {b"\r\n", b"\n", b""}:
                        pass
                    return
                yield await self.reader.readexactly(size)
                await self.reader.readexactly(2)

    async def aiter_lines(self):
        pending = bytearray()
        async for chunk in self._body_chunks():
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(pending[:newline]).rstrip(b"\r")
                del pending[: newline + 1]
                yield line.decode("utf-8", errors="replace")
        if pending:
            yield bytes(pending).rstrip(b"\r").decode("utf-8", errors="replace")

    async def close(self) -> None:
        writer = self.writer
        self.writer = None
        self.reader = None
        if writer is None:
            return
        writer.close()
        with suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=SSE_STREAM_CLOSE_TIMEOUT_SECONDS)


def load_generator_resource_limits() -> dict[str, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        "nofile_soft": int(soft),
        "nofile_hard": int(hard),
    }


def max_sse_open_timeout_seconds(origin: str) -> float:
    """Return the QA handshake ceiling for the selected transport target."""

    hostname = urlsplit(str(origin)).hostname
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return SSE_OPEN_TIMEOUT_MAX_ORIGIN_LOCAL_SECONDS
    return SSE_OPEN_TIMEOUT_MAX_PUBLIC_SECONDS


def plateau_probe_count(*, capacity_limit: int, connection_count: int) -> int:
    """Return the explicit N+10 probe size for a signed-cap plateau run."""

    return (
        SSE_PLATEAU_PROBE_CONNECTIONS
        if capacity_limit > 0 and connection_count == capacity_limit
        else 0
    )


class SseMetrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.response_statuses: Counter[str] = Counter()
        self.connect_latencies: list[float] = []
        self.event_latencies: list[float] = []
        self.initial_connected_user_ids: set[str] = set()
        self.error_samples: list[dict[str, Any]] = []
        self.response_error_samples: list[dict[str, Any]] = []
        self.max_error_samples = 25

    def mark(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def connection_opened(self, *, user_id: str | None = None) -> None:
        self.mark("connected")
        self.mark("active_connections")
        if user_id is not None:
            self.initial_connected_user_ids.add(str(user_id))
        self.counters["max_active_connections"] = max(
            self.counters["max_active_connections"],
            self.counters["active_connections"],
        )

    def response_status(self, status_code: int) -> None:
        self.response_statuses[str(status_code)] += 1

    def connection_closed(self) -> None:
        if self.counters["active_connections"] > 0:
            self.mark("active_connections", -1)
        self.mark("disconnects")

    def record_error(
        self,
        error: BaseException,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.mark("errors")
        if len(self.error_samples) < self.max_error_samples:
            sample: dict[str, Any] = {
                "type": type(error).__name__,
                "message": str(error)[:300],
            }
            if details:
                sample.update(details)
            self.error_samples.append(sample)

    def record_response_error(
        self,
        *,
        status_code: int,
        body: bytes,
        headers: httpx.Headers,
        request_id: str | None = None,
        method: str = "GET",
        path: str = "/ready-check/events",
    ) -> None:
        if len(self.response_error_samples) >= self.max_error_samples:
            return
        safe_headers = {
            name.lower(): (
                "<redacted>"
                if any(
                    marker in name.lower()
                    for marker in (
                        "authorization",
                        "cookie",
                        "set-cookie",
                        "token",
                        "secret",
                        "password",
                        "credential",
                        "signature",
                    )
                )
                else value[:500]
            )
            for name, value in headers.items()
        }
        sample: dict[str, Any] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "method": method,
            "path": path.split("?", 1)[0],
            "status": status_code,
            "headers": safe_headers,
            "content_type": safe_headers.get("content-type", ""),
            "server": safe_headers.get("server", ""),
            "body": body[:4096].decode("utf-8", errors="replace"),
        }
        if request_id:
            sample["request_id"] = request_id
        for header_name in (
            "cf-ray",
            "cf-cache-status",
            "cf-error-type",
            "date",
            "retry-after",
            "transfer-encoding",
            "x-request-id",
        ):
            value = headers.get(header_name)
            if value:
                sample[header_name.replace("-", "_")] = value[:200]
        self.response_error_samples.append(sample)

    def summary(self) -> dict[str, Any]:
        attempts = self.counters["connection_attempts"]
        connected = self.counters["connected"]
        return {
            "connection_attempts": attempts,
            "connected": connected,
            "initial_connected": self.counters["initial_connected"],
            "initial_connected_user_count": len(self.initial_connected_user_ids),
            "max_active_connections": self.counters["max_active_connections"],
            "active_connections": self.counters["active_connections"],
            "completed": self.counters["completed"],
            "reconnects": self.counters["reconnects"],
            "disconnects": self.counters["disconnects"],
            "rejected_429": self.counters["rejected_429"],
            "rejected_503": self.counters["rejected_503"],
            "rejected_other": self.counters["rejected_other"],
            "open_timeouts": self.counters["open_timeouts"],
            "fallback_polling_eligible": self.counters["fallback_polling_eligible"],
            "errors": self.counters["errors"],
            "keepalives": self.counters["keepalives"],
            "events": self.counters["events"],
            "resyncs": self.counters["resyncs"],
            "bytes_received": self.counters["bytes_received"],
            "connected_percent": round(connected / attempts * 100, 3) if attempts else 0.0,
            "connect_latency_ms": {
                "count": len(self.connect_latencies),
                "p50": percentile(self.connect_latencies, 50),
                "p95": percentile(self.connect_latencies, 95),
                "p99": percentile(self.connect_latencies, 99),
            },
            "event_delivery_latency_ms": {
                "count": len(self.event_latencies),
                "p50": percentile(self.event_latencies, 50),
                "p95": percentile(self.event_latencies, 95),
                "p99": percentile(self.event_latencies, 99),
            },
            "error_samples": list(self.error_samples),
            "response_error_samples": list(self.response_error_samples),
            "response_statuses": dict(sorted(self.response_statuses.items())),
        }


class NormalApiMetrics:
    """Bounded metrics for ordinary authenticated traffic during SSE load."""

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.errors = 0
        self.statuses: Counter[str] = Counter()
        self.kinds: Counter[str] = Counter()
        self.latencies: list[float] = []
        self.error_samples: list[dict[str, Any]] = []

    def record_success(
        self,
        *,
        status_code: int,
        elapsed_ms: float,
        kind: str,
    ) -> None:
        self.attempts += 1
        self.successes += 1
        self.statuses[str(status_code)] += 1
        self.kinds[kind] += 1
        self.latencies.append(elapsed_ms)

    def record_error(
        self,
        error: BaseException,
        *,
        path: str,
        elapsed_ms: float,
        kind: str,
    ) -> None:
        self.attempts += 1
        self.errors += 1
        self.kinds[f"{kind}:error"] += 1
        if len(self.error_samples) < 25:
            self.error_samples.append(
                {
                    "type": type(error).__name__,
                    "message": str(error)[:300],
                    "path": path,
                    "kind": kind,
                    "elapsed_ms": round(elapsed_ms, 2),
                }
            )

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "errors": self.errors,
            "statuses": dict(sorted(self.statuses.items())),
            "kinds": dict(sorted(self.kinds.items())),
            "latency_ms": {
                "count": len(self.latencies),
                "p50": percentile(self.latencies, 50),
                "p95": percentile(self.latencies, 95),
                "p99": percentile(self.latencies, 99),
            },
            "error_samples": list(self.error_samples),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run retained SSE-only or polling+SSE production QA."
    )
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument(
        "--request-origin",
        default=None,
        help="Origin header to send when --origin points at a direct origin address.",
    )
    parser.add_argument(
        "--fixture-origin",
        default=None,
        help=(
            "Origin for authenticated fixture/control requests. Ready Check "
            "measurement remains on --origin; production runs use loopback here."
        ),
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--summary-path", type=Path, default=None)
    parser.add_argument(
        "--ready-file",
        type=Path,
        default=None,
        help="Write a private readiness marker after fixture/ticket setup and before opening SSE.",
    )
    parser.add_argument("--control-email", required=True)
    parser.add_argument("--target-sha", default=None)
    parser.add_argument("--github-run-id", type=int, default=None)
    parser.add_argument("--mode", choices=("sse", "combined"), required=True)
    parser.add_argument("--users-per-tournament", type=int, default=500)
    parser.add_argument("--sse-connections", type=int, default=128)
    parser.add_argument("--sse-duration", type=float, default=60.0)
    parser.add_argument("--sse-open-concurrency", type=int, default=256)
    parser.add_argument(
        "--sse-open-rate",
        type=float,
        default=0.0,
        help="Gradual-fill opening rate in new SSE/sec; zero keeps bounded burst behavior.",
    )
    parser.add_argument(
        "--sse-capacity-limit",
        type=int,
        default=0,
        help="Explicit QA-only global cap; high-cap runs are restricted to ticketed Ready Check.",
    )
    parser.add_argument(
        "--sse-open-timeout",
        type=float,
        default=5.0,
        help="Abort an SSE handshake after this many seconds and count it as polling fallback eligible.",
    )
    parser.add_argument("--sse-reconnect-cycles", type=int, default=0)
    parser.add_argument(
        "--sse-admission-mode",
        choices=("ticket", "legacy"),
        default="ticket",
        help="Use a signed workspace-issued ticket or the legacy DB-backed SSE admission path.",
    )
    parser.add_argument(
        "--sse-scope",
        choices=("bracket", "ready-check"),
        default="bracket",
        help="Measure the legacy bracket stream or the global Ready Check stream.",
    )
    parser.add_argument("--sse-event-count", type=int, default=3)
    parser.add_argument("--sse-event-interval", type=float, default=1.0)
    parser.add_argument("--combined-polling-duration", type=float, default=30.0)
    parser.add_argument("--combined-polling-open-stagger", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--http-max-connections", type=int, default=40)
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10.0,
        help="Bound each workload HTTP request so edge queues become a measured failure, not a multi-minute wait.",
    )
    parser.add_argument("--system-sample-interval", type=float, default=1.0)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    max_open_timeout = max_sse_open_timeout_seconds(args.origin)
    if not 0.5 <= args.sse_open_timeout <= max_open_timeout:
        parser.error(
            "--sse-open-timeout must be between 0.5 and "
            f"{max_open_timeout:g} seconds for this origin"
        )
    if not 1.0 <= args.sse_duration <= SSE_HOLD_MAX_SECONDS:
        parser.error(
            f"--sse-duration must be between 1 and {SSE_HOLD_MAX_SECONDS:g} seconds"
        )
    if not 0.0 <= args.sse_open_rate <= 1000.0:
        parser.error("--sse-open-rate must be between 0 and 1000 new SSE/sec")
    if not 1 <= args.sse_connections <= SSE_QA_GLOBAL_LIMIT_MAX:
        parser.error(
            f"--sse-connections must be between 1 and {SSE_QA_GLOBAL_LIMIT_MAX}"
        )
    if not 1 <= args.sse_open_concurrency <= SSE_QA_GLOBAL_LIMIT_MAX:
        parser.error(
            f"--sse-open-concurrency must be between 1 and {SSE_QA_GLOBAL_LIMIT_MAX}"
        )
    if not 0 <= args.sse_capacity_limit <= SSE_QA_GLOBAL_LIMIT_MAX:
        parser.error(
            f"--sse-capacity-limit must be between 0 and {SSE_QA_GLOBAL_LIMIT_MAX}"
        )
    if args.sse_scope == "ready-check" and args.sse_admission_mode != "ticket":
        parser.error("--sse-scope ready-check requires signed ticket admission")
    if args.sse_scope == "ready-check" and args.mode != "sse":
        parser.error("--sse-scope ready-check is supported only by --mode sse")
    return args


async def prepare_fixture(
    qa: ProductionQa,
    api_client: httpx.AsyncClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Build the smallest valid hot-key fixture for SSE admission tests.

    The browser-polling profile deliberately builds several tournament states,
    including background assignment workflows.  That is useful for polling
    coverage but makes it a poor prerequisite for an SSE capacity test: a
    delayed worker would be measured as SSE connect latency.  SSE authorization
    only needs a public tournament and authenticated sessions, so keep this
    fixture focused on the single Redis/channel hot key that the test measures.
    """

    with qa.phase("sse_seed_users"):
        users = await qa.bulk_register_scale_users()
    qa.scenario(
        "sse_users_created",
        len(users) == qa.scale_users,
        {"users": len(users), "requested": qa.scale_users},
    )

    if not users:
        raise RuntimeError("SSE fixture requires at least one synthetic user")
    organizer = users[0]
    await qa.grant_browser_polling_permissions([organizer], [])

    tournaments: list[dict[str, Any]] = []
    with qa.phase("sse_tournament_setup"):
        tournaments.append(
            await qa.create_browser_polling_tournament(
                api_client,
                organizer=organizer,
                category="registration_open",
                index=1,
            )
        )
        await qa.record_preprod_run(tournaments_created=1)
    qa.scenario(
        "sse_tournaments_created",
        len(tournaments) == 1,
        {"tournaments": len(tournaments), "fixture": "hot-public-single-tournament"},
    )
    qa.report["sse_fixture"] = {
        "profile": "hot-public-single-tournament",
        "background_workflows": False,
        "users": len(users),
        "tournaments": len(tournaments),
    }
    qa.report["planned_tournaments"] = 1
    return users, tournaments, [users]


async def prepare_ready_check_fixture(
    qa: ProductionQa,
    fixture_client: httpx.AsyncClient,
    *,
    agenda_client: httpx.AsyncClient,
    sse_capacity_token: str | None,
    agenda_open_rate_per_second: float = READY_CHECK_AGENDA_OPEN_RATE_PER_SECOND,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[list[dict[str, Any]]],
    dict[str, str],
    dict[str, str],
    dict[str, float],
]:
    """Build a ticketed, eligible Ready Check cohort for the SSE staircase.

    Fixture setup is deliberately completed before the measured connection
    phase. The agenda calls exercise the one-time PostgreSQL admission path;
    the measured stream and overflow probes exercise only HMAC/Redis hot
    paths. The active round is started after initial stream admission so the
    test measures event delivery and immediate client-side release as well.
    """

    with qa.phase("ready_check_seed_users"):
        users = await qa.bulk_register_scale_users()
    qa.scenario(
        "ready_check_users_created",
        len(users) == qa.scale_users,
        {"users": len(users), "requested": qa.scale_users},
    )
    if not users:
        raise RuntimeError("Ready Check fixture requires at least one synthetic user")

    organizer = users[0]
    now = datetime.now(UTC)
    schedule = ready_check_fixture_schedule(
        now=now,
        user_count=len(users),
        agenda_open_rate_per_second=agenda_open_rate_per_second,
    )
    registration_starts_at = schedule["registration_starts_at"]
    registration_closes_at = schedule["registration_closes_at"]
    ready_check_starts_at = schedule["ready_check_starts_at"]
    ready_check_ends_at = schedule["ready_check_ends_at"]
    captain_selection_starts_at = schedule["captain_selection_starts_at"]
    starts_at = schedule["starts_at"]
    with qa.phase("ready_check_tournament_setup"):
        created = await qa.request_as(
            fixture_client,
            organizer,
            "POST",
            "/tournaments",
            expected=201,
            json_payload={
                "name": f"RC {qa.marker[-20:]}"[:25],
                "description": f"Ready Check SSE profile {qa.marker}.",
                "visibility": "public",
                "format_slug": "solo",
                "allowed_ranks": VALID_RANKS,
                "max_participants": qa.scale_users,
                "registration_starts_at": registration_starts_at.isoformat(),
                "registration_closes_at": registration_closes_at.isoformat(),
                "ready_check_starts_at": ready_check_starts_at.isoformat(),
                "ready_check_ends_at": ready_check_ends_at.isoformat(),
                "captain_selection_starts_at": captain_selection_starts_at.isoformat(),
                "starts_at": starts_at.isoformat(),
                "match_format": "bo3",
                "final_format": "bo5",
                "teams_count": 2,
            },
        )
        tournament = {**created, "category": "ready_check_upcoming", "organizer": organizer}
        qa.tournament_id = str(created["id"])
        qa.tournament_slug = str(created["slug"])
        qa.tournament_ids.append(qa.tournament_id)
        qa.tournament_slugs.append(qa.tournament_slug)
        qa.report["tournament_ids"] = list(qa.tournament_ids)
        qa.report["tournament_slugs"] = list(qa.tournament_slugs)
        await qa.request_as(
            fixture_client,
            organizer,
            "PATCH",
            f"/tournaments/{qa.tournament_slug}/status",
            expected=200,
            json_payload={"status": "registration_open"},
        )
        await qa.record_preprod_run(tournaments_created=1)

    with qa.phase("ready_check_join_setup"):
        await qa.join_browser_polling_participants(
            fixture_client,
            tournament=tournament,
            participants=users,
        )
        await qa.request_as(
            fixture_client,
            organizer,
            "PATCH",
            f"/tournaments/{qa.tournament_slug}/status",
            expected=200,
            json_payload={"status": "registration_closed"},
        )

    sse_tickets: dict[str, str] = {}
    state_tickets: dict[str, str] = {}
    sse_open_at_by_user_id: dict[str, float] = {}

    async def issue_user_tickets(user: dict[str, Any]) -> None:
        extra_headers = (
            {SSE_LOAD_TEST_CAPACITY_HEADER: sse_capacity_token}
            if sse_capacity_token is not None
            else None
        )
        payload = await qa.request_as(
            agenda_client,
            user,
            "GET",
            "/ready-check/agenda",
            expected=200,
            extra_headers=extra_headers,
        )
        checks = payload.get("checks") if isinstance(payload, dict) else None
        check = next(
            (
                item
                for item in checks or []
                if isinstance(item, dict)
                and str(item.get("tournament_id")) == qa.tournament_id
            ),
            None,
        )
        stream_ticket = payload.get("sse_ticket") if isinstance(payload, dict) else None
        state_ticket = check.get("state_ticket") if isinstance(check, dict) else None
        if not isinstance(stream_ticket, str) or not stream_ticket:
            raise RuntimeError(f"Ready Check stream ticket missing for {user['label']}")
        if not isinstance(state_ticket, str) or not state_ticket:
            raise RuntimeError(f"Ready Check state ticket missing for {user['label']}")
        admission_open_at = check.get("admission_open_at") if isinstance(check, dict) else None
        if not isinstance(admission_open_at, str) or not admission_open_at:
            raise RuntimeError(f"Ready Check admission slot missing for {user['label']}")
        try:
            admission_open_time = datetime.fromisoformat(
                admission_open_at.replace("Z", "+00:00")
            )
            if admission_open_time.tzinfo is None or admission_open_time.utcoffset() is None:
                raise ValueError("Ready Check admission slot must include a timezone")
            admission_open_epoch = admission_open_time.timestamp()
        except ValueError as exc:
            raise RuntimeError(
                f"Ready Check admission slot is invalid for {user['label']}"
            ) from exc
        sse_tickets[str(user["id"])] = stream_ticket
        state_tickets[str(user["id"])] = state_ticket
        sse_open_at_by_user_id[str(user["id"])] = admission_open_epoch

    with qa.phase("ready_check_admission_tickets"):
        await qa.bounded_each_at_rate(
            users,
            issue_user_tickets,
            rate_per_second=agenda_open_rate_per_second,
        )

    qa.report["sse_fixture"] = {
        "profile": "global-ready-check-cohort",
        "background_workflows": False,
        "users": len(users),
        "tournaments": 1,
        "agenda_requests": len(sse_tickets),
        "state_endpoint": "/ready-check/state",
        "scheduled_streams": len(sse_open_at_by_user_id),
        "admission_open_at_min": min(sse_open_at_by_user_id.values()),
        "admission_open_at_max": max(sse_open_at_by_user_id.values()),
        "agenda_open_rate_per_second": agenda_open_rate_per_second,
        "fixture_origin": str(fixture_client.base_url),
        "agenda_origin": str(agenda_client.base_url),
    }
    qa.report["planned_tournaments"] = 1
    return (
        users,
        [tournament],
        [users],
        sse_tickets,
        state_tickets,
        sse_open_at_by_user_id,
    )


async def issue_public_sse_admission_ticket(
    qa: ProductionQa,
    client: httpx.AsyncClient,
    tournament_slug: str,
) -> str:
    """Fetch one public bracket descriptor and retain its signed SSE proof."""

    response = await client.get(
        f"/tournaments/{quote(tournament_slug, safe='')}/bracket?teams_view=summary",
        headers={
            "Accept": "application/json",
            "Origin": qa.request_origin,
            "X-Platform-QA-Phase": qa.current_phase,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Public SSE admission ticket request failed with "
            f"HTTP {response.status_code}."
        )
    try:
        ticket = response.json().get("sse_admission_ticket")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Public SSE admission ticket response was not JSON.") from exc
    if not isinstance(ticket, str) or not ticket:
        raise RuntimeError("Public SSE admission ticket was missing from the bracket response.")
    return ticket


async def publish_probe_events(
    tournament_ids: list[str],
    *,
    count: int,
    interval_seconds: float,
) -> dict[str, Any]:
    if count <= 0 or not tournament_ids:
        return {"published_events": 0, "subscriber_counts": []}
    settings = get_settings()
    client: Redis = from_url(settings.platform_redis_url, decode_responses=True)
    subscriber_counts: list[int] = []
    try:
        for event_index in range(count):
            published_at_ms = int(time.time() * 1000)
            payload = json.dumps(
                {
                    "type": SSE_EVENT_TYPE,
                    "revision": event_index + 1,
                    "qa_published_at_ms": published_at_ms,
                },
                separators=(",", ":"),
            )
            deliveries = await asyncio.gather(
                *(
                    client.publish(bracket_channel(tournament_id), payload)
                    for tournament_id in tournament_ids
                )
            )
            subscriber_counts.append(sum(int(delivery) for delivery in deliveries))
            if event_index + 1 < count:
                await asyncio.sleep(max(0.0, interval_seconds))
    finally:
        await client.aclose()
    return {
        "published_events": len(subscriber_counts),
        "subscriber_counts": subscriber_counts,
        "max_subscribers_reported": max(subscriber_counts, default=0),
    }


async def probe_ready_check_overflow(
    qa: ProductionQa,
    api_client: httpx.AsyncClient,
    users: list[dict[str, Any]],
    *,
    tournament_slug: str,
    state_tickets: dict[str, str],
    metrics: SseMetrics,
) -> dict[str, Any]:
    """Probe only streams rejected by admission, once after the active event."""

    overflow_users = [
        user
        for user in users
        if str(user["id"]) not in metrics.initial_connected_user_ids
    ]
    status_counts: Counter[str] = Counter()

    async def probe_one(user: dict[str, Any]) -> None:
        user_id = str(user["id"])
        state_ticket = state_tickets.get(user_id)
        if not state_ticket:
            raise RuntimeError(f"Ready Check state ticket missing for {user['label']}")
        payload = await qa.request_as(
            api_client,
            user,
            "GET",
            (
                f"/ready-check/state?slug={quote(tournament_slug, safe='')}"
                f"&ticket={quote(state_ticket, safe='')}"
            ),
            expected=200,
        )
        state = str(payload.get("status")) if isinstance(payload, dict) else "missing"
        status_counts[state] += 1
        if state != "active":
            raise RuntimeError(
                f"Ready Check overflow probe did not observe active state for {user['label']}: {payload}"
            )

    request_gate = asyncio.Semaphore(max(1, min(256, qa.concurrency * 4)))
    with qa.phase("ready_check_overflow_state_probe"):
        await qa.bounded_each(overflow_users, probe_one, semaphore=request_gate)
    return {
        "users": len(overflow_users),
        "requests": len(overflow_users),
        "status_counts": dict(sorted(status_counts.items())),
        "route": "GET /ready-check/state",
        "trigger": "after_ready_check_start_event",
    }


async def consume_sse_connection(
    qa: ProductionQa,
    client: httpx.AsyncClient | None,
    metrics: SseMetrics,
    *,
    user: dict[str, Any],
    tournament_slug: str,
    sse_ticket: str | None,
    sse_ticket_by_user_id: dict[str, str] | None,
    sse_scope: str,
    duration_seconds: float,
    reconnect_cycles: int,
    open_gate: asyncio.Semaphore,
    open_timeout_seconds: float,
    open_delay_seconds: float,
    sse_capacity_token: str | None,
    all_attempts_done: asyncio.Event,
    mark_attempt_finished,
    hold_deadline_after_barrier,
    raw_sse_transport: bool,
) -> None:
    token = qa.session_tokens_by_user_id[str(user["id"])]
    headers = {
        "Accept": "text/event-stream, application/problem+json",
        "Cache-Control": "no-cache",
        "Origin": qa.request_origin,
        "Cookie": f"{qa.session_cookie_name}={token}",
        "X-Platform-QA-Phase": qa.current_phase,
    }
    origin_host = urlsplit(str(qa.api_origin)).hostname
    if origin_host in {"127.0.0.1", "localhost", "::1"}:
        # The loopback contour is an origin-only diagnostic. Its source IP is
        # necessarily loopback and cannot be configured as a production load
        # source, so use the existing signed QA bypass for source-cap analysis.
        # Global, per-user, Redis and route authorization protections remain.
        headers[SSE_LOAD_TEST_BYPASS_HEADER] = sse_load_test_bypass_token(
            get_settings()
        )
    if sse_capacity_token is not None:
        headers[SSE_LOAD_TEST_CAPACITY_HEADER] = sse_capacity_token
    deadline: float | None = None
    cycles = max(0, reconnect_cycles)
    per_cycle_hold = duration_seconds / (cycles + 1) if cycles else duration_seconds
    initial_attempt_marked = False
    initial_connection_marked = False

    if open_delay_seconds > 0:
        await asyncio.sleep(open_delay_seconds)

    async def wait_before_reconnect(attempt: int) -> None:
        if deadline is None:
            return
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return
        backoff_cap = min(
            SSE_RECONNECT_MAX_BACKOFF_SECONDS,
            0.25 * (2 ** min(attempt, 4)),
            remaining,
        )
        jitter = secrets.randbelow(1_000_000) / 1_000_000
        await asyncio.sleep(backoff_cap * jitter)

    for cycle in range(cycles + 1):
        # Reconnect cycles model recovery after an established stream.  A
        # client that never completed its initial handshake must fail closed
        # into the separate fallback path rather than retrying in a hot loop.
        if cycle > 0 and not initial_connection_marked:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        reconnect_attempt = 0
        while True:
            if cycle > 0 and deadline is not None and time.monotonic() >= deadline:
                break
            metrics.mark("connection_attempts")
            attempt_started = time.monotonic()
            request_id = f"sseqa-{secrets.token_hex(8)}"
            headers["X-Request-ID"] = request_id
            counters_before = {
                name: metrics.counters[name]
                for name in ("bytes_received", "events", "keepalives")
            }
            stream_context = None
            response = None
            connected_this_attempt = False
            open_timed_out = False
            retry_reconnect = False
            try:
                user_ticket = (
                    sse_ticket_by_user_id.get(str(user["id"]))
                    if sse_ticket_by_user_id is not None
                    else sse_ticket
                )
                if sse_scope == "ready-check":
                    stream_path = (
                        "/ready-check/events"
                        f"?ticket={quote(user_ticket or '', safe='')}"
                    )
                else:
                    stream_path = (
                        f"/tournaments/{quote(tournament_slug, safe='')}/bracket/events"
                        f"?ticket={quote(user_ticket, safe='')}"
                        if user_ticket
                        else f"/tournaments/{quote(tournament_slug, safe='')}/bracket/events"
                    )
                if raw_sse_transport:
                    stream_context = _RawSseStream(
                        f"{qa.api_origin}{stream_path}",
                        headers,
                    )
                else:
                    assert client is not None
                    stream_context = client.stream(
                        "GET",
                        stream_path,
                        headers=headers,
                    )
                try:
                    async with asyncio.timeout(max(0.1, open_timeout_seconds)):
                        async with open_gate:
                            response = await stream_context.__aenter__()
                except TimeoutError:
                    open_timed_out = True
                    metrics.mark("open_timeouts")
                    metrics.mark("fallback_polling_eligible")
                    if cycle > 0 and deadline is not None and time.monotonic() < deadline:
                        retry_reconnect = True
                    else:
                        break
                if open_timed_out:
                    await _close_sse_stream_context(stream_context)
                    stream_context = None
                    if retry_reconnect:
                        reconnect_attempt += 1
                        await wait_before_reconnect(reconnect_attempt)
                        continue
                    break
                metrics.response_status(response.status_code)
                if cycle == 0 and not initial_attempt_marked:
                    initial_attempt_marked = True
                    await mark_attempt_finished()
                if response.status_code != 200:
                    metrics.mark("fallback_polling_eligible")
                    if response.status_code == 429:
                        metrics.mark("rejected_429")
                    elif response.status_code == 503:
                        metrics.mark("rejected_503")
                    else:
                        metrics.mark("rejected_other")
                    with suppress(Exception):
                        metrics.record_response_error(
                            status_code=response.status_code,
                            body=await response.aread(),
                            headers=response.headers,
                            request_id=request_id,
                            method="GET",
                            path=stream_path,
                        )
                    if cycle > 0 and deadline is not None and time.monotonic() < deadline:
                        retry_reconnect = True
                    else:
                        break
                if retry_reconnect:
                    await _close_sse_stream_context(stream_context)
                    stream_context = None
                    reconnect_attempt += 1
                    await wait_before_reconnect(reconnect_attempt)
                    continue

                metrics.connection_opened(user_id=str(user["id"]))
                if not initial_connection_marked:
                    metrics.mark("initial_connected")
                    initial_connection_marked = True
                else:
                    metrics.mark("reconnects")
                connected_this_attempt = True
                metrics.connect_latencies.append(
                    (time.monotonic() - attempt_started) * 1000
                )
                if cycle == 0:
                    deadline = await hold_deadline_after_barrier(
                        all_attempts_done,
                        duration_seconds,
                    )
                cycle_deadline = min(deadline, time.monotonic() + per_cycle_hold)
                current_event = False
                try:
                    async with asyncio.timeout(
                        max(0.1, cycle_deadline - time.monotonic())
                    ):
                        async for line in response.aiter_lines():
                            metrics.mark("bytes_received", len(line.encode("utf-8")) + 1)
                            if line.startswith(": keepalive"):
                                metrics.mark("keepalives")
                            elif line == "event: connected":
                                current_event = False
                            elif line == "event: resync":
                                metrics.mark("resyncs")
                                current_event = False
                            elif line == f"event: {'ready_check' if sse_scope == 'ready-check' else 'bracket'}":
                                current_event = True
                            elif current_event and line.startswith("data: "):
                                metrics.mark("events")
                                current_event = False
                                with suppress(TypeError, ValueError):
                                    payload = json.loads(line[6:])
                                    published_at_ms = int(
                                        payload.get("qa_published_at_ms", 0)
                                    )
                                    if published_at_ms:
                                        metrics.event_latencies.append(
                                            max(
                                                0.0,
                                                time.time() * 1000 - published_at_ms,
                                            )
                                        )
                                if sse_scope == "ready-check":
                                    metrics.mark("ready_check_events")
                                    break
                            if time.monotonic() >= cycle_deadline:
                                break
                except TimeoutError:
                    metrics.mark("completed")
                else:
                    metrics.mark("completed")
                if cycle < cycles and time.monotonic() < deadline:
                    await wait_before_reconnect(0)
                break
            except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
                elapsed_ms = (time.monotonic() - attempt_started) * 1000
                if open_timed_out or (
                    response is None and elapsed_ms >= open_timeout_seconds * 1000
                ):
                    if not open_timed_out:
                        metrics.mark("open_timeouts")
                        metrics.mark("fallback_polling_eligible")
                    if cycle > 0 and deadline is not None and time.monotonic() < deadline:
                        retry_reconnect = True
                    else:
                        break
                else:
                    details: dict[str, Any] = {
                        "request_id": request_id,
                        "elapsed_ms": round(elapsed_ms, 2),
                        "bytes_received": metrics.counters["bytes_received"]
                        - counters_before["bytes_received"],
                        "events": metrics.counters["events"] - counters_before["events"],
                        "keepalives": metrics.counters["keepalives"]
                        - counters_before["keepalives"],
                    }
                    if response is not None:
                        details["status"] = response.status_code
                        for header_name in ("cf-ray", "server", "x-request-id"):
                            value = response.headers.get(header_name)
                            if value:
                                details[header_name.replace("-", "_")] = value[:200]
                    metrics.record_error(exc, details=details)
                    if cycle > 0 and deadline is not None and time.monotonic() < deadline:
                        retry_reconnect = True
                    else:
                        break
            except Exception as exc:  # pragma: no cover - defensive load-harness boundary
                metrics.record_error(
                    exc,
                    details={
                        "request_id": request_id,
                        "elapsed_ms": round(
                            (time.monotonic() - attempt_started) * 1000, 2
                        ),
                        "bytes_received": metrics.counters["bytes_received"]
                        - counters_before["bytes_received"],
                        "events": metrics.counters["events"] - counters_before["events"],
                        "keepalives": metrics.counters["keepalives"]
                        - counters_before["keepalives"],
                    },
                )
                break
            finally:
                if cycle == 0 and not initial_attempt_marked:
                    initial_attempt_marked = True
                    await mark_attempt_finished()
                if stream_context is not None:
                    await _close_sse_stream_context(stream_context)
                if connected_this_attempt:
                    metrics.connection_closed()
            if retry_reconnect:
                reconnect_attempt += 1
                await wait_before_reconnect(reconnect_attempt)


async def run_connections(
    qa: ProductionQa,
    users: list[dict[str, Any]],
    tournaments: list[dict[str, Any]],
    *,
    connection_count: int,
    duration_seconds: float,
    open_concurrency: int,
    open_timeout_seconds: float,
    sse_ticket: str | None,
    reconnect_cycles: int,
    event_count: int,
    event_interval: float,
    open_rate_per_second: float,
    sse_capacity_token: str | None,
    global_admission_limit: int,
    plateau_probe_connections: int,
    http_max_connections: int,
    sse_scope: str = "bracket",
    sse_ticket_by_user_id: dict[str, str] | None = None,
    sse_open_at_by_user_id: dict[str, float] | None = None,
    after_connection_barrier=None,
    fallback_probe=None,
) -> dict[str, Any]:
    metrics = SseMetrics()
    raw_sse_transport = urlsplit(str(qa.api_origin)).hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    client_connection_ceiling = max(
        1,
        http_max_connections,
        connection_count + max(0, plateau_probe_connections),
    )
    sse_client: httpx.AsyncClient | None = None
    if not raw_sse_transport:
        sse_client = httpx.AsyncClient(
            base_url=qa.api_origin,
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=max(10.0, open_timeout_seconds + 5.0),
                read=None,
                write=10.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_connections=client_connection_ceiling,
                max_keepalive_connections=client_connection_ceiling,
            ),
        )
        qa.clients.append(sse_client)
    open_gate = asyncio.Semaphore(max(1, open_concurrency))
    attempts_finished = 0
    attempts_lock = asyncio.Lock()
    all_attempts_done = asyncio.Event()
    hold_deadline = 0.0
    hold_deadline_lock = asyncio.Lock()

    async def mark_attempt_finished() -> None:
        nonlocal attempts_finished
        async with attempts_lock:
            attempts_finished += 1
            if attempts_finished >= connection_count:
                all_attempts_done.set()

    probe_attempts_finished = 0
    probe_attempts_done = asyncio.Event()
    probe_attempts_lock = asyncio.Lock()

    async def mark_probe_attempt_finished() -> None:
        nonlocal probe_attempts_finished
        async with probe_attempts_lock:
            probe_attempts_finished += 1
            if probe_attempts_finished >= plateau_probe_connections:
                probe_attempts_done.set()

    async def hold_deadline_after_barrier(
        barrier: asyncio.Event,
        hold_seconds: float,
    ) -> float:
        nonlocal hold_deadline
        await barrier.wait()
        async with hold_deadline_lock:
            if hold_deadline <= 0:
                hold_deadline = time.monotonic() + hold_seconds
            return hold_deadline

    def create_connection_tasks(
        count: int,
        *,
        index_offset: int,
        attempt_finished,
    ) -> list[asyncio.Task[None]]:
        connections = [
            (
                users[(index_offset + index) % len(users)],
                tournaments[(index_offset + index) % len(tournaments)],
            )
            for index in range(count)
        ]
        return [
            asyncio.create_task(
                consume_sse_connection(
                    qa,
                    sse_client,
                    metrics,
                    user=user,
                    tournament_slug=str(tournament["slug"]),
                    sse_ticket=sse_ticket,
                    sse_ticket_by_user_id=sse_ticket_by_user_id,
                    sse_scope=sse_scope,
                    duration_seconds=duration_seconds,
                    reconnect_cycles=reconnect_cycles,
                    open_gate=open_gate,
                    open_timeout_seconds=open_timeout_seconds,
                    open_delay_seconds=sse_open_delay_seconds(
                        index=index,
                        open_rate_per_second=open_rate_per_second,
                        scheduled_open_at=(
                            sse_open_at_by_user_id.get(str(user["id"]))
                            if sse_open_at_by_user_id is not None
                            else None
                        ),
                    ),
                    sse_capacity_token=sse_capacity_token,
                    all_attempts_done=all_attempts_done,
                    mark_attempt_finished=attempt_finished,
                    hold_deadline_after_barrier=hold_deadline_after_barrier,
                    raw_sse_transport=raw_sse_transport,
                )
            )
            for index, (user, tournament) in enumerate(connections)
        ]

    tasks = create_connection_tasks(
        connection_count,
        index_offset=0,
        attempt_finished=mark_attempt_finished,
    )
    probe_tasks: list[asyncio.Task[None]] = []
    plateau_report = {
        "requested_connections": max(0, plateau_probe_connections),
        "base_connected": 0,
        "probe_connected": 0,
        "probe_rejected_429": 0,
        "probe_rejected_503": 0,
        "probe_open_timeouts": 0,
        "executed": False,
    }
    publisher: asyncio.Task[dict[str, Any]] | None = None
    try:
        await all_attempts_done.wait()
        plateau_report["base_connected"] = metrics.counters["initial_connected"]
        if after_connection_barrier is not None:
            await after_connection_barrier()
        fallback_report: dict[str, Any] | None = None
        if fallback_probe is not None:
            fallback_report = await fallback_probe(metrics)
        if (
            plateau_probe_connections > 0
            and sse_capacity_token is not None
            and plateau_report["base_connected"] >= connection_count
        ):
            plateau_report["executed"] = True
            before_connected = metrics.counters["connected"]
            before_rejected_429 = metrics.counters["rejected_429"]
            before_rejected_503 = metrics.counters["rejected_503"]
            before_open_timeouts = metrics.counters["open_timeouts"]
            probe_tasks = create_connection_tasks(
                plateau_probe_connections,
                index_offset=connection_count,
                attempt_finished=mark_probe_attempt_finished,
            )
            await probe_attempts_done.wait()
            plateau_report.update(
                {
                    "probe_connected": metrics.counters["connected"] - before_connected,
                    "probe_rejected_429": metrics.counters["rejected_429"] - before_rejected_429,
                    "probe_rejected_503": metrics.counters["rejected_503"] - before_rejected_503,
                    "probe_open_timeouts": metrics.counters["open_timeouts"] - before_open_timeouts,
                }
            )
        await asyncio.sleep(min(2.0, max(0.1, duration_seconds / 4)))
        if sse_scope == "ready-check":
            publisher_report = {
                "published_events": 0,
                "subscriber_counts": [],
                "max_subscribers_reported": 0,
            }
        else:
            publisher = asyncio.create_task(
                publish_probe_events(
                    [str(tournament["id"]) for tournament in tournaments],
                    count=event_count,
                    interval_seconds=event_interval,
                )
            )
        await asyncio.gather(*tasks, *probe_tasks)
        if publisher is not None:
            publisher_report = await publisher
        return {
            "profile": "ready-check-sse" if sse_scope == "ready-check" else "sse-only",
            "target_connections": connection_count,
            "duration_seconds": duration_seconds,
            "open_concurrency": open_concurrency,
            "client_connection_ceiling": client_connection_ceiling,
            "transport": "asyncio-http11" if raw_sse_transport else "httpx",
            "open_timeout_seconds": open_timeout_seconds,
            "open_rate_per_second": open_rate_per_second,
            "capacity_mode": sse_capacity_token is not None,
            "admission_mode": (
                "ready-check-ticket"
                if sse_scope == "ready-check"
                else ("ticket" if sse_ticket else "legacy")
            ),
            "sse_scope": sse_scope,
            "reconnect_cycles": reconnect_cycles,
            "probe_event_count": event_count,
            "probe_event_interval_seconds": event_interval,
            "expected_events": (
                metrics.counters["initial_connected"]
                if sse_scope == "ready-check"
                else metrics.counters["initial_connected"] * max(0, event_count)
            ),
            "publisher": publisher_report,
            "fallback_probe": fallback_report,
            "application_global_admission_limit": global_admission_limit,
            "plateau_probe": plateau_report,
            "load_generator_resources": load_generator_resource_limits(),
            "metrics": metrics.summary(),
        }
    finally:
        if publisher is not None and not publisher.done():
            publisher.cancel()
        if publisher is not None:
            with suppress(asyncio.CancelledError, Exception):
                await publisher
        pending_tasks = [task for task in (*tasks, *probe_tasks) if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if sse_client is not None:
            with suppress(Exception):
                await sse_client.aclose()


async def run_normal_api_traffic(
    qa: ProductionQa,
    api_client: httpx.AsyncClient,
    users: list[dict[str, Any]],
    *,
    tournament_slug: str,
    stop_event: asyncio.Event,
    metrics: NormalApiMetrics,
) -> None:
    """Keep a bounded ordinary-user workload beside persistent SSE.

    This is intentionally small and deterministic. The browser-polling tasks
    already supply the large conditional workspace population; this companion
    workload exercises authenticated/session/profile reads and one
    organizer-owned, immediately reversible invite mutation. It keeps normal
    application traffic visible in the mixed-load decision without turning
    the test into a second uncontrolled burst.
    """

    sampled_users = users[: min(NORMAL_TRAFFIC_USER_LIMIT, len(users))]
    if not sampled_users:
        return
    request_gate = asyncio.Semaphore(NORMAL_TRAFFIC_REQUEST_CONCURRENCY)
    slug = quote(tournament_slug, safe="")
    workspace_path = (
        f"/tournaments/{slug}/workspace?participants_limit=0"
        "&participants_offset=0&workspace_view=detail&include_current_user=false"
    )
    bracket_path = f"/tournaments/{slug}/bracket?teams_view=summary"

    async def request(
        user: dict[str, Any],
        method: str,
        path: str,
        *,
        kind: str,
        expected: int | tuple[int, ...] = 200,
        json_payload: dict[str, Any] | None = None,
    ) -> Any | None:
        started = time.monotonic()
        try:
            async with request_gate:
                payload, status_code, _headers = await qa.request_as(
                    api_client,
                    user,
                    method,
                    path,
                    expected=expected,
                    json_payload=json_payload,
                    return_response_meta=True,
                )
        except Exception as exc:
            metrics.record_error(
                exc,
                path=path,
                elapsed_ms=(time.monotonic() - started) * 1000,
                kind=kind,
            )
            return None
        metrics.record_success(
            status_code=status_code,
            elapsed_ms=(time.monotonic() - started) * 1000,
            kind=kind,
        )
        return payload

    async def read_user(user: dict[str, Any]) -> None:
        for method, path, kind, expected in (
            ("GET", "/auth/session", "auth_session", 200),
            ("GET", "/users/me", "user", 200),
            ("GET", "/profiles/me", "profile", 200),
            ("GET", "/profiles/me/deadlock", "deadlock_profile", 200),
            ("GET", "/profiles/me/deadlock/dream-slots", "dream_slots", 200),
            (
                "GET",
                "/tournaments?limit=9&offset=0&open_registration=true",
                "tournament_list",
                200,
            ),
            ("GET", f"/tournaments/{slug}", "tournament_detail", 200),
            ("GET", workspace_path, "workspace", (200, 304)),
            ("GET", bracket_path, "bracket", (200, 304)),
        ):
            await request(
                user,
                method,
                path,
                kind=kind,
                expected=expected,
            )

    async def wait_until_stop(timeout_seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass

    async def read_loop(user: dict[str, Any], user_index: int) -> None:
        # Real browser tabs do not all render and refetch on the same event
        # loop tick. Spread the first pass and keep a small deterministic
        # per-user offset so the probe measures ordinary traffic, not a
        # synthetic 32-user barrier burst.
        await wait_until_stop(user_index * 0.25)
        while not stop_event.is_set():
            await read_user(user)
            await wait_until_stop(
                NORMAL_TRAFFIC_INTERVAL_SECONDS + (user_index % 8) * 0.5
            )

    async def mutation_loop() -> None:
        organizer = sampled_users[0]
        while not stop_event.is_set():
            invite = await request(
                organizer,
                "POST",
                f"/tournaments/{slug}/invites",
                kind="invite_create",
                expected=201,
                json_payload={"note": f"AS20 probe {qa.marker}", "max_uses": 1},
            )
            invite_id = invite.get("id") if isinstance(invite, dict) else None
            if invite_id:
                await request(
                    organizer,
                    "DELETE",
                    f"/tournaments/{slug}/invites/{quote(str(invite_id), safe='')}",
                    kind="invite_revoke",
                    expected=204,
                )
            await wait_until_stop(NORMAL_TRAFFIC_MUTATION_INTERVAL_SECONDS)

    await asyncio.gather(
        *(read_loop(user, index) for index, user in enumerate(sampled_users)),
        mutation_loop(),
    )


def combined_profile_timeout_seconds(
    *,
    polling_duration_seconds: float,
    polling_open_stagger_seconds: float,
    http_timeout_seconds: float,
    sse_duration_seconds: float = 0.0,
    sse_open_span_seconds: float = 0.0,
) -> float:
    """Bound a combined run after the last virtual tab is allowed to open."""

    return max(
        30.0,
        max(0.0, polling_open_stagger_seconds)
        + max(1.0, polling_duration_seconds)
        + max(1.0, http_timeout_seconds)
        + COMBINED_TIMEOUT_GRACE_SECONDS,
        max(0.0, sse_open_span_seconds)
        + max(0.0, sse_duration_seconds)
        + COMBINED_TIMEOUT_GRACE_SECONDS,
    )


async def run_profile(args: argparse.Namespace) -> dict[str, Any]:
    env_file = args.env_file
    if env_file is None:
        configured_env = os.environ.get("PLATFORM_ENV_FILE", "").strip()
        env_file = Path(configured_env) if configured_env else PLATFORM_ROOT / ".env.platform"
    load_env_file(env_file)

    settings = get_settings()
    if args.sse_capacity_limit:
        is_ready_check_capacity_probe = (
            args.mode == "sse"
            and args.sse_scope == "ready-check"
            and args.sse_admission_mode == "ticket"
        )
        if args.sse_capacity_limit > SSE_GLOBAL_LIMIT and not (
            is_ready_check_capacity_probe
        ):
            raise RuntimeError(
                "QA SSE capacity mode above the production cap requires the "
                "ticketed Ready Check scope."
            )
        sse_capacity_token = sse_load_test_capacity_token(
            settings,
            args.sse_capacity_limit,
        )
    else:
        sse_capacity_token = None
    plateau_probe_connections = plateau_probe_count(
        capacity_limit=args.sse_capacity_limit,
        connection_count=args.sse_connections,
    )

    combined_users = sum(count for _, count in BROWSER_POLLING_TOURNAMENT_PLAN) * args.users_per_tournament
    requested_users = max(
        args.sse_connections,
        combined_users if args.mode == "combined" else 14,
    )
    qa = ProductionQa(
        origin=args.origin,
        request_origin=args.request_origin,
        report_path=args.report_path,
        keep_data=args.keep_data,
        browser_gate_dir=None,
        browser_gate_timeout=120.0,
        http_timeout=args.http_timeout,
        mode="sse",
        scale_users=requested_users,
        concurrency=args.concurrency,
        http_max_connections=args.http_max_connections,
        collect_performance=True,
        system_sample_interval=args.system_sample_interval,
        browser_polling_duration=args.combined_polling_duration,
        browser_polling_open_stagger=args.combined_polling_open_stagger,
        browser_polling_users_per_tournament=args.users_per_tournament,
    )
    qa.report["mode"] = args.mode
    qa.report["control_email"] = args.control_email.strip().lower()
    qa.report["target_sha"] = args.target_sha
    qa.report["github_run_id"] = args.github_run_id
    qa.report["sse"] = {}
    qa.report["sse_admission"] = {"mode": args.sse_admission_mode, "issued": False}
    started = time.monotonic()
    public_client = await qa.new_client()
    fixture_origin = args.fixture_origin or args.origin
    fixture_client = (
        await qa.new_client(origin=fixture_origin)
        if args.sse_scope == "ready-check"
        else public_client
    )
    qa.report["sse_transport"] = {
        "measured_origin": qa.origin,
        "fixture_origin": fixture_origin,
        "fixture_origin_mode": (
            "origin-local"
            if urlsplit(fixture_origin).hostname in {"127.0.0.1", "localhost", "::1"}
            else "public"
        ),
        "agenda_origin": qa.origin if args.sse_scope == "ready-check" else None,
        "events_origin": qa.origin,
    }
    try:
        await qa.record_preprod_run(status="running", requested_users=qa.scale_users)
        if args.sse_scope != "ready-check":
            await qa.start_performance_collection()
        try:
            if args.sse_scope == "ready-check":
                (
                    users,
                    tournaments,
                    user_chunks,
                    sse_ticket_by_user_id,
                    state_tickets_by_user_id,
                    sse_open_at_by_user_id,
                ) = await asyncio.wait_for(
                    prepare_ready_check_fixture(
                        qa,
                        fixture_client,
                        agenda_client=public_client,
                        sse_capacity_token=sse_capacity_token,
                    ),
                    timeout=READY_CHECK_FIXTURE_TIMEOUT_SECONDS,
                )
            else:
                users, tournaments, user_chunks = await asyncio.wait_for(
                    prepare_fixture(qa, public_client),
                    timeout=SSE_FIXTURE_TIMEOUT_SECONDS,
                )
                sse_ticket_by_user_id = None
                state_tickets_by_user_id = None
                sse_open_at_by_user_id = None
        except TimeoutError as exc:
            fixture_timeout = (
                READY_CHECK_FIXTURE_TIMEOUT_SECONDS
                if args.sse_scope == "ready-check"
                else SSE_FIXTURE_TIMEOUT_SECONDS
            )
            qa.report["sse_fixture_timeout_seconds"] = fixture_timeout
            raise RuntimeError(
                "SSE fixture setup exceeded its bounded budget of "
                f"{fixture_timeout:.1f}s"
            ) from exc
        if args.sse_scope == "ready-check":
            await qa.start_performance_collection()
        sse_ticket: str | None = None
        after_connection_barrier = None
        fallback_probe = None
        if args.sse_scope == "ready-check":
            qa.report["sse_admission"] = {
                "mode": "ticket",
                "issued": True,
                "scope": "authenticated-eligible-ready-check",
            }

            async def start_ready_check() -> None:
                await qa.request_as(
                    fixture_client,
                    users[0],
                    "POST",
                    f"/tournaments/{tournaments[0]['slug']}/deadlock/ready-check/start",
                    expected=201,
                )

            async def probe_overflow(metrics: SseMetrics) -> dict[str, Any]:
                assert state_tickets_by_user_id is not None
                return await probe_ready_check_overflow(
                    qa,
                    public_client,
                    users,
                    tournament_slug=str(tournaments[0]["slug"]),
                    state_tickets=state_tickets_by_user_id,
                    metrics=metrics,
                )

            after_connection_barrier = start_ready_check
            fallback_probe = probe_overflow
        elif args.sse_admission_mode == "ticket":
            with qa.phase("sse_admission_ticket"):
                sse_ticket = await issue_public_sse_admission_ticket(
                    qa,
                    public_client,
                    str(tournaments[0]["slug"]),
                )
            qa.report["sse_admission"] = {
                "mode": "ticket",
                "issued": True,
                "scope": "anonymous-public",
            }
        if args.ready_file is not None:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text(
                json.dumps(
                    {
                        "ready": True,
                        "marker": qa.marker,
                        "mode": args.mode,
                        "tournament_id": str(tournaments[0]["id"]),
                        "tournament_slug": str(tournaments[0]["slug"]),
                        "written_at": datetime.now(UTC).isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if args.mode == "sse":
            with qa.phase("sse_only_run"):
                sse_report = await run_connections(
                    qa,
                    users,
                    tournaments,
                    connection_count=args.sse_connections,
                    duration_seconds=args.sse_duration,
                    open_concurrency=args.sse_open_concurrency,
                    open_timeout_seconds=args.sse_open_timeout,
                    open_rate_per_second=args.sse_open_rate,
                    sse_capacity_token=sse_capacity_token,
                    global_admission_limit=args.sse_capacity_limit or SSE_GLOBAL_LIMIT,
                    plateau_probe_connections=plateau_probe_connections,
                    sse_ticket=sse_ticket,
                    reconnect_cycles=args.sse_reconnect_cycles,
                    event_count=args.sse_event_count,
                    event_interval=args.sse_event_interval,
                    http_max_connections=max(args.http_max_connections, args.sse_connections),
                    sse_scope=args.sse_scope,
                    sse_ticket_by_user_id=sse_ticket_by_user_id,
                    sse_open_at_by_user_id=sse_open_at_by_user_id,
                    after_connection_barrier=after_connection_barrier,
                    fallback_probe=fallback_probe,
                )
            qa.report["sse"] = sse_report
            metrics = sse_report["metrics"]
            qa.scenario(
                "sse_no_unexpected_errors",
                metrics["errors"] == 0 and metrics["rejected_other"] == 0,
                metrics,
                fatal=False,
            )
            qa.scenario(
                "sse_admission_cap_respected",
                metrics["max_active_connections"]
                <= (args.sse_capacity_limit or SSE_GLOBAL_LIMIT),
                metrics,
            )
            qa.scenario(
                "sse_capacity_or_explicit_admission_observed",
                metrics["initial_connected"] == args.sse_connections
                or metrics["rejected_429"] > 0
                or metrics["open_timeouts"] > 0,
                metrics,
            )
            qa.scenario(
                "sse_event_delivery_complete",
                metrics["events"] >= sse_report["expected_events"],
                {
                    "events": metrics["events"],
                    "expected_events": sse_report["expected_events"],
                },
                fatal=False,
            )
        else:
            tabs = qa.build_browser_polling_tabs(
                tournaments=tournaments,
                user_chunks=user_chunks,
            )
            qa.report["polling"] = {
                "profile": "combined-polling-with-sse",
                "duration_seconds": args.combined_polling_duration,
                "open_stagger_seconds": args.combined_polling_open_stagger,
                "request_concurrency": max(1, min(128, qa.concurrency)),
                "tabs_planned": len(tabs),
                "visible_tabs": sum(1 for tab in tabs if not tab["hidden_after_open"]),
                "hidden_tabs": sum(1 for tab in tabs if tab["hidden_after_open"]),
                "load_generator_local": qa.origin.startswith("http://127.0.0.1"),
            }
            with qa.phase("combined_sse_and_polling"):
                inflight: set[str] = set()
                polling_request_gate = asyncio.Semaphore(max(1, min(128, qa.concurrency)))
                normal_metrics = NormalApiMetrics()
                normal_stop_event = asyncio.Event()

                async def run_polling() -> None:
                    await asyncio.gather(
                        *(
                            qa.run_browser_polling_tab(
                                public_client,
                                tab,
                                profile_duration=args.combined_polling_duration,
                                inflight=inflight,
                                request_gate=polling_request_gate,
                            )
                            for tab in tabs
                        )
                    )

                polling_task = asyncio.create_task(run_polling())
                sse_task = asyncio.create_task(
                    run_connections(
                        qa,
                        users,
                        tournaments,
                        connection_count=args.sse_connections,
                        duration_seconds=args.sse_duration,
                        open_concurrency=args.sse_open_concurrency,
                        open_timeout_seconds=args.sse_open_timeout,
                        open_rate_per_second=args.sse_open_rate,
                        sse_capacity_token=sse_capacity_token,
                        global_admission_limit=args.sse_capacity_limit or SSE_GLOBAL_LIMIT,
                        plateau_probe_connections=plateau_probe_connections,
                        sse_ticket=sse_ticket,
                        reconnect_cycles=args.sse_reconnect_cycles,
                        event_count=args.sse_event_count,
                        event_interval=args.sse_event_interval,
                        http_max_connections=max(args.http_max_connections, args.sse_connections),
                    )
                )
                normal_api_task = asyncio.create_task(
                    run_normal_api_traffic(
                        qa,
                        public_client,
                        users,
                        tournament_slug=str(tournaments[0]["slug"]),
                        stop_event=normal_stop_event,
                        metrics=normal_metrics,
                    )
                )
                combined_timeout = combined_profile_timeout_seconds(
                    polling_duration_seconds=args.combined_polling_duration,
                    polling_open_stagger_seconds=args.combined_polling_open_stagger,
                    http_timeout_seconds=args.http_timeout,
                    sse_duration_seconds=args.sse_duration,
                    sse_open_span_seconds=(
                        max(0, args.sse_connections - 1) / args.sse_open_rate
                        if args.sse_open_rate > 0
                        else 0.0
                    ),
                )
                try:
                    done, pending = await asyncio.wait(
                        {polling_task, sse_task},
                        timeout=combined_timeout,
                        return_when=asyncio.ALL_COMPLETED,
                    )
                finally:
                    normal_stop_event.set()
                    await asyncio.gather(normal_api_task, return_exceptions=True)
                qa.report["normal_api"] = normal_metrics.summary()
                if pending:
                    for task in pending:
                        task.cancel()
                    with suppress(asyncio.TimeoutError, TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=5.0,
                        )
                    qa.report["combined_execution_timeout_seconds"] = combined_timeout
                    raise RuntimeError(
                        "combined workload exceeded its bounded execution budget "
                        f"of {combined_timeout:.1f}s"
                    )
                results = [task.result() for task in done]
                sse_report = next(
                    result
                    for result in results
                    if isinstance(result, dict) and "metrics" in result
                )
            qa.report["polling"].update(qa.polling_metrics.summary())
            qa.report["sse"] = sse_report
            qa.scenario(
                "combined_polling_requests_without_errors",
                qa.report["polling"]["errors"] == 0,
                qa.report["polling"],
                fatal=True,
            )
            qa.scenario(
                "combined_normal_api_without_errors",
                qa.report["normal_api"]["errors"] == 0,
                qa.report["normal_api"],
                fatal=True,
            )
            qa.scenario(
                "combined_sse_admission_cap_respected",
                sse_report["metrics"]["max_active_connections"]
                <= (args.sse_capacity_limit or SSE_GLOBAL_LIMIT),
                sse_report["metrics"],
            )
            qa.scenario(
                "combined_sse_no_unexpected_errors",
                sse_report["metrics"]["errors"] == 0
                and sse_report["metrics"]["rejected_other"] == 0
                and sse_report["metrics"]["rejected_503"] == 0,
                sse_report["metrics"],
                fatal=True,
            )
            qa.scenario(
                "combined_sse_event_delivery_complete",
                sse_report["metrics"]["events"] >= sse_report["expected_events"],
                {
                    "events": sse_report["metrics"]["events"],
                    "expected_events": sse_report["expected_events"],
                },
                fatal=True,
            )

        qa.report["duration_seconds"] = round(time.monotonic() - started, 4)
        qa.scenario("sse_profile_complete", True)
        await qa.record_preprod_run(
            status="passed",
            created_users=len(users),
            tournaments_created=len(tournaments),
            finished_at=datetime.now(UTC),
        )
    except Exception as exc:
        qa.report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        qa.report["fatal_traceback"] = traceback.format_exc(limit=40)
        with suppress(Exception):
            await qa.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
        raise
    finally:
        try:
            await qa.stop_performance_collection()
        except Exception as exc:
            qa.report["performance_collection_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            qa.report["performance_collection_traceback"] = traceback.format_exc(limit=40)
            qa.scenarios.append(
                {
                    "name": "performance_collection",
                    "ok": False,
                    "detail": qa.report["performance_collection_error"],
                }
            )
        for client in qa.clients:
            with suppress(Exception):
                await client.aclose()
        if not qa.keep_data:
            cleanup = await qa.cleanup_targeted()
            qa.report["cleanup"] = cleanup
            qa.scenarios.append(
                {"name": "sse_cleanup", "ok": cleanup.get("ok", False), "detail": cleanup}
            )
        else:
            qa.report["cleanup"] = {"ok": False, "kept": True}
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        qa.report["passed"] = not qa.report.get("fatal_error") and all(
            item["ok"] for item in qa.scenarios
        )
        qa.report_path.parent.mkdir(parents=True, exist_ok=True)
        qa.report_path.write_text(
            json.dumps(qa.report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with suppress(Exception):
            await qa.record_preprod_run(report_path=str(qa.report_path))
        if args.summary_path is not None:
            args.summary_path.parent.mkdir(parents=True, exist_ok=True)
            args.summary_path.write_text(
                json.dumps(summary(qa.report), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    return qa.report


def summary(report: dict[str, Any]) -> dict[str, Any]:
    sse = report.get("sse") if isinstance(report.get("sse"), dict) else {}
    metrics = sse.get("metrics") if isinstance(sse.get("metrics"), dict) else {}
    polling = report.get("polling") if isinstance(report.get("polling"), dict) else {}
    return {
        "marker": report.get("marker"),
        "mode": report.get("mode"),
        "target_sha": report.get("target_sha"),
        "github_run_id": report.get("github_run_id"),
        "control_email": report.get("control_email"),
        "passed": report.get("passed"),
        "report_path": report.get("report_path"),
        "created_users": report.get("created_users"),
        "planned_users": report.get("requested_users"),
        "tournament_ids": report.get("tournament_ids", []),
        "completed_tournaments": len(report.get("tournament_ids", [])),
        "planned_tournaments": report.get(
            "planned_tournaments",
            sum(count for _, count in BROWSER_POLLING_TOURNAMENT_PLAN),
        ),
        "sse": {
            "admission_mode": sse.get("admission_mode") or (
                report.get("sse_admission") or {}
            ).get("mode"),
            "target_connections": sse.get("target_connections"),
            "transport": sse.get("transport"),
            "open_rate_per_second": sse.get("open_rate_per_second"),
            "capacity_mode": sse.get("capacity_mode"),
            "application_global_admission_limit": sse.get(
                "application_global_admission_limit"
            ),
            "plateau_probe": sse.get("plateau_probe"),
            "connected": metrics.get("connected"),
            "initial_connected": metrics.get("initial_connected"),
            "max_active_connections": metrics.get("max_active_connections"),
            "completed": metrics.get("completed"),
            "disconnects": metrics.get("disconnects"),
            "rejected_429": metrics.get("rejected_429"),
            "rejected_503": metrics.get("rejected_503"),
            "rejected_other": metrics.get("rejected_other"),
            "open_timeouts": metrics.get("open_timeouts"),
            "fallback_polling_eligible": metrics.get("fallback_polling_eligible"),
            "errors": metrics.get("errors"),
            "keepalives": metrics.get("keepalives"),
            "bytes_received": metrics.get("bytes_received"),
            "error_samples": metrics.get("error_samples", []),
            "events": metrics.get("events"),
            "resyncs": metrics.get("resyncs"),
            "expected_events": sse.get("expected_events"),
            "publisher": sse.get("publisher"),
            "reconnects": metrics.get("reconnects"),
            "response_statuses": metrics.get("response_statuses", {}),
            "response_error_samples": metrics.get("response_error_samples", []),
            "connect_latency_ms": metrics.get("connect_latency_ms"),
            "event_delivery_latency_ms": metrics.get("event_delivery_latency_ms"),
            "load_generator_resources": sse.get("load_generator_resources"),
        },
        "polling": {
            "tabs_planned": polling.get("tabs_planned"),
            "request_concurrency": polling.get("request_concurrency"),
            "executed": polling.get("executed"),
            "errors": polling.get("errors"),
            "not_modified": polling.get("not_modified"),
        }
        if polling
        else None,
        "normal_api": report.get("normal_api"),
        "performance": report.get("performance", {}).get("bottleneck_summary", {}),
        "fatal_error": report.get("fatal_error"),
        "fatal_traceback": report.get("fatal_traceback"),
        "performance_collection_error": report.get("performance_collection_error"),
        "performance_collection_traceback": report.get(
            "performance_collection_traceback"
        ),
        "sse_transport": report.get("sse_transport"),
        "http_failure_diagnostics": report.get("http_failure_diagnostics", []),
        "combined_execution_timeout_seconds": report.get(
            "combined_execution_timeout_seconds"
        ),
        "scenarios": report.get("scenarios", []),
        "rows": [{
            "synthetic_users": len(report.get("user_ids", [])),
            "report_path": report.get("report_path"),
            "result": {
                "passed": report.get("passed"),
                "marker": report.get("marker"),
                "report_path": report.get("report_path"),
            },
        }],
    }


async def async_main() -> int:
    args = parse_args()
    try:
        report = await run_profile(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "fatal_error": f"{type(exc).__name__}: {exc}",
                    "fatal_traceback": traceback.format_exc(limit=40),
                }
            )
        )
        return 1
    finally:
        await dispose_engine()
    try:
        compact_summary = summary(report)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "fatal_error": f"{type(exc).__name__}: {exc}",
                    "fatal_traceback": traceback.format_exc(limit=40),
                }
            )
        )
        return 1
    print(json.dumps(compact_summary, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
