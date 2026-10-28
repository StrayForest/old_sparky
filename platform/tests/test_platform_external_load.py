from __future__ import annotations

from contextlib import ExitStack
import http.client
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import tools.platform_external_load as external_load
import tools.platform_ssr_capacity_diagnostic as capacity_diagnostic
from tools.platform_evidence_sanitizer import project_public_artifact
from tools.platform_external_load import (
    ExternalLoadError,
    LogicalRequestResult,
    RequestResult,
    VirtualUser,
    _route_for_read,
    analyze_concurrency_ramp,
    _ready_vote_action,
    _annotate_timing,
    _ResultAccumulator,
    _request,
    load_manifest,
    percentile,
    run_load,
    run_phase,
    run_authenticated_workspace_capacity_phase,
    run_rate_phase,
    spread_offsets,
    summarize_logical_results,
    summarize_results,
)


RSS_PROBE = Path(__file__).with_name("fixtures") / "external_load_rss_probe.py"
RSS_DEADLINE_SECONDS = 15.0
RSS_DEADLINES = {"live": RSS_DEADLINE_SECONDS, "accumulator": 30.0, "hostile": 0.5}
LIVE_HWM_LIMIT_BYTES = 192 * 1024 * 1024
ACCUMULATOR_HWM_LIMIT_BYTES = 24 * 1024 * 1024
PROBE_OUTPUT_LIMIT_BYTES = 64 * 1024


def lifecycle_marker(workspace_ready: bool | None) -> bytes:
    readiness_attribute = (
        b""
        if workspace_ready is None
        else b' data-ssr-workspace-ready="'
        + (b"true" if workspace_ready else b"false")
        + b'"'
    )
    return (
        b'<span data-testid="tournament-detail-lifecycle"'
        + readiness_attribute
        + b' hidden></span>'
    )


def raw_heading(aria_label: str) -> bytes:
    return (
        b'<h1 class="hero-title" aria-label="'
        + aria_label.encode("utf-8")
        + b'"></h1>'
    )


def _run_rss_probe(mode: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        stdout_path, stderr_path = Path(directory) / "out", Path(directory) / "err"
        started = time.monotonic()
        deadline_seconds = RSS_DEADLINES[mode]
        deadline = started + deadline_seconds
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, str(RSS_PROBE), mode],
                cwd=Path(__file__).parents[1],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=True,
                preexec_fn=(
                    (lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN) or os.write(1, b"hostile-ready\n"))
                    if mode == "hostile" else None
                ),
            )
            pgid = process.pid
            cleanup_budget = min(0.5, deadline_seconds / 2)
            def signal_group(sig: int) -> None:
                try:
                    if process.poll() is None:
                        if os.getpgid(process.pid) != pgid:
                            raise AssertionError("probe process-group identity changed")
                        os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic() - cleanup_budget))
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGTERM)
                try:
                    process.wait(timeout=max(0.0, min(0.25, cleanup_budget / 2, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    signal_group(signal.SIGKILL)
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
            finally:
                signal_group(signal.SIGKILL)
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        if elapsed > deadline_seconds:
            raise AssertionError("RSS probe exceeded its absolute deadline")
        if os.path.exists(f"/proc/{pgid}"):
            raise AssertionError("RSS probe process was not reaped")
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("RSS probe process group survived cleanup")
        if process.returncode != 0:
            raise AssertionError(f"RSS probe failed (returncode={process.returncode}, stdout_bytes={min(stdout_path.stat().st_size, PROBE_OUTPUT_LIMIT_BYTES)}, stderr_bytes={min(stderr_path.stat().st_size, PROBE_OUTPUT_LIMIT_BYTES)})")
        if stdout_path.stat().st_size > PROBE_OUTPUT_LIMIT_BYTES or stderr_path.stat().st_size > PROBE_OUTPUT_LIMIT_BYTES:
            raise AssertionError("RSS probe output exceeded its cap")
        if mode == "hostile" and b"hostile-ready\n" not in stdout_path.read_bytes():
            raise AssertionError("RSS probe ready marker missing")
        if stderr_path.read_bytes():
            raise AssertionError("RSS probe emitted stderr")
        report = json.loads(stdout_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("mode") != mode:
            raise AssertionError("RSS probe report is invalid")
        return report


def manifest_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "purpose": "external_ready_vote",
        "origin": "https://old-sparky.com",
        "session_cookie_name": "deadlock_platform_session",
        "csrf_cookie_name": "deadlock_platform_session_csrf",
        "marker": "preprod26082900000000ab",
        "tournaments": [
            {"id": "tournament-1", "slug": "qa-tournament", "user_count": 2}
        ],
        "users": [
            {
                "user_id": "user-00000001",
                "tournament_slug": "qa-tournament",
                "session_token": "s" * 64,
                "csrf_token": "c" * 64,
            },
            {
                "user_id": "user-00000002",
                "tournament_slug": "qa-tournament",
                "session_token": "t" * 64,
                "csrf_token": "d" * 64,
            },
        ],
    }


def load_manifest_from_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], list[VirtualUser]]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_manifest(path)


class ExternalLoadTests(unittest.TestCase):
    def _assert_capacity_failure_evidence_is_bounded_closed_and_uses_observed_times(self) -> None:
        off = RequestResult(
            "off", "GET", "/private-fixture", 0, 5000.0, False, 0,
            error_kind="TimeoutError", failure_stage="response_headers",
            started_at_utc="2026-10-28T10:00:00+00:00",
            exception_at_utc="2026-10-28T10:00:05+00:00",
            finished_at_utc="2026-10-28T10:00:05+00:00",
        )
        on = RequestResult(
            "on", "GET", "/private-fixture", 0, 5000.0, False, 0,
            error_kind="TimeoutError", observed_status=200,
            failure_stage="body_receive",
            started_at_utc="2026-10-28T10:00:10+00:00",
            exception_at_utc="2026-10-28T10:00:15+00:00",
            finished_at_utc="2026-10-28T10:00:15+00:00",
            transport_timing={"body_receive_ms": 5000.0, "unsafe": "private"},
        )
        evidence = capacity_diagnostic._failure_evidence(
            off_results=[off], on_results=[on] * 10,
        )
        self.assertEqual(evidence["failure_count"], 11)
        self.assertTrue(evidence["records_truncated"])
        self.assertEqual(evidence["by_phase"]["off_probe"]["failure_count"], 1)
        self.assertEqual(evidence["by_phase"]["on_worker"]["failure_count"], 10)
        self.assertEqual(len(evidence["records"]), capacity_diagnostic.FAILURE_RECORD_LIMIT)
        self.assertEqual(evidence["records"][0]["phase"], "off_probe")
        self.assertIsNone(evidence["records"][0]["observed_status"])
        self.assertEqual(evidence["records"][1]["observed_status"], 200)
        self.assertEqual(evidence["records"][1]["request_status"], 0)
        self.assertEqual(evidence["records"][1]["stage"], "body_receive")
        self.assertEqual(evidence["records"][1]["phase_timing_ms"], {"body_receive_ms": 5000.0})
        self.assertNotIn("private", repr(evidence))
        self.assertTrue(capacity_diagnostic._failure_evidence_is_closed(evidence, completed=11))
        self.assertTrue(capacity_diagnostic._failure_evidence_is_closed(
            evidence, completed=11, phase_completed={"off_probe": 1, "on_worker": 10},
        ))
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(
            evidence, completed=11, phase_completed={"off_probe": 0, "on_worker": 10},
        ))
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(evidence, completed=9))
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(
            {**evidence, "unsafe": "private"}, completed=11,
        ))
        malformed = {**evidence, "records": [dict(evidence["records"][0], stage="private/path")]
                     + evidence["records"][1:]}
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(malformed, completed=11))
        bad_order = {**evidence, "records": [dict(
            evidence["records"][0], exception_at_utc="2026-10-28T09:59:59+00:00",
        )] + evidence["records"][1:]}
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(bad_order, completed=11))
        bad_status_pair = {**evidence, "records": [dict(
            evidence["records"][1], stage="body_receive", observed_status=None,
        )] + evidence["records"][1:]}
        self.assertFalse(capacity_diagnostic._failure_evidence_is_closed(bad_status_pair, completed=11))

    def _assert_capacity_actual_request_window_never_uses_planned_window(self) -> None:
        self.assertEqual(capacity_diagnostic._actual_request_window(None), (None, None))
        self.assertEqual(capacity_diagnostic._actual_request_window([]), (None, None))
        observed = [
            RequestResult(
                "on", "GET", "/private-fixture", 200, 12.0, True, 8,
                started_at_utc="2026-10-28T10:00:10+00:00",
                finished_at_utc="2026-10-28T10:00:11+00:00",
            ),
            RequestResult(
                "on", "GET", "/private-fixture", 0, 5000.0, False, 0,
                error_kind="TimeoutError",
                started_at_utc="2026-10-28T10:00:12+00:00",
                finished_at_utc="2026-10-28T10:00:17+00:00",
            ),
        ]
        self.assertEqual(capacity_diagnostic._actual_request_window(observed), (
            "2026-10-28T10:00:10+00:00", "2026-10-28T10:00:17+00:00",
        ))

    def _assert_capacity_worker_legacy_projection_marks_evidence_unavailable(self) -> None:
        legacy = {"worker_report_schema": 2, "status": "incomplete"}
        legacy_shape = frozenset(legacy)
        result = capacity_diagnostic._normalize_failure_evidence_fields(
            legacy,
            legacy_shapes=(legacy_shape,),
            current_shapes=(frozenset({
                "worker_report_schema", "status", "failure_evidence",
                "failure_evidence_status", "actual_on_request_started_at_utc",
                "actual_on_request_finished_at_utc",
            }),),
        )
        self.assertIs(result, True)
        self.assertEqual(legacy["failure_evidence_status"], "unavailable")
        self.assertIsNone(legacy["failure_evidence"])
        self.assertIsNone(legacy["actual_on_request_started_at_utc"])
        self.assertIsNone(legacy["actual_on_request_finished_at_utc"])

        malformed_current = {
            "worker_report_schema": 2, "status": "incomplete",
            "failure_evidence": {"private": "must-not-echo"},
            "failure_evidence_status": "unavailable",
            "actual_on_request_started_at_utc": None,
            "actual_on_request_finished_at_utc": None,
        }
        self.assertIsNone(capacity_diagnostic._normalize_failure_evidence_fields(
            malformed_current,
            legacy_shapes=(legacy_shape,),
            current_shapes=(frozenset(malformed_current),),
        ))
        canonical_legacy = {
            "worker_report_schema": 2, "status": "incomplete",
            "failure_evidence": None,
            "failure_evidence_status": "unavailable",
            "actual_on_request_started_at_utc": None,
            "actual_on_request_finished_at_utc": None,
        }
        canonical_shape = frozenset(canonical_legacy)
        self.assertIs(capacity_diagnostic._normalize_failure_evidence_fields(
            canonical_legacy, legacy_shapes=(), current_shapes=(canonical_shape,),
        ), True)
        for mutation in (
            {"failure_evidence": {"private": "must-not-echo"}},
            {"actual_on_request_started_at_utc": "2026-10-28T10:00:00Z"},
        ):
            self.assertIsNone(capacity_diagnostic._normalize_failure_evidence_fields(
                {**canonical_legacy, **mutation},
                legacy_shapes=(), current_shapes=(canonical_shape,),
            ))

    def _assert_capacity_reports_preserve_off_and_partial_on_failures(self) -> None:
        planned_start = 1_798_200_000_000
        capacity = {
            "concurrency": 8,
            "workload": capacity_diagnostic.WORKLOAD,
            "on_start_ms": planned_start,
            "on_end_ms": planned_start + capacity_diagnostic.ON_WINDOW_MS,
        }
        binding = {
            "source_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "external_run_id": "123",
            "external_run_attempt": "1",
            "run_id": "c" * 32,
            "profile_digest": capacity_diagnostic.profile_digest(8),
        }
        off_failure = RequestResult(
            "off", "GET", "/private-fixture", 0, 5000.0, False, 0,
            error_kind="TimeoutError", failure_stage="response_headers",
            started_at_utc="2026-10-28T10:00:00+00:00",
            exception_at_utc="2026-10-28T10:00:05+00:00",
            finished_at_utc="2026-10-28T10:00:05+00:00",
        )
        before_on = capacity_diagnostic._incomplete_report(
            capacity, binding, "off_probe_failed", submitted=0, completed=0,
            observed_duration_ms=0, manifest_sha256="d" * 64,
            off_probe_submitted=32, off_probe_completed=32,
            off_results=[off_failure],
        )
        self.assertEqual(before_on["status"], "incomplete")
        self.assertEqual(before_on["resource_abort_reason"], "off_probe_failed")
        self.assertEqual(before_on["on_window_start_ms"], planned_start)
        self.assertEqual(before_on["on_window_end_ms"], planned_start + capacity_diagnostic.ON_WINDOW_MS)
        self.assertIsNone(before_on["actual_on_request_started_at_utc"])
        self.assertIsNone(before_on["actual_on_request_finished_at_utc"])
        self.assertEqual(before_on["failure_evidence"]["by_phase"]["off_probe"]["error_class_counts"], {"timeout": 1})
        self.assertEqual(before_on["failure_evidence"]["by_phase"]["on_worker"]["failure_count"], 0)
        self.assertEqual(before_on["failure_evidence"]["records"][0]["observed_status"], None)

        on_success = RequestResult(
            "on", "GET", "/private-fixture", 200, 1000.0, True, 128,
            started_at_utc="2026-10-28T10:01:00+00:00",
            finished_at_utc="2026-10-28T10:01:01+00:00",
        )
        on_timeout = RequestResult(
            "on", "GET", "/private-fixture", 0, 5000.0, False, 0,
            error_kind="TimeoutError", observed_status=200,
            failure_stage="body_receive",
            started_at_utc="2026-10-28T10:01:02+00:00",
            exception_at_utc="2026-10-28T10:01:07+00:00",
            finished_at_utc="2026-10-28T10:01:07+00:00",
        )
        partial_on = capacity_diagnostic._incomplete_report(
            capacity, binding, "response_failure", submitted=2, completed=2,
            observed_duration_ms=7000, manifest_sha256="e" * 64,
            off_probe_submitted=32, off_probe_completed=32, off_probe_status=200,
            on_results=[on_success, on_timeout],
        )
        self.assertEqual(partial_on["status"], capacity_diagnostic._capacity_worker_status(
            aborted=True, all_200=False,
        ))
        self.assertEqual(partial_on["resource_abort_reason"], "response_failure")
        self.assertEqual(partial_on["actual_on_request_started_at_utc"], on_success.started_at_utc)
        self.assertEqual(partial_on["actual_on_request_finished_at_utc"], on_timeout.finished_at_utc)
        self.assertNotEqual(partial_on["actual_on_request_started_at_utc"], str(planned_start))
        record = partial_on["failure_evidence"]["records"][0]
        self.assertEqual(record["error_class"], "timeout")
        self.assertEqual(record["request_status"], 0)
        self.assertEqual(record["observed_status"], 200)
        self.assertEqual(record["phase"], "on_worker")

    def test_authenticated_html_contract_requires_authenticated_ready_page_markers(self) -> None:
        # Match the route's raw SSR shape: the workspace-seeded Hero and
        # authenticated header are rendered, while the detail view's
        # ssr:false boundary contributes its generic loading panel.
        marker = b"".join((
            lifecycle_marker(True),
            b"<span class='extra header-profile-avatar'></span>",
            b'<section class="hero-wrap">'
            + raw_heading("Fixture title")
            + b'</section>',
            b'<section class="panel panel-pad route-loading-shell" aria-busy="true"></section>',
        ))
        body = b" " * (64 * 1024 - 11) + marker
        self.assertEqual(
            external_load._authenticated_html_markers("Text/Html; charset=utf-8", body),
            (True, True),
        )
        chunk_boundary = 64 * 1024
        boundary_leading = (
            lifecycle_marker(True)
            + b'<span class="header-profile-avatar"></span><div class="breadcrumbs">'
        )
        breadcrumb_text = "Турниры / Fixture title"
        breadcrumb = breadcrumb_text.encode("utf-8")
        # Place the first byte of the two-byte Cyrillic `Т` at index 65535,
        # so its second byte lands in the next 64 KiB production feed slice.
        padding_size = chunk_boundary - 1 - len(boundary_leading)
        self.assertGreaterEqual(padding_size, 0)
        split_character_body = (
            boundary_leading
            + b" " * padding_size
            + breadcrumb
            + b'</div>'
            + raw_heading("Fixture title")
            + b'<section class="route-loading-shell"></section>'
        )
        self.assertEqual(split_character_body.find(breadcrumb), chunk_boundary - 1)

        recorded_parsers = []

        class RecordingMarkerParser(external_load._AuthenticatedHtmlMarkerParser):
            def __init__(self) -> None:
                super().__init__()
                self.text_nodes: list[str] = []
                recorded_parsers.append(self)

            def handle_data(self, data: str) -> None:
                super().handle_data(data)
                self.text_nodes.append(data)

        with patch.object(external_load, "_AuthenticatedHtmlMarkerParser", RecordingMarkerParser):
            self.assertEqual(
                external_load._authenticated_html_markers("text/html", split_character_body),
                (True, True),
            )
        parsed_breadcrumb = "".join(recorded_parsers[0].text_nodes)
        self.assertIn(breadcrumb_text, parsed_breadcrumb)
        self.assertNotIn("\ufffd", parsed_breadcrumb)
        self.assertEqual(
            external_load._authenticated_html_markers("application/json", marker),
            (False, True),
        )
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(True)
                    + b'<span class="header-profile-avatar"></span>'
                    + '<div class="breadcrumbs">Турниры / Fixture</div>'.encode("utf-8")
                    + raw_heading("Fixture")
                    + b'<section class="route-loading-shell"></section>'
                ),
            ),
            (True, True),
        )
        # The workspace seed is absent on the route-level loading response,
        # even though it has the same lifecycle/header markers.
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(False)
                    + b'<span class="header-profile-avatar"></span>'
                    + b'<h1 aria-label="Arbitrary error title"></h1>'
                    + b'<section class="route-loading-shell route-loading-tournament-detail"></section>'
                ),
            ),
            (True, False),
        )
        # Missing or false seed markers cannot pass even if the rest of the
        # authenticated raw page looks ready.
        for marker_without_seed in (lifecycle_marker(None), lifecycle_marker(False)):
            self.assertEqual(
                external_load._authenticated_html_markers(
                    "text/html",
                    marker_without_seed
                    + b'<span class="header-profile-avatar"></span>'
                    + raw_heading("Fixture title")
                    + b'<section class="route-loading-shell"></section>',
                ),
                (True, False),
            )
        # Public guest HTML has a ready workspace seed and expected fallback,
        # but must not satisfy the authenticated-header requirement.
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(True)
                    + raw_heading("Fixture")
                    + b'<section class="route-loading-shell"></section>'
                ),
            ),
            (True, False),
        )
        # A seeded title paired with the route-level loading variant is not
        # the expected ssr:false detail-boundary fallback.
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(True)
                    + b'<span class="header-profile-avatar"></span>'
                    + raw_heading("Fixture")
                    + b'<section class="route-loading-shell route-loading-tournament-detail"></section>'
                ),
            ),
            (True, False),
        )
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    b'<span class="header-profile-avatar"></span>'
                    + raw_heading("Fixture title")
                    + b'<section class="route-loading-shell"></section>'
                ),
            ),
            (True, False),
        )
        # A ready seed without the expected server fallback is not the
        # documented raw-SSR shape for this detail route.
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(True)
                    + b'<span class="header-profile-avatar"></span>'
                    + raw_heading("Fixture")
                ),
            ),
            (True, False),
        )
        # The hydrated detail grid is not a valid raw-SSR substitute for the
        # seeded heading and expected dynamic-boundary fallback.
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    lifecycle_marker(True)
                    + b'<span class="header-profile-avatar"></span>'
                    + b'<section class="top-detail-grid"></section>'
                ),
            ),
            (True, False),
        )
        self.assertEqual(
            external_load._authenticated_html_markers(
                "text/html",
                (
                    b'<!-- <span data-testid="tournament-detail-lifecycle" '
                    b'data-ssr-workspace-ready="true"></span> '
                    b'<section class="top-detail-grid"></section> '
                    b'<span class="header-profile-avatar"></span> '
                    + raw_heading("Fake")
                    + b' '
                    b'<section class="route-loading-shell"></section> -->'
                    b'<script>"<span data-testid=\"tournament-detail-lifecycle\"></span>"</script>'
                ),
            ),
            (True, False),
        )
        self.assertEqual(
            external_load._authenticated_html_markers("text/html", "not-bytes"),
            (True, False),
        )
        self.assertEqual(
            external_load._authenticated_html_marker_details("text/html", "not-bytes"),
            (True, False, None),
        )

    def test_html_keepalive_request_retains_only_closed_marker_flags(self) -> None:
        body = (
            lifecycle_marker(True)
            + b'<span class="header-profile-avatar"></span>'
            + b'<h1 class="hero-title" aria-label="'
            + b'Fixture"></h1>'
            + b'<section class="route-loading-shell"></section>'
        )
        client = Mock()
        client.last_timing = {}
        client.get.return_value = Mock(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
            timing={"total_ms": 12.0, "ttfb_ms": 4.0},
            content_encoding="identity",
            content_type_is_html=True,
        )
        user = VirtualUser("actor", "fixture-slug", "s" * 32, "c" * 32)
        with patch("tools.platform_external_load._http11_keepalive_client", return_value=client):
            result = external_load._page_request_http11_keepalive(
                "https://old-sparky.com", user, "html-capacity", 5.0,
                session_cookie_name="session", csrf_cookie_name="csrf",
                require_authenticated_html=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.html_content_type_ok, True)
        self.assertEqual(result.html_page_marker_ok, True)
        self.assertEqual(result.html_marker_flags, {
            "lifecycle": True, "authenticated_header": True,
            "workspace_ready": True, "detail_fallback": True,
        })
        self.assertEqual(result.html_content_encoding, "identity")
        self.assertEqual(result.response_bytes, len(body))
        self.assertNotIn(body.decode(), repr(result))
        call = client.get.call_args
        self.assertEqual(call.args[0], "/tournaments/fixture-slug")
        self.assertIn("session=" + user.session_token, call.kwargs["headers"]["Cookie"])

        state = {
            "gzip": False, "delay": False, "accept_encoding": [],
            "timeout_diagnostic_ids": [],
        }

        class LoopbackHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                state["accept_encoding"].append(self.headers.get("Accept-Encoding"))
                state["timeout_diagnostic_ids"].append(
                    self.headers.get("X-Platform-Timeout-Diagnostic-ID"),
                )
                if state["delay"]:
                    time.sleep(0.2)
                response_body = gzip.compress(body) if state["gzip"] else body
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if state["gzip"]:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                try:
                    self.wfile.write(response_body)
                except OSError:
                    return

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        external_load._page_transport_local.client = None
        try:
            ready_result = external_load._page_request(
                origin, user, "html-capacity", 2.0,
                session_cookie_name="session", csrf_cookie_name="csrf",
                transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
                capture_timestamps=True,
                require_authenticated_html=True,
            )
            self.assertIsNotNone(ready_result.started_at_utc)
            self.assertIsNotNone(ready_result.finished_at_utc)
            self.assertEqual(ready_result.status, 200)
            self.assertTrue(ready_result.html_content_type_ok)
            self.assertEqual(ready_result.html_content_encoding, "identity")
            self.assertEqual(ready_result.html_marker_flags, {
                "lifecycle": True, "authenticated_header": True,
                "workspace_ready": True, "detail_fallback": True,
            })
            self.assertTrue(ready_result.ok, (
                f"status={ready_result.status} content_type_ok={ready_result.html_content_type_ok} "
                f"marker_flags={ready_result.html_marker_flags} "
                f"coding={ready_result.html_content_encoding} error={ready_result.error_kind}"
            ))
            state["gzip"] = True
            gzip_result = external_load._page_request_http11_keepalive(
                origin, user, "html-capacity", 2.0,
                session_cookie_name="session", csrf_cookie_name="csrf",
                require_authenticated_html=True,
            )
            state["gzip"] = False
            state["delay"] = True
            delayed_result = external_load._page_request_http11_keepalive(
                origin, user, "html-capacity", 0.05,
                session_cookie_name="session", csrf_cookie_name="csrf",
                require_authenticated_html=True,
            )
            time.sleep(0.25)
        finally:
            external_load._page_transport_local.client.close()
            external_load._page_transport_local.client = None
        self.assertEqual(state["accept_encoding"], ["identity", "identity", "identity"])
        self.assertEqual(state["timeout_diagnostic_ids"], [None, None, None])
        self.assertFalse(gzip_result.ok)
        self.assertEqual(gzip_result.html_content_encoding, "gzip")
        self.assertFalse(gzip_result.html_page_marker_ok)
        self.assertTrue(
            gzip_result.html_marker_flags is None
            or all(value is False for value in gzip_result.html_marker_flags.values())
        )
        self.assertNotIn(body.decode("ascii"), repr(gzip_result))
        self.assertFalse(delayed_result.ok)
        self.assertEqual(delayed_result.status, 0)
        self.assertEqual(delayed_result.error_kind, "TimeoutError")
        self.assertIsNone(delayed_result.html_marker_flags)
        self.assertIsNone(delayed_result.html_content_encoding)

    def test_html_capacity_response_failure_stops_refill_and_keeps_partial_counts(self) -> None:
        actors = [VirtualUser(f"actor-{index}", "fixture", "s" * 32, "c" * 32) for index in range(14)]
        calls = 0
        lock = threading.Lock()

        def request(_origin: str, _user: VirtualUser, phase: str, _timeout: float) -> RequestResult:
            nonlocal calls
            with lock:
                calls += 1
            return RequestResult(phase, "GET", "/tournaments/fixture", 503, 3.0, False, 128)

        start_at = time.time_ns() // 1_000_000 + 25
        result = run_authenticated_workspace_capacity_phase(
            "https://example.invalid", actors, phase="html-capacity",
            duration_seconds=20, start_at_epoch_ms=start_at, end_at_epoch_ms=start_at + 20_000,
            concurrency=2, timeout=5, request_builder=request, stop_requested=lambda: None,
            abort_on_result=lambda item: "response_failure" if item.ok is not True else None,
        )
        self.assertTrue(result["aborted"])
        self.assertEqual(result["abort_reason"], "response_failure")
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(calls, 2)
        self._assert_capacity_failure_evidence_is_bounded_closed_and_uses_observed_times()
        self._assert_capacity_actual_request_window_never_uses_planned_window()
        self._assert_capacity_worker_legacy_projection_marks_evidence_unavailable()
        self._assert_capacity_reports_preserve_off_and_partial_on_failures()

    def test_capacity_closed_loop_guard_stops_refill_and_drains_inflight(self) -> None:
        actors = [VirtualUser(f"actor-{index}", "fixture", "s" * 32, "c" * 32) for index in range(2)]
        both_started = threading.Barrier(2)
        guard_tripped = threading.Event()
        end_callbacks: list[None] = []

        def request(_origin: str, _user: VirtualUser, phase: str, _timeout: float) -> RequestResult:
            both_started.wait(timeout=1)
            guard_tripped.set()
            return RequestResult(phase, "GET", "/workspace", 200, 1.0, True, 0)

        def guard() -> str | None:
            return "memory_floor" if guard_tripped.is_set() else None

        start_at = time.time_ns() // 1_000_000 + 25
        result = run_authenticated_workspace_capacity_phase(
            "https://example.invalid", actors, phase="ssr-capacity",
            duration_seconds=20, start_at_epoch_ms=start_at, end_at_epoch_ms=start_at + 20_000,
            concurrency=2, timeout=1,
            request_builder=request, stop_requested=guard,
            window_end_callback=lambda: end_callbacks.append(None),
        )
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertTrue(result["aborted"])
        self.assertEqual(result["abort_reason"], "memory_floor")
        self.assertEqual(result["window_start_at_ms"], start_at)
        self.assertEqual(result["window_end_at_ms"], start_at + 20_000)
        self.assertEqual([item.status for item in result["results"]], [200, 200])
        self.assertEqual(end_callbacks, [])

        def run_scheduled_window(*, pending_through_deadline: bool, control_complete: bool):
            class FakeClock:
                def __init__(self) -> None:
                    self.current = 100.0
                    self.start_epoch_ms = 1_000_000

                def monotonic(self) -> float:
                    return self.current

                def time_ns(self) -> int:
                    elapsed_ms = round((self.current - 100.0) * 1000)
                    return (self.start_epoch_ms + elapsed_ms) * 1_000_000

                def sleep(self, seconds: float) -> None:
                    self.current += seconds

            class FakeExecutor:
                _max_workers = 1

                def __init__(self) -> None:
                    self.futures: list[Future[RequestResult]] = []

                def submit(self, *_args: object, **_kwargs: object) -> Future[RequestResult]:
                    future: Future[RequestResult] = Future()
                    self.futures.append(future)
                    if not pending_through_deadline:
                        future.set_result(RequestResult(
                            "ssr-capacity", "GET", "/workspace", 200, 1.0, True, 0,
                        ))
                    return future

            clock = FakeClock()
            fake_executor = FakeExecutor()
            deadline = clock.current + 20.0
            edge_callbacks: list[tuple[float, bool]] = []
            wait_count = 0

            def fake_wait(futures, *, timeout, return_when):
                nonlocal wait_count
                wait_count += 1
                pending = {future for future in futures if not future.done()}
                if pending and clock.current < deadline:
                    clock.current = min(deadline, clock.current + timeout)
                    return set(), set(futures)
                if pending:
                    # The end edge callback must run before the overdue request
                    # is released and before the phase drains it.
                    self.assertEqual(edge_callbacks, [(deadline, False)])
                    clock.current = deadline + 0.376
                    for future in pending:
                        future.set_result(RequestResult(
                            "ssr-capacity", "GET", "/workspace", 200, 1.0, True, 0,
                        ))
                    return pending, set()
                done = {future for future in futures if future.done()}
                if done and clock.current < deadline:
                    clock.current = deadline
                return done, set()

            def request(*_args: object) -> RequestResult:
                raise AssertionError("fake executor does not run submitted callables")

            def stop_requested() -> str | None:
                if not control_complete or clock.current < deadline:
                    return None
                return "window_complete"

            def capture_end() -> None:
                edge_callbacks.append((clock.current, bool(fake_executor.futures[0].done())))

            with patch.object(external_load, "time", clock), patch.object(
                external_load, "wait", fake_wait,
            ):
                outcome = run_authenticated_workspace_capacity_phase(
                    "https://example.invalid", actors[:1], phase="ssr-capacity",
                    duration_seconds=20, start_at_epoch_ms=clock.start_epoch_ms,
                    end_at_epoch_ms=clock.start_epoch_ms + 20_000,
                    concurrency=1, timeout=5, request_builder=request,
                    stop_requested=stop_requested,
                    window_end_callback=capture_end,
                    executor=fake_executor,
                )
            self.assertGreater(wait_count, 0)
            self.assertEqual(len(edge_callbacks), 1)
            self.assertFalse(outcome["aborted"])
            if pending_through_deadline:
                self.assertEqual(
                    outcome["abort_reason"],
                    "window_complete" if control_complete else None,
                )
                self.assertEqual(outcome["duration_ms"], 20_376)
                self.assertEqual(outcome["completed"], 1)
            else:
                self.assertIsNone(outcome["abort_reason"])
                self.assertEqual(outcome["duration_ms"], 20_000)
                self.assertEqual(outcome["completed"], 1)
            return edge_callbacks

        # A control-file completion at the deadline must snapshot before the
        # 376ms in-flight drain; a local deadline with no in-flight work must
        # also invoke the callback once before the loop exits.
        self.assertEqual(
            run_scheduled_window(pending_through_deadline=True, control_complete=True),
            [(120.0, False)],
        )
        self.assertEqual(
            run_scheduled_window(pending_through_deadline=True, control_complete=False),
            [(120.0, False)],
        )
        self.assertEqual(
            run_scheduled_window(pending_through_deadline=False, control_complete=False),
            [(120.0, True)],
        )

    @staticmethod
    def _cgroup_diagnostic_snapshot(
        *,
        usage_usec: int = 10,
        membership: str = "/tenant/job",
        mount_root: str = "/tenant",
        mountpoint: str = "/sys/fs/cgroup",
        mount_id: int = 36,
        namespace_identity: tuple[int, int] = (1, 2),
        max_files: dict[str, str] | None = None,
        stat_override: str | None = None,
    ) -> dict[str, object]:
        mountinfo = (
            f"{mount_id} 25 0:32 {mount_root} {mountpoint} rw - cgroup2 cgroup rw\n"
        )
        files = max_files or {}

        def reader(path: Path) -> str:
            if path.name == "cpu.max":
                return files.get(str(path), "max 100000\n")
            if path.name == "cpu.stat":
                if stat_override is not None:
                    return stat_override
                values = {
                    "usage_usec": usage_usec,
                    "user_usec": usage_usec // 2,
                    "system_usec": usage_usec // 2,
                    "nr_periods": 4,
                    "nr_throttled": 1,
                    "throttled_usec": 2,
                }
                return "".join(f"{key} {value}\n" for key, value in values.items())
            if path.name == "cpuset.cpus.effective":
                return "0-3\n"
            raise AssertionError("unexpected cgroup diagnostic file")

        return external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text=f"0::{membership}\n",
            mountinfo_text=mountinfo,
            namespace_identity=namespace_identity,
            file_reader=reader,
        )

    def test_effective_cpu_capacity_stays_unknown_without_trusted_host_root(self) -> None:
        def unexpected_cpu_max_read(_path: Path) -> str:
            self.fail("visible cpu.max files cannot prove hidden ancestors absent")

        mount_cases = (
            ("/", "0::/tenant/job\n"),
            ("/tenant", "0::/tenant/job\n"),
        )
        for mount_root, cgroup_text in mount_cases:
            with self.subTest(mount_root=mount_root):
                mountinfo = (
                    f"36 25 0:32 {mount_root} /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
                )
                self.assertEqual(
                    external_load._effective_cpu_capacity_details(
                        cgroup_text=cgroup_text,
                        mountinfo_text=mountinfo,
                    ),
                    (None, "trusted_host_root_unproven"),
                )
                self.assertIsNone(
                    external_load._effective_cpu_capacity_cores(
                        cgroup_text=cgroup_text,
                        mountinfo_text=mountinfo,
                    )
                )

        ambiguous_mounts = (
            "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "37 25 0:33 / /other/cgroup rw - cgroup2 cgroup rw\n"
        )
        self.assertEqual(
            external_load._effective_cpu_capacity_details(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text=ambiguous_mounts,
            ),
            (None, "cgroup2_mount_ambiguous"),
        )
        self.assertEqual(
            external_load._effective_cpu_capacity_details(
                cgroup_text="0::/tenant/../private/path\n",
                mountinfo_text=(
                    "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
                ),
            ),
            (None, "cgroup_path_invalid"),
        )

    def test_cgroup_diagnostics_keep_subroot_max_unknown_and_report_visible_deltas(self) -> None:
        visible_limits = {
            "/sys/fs/cgroup/job/cpu.max": "max 100000\n",
            "/sys/fs/cgroup/cpu.max": "max 100000\n",
        }
        before = self._cgroup_diagnostic_snapshot(
            usage_usec=100, max_files=visible_limits
        )
        after = self._cgroup_diagnostic_snapshot(
            usage_usec=130, max_files=visible_limits
        )
        report = external_load._cgroup_diagnostic_report(before, after)
        self.assertEqual(report["visible_evidence_status"], "complete")
        self.assertEqual(report["quota_completeness"], "unknown")
        self.assertFalse(report["visible_mount_root_is_global"])
        self.assertEqual(report["visible_ancestor_count"], 2)
        self.assertEqual(
            [row["cpu_max_kind"] for row in report["visible_ancestors"]],
            ["max", "max"],
        )
        self.assertEqual(report["visible_ancestors"][0]["cpu_stat_delta"]["usage_usec"], 30)
        self.assertIn("trusted_host_root_unproven", report["reasons"])
        self.assertNotIn("/sys/fs/cgroup", json.dumps(report))
        self.assertNotIn("tenant", json.dumps(report))

        finite_limits = {
            "/sys/fs/cgroup/job/cpu.max": "99999999 100000000\n",
            "/sys/fs/cgroup/cpu.max": "max 100000\n",
        }
        finite_report = external_load._cgroup_diagnostic_report(
            self._cgroup_diagnostic_snapshot(usage_usec=100, max_files=finite_limits),
            self._cgroup_diagnostic_snapshot(usage_usec=130, max_files=finite_limits),
        )
        self.assertEqual(
            finite_report["visible_ancestors"][0]["cpu_max_quota_us"], 99_999_999
        )
        self.assertEqual(
            finite_report["visible_ancestors"][0]["cpu_max_period_us"], 100_000_000
        )

    def test_cgroup_diagnostic_mapping_rejects_malformed_or_ambiguous_mounts(self) -> None:
        with self.assertRaises(external_load._CgroupDiagnosticError) as mismatch:
            external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/outside/job\n",
                mountinfo_text="36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
                namespace_identity=(1, 2),
                file_reader=lambda _path: "max 100000\n",
            )
        self.assertEqual(mismatch.exception.reason, "cgroup_mount_mapping_invalid")

        duplicate_mounts = (
            "36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "37 25 0:32 /tenant /other/cgroup rw - cgroup2 cgroup rw\n"
        )
        with self.assertRaises(external_load._CgroupDiagnosticError) as ambiguous:
            external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text=duplicate_mounts,
                namespace_identity=(1, 2),
                file_reader=lambda _path: "max 100000\n",
            )
        self.assertEqual(ambiguous.exception.reason, "cgroup2_mount_ambiguous")

    def test_global_cgroup_root_without_cpu_max_is_unlimited_but_leaf_missing_is_invalid(self) -> None:
        mountinfo = "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        stat = "usage_usec 10\nuser_usec 5\nsystem_usec 5\nnr_periods 1\nnr_throttled 0\nthrottled_usec 0\n"

        def root_missing_reader(path: Path) -> str:
            if path == Path("/sys/fs/cgroup/cpu.max"):
                raise FileNotFoundError(path)
            if path.name == "cpu.max":
                return "max 100000\n"
            if path.name == "cpu.stat":
                return stat
            if path.name == "cpuset.cpus.effective":
                return "0-3\n"
            raise FileNotFoundError(path)

        snapshot = external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text="0::/job\n", mountinfo_text=mountinfo,
            namespace_identity=(1, 2), file_reader=root_missing_reader,
        )
        self.assertEqual(snapshot["reasons"], [])
        self.assertEqual(snapshot["ancestors"][-1]["cpu_max"], (None, 100_000))

        def leaf_missing_reader(path: Path) -> str:
            if path == Path("/sys/fs/cgroup/job/cpu.max"):
                raise FileNotFoundError(path)
            if path.name == "cpu.max":
                return "max 100000\n"
            if path.name == "cpu.stat":
                return stat
            if path.name == "cpuset.cpus.effective":
                return "0-3\n"
            raise FileNotFoundError(path)

        incomplete = external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text="0::/job\n", mountinfo_text=mountinfo,
            namespace_identity=(1, 2), file_reader=leaf_missing_reader,
        )
        self.assertIn("cpu_max_unavailable", incomplete["reasons"])

        root_usage_only = "usage_usec 100\nuser_usec 50\nsystem_usec 50\n"
        def root_usage_reader(path: Path) -> str:
            if path == Path("/sys/fs/cgroup/cpu.stat"):
                return root_usage_only
            if path.name == "cpu.max":
                if path == Path("/sys/fs/cgroup/cpu.max"):
                    raise FileNotFoundError(path)
                return "200000 100000\n"
            if path.name == "cpu.stat":
                return stat
            if path.name == "cpuset.cpus.effective":
                return "0-3\n"
            raise FileNotFoundError(path)

        root_usage_snapshot = external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text="0::/job\n", mountinfo_text=mountinfo,
            namespace_identity=(1, 2), file_reader=root_usage_reader,
        )
        self.assertEqual(root_usage_snapshot["reasons"], [])
        self.assertEqual(root_usage_snapshot["ancestors"][-1]["cpu_stat_optional_status"], "not_applicable")
        self.assertEqual(set(root_usage_snapshot["ancestors"][-1]["cpu_stat"]), {"usage_usec", "user_usec", "system_usec"})

        def missing_leaf_throttle_reader(path: Path) -> str:
            if path == Path("/sys/fs/cgroup/job/cpu.stat"):
                return root_usage_only
            return root_usage_reader(path)

        missing_leaf_throttle = external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text="0::/job\n", mountinfo_text=mountinfo,
            namespace_identity=(1, 2), file_reader=missing_leaf_throttle_reader,
        )
        self.assertIn("cpu_stat_partial", missing_leaf_throttle["reasons"])

        duplicate_stat = "".join(
            f"{key} {value}\n"
            for key, value in (
                ("usage_usec", 10),
                ("usage_usec", 11),
                ("user_usec", 5),
                ("system_usec", 5),
                ("nr_periods", 4),
                ("nr_throttled", 1),
                ("throttled_usec", 2),
            )
        )
        duplicate = self._cgroup_diagnostic_snapshot(stat_override=duplicate_stat)
        self.assertIn("cpu_stat_invalid", duplicate["reasons"])
        self.assertIsNone(duplicate["ancestors"][0]["cpu_stat"])
        partial_optional = self._cgroup_diagnostic_snapshot(
            stat_override="usage_usec 10\nuser_usec 5\nsystem_usec 5\nnr_periods 1\n"
        )
        self.assertIn("cpu_stat_invalid", partial_optional["reasons"])
        self.assertIsNone(partial_optional["ancestors"][0]["cpu_stat"])

    def test_cgroup_diagnostic_visible_root_omission_stays_unknown(self) -> None:
        root = Path("/sys/fs/cgroup")
        def snapshot(sequence: int) -> dict[str, object]:
            def reader(path: Path) -> str:
                if path == root / "job/cpu.max":
                    return "250000 100000\n"
                if path.name == "cpu.max":
                    raise FileNotFoundError
                if path.parent == root:
                    values = {
                        "usage_usec": 100 + sequence * 12,
                        "user_usec": 50 + sequence * 5,
                        "system_usec": 50 + sequence * 7,
                    }
                else:
                    values = {
                        "usage_usec": 10 + sequence * 15,
                        "user_usec": 5 + sequence * 6,
                        "system_usec": 5 + sequence * 9,
                        "nr_periods": 1 + sequence,
                        "nr_throttled": sequence,
                        "throttled_usec": sequence * 3,
                    }
                return "".join(f"{key} {value}\n" for key, value in values.items())

            return external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text="36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
                namespace_identity=(1, 2),
                file_reader=reader,
            )

        report = external_load._cgroup_diagnostic_report(snapshot(0), snapshot(1))
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["quota_completeness"], "unknown")
        self.assertIn("cpu_max_unavailable", report["reasons"])
        self.assertIn("cpu_stat_partial", report["reasons"])
        self.assertEqual(report["visible_ancestor_count"], 2)
        child, visible_root = report["visible_ancestors"]
        self.assertEqual(child["cpu_max_kind"], "quota")
        self.assertEqual(child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertEqual(visible_root["cpu_max_kind"], "unavailable")
        self.assertEqual(visible_root["cpu_stat_completeness"], "partial")
        self.assertEqual(visible_root["cpu_stat_delta"]["usage_usec"], 12)
        self.assertNotIn("nr_periods", visible_root["cpu_stat_delta"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        child_before = stable_before["ancestors"][0]
        child_before["cpu_max"] = None
        stable_before["reasons"] = ["cpu_max_unavailable"]
        reverse_missing = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        reverse_child = reverse_missing["visible_ancestors"][0]
        self.assertEqual(reverse_child["cpu_max_kind"], "unavailable")
        self.assertEqual(reverse_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_unavailable", reverse_missing["reasons"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        stable_after["ancestors"][0]["cpu_max"] = None
        stable_after["reasons"] = ["cpu_max_unavailable"]
        forward_missing = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        forward_child = forward_missing["visible_ancestors"][0]
        self.assertEqual(forward_child["cpu_max_kind"], "unavailable")
        self.assertEqual(forward_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_unavailable", forward_missing["reasons"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        stable_after["ancestors"][0]["cpu_max"] = (300_000, 100_000)
        changed = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        changed_child = changed["visible_ancestors"][0]
        self.assertEqual(changed["status"], "incomplete")
        self.assertEqual(changed_child["cpu_max_kind"], "unavailable")
        self.assertNotIn("cpu_max_quota_us", changed_child)
        self.assertEqual(changed_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_changed", changed["reasons"])

    def test_cgroup_diagnostic_identity_changes_and_counter_resets_are_incomplete(self) -> None:
        before = self._cgroup_diagnostic_snapshot(usage_usec=100)
        variants = (
            (
                "namespace_identity_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, namespace_identity=(1, 3)),
            ),
            (
                "membership_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, membership="/tenant/other"),
            ),
            (
                "mount_identity_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, mount_id=37),
            ),
        )
        for reason, after in variants:
            with self.subTest(reason=reason):
                report = external_load._cgroup_diagnostic_report(before, after)
                self.assertEqual(report["status"], "incomplete")
                self.assertIn(reason, report["reasons"])
                self.assertEqual(report["quota_completeness"], "unknown")

        changed_quota = self._cgroup_diagnostic_snapshot(
            usage_usec=130,
            max_files={"/sys/fs/cgroup/job/cpu.max": "50000 100000\n"},
        )
        changed_quota_report = external_load._cgroup_diagnostic_report(before, changed_quota)
        self.assertEqual(changed_quota_report["status"], "incomplete")
        self.assertIn("cpu_max_changed", changed_quota_report["reasons"])

        reset = self._cgroup_diagnostic_snapshot(usage_usec=20)
        reset_report = external_load._cgroup_diagnostic_report(before, reset)
        self.assertEqual(reset_report["status"], "incomplete")
        self.assertIn("cpu_stat_reset", reset_report["reasons"])

    def test_process_cpu_sampler_is_bounded_and_reports_truncation(self) -> None:
        with patch.object(external_load.time, "process_time_ns", side_effect=OSError):
            unavailable = external_load._ProcessCpuSampler()
            unavailable.start()
            unavailable_report = unavailable.stop()
        self.assertEqual(unavailable_report["status"], "unavailable")
        self.assertEqual(unavailable_report["samples"], [])

        with (
            patch.object(external_load, "CLIENT_CPU_SAMPLE_LIMIT", 4),
        ):
            sampler = external_load._ProcessCpuSampler()
            sampler.start()
            time.sleep(1.15)
            report = sampler.stop()
        self.assertEqual(report["scope"], "load_worker_process")
        self.assertIn(report["status"], {"complete", "truncated"})
        self.assertGreaterEqual(report["sample_count"], 1)
        self.assertLessEqual(report["sample_count"], 4)
        self.assertEqual(report["sample_count"], len(report["samples"]))
        for row in report["samples"]:
            self.assertLess(row["interval_started_at"], row["timestamp"])
            self.assertGreaterEqual(row["interval_elapsed_ms"], 100)  # sampler is still monotonic
            self.assertGreaterEqual(row["process_cpu_percent"], 0)

        with (
            patch.object(external_load, "CLIENT_CPU_SAMPLE_INTERVAL_SECONDS", 0.005),
            patch.object(external_load, "CLIENT_CPU_SAMPLE_LIMIT", 1),
        ):
            capped = external_load._ProcessCpuSampler()
            capped.start()
            time.sleep(0.03)
            capped_report = capped.stop()
        self.assertEqual(capped_report["status"], "truncated")
        self.assertEqual(capped_report["sample_count"], 1)

    def test_run_load_stops_cpu_sampler_when_workload_raises(self) -> None:
        sampler = Mock()
        sampler.stop.return_value = {"status": "unavailable", "samples": [], "sample_count": 0}
        with (
            patch.object(external_load, "_ProcessCpuSampler", return_value=sampler),
            patch.object(external_load, "_run_load_impl", side_effect=ExternalLoadError("fixture")),
            self.assertRaises(ExternalLoadError),
        ):
            run_load()
        sampler.start.assert_called_once_with()
        sampler.stop.assert_called_once_with()

    def test_run_load_attaches_only_cpu_intervals_inside_its_window(self) -> None:
        sampler = Mock()
        sampler.stop.return_value = {
            "schema": 1,
            "status": "complete",
            "scope": "load_worker_process",
            "sample_interval_ms": 1000,
            "affinity_cpu_count": 2,
            "effective_cpu_capacity_cores": None,
            "cpu_capacity_status": "unknown",
            "sample_count": 3,
            "samples": [
                {
                    "interval_started_at": "2026-10-11T10:00:00Z",
                    "timestamp": "2026-10-11T10:00:01Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 20.0,
                },
                {
                    "interval_started_at": "2026-10-11T10:00:01Z",
                    "timestamp": "2026-10-11T10:00:02Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 25.0,
                },
                {
                    "interval_started_at": "2026-10-11T10:00:02Z",
                    "timestamp": "2026-10-11T10:00:03Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 30.0,
                },
            ],
        }
        report_source = {
            "started_at": "2026-10-11T10:00:00.5Z",
            "finished_at": "2026-10-11T10:00:02.5Z",
        }
        with (
            patch.object(external_load, "_ProcessCpuSampler", return_value=sampler),
            patch.object(external_load, "_run_load_impl", return_value=report_source),
        ):
            report = run_load()
        sampler.start.assert_called_once_with()
        sampler.stop.assert_called_once_with()
        usage = report["client_resource_usage"]
        self.assertEqual(usage["status"], "complete")
        self.assertEqual(usage["sample_count"], 1)
        self.assertEqual(usage["samples"][0]["timestamp"], "2026-10-11T10:00:02Z")

    def test_effective_cpu_set_parser_is_bounded_and_closed(self) -> None:
        self.assertEqual(external_load._parse_effective_cpu_set("0-3,6,8-9\n"), 7)
        for invalid in ("", "0-", "3-1", "0-1,1-2", "0,,2", "0-5000", "x"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(external_load._parse_effective_cpu_set(invalid))
        sampler = external_load.RunnerCgroupSampler(runner_environment="self-hosted")
        sampler.start(2, 1)
        result = sampler.stop(namespace_pid=2, namespace_starttime=1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "trusted_runner_context_unavailable")
        self.assertEqual(set(result["identity"]), set(sampler.IDENTITY_FIELDS))
        self.assertFalse(any(result["identity"].values()))

    def test_runner_cgroup_sampler_requires_and_aggregates_stable_host_binding(self) -> None:
        sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        sampler.SAMPLE_INTERVAL_SECONDS = 0.005
        membership = "0::/runner/load\n"
        mount_identity = (31, 17, "0:29", "/", "/sys/fs/cgroup", "rw")
        namespace_ids = {
            "/proc/self/ns/pid": (1, 1),
            "/proc/self/ns/mnt": (1, 2),
            "/proc/self/ns/cgroup": (1, 3),
            "/proc/4321/ns/pid": (1, 5), "/proc/4321/ns/mnt": (1, 4),
            "/proc/4321/ns/cgroup": (1, 3),
        }

        stat_result = external_load.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"1:1\n1:2\n1:3\n", stderr=b""
        )
        with patch("tools.platform_external_load.subprocess.run", return_value=stat_result) as run:
            proc1_ids = external_load._read_proc1_namespace_identities()
        self.assertEqual(proc1_ids, {"pid": (1, 1), "mount": (1, 2), "cgroup": (1, 3)})
        command = run.call_args.args[0]
        self.assertEqual(command[:10], [
            "/usr/bin/sudo", "-n", "/usr/bin/timeout", "--signal=TERM",
            "--kill-after=0.2s", "1s", "/usr/bin/stat", "--dereference",
            "--printf=%d:%i\\n", "--",
        ])
        self.assertEqual(command[10:], [
            "/proc/1/ns/pid", "/proc/1/ns/mnt", "/proc/1/ns/cgroup",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)
        self.assertIs(run.call_args.kwargs["stdin"], external_load.subprocess.DEVNULL)

        pidfd_read, pidfd_write = os.pipe()
        try:
            self.assertEqual(external_load.RunnerCgroupSampler._pidfd_state(pidfd_read), "live")

            class FakePoll:
                def __init__(self, events: object) -> None:
                    self.events = events

                def register(self, _fd: int, _events: int) -> None:
                    return None

                def poll(self, _timeout: int) -> object:
                    return self.events

            for poll_events, expected_state in (
                ([(pidfd_read, external_load.select.POLLHUP)], "exited"),
                ([(pidfd_read, external_load.select.POLLIN | external_load.select.POLLHUP)], "exited"),
                ([(pidfd_read, external_load.select.POLLIN | external_load.select.POLLERR)], "unknown"),
                ([(pidfd_read, external_load.select.POLLHUP | external_load.select.POLLERR)], "unknown"),
                ([(pidfd_read, external_load.select.POLLNVAL)], "unknown"),
                ([(pidfd_read + 1, external_load.select.POLLHUP)], "unknown"),
                ([(pidfd_read, external_load.select.POLLHUP), (pidfd_read, external_load.select.POLLHUP)], "unknown"),
                ([pidfd_read], "unknown"),
            ):
                with self.subTest(poll_events=poll_events):
                    with patch(
                        "tools.platform_external_load.select.poll",
                        return_value=FakePoll(poll_events),
                    ):
                        self.assertEqual(
                            external_load.RunnerCgroupSampler._pidfd_state(pidfd_read),
                            expected_state,
                        )
            os.write(pidfd_write, b"x")
            self.assertEqual(external_load.RunnerCgroupSampler._pidfd_state(pidfd_read), "exited")
            with patch("tools.platform_external_load.select.poll", side_effect=OSError("poll")):
                self.assertEqual(external_load.RunnerCgroupSampler._pidfd_state(pidfd_read), "unknown")
            os.close(pidfd_read)
            pidfd_read = -1
            self.assertEqual(external_load.RunnerCgroupSampler._pidfd_state(pidfd_read), "unknown")
        finally:
            if pidfd_read >= 0:
                os.close(pidfd_read)
            os.close(pidfd_write)

        invalid_stat_results = (
            external_load.subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b""),
            external_load.subprocess.CompletedProcess(args=[], returncode=0, stdout=b"bad\n", stderr=b""),
            external_load.subprocess.CompletedProcess(args=[], returncode=0, stdout=b"1:1\n1:2\n1:3\n4:4\n", stderr=b""),
        )
        for invalid_result in invalid_stat_results:
            with self.subTest(returncode=invalid_result.returncode, stdout=invalid_result.stdout):
                with patch("tools.platform_external_load.subprocess.run", return_value=invalid_result):
                    with self.assertRaises(external_load._CgroupDiagnosticError) as raised:
                        external_load._read_proc1_namespace_identities()
                self.assertEqual(raised.exception.reason, "runner_snapshot_unavailable")
        with patch(
            "tools.platform_external_load.subprocess.run",
            side_effect=external_load.subprocess.TimeoutExpired(cmd="fixed-stat", timeout=3.0),
        ):
            with self.assertRaises(external_load._CgroupDiagnosticError) as raised:
                external_load._read_proc1_namespace_identities()
        self.assertEqual(raised.exception.reason, "runner_snapshot_unavailable")

        mapping = {
            "root": Path("/"), "mountpoint": Path("/sys/fs/cgroup"),
            "mount_identity": mount_identity,
        }

        def common_patches(
            target_sampler: external_load.RunnerCgroupSampler,
            target_pidfd_read: int,
            snapshot_fn: object,
            proc1_reader: object = lambda: proc1_ids,
        ) -> ExitStack:
            stack = ExitStack()
            stack.enter_context(patch.object(
                target_sampler, "_namespace_identity", side_effect=lambda path: namespace_ids[path],
            ))
            stack.enter_context(patch.object(target_sampler, "_target_is_stable", return_value=True))
            stack.enter_context(patch(
                "tools.platform_external_load.os.pidfd_open",
                side_effect=lambda *_args: os.dup(target_pidfd_read),
            ))
            stack.enter_context(patch(
                "tools.platform_external_load._read_proc1_namespace_identities",
                side_effect=proc1_reader,
            ))
            stack.enter_context(patch(
                "tools.platform_external_load._read_bounded_ascii", return_value=membership,
            ))
            stack.enter_context(patch(
                "tools.platform_external_load._bounded_proc_text", return_value="mountinfo",
            ))
            stack.enter_context(patch(
                "tools.platform_external_load._cgroup2_membership_mapping", return_value=mapping,
            ))
            stack.enter_context(patch(
                "tools.platform_external_load._read_cgroup_diagnostic_snapshot",
                side_effect=snapshot_fn,
            ))
            stack.enter_context(patch(
                "tools.platform_load_runtime._read_process_starttime", return_value=77,
            ))
            return stack

        denied_sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        denied_pipe_read, denied_pipe_write = os.pipe()
        with common_patches(
            denied_sampler, denied_pipe_read, lambda **_kwargs: {},
            proc1_reader=lambda: (_ for _ in ()).throw(
                external_load._CgroupDiagnosticError("runner_snapshot_unavailable")
            ),
        ):
            denied_sampler.start(4321, 77)
            denied = denied_sampler.stop()
        os.close(denied_pipe_read)
        os.close(denied_pipe_write)
        self.assertEqual(denied["status"], "unknown")
        self.assertEqual(denied["reason"], "runner_snapshot_unavailable")
        self.assertEqual(denied["sample_count"], 0)
        self.assertFalse(any(denied["identity"].values()))
        reads = [0]

        def snapshot(**_kwargs: object) -> dict[str, object]:
            reads[0] += 1
            value = reads[0] * 10
            return {
                "membership": "/runner/load", "mount_identity": mount_identity,
                "namespace_identity": (1, 3), "mount_root_is_global": True,
                "reasons": [],
                "ancestors": [
                    {
                        "path": Path("/sys/fs/cgroup/runner/load"),
                        "cpu_max": (200_000, 100_000), "cpuset_count": 4,
                        "cpu_stat_optional_status": "available",
                        "cpu_stat": {
                            "usage_usec": value, "user_usec": value // 2,
                            "system_usec": value // 2, "nr_periods": value // 1000,
                            "nr_throttled": 0, "throttled_usec": 0,
                        },
                    },
                    {
                        "path": Path("/sys/fs/cgroup"),
                        "cpu_max": (None, 100_000), "cpuset_count": 4,
                        "cpu_stat_optional_status": "not_applicable",
                        "cpu_stat": {
                            "usage_usec": value, "user_usec": value // 2,
                            "system_usec": value // 2,
                        },
                    },
                ],
            }

        sampler_pipe_read, sampler_pipe_write = os.pipe()
        with common_patches(sampler, sampler_pipe_read, snapshot):
            sampler.start(4321, 77)
            time.sleep(0.13)
            sampler_owned_pidfd = sampler._pidfd
            result = sampler.stop()
        os.close(sampler_pipe_read)
        os.close(sampler_pipe_write)
        self.assertIsNone(sampler._pidfd)
        self.assertIsNotNone(sampler_owned_pidfd)
        with self.assertRaises(OSError):
            os.fstat(sampler_owned_pidfd)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["reason"], "none")
        self.assertGreaterEqual(result["sample_count"], 3)
        self.assertEqual(result["effective_cpu_capacity_cores"], 2.0)
        self.assertGreater(result["window_seconds"], 0)
        self.assertEqual(result["max_visible_throttled_periods_delta"], 0)
        self.assertEqual(result["identity"]["global_cgroup2_mount"], True)

        changed_proc1_ids = {**proc1_ids, "pid": (1, 99)}
        changed_sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        changed_sampler.SAMPLE_INTERVAL_SECONDS = 0.005
        changed_proc1_reads = [0]

        def changed_proc1_reader() -> dict[str, tuple[int, int]]:
            changed_proc1_reads[0] += 1
            return proc1_ids if changed_proc1_reads[0] == 1 else changed_proc1_ids

        changed_pipe_read, changed_pipe_write = os.pipe()
        with common_patches(
            changed_sampler, changed_pipe_read, snapshot,
            proc1_reader=changed_proc1_reader,
        ):
            changed_sampler.start(4321, 77)
            time.sleep(0.02)
            changed_identity = changed_sampler.stop()
        os.close(changed_pipe_read)
        os.close(changed_pipe_write)
        self.assertEqual(changed_identity["status"], "unknown")
        self.assertEqual(changed_identity["reason"], "runner_snapshot_invalid")
        self.assertEqual(changed_identity["sample_count"], 1)

        reused_sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        reuse_read, reuse_write = os.pipe()
        reuse_pidfds: list[int] = []

        def reused_pidfd(*_args: object) -> int:
            value = os.dup(reuse_read)
            reuse_pidfds.append(value)
            return value

        with ExitStack() as stack:
            stack.enter_context(patch(
                "tools.platform_external_load.os.pidfd_open", side_effect=reused_pidfd,
            ))
            stack.enter_context(patch(
                "tools.platform_load_runtime._read_process_starttime", side_effect=[77, 78],
            ))
            reused_sampler.start(4321, 77)
            reused = reused_sampler.stop()
        os.close(reuse_read)
        os.close(reuse_write)
        self.assertEqual(reused["status"], "unknown")
        self.assertEqual(reused["reason"], "workload_identity_changed")
        self.assertEqual(len(reuse_pidfds), 1)
        with self.assertRaises(OSError):
            os.fstat(reuse_pidfds[0])

        exit_sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        exit_sampler.SAMPLE_INTERVAL_SECONDS = 10.0
        exit_reads = [0]

        def exit_snapshot(**kwargs: object) -> dict[str, object]:
            exit_reads[0] += 1
            return snapshot(**kwargs)

        exit_pipe_read, exit_pipe_write = os.pipe()
        with common_patches(exit_sampler, exit_pipe_read, exit_snapshot):
            exit_sampler.start(4321, 77)
            os.write(exit_pipe_write, b"x")
            exited = exit_sampler.stop()
        os.close(exit_pipe_read)
        os.close(exit_pipe_write)
        self.assertEqual(exited["status"], "unknown")
        self.assertEqual(exited["reason"], "runner_samples_incomplete")
        self.assertEqual(exited["sample_count"], 1)
        self.assertEqual(exit_reads[0], 1, "confirmed exit must discard the final counter snapshot")

        raced_sampler = external_load.RunnerCgroupSampler(runner_environment="github-hosted")
        raced_sampler.SAMPLE_INTERVAL_SECONDS = 0.001
        raced_reads = [0]
        raced_snapshot_done = threading.Event()
        raced_pipe_read, raced_pipe_write = os.pipe()

        def race_snapshot(**kwargs: object) -> dict[str, object]:
            raced_reads[0] += 1
            value = snapshot(**kwargs)
            if raced_reads[0] == 2:
                os.write(raced_pipe_write, b"x")
                raced_snapshot_done.set()
            return value

        with common_patches(raced_sampler, raced_pipe_read, race_snapshot):
            raced_sampler.start(4321, 77)
            self.assertTrue(raced_snapshot_done.wait(1.0))
            raced = raced_sampler.stop()
        os.close(raced_pipe_read)
        os.close(raced_pipe_write)
        self.assertEqual(raced["status"], "unknown")
        self.assertEqual(raced["reason"], "runner_samples_incomplete")
        self.assertEqual(raced["sample_count"], 1)
        self.assertEqual(raced_reads[0], 2, "sample racing with exit must not be retained")

        started_at = "2026-10-18T10:00:00.000Z"
        finished_at = "2026-10-18T10:00:00.110Z"
        timing = {
            "expected_count": 10, "submitted_count": 10, "started_count": 10,
            "completed_count": 10, "scheduled_count": 10,
            "response_completion_count": 10, "user_observed_count": 10,
            "dropped_work": 0, "partial": False, "late_start_count": 10,
            "executor_queue_wait": {
                "count": 10, "avg_ms": 0.5, "p50_ms": 0.5,
                "p95_ms": 0.5, "p99_ms": 0.8, "max_ms": 0.8,
            },
        }
        usage = {
            "schema": 1, "status": "complete", "scope": "load_worker_process",
            "sample_interval_ms": 1000, "cpu_capacity_status": "unknown",
            "cpu_capacity_reason": "trusted_host_root_unproven", "sample_count": 1,
            "samples": [{
                "interval_started_at": "2026-10-18T10:00:00.001Z",
                "timestamp": "2026-10-18T10:00:00.101Z",
                "interval_elapsed_ms": 100.0, "process_cpu_percent": 20.0,
            }],
            "cgroup_diagnostics": {
                "schema": 1, "status": "complete", "visible_evidence_status": "complete",
                "quota_completeness": "unknown", "visible_ancestor_count": 1,
                "namespace_identity_stable": True, "membership_stable": True,
                "mount_identity_stable": True, "visible_mount_root_is_global": False,
                "visible_ancestors": [{
                    "cpu_max_kind": "max", "cpu_max_period_us": 100000,
                    "cpu_stat_completeness": "complete",
                    "cpu_stat_delta": {
                        "usage_usec": 10, "user_usec": 5, "system_usec": 5,
                        "nr_periods": 1, "nr_throttled": 0, "throttled_usec": 0,
                    },
                }], "reasons": ["trusted_host_root_unproven"],
            },
        }
        report = {
            "schema": 1, "measurement_schema": 2, "mode": "read-mix",
            "arrival_model": "closed_loop_concurrency_ramp", "scenario_kind": "stress",
            "concurrency_stages": [8], "wall_seconds": 0.105,
            "started_at": started_at, "finished_at": finished_at,
            "logical": {"timing": timing}, "client_resource_usage": usage,
            "runner_cgroup_usage": result,
        }
        report["capacity_attribution"] = external_load._capacity_attribution(
            report, usage, result
        )
        sanitized = project_public_artifact("external_load", report)
        self.assertEqual(sanitized["runner_cgroup_usage"]["status"], "complete")
        self.assertEqual(sanitized["capacity_attribution"]["status"], "eligible", {
            "attribution": sanitized["capacity_attribution"],
            "runner": sanitized.get("runner_cgroup_usage"),
            "client": sanitized.get("client_resource_usage"),
        })
        self.assertIsNone(sanitized["capacity_attribution"]["server_capacity_claim"])
        self.assertEqual(sanitized["capacity_attribution"]["capacity_scope"], "generator_headroom_only")
        changed_identity = sampler.stop(namespace_pid=4322, namespace_starttime=77)
        self.assertEqual(changed_identity["status"], "unknown")
        self.assertEqual(changed_identity["reason"], "workload_identity_changed")

    def test_capacity_attribution_stays_unknown_without_trusted_effective_quota(self) -> None:
        report = {
            "mode": "read-mix", "scenario_kind": "stress",
            "arrival_model": "closed_loop_concurrency_ramp", "concurrency_stages": [16, 32],
            "acceptance": {"decision": "STRESS BEHAVIOR FAIL"},
            "logical": {"timing": {
                "expected_count": 100, "submitted_count": 100, "started_count": 100,
                "completed_count": 100, "scheduled_count": 100,
                "response_completion_count": 100, "user_observed_count": 100,
                "dropped_work": 0, "partial": False, "late_start_count": 0,
                "executor_queue_wait": {"count": 100, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0},
                "schedule_delay": {"p95_ms": 0.0, "p99_ms": 0.0},
            }},
        }
        sampler = external_load._ProcessCpuSampler()
        sampler.start()
        time.sleep(external_load.CLIENT_CPU_SAMPLE_INTERVAL_SECONDS + 0.05)
        usage = sampler.stop()
        result = external_load._capacity_attribution(report, usage)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["server_capacity_claim"])
        self.assertEqual(result["reason"], "trusted_runner_evidence_missing")
        self.assertEqual(report["acceptance"]["decision"], "STRESS BEHAVIOR FAIL")

        # Open-loop scheduling and slow SUT status do not convert missing
        # generator-quota proof into a server-capacity claim.
        report["mode"] = "ready-vote"
        report["scenario_kind"] = "capacity"
        report["arrival_model"] = "open_loop_scheduled_rate"
        open_loop = external_load._capacity_attribution(report, usage)
        self.assertEqual(open_loop["status"], "unknown")
        self.assertEqual(open_loop["reason"], "trusted_runner_evidence_missing")
        self.assertIsNone(open_loop["server_capacity_claim"])

    def test_capacity_attribution_excludes_generator_limits_and_keeps_unknown_quota_unknown(self) -> None:
        report = {
            "mode": "read-mix", "scenario_kind": "stress",
            "arrival_model": "closed_loop_concurrency_ramp", "concurrency_stages": [16],
            "logical": {"timing": {
                "expected_count": 10, "submitted_count": 10, "started_count": 10,
                "completed_count": 10, "scheduled_count": 10,
                "response_completion_count": 10, "user_observed_count": 10,
                "dropped_work": 0, "partial": False, "late_start_count": 0,
                "executor_queue_wait": {"count": 10, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0},
            }},
        }
        usage = {
            "status": "complete", "cpu_capacity_status": "known", "cpu_capacity_reason": "known",
            "effective_cpu_capacity_cores": 2.0, "samples": [{"process_cpu_percent": 50.0}],
            "cgroup_diagnostics": {
                "status": "complete", "quota_completeness": "complete",
                "visible_ancestors": [{"cpu_stat_completeness": "complete", "cpu_stat_delta": {"nr_throttled": 0}}],
            },
        }
        report["logical"]["timing"]["executor_queue_wait"]["p95_ms"] = 2.1
        report["logical"]["timing"]["executor_queue_wait"]["p99_ms"] = 2.1
        report["logical"]["timing"]["executor_queue_wait"]["max_ms"] = 2.1
        queue_limited = external_load._capacity_attribution(report, usage)
        self.assertEqual((queue_limited["status"], queue_limited["reason"]), ("ineligible", "client_queue_lag"))
        report["logical"]["timing"]["executor_queue_wait"]["p95_ms"] = 0.0
        report["logical"]["timing"]["executor_queue_wait"]["p99_ms"] = 0.0
        report["logical"]["timing"]["executor_queue_wait"]["max_ms"] = 0.0
        report["logical"]["timing"]["completed_count"] = 9
        underdelivered = external_load._capacity_attribution(report, usage)
        self.assertEqual((underdelivered["status"], underdelivered["reason"]), ("ineligible", "work_under_delivered"))
        report["logical"]["timing"]["completed_count"] = 10
        usage["cgroup_diagnostics"]["visible_ancestors"][0]["cpu_stat_delta"]["nr_throttled"] = 1
        throttled = external_load._capacity_attribution(report, usage)
        self.assertEqual((throttled["status"], throttled["reason"]), ("ineligible", "generator_quota_throttled"))
        usage["cgroup_diagnostics"]["visible_ancestors"][0]["cpu_stat_delta"]["nr_throttled"] = 0
        usage["samples"][0]["process_cpu_percent"] = 200.0
        saturated = external_load._capacity_attribution(report, usage)
        self.assertEqual((saturated["status"], saturated["reason"]), ("unknown", "trusted_runner_evidence_missing"))
        usage["samples"][0]["process_cpu_percent"] = 50.0
        usage["cgroup_diagnostics"]["visible_ancestors"] = []
        incomplete_hierarchy = external_load._capacity_attribution(report, usage)
        self.assertEqual((incomplete_hierarchy["status"], incomplete_hierarchy["reason"]), ("unknown", "trusted_runner_evidence_missing"))
        usage["cgroup_diagnostics"]["visible_ancestors"] = [{
            "cpu_stat_completeness": "incomplete", "cpu_stat_delta": {"nr_throttled": 0},
        }]
        malformed_hierarchy = external_load._capacity_attribution(report, usage)
        self.assertEqual((malformed_hierarchy["status"], malformed_hierarchy["reason"]), ("unknown", "trusted_runner_evidence_missing"))
        usage["cgroup_diagnostics"]["visible_ancestors"] = [{
            "cpu_stat_completeness": "complete", "cpu_stat_delta": {"nr_throttled": 0},
        }]
        usage["cpu_capacity_status"] = "unknown"
        unknown = external_load._capacity_attribution(report, usage)
        self.assertEqual((unknown["status"], unknown["reason"]), ("unknown", "trusted_runner_evidence_missing"))
        self.assertIsNone(unknown["server_capacity_claim"])

    def test_real_closed_loop_producer_positive_jitter_does_not_block_headroom(self) -> None:
        users = [VirtualUser(f"fast-{index}", "synthetic", "s" * 32, "c" * 32) for index in range(8)]

        def immediate(_origin: str, _user: VirtualUser, phase: str, _timeout: float) -> RequestResult:
            started = time.monotonic()
            return RequestResult(
                phase, "GET", "/synthetic", 200, 0.1, True, 0,
                started_at_monotonic=started, finished_at_monotonic=started + 0.0001,
            )

        results = run_phase(
            "https://example.invalid", users, phase="capacity-jitter",
            spread_seconds=0, concurrency=8, timeout=1, request_builder=immediate,
        )
        accumulator = external_load._TimingAccumulator()
        for result in results:
            accumulator.add(result)
        timing = accumulator.summary(unit="actions", expected_count=8, submitted_count=8)
        self.assertEqual(timing["late_start_count"], 8)
        self.assertGreater(timing["executor_queue_wait"]["p95_ms"], 0)
        self.assertLessEqual(
            timing["executor_queue_wait"]["p95_ms"],
            external_load.CAPACITY_ATTRIBUTION_LAG_FLOOR_MS,
        )
        self.assertLessEqual(
            timing["executor_queue_wait"]["p99_ms"],
            external_load.CAPACITY_ATTRIBUTION_LAG_FLOOR_MS,
        )
        report = {
            "mode": "read-mix", "scenario_kind": "stress",
            "arrival_model": "closed_loop_concurrency_ramp", "concurrency_stages": [8],
            "wall_seconds": 1.0, "logical": {"timing": timing},
        }
        usage = {
            "status": "complete", "cpu_capacity_status": "unknown",
            "samples": [{"process_cpu_percent": 20.0}],
        }
        identity = {
            "outer_pid_namespace_matches_pid1": True,
            "outer_mount_namespace_matches_pid1": True,
            "outer_cgroup_namespace_matches_pid1": True,
            "target_cgroup_namespace_matches_outer": True,
            "target_mount_namespace_is_isolated": True,
            "target_pid_namespace_is_isolated": True,
            "global_cgroup2_mount": True,
            "workload_membership_matches_outer": True,
        }
        runner = {
            "status": "complete", "reason": "none", "runner_environment": "github-hosted",
            "identity": identity, "effective_cpu_capacity_cores": 2.0,
            "max_cgroup_cpu_usage_cores": 0.2, "max_visible_throttled_periods_delta": 0,
            "window_seconds": 2.0,
        }
        attribution = external_load._capacity_attribution(report, usage, runner)
        self.assertEqual(attribution["status"], "eligible")
        self.assertEqual(attribution["reason"], "headroom_observed")
        self.assertIsNone(attribution["schedule_delay_p95_ms"])

    def test_real_open_loop_pacing_uses_material_schedule_lag_floor(self) -> None:
        users = [VirtualUser(f"paced-{index}", "synthetic", "s" * 32, "c" * 32) for index in range(40)]

        def immediate(_origin: str, _user: VirtualUser, phase: str, _timeout: float) -> RequestResult:
            started = time.monotonic()
            return RequestResult(
                phase, "GET", "/synthetic", 200, 0.1, True, 0,
                started_at_monotonic=started, finished_at_monotonic=started + 0.0001,
            )

        results, _window = run_rate_phase(
            "https://example.invalid", users, phase="capacity-paced",
            duration_seconds=1.0, concurrency=8, timeout=1, request_builder=immediate,
        )
        accumulator = external_load._TimingAccumulator()
        for result in results:
            accumulator.add(result)
        timing = accumulator.summary(unit="actions", expected_count=40, submitted_count=40)
        self.assertEqual(timing["late_start_count"], 40)
        self.assertGreater(timing["schedule_delay"]["p95_ms"], 0)
        self.assertLessEqual(timing["schedule_delay"]["p95_ms"], external_load.CAPACITY_ATTRIBUTION_LAG_FLOOR_MS)
        report = {
            "mode": "ready-vote", "scenario_kind": "capacity",
            "arrival_model": "open_loop_scheduled_rate", "wall_seconds": 1.0,
            "logical": {"timing": timing},
        }
        usage = {"status": "complete", "cpu_capacity_status": "unknown", "samples": [{"process_cpu_percent": 20.0}]}
        identity = {
            "outer_pid_namespace_matches_pid1": True,
            "outer_mount_namespace_matches_pid1": True,
            "outer_cgroup_namespace_matches_pid1": True,
            "target_cgroup_namespace_matches_outer": True,
            "target_mount_namespace_is_isolated": True,
            "target_pid_namespace_is_isolated": True,
            "global_cgroup2_mount": True,
            "workload_membership_matches_outer": True,
        }
        runner = {
            "status": "complete", "reason": "none", "runner_environment": "github-hosted",
            "identity": identity, "effective_cpu_capacity_cores": 2.0,
            "max_cgroup_cpu_usage_cores": 0.2, "max_visible_throttled_periods_delta": 0,
            "window_seconds": 2.0,
        }
        attribution = external_load._capacity_attribution(report, usage, runner)
        self.assertEqual(attribution["status"], "eligible")
        self.assertEqual(attribution["schedule_delay_p95_ms"], timing["schedule_delay"]["p95_ms"])

        report["logical"]["timing"]["schedule_delay"]["p95_ms"] = (
            external_load.CAPACITY_ATTRIBUTION_LAG_FLOOR_MS + 0.001
        )
        report["logical"]["timing"]["schedule_delay"]["p99_ms"] = (
            external_load.CAPACITY_ATTRIBUTION_LAG_FLOOR_MS + 0.001
        )
        material_delay = external_load._capacity_attribution(report, usage, runner)
        self.assertEqual((material_delay["status"], material_delay["reason"]), ("ineligible", "client_queue_lag"))

    def test_progress_callback_reports_only_completed_status_aggregates(self) -> None:
        users = [
            VirtualUser(f"progress-{index}", "synthetic", "s" * 32, "c" * 32)
            for index in range(2)
        ]
        callbacks: list[tuple[str, object | None]] = []

        def build(_origin: str, user: VirtualUser, phase: str, _timeout: float):
            first = RequestResult(phase, "GET", "/private", 200, 1.0, True, 0)
            if user.user_id.endswith("0"):
                return first
            retry = RequestResult(phase, "GET", "/private", 503, 1.0, False, 0)
            final = RequestResult(phase, "GET", "/private", 200, 1.0, True, 0)
            return LogicalRequestResult([retry, final], elapsed_ms=2.0)

        run_phase(
            "https://old-sparky.com",
            users,
            phase="synthetic-progress",
            spread_seconds=0,
            concurrency=2,
            timeout=1,
            request_builder=build,
            progress_callback=lambda phase, result: callbacks.append((phase, result)),
        )

        self.assertEqual(callbacks[0], ("http", None))
        completed = [result for phase, result in callbacks if phase == "http" and result is not None]
        self.assertEqual(len(completed), 2)
        self.assertEqual([len(getattr(item, "attempts", [item])) for item in completed], [1, 2])
        self.assertTrue(all(isinstance(item, (RequestResult, LogicalRequestResult)) for item in completed))
        # A broken diagnostic sink cannot change or stop measured requests.
        def fail_progress(_phase: str, _result: object | None) -> None:
            raise OSError("diagnostic-only failure")

        returned = run_phase(
            "https://old-sparky.com",
            users[:1],
            phase="synthetic-progress-failure",
            spread_seconds=0,
            concurrency=1,
            timeout=1,
            request_builder=build,
            progress_callback=fail_progress,
        )
        self.assertEqual(len(returned), 1)

        streamed = LogicalRequestResult(
            [
                RequestResult("stream", "GET", "/private", 503, 1.0, False, 1),
                RequestResult("stream", "GET", "/private", 200, 1.0, True, 1),
            ],
            elapsed_ms=2.0,
        )
        observed_statuses: list[list[int]] = []
        consumed: list[LogicalRequestResult] = []

        def capture_progress(phase: str, result: object | None) -> None:
            if phase == "http" and result is not None:
                observed_statuses.append([attempt.status for attempt in result.attempts])

        run_phase(
            "https://old-sparky.com",
            users[:1],
            phase="streamed-progress",
            spread_seconds=0,
            concurrency=1,
            timeout=1,
            request_builder=lambda *_args: streamed,
            result_consumer=consumed.append,
            progress_callback=capture_progress,
        )
        self.assertEqual(observed_statuses, [[503, 200]])
        self.assertEqual(consumed, [streamed])
        self.assertEqual(streamed.attempts, [])

        streamed_rate = LogicalRequestResult(
            [
                RequestResult("rate", "GET", "/private", 429, 1.0, False, 0),
                RequestResult("rate", "GET", "/private", 200, 1.0, True, 0),
            ],
            elapsed_ms=2.0,
        )
        rate_statuses: list[list[int]] = []

        def rate_progress(phase: str, result: object | None) -> None:
            if phase == "http" and result is not None:
                rate_statuses.append([attempt.status for attempt in result.attempts])

        run_rate_phase(
            "https://old-sparky.com",
            users[:1],
            phase="streamed-rate-progress",
            duration_seconds=0.1,
            concurrency=1,
            timeout=1,
            request_builder=lambda *_args: streamed_rate,
            result_consumer=lambda _result: None,
            progress_callback=rate_progress,
        )
        self.assertEqual(rate_statuses, [[429, 200]])
        self.assertEqual(streamed_rate.attempts, [])

    def test_body_read_transport_errors_are_counted_without_stopping_phase(self) -> None:
        users = [
            VirtualUser(f"read-{index:08d}", "synthetic", "s" * 64, "c" * 64)
            for index in range(5)
        ]
        calls = 0

        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __init__(self, partial_failure: bool) -> None:
                self.partial_failure = partial_failure
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                self.reads += 1
                if self.partial_failure:
                    if self.reads == 1:
                        return b"{"
                    if self.reads == 2:
                        raise http.client.IncompleteRead(b"private-body", 8)
                    raise AssertionError("failed response was read again")
                return (b"{" if self.reads == 1 else b"}" if self.reads == 2 else b"")

        def open_response(*_args: object, **_kwargs: object) -> Response:
            nonlocal calls
            calls += 1
            return Response(partial_failure=calls == 1)

        accumulator = _ResultAccumulator()

        def build_request(origin: str, user: VirtualUser, phase: str, timeout: float):
            return _request(
                origin,
                user,
                method="GET",
                path="/api/v1/users/me",
                phase=phase,
                timeout=timeout,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
                attempt_number=1,
            )

        with patch("tools.platform_external_load.urlopen", side_effect=open_response):
            run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic-transport",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                request_builder=build_request,
                result_consumer=accumulator.add,
            )

        summary = accumulator.summary()
        self.assertEqual(calls, len(users))
        self.assertEqual(summary["requests"], len(users))
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["status_counts"], {"200": len(users)})
        self.assertEqual(summary["retry_attempts"], 0)
        self.assertEqual(summary["error_kinds"], {"transport": 1})
        self.assertNotIn("private-body", repr(summary))

        class BrokenResponse:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                raise AssertionError("programming defect")

        with patch("tools.platform_external_load.urlopen", return_value=BrokenResponse()):
            with self.assertRaisesRegex(AssertionError, "programming defect"):
                _request(
                    "https://old-sparky.com",
                    users[0],
                    method="GET",
                    path="/users/me",
                    phase="synthetic-programming-error",
                    timeout=1,
                    session_cookie_name="session",
                    csrf_cookie_name="csrf",
                )

    def test_http_error_body_read_transport_failure_is_bounded(self) -> None:
        class PartialErrorBody:
            def read(self, _size: int) -> bytes:
                raise http.client.IncompleteRead(b"private-error-body", 32)

            def close(self) -> None:
                return None

        error = HTTPError(
            "https://old-sparky.com/api/v1/users/me",
            503,
            "service unavailable",
            {},
            PartialErrorBody(),
        )
        user = VirtualUser("read-error", "synthetic", "s" * 64, "c" * 64)
        with patch("tools.platform_external_load.urlopen", side_effect=error):
            result = _request(
                "https://old-sparky.com",
                user,
                method="GET",
                path="/users/me",
                phase="synthetic-http-error",
                timeout=1,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error_kind, "transport_http_response_read")
        self.assertEqual(result.response_bytes, len(b"private-error-body"))
        self.assertIsNone(result.response_json)
        self.assertNotIn("private-error-body", repr(result))

    def test_streaming_accumulator_preserves_high_cardinality_counts_and_percentiles(self) -> None:
        accumulator = _ResultAccumulator()
        latencies = [float((index * 37) % 1000) for index in range(4096)]
        for index, elapsed_ms in enumerate(latencies):
            accumulator.add(
                RequestResult(
                    phase="synthetic",
                    method="GET",
                    path="/health",
                    status=200 if index % 17 else 503,
                    elapsed_ms=elapsed_ms,
                    ok=index % 17 != 0,
                    response_bytes=index,
                )
            )

        summary = accumulator.summary()

        self.assertEqual(summary["requests"], 4096)
        self.assertEqual(summary["errors"], 241)
        self.assertEqual(summary["status_counts"], {"200": 3855, "503": 241})
        self.assertEqual(
            summary["latency"]["p95_ms"],
            round(percentile(latencies, 95) or 0, 3),
        )
        self.assertEqual(summary["response_bytes"]["max_bytes"], 4095)

    def test_sliding_phase_bounds_live_work_and_reduces_each_completion(self) -> None:
        users = [
            VirtualUser(f"user-{index:08d}", "qa-tournament", "s" * 64, "c" * 64)
            for index in range(96)
        ]
        active = 0
        peak_active = 0
        completed = 0
        last_result: RequestResult | None = None
        lock = threading.Lock()

        def builder(
            _origin: str,
            user: VirtualUser,
            phase: str,
            _timeout: float,
        ) -> RequestResult:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.001)
            with lock:
                active -= 1
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/users/{user.user_id}",
                status=200,
                elapsed_ms=1.0,
                ok=True,
                response_bytes=1,
                response_json={"large": "x" * 4096},
            )

        def consume(result: RequestResult) -> None:
            nonlocal completed, last_result
            self.assertIsNotNone(result.response_json)
            completed += 1
            last_result = result

        class TrackingExecutor(ThreadPoolExecutor):
            last: "TrackingExecutor | None" = None

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pending = set()
                self.peak_pending = 0
                self.submitted = 0
                type(self).last = self

            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                self.submitted += 1
                self.pending.add(future)
                self.peak_pending = max(self.peak_pending, len(self.pending))

                def discard(completed_future) -> None:
                    self.pending.discard(completed_future)

                future.add_done_callback(discard)
                return future

        before = {
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("external-load")
        }
        with patch(
            "tools.platform_external_load.ThreadPoolExecutor", TrackingExecutor
        ):
            returned = run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic",
                spread_seconds=0,
                concurrency=4,
                timeout=1,
                request_builder=builder,
                result_consumer=consume,
            )

        self.assertEqual(returned, [])
        self.assertEqual(completed, len(users))
        self.assertLessEqual(peak_active, 4)
        self.assertEqual(TrackingExecutor.last.submitted, len(users))
        self.assertLessEqual(TrackingExecutor.last.peak_pending, 4)
        self.assertEqual(TrackingExecutor.last.pending, set())
        self.assertIsNotNone(last_result)
        self.assertIsNone(last_result.response_json)
        self.assertEqual(active, 0)
        self.assertEqual(
            {
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith("external-load")
            },
            before,
        )

    def test_sliding_phase_cancels_pending_work_on_deadline_signal(self) -> None:
        users = [
            VirtualUser(f"user-{index:08d}", "qa-tournament", "s" * 64, "c" * 64)
            for index in range(8)
        ]
        started = threading.Event()

        class DeadlineBudget:
            def check(self, _phase: str, *, operation: str) -> None:
                if operation == "future_complete":
                    raise RuntimeError("synthetic deadline")

            def remaining_seconds(self) -> float:
                return 1.0

            def remaining_runner_seconds(self) -> float:
                return 1.0

            def sleep(self, delay: float, _phase: str, *, operation: str) -> None:
                time.sleep(delay)

        def builder(
            _origin: str,
            _user: VirtualUser,
            phase: str,
            _timeout: float,
        ) -> RequestResult:
            started.set()
            time.sleep(0.01)
            return RequestResult(
                phase=phase,
                method="GET",
                path="/health",
                status=200,
                elapsed_ms=1.0,
                ok=True,
                response_bytes=1,
            )

        with self.assertRaisesRegex(RuntimeError, "synthetic deadline"):
            run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic-deadline",
                spread_seconds=0,
                concurrency=2,
                timeout=1,
                request_builder=builder,
                budget=DeadlineBudget(),
            )
        self.assertTrue(started.is_set())
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
            thread.name.startswith("external-load")
            for thread in threading.enumerate()
        ):
            time.sleep(0.005)
        self.assertFalse(
            any(
                thread.name.startswith("external-load")
                for thread in threading.enumerate()
            )
        )

    def test_no_consumer_phase_compatibility_keeps_exact_results_and_threads(self) -> None:
        users = [VirtualUser(f"compat-{index}", "synthetic", "s", "c") for index in range(8)]
        baseline = {thread.name for thread in threading.enumerate() if thread.name.startswith("external-load")}
        for name, runner, option in (("run_phase", run_phase, {"spread_seconds": 0}), ("run_rate_phase", run_rate_phase, {"duration_seconds": 0})):
            returned = runner("http://synthetic.invalid", users, phase=name, concurrency=2, timeout=1, request_builder=lambda _origin, user, phase, _timeout: RequestResult(phase=phase, method="GET", path="/synthetic/compat", status=200, elapsed_ms=1, ok=True, response_bytes=1, response_json=user.user_id), **option)
            self.assertEqual(sorted(result.response_json for result in (returned[0] if name == "run_rate_phase" else returned)), [f"compat-{index}" for index in range(8)])
            self.assertEqual({thread.name for thread in threading.enumerate() if thread.name.startswith("external-load")}, baseline)

    def test_rss_supervisor_reaps_hostile_child_with_short_deadline(self) -> None:
        with patch.object(os, "killpg", wraps=os.killpg) as killpg, self.assertRaisesRegex(AssertionError, "RSS probe failed"):
            _run_rss_probe("hostile")
        self.assertEqual([call.args[1] for call in killpg.call_args_list[:2]], [signal.SIGTERM, signal.SIGKILL])

    def test_real_rss_probe_bounds_live_phases_and_accumulators(self) -> None:
        parent_fd_count = len(os.listdir("/proc/self/fd"))
        live = _run_rss_probe("live")
        accumulator = _run_rss_probe("accumulator")
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), parent_fd_count)

        self.assertEqual(live["requests"], 4096)
        self.assertEqual(live["concurrency"], 512)
        self.assertEqual(live["payload_bytes"], 64 * 1024)
        self.assertEqual(live["baseline_external_thread_ids"], [])
        self.assertEqual(live["after_external_thread_ids"], [])
        self.assertEqual(live["baseline_thread_ids"], live["after_thread_ids"])
        self.assertEqual(live["direct_children_before"], [])
        self.assertEqual(live["direct_children_after"], [])
        for phase in live["runs"]:
            self.assertEqual(phase["submitted"], 4096)
            self.assertEqual(phase["completed"], 4096)
            self.assertLessEqual(phase["peak_pending"], 512)
            self.assertLessEqual(phase["peak_live_payloads"], 512)
            self.assertEqual(phase["pending_after"], 0)
            self.assertEqual(phase["live_payloads_after"], 0)
            self.assertTrue(phase["ready"])
            self.assertEqual(phase["returned_results"], 0)

        self.assertEqual(accumulator["accumulators"], 4)
        self.assertEqual(accumulator["requests_per_accumulator"], 16384)
        self.assertEqual(accumulator["completed"], 4 * 16384)
        self.assertEqual(accumulator["unique_cf_rays"], 4 * 16384)
        self.assertTrue(accumulator["timing_complete"])
        self.assertEqual(accumulator["after_external_thread_ids"], [])
        self.assertEqual(accumulator["baseline_thread_ids"], accumulator["after_thread_ids"])
        self.assertEqual(accumulator["direct_children_before"], [])
        self.assertEqual(accumulator["direct_children_after"], [])
        fields = ("dns_ms", "tcp_connect_ms", "tls_handshake_ms", "request_write_ms", "edge_wait_ms", "ttfb_ms", "body_receive_ms", "total_ms")
        self.assertEqual(accumulator["transport_field_counts"], {field: 4 * 16384 for field in fields})
        for report, limit in ((live, LIVE_HWM_LIMIT_BYTES), (accumulator, ACCUMULATOR_HWM_LIMIT_BYTES)):
            before, after = report["baseline"], report["after"]
            for sample in (before, after):
                self.assertGreaterEqual(sample["VmHWM"], sample["VmRSS"])
            self.assertEqual(report["rss_delta_bytes"], after["VmRSS"] - before["VmRSS"])
            self.assertEqual(report["hwm_delta_bytes"], max(after[key] - before[key] for key in ("VmHWM", "ru_maxrss")))
            self.assertLess(report["hwm_delta_bytes"], limit)
            self.assertLessEqual(report["after_fd_count"], report["baseline_fd_count"])

    def test_request_projects_only_route_correctness_fields(self) -> None:
        class FakeResponse:
            headers = {"etag": '"etag-1"'}

            def __init__(self, body: bytes, status: int = 200) -> None:
                self.body = body
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size: int) -> bytes:
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        def request_with(
            body: bytes,
            *,
            method: str,
            path: str,
            status: int = 200,
        ) -> RequestResult:
            with patch(
                "tools.platform_external_load.urlopen",
                return_value=FakeResponse(body, status),
            ):
                return _request(
                    "https://old-sparky.com",
                    user,
                    method=method,
                    path=path,
                    phase="synthetic",
                    timeout=1.0,
                    session_cookie_name="session",
                    csrf_cookie_name="csrf",
                )

        read = request_with(
            json.dumps({"secret": "must-not-survive", "changed": True}).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
        )
        self.assertIsNone(read.response_json)
        self.assertEqual(read.response_etag, '"etag-1"')
        read_overload = request_with(
            json.dumps(
                {
                    "code": "AUTHENTICATED_READ_OVERLOADED",
                    "detail": "must-not-survive",
                }
            ).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
            status=503,
        )
        self.assertEqual(
            read_overload.response_json,
            {"code": "AUTHENTICATED_READ_OVERLOADED"},
        )
        unrelated = request_with(
            json.dumps(
                {"code": "OTHER_503", "detail": "must-not-survive"}
            ).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
            status=503,
        )
        self.assertIsNone(unrelated.response_json)
        overload_summary = summarize_results([read_overload])
        self.assertEqual(overload_summary["temporary_overload_responses"], 1)
        self.assertEqual(overload_summary["unexpected_statuses"], 0)
        unrelated_summary = summarize_results([unrelated])
        self.assertEqual(unrelated_summary["temporary_overload_responses"], 0)
        self.assertEqual(unrelated_summary["unexpected_statuses"], 1)
        vote = request_with(
            json.dumps(
                {
                    "code": "READY_VOTE_OVERLOADED",
                    "retryable": True,
                    "retry_after_ms": 250,
                    "changed": True,
                    "secret": "must-not-survive",
                }
            ).encode(),
            method="POST",
            path="/tournaments/qa-tournament/deadlock/ready-check/vote",
        )
        self.assertEqual(
            vote.response_json,
            {
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
                "changed": True,
            },
        )
        self.assertNotIn("secret", json.dumps(vote.response_json))
        huge_retry = request_with(
            json.dumps(
                {
                    "code": "READY_VOTE_OVERLOADED",
                    "retry_after_ms": 10**4000,
                }
            ).encode(),
            method="POST",
            path="/tournaments/qa-tournament/deadlock/ready-check/vote",
        )
        self.assertEqual(
            huge_retry.response_json,
            {"code": "READY_VOTE_OVERLOADED"},
        )

    def test_read_mix_uses_the_current_tournament_page_request(self) -> None:
        route = _route_for_read(0, "qa-tournament")

        self.assertEqual(
            route,
            "/tournaments/qa-tournament/workspace?participants_limit=0"
            "&participants_offset=0&workspace_view=detail&include_current_user=false",
        )

    def test_spread_offsets_are_bounded_and_deterministic(self) -> None:
        self.assertEqual(spread_offsets(5, 10), [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(spread_offsets(2, 0), [0.0, 0.0])
        self.assertEqual(spread_offsets(0, 10), [])

    def test_manifest_validates_identity_and_tournament_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
            payload, users = load_manifest(path)

        self.assertEqual(payload["marker"], "preprod26082900000000ab")
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].tournament_slug, "qa-tournament")

    def test_manifest_accepts_production_host_cookie_names(self) -> None:
        payload = manifest_payload()
        payload["session_cookie_name"] = "__Host-deadlock_platform_session"
        payload["csrf_cookie_name"] = "__Host-deadlock_platform_session_csrf"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            load_manifest(path)

    def test_manifest_rejects_duplicate_session_material(self) -> None:
        payload = manifest_payload()
        users = payload["users"]
        assert isinstance(users, list)
        users[1] = dict(users[1])
        users[1]["session_token"] = users[0]["session_token"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ExternalLoadError):
                load_manifest(path)

    def test_summary_contains_metrics_but_not_session_material(self) -> None:
        secret = "session-secret-that-must-not-be-serialized"
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=200,
            elapsed_ms=100.0,
            ok=True,
            response_bytes=120,
            response_json={"changed": True, "internal": secret},
        )
        summary = summarize_results([result])
        serialized = json.dumps(summary)
        self.assertEqual(summary["scope"], "full_population")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertNotIn(secret, serialized)

    def test_timing_summary_separates_service_queue_and_user_observed_latency(self) -> None:
        result = RequestResult(
            phase="diagnostic",
            method="GET",
            path="/health",
            status=200,
            elapsed_ms=10.0,
            ok=True,
            response_bytes=1,
            started_at_monotonic=12.0,
            finished_at_monotonic=12.01,
            user_observed_elapsed_ms=30.0,
        )
        _annotate_timing(result, scheduled_at=10.0, enqueued_at=11.0)

        summary = summarize_results([result])

        self.assertEqual(summary["latency"]["p95_ms"], 10.0)
        self.assertEqual(summary["timing"]["service_latency"]["p95_ms"], 10.0)
        self.assertEqual(summary["timing"]["user_observed_latency"]["p95_ms"], 2010.0)
        self.assertEqual(summary["timing"]["executor_queue_wait"]["p95_ms"], 1000.0)
        self.assertEqual(summary["timing"]["late_start"]["p95_ms"], 2000.0)
        self.assertEqual(summary["timing"]["late_start_count"], 1)
        self.assertEqual(summary["timing"]["missing_schedule_context"], 0)

    def test_inline_retry_has_no_executor_queue_or_schedule_lateness(self) -> None:
        first = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/ready-check/vote",
            status=503,
            elapsed_ms=1_000.0,
            ok=False,
            response_bytes=1,
            attempt_number=1,
            started_at_monotonic=12.0,
            finished_at_monotonic=13.0,
        )
        retry = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/ready-check/vote",
            status=200,
            elapsed_ms=1_000.0,
            ok=True,
            response_bytes=1,
            attempt_number=2,
            started_at_monotonic=14.0,
            finished_at_monotonic=15.0,
        )
        logical = LogicalRequestResult(
            attempts=[first, retry],
            elapsed_ms=3_000.0,
            started_at_monotonic=12.0,
            finished_at_monotonic=15.0,
        )

        _annotate_timing(logical, scheduled_at=10.0, enqueued_at=11.0)

        self.assertEqual(first.executor_queue_wait_ms, 1_000.0)
        self.assertEqual(retry.executor_queue_wait_ms, 0.0)
        self.assertIsNone(retry.schedule_delay_ms)
        self.assertIsNone(retry.late_start_ms)
        self.assertEqual(logical.executor_queue_wait_ms, 1_000.0)
        self.assertEqual(logical.schedule_delay_ms, 2_000.0)
        self.assertEqual(logical.late_start_ms, 2_000.0)
        self.assertEqual(logical.user_observed_elapsed_ms, 5_000.0)

        raw_summary = summarize_results([first, retry])
        self.assertEqual(raw_summary["timing"]["executor_queue_wait"]["p95_ms"], 950.0)
        self.assertEqual(raw_summary["timing"]["schedule_delay"]["p95_ms"], 2_000.0)
        self.assertEqual(raw_summary["timing"]["late_start_count"], 1)
        self.assertFalse(raw_summary["timing"]["partial"])
        logical_summary = summarize_logical_results([logical])
        self.assertEqual(logical_summary["timing"]["user_observed_latency"]["p95_ms"], 5_000.0)

    def test_timing_summary_marks_uninstrumented_builder_context(self) -> None:
        result = RequestResult(
            phase="diagnostic",
            method="GET",
            path="/health",
            status=200,
            elapsed_ms=10.0,
            ok=True,
            response_bytes=1,
        )

        summary = summarize_results([result])

        self.assertEqual(summary["timing"]["missing_schedule_context"], 1)
        self.assertEqual(summary["timing"]["dropped_work"], 1)
        self.assertTrue(summary["timing"]["partial"])

    def test_summary_reports_explicit_client_transport_and_phase_timings(self) -> None:
        result = RequestResult(
            phase="authenticated_page_load",
            method="GET",
            path="/tournaments/qa-tournament",
            status=200,
            elapsed_ms=40.0,
            ok=True,
            response_bytes=120,
            time_to_first_byte_ms=18.0,
            transport_timing={
                "transport": "http1-keepalive",
                "http_version": "1.1",
                "connection_reused": True,
                "dns_ms": 1.0,
                "tcp_connect_ms": 2.0,
                "tls_handshake_ms": 3.0,
                "request_write_ms": 4.0,
                "edge_wait_ms": 5.0,
                "ttfb_ms": 18.0,
                "body_receive_ms": 22.0,
                "total_ms": 40.0,
            },
        )

        summary = summarize_results([result])

        self.assertEqual(summary["transport"]["names"], {"http1-keepalive": 1})
        self.assertEqual(summary["transport"]["http_versions"], {"1.1": 1})
        self.assertEqual(summary["transport"]["connection_reused"], 1)
        self.assertEqual(
            summary["transport"]["phase_timings"]["ttfb_ms"]["p95_ms"],
            18.0,
        )

    def test_summary_keeps_only_bounded_cloudflare_failure_diagnostics(self) -> None:
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=522,
            elapsed_ms=30_000.0,
            ok=False,
            response_bytes=0,
            time_to_first_byte_ms=None,
            cf_ray="ray-522",
            cf_error_type="522",
            cf_error_origin="connection_failure",
            retry_after="60",
            error_kind="unexpected_status",
        )

        summary = summarize_results([result])

        self.assertEqual(summary["cf_error_type_counts"], {"cf_522": 1})
        self.assertEqual(summary["cf_error_origin_counts"], {"origin": 1})
        self.assertEqual(
            summary["error_samples"][0],
            {
                "phase": "write_burst",
                "method": "POST",
                "route_class": "ready_vote",
                "status": 522,
                "error_class": "server_error",
                "cf_error_class": "cf_522",
                "cf_error_origin_class": "origin",
                "ttfb_ms": None,
                "elapsed_ms": 30_000.0,
            },
        )

    def test_request_extracts_only_allowlisted_failure_headers(self) -> None:
        class FakeContext:
            verify_mode = ssl.CERT_REQUIRED
            check_hostname = True
            post_handshake_auth = False

            def __init__(self) -> None:
                self.alpn_protocols: list[str] = []

            def set_alpn_protocols(self, protocols: list[str]) -> None:
                self.alpn_protocols = protocols

        class FakeResponse:
            status = 522
            headers = {
                "cf-ray": "ray-522",
                "cf-error-type": "type-" + ("x" * 200),
                "cf-error-origin": "origin",
                "retry-after": "60",
                "x-private-debug": "must-not-be-captured",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def read(_size: int) -> bytes:
                return b""

        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        original_context = external_load._DEFAULT_HTTPS_CONTEXT
        fake_context = FakeContext()
        with external_load._DEFAULT_HTTPS_CONTEXT_LOCK:
            external_load._DEFAULT_HTTPS_CONTEXT = None
        try:
            with patch(
                "tools.platform_external_load.ssl.create_default_context",
                return_value=fake_context,
            ) as factory:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    contexts = list(
                        executor.map(
                            lambda _index: external_load._default_https_context(),
                            range(64),
                        )
                    )
                with patch(
                    "tools.platform_external_load.urlopen",
                    return_value=FakeResponse(),
                ) as opener:
                    result = _request(
                        "https://old-sparky.com",
                        user,
                        method="GET",
                        path="/tournaments/qa-tournament",
                        phase="diagnostic",
                        timeout=1.0,
                        session_cookie_name="session",
                        csrf_cookie_name="csrf",
                    )
                    _request(
                        "https://old-sparky.com",
                        user,
                        method="GET",
                        path="/tournaments/qa-tournament",
                        phase="diagnostic",
                        timeout=1.0,
                        session_cookie_name="session",
                        csrf_cookie_name="csrf",
                    )
            self.assertEqual(factory.call_count, 1)
            factory.assert_called_once_with()
            self.assertTrue(all(context is fake_context for context in contexts))
            self.assertEqual(fake_context.alpn_protocols, ["http/1.1"])
            self.assertTrue(fake_context.post_handshake_auth)
            self.assertTrue(fake_context.check_hostname)
            self.assertEqual(fake_context.verify_mode, ssl.CERT_REQUIRED)
            passed_contexts = [call.kwargs["context"] for call in opener.call_args_list]
            self.assertEqual(len(passed_contexts), 2)
            self.assertTrue(all(context is fake_context for context in passed_contexts))
        finally:
            with external_load._DEFAULT_HTTPS_CONTEXT_LOCK:
                external_load._DEFAULT_HTTPS_CONTEXT = original_context

        actual_context = external_load._default_https_context()
        self.assertIsInstance(actual_context, ssl.SSLContext)
        self.assertEqual(actual_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual_context.check_hostname)
        if actual_context.post_handshake_auth is not None:
            self.assertTrue(actual_context.post_handshake_auth)

        self.assertEqual(result.status, 522)
        self.assertEqual(result.cf_ray, "ray-522")
        self.assertEqual(result.cf_error_origin, "origin")
        self.assertEqual(result.retry_after, "60")
        self.assertEqual(len(result.cf_error_type or ""), 128)

    def test_diagnostic_request_carries_id_and_monotonic_timestamps_on_timeout(self) -> None:
        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        captured: list[object] = []

        def fail(
            request: object,
            *,
            timeout: float,
            context: ssl.SSLContext,
        ) -> object:
            captured.append(request)
            raise TimeoutError("timed out")

        with patch("tools.platform_external_load.urlopen", side_effect=fail):
            result = _request(
                "https://old-sparky.com",
                user,
                method="GET",
                path="/tournaments/qa-tournament",
                phase="diagnostic",
                timeout=1.0,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
                diagnostic_id="tdiag-123-00001",
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].get_header("X-platform-timeout-diagnostic-id"),
            "tdiag-123-00001",
        )
        self.assertEqual(result.error_kind, "TimeoutError")
        self.assertEqual(result.status, 0)
        self.assertIsNotNone(result.started_at_utc)
        self.assertIsNotNone(result.exception_at_utc)
        self.assertIsNotNone(result.finished_at_utc)

    def test_diagnostic_summary_caps_rows_and_reports_totals(self) -> None:
        results = [
            RequestResult(
                phase="diagnostic",
                method="GET",
                path=f"/tournaments/qa-{index}",
                status=0,
                elapsed_ms=30_000.0,
                ok=False,
                response_bytes=0,
                error_kind="TimeoutError",
                diagnostic_id=f"tdiag-123-{index:05d}",
            )
            for index in range(26)
        ]

        summary = summarize_results(results)

        self.assertEqual(len(summary["error_samples"]), 25)
        self.assertEqual(len(summary["timeout_diagnostics"]), 25)
        self.assertEqual(summary["timeout_diagnostic_total"], 26)
        self.assertEqual(summary["timeout_diagnostic_truncated"], 1)
        self.assertEqual(
            summary["timeout_diagnostics"][0]["diagnostic_id"],
            "tdiag-123-00000",
        )

    def test_ready_vote_retries_only_explicit_overload_and_reports_logical_latency(self) -> None:
        _, users = load_manifest_from_payload(manifest_payload())
        responses = [
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=503,
                elapsed_ms=12.0,
                ok=False,
                response_bytes=88,
                response_json={
                    "code": "READY_VOTE_OVERLOADED",
                    "retryable": True,
                    "retry_after_ms": 250,
                },
            ),
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=18.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            ),
        ]

        with (
            patch("tools.platform_external_load._ready_vote_request", side_effect=responses),
            patch("tools.platform_external_load.time.sleep") as sleep,
            patch("tools.platform_external_load.random.uniform", return_value=200.0),
        ):
            logical = _ready_vote_action(
                "https://old-sparky.com",
                users[0],
                "write_external_vote",
                1.0,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
            )

        self.assertEqual(len(logical.attempts), 2)
        self.assertEqual(logical.retry_count, 1)
        sleep.assert_called_once_with(0.25)
        summary = summarize_logical_results([logical])
        self.assertEqual(summary["actions"], 1)
        self.assertEqual(summary["final_successes"], 1)
        self.assertEqual(summary["total_retries"], 1)
        self.assertEqual(summary["changed_counts"], {"True": 1})

    def test_logical_summary_keeps_final_failure_rate_separate_from_raw_overloads(self) -> None:
        overloaded = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=503,
            elapsed_ms=4.0,
            ok=False,
            response_bytes=80,
            response_json={
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
            },
        )
        logical = LogicalRequestResult([overloaded], elapsed_ms=4.0)

        raw = summarize_results([overloaded])
        logical_summary = summarize_logical_results([logical])
        self.assertEqual(raw["temporary_overload_responses"], 1)
        self.assertEqual(logical_summary["final_failures"], 1)
        self.assertEqual(logical_summary["final_failure_rate_percent"], 100.0)

    def test_ready_vote_load_report_separates_primary_http_and_logical_layers(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=12.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=12.0)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                authoritative_binding={
                    "profile_id": "ready-vote-slo-v2",
                    "profile_version": 2,
                    "profile_digest": "a" * 64,
                    "source_git_sha": "a" * 40,
                    "external_run_id": "123",
                },
            )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["raw_http"]["requests"], 2)
        self.assertEqual(report["logical"]["actions"], 2)
        self.assertEqual(report["logical"]["final_failures"], 0)
        self.assertGreater(report["logical"]["successful_goodput_actions_per_second"], 0)

    def test_stress_allows_shed_duplicates_but_requires_successful_noops(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            duplicate = phase.endswith("duplicate")
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=503 if duplicate else 200,
                elapsed_ms=12.0,
                ok=not duplicate,
                response_bytes=120,
                response_json=(
                    {"code": "READY_VOTE_OVERLOADED", "retryable": True}
                    if duplicate
                    else {"changed": True}
                ),
                error_kind="http_error" if duplicate else None,
            )
            return LogicalRequestResult(
                [result], elapsed_ms=12.0, user_id=user.user_id
            )

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=1,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                scenario_kind="stress",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 1500,
                        "p90_ms": 3000,
                        "p95_ms": 5000,
                        "p99_ms": 8000,
                    },
                    "max_shed_percent": 99.9,
                    "max_retry_amplification_percent": 200,
                },
            )

        self.assertTrue(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["phases"]["duplicate"]["logical"]["final_failures"], 1)

    def test_stress_marks_duplicate_phase_incomplete_when_primary_successes_are_short(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            duplicate = phase.endswith("duplicate")
            primary_shed = not duplicate and user.user_id == "user-00000002"
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=503 if primary_shed else 200,
                elapsed_ms=12.0,
                ok=not primary_shed,
                response_bytes=120,
                response_json=(
                    {"code": "READY_VOTE_OVERLOADED", "retryable": True}
                    if primary_shed
                    else {"changed": not duplicate}
                ),
                error_kind="http_error" if primary_shed else None,
            )
            return LogicalRequestResult(
                [result], elapsed_ms=12.0, user_id=user.user_id
            )

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 1}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=2,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                scenario_kind="stress",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 1500,
                        "p90_ms": 3000,
                        "p95_ms": 5000,
                        "p99_ms": 8000,
                    },
                    "max_shed_percent": 99.9,
                    "max_retry_amplification_percent": 200,
                    "minimum_useful_goodput_actions_per_second": 0.1,
                },
            )

        duplicate = report["phases"]["duplicate"]
        self.assertEqual(duplicate["configured_actions"], 2)
        self.assertEqual(duplicate["candidate_actions"], 1)
        self.assertEqual(duplicate["submitted_actions"], 1)
        self.assertEqual(duplicate["missing_actions"], 1)
        self.assertFalse(duplicate["complete"])
        self.assertTrue(duplicate["logical"]["timing"]["partial"])
        self.assertEqual(report["logical"]["configured_duplicate_actions"], 2)
        self.assertEqual(report["logical"]["actions"], 3)
        self.assertEqual(report["raw_http"]["requests"], 3)
        self.assertFalse(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["checks"]["timing_complete"])

    def test_read_mix_manual_refresh_uses_conditional_workspace_request(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[dict[str, object]] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(
                {
                    "phase": phase,
                    "path": path,
                    "expected_statuses": expected_statuses,
                    "extra_headers": extra_headers,
                }
            )
            is_refresh = extra_headers is not None
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=304 if is_refresh else 200,
                elapsed_ms=10.0,
                ok=(304 if is_refresh else 200) in expected_statuses,
                response_bytes=0,
                response_etag='"workspace-etag"',
            )

        with patch("tools.platform_external_load._trace", side_effect=fake_trace):
            with patch("tools.platform_external_load._request", side_effect=fake_request):
                report = run_load(
                    payload,
                    users,
                    mode="read-mix",
                    spread_seconds=0,
                    concurrency=1,
                    timeout=1,
                    duplicate_count=0,
                    manual_refresh_count=1,
                    p95_budget_ms=1000,
                    p99_budget_ms=2000,
                    acceptance_contract={
                        "kind": "slo",
                        "accepted_request_latency": {
                            "p50_ms": 100,
                            "p90_ms": 100,
                            "p95_ms": 100,
                            "p99_ms": 100,
                        },
                        "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                        "logical_final_failure_percent": 0,
                        "max_shed_percent": 0,
                        "max_retry_amplification_percent": 0,
                    },
                )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["scope"], "full_population")
        self.assertFalse(report["acceptance"]["checks"]["authoritative_binding"])
        self.assertEqual(report["phases"]["read_mix"]["requests"], 2)
        self.assertEqual(report["phases"]["manual_refresh"]["requests"], 1)
        self.assertEqual(report["phases"]["manual_refresh"]["status_counts"], {"304": 1})
        refresh_call = next(call for call in calls if call["phase"] == "manual_workspace_refresh")
        self.assertEqual(refresh_call["expected_statuses"], frozenset({200, 304}))
        self.assertEqual(refresh_call["extra_headers"], {"If-None-Match": '"workspace-etag"'})

    def test_page_load_reports_html_ttfb_and_total_latency(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._page_request", side_effect=fake_page_request),
        ):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 100,
                        "p90_ms": 100,
                        "p95_ms": 100,
                        "p99_ms": 100,
                    },
                    "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                    "max_shed_percent": 0,
                    "max_retry_amplification_percent": 0,
                },
            )

        self.assertTrue(report["acceptance"]["contract_ok"])
        self.assertEqual(report["phases"]["authenticated_page_load"]["requests"], 2)
        self.assertEqual(
            report["overall"]["time_to_first_byte"]["p95_ms"],
            18.0,
        )
        self.assertEqual(report["overall"]["response_bytes"]["max_bytes"], 12_000)

    def test_page_load_passes_explicit_transport_and_retains_phase_evidence(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        transports: list[str] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            transport: str = "urllib-http1-close",
        ) -> RequestResult:
            transports.append(transport)
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
                transport_timing={
                    "transport": transport,
                    "http_version": "1.1",
                    "connection_reused": len(transports) > 1,
                    "dns_ms": 1.0,
                    "tcp_connect_ms": 2.0,
                    "tls_handshake_ms": 3.0,
                    "request_write_ms": 4.0,
                    "edge_wait_ms": 5.0,
                    "ttfb_ms": 18.0,
                    "body_receive_ms": 22.0,
                    "total_ms": 40.0,
                },
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._page_request", side_effect=fake_page_request),
        ):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                client_transport="http1-keepalive",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 100,
                        "p90_ms": 100,
                        "p95_ms": 100,
                        "p99_ms": 100,
                    },
                    "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                    "max_shed_percent": 0,
                    "max_retry_amplification_percent": 0,
                },
            )

        self.assertEqual(transports, ["http1-keepalive", "http1-keepalive"])
        self.assertEqual(report["client_transport"], "http1-keepalive")
        self.assertEqual(
            report["overall"]["transport"]["phase_timings"]["ttfb_ms"]["p95_ms"],
            18.0,
        )

    def test_page_load_diagnostic_mode_assigns_stable_per_user_ids(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        diagnostic_ids: list[str | None] = []

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            diagnostic_id: str | None = None,
        ) -> RequestResult:
            diagnostic_ids.append(diagnostic_id)
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
                diagnostic_id=diagnostic_id,
            )

        with patch("tools.platform_external_load._page_request", side_effect=fake_page_request):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                timeout_diagnostics_run_id="123",
            )

        self.assertEqual(
            diagnostic_ids,
            ["tdiag-123-00000", "tdiag-123-00001"],
        )
        self.assertEqual(
            report["timeout_path_diagnostics"],
            {"enabled": True, "request_count": 2},
        )

    def test_concurrency_ramp_marks_latency_knee_as_operator_candidate(self) -> None:
        def stage(p95: float, rps: float) -> dict[str, object]:
            return {
                "latency": {"p95_ms": p95, "p99_ms": p95 * 1.2},
                "requests_per_second": rps,
            }

        analysis = analyze_concurrency_ramp(
            {"16": stage(500, 100), "32": stage(700, 105), "48": stage(1400, 106)}
        )

        self.assertEqual(analysis["knee"]["stable_concurrency"], 32)
        self.assertEqual(analysis["recommended_max_concurrency"], 32)
        self.assertTrue(analysis["rollout_required"])

    def test_read_mix_concurrency_ramp_reports_each_full_population_stage(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[str] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(phase)
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=150,
                response_etag='"workspace-etag"' if user.user_id == "user-00000001" else None,
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._request", side_effect=fake_request),
        ):
            report = run_load(
                payload,
                users,
                mode="read-mix",
                spread_seconds=0,
                concurrency=2,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                concurrency_stages=[1, 2],
            )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["phases"]["read_mix"]["requests"], 4)
        self.assertEqual(
            report["phases"]["capacity_ramp"]["concurrency_stages"],
            [1, 2],
        )
        self.assertEqual(
            [report["phases"]["capacity_ramp"]["stages"][str(stage)]["requests"] for stage in (1, 2)],
            [2, 2],
        )
        self.assertTrue(all(phase.startswith("scale_external_read_mix_c") for phase in calls))
        self.assertEqual(report["phase_windows_status"], "complete")
        self.assertEqual(
            [window["phase"] for window in report["phase_windows"]],
            ["read_mix_c1", "read_mix_c2"],
        )
        run_started = report["started_at"]
        run_finished = report["finished_at"]
        previous_finish = run_started
        for window in report["phase_windows"]:
            self.assertGreaterEqual(window["started_at"], run_started)
            self.assertLessEqual(window["finished_at"], run_finished)
            self.assertGreaterEqual(window["started_at"], previous_finish)
            self.assertLess(window["started_at"], window["finished_at"])
            previous_finish = window["finished_at"]

        public_report = project_public_artifact("external_load", report)
        self.assertEqual(public_report["run_window_status"], "complete")
        self.assertEqual(public_report["phase_windows_status"], "complete")
        self.assertEqual(
            [window["phase"] for window in public_report["phase_windows"]],
            ["read_mix_c1", "read_mix_c2"],
        )

    def test_ready_vote_rate_plan_reports_offered_rate_per_phase(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            retry_policy: dict[str, object] | None = None,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=10.0, user_id=user.user_id)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                phase_plan=[
                    {"name": "rate-1", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                    {"name": "rate-1b", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                ],
                scenario_kind="capacity",
                acceptance_contract={
                    "kind": "capacity",
                    "slo": {
                        "accepted_request_latency": {"p50_ms": 250, "p90_ms": 400, "p95_ms": 600, "p99_ms": 1000},
                        "logical_latency": {"p95_ms": 600, "p99_ms": 1000},
                        "logical_final_failure_percent": 0.5,
                        "max_shed_percent": 0,
                        "max_retry_amplification_percent": 0,
                    },
                    "capacity": {"target_logical_actions_per_second": [1, 1], "steady_duration_seconds": 1},
                },
            )

        phases = report["phases"]["ramp"]["phases"]
        self.assertEqual(set(phases), {"rate-1", "rate-1b"})
        self.assertEqual(phases["rate-1"]["logical"]["target_logical_actions_per_second"], 1)
        self.assertGreater(phases["rate-1"]["raw_http"]["attempts_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
