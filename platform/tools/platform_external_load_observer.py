#!/usr/bin/env python3
"""Collect origin resource evidence while an external load is running."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import time
from pstats import Stats
from uuid import uuid4

from sqlalchemy import text

from platform_production_qa import (
    SystemSampler,
    collect_api_journal_lines,
    iter_processes,
    process_label,
    load_env_file,
    summarize_request_perf_logs,
)
from python_packages.platform_infra.db import session_factory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe one external load window on the origin.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-runtime", type=float, default=9_000.0)
    return parser.parse_args()


def signal_api_workers(signum: signal.Signals) -> list[int]:
    """Arm or flush only API workers; the master and unrelated services stay untouched."""

    signalled: list[int] = []
    for process in iter_processes():
        if process_label(process) != "deadlock-api":
            continue
        pid = int(process["pid"])
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        signalled.append(pid)
    return signalled


def cpu_profile_summary(output_dir: Path | None) -> dict[str, object]:
    if output_dir is None or not output_dir.is_dir():
        return {"enabled": False, "profiles": []}
    profiles: list[dict[str, object]] = []
    for path in sorted(output_dir.glob("ready-vote-cprofile-*.pstats")):
        try:
            stats = Stats(str(path))
        except (OSError, TypeError, ValueError):
            continue
        functions: list[dict[str, object]] = []
        for (filename, line, name), (primitive_calls, total_calls, self_time, cumulative_time, _callers) in sorted(
            stats.stats.items(),
            key=lambda item: item[1][3],
            reverse=True,
        )[:100]:
            functions.append(
                {
                    "function": f"{filename}:{line}({name})",
                    "primitive_calls": int(primitive_calls),
                    "calls": int(total_calls),
                    "self_seconds": round(float(self_time), 6),
                    "cumulative_seconds": round(float(cumulative_time), 6),
                }
            )
        profiles.append({"path": str(path), "functions": functions})
    return {"enabled": True, "profiles": profiles}


async def postgres_statement_snapshot() -> dict[str, object]:
    """Read cumulative statement counters without resetting shared statistics."""

    try:
        async with session_factory()() as db_session:
            rows = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            queryid::text AS queryid,
                            query,
                            calls::bigint AS calls,
                            total_exec_time::double precision AS total_exec_ms,
                            mean_exec_time::double precision AS mean_exec_ms,
                            rows::bigint AS rows,
                            shared_blks_hit::bigint AS shared_blks_hit,
                            shared_blks_read::bigint AS shared_blks_read,
                            temp_blks_written::bigint AS temp_blks_written
                        FROM pg_stat_statements
                        WHERE dbid = (
                            SELECT oid FROM pg_database WHERE datname = current_database()
                        )
                        ORDER BY total_exec_time DESC
                        LIMIT 500
                        """
                    )
                )
            ).mappings().all()
        selected = []
        for row in rows:
            query = " ".join(str(row["query"] or "").split())
            lowered = query.lower()
            if not any(
                marker in lowered
                for marker in (
                    "sessions",
                    "tournament_deadlock_ready_rounds",
                    "tournament_deadlock_ready_votes",
                    "tournament_deadlock_ready_vote_count_shards",
                )
            ):
                continue
            selected.append(
                {
                    "queryid": str(row["queryid"]),
                    "query": query[:500],
                    "calls": int(row["calls"] or 0),
                    "total_exec_ms": round(float(row["total_exec_ms"] or 0), 6),
                    "mean_exec_ms": round(float(row["mean_exec_ms"] or 0), 6),
                    "rows": int(row["rows"] or 0),
                    "shared_blks_hit": int(row["shared_blks_hit"] or 0),
                    "shared_blks_read": int(row["shared_blks_read"] or 0),
                    "temp_blks_written": int(row["temp_blks_written"] or 0),
                }
            )
        return {"available": True, "rows": selected}
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__, "rows": []}


async def ready_vote_explain_evidence() -> dict[str, object]:
    """EXPLAIN the three hot statements against one retained fixture row.

    The SELECTs are read-only. The upsert is deliberately executed inside an
    explicit transaction and rolled back after deferred constraints are made
    immediate, so trigger timing is observed without leaving a vote or counter
    mutation behind.
    """

    class ExplainRollback(Exception):
        pass

    try:
        async with session_factory()() as db_session:
            fixture = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            t.slug,
                            r.id AS round_id,
                            p.user_id,
                            s.token_digest,
                            COALESCE(v.choice, '') AS existing_choice
                        FROM platform.preprod_test_runs AS run
                        JOIN platform.tournaments AS t
                          ON t.description LIKE ('%' || run.marker || '%')
                        JOIN platform.tournament_deadlock_ready_rounds AS r
                          ON r.tournament_id = t.id
                         AND r.status = 'active'
                        JOIN platform.tournament_participants AS p
                          ON p.tournament_id = t.id
                         AND p.status NOT IN ('withdrawn', 'disqualified')
                        JOIN platform.sessions AS s
                          ON s.user_id = p.user_id
                         AND s.invalidated_at IS NULL
                        LEFT JOIN platform.tournament_deadlock_ready_votes AS v
                          ON v.round_id = r.id
                         AND v.user_id = p.user_id
                        WHERE run.status = 'running'
                        ORDER BY t.created_at DESC, r.id DESC
                        LIMIT 1
                        """
                    )
                )
            ).mappings().first()
            if fixture is None:
                return {"available": False, "error": "fixture_not_found"}

            explain_params = {
                "token_digest": str(fixture["token_digest"]),
                "now": datetime.now(UTC),
                "slug": str(fixture["slug"]),
                "user_id": str(fixture["user_id"]),
                "round_id": int(fixture["round_id"]),
                "vote_id": str(uuid4()),
                "choice": "no" if str(fixture["existing_choice"]) == "yes" else "yes",
                "responded_at": datetime.now(UTC),
            }

            async def explain(statement: str, parameters: dict[str, object]) -> object:
                result = await db_session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement),
                    parameters,
                )
                return result.scalar_one()

            auth_plan = await explain(
                """
                SELECT s.user_id
                FROM platform.sessions AS s
                JOIN platform.users AS u ON u.id = s.user_id
                WHERE s.token_digest = :token_digest
                  AND s.invalidated_at IS NULL
                  AND s.expires_at > :now
                  AND u.status = 'active'
                  AND (u.email IS NULL OR u.email_verified_at IS NOT NULL)
                LIMIT 1
                """,
                explain_params,
            )
            preflight_plan = await explain(
                """
                SELECT
                    t.id,
                    t.slug,
                    t.format_slug,
                    t.status,
                    t.registration_closes_at,
                    t.ready_check_starts_at,
                    t.ready_check_ends_at,
                    t.automation_ready_check_closed_at,
                    EXISTS (
                        SELECT 1 FROM platform.tournament_participants AS p
                        WHERE p.tournament_id = t.id
                          AND p.user_id = :user_id
                          AND p.status NOT IN ('withdrawn', 'disqualified')
                    ) AS has_participant,
                    EXISTS (
                        SELECT 1 FROM platform.deadlock_profiles AS dp
                        WHERE dp.user_id = :user_id
                    ) AS has_deadlock_profile,
                    EXISTS (
                        SELECT 1 FROM platform.tournament_deadlock_assignment_runs AS ar
                        WHERE ar.tournament_id = t.id
                          AND ar.status = 'locked'
                    ) AS has_locked_roster,
                    rr.id AS ready_round_id,
                    rr.tournament_id AS ready_round_tournament_id,
                    rr.status AS ready_round_status,
                    COALESCE(jsonb_array_length(rr.eligible_user_ids::jsonb), 0)
                        AS eligible_participant_count,
                    (
                        COALESCE(jsonb_array_length(rr.eligible_user_ids::jsonb), 0) = 0
                        OR rr.eligible_user_ids::jsonb ? :user_id
                    ) AS user_is_eligible
                FROM platform.tournaments AS t
                LEFT JOIN platform.tournament_deadlock_ready_rounds AS rr
                  ON rr.tournament_id = t.id
                 AND rr.status = 'active'
                WHERE t.slug = :slug
                LIMIT 1
                """,
                explain_params,
            )
            # End the read-only transaction before opening the write-capable
            # diagnostic transaction used for the rollback-safe upsert plan.
            await db_session.rollback()
            plans: dict[str, object] = {
                "auth": auth_plan,
                "preflight": preflight_plan,
            }
            async with db_session.begin():
                plans["upsert"] = await explain(
                    """
                    INSERT INTO platform.tournament_deadlock_ready_votes
                        (id, round_id, user_id, choice, responded_at)
                    VALUES (:vote_id, :round_id, :user_id, :choice, :responded_at)
                    ON CONFLICT (round_id, user_id) DO UPDATE
                    SET choice = :choice,
                        responded_at = :responded_at,
                        updated_at = :responded_at
                    WHERE platform.tournament_deadlock_ready_votes.choice <> :choice
                    RETURNING id
                    """,
                    explain_params,
                )
                await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                raise ExplainRollback
    except ExplainRollback:
        return {"available": True, "plans": plans}
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__}


def postgres_statement_delta(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    if not before.get("available") or not after.get("available"):
        return {"available": False, "rows": []}
    before_rows = {
        (str(row["queryid"]), str(row["query"])): row
        for row in before.get("rows", [])
        if isinstance(row, dict)
    }
    delta_rows = []
    for row in after.get("rows", []):
        if not isinstance(row, dict):
            continue
        key = (str(row["queryid"]), str(row["query"]))
        previous = before_rows.get(key, {})
        calls = max(0, int(row.get("calls", 0)) - int(previous.get("calls", 0)))
        total_ms = max(
            0.0,
            float(row.get("total_exec_ms", 0)) - float(previous.get("total_exec_ms", 0)),
        )
        if calls == 0 and total_ms == 0:
            continue
        delta = dict(row)
        delta["calls"] = calls
        delta["total_exec_ms"] = round(total_ms, 6)
        delta["mean_exec_ms"] = round(total_ms / calls, 6) if calls else 0.0
        for field in ("rows", "shared_blks_hit", "shared_blks_read", "temp_blks_written"):
            delta[field] = max(0, int(row.get(field, 0)) - int(previous.get(field, 0)))
        delta_rows.append(delta)
    delta_rows.sort(key=lambda row: float(row["total_exec_ms"]), reverse=True)
    return {"available": True, "rows": delta_rows}


async def async_main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("external load observer must run as root")
    if not 0.25 <= args.interval <= 60:
        raise ValueError("observer interval must be between 0.25 and 60 seconds")
    if not 1 <= args.max_runtime <= 18_000:
        raise ValueError("observer max-runtime is outside the supported bound")
    load_env_file(args.env_file)
    profile_dir_raw = os.environ.get("PLATFORM_READY_VOTE_CPU_PROFILE_DIR", "").strip()
    profile_dir = Path(profile_dir_raw) if profile_dir_raw else None
    postgres_before = await postgres_statement_snapshot()

    sampler = SystemSampler(interval_seconds=args.interval)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            pass

    started_at = datetime.now(UTC)
    journal_since = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    started_monotonic = time.monotonic()
    await sampler.start()
    profiled_workers = signal_api_workers(signal.SIGUSR1) if profile_dir else []
    timed_out = False
    try:
        while not args.stop_file.exists() and not stop_event.is_set():
            if time.monotonic() - started_monotonic >= args.max_runtime:
                timed_out = True
                break
            await asyncio.sleep(min(1.0, args.interval))
    finally:
        await sampler.stop()
        flushed_workers = signal_api_workers(signal.SIGUSR2) if profile_dir else []
    postgres_after = await postgres_statement_snapshot()
    postgres_explain = await ready_vote_explain_evidence()

    finished_at = datetime.now(UTC)
    journal_until = finished_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    request_perf_lines = collect_api_journal_lines(journal_since, journal_until)

    system_summary = sampler.summary()
    system_summary["timeline"] = [
        {
            "timestamp": sample.get("timestamp"),
            "cpu_per_core_percent": sample.get("cpu_per_core_percent"),
            "postgres_cpu_percent": sample.get("postgres_cpu_percent"),
            "api_connections": sample.get("api_connections"),
            "postgres_connections": sample.get("postgres_connections"),
            "redis_connections": sample.get("redis_connections"),
            "gunicorn": sample.get("gunicorn"),
            "postgres_waits": sample.get("postgres_waits"),
            "celery_backlog": sample.get("celery_backlog"),
            "api_process": (sample.get("processes") or {}).get("deadlock-api"),
        }
        for sample in sampler.samples
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "stop_file_seen": args.stop_file.exists(),
        "timed_out": timed_out,
        "system": system_summary,
        "measurement_scope": {
            "http_client": "external_load_runner_report",
            "server_request_perf_logs": "diagnostic_sample",
            "note": (
                "This origin-side observer contains only the selected request_perf "
                "journal lines; full-population HTTP latency is in the external "
                "load runner report."
            ),
        },
        "server_request_perf_logs": summarize_request_perf_logs(
            request_perf_lines,
            tournament_slug=None,
        ),
        "cpu_profile": {
            **cpu_profile_summary(profile_dir),
            "armed_workers": profiled_workers,
            "flushed_workers": flushed_workers,
        },
        "postgres_stat_statements": {
            "before": postgres_before,
            "after": postgres_after,
            "delta": postgres_statement_delta(postgres_before, postgres_after),
        },
        "postgres_explain": postgres_explain,
    }
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0 if not timed_out else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
