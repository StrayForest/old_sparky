from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import platform_cpu_diagnostic_plan as diagnostic_plan_tool
from tools import platform_ssr_capacity_diagnostic as capacity_diagnostic

from tools.platform_cpu_diagnostic_plan import (
    is_ssr_response_id_eligible as is_eligible_response_id,
    ssr_request_response_id_selection_digest as response_id_selection_digest,
    summarize_ssr_request_correlation,
)


RUN_ID = "0123456789abcdef0123456789abcdef"
PHASE = "on"
WINDOW_START = "2026-10-29T12:00:00+00:00"
WINDOW_END = "2026-10-29T12:03:00+00:00"
ID_ONE = "00000000000000000000000000000002"
ID_TWO = "00000000000000000000000000000005"
ID_THREE = "00000000000000000000000000000017"
INELIGIBLE_ID = "33333333333333333333333333333333"


def event(
    request_id: str,
    *,
    source_time: str = "2026-10-29T12:01:00Z",
    phase: str = PHASE,
    run_id: str = RUN_ID,
    **fields: object,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "phase": phase,
        "window_start_utc": WINDOW_START,
        "window_end_utc": WINDOW_END,
        "event_at_utc": source_time,
        "request_id": request_id,
        **fields,
    }


def summarize(**overrides: object) -> dict[str, object]:
    selected_ids = overrides.get("selected_request_ids", [ID_ONE, ID_TWO])
    values: dict[str, object] = {
        "selected_request_ids": selected_ids,
        "submitted_html_count": 7,
        "valid_response_id_count": 2,
        "eligible_response_id_count": len({
            value
            for value in selected_ids
            if isinstance(value, str) and is_eligible_response_id(RUN_ID, value)
        }),
        "selection_scheme": "sha256-runid-requestid-1of8-v1",
        "run_id": RUN_ID,
        "phase": PHASE,
        "window_start_utc": WINDOW_START,
        "window_end_utc": WINDOW_END,
        "nginx_rows": [
            event(ID_ONE, request_time_ms=100.0, cf_ray="ray-one"),
            event(ID_TWO, request_time_ms=200.0, cf_ray="ray-two"),
        ],
        "ssr_rows": [
            event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0),
            event(ID_ONE, stage="tournament_workspace", duration_ms=60.0),
            event(ID_ONE, stage="page_component", duration_ms=90.0),
            event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=80.0),
        ],
        "stream_rows": [],
        "api_rows": [
            event(
                ID_ONE,
                route_class="auth_bootstrap",
                total_ms=30.0,
                sql_ms=5.0,
                max_sql_ms=3.0,
                pool_checkout_wait_ms=1.0,
                pool_connection_hold_ms=4.0,
                compute_ms=10.0,
                sql_count=2,
            ),
            event(
                ID_ONE,
                route_class="tournament_workspace",
                total_ms=50.0,
                sql_ms=20.0,
                max_sql_ms=15.0,
                pool_checkout_wait_ms=5.0,
                pool_connection_hold_ms=10.0,
                compute_ms=20.0,
                sql_count=3,
            ),
            event(ID_TWO, route_class="auth_bootstrap", total_ms=70.0),
        ],
    }
    values.update(overrides)
    return summarize_ssr_request_correlation(**values)  # type: ignore[arg-type]


class SsrRequestCorrelationTests(unittest.TestCase):
    def test_capacity_capture_waits_to_grace_edge_then_syncs_journal(self) -> None:
        calls: list[object] = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return type("Result", (), {"returncode": 0})()

        with (
            patch.object(diagnostic_plan_tool.time, "time_ns", return_value=10_000_000_000),
            patch.object(diagnostic_plan_tool.time, "sleep", side_effect=calls.append),
            patch.object(diagnostic_plan_tool.subprocess, "run", side_effect=run),
        ):
            self.assertTrue(diagnostic_plan_tool._wait_and_sync_capacity_capture_marker(10_000))
        self.assertEqual(calls[0], 1.0)
        self.assertEqual(calls[1][0], ["/usr/bin/journalctl", "--sync"])  # type: ignore[index]

        calls.clear()
        with (
            patch.object(diagnostic_plan_tool.time, "time_ns", return_value=8_000_000_000),
            patch.object(diagnostic_plan_tool.subprocess, "run", side_effect=AssertionError("early sync")),
        ):
            self.assertFalse(diagnostic_plan_tool._wait_and_sync_capacity_capture_marker(10_000))

    def test_capacity_journal_collection_is_bounded_exact_and_selected(self) -> None:
        plan = {
            "targets": [{
                "service": "web", "pid": 123, "start_ticks": 456,
                "invocation_id": "a" * 32,
            }],
        }
        request_ids = {f"{value:032x}" for value in range(1_000)}
        unit = "deadlock-web.service"
        cgroup = f"/system.slice/{unit}"
        run_id = "b" * 32
        marker = (
            "ssr_stream_capture_coverage scheme=sha256-runid-requestid-1of8-v1 "
            f"run_id={run_id} eligible=1000 retained=1000 lost=0 rows=25000 "
            "lost_rows=0 status=complete plan_selected=1000 header_set=1000 "
            "header_already_sent=0 header_unavailable=0 header_error=0 "
            "selector_invalid=0 selector_unmatched=0 selector_capacity=0 "
            "outcome_status=complete"
        )
        messages = []
        padding = "x" * 430
        for request_id in sorted(request_ids):
            for _ in range(20):
                messages.append(
                    "ssr_perf diagnostic_run_id=" + run_id + " request_id=" + request_id
                    + " stage=page_component duration_ms=1 padding=" + padding
                )
            for _ in range(5):
                messages.append(
                    "ssr_stream request_id=" + request_id
                    + " stage=request_start elapsed_ms=1 padding=" + padding
                )
        messages.append(marker)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "journal.jsonl"
            with fixture.open("wb") as handle:
                for message in messages:
                    handle.write(json.dumps({
                        "MESSAGE": message,
                        "_SYSTEMD_UNIT": unit,
                        "_SYSTEMD_CGROUP": cgroup,
                        "_SYSTEMD_INVOCATION_ID": "a" * 32,
                        "__REALTIME_TIMESTAMP": "1793380800000000",
                    }, separators=(",", ":")).encode() + b"\n")
            self.assertLess(fixture.stat().st_size, diagnostic_plan_tool.CAPACITY_JOURNAL_BYTES)

            real_popen = subprocess.Popen
            commands: list[list[str]] = []

            def fixture_popen(command, **kwargs):
                commands.append(command)
                return real_popen(
                    [sys.executable, "-c",
                     "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                     str(fixture)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                )

            with patch.object(diagnostic_plan_tool.subprocess, "Popen", side_effect=fixture_popen):
                rows, reason = diagnostic_plan_tool._bounded_capacity_journal_rows(
                    "web", plan, since_ms=1_793_380_799_000,
                    until_ms=1_793_380_801_000,
                    markers=("ssr_perf", "ssr_stream", "ssr_stream_capture_coverage"),
                    run_id=run_id, request_ids=request_ids,
                )
            self.assertEqual(reason, "complete")
            self.assertEqual(len(rows), 25_001)
            self.assertIn("_SYSTEMD_UNIT=deadlock-web.service", commands[0])
            self.assertIn("_SYSTEMD_CGROUP=/system.slice/deadlock-web.service", commands[0])
            self.assertIn("_SYSTEMD_INVOCATION_ID=" + "a" * 32, commands[0])
            self.assertFalse(any(arg == "+" or arg.startswith("+_SYSTEMD_") for arg in commands[0]))
            self.assertNotIn("_PID", next(
                arg for arg in commands[0] if arg.startswith("--output-fields=")
            ))

            with fixture.open("wb") as handle:
                oversized = json.dumps({
                    "MESSAGE": "ssr_perf " + padding * 2,
                    "_SYSTEMD_UNIT": unit,
                    "_SYSTEMD_CGROUP": cgroup,
                    "_SYSTEMD_INVOCATION_ID": "a" * 32,
                    "__REALTIME_TIMESTAMP": "1793380800000000",
                }, separators=(",", ":")).encode() + b"\n"
                for _ in range(60_000):
                    handle.write(oversized)
            self.assertGreater(fixture.stat().st_size, diagnostic_plan_tool.CAPACITY_JOURNAL_BYTES)
            with patch.object(diagnostic_plan_tool.subprocess, "Popen", side_effect=fixture_popen):
                rows, reason = diagnostic_plan_tool._bounded_capacity_journal_rows(
                    "web", plan, since_ms=1_793_380_799_000,
                    until_ms=1_793_380_801_000, markers=("ssr_perf",),
                    run_id=run_id, request_ids=request_ids,
                )
            self.assertEqual(rows, [])
            self.assertEqual(reason, "byte_cap")

            with fixture.open("wb") as handle:
                bad_identity = json.dumps({
                    "MESSAGE": "ssr_perf diagnostic_run_id=" + run_id
                    + " request_id=" + sorted(request_ids)[0]
                    + " stage=page_component duration_ms=1",
                    "_SYSTEMD_UNIT": unit,
                    "_SYSTEMD_CGROUP": cgroup,
                    "_SYSTEMD_INVOCATION_ID": "c" * 32,
                    "__REALTIME_TIMESTAMP": "1793380800000000",
                }, separators=(",", ":")).encode() + b"\n"
                handle.write(bad_identity)
            with patch.object(diagnostic_plan_tool.subprocess, "Popen", side_effect=fixture_popen):
                rows, reason = diagnostic_plan_tool._bounded_capacity_journal_rows(
                    "web", plan, since_ms=1_793_380_799_000,
                    until_ms=1_793_380_801_000, markers=("ssr_perf",),
                    run_id=run_id, request_ids=request_ids,
                )
            self.assertEqual(rows, [])
            self.assertEqual(reason, "invalid_identity")

            with fixture.open("wb") as handle:
                handle.write(b"not-json\n")
                handle.write(bad_identity)
            with patch.object(diagnostic_plan_tool.subprocess, "Popen", side_effect=fixture_popen):
                rows, reason = diagnostic_plan_tool._bounded_capacity_journal_rows(
                    "web", plan, since_ms=1_793_380_799_000,
                    until_ms=1_793_380_801_000, markers=("ssr_perf",),
                    run_id=run_id, request_ids=request_ids,
                )
            self.assertEqual(rows, [])
            self.assertEqual(reason, "invalid_event")

    def test_pinned_plan_correlation_runs_from_isolated_single_file_copy(self) -> None:
        plan_source = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_cpu_diagnostic_plan.py"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            isolated_plan = Path(temporary_directory) / "platform_cpu_diagnostic_plan.py"
            shutil.copyfile(plan_source, isolated_plan)
            script = r"""
import importlib.util
import sys
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location("isolated_plan", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
summary = module.summarize_ssr_request_correlation(
    selected_request_ids=[], submitted_html_count=0, valid_response_id_count=0,
    eligible_response_id_count=0,
    selection_scheme="sha256-runid-requestid-1of8-v1",
    run_id="0123456789abcdef0123456789abcdef", phase="on",
    window_start_utc="2026-10-29T12:00:00+00:00",
    window_end_utc="2026-10-29T12:00:15+00:00",
    nginx_rows=[], ssr_rows=[], stream_rows=[], api_rows=[],
)
assert summary["schema_version"] == 1

request_id = "00000000000000000000000000000002"
run_id = "0123456789abcdef0123456789abcdef"
start_ms = int(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
event_time = datetime.fromtimestamp((start_ms + 100) / 1000, timezone.utc).isoformat()
event_us = str((start_ms + 100) * 1000)
api_messages = [
    "request_perf request_id=" + request_id + " path=/api/v1/auth/bootstrap "
    "total_ms=22 sql_ms=3 max_sql_ms=2 pool_checkout_wait_ms=0.5 "
    "pool_connection_hold_ms=4 compute_ms=8 sql_count=1",
    "request_perf request_id=" + request_id + " path=/api/v1/tournaments/t/workspace "
    "total_ms=41 sql_ms=9 max_sql_ms=6 pool_checkout_wait_ms=1 "
    "pool_connection_hold_ms=12 compute_ms=16 sql_count=2",
]
web_messages = [
    "ssr_perf diagnostic_run_id=" + run_id + " request_id=" + request_id
    + " stage=page_component duration_ms=80",
    "ssr_perf diagnostic_run_id=" + run_id + " request_id=" + request_id
    + " stage=tournament_workspace duration_ms=30",
]
web_messages += [
    "ssr_stream request_id=" + request_id + " stage=" + stage + " elapsed_ms=" + str(index)
    for index, stage in enumerate((
        "request_start", "response_stream_start", "first_body_write_attempt",
        "response_finish", "response_close", "response_error",
    ), 1)
]
web_messages.append(
    "ssr_stream_capture_coverage scheme=sha256-runid-requestid-1of8-v1 "
    "run_id=" + run_id + " eligible=1 retained=1 lost=0 rows=6 lost_rows=0 status=complete "
    "plan_selected=1 header_set=1 header_already_sent=0 header_unavailable=0 header_error=0 "
    "selector_invalid=0 selector_unmatched=0 selector_capacity=0 outcome_status=complete"
)
late_marker = web_messages.pop()
def records(messages):
    return [{"MESSAGE": message, "__REALTIME_TIMESTAMP": event_us} for message in messages]

query_order = []
marker_ready = False
collector_state = {"sync_ok": True, "web_reason": "complete"}
def wait_and_sync(expires_at_ms):
    global marker_ready
    query_order.append(("sync", expires_at_ms))
    marker_ready = True
    return collector_state["sync_ok"]
def bounded_rows(service, *args, **kwargs):
    query_order.append(("query", service))
    if service == "web":
        assert marker_ready
        if collector_state["web_reason"] != "complete":
            return [], collector_state["web_reason"]
        return records(web_messages + [late_marker]), "complete"
    return records(api_messages), "complete"
module._wait_and_sync_capacity_capture_marker = wait_and_sync
module._bounded_capacity_journal_rows = bounded_rows
module._bounded_nginx_rows = lambda *args, **kwargs: ([{
    "request_id": request_id, "time": event_time, "request_time": "0.09",
    "cf_ray": "safe-ray",
}], False)
plan = {
    "run_id": run_id, "workload": "authenticated_html_ssr_capacity_120s_v1",
    "on_start_ms": start_ms, "on_end_ms": start_ms + 120000,
    "expires_at_ms": start_ms + 135000,
}
correlated = module._capacity_request_telemetry(
    {"api": dict(plan), "web": dict(plan)},
    selected_request_ids=[request_id],
    cohort_counts={
        "submitted_html_count": 1, "valid_response_id_count": 1,
        "missing_response_id_count": 0, "duplicate_response_id_count": 0,
        "malformed_response_id_count": 0, "eligible_response_id_count": 1,
        "retained_sample_count": 1,
        "sample_scheme": "sha256-runid-requestid-1of8-v1",
        "sample_cap_truncated": False,
    },
)
assert correlated["join"]["matched_api_rows"] == 2
assert correlated["timing_ms"]["api_request_by_route"]["auth_bootstrap"]["total_ms"]["count"] == 1
assert correlated["timing_ms"]["api_request_by_route"]["tournament_workspace"]["total_ms"]["count"] == 1
assert correlated["timing_ms"]["ssr_stage"]["page_component"]["count"] == 1
assert correlated["timing_ms"]["ssr_stream_milestone_elapsed_ms"]["request_start"]["count"] == 1
assert correlated["ssr_stream_coverage"]["capture_status"] == "complete"
assert query_order == [("query", "api"), ("sync", plan["expires_at_ms"]), ("query", "web")]
assert correlated["coverage"]["source_collection_reason"] == {"api": "complete", "web": "complete"}
collector_state.update(sync_ok=True, web_reason="byte_cap")
failed_query = module._capacity_request_telemetry(
    {"api": dict(plan), "web": dict(plan)},
    selected_request_ids=[request_id],
    cohort_counts={
        "submitted_html_count": 1, "valid_response_id_count": 1,
        "missing_response_id_count": 0, "duplicate_response_id_count": 0,
        "malformed_response_id_count": 0, "eligible_response_id_count": 1,
        "retained_sample_count": 1,
        "sample_scheme": "sha256-runid-requestid-1of8-v1",
        "sample_cap_truncated": False,
    },
)
assert failed_query["coverage"]["source_collection_reason"]["web"] == "byte_cap"
assert failed_query["coverage"]["source_rows_truncated"]["ssr"]
assert failed_query["coverage"]["source_rows_truncated"]["stream"]
collector_state.update(sync_ok=False, web_reason="byte_cap")
sync_and_query_failure = module._capacity_request_telemetry(
    {"api": dict(plan), "web": dict(plan)},
    selected_request_ids=[request_id],
    cohort_counts={
        "submitted_html_count": 1, "valid_response_id_count": 1,
        "missing_response_id_count": 0, "duplicate_response_id_count": 0,
        "malformed_response_id_count": 0, "eligible_response_id_count": 1,
        "retained_sample_count": 1,
        "sample_scheme": "sha256-runid-requestid-1of8-v1",
        "sample_cap_truncated": False,
    },
)
assert sync_and_query_failure["coverage"]["source_collection_reason"]["web"] == "journal_sync_failed"
collector_state.update(sync_ok=False, web_reason="complete")
failed_sync = module._capacity_request_telemetry(
    {"api": dict(plan), "web": dict(plan)},
    selected_request_ids=[request_id],
    cohort_counts={
        "submitted_html_count": 1, "valid_response_id_count": 1,
        "missing_response_id_count": 0, "duplicate_response_id_count": 0,
        "malformed_response_id_count": 0, "eligible_response_id_count": 1,
        "retained_sample_count": 1,
        "sample_scheme": "sha256-runid-requestid-1of8-v1",
        "sample_cap_truncated": False,
    },
)
assert failed_sync["coverage"]["source_collection_reason"]["web"] == "journal_sync_failed"
print("ok")
"""
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script, str(isolated_plan)],
                cwd=temporary_directory,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_one_html_parent_joins_all_api_calls_and_ssr_stages(self) -> None:
        unselected_id = "00000000000000000000000000000004"
        trace_start = "2026-10-29T11:59:32+00:00"

        def trace_event(request_id: str, **fields: object) -> dict[str, object]:
            row = event(request_id, **fields)
            row["window_start_utc"] = trace_start
            return row

        report = summarize(
            window_start_utc=trace_start,
            nginx_rows=[
                trace_event(ID_ONE, request_time_ms=100.0, cf_ray="ray-one"),
                trace_event(ID_TWO, request_time_ms=200.0, cf_ray="ray-two"),
                trace_event(unselected_id, request_time_ms=300.0, cf_ray="unselected-ray"),
            ],
            ssr_rows=[
                trace_event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0),
                trace_event(ID_ONE, stage="tournament_workspace", duration_ms=60.0),
                trace_event(ID_ONE, stage="page_component", duration_ms=90.0),
                trace_event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=80.0),
                trace_event(unselected_id, stage="page_component", duration_ms=500.0),
            ],
            api_rows=[
                trace_event(ID_ONE, source_time="2026-10-29T11:59:53Z",
                           route_class="auth_bootstrap", total_ms=25.0),
                trace_event(ID_ONE, route_class="auth_bootstrap", total_ms=30.0, sql_ms=5.0,
                            max_sql_ms=3.0, pool_checkout_wait_ms=1.0,
                            pool_connection_hold_ms=4.0, compute_ms=10.0, sql_count=2),
                trace_event(ID_ONE, route_class="tournament_workspace", total_ms=50.0, sql_ms=20.0,
                            max_sql_ms=15.0, pool_checkout_wait_ms=5.0,
                            pool_connection_hold_ms=10.0, compute_ms=20.0, sql_count=3),
                trace_event(ID_TWO, route_class="auth_bootstrap", total_ms=70.0),
                trace_event(unselected_id, route_class="auth_bootstrap", total_ms=999.0),
            ],
        )

        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_ssr_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_rows"], 4)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_by_request_id"], 4)  # type: ignore[index]
        api_metrics = report["timing_ms"]["api_request_by_route"]  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["total_ms"]["count"], 3)  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["sql_ms"]["count"], 1)  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["sql_count"]["count"], 1)  # type: ignore[index]
        self.assertEqual(api_metrics["tournament_workspace"]["total_ms"]["count"], 1)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["api_metric_missing_or_invalid_by_route"]["auth_bootstrap"]["sql_ms"],
            2,  # type: ignore[index]
        )
        self.assertEqual(
            report["timing_ms"]["ssr_stage"]["auth_bootstrap_fetch"]["count"],
            # type: ignore[index]
            2,
        )
        self.assertEqual(
            report["timing_ms"]["ssr_stage"]["page_component"]["max_ms"],
            90.0,  # type: ignore[index]
        )
        serialized = json.dumps(report)
        self.assertNotIn(ID_ONE, serialized)
        self.assertNotIn(ID_TWO, serialized)

    def test_shared_selection_scheme_hash_vectors(self) -> None:
        self.assertEqual(
            response_id_selection_digest(RUN_ID, ID_ONE),
            "a078d044988363164c156d124496875ff00154849f1e9bd69494d0d35c1e5543",
        )
        self.assertTrue(is_eligible_response_id(RUN_ID, ID_ONE))
        self.assertEqual(
            response_id_selection_digest(RUN_ID, INELIGIBLE_ID),
            "afb26a2492c5078e5fbfe5479af31e66eac3813ddfa56b088e0d6a37eefee335",
        )
        self.assertFalse(is_eligible_response_id(RUN_ID, INELIGIBLE_ID))
        web_ineligible_id = "11111111111111111111111111111111"
        self.assertEqual(
            response_id_selection_digest(RUN_ID, web_ineligible_id),
            "3292b6e9f49cf13f86d5fa9119fb018d2de5d94882332e519636f1454bbe5380",
        )
        self.assertFalse(is_eligible_response_id(RUN_ID, web_ineligible_id))

    def test_stream_offsets_are_separate_from_ssr_durations(self) -> None:
        report = summarize(
            stream_rows=[
                event(
                    ID_ONE,
                    stage="first_body_write_attempt",
                    elapsed_ms=120.0,
                    headers="private-header-value",
                ),
                event(ID_ONE, stage="response_finish", elapsed_ms=240.0),
                event(ID_ONE, stage="response_close"),
                event(ID_ONE, stage="response_error", elapsed_ms=float("nan")),
            ]
        )

        ssr_stages = report["timing_ms"]["ssr_stage"]  # type: ignore[index]
        milestones = report["timing_ms"]["ssr_stream_milestone_elapsed_ms"]  # type: ignore[index]
        self.assertEqual(ssr_stages["page_component"]["max_ms"], 90.0)  # type: ignore[index]
        self.assertNotIn("first_body_write_attempt", ssr_stages)
        self.assertEqual(milestones["first_body_write_attempt"]["max_ms"], 120.0)  # type: ignore[index]
        self.assertEqual(milestones["response_finish"]["max_ms"], 240.0)  # type: ignore[index]
        self.assertEqual(milestones["response_close"]["count"], 0)  # type: ignore[index]
        self.assertIsNone(milestones["response_close"]["avg_ms"])  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["stream_milestone_missing_or_invalid"],
            {"response_close": 1, "response_error": 1},  # type: ignore[index]
        )
        serialized = json.dumps(report)
        self.assertNotIn("private-header-value", serialized)
        self.assertNotIn(ID_ONE, serialized)

    def test_distinct_repeated_attempts_remain_separate(self) -> None:
        report = summarize(
            submitted_html_count=2,
            valid_response_id_count=2,
            selected_request_ids=[ID_ONE, ID_TWO],
            nginx_rows=[
                event(ID_ONE, request_time_ms=100.0),
                event(ID_TWO, request_time_ms=100.0),
            ],
            ssr_rows=[
                event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0),
                event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=10.0),
            ],
            api_rows=[
                event(ID_ONE, route_class="auth_bootstrap", total_ms=5.0),
                event(ID_TWO, route_class="auth_bootstrap", total_ms=5.0),
            ],
        )

        self.assertEqual(report["cohort"]["submitted_html_requests"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_rows"], 2)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["api_request_by_route"]["auth_bootstrap"]["total_ms"]["count"],
            # type: ignore[index]
            2,
        )

    def test_late_completion_and_error_remain_in_submitted_denominator(self) -> None:
        late = "2026-10-29T12:02:59Z"
        report = summarize(
            submitted_html_count=10,
            valid_response_id_count=2,
            window_end_utc=WINDOW_END,
            nginx_rows=[
                event(ID_ONE, source_time=late, request_time_ms=800.0),
                event(ID_TWO, request_time_ms=200.0),
            ],
            ssr_rows=[
                event(
                    ID_ONE,
                    source_time=late,
                    stage="auth_bootstrap_fetch",
                    duration_ms=700.0,
                    outcome="error",
                )
            ],
            api_rows=[
                event(
                    ID_ONE,
                    source_time=late,
                    route_class="auth_bootstrap",
                    total_ms=600.0,
                    outcome="error",
                )
            ],
        )

        self.assertEqual(report["cohort"]["submitted_html_requests"], 10)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["nginx_request"]["max_ms"],
            800.0,  # type: ignore[index]
        )
        self.assertEqual(report["join"]["matched_api_rows"], 1)  # type: ignore[index]

    def test_orphans_duplicate_parent_and_duplicate_sample_ids_are_reported(self) -> None:
        orphan = "d" * 32
        report = summarize(
            selected_request_ids=[ID_ONE, ID_ONE, ID_TWO],
            nginx_rows=[
                event(ID_ONE, request_time_ms=10.0),
                event(ID_ONE, request_time_ms=11.0),
                event(ID_TWO, request_time_ms=12.0),
                event(orphan, request_time_ms=13.0),
            ],
            ssr_rows=[event(orphan, stage="auth_bootstrap_fetch", duration_ms=4.0)],
            api_rows=[event(orphan, route_class="auth_bootstrap", total_ms=3.0)],
        )

        self.assertEqual(
            report["cohort"]["selected_sample_duplicate_ids"],
            1,  # type: ignore[index]
        )
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]
        self.assertEqual(report["join"]["duplicate_nginx_request_ids"], 1)  # type: ignore[index]
        self.assertEqual(report["join"]["orphan_ssr_rows"], 1)  # type: ignore[index]
        self.assertEqual(report["join"]["orphan_api_rows"], 1)  # type: ignore[index]

    def test_cross_run_or_phase_rows_are_rejected(self) -> None:
        report = summarize(
            ssr_rows=[
                event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0, run_id="other-run"),
                event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=50.0, phase="off"),
            ],
        )

        self.assertEqual(report["join"]["matched_ssr_parents"], 0)  # type: ignore[index]
        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]
        self.assertEqual(
            report["coverage"]["source_rejections"]["ssr"]["context_mismatch"],
            # type: ignore[index]
            2,
        )

    def test_context_mismatch_cannot_replace_matching_parent_row(self) -> None:
        report = summarize(
            nginx_rows=[
                event(ID_ONE, run_id="other-run", request_time_ms=999.0),
                event(ID_ONE, request_time_ms=100.0),
                event(ID_TWO, request_time_ms=200.0),
            ],
        )

        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["timing_ms"]["nginx_request"]["max_ms"], 200.0)  # type: ignore[index]
        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]

    def test_missing_or_invalid_event_timestamps_fail_closed(self) -> None:
        report = summarize(
            nginx_rows=[
                {
                    key: value
                    for key, value in event(ID_ONE, request_time_ms=100.0).items()
                    if key != "event_at_utc"
                },
                event(ID_TWO, source_time="not-a-time", request_time_ms=200.0),
            ],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0)],
            api_rows=[],
        )

        self.assertEqual(report["join"]["matched_nginx_parents"], 0)  # type: ignore[index]
        self.assertEqual(
            report["coverage"]["source_rejections"]["nginx"]["missing_or_invalid_timestamp"],
            2,  # type: ignore[index]
        )
        self.assertEqual(report["timing_ms"]["nginx_request"]["count"], 0)  # type: ignore[index]

    def test_declared_and_local_truncation_prevent_full_coverage_claim(self) -> None:
        declared = summarize(rows_truncated={"api": True})
        bounded = summarize(
            max_rows=1,
            nginx_rows=[event(ID_ONE, request_time_ms=10.0), event(ID_TWO, request_time_ms=20.0)],
        )

        self.assertFalse(declared["coverage"]["complete"])  # type: ignore[index]
        self.assertTrue(declared["coverage"]["source_rows_truncated"]["api"])  # type: ignore[index]
        self.assertFalse(bounded["coverage"]["complete"])  # type: ignore[index]
        self.assertTrue(
            bounded["coverage"]["source_rows_truncated"]["nginx"]  # type: ignore[index]
        )

    def test_source_collection_reason_is_closed_and_consistent_with_truncation(self) -> None:
        with self.assertRaises(ValueError):
            summarize(source_collection_reason={"api": "byte_cap", "web": "complete"})
        with self.assertRaises(ValueError):
            summarize(source_collection_reason={"api": "private detail", "web": "complete"})
        with self.assertRaises(ValueError):
            summarize(source_collection_reason={"api": [], "web": "complete"})

        incomplete = summarize(
            rows_truncated={"api": True},
            source_collection_reason={"api": "byte_cap", "web": "complete"},
        )
        incomplete["ssr_stream_coverage"] = {
            "records": 0, "selector_overflow": 0,
            "selector_overflow_unattributed": 0, "row_cap_records": 0,
            "row_cap_rows": 0, "truncated_records": 0, "malformed_records": 0,
            "capture_marker_records": 1, "capture_eligible_requests": 1,
            "capture_retained_requests": 1, "capture_lost_requests": 0,
            "capture_rows": 1, "capture_lost_rows": 0,
            "capture_truncated_records": 0, "capture_malformed_records": 0,
            "plan_selected": 1, "header_set": 1, "header_already_sent": 0,
            "header_unavailable": 0, "header_error": 0, "selector_invalid": 0,
            "selector_unmatched": 0, "selector_capacity": 0,
            "outcome_truncated_records": 0,
            "capture_status": "complete", "status": "none",
        }
        projected = capacity_diagnostic._project_request_correlation({
            "status": "incomplete", "summary": incomplete,
        })
        self.assertIsNotNone(projected)
        self.assertEqual(
            projected["summary"]["coverage"]["source_collection_reason"]["api"],
            "byte_cap",
        )
        malformed = json.loads(json.dumps({
            "status": "incomplete", "summary": incomplete,
        }))
        malformed["summary"]["coverage"]["source_collection_reason"]["api"] = "free text"
        self.assertIsNone(capacity_diagnostic._project_request_correlation(malformed))
        contradictory = json.loads(json.dumps({
            "status": "incomplete", "summary": incomplete,
        }))
        contradictory["summary"]["coverage"]["complete"] = True
        contradictory["summary"]["coverage"]["full_cohort"] = True
        self.assertIsNone(capacity_diagnostic._project_request_correlation(contradictory))

    def test_malformed_ids_do_not_join_or_escape_report(self) -> None:
        secret = "request-secret-with-sensitive-value"
        report = summarize(
            selected_request_ids=[secret, ID_TWO],
            nginx_rows=[event(secret, request_time_ms=1.0), event(ID_TWO, request_time_ms=2.0)],
        )

        self.assertEqual(
            report["cohort"]["selected_sample_malformed_ids"],
            1,  # type: ignore[index]
        )
        self.assertEqual(report["join"]["matched_nginx_parents"], 1)  # type: ignore[index]
        self.assertNotIn(secret, json.dumps(report))

    def test_cf_ray_fallback_requires_explicit_exact_proof(self) -> None:
        parent = event(ID_ONE, cf_ray="ray-exact", request_time_ms=100.0)
        api = event("e" * 32, cf_ray="ray-exact", route_class="auth_bootstrap", total_ms=20.0)
        no_fallback = summarize(
            nginx_rows=[parent],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0)],
            api_rows=[api],
            valid_response_id_count=1,
            selected_request_ids=[ID_ONE],
        )
        proven_fallback = summarize(
            nginx_rows=[parent],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0)],
            api_rows=[api],
            valid_response_id_count=1,
            selected_request_ids=[ID_ONE],
            cf_ray_fallback_proven=True,
        )

        self.assertEqual(no_fallback["join"]["matched_api_rows"], 0)  # type: ignore[index]
        self.assertEqual(proven_fallback["join"]["matched_api_rows"], 1)  # type: ignore[index]
        self.assertEqual(proven_fallback["join"]["matched_api_by_cf_ray"], 1)  # type: ignore[index]

    def test_component_metrics_reject_negative_nan_and_ignore_extra_fields(self) -> None:
        secret = "never-serialize-private-observer-content"
        report = summarize(
            selected_request_ids=[ID_ONE],
            submitted_html_count=1,
            valid_response_id_count=1,
            nginx_rows=[event(ID_ONE, request_time_ms=10.0)],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=2.0)],
            api_rows=[
                event(
                    ID_ONE,
                    route_class="auth_bootstrap",
                    total_ms=-2.0,
                    sql_ms=float("nan"),
                    pool_checkout_wait_ms=-1.0,
                    sql_count=2,
                    arbitrary_private_field=secret,
                )
            ],
        )

        metrics = report["timing_ms"]["api_request_by_route"]["auth_bootstrap"]  # type: ignore[index]
        self.assertEqual(metrics["total_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["sql_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["pool_checkout_wait_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["sql_count"]["count"], 1)  # type: ignore[index]
        missing = report["timing_ms"]["api_metric_missing_or_invalid_by_route"][
            "auth_bootstrap"
        ]  # type: ignore[index]
        self.assertEqual(missing["total_ms"], 1)  # type: ignore[index]
        self.assertEqual(missing["sql_ms"], 1)  # type: ignore[index]
        self.assertEqual(missing["pool_checkout_wait_ms"], 1)  # type: ignore[index]
        serialized = json.dumps(report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("nan", serialized)

    def test_duplicate_cohort_ids_are_excluded_as_ambiguous(self) -> None:
        report = summarize(
            submitted_html_count=2,
            valid_response_id_count=0,
            duplicate_response_id_count=2,
            selected_request_ids=[],
            nginx_rows=[],
            ssr_rows=[],
            api_rows=[],
        )

        self.assertEqual(report["cohort"]["valid_response_ids"], 0)  # type: ignore[index]
        self.assertEqual(report["cohort"]["duplicate_response_ids"], 2)  # type: ignore[index]
        self.assertFalse(report["coverage"]["full_cohort"])  # type: ignore[index]

    def test_caller_and_helper_bounds_are_visible_in_coverage(self) -> None:
        report = summarize(
            selected_request_ids=[ID_ONE, ID_TWO, ID_THREE],
            valid_response_id_count=3,
            sample_limit=2,
        )

        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]
        self.assertEqual(report["cohort"]["selected_sample_received"], 2)  # type: ignore[index]

    def test_ineligible_sample_id_is_rejected(self) -> None:
        report = summarize(
            selected_request_ids=[ID_ONE, INELIGIBLE_ID],
            valid_response_id_count=2,
            eligible_response_id_count=2,
        )

        self.assertEqual(report["cohort"]["selected_sample_ineligible_ids"], 1)  # type: ignore[index]
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]

    def test_selection_scheme_and_eligible_count_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            summarize(selection_scheme="unreviewed")
        with self.assertRaises(ValueError):
            summarize(valid_response_id_count=1, eligible_response_id_count=2)


if __name__ == "__main__":
    unittest.main()
