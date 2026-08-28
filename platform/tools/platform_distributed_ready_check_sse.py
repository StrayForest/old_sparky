#!/usr/bin/env python3
"""Distributed public Ready Check SSE capacity harness.

The coordinator runs on production only for fixture creation, the shared
barrier, one Ready Check start request, and origin-side observability.  The
SSE generators are independent GitHub-hosted processes and never run on the
production VPS.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
from typing import Any
from urllib.parse import quote

import httpx


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
PUBLIC_ORIGIN = "https://old-sparky.com"
READY_CHECK_FILE_TOOL = "/root/old_sparky/platform/tools/platform_distributed_ready_check_files.py"
CAPACITY_HEADER = "x-platform-qa-sse-capacity"
MAX_SHARDS = 32
MAX_CONNECTIONS_PER_SHARD = 2_000
DEFAULT_AGENDA_RATE = 5.0
DEFAULT_OPEN_RATE = 5.0
DEFAULT_OPEN_TIMEOUT = 5.0
DEFAULT_HOLD_SECONDS = 60.0
DEFAULT_BARRIER_TIMEOUT = 1_800.0
DEFAULT_HTTP_TIMEOUT = 15.0


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * percent / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower), 2)


def latency_stats(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 2) if values else None,
    }


def safe_headers(response: httpx.Response) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in response.headers.items():
        lowered = name.lower()
        if any(word in lowered for word in ("authorization", "cookie", "token", "secret", "password")):
            continue
        result[lowered] = value[:500]
    return result


def response_diagnostic(response: httpx.Response, *, path: str) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "path": path.split("?", 1)[0],
        "headers": safe_headers(response),
        "body": response.content[:4096].decode("utf-8", errors="replace"),
    }


def is_cloudflare_1200(response: httpx.Response) -> bool:
    headers = {name.lower(): value.lower() for name, value in response.headers.items()}
    server = headers.get("server", "")
    error_type = headers.get("cf-error-type", "")
    body = response.content[:4096].decode("utf-8", errors="replace").lower()
    return error_type == "1200" or (
        response.status_code == 503
        and ("cloudflare" in server or "cf-ray" in headers or "error 1200" in body)
    )


def is_cloudflare_5xx(response: httpx.Response) -> bool:
    headers = {name.lower(): value.lower() for name, value in response.headers.items()}
    return 500 <= response.status_code < 600 and (
        "cloudflare" in headers.get("server", "") or "cf-ray" in headers
    )


def validate_numeric(value: str, *, name: str, minimum: int = 0, maximum: int = 10**12) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{name} must be numeric")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def expected_run_root(run_id: str) -> Path:
    validate_numeric(run_id, name="run_id")
    return RUN_ROOT_BASE / f"gha-{run_id}"


def atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    os.chmod(path, mode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("coordinator", "shard"), required=True)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-sha", default=None)
    parser.add_argument("--origin", default=PUBLIC_ORIGIN)
    parser.add_argument("--request-origin", default=PUBLIC_ORIGIN)
    parser.add_argument("--fixture-origin", default="http://127.0.0.1:8010")
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--control-email", default=None)
    parser.add_argument("--shards", type=int, default=5)
    parser.add_argument("--shard", type=int, default=None)
    parser.add_argument("--connections-per-shard", type=int, default=20)
    parser.add_argument("--capacity-limit", type=int, default=100)
    parser.add_argument("--duration", type=float, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument("--open-timeout", type=float, default=DEFAULT_OPEN_TIMEOUT)
    parser.add_argument("--agenda-rate", type=float, default=DEFAULT_AGENDA_RATE)
    parser.add_argument("--open-rate", type=float, default=DEFAULT_OPEN_RATE)
    parser.add_argument("--barrier-timeout", type=float, default=DEFAULT_BARRIER_TIMEOUT)
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--hold-after-event", type=float, default=10.0)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--ssh-config", type=Path, default=None)
    parser.add_argument("--ssh-host", default=None)
    parser.add_argument("--ssh-user", default=None)
    parser.add_argument("--ssh-identity", type=Path, default=None)
    parser.add_argument("--file-tool", default=READY_CHECK_FILE_TOOL)
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args()
    validate_numeric(args.run_id, name="run_id")
    if args.role == "coordinator":
        if not 1 <= args.shards <= MAX_SHARDS:
            parser.error(f"--shards must be between 1 and {MAX_SHARDS}")
        if not 1 <= args.connections_per_shard <= MAX_CONNECTIONS_PER_SHARD:
            parser.error(
                f"--connections-per-shard must be between 1 and {MAX_CONNECTIONS_PER_SHARD}"
            )
        if args.shards * args.connections_per_shard > 30_000:
            parser.error("distributed target must not exceed the QA SSE maximum")
        if args.control_email is None or not re.fullmatch(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", args.control_email
        ):
            parser.error("--control-email is required and must be a valid email")
        if args.run_root is None:
            parser.error("--run-root is required for coordinator")
    else:
        if args.shard is None or not 0 <= args.shard < MAX_SHARDS:
            parser.error("--shard must be in the bounded range")
        if args.manifest is None:
            parser.error("--manifest is required for shard")
        for name, value in (
            ("--ssh-config", args.ssh_config),
            ("--ssh-host", args.ssh_host),
            ("--ssh-user", args.ssh_user),
            ("--ssh-identity", args.ssh_identity),
        ):
            if value is None:
                parser.error(f"{name} is required for shard")
        if args.report_path is None:
            parser.error("--report-path is required for shard")
        if args.open_timeout < 0.5 or args.open_timeout > 60:
            parser.error("--open-timeout must be between 0.5 and 60 seconds")
        if args.agenda_rate <= 0 or args.open_rate <= 0:
            parser.error("agenda and open rates must be positive")
    return args


async def ssh_call(
    args: argparse.Namespace,
    command_args: list[str],
    *,
    input_bytes: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    command = [
        "ssh",
        "-F",
        str(args.ssh_config),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        str(args.ssh_identity),
        f"{args.ssh_user}@{args.ssh_host}",
        "sudo",
        "-n",
        "--",
        args.file_tool,
        *command_args,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input_bytes)
    return process.returncode or 0, stdout, stderr


async def write_remote_marker(args: argparse.Namespace, kind: str, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, _stdout, stderr = await ssh_call(
        args,
        [
            "--action",
            "write-marker",
            "--run-id",
            args.run_id,
            "--shard",
            str(args.shard),
            "--kind",
            kind,
        ],
        input_bytes=raw,
    )
    if status != 0:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"remote {kind} marker write failed: {detail}")


async def read_remote_event(args: argparse.Namespace) -> dict[str, Any] | None:
    status, stdout, stderr = await ssh_call(
        args,
        [
            "--action",
            "read-file",
            "--run-id",
            args.run_id,
            "--kind",
            "event-triggered",
            "--optional",
        ]
    )
    if status != 0:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"remote event marker read failed: {detail}")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("remote event marker is not valid JSON") from exc
    if payload.get("status") == "pending":
        return None
    if not isinstance(payload, dict) or str(payload.get("run_id")) != args.run_id:
        raise RuntimeError("remote event marker identity does not match this run")
    return payload


async def fetch_remote_manifest(args: argparse.Namespace) -> dict[str, Any]:
    status, stdout, stderr = await ssh_call(
        args,
        [
            "--action",
            "read-file",
            "--run-id",
            args.run_id,
            "--shard",
            str(args.shard),
            "--kind",
            "manifest",
        ]
    )
    if status != 0:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"remote shard manifest read failed: {detail}")
    try:
        manifest = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("shard manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("shard manifest must be a JSON object")
    return manifest


def validate_manifest(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    if str(manifest.get("run_id")) != args.run_id:
        raise ValueError("manifest run id does not match the shard")
    if int(manifest.get("shard", -1)) != args.shard:
        raise ValueError("manifest shard does not match the shard")
    if args.target_sha and str(manifest.get("target_sha") or "") != args.target_sha:
        raise ValueError("manifest target SHA does not match the shard")
    if not isinstance(manifest.get("capacity_token"), str) or not manifest["capacity_token"]:
        raise ValueError("manifest capacity proof is missing")
    users = manifest.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("manifest users are missing")
    ids: set[str] = set()
    for user in users:
        if not isinstance(user, dict):
            raise ValueError("manifest user is not an object")
        user_id = str(user.get("id") or "")
        if not user_id or user_id in ids:
            raise ValueError("manifest contains duplicate or empty user ids")
        if not isinstance(user.get("session_token"), str) or not user["session_token"]:
            raise ValueError("manifest session credential is missing")
        ids.add(user_id)


async def discover_egress(origin: str, timeout: float) -> dict[str, Any]:
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(base_url=origin.rstrip("/"), timeout=timeout, limits=limits) as client:
        trace_response = await client.get("/cdn-cgi/trace", headers={"Accept": "text/plain"})
        trace: dict[str, str] = {}
        for line in trace_response.text.splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                trace[name.strip()] = value.strip()[:200]
        health_response = await client.get("/api/v1/health", headers={"Accept": "application/json"})
        return {
            "trace_status": trace_response.status_code,
            "ip": trace.get("ip"),
            "colo": trace.get("colo"),
            "trace_http": trace.get("http"),
            "trace_loc": trace.get("loc"),
            "health_status": health_response.status_code,
            "health_cf_ray": health_response.headers.get("cf-ray"),
            "health_server": health_response.headers.get("server"),
        }


async def issue_agenda(
    client: httpx.AsyncClient,
    *,
    origin: str,
    session_cookie_name: str,
    capacity_token: str,
    tournament_id: str,
    users: list[dict[str, Any]],
    rate_per_second: float,
    request_timeout: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    tickets: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    diagnostics: list[dict[str, Any]] = []
    started = time.monotonic()
    lock = asyncio.Lock()

    async def one(index: int, user: dict[str, Any]) -> None:
        await asyncio.sleep(index / rate_per_second)
        path = "/api/v1/ready-check/agenda"
        request_started = time.monotonic()
        try:
            response = await client.get(
                path,
                headers={
                    "Accept": "application/json",
                    "Origin": origin,
                    "Cookie": f"{session_cookie_name}={user['session_token']}",
                    CAPACITY_HEADER: capacity_token,
                    "X-Platform-QA-Phase": "distributed_ready_check_agenda",
                },
                timeout=request_timeout,
            )
            status_counts[str(response.status_code)] += 1
            if response.status_code != 200:
                if len(diagnostics) < 25:
                    diagnostics.append(response_diagnostic(response, path=path))
                return
            payload = response.json()
            checks = payload.get("checks") if isinstance(payload, dict) else None
            check = next(
                (item for item in checks or [] if isinstance(item, dict) and str(item.get("tournament_id")) == tournament_id),
                None,
            )
            stream_ticket = payload.get("sse_ticket") if isinstance(payload, dict) else None
            state_ticket = check.get("state_ticket") if isinstance(check, dict) else None
            admission_open_at = check.get("admission_open_at") if isinstance(check, dict) else None
            if not all(isinstance(value, str) and value for value in (stream_ticket, state_ticket, admission_open_at)):
                if len(diagnostics) < 25:
                    diagnostics.append({"status": 200, "path": path, "error": "agenda_missing_ticket_or_slot"})
                return
            try:
                open_epoch = datetime.fromisoformat(admission_open_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                if len(diagnostics) < 25:
                    diagnostics.append({"status": 200, "path": path, "error": "agenda_invalid_admission_slot"})
                return
            async with lock:
                tickets[str(user["id"])] = {
                    "stream_ticket": stream_ticket,
                    "state_ticket": state_ticket,
                    "admission_open_at": open_epoch,
                }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            status_counts["exception"] += 1
            if len(diagnostics) < 25:
                diagnostics.append(
                    {
                        "path": path,
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                        "elapsed_ms": round((time.monotonic() - request_started) * 1000, 2),
                    }
                )

    await asyncio.gather(*(one(index, user) for index, user in enumerate(users)))
    return tickets, {
        "attempted": len(users),
        "successful": len(tickets),
        "failed": len(users) - len(tickets),
        "status_counts": dict(sorted(status_counts.items())),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rate_per_second": rate_per_second,
        "diagnostics": diagnostics,
    }


async def open_one_stream(
    *,
    client: httpx.AsyncClient,
    origin: str,
    session_cookie_name: str,
    capacity_token: str,
    tournament_slug: str,
    user: dict[str, Any],
    ticket: dict[str, Any],
    index: int,
    open_rate: float,
    open_timeout: float,
    hold_after_event: float,
    release_event: asyncio.Event,
    release_payload: dict[str, Any],
    mark_attempt_finished,
    metrics: dict[str, Any],
    open_gate: asyncio.Semaphore,
) -> None:
    scheduled_delay = max(0.0, float(ticket["admission_open_at"]) - time.time())
    await asyncio.sleep(max(index / open_rate, scheduled_delay))
    attempted_at = time.monotonic()
    metrics["attempted"] += 1
    metrics["opening_started_at"] = metrics["opening_started_at"] or time.time()
    path = (
        "/api/v1/ready-check/events?ticket="
        + quote(str(ticket["stream_ticket"]), safe="")
    )
    headers = {
        "Accept": "text/event-stream, application/problem+json",
        "Cache-Control": "no-cache",
        "Origin": origin,
        "Cookie": f"{session_cookie_name}={user['session_token']}",
        CAPACITY_HEADER: capacity_token,
        "X-Platform-QA-Phase": "distributed_ready_check_sse",
        "X-Request-ID": f"distributed-{metrics['shard']}-{index}-{os.getpid()}",
    }
    stream_context: Any | None = None
    response: httpx.Response | None = None
    connected = False
    event_received = False
    attempt_marked = False

    async def finish_attempt() -> None:
        nonlocal attempt_marked
        if not attempt_marked:
            attempt_marked = True
            await mark_attempt_finished()

    try:
        stream_context = client.stream("GET", path, headers=headers)
        try:
            async with asyncio.timeout(open_timeout):
                async with open_gate:
                    response = await stream_context.__aenter__()
        except TimeoutError:
            metrics["handshake_timeouts"] += 1
            metrics["fallback_polling_eligible"] += 1
            await finish_attempt()
            return
        await finish_attempt()
        metrics["response_statuses"][str(response.status_code)] += 1
        if response.status_code != 200:
            await response.aread()
            if response.status_code == 429:
                metrics["rejected_429"] += 1
            elif response.status_code == 503:
                metrics["rejected_503"] += 1
            else:
                metrics["rejected_other"] += 1
            if is_cloudflare_1200(response):
                metrics["error_1200"] += 1
            if is_cloudflare_5xx(response):
                metrics["cloudflare_5xx"] += 1
            if len(metrics["diagnostics"]) < 25:
                metrics["diagnostics"].append(response_diagnostic(response, path=path))
            return
        connected = True
        metrics["connected"] += 1
        metrics["connect_latencies"].append((time.monotonic() - attempted_at) * 1000)
        await release_event.wait()
        if not release_payload.get("triggered"):
            await asyncio.sleep(max(0.0, metrics["duration"]))
            return
        current_event = False
        event_started = time.monotonic()
        async with asyncio.timeout(max(1.0, metrics["duration"] + hold_after_event)):
            async for line in response.aiter_lines():
                if line == "event: ready_check":
                    current_event = True
                elif current_event and line.startswith("data: "):
                    event_received = True
                    metrics["events"] += 1
                    event_payload: dict[str, Any] = {}
                    try:
                        parsed = json.loads(line[6:])
                        if isinstance(parsed, dict):
                            event_payload = parsed
                    except json.JSONDecodeError:
                        pass
                    published_at_ms = int(event_payload.get("qa_published_at_ms") or 0)
                    if published_at_ms:
                        metrics["event_latencies"].append(max(0.0, time.time() * 1000 - published_at_ms))
                    else:
                        metrics["event_latencies"].append((time.monotonic() - event_started) * 1000)
                    await asyncio.sleep(max(0.0, hold_after_event))
                    break
                elif line.startswith(": keepalive"):
                    metrics["keepalives"] += 1
                if time.monotonic() - event_started >= metrics["duration"]:
                    break
        if not event_received:
            metrics["unexpected_disconnects"] += 1
    except TimeoutError:
        if release_payload.get("triggered"):
            metrics["unexpected_disconnects"] += 1
    except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        metrics["errors"] += 1
        if len(metrics["diagnostics"]) < 25:
            metrics["diagnostics"].append(
                {
                    "path": path.split("?", 1)[0],
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                }
            )
    finally:
        metrics["opening_finished_at"] = time.time()
        await finish_attempt()
        if stream_context is not None:
            try:
                await asyncio.wait_for(stream_context.__aexit__(None, None, None), timeout=0.5)
            except Exception:
                pass
        if connected:
            metrics["closed"] += 1


async def run_shard(args: argparse.Namespace) -> int:
    started = time.monotonic()
    report: dict[str, Any] = {
        "mode": "distributed-ready-check-sse-shard",
        "run_id": int(args.run_id),
        "shard": args.shard,
        "target_sha": args.target_sha,
        "origin": args.origin.rstrip("/"),
        "request_origin": args.request_origin.rstrip("/"),
        "passed": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    marker_payload: dict[str, Any] = {
        "run_id": int(args.run_id),
        "shard": args.shard,
        "status": "failed",
        "attempted": 0,
        "connected": 0,
        "events": 0,
    }
    ready_written = False
    try:
        if args.manifest is not None and args.manifest.is_file():
            try:
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("local shard manifest is not valid JSON") from exc
            if not isinstance(manifest, dict):
                raise RuntimeError("local shard manifest must be a JSON object")
        else:
            manifest = await fetch_remote_manifest(args)
        validate_manifest(args, manifest)
        users = manifest["users"]
        if len(users) != args.connections_per_shard:
            raise ValueError(
                f"manifest contains {len(users)} users instead of {args.connections_per_shard}"
            )
        tournament = manifest.get("tournament") or {}
        tournament_id = str(tournament.get("id") or "")
        tournament_slug = str(tournament.get("slug") or "")
        if not tournament_id or not tournament_slug:
            raise ValueError("manifest tournament identity is missing")
        if str(manifest.get("origin") or args.origin).rstrip("/") != args.origin.rstrip("/"):
            raise ValueError("manifest origin does not match the shard origin")
        egress = await discover_egress(args.origin, args.http_timeout)
        report["egress"] = egress
        agenda_limits = httpx.Limits(
            max_connections=max(64, min(2_048, len(users) + 32)),
            max_keepalive_connections=max(32, min(256, len(users))),
        )
        async with httpx.AsyncClient(
            base_url=args.origin.rstrip("/"),
            follow_redirects=True,
            timeout=httpx.Timeout(args.http_timeout, read=None),
            limits=agenda_limits,
        ) as agenda_client:
            tickets, agenda_report = await issue_agenda(
                agenda_client,
                origin=args.request_origin.rstrip("/"),
                session_cookie_name=str(manifest["session_cookie_name"]),
                capacity_token=str(manifest["capacity_token"]),
                tournament_id=tournament_id,
                users=users,
                rate_per_second=args.agenda_rate,
                request_timeout=args.http_timeout,
            )
        report["agenda"] = agenda_report
        stream_users = [user for user in users if str(user["id"]) in tickets]
        metrics: dict[str, Any] = {
            "shard": args.shard,
            "duration": args.duration,
            "attempted": 0,
            "connected": 0,
            "closed": 0,
            "events": 0,
            "keepalives": 0,
            "errors": 0,
            "handshake_timeouts": 0,
            "fallback_polling_eligible": 0,
            "rejected_429": 0,
            "rejected_503": 0,
            "rejected_other": 0,
            "error_1200": 0,
            "cloudflare_5xx": 0,
            "unexpected_disconnects": 0,
            "opening_started_at": None,
            "opening_finished_at": None,
            "response_statuses": Counter(),
            "connect_latencies": [],
            "event_latencies": [],
            "diagnostics": [],
        }
        local_attempts_finished = 0
        local_attempts_done = asyncio.Event()
        local_lock = asyncio.Lock()

        async def mark_attempt_finished() -> None:
            nonlocal local_attempts_finished
            async with local_lock:
                local_attempts_finished += 1
                if local_attempts_finished >= len(stream_users):
                    local_attempts_done.set()

        release_event = asyncio.Event()
        remote_release = asyncio.Event()
        release_payload: dict[str, Any] = {}

        async def poll_release() -> None:
            while not remote_release.is_set():
                try:
                    payload = await read_remote_event(args)
                except (OSError, RuntimeError):
                    await asyncio.sleep(2.0)
                    continue
                if payload is not None:
                    release_payload.update(payload)
                    remote_release.set()
                    return
                await asyncio.sleep(1.0)

        if not stream_users:
            local_attempts_done.set()
        release_poller = asyncio.create_task(poll_release())
        client_limits = httpx.Limits(
            max_connections=max(64, min(2_048, len(stream_users) + 32)),
            max_keepalive_connections=max(32, min(256, len(stream_users))),
        )
        try:
            async with httpx.AsyncClient(
                base_url=args.origin.rstrip("/"),
                follow_redirects=True,
                timeout=httpx.Timeout(
                    connect=max(args.open_timeout + 2.0, 10.0),
                    read=None,
                    write=10.0,
                    pool=10.0,
                ),
                limits=client_limits,
            ) as sse_client:
                open_gate = asyncio.Semaphore(max(1, min(256, len(stream_users))))
                tasks = [
                    asyncio.create_task(
                        open_one_stream(
                            client=sse_client,
                            origin=args.request_origin.rstrip("/"),
                            session_cookie_name=str(manifest["session_cookie_name"]),
                            capacity_token=str(manifest["capacity_token"]),
                            tournament_slug=tournament_slug,
                            user=user,
                            ticket=tickets[str(user["id"])],
                            index=index,
                            open_rate=args.open_rate,
                            open_timeout=args.open_timeout,
                            hold_after_event=args.hold_after_event,
                            release_event=release_event,
                            release_payload=release_payload,
                            mark_attempt_finished=mark_attempt_finished,
                            metrics=metrics,
                            open_gate=open_gate,
                        )
                    )
                    for index, user in enumerate(stream_users)
                ]
                await local_attempts_done.wait()
                metrics["response_statuses"] = dict(sorted(metrics["response_statuses"].items()))
                marker_payload = {
                    "run_id": int(args.run_id),
                    "shard": args.shard,
                    "status": (
                        "ready"
                        if (
                            metrics["errors"] == 0
                            and metrics["attempted"] == len(users)
                            and metrics["connected"] == len(users)
                            and agenda_report["successful"] == len(users)
                            and metrics["rejected_429"] == 0
                            and metrics["rejected_503"] == 0
                            and metrics["rejected_other"] == 0
                            and metrics["handshake_timeouts"] == 0
                        )
                        else "failed"
                    ),
                    "attempted": metrics["attempted"],
                    "connected": metrics["connected"],
                    "agenda_attempted": agenda_report["attempted"],
                    "agenda_successful": agenda_report["successful"],
                    "error_1200": metrics["error_1200"],
                    "cloudflare_5xx": metrics["cloudflare_5xx"],
                    "rejected_429": metrics["rejected_429"],
                    "errors": metrics["errors"],
                    "handshake_timeouts": metrics["handshake_timeouts"],
                    "unexpected_disconnects": metrics["unexpected_disconnects"],
                    "opening_started_at": metrics["opening_started_at"],
                    "opening_finished_at": metrics["opening_finished_at"],
                    "opening_phase_seconds": round(
                        max(
                            0.0,
                            float(metrics["opening_finished_at"] or 0.0)
                            - float(metrics["opening_started_at"] or 0.0),
                        ),
                        3,
                    ),
                    "actual_open_rate_per_second": round(
                        metrics["attempted"]
                        / max(
                            0.001,
                            float(metrics["opening_finished_at"] or 0.0)
                            - float(metrics["opening_started_at"] or 0.0),
                        ),
                        3,
                    ),
                    "egress": egress,
                    "response_statuses": metrics["response_statuses"],
                }
                await write_remote_marker(args, "ready", marker_payload)
                ready_written = True
                await asyncio.gather(local_attempts_done.wait(), remote_release.wait())
                release_event.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if not release_poller.done():
                release_poller.cancel()
            await asyncio.gather(release_poller, return_exceptions=True)
        report["sse"] = {
            key: value
            for key, value in metrics.items()
            if key not in {"connect_latencies", "event_latencies", "response_statuses"}
        }
        report["sse"]["response_statuses"] = metrics["response_statuses"]
        report["sse"]["connect_latency_ms"] = latency_stats(metrics["connect_latencies"])
        report["sse"]["event_latency_ms"] = latency_stats(metrics["event_latencies"])
        report["sse"]["connect_latency_samples_ms"] = [round(value, 2) for value in metrics["connect_latencies"]]
        report["sse"]["event_latency_samples_ms"] = [round(value, 2) for value in metrics["event_latencies"]]
        report["barrier"] = {"reached": True, "release": release_payload}
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["passed"] = bool(
            release_payload.get("triggered")
            and agenda_report["successful"] == len(users)
            and metrics["connected"] == len(users)
            and metrics["events"] == metrics["connected"]
            and metrics["errors"] == 0
            and metrics["error_1200"] == 0
            and metrics["cloudflare_5xx"] == 0
            and metrics["rejected_429"] == 0
            and metrics["unexpected_disconnects"] == 0
        )
    except Exception as exc:
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        report["fatal_traceback"] = traceback.format_exc(limit=30)
        marker_payload.update(
            {
                "status": "failed",
                "error": report["fatal_error"],
            }
        )
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(args.report_path, 0o600)
        if not ready_written:
            try:
                await write_remote_marker(args, "ready", marker_payload)
            except Exception as exc:
                report["ready_marker_error"] = f"{type(exc).__name__}: {exc}"
        done_payload = {
            "run_id": int(args.run_id),
            "shard": args.shard,
            "status": "done",
            "passed": bool(report.get("passed")),
            "attempted": int((report.get("sse") or {}).get("attempted", 0)),
            "connected": int((report.get("sse") or {}).get("connected", 0)),
            "events": int((report.get("sse") or {}).get("events", 0)),
            "error_1200": int((report.get("sse") or {}).get("error_1200", 0)),
            "cloudflare_5xx": int((report.get("sse") or {}).get("cloudflare_5xx", 0)),
            "rejected_429": int((report.get("sse") or {}).get("rejected_429", 0)),
            "errors": int((report.get("sse") or {}).get("errors", 0)),
            "handshake_timeouts": int((report.get("sse") or {}).get("handshake_timeouts", 0)),
            "unexpected_disconnects": int((report.get("sse") or {}).get("unexpected_disconnects", 0)),
            "egress": report.get("egress", {}),
            "report_path": str(args.report_path),
        }
        try:
            await write_remote_marker(args, "done", done_payload)
        except Exception as exc:
            report["done_marker_error"] = f"{type(exc).__name__}: {exc}"
        args.report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(args.report_path, 0o600)
    return 0 if report.get("passed") else 1


def load_env_file(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


async def wait_for_shard_markers(
    root: Path,
    *,
    shards: int,
    kind: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    paths = [root / "distributed" / f"shard-{index}-{kind}.json" for index in range(shards)]
    while time.monotonic() < deadline:
        payloads: list[dict[str, Any]] = []
        complete = True
        for index, path in enumerate(paths):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                complete = False
                break
            if not isinstance(payload, dict) or int(payload.get("run_id", -1)) != int(root.name.removeprefix("gha-")) or int(payload.get("shard", -1)) != index:
                raise RuntimeError(f"invalid {kind} marker for shard {index}")
            payloads.append(payload)
        if complete and len(payloads) == shards:
            return payloads
        await asyncio.sleep(2.0)
    raise TimeoutError(f"distributed Ready Check barrier timed out waiting for {kind} markers")


def aggregate_shard_markers(markers: list[dict[str, Any]], *, target: int, triggered: bool) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    total = Counter()
    egress_ips: list[str] = []
    egress_colos: list[str] = []
    opening_started_at: list[float] = []
    opening_finished_at: list[float] = []
    for marker in markers:
        for status, count in (marker.get("response_statuses") or {}).items():
            status_counts[str(status)] += int(count or 0)
        for key in (
            "attempted",
            "connected",
            "agenda_attempted",
            "agenda_successful",
            "error_1200",
        "cloudflare_5xx",
        "rejected_429",
        "errors",
        "handshake_timeouts",
            "unexpected_disconnects",
        ):
            total[key] += int(marker.get(key) or 0)
        egress = marker.get("egress") or {}
        if egress.get("ip"):
            egress_ips.append(str(egress["ip"]))
        if egress.get("colo"):
            egress_colos.append(str(egress["colo"]))
        if marker.get("opening_started_at") is not None:
            opening_started_at.append(float(marker["opening_started_at"]))
        if marker.get("opening_finished_at") is not None:
            opening_finished_at.append(float(marker["opening_finished_at"]))
    independent = len(egress_ips) == len(set(egress_ips)) == len(markers)
    return {
        "target_connections": target,
        "attempted": total["attempted"],
        "connected": total["connected"],
        "agenda_attempted": total["agenda_attempted"],
        "agenda_successful": total["agenda_successful"],
        "error_1200": total["error_1200"],
        "cloudflare_5xx": total["cloudflare_5xx"],
        "rejected_429": total["rejected_429"],
        "handshake_timeouts": total["handshake_timeouts"],
        "unexpected_disconnects": total["unexpected_disconnects"],
        "opening_phase_seconds": (
            round(max(opening_finished_at) - min(opening_started_at), 3)
            if opening_started_at and opening_finished_at
            else None
        ),
        "actual_aggregate_open_rate_per_second": (
            round(
                total["attempted"]
                / max(0.001, max(opening_finished_at) - min(opening_started_at)),
                3,
            )
            if opening_started_at and opening_finished_at
            else None
        ),
        "events_received": None,
        "response_statuses": dict(sorted(status_counts.items())),
        "egress_ips": egress_ips,
        "egress_colos": egress_colos,
        "independent_egress": independent,
        "triggered": triggered,
        "shards": markers,
    }


async def run_coordinator(args: argparse.Namespace) -> int:
    # Imports are deliberately lazy: public shard runners need only httpx and
    # must not install or import the production application's DB stack.
    from python_packages.platform_infra.config import get_settings
    from python_packages.platform_infra.db import dispose_engine
    from python_packages.platform_infra.sse_connection_limit import (
        _ready_check_global_key,
        sse_load_test_capacity_token,
    )
    from tools.platform_production_qa import ProductionQa
    from tools.platform_sse_qa import prepare_ready_check_fixture
    from redis.asyncio import from_url

    load_env_file(args.env_file)
    root = expected_run_root(args.run_id)
    if args.run_root.resolve() != root.resolve() or root.is_symlink():
        raise ValueError("coordinator run root is outside the fixed retained-load root")
    distributed_root = root / "distributed"
    distributed_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings = get_settings()
    capacity_token = sse_load_test_capacity_token(settings, args.capacity_limit)
    total_connections = args.shards * args.connections_per_shard
    detail_path = distributed_root / "distributed-report.json"
    qa = ProductionQa(
        origin=args.fixture_origin,
        request_origin=args.request_origin,
        report_path=detail_path,
        keep_data=True,
        browser_gate_dir=None,
        browser_gate_timeout=120.0,
        http_timeout=15.0,
        mode="sse",
        scale_users=total_connections,
        concurrency=4,
        http_max_connections=64,
        collect_performance=True,
        system_sample_interval=1.0,
    )
    qa.report.update(
        {
            "mode": "sse",
            "target_sha": args.target_sha,
            "github_run_id": int(args.run_id),
            "control_email": args.control_email.strip().lower(),
            "request_origin": args.request_origin.rstrip("/"),
            "distributed": {
                "profile": "public-cloudflare-distributed",
                "shards": args.shards,
                "connections_per_shard": args.connections_per_shard,
                "target_connections": total_connections,
                "aggregate_agenda_rate_per_second": args.shards * args.agenda_rate,
                "aggregate_open_rate_per_second": args.shards * args.open_rate,
                "capacity_limit": args.capacity_limit,
                "generator_location": "github-hosted-runners",
                "coordinator_location": "production-control-only",
            },
        }
    )
    fixture_client = await qa.new_client(origin=args.fixture_origin)
    performance_started = False
    performance_stopped = False
    lease_client = None
    lease_sampler_task: asyncio.Task[None] | None = None
    lease_sampler_stop = asyncio.Event()
    lease_samples: list[dict[str, Any]] = []
    lease_sample_errors = 0

    async def sample_ready_check_leases() -> None:
        nonlocal lease_sample_errors
        while not lease_sampler_stop.is_set():
            try:
                lease_count = int(await lease_client.zcard(_ready_check_global_key()))
                lease_samples.append(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "active_leases": lease_count,
                    }
                )
            except Exception:
                lease_sample_errors += 1
            try:
                await asyncio.wait_for(lease_sampler_stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    try:
        await qa.record_preprod_run(status="running", requested_users=total_connections)
        users, tournaments, _chunks, _sse, _state, _slots = await prepare_ready_check_fixture(
            qa,
            fixture_client,
            agenda_client=fixture_client,
            sse_capacity_token=capacity_token,
            agenda_open_rate_per_second=args.shards * args.agenda_rate,
            issue_agenda=False,
        )
        tournament = tournaments[0]
        for index in range(args.shards):
            shard_users = [
                user
                for user in users
                if int(user.get("profile_index", -1)) // args.connections_per_shard == index
            ]
            if len(shard_users) != args.connections_per_shard:
                raise RuntimeError(f"shard {index} has {len(shard_users)} users instead of {args.connections_per_shard}")
            manifest = {
                "version": 1,
                "run_id": int(args.run_id),
                "target_sha": args.target_sha,
                "shard": index,
                "connections_per_shard": args.connections_per_shard,
                "origin": args.request_origin.rstrip("/"),
                "request_origin": args.request_origin.rstrip("/"),
                "session_cookie_name": qa.session_cookie_name,
                "capacity_token": capacity_token,
                "tournament": {"id": str(tournament["id"]), "slug": str(tournament["slug"])},
                "users": [
                    {
                        "id": str(user["id"]),
                        "label": str(user["label"]),
                        "profile_index": int(user["profile_index"]),
                        "session_token": qa.session_tokens_by_user_id[str(user["id"])],
                    }
                    for user in shard_users
                ],
            }
            atomic_json(distributed_root / f"shard-{index}.json", manifest)
        control = {
            "version": 1,
            "run_id": int(args.run_id),
            "target_sha": args.target_sha,
            "status": "prepared",
            "fixture_ready_at": datetime.now(UTC).isoformat(),
            "marker": qa.marker,
            "tournament_id": str(tournament["id"]),
            "tournament_slug": str(tournament["slug"]),
            "shards": args.shards,
            "connections_per_shard": args.connections_per_shard,
            "total_connections": total_connections,
            "capacity_limit": args.capacity_limit,
            "ready_check_starts_at": tournament.get("ready_check_starts_at"),
            "ready_check_ends_at": tournament.get("ready_check_ends_at"),
        }
        atomic_json(distributed_root / "control.json", control)
        # Publish a cleanup-safe inventory before any external shard opens a
        # socket.  If the coordinator is terminated during the barrier, the
        # existing exact retained-load cleanup workflow still has the marker,
        # user IDs, tournament ID, and private detail-report path it needs.
        atomic_json(detail_path, qa.report)
        atomic_json(
            distributed_root / "matrix-summary.json",
            {
                "mode": "sse",
                "target_sha": args.target_sha,
                "github_run_id": int(args.run_id),
                "control_email": args.control_email.strip().lower(),
                "planned_users": total_connections,
                "completed_users": len(users),
                "planned_tournaments": 1,
                "completed_tournaments": 1,
                "passed": False,
                "marker": qa.marker,
                "rows": [
                    {
                        "synthetic_users": len(users),
                        "report_path": str(detail_path),
                        "result": {
                            "passed": False,
                            "marker": qa.marker,
                            "report_path": str(detail_path),
                        },
                    }
                ],
            },
        )
        await qa.start_performance_collection()
        performance_started = True
        lease_client = from_url(settings.platform_redis_url, decode_responses=True)
        lease_sampler_task = asyncio.create_task(sample_ready_check_leases())
        ready_markers = await wait_for_shard_markers(
            root,
            shards=args.shards,
            kind="ready",
            timeout_seconds=args.barrier_timeout,
        )
        egress_ips = [str((marker.get("egress") or {}).get("ip") or "") for marker in ready_markers]
        independent_egress = len(egress_ips) == len(set(egress_ips)) == args.shards and all(egress_ips)
        opening_valid = independent_egress and all(marker.get("status") == "ready" for marker in ready_markers)
        if opening_valid:
            await qa.request_as(
                fixture_client,
                users[0],
                "POST",
                f"/tournaments/{tournament['slug']}/deadlock/ready-check/start",
                expected=201,
            )
        event_payload = {
            "run_id": int(args.run_id),
            "status": "triggered" if opening_valid else "released_without_event",
            "triggered": opening_valid,
            "released_at": datetime.now(UTC).isoformat(),
            "reason": None if opening_valid else (
                "egress_not_independent" if not independent_egress else "opening_phase_failed"
            ),
            "egress_ips": egress_ips,
        }
        atomic_json(distributed_root / "event-triggered.json", event_payload)
        done_markers = await wait_for_shard_markers(
            root,
            shards=args.shards,
            kind="done",
            timeout_seconds=max(120.0, args.duration + args.barrier_timeout),
        )
        aggregate = aggregate_shard_markers(
            ready_markers,
            target=total_connections,
            triggered=opening_valid,
        )
        aggregate["events_received"] = sum(int(marker.get("events") or 0) for marker in done_markers)
        aggregate["errors"] = sum(int(marker.get("errors") or 0) for marker in done_markers)
        aggregate["unexpected_disconnects"] = sum(
            int(marker.get("unexpected_disconnects") or 0) for marker in done_markers
        )
        aggregate["all_done_passed"] = all(marker.get("passed") is True for marker in done_markers)
        aggregate["done_shards"] = len(done_markers)
        aggregate["ready_shards"] = len(ready_markers)
        aggregate["opening_valid"] = opening_valid
        aggregate["barrier_reached"] = True
        aggregate["event_delivery_complete"] = aggregate["events_received"] == total_connections
        lease_sampler_stop.set()
        if lease_sampler_task is not None:
            await asyncio.gather(lease_sampler_task, return_exceptions=True)
            lease_sampler_task = None
        redis_info: dict[str, Any] = {}
        try:
            redis_info = await lease_client.info("stats", "clients", "memory")
        except Exception as exc:
            redis_info = {"error": type(exc).__name__}
        qa.report["redis_ready_check"] = {
            "global_key": _ready_check_global_key(),
            "lease_samples": lease_samples,
            "lease_sample_errors": lease_sample_errors,
            "max_active_leases": max((row["active_leases"] for row in lease_samples), default=0),
            "last_active_leases": lease_samples[-1]["active_leases"] if lease_samples else None,
            "rejected_connections": int(redis_info.get("rejected_connections") or 0),
            "evicted_keys": int(redis_info.get("evicted_keys") or 0),
            "connected_clients": int(redis_info.get("connected_clients") or 0),
        }
        qa.report["distributed"].update(aggregate)
        qa.report["sse"] = {
            "profile": "ready-check-sse-distributed",
            "target_connections": total_connections,
            "initial_connected": aggregate["connected"],
            "connected": aggregate["connected"],
            "events": aggregate["events_received"],
            "expected_events": total_connections if opening_valid else 0,
            "rejected_429": aggregate["rejected_429"],
            "rejected_503": aggregate["cloudflare_5xx"],
            "errors": aggregate["errors"],
            "handshake_timeouts": aggregate["handshake_timeouts"],
        }
        passed = bool(
            opening_valid
            and aggregate["agenda_successful"] == total_connections
            and aggregate["attempted"] == total_connections
            and aggregate["connected"] == total_connections
            and aggregate["events_received"] == total_connections
            and aggregate["error_1200"] == 0
            and aggregate["cloudflare_5xx"] == 0
            and aggregate["rejected_429"] == 0
            and aggregate["errors"] == 0
            and aggregate["unexpected_disconnects"] == 0
            and aggregate["all_done_passed"]
            and aggregate["done_shards"] == args.shards
        )
        qa.report["passed"] = passed
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        await qa.stop_performance_collection()
        performance_stopped = True
        qa.report["performance"] = qa.report.get("performance", {})
        await qa.record_preprod_run(
            status="passed" if passed else "failed",
            created_users=len(users),
            tournaments_created=len(tournaments),
            finished_at=datetime.now(UTC),
            report_path=str(detail_path),
        )
        atomic_json(detail_path, qa.report)
        summary = {
            "mode": "sse",
            "target_sha": args.target_sha,
            "github_run_id": int(args.run_id),
            "control_email": args.control_email.strip().lower(),
            "planned_users": total_connections,
            "completed_users": len(users),
            "planned_tournaments": 1,
            "completed_tournaments": 1,
            "passed": passed,
            "marker": qa.marker,
            "distributed": aggregate,
            "sse": qa.report["sse"],
            "performance": qa.report.get("performance", {}),
            "rows": [
                {
                    "synthetic_users": len(users),
                    "report_path": str(detail_path),
                    "result": {
                        "passed": passed,
                        "marker": qa.marker,
                        "report_path": str(detail_path),
                    },
                }
            ],
        }
        atomic_json(distributed_root / "matrix-summary.json", summary)
        return 0 if passed else 1
    except Exception as exc:
        qa.report["passed"] = False
        qa.report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        qa.report["fatal_traceback"] = traceback.format_exc(limit=40)
        if performance_started and not performance_stopped:
            try:
                await qa.stop_performance_collection()
                performance_stopped = True
            except Exception as performance_exc:
                qa.report["performance_collection_error"] = (
                    f"{type(performance_exc).__name__}: {performance_exc}"
                )
        atomic_json(distributed_root / "distributed-report.json", qa.report)
        if (distributed_root / "control.json").is_file() and not (distributed_root / "event-triggered.json").exists():
            atomic_json(
                distributed_root / "event-triggered.json",
                {
                    "run_id": int(args.run_id),
                    "status": "released_without_event",
                    "triggered": False,
                    "released_at": datetime.now(UTC).isoformat(),
                    "reason": "coordinator_failed",
                },
            )
        if qa.report.get("user_ids"):
            failure_summary = {
                "mode": "sse",
                "target_sha": args.target_sha,
                "github_run_id": int(args.run_id),
                "control_email": args.control_email.strip().lower(),
                "planned_users": total_connections,
                "completed_users": len(qa.report.get("user_ids") or []),
                "planned_tournaments": 1,
                "completed_tournaments": len(qa.report.get("tournament_ids") or []),
                "passed": False,
                "marker": qa.marker,
                "rows": [
                    {
                        "synthetic_users": len(qa.report.get("user_ids") or []),
                        "report_path": str(detail_path),
                        "result": {
                            "passed": False,
                            "marker": qa.marker,
                            "report_path": str(detail_path),
                        },
                    }
                ],
            }
            atomic_json(distributed_root / "matrix-summary.json", failure_summary)
        raise
    finally:
        lease_sampler_stop.set()
        if lease_sampler_task is not None:
            try:
                await asyncio.gather(lease_sampler_task, return_exceptions=True)
            except Exception:
                pass
        if lease_client is not None:
            try:
                await lease_client.aclose()
            except Exception:
                pass
        if performance_started and not performance_stopped:
            try:
                await qa.stop_performance_collection()
            except Exception:
                pass
        for client in qa.clients:
            try:
                await client.aclose()
            except Exception:
                pass
        await dispose_engine()


async def async_main() -> int:
    args = parse_args()
    if args.role == "shard":
        return await run_shard(args)
    return await run_coordinator(args)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
