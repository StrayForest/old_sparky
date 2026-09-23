#!/usr/bin/env python3
"""Collect origin resource evidence while an external load is running."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import pwd
import re
import signal
import time
from pstats import Stats
from typing import Callable
from uuid import uuid4

from sqlalchemy import text

try:
    from platform_evidence_sanitizer import (
        finite_number,
        safe_backend,
        safe_error_class,
        safe_int,
        safe_query_category,
        safe_wait_state,
    )
except ModuleNotFoundError:  # Imported as ``tools.*`` by focused contract tests.
    from tools.platform_evidence_sanitizer import (
        finite_number,
        safe_backend,
        safe_error_class,
        safe_int,
        safe_query_category,
        safe_wait_state,
    )

try:
    from platform_production_qa import (
        SystemSampler,
        collect_api_journal_lines,
        collect_nginx_access_records,
        collect_web_journal_lines,
        iter_processes,
        process_label,
        load_env_file,
        summarize_ssr_observability,
        summarize_request_perf_logs,
    )
except ModuleNotFoundError:  # Imported as ``tools.*`` by focused contract tests.
    from tools.platform_production_qa import (
        SystemSampler,
        collect_api_journal_lines,
        collect_nginx_access_records,
        collect_web_journal_lines,
        iter_processes,
        process_label,
        load_env_file,
        summarize_ssr_observability,
        summarize_request_perf_logs,
    )
from python_packages.platform_infra.db import session_factory


TIMEOUT_DIAGNOSTIC_ID_RE = re.compile(r"^tdiag-[0-9]{1,32}-[0-9]{5}$")
FIXTURE_MARKER_RE = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
EXTERNAL_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
CPROFILE_NAME_RE = re.compile(
    r"^ready-vote-cprofile-(?P<pid>[0-9]+)-(?P<start_time_ticks>[1-9][0-9]*)\.pstats$"
)
API_SERVICE_USER = "oldsparky-api"
ProcessReader = Callable[[int], dict[str, object] | None]


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def expected_api_uid() -> int | None:
    """Resolve the API service UID from the local account database."""

    try:
        uid = int(pwd.getpwnam(API_SERVICE_USER).pw_uid)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    return uid if uid > 0 else None


def _process_uid(process: dict[str, object]) -> int | None:
    for key in ("uid", "euid", "real_uid"):
        value = process.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    pid = _positive_int(process.get("pid"))
    if pid is None:
        return None
    try:
        status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in status.splitlines():
        if not line.startswith("Uid:"):
            continue
        columns = line.split()
        if len(columns) < 2:
            return None
        try:
            return int(columns[1])
        except ValueError:
            return None
    return None


def _is_api_gunicorn_process(process: dict[str, object]) -> bool:
    cmdline = str(process.get("cmdline") or "").lower()
    return (
        process_label(process) == "deadlock-api"
        and "gunicorn" in cmdline
        and "apps.platform_api.app.main:app" in cmdline
    )


def _process_identity(
    process: dict[str, object],
    *,
    expected_uid: int,
    master: dict[str, object],
) -> dict[str, object] | None:
    pid = _positive_int(process.get("pid"))
    ppid = _positive_int(process.get("ppid"))
    start_time_ticks = _positive_int(process.get("start_time_ticks"))
    master_pid = _positive_int(master.get("pid"))
    master_start_time_ticks = _positive_int(master.get("start_time_ticks"))
    process_uid = _process_uid(process)
    master_uid = _process_uid(master)
    if None in (
        pid,
        ppid,
        start_time_ticks,
        master_pid,
        master_start_time_ticks,
        process_uid,
        master_uid,
    ):
        return None
    if process_uid != expected_uid or master_uid != expected_uid:
        return None
    state = str(process.get("state") or "").lower()
    if state in {"z", "x"}:
        return None
    return {
        "pid": pid,
        "uid": process_uid,
        "ppid": ppid,
        "start_time_ticks": start_time_ticks,
        "master_pid": master_pid,
        "master_uid": master_uid,
        "master_start_time_ticks": master_start_time_ticks,
        "cmdline": str(process.get("cmdline") or ""),
        "comm": str(process.get("comm") or ""),
    }


def api_worker_identities(
    processes: list[dict[str, object]] | None = None,
    *,
    expected_uid: int | None = None,
) -> dict[int, dict[str, object]]:
    """Select direct Gunicorn API workers and bind them to their master.

    ``process_label`` deliberately groups the Gunicorn master and workers as
    ``deadlock-api`` for resource accounting.  Profiling signals need a
    narrower identity: one API worker must be a direct child of the one
    unambiguous API master in this snapshot, with a stable UID and procfs
    start-time identity.  Ambiguous masters fail closed.
    """

    records = list(iter_processes() if processes is None else processes)
    uid = expected_api_uid() if expected_uid is None else expected_uid
    if uid is None or isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return {}
    candidates = [
        process
        for process in records
        if isinstance(process, dict)
        and _is_api_gunicorn_process(process)
        and _process_uid(process) == uid
        and _positive_int(process.get("pid")) is not None
        and _positive_int(process.get("ppid")) is not None
        and _positive_int(process.get("start_time_ticks")) is not None
    ]
    by_pid = {
        int(process["pid"]): process
        for process in candidates
        if isinstance(process.get("pid"), int) and not isinstance(process.get("pid"), bool)
    }
    if not by_pid:
        return {}
    roots = [
        process
        for process in candidates
        if int(process["ppid"]) not in by_pid
    ]
    # Ignore an isolated foreign/stale API-shaped process, but refuse to pick
    # between two complete Gunicorn trees.  The latter commonly occurs during
    # a restart and must never result in a signal to the wrong generation.
    roots_with_children = [
        root
        for root in roots
        if any(int(process["ppid"]) == int(root["pid"]) for process in candidates)
    ]
    if len(roots_with_children) != 1:
        return {}
    master = roots_with_children[0]
    master_state = str(master.get("state") or "").lower()
    if master_state in {"z", "x"}:
        return {}
    master_pid = int(master["pid"])
    workers: dict[int, dict[str, object]] = {}
    for process in candidates:
        pid = int(process["pid"])
        if pid == master_pid or int(process["ppid"]) != master_pid:
            continue
        identity = _process_identity(
            process,
            expected_uid=uid,
            master=master,
        )
        if identity is not None:
            workers[pid] = identity
    return workers


def _read_proc_record(pid: int) -> dict[str, object] | None:
    """Read one live process identity without trusting a stale PID listing."""

    if pid <= 0:
        return None
    proc_path = Path("/proc") / str(pid)
    try:
        raw_stat = (proc_path / "stat").read_text(encoding="utf-8")
        comm_end = raw_stat.rfind(")")
        columns = raw_stat[comm_end + 2 :].split()
        if comm_end < 0 or len(columns) < 20:
            return None
        state = columns[0]
        ppid = int(columns[1])
        start_time_ticks = int(columns[19])
        comm = (proc_path / "comm").read_text(encoding="utf-8").strip()
        cmdline = (proc_path / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
        uid = None
        for line in (proc_path / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Uid:"):
                values = line.split()
                if len(values) >= 2:
                    uid = int(values[1])
                break
        if uid is None:
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    return {
        "pid": pid,
        "ppid": ppid,
        "uid": uid,
        "state": state,
        "comm": comm,
        "cmdline": cmdline,
        "start_time_ticks": start_time_ticks,
    }


def _live_identity_matches(
    identity: dict[str, object],
    *,
    expected_uid: int,
    process_reader: ProcessReader | None = None,
) -> bool | None:
    process_reader = _read_proc_record if process_reader is None else process_reader
    pid = _positive_int(identity.get("pid"))
    if pid is None:
        return False
    process = process_reader(pid)
    if process is None:
        # A missing proc entry means the process exited between snapshots; it
        # is not safe to signal a possibly reused PID.
        return False
    for key in ("pid", "uid", "ppid", "start_time_ticks", "cmdline"):
        if process.get(key) != identity.get(key):
            return False
    if process.get("uid") != expected_uid or str(process.get("state") or "").lower() in {"z", "x"}:
        return False
    master_pid = _positive_int(identity.get("master_pid"))
    master_start = _positive_int(identity.get("master_start_time_ticks"))
    if master_pid is None or master_start is None or int(process["ppid"]) != master_pid:
        return False
    master = process_reader(master_pid)
    if master is None:
        return False
    if (
        master.get("uid") != expected_uid
        or master.get("start_time_ticks") != master_start
        or not _is_api_gunicorn_process(master)
        or str(master.get("state") or "").lower() in {"z", "x"}
    ):
        return False
    # The selected parent is the master/arbiter, not another worker.  A
    # second API-shaped process above it indicates an ambiguous/restarting
    # tree, so refuse the signal.
    grandparent_pid = _positive_int(master.get("ppid"))
    if grandparent_pid is not None:
        grandparent = process_reader(grandparent_pid)
        if grandparent is not None and _is_api_gunicorn_process(grandparent):
            return False
    return True


def _send_identity_signal(
    identity: dict[str, object],
    signum: signal.Signals,
    *,
    expected_uid: int,
    process_reader: ProcessReader | None = None,
) -> tuple[bool, str]:
    """Signal one identity through pidfd, never through a numeric PID."""

    pid = _positive_int(identity.get("pid"))
    if pid is None:
        return False, "invalid_pid"
    process_reader = _read_proc_record if process_reader is None else process_reader
    live_match = _live_identity_matches(
        identity,
        expected_uid=expected_uid,
        process_reader=process_reader,
    )
    if live_match is not True:
        return False, "identity_mismatch"
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return False, "pidfd_unavailable"
    try:
        pidfd = pidfd_open(pid, 0)
    except (OSError, ValueError):
        return False, "pidfd_open_failed"
    try:
        # Bind the descriptor before the final procfs comparison. Even if the
        # numeric PID is reused after this point, pidfd targets the original
        # process or fails without signalling the replacement.
        if _live_identity_matches(
            identity,
            expected_uid=expected_uid,
            process_reader=process_reader,
        ) is not True:
            return False, "identity_mismatch"
        pidfd_send_signal(pidfd, signum)
        return True, "pidfd"
    except (OSError, ValueError):
        return False, "pidfd_send_failed"
    finally:
        try:
            os.close(pidfd)
        except OSError:
            pass


def _signal_api_workers_detailed(
    signum: signal.Signals,
    *,
    processes: list[dict[str, object]] | None = None,
    expected_uid: int | None = None,
    armed_identities: dict[int, dict[str, object]] | None = None,
    armed_workers: dict[int, dict[str, object]] | None = None,
    process_reader: ProcessReader | None = None,
) -> tuple[list[int], Counter[str], str | None]:
    process_reader = _read_proc_record if process_reader is None else process_reader
    if armed_identities is not None and armed_workers is not None:
        raise ValueError("provide only one armed worker identity mapping")
    armed = armed_identities if armed_identities is not None else armed_workers
    uid = expected_api_uid() if expected_uid is None else expected_uid
    if uid is None:
        # There is no safe identity basis for a request in this case.  Keep
        # the availability condition separate from worker rejection counts so
        # requested=delivered+rejected remains true (with zero requested).
        return [], Counter(), "uid_unavailable"
    current = api_worker_identities(processes, expected_uid=uid)
    reasons: Counter[str] = Counter()
    if armed is not None:
        live_identities = current
        current = {}
        for pid, expected_identity in armed.items():
            live_identity = live_identities.get(pid)
            if live_identity is None:
                reasons["worker_missing"] += 1
            elif live_identity != expected_identity:
                reasons["identity_mismatch"] += 1
            else:
                current[pid] = live_identity
    signalled: list[int] = []
    for pid, identity in sorted(current.items()):
        delivered, reason = _send_identity_signal(
            identity,
            signum,
            expected_uid=uid,
            process_reader=process_reader,
        )
        if delivered:
            signalled.append(pid)
        else:
            reasons[reason] += 1
    return signalled, reasons, None


def signal_api_workers(
    signum: signal.Signals,
    *,
    processes: list[dict[str, object]] | None = None,
    expected_uid: int | None = None,
    armed_identities: dict[int, dict[str, object]] | None = None,
    armed_workers: dict[int, dict[str, object]] | None = None,
    process_reader: ProcessReader | None = None,
) -> list[int]:
    """Signal only direct API workers with an optional exact armed identity."""
    signalled, _reasons, _availability_reason = _signal_api_workers_detailed(
        signum,
        processes=processes,
        expected_uid=expected_uid,
        armed_identities=armed_identities,
        armed_workers=armed_workers,
        process_reader=process_reader,
    )
    return signalled


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe one external load window on the origin.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-runtime", type=float, default=9_000.0)
    parser.add_argument("--diagnostic-id-file", type=Path)
    parser.add_argument("--fixture-marker", required=True)
    parser.add_argument("--external-run-id", required=True)
    return parser.parse_args()


def load_timeout_diagnostic_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, list) or len(payload) > 20_000:
        return set()
    return {
        value
        for value in payload
        if isinstance(value, str) and TIMEOUT_DIAGNOSTIC_ID_RE.fullmatch(value)
    }


def profile_artifact_snapshot(output_dir: Path | None) -> dict[str, tuple[int, int, int, int]]:
    """Capture bounded metadata without retaining profile paths or contents."""

    if output_dir is None or not output_dir.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int, int, int]] = {}
    for path in output_dir.glob("ready-vote-cprofile-*.pstats"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            metadata = path.stat()
        except OSError:
            continue
        snapshot[path.name] = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    return snapshot


def cpu_profile_summary(
    output_dir: Path | None,
    *,
    armed_pids: list[int] | tuple[int, ...] = (),
    armed_identities: dict[int, dict[str, object]] | None = None,
    baseline_artifacts: dict[str, tuple[int, int, int, int]] | None = None,
) -> dict[str, object]:
    if output_dir is None or not output_dir.is_dir():
        return {"enabled": False, "profiles": []}
    allowed_pids = {int(pid) for pid in armed_pids}
    expected_start_times: dict[int, int] = {}
    if armed_identities is not None:
        allowed_pids = set()
        for raw_pid, identity in armed_identities.items():
            pid = _positive_int(raw_pid)
            start_time_ticks = _positive_int(identity.get("start_time_ticks"))
            if pid is not None and start_time_ticks is not None:
                allowed_pids.add(pid)
                expected_start_times[pid] = start_time_ticks
    profiles: list[dict[str, object]] = []
    all_profile_paths = sorted(output_dir.glob("ready-vote-cprofile-*.pstats"))
    bound_profile_paths: list[Path] = []
    unbound_profile_count = 0
    stale_profile_count = 0
    invalid_profile_count = 0
    for path in all_profile_paths:
        if path.is_symlink() or not path.is_file():
            unbound_profile_count += 1
            continue
        match = CPROFILE_NAME_RE.fullmatch(path.name)
        if match is None:
            unbound_profile_count += 1
            continue
        pid = int(match.group("pid"))
        start_time_ticks = int(match.group("start_time_ticks"))
        # An exact worker generation is mandatory. The legacy armed_pids-only
        # argument is retained for callers during the schema transition, but
        # cannot authorize a profile by itself.
        if (
            pid not in allowed_pids
            or pid not in expected_start_times
            or expected_start_times[pid] != start_time_ticks
        ):
            unbound_profile_count += 1
            continue
        if baseline_artifacts is not None:
            try:
                metadata = path.stat()
            except OSError:
                continue
            current_metadata = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
            if baseline_artifacts.get(path.name) == current_metadata:
                stale_profile_count += 1
                continue
        bound_profile_paths.append(path)
    # Keep the forensic report bounded, but never delete from the shared
    # caller-owned directory.  PID binding prevents a concurrent/old worker's
    # profile from being attributed to this observer window.
    profile_paths = bound_profile_paths[:32]
    for path in profile_paths:
        try:
            stats = Stats(str(path))
        except (EOFError, OSError, TypeError, ValueError):
            invalid_profile_count += 1
            continue
        functions: list[dict[str, object]] = []
        for (_filename, line, name), (primitive_calls, total_calls, self_time, cumulative_time, _callers) in sorted(
            stats.stats.items(),
            key=lambda item: item[1][3],
            reverse=True,
        )[:100]:
            functions.append(
                {
                    # Source filenames can contain the checkout/home path.
                    # Function names and numeric line/call metrics are enough
                    # to identify a hot profile frame without that path.
                    "function": (
                        str(name)
                        if re.fullmatch(r"[A-Za-z0-9_.<>-]{1,128}", str(name))
                        else "other"
                    ),
                    "line": int(line) if isinstance(line, int) and line >= 0 else 0,
                    "primitive_calls": int(primitive_calls),
                    "calls": int(total_calls),
                    "self_seconds": round(float(self_time), 6),
                    "cumulative_seconds": round(float(cumulative_time), 6),
                }
            )
        profiles.append({"profile_available": True, "functions": functions})
    return {
        "enabled": True,
        "profiles": profiles,
        # Compatibility fields from the schema-1 observer summary. Retention
        # is deliberately owned by the caller/host maintenance job.
        "cleaned_files": 0,
        "cleanup_ok": True,
        "retention": {
            "mode": "caller_owned_armed_identity_changed_bounded_summary",
            "max_profiles": 32,
            "available_profiles": len(all_profile_paths),
            "bound_profiles": len(bound_profile_paths),
            "summarized_profiles": len(profile_paths),
            "ignored_unbound_profiles": unbound_profile_count,
            "ignored_stale_profiles": stale_profile_count,
            "invalid_profiles": invalid_profile_count,
            "truncated_profiles": max(0, len(bound_profile_paths) - len(profile_paths)),
            "armed_pids": sorted(allowed_pids),
            "artifacts_preserved": True,
        },
    }


def _signal_delivery_summary(
    delivered: list[int],
    reasons: Counter[str],
    *,
    requested: int,
    availability_reason: str | None = None,
) -> dict[str, object]:
    """Expose only bounded signal outcome facts; identities stay private."""

    bounded_requested = max(0, int(requested))
    bounded_delivered = min(len(delivered), bounded_requested)
    bounded_rejected = sum(reasons.values())
    # Every worker selected for a signal must be accounted for, even if a
    # future caller introduces a new pre-delivery rejection path.
    unaccounted = max(0, bounded_requested - bounded_delivered - bounded_rejected)
    if unaccounted:
        reasons = Counter(reasons)
        reasons["worker_missing"] += unaccounted
        bounded_rejected += unaccounted

    summary: dict[str, object] = {
        "requested_count": bounded_requested,
        "delivered_count": bounded_delivered,
        "rejected_count": bounded_rejected,
        "rejection_reasons": dict(sorted(reasons.items())),
        "pidfd_api_available": callable(getattr(os, "pidfd_open", None))
        and callable(getattr(signal, "pidfd_send_signal", None)),
    }
    if availability_reason is not None:
        summary["availability_reason"] = availability_reason
    return summary


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
            selected.append(
                {
                    "queryid": (
                        str(row["queryid"])
                        if re.fullmatch(r"-?[0-9]+", str(row.get("queryid") or ""))
                        else "other"
                    ),
                    "query_category": safe_query_category("other"),
                    "backend": "postgres",
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
        return {"available": False, "error_class": safe_error_class(type(exc).__name__), "rows": []}


async def ready_vote_explain_evidence(
    fixture_marker: str,
) -> dict[str, object]:
    """EXPLAIN the three hot statements against one retained fixture row.

    The SELECTs are read-only. The upsert is deliberately executed inside an
    explicit transaction and rolled back after deferred constraints are made
    immediate, so trigger timing is observed without leaving a vote or counter
    mutation behind.
    """

    class ExplainRollback(Exception):
        pass

    def summarize_plan(plan: object) -> dict[str, object]:
        """Keep EXPLAIN's numeric bottleneck signals without plan literals."""

        node_counts: Counter[str] = Counter()
        numeric: dict[str, list[float]] = defaultdict(list)
        allowed_nodes = {
            "aggregate",
            "append",
            "bitmap_heap_scan",
            "bitmap_index_scan",
            "delete",
            "hash_join",
            "index_scan",
            "insert",
            "limit",
            "modify_table",
            "nested_loop",
            "result",
            "seq_scan",
            "sort",
            "update",
            "window_aggregate",
            "other",
        }
        numeric_keys = {
            "Actual Rows": "actual_rows",
            "Actual Loops": "actual_loops",
            "Actual Total Time": "actual_total_time_ms",
            "Actual Startup Time": "actual_startup_time_ms",
            "Plan Rows": "plan_rows",
            "Plan Width": "plan_width",
            "Shared Hit Blocks": "shared_hit_blocks",
            "Shared Read Blocks": "shared_read_blocks",
            "Shared Dirtied Blocks": "shared_dirtied_blocks",
            "Shared Written Blocks": "shared_written_blocks",
            "Temp Read Blocks": "temp_read_blocks",
            "Temp Written Blocks": "temp_written_blocks",
            "WAL Records": "wal_records",
            "WAL FPI": "wal_fpi",
            "WAL Bytes": "wal_bytes",
            "Planning Time": "planning_time_ms",
            "Execution Time": "execution_time_ms",
        }

        def visit(value: object) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return
            raw_node = str(value.get("Node Type") or "").strip().lower().replace(" ", "_")
            if raw_node:
                node_counts[raw_node if raw_node in allowed_nodes else "other"] += 1
            for source_key, output_key in numeric_keys.items():
                candidate = value.get(source_key)
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    number = float(candidate)
                    if math.isfinite(number) and number >= 0:
                        numeric[output_key].append(number)
            visit(value.get("Plan"))
            visit(value.get("Plans"))

        visit(plan)
        output: dict[str, object] = {
            "node_type_counts": dict(sorted(node_counts.items())),
        }
        for key, values in sorted(numeric.items()):
            output[key] = round(max(values), 6)
        return output

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
                        WHERE run.marker = :fixture_marker
                          AND run.status IN ('running', 'passed')
                        ORDER BY t.created_at DESC, r.id DESC
                        LIMIT 1
                        """
                    ),
                    {"fixture_marker": fixture_marker},
                )
            ).mappings().first()
            if fixture is None:
                return {
                    "available": False,
                    "error_class": "fixture_not_found",
                }

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
                    text("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + statement),
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
                "auth": summarize_plan(auth_plan),
                "preflight": summarize_plan(preflight_plan),
            }
            async with db_session.begin():
                plans["upsert"] = summarize_plan(await explain(
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
                ))
                await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                raise ExplainRollback
    except ExplainRollback:
        return {"available": True, "plans": plans}
    except Exception as exc:
        return {"available": False, "error_class": safe_error_class(type(exc).__name__)}


def postgres_statement_delta(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    if not before.get("available") or not after.get("available"):
        return {"available": False, "rows": []}
    before_rows = {
        str(row["queryid"]): row
        for row in before.get("rows", [])
        if isinstance(row, dict)
    }
    delta_rows = []
    for row in after.get("rows", []):
        if not isinstance(row, dict):
            continue
        key = str(row["queryid"])
        previous = before_rows.get(key, {})
        calls = max(0, int(row.get("calls", 0)) - int(previous.get("calls", 0)))
        total_ms = max(
            0.0,
            float(row.get("total_exec_ms", 0)) - float(previous.get("total_exec_ms", 0)),
        )
        if calls == 0 and total_ms == 0:
            continue
        delta = {
            "queryid": (
                str(row.get("queryid"))
                if re.fullmatch(r"-?[0-9]+", str(row.get("queryid") or ""))
                else "other"
            ),
            "query_category": safe_query_category(row.get("query_category")),
            "backend": "postgres",
            "calls": calls,
            "total_exec_ms": round(total_ms, 6),
            "mean_exec_ms": round(total_ms / calls, 6) if calls else 0.0,
        }
        for field in ("rows", "shared_blks_hit", "shared_blks_read", "temp_blks_written"):
            delta[field] = max(0, int(row.get(field, 0)) - int(previous.get(field, 0)))
        delta_rows.append(delta)
    delta_rows.sort(key=lambda row: float(row["total_exec_ms"]), reverse=True)
    return {"available": True, "rows": delta_rows}


_SAFE_PROCESS_LABELS = frozenset(
    {
        "deadlock-api",
        "deadlock-web",
        "deadlock-worker",
        "postgresql",
        "redis-server",
        "nginx",
        "load-generator",
    }
)


def _safe_postgres_wait_snapshot(value: object) -> dict[str, object]:
    """Keep timeline wait evidence to bounded labels and numeric aggregates."""

    if not isinstance(value, dict):
        return {}
    output: dict[str, object] = {}
    for key in (
        "lock_waiters",
        "waiting_backends",
        "active_backends",
        "ungranted_locks",
        "backend_connections",
    ):
        number = safe_int(value.get(key))
        if number is not None:
            output[key] = number
    for key in ("max_waiting_query_ms", "max_lock_waiting_query_ms"):
        raw = value.get(key)
        number = finite_number(raw)
        if number is not None:
            output[key] = number
    wait_counts: Counter[str] = Counter()
    raw_wait_counts = value.get("wait_state_counts")
    if isinstance(raw_wait_counts, dict):
        for raw_state, raw_count in raw_wait_counts.items():
            count = safe_int(raw_count)
            if count is not None:
                wait_counts[safe_wait_state(raw_state)] += count
    output["wait_state_counts"] = dict(sorted(wait_counts.items()))
    ownership: Counter[str] = Counter()
    raw_ownership = value.get("backend_ownership")
    if isinstance(raw_ownership, list):
        for entry in raw_ownership:
            if not isinstance(entry, dict):
                continue
            count = safe_int(entry.get("current"))
            if count is not None:
                ownership[safe_backend(entry.get("application_name"))] += count
    output["backend_ownership"] = dict(sorted(ownership.items()))
    if "error_class" in value:
        output["error_class"] = safe_error_class(value.get("error_class"))
    return output


def _safe_process_lifecycle(value: object) -> dict[str, dict[str, int]]:
    """Drop PID/start-time identities while retaining restart counts."""

    if not isinstance(value, dict):
        return {}
    output: dict[str, dict[str, int]] = {}
    for raw_label, raw_row in value.items():
        label = str(raw_label)
        if label not in _SAFE_PROCESS_LABELS or not isinstance(raw_row, dict):
            continue
        new_processes = raw_row.get("new_processes")
        missing_processes = raw_row.get("missing_processes")
        output[label] = {
            "new_process_count": len(new_processes) if isinstance(new_processes, list) else 0,
            "missing_process_count": len(missing_processes) if isinstance(missing_processes, list) else 0,
        }
    return output


async def async_main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("external load observer must run as root")
    if not 0.25 <= args.interval <= 60:
        raise ValueError("observer interval must be between 0.25 and 60 seconds")
    if not 1 <= args.max_runtime <= 18_000:
        raise ValueError("observer max-runtime is outside the supported bound")
    if FIXTURE_MARKER_RE.fullmatch(args.fixture_marker) is None:
        raise ValueError("observer fixture-marker is invalid")
    if EXTERNAL_RUN_ID_RE.fullmatch(args.external_run_id) is None:
        raise ValueError("observer external-run-id is invalid")
    load_env_file(args.env_file)
    os.environ["PLATFORM_RUNTIME_SERVICE"] = "observer"
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
    profile_baseline = profile_artifact_snapshot(profile_dir) if profile_dir else None
    armed_worker_identities = api_worker_identities() if profile_dir else {}
    candidate_worker_count = len(armed_worker_identities)
    profiled_workers: list[int] = []
    arm_reasons: Counter[str] = Counter()
    arm_availability_reason: str | None = None
    flush_reasons: Counter[str] = Counter()
    flush_availability_reason: str | None = None
    if profile_dir:
        profiled_workers, arm_reasons, arm_availability_reason = _signal_api_workers_detailed(
            signal.SIGUSR1,
            armed_identities=armed_worker_identities,
        )
    armed_worker_identities = {
        pid: armed_worker_identities[pid]
        for pid in profiled_workers
        if pid in armed_worker_identities
    }
    timed_out = False
    try:
        while not args.stop_file.exists() and not stop_event.is_set():
            if time.monotonic() - started_monotonic >= args.max_runtime:
                timed_out = True
                break
            await asyncio.sleep(min(1.0, args.interval))
    finally:
        await sampler.stop()
        flushed_workers: list[int] = []
        if profile_dir:
            flushed_workers, flush_reasons, flush_availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR2,
                armed_identities=armed_worker_identities,
            )
    # Nginx buffers access records for up to five seconds. Let the final
    # records reach disk before taking the window's read-only snapshot.
    await asyncio.sleep(6)
    postgres_after = await postgres_statement_snapshot()
    postgres_explain = await ready_vote_explain_evidence(args.fixture_marker)

    finished_at = datetime.now(UTC)
    journal_until = finished_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    request_perf_lines = collect_api_journal_lines(
        journal_since,
        journal_until,
        with_timestamps=True,
    )
    web_journal_lines = collect_web_journal_lines(
        journal_since,
        journal_until,
        with_timestamps=True,
    )
    nginx_access_records = collect_nginx_access_records(started_at, finished_at)
    timeout_diagnostic_ids = load_timeout_diagnostic_ids(args.diagnostic_id_file)

    system_summary = sampler.summary()
    system_summary["timeline"] = [
        {
            "timestamp": sample.get("timestamp"),
            "cpu_per_core_percent": sample.get("cpu_per_core_percent"),
            "cpu_steal_per_core_percent": sample.get("cpu_steal_per_core_percent"),
            "postgres_cpu_percent": sample.get("postgres_cpu_percent"),
            "api_connections": sample.get("api_connections"),
            "postgres_tcp_established_connections": sample.get("postgres_tcp_connections"),
            "postgres_backend_connections": (
                (sample.get("postgres_waits") or {}).get("backend_connections")
            ),
            "postgres_backend_ownership": _safe_postgres_wait_snapshot(
                sample.get("postgres_waits")
            ).get("backend_ownership", {}),
            "tcp_socket_states": sample.get("tcp_socket_states"),
            "tcp_listen_counters": sample.get("tcp_listen_counters"),
            "conntrack": sample.get("conntrack"),
            "process_lifecycle": _safe_process_lifecycle(
                sample.get("process_lifecycle")
            ),
            "redis_connections": sample.get("redis_connections"),
            "gunicorn": sample.get("gunicorn"),
            "postgres_waits": _safe_postgres_wait_snapshot(
                sample.get("postgres_waits")
            ),
            "celery_backlog": sample.get("celery_backlog"),
            "api_process": (sample.get("processes") or {}).get("deadlock-api"),
            "web_process": (sample.get("processes") or {}).get("deadlock-web"),
        }
        for sample in sampler.samples
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "binding": {
            "fixture_marker": args.fixture_marker,
            "external_run_id": args.external_run_id,
            "complete": True,
        },
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "stop_file_seen": args.stop_file.exists(),
        "timed_out": timed_out,
        "system": system_summary,
        "measurement_scope": {
            "http_client": "external_load_runner_report",
            "server_request_perf_logs": "diagnostic_sample",
            "server_ssr_observability": "diagnostic_sample_plus_nginx_html_and_api_window",
            "note": (
                "The observer contains sampled API/SSR journal timings plus the "
                "bounded Nginx HTML/API timing window; full-population HTTP latency "
                "is in the external load runner report. API paths are emitted only "
                "as safe route classes."
            ),
            "timeout_diagnostics": bool(args.diagnostic_id_file),
        },
        "server_request_perf_logs": summarize_request_perf_logs(
            request_perf_lines,
            tournament_slug=None,
        ),
        "server_ssr_observability": summarize_ssr_observability(
            web_journal_lines,
            nginx_access_records,
            request_perf_lines,
            timeout_diagnostic_ids=timeout_diagnostic_ids,
        ),
        "cpu_profile": {
            **cpu_profile_summary(
                profile_dir,
                armed_identities=armed_worker_identities,
                baseline_artifacts=profile_baseline,
            ),
            "signal_delivery": {
                "arm": _signal_delivery_summary(
                    profiled_workers,
                    arm_reasons,
                    requested=candidate_worker_count,
                    availability_reason=arm_availability_reason,
                ),
                "flush": _signal_delivery_summary(
                    flushed_workers,
                    flush_reasons,
                    requested=len(armed_worker_identities),
                    availability_reason=flush_availability_reason,
                ),
            },
            "armed_workers": profiled_workers,
            "flushed_workers": flushed_workers,
            "armed_worker_identities": [
                {
                    key: identity[key]
                    for key in (
                        "pid",
                        "uid",
                        "ppid",
                        "start_time_ticks",
                        "master_pid",
                        "master_uid",
                        "master_start_time_ticks",
                    )
                    if key in identity
                }
                for _pid, identity in sorted(armed_worker_identities.items())
            ],
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
