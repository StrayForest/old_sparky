from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tools import platform_live_qa_guard


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOLS_ROOT = PLATFORM_ROOT / "tools"
REPORTER = REPO_ROOT / "platform/apps/platform_web/tests/support/live-count-reporter.cjs"


def _browser_gate_payload() -> dict[str, object]:
    events = [
        {"kind": "csp_violation", "phase": "journey", "disposition": "enforce", "directive": "img-src"},
        {"kind": "csp_console", "phase": "journey"},
        {"kind": "forbidden_request", "phase": "journey", "resource_type": "fetch", "method": "POST", "route": "account"},
        {"kind": "page_error", "phase": "logout"},
        {"kind": "request_failed", "phase": "logout", "resource_type": "fetch", "method": "GET", "route": "other", "failure_class": "connection"},
    ]
    return {
        "schema": 2,
        "coverage": "journey_logout_before_context_close",
        "counts": {
            "csp_violation": 1,
            "csp_console": 1,
            "forbidden_request": 1,
            "page_error": 1,
            "request_failed": 1,
        },
        "counts_truncated": False,
        "events_truncated": False,
        "events": events,
    }


class LiveUserBrowserGateEvidenceTests(unittest.TestCase):
    def test_extracted_userqa_status43_branch_keeps_counts_and_original_child_failure(self) -> None:
        source = (TOOLS_ROOT / "platform_live_user_qa.sh").read_text(encoding="utf-8")
        branch_start = source.index("if (( count_status == 42 || count_status == 43 )); then")
        branch_end_marker = '  exit "$browser_status"\nfi'
        branch_end = source.index(branch_end_marker, branch_start) + len(branch_end_marker)
        extracted_branch = source[branch_start:branch_end]

        count_line = (
            "LIVE_BROWSER_COUNTS schema=1 run_status=failed logical_total=1 logical_pass=0 "
            "logical_fail=1 logical_expected_fail=0 logical_flaky=0 logical_skip=0 "
            "logical_interrupted=0 attempt_total=1 attempt_pass=0 attempt_fail=1 "
            "attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 "
            f"source_sha={'a' * 40} app_sha={'b' * 40} marker_sha256={'c' * 64}"
        )
        valid_summary = (
            "LIVE_BROWSER_FAILURE_SUMMARY schema=1 reason=private_store_available "
            "token=eyJzY2hlbWEiOjF9"
        )

        def invoke(browser_status: int, summary: str):
            output = count_line + "\n" + summary
            script = "\n".join((
                "set -euo pipefail",
                "count_status=43",
                f"browser_status={browser_status}",
                "diagnostic_valid=0",
                "diagnostic_kind=none",
                "diagnostic_child_exit=0",
                f"browser_counts={shlex.quote(output)}",
                "failure_summary_line=''",
                extracted_branch,
            ))
            return subprocess.run(
                ["/bin/bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )

        retained = invoke(1, valid_summary)
        self.assertEqual(retained.returncode, 1)
        self.assertEqual(retained.stdout, count_line + "\n")
        self.assertEqual(retained.stderr, "")

        rejected = invoke(2, "LIVE_BROWSER_FAILURE_SUMMARY schema=1 reason=unknown token=private")
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(rejected.stderr, "")

    @unittest.skipUnless(os.geteuid() == 0, "root-owned live-QA diagnostics contract")
    def test_reporter_reader_publisher_and_retained_validator_preserve_closed_evidence(self) -> None:
        node_wrapper = TOOLS_ROOT / "platform_node.sh"
        if not node_wrapper.is_file():
            self.fail("the pinned platform Node wrapper is required for reporter integration")

        source_sha, app_sha, marker_sha = "a" * 40, "b" * 40, "c" * 64
        with tempfile.TemporaryDirectory(prefix="live-user-browser-gate-", dir="/root") as temporary:
            root = Path(temporary)
            secret = root / "liveqa"
            secret.mkdir(mode=0o700)
            releases = secret / "releases"
            releases.mkdir(mode=0o755)
            payload = releases / app_sha
            web_root = payload / "runtime/web"
            source_spec = web_root / "tests/smoke/live-user-journey.spec.ts"
            source_spec.parent.mkdir(parents=True, mode=0o755)
            source_spec.write_text(
                'test("live player completes the accelerated tournament journey through visible controls", async () => {\n'
                '  expect(true).toBe(true);\n});\n', encoding="ascii",
            )
            reporter_path = web_root / "tests/support/live-count-reporter.cjs"
            reporter_path.parent.mkdir(parents=True, mode=0o755)
            shutil.copyfile(REPORTER, reporter_path)
            source_spec.chmod(0o444)
            reporter_path.chmod(0o444)
            payload.chmod(0o555)
            manifest = {
                "version": 1,
                "source_sha": app_sha,
                "release_slug": "browser-gate-contract",
                "payload": str(payload),
                "payload_tree_sha256": "d" * 64,
                "files": {
                    "runtime/web/tests/smoke/live-user-journey.spec.ts": hashlib.sha256(source_spec.read_bytes()).hexdigest(),
                    "runtime/web/tests/support/live-count-reporter.cjs": hashlib.sha256(reporter_path.read_bytes()).hexdigest(),
                },
            }
            active_manifest = secret / "active-manifest.json"
            active_manifest.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="ascii")
            active_manifest.chmod(0o444)
            (secret / "active").symlink_to(payload)

            run_root = root / "run"
            run_root.mkdir(mode=0o711)
            gate = run_root / "live-user-qa.Abc123"
            gate.mkdir(mode=0o700)
            results = gate / "test-results"
            results.mkdir(mode=0o700)
            unavailable_gate = run_root / "live-user-qa.Def456"
            unavailable_gate.mkdir(mode=0o700)
            unavailable_results = unavailable_gate / "test-results"
            unavailable_results.mkdir(mode=0o700)
            attachment_path = root / "browser-gate.json"
            attachment_path.write_text(
                json.dumps(_browser_gate_payload(), separators=(",", ":")), encoding="ascii"
            )
            node_program = textwrap.dedent(
                """
                const fs = require("node:fs");
                const path = require("node:path");
                const Reporter = require(process.argv[1]);
                const [gateRoot, sourceRoot, attachmentPath, unavailableGate] = process.argv.slice(2);
                const attachment = {
                  name: "live-user-browser-gate-v2",
                  contentType: "application/json",
                  body: fs.readFileSync(attachmentPath),
                };
                if (!Reporter.normalizedLiveUserBrowserGateAttachment(attachment)) {
                  throw new Error("valid closed browser gate attachment was rejected");
                }
                const privatePayload = JSON.parse(attachment.body.toString("ascii"));
                const invalid = [
                  { ...privatePayload, private_url: "https://private.example/token" },
                  { ...privatePayload, events: [{ kind: "unknown", phase: "journey" }] },
                ];
                for (const payload of invalid) {
                  if (Reporter.normalizedLiveUserBrowserGateAttachment({
                    ...attachment, body: Buffer.from(JSON.stringify(payload), "ascii"),
                  }) !== null) throw new Error("malformed/private browser evidence was accepted");
                }
                const spec = path.join(sourceRoot, "tests/smoke/live-user-journey.spec.ts");
                const test = {
                  title: "live player completes the accelerated tournament journey through visible controls",
                  expectedStatus: "passed",
                  parent: { project: () => ({ name: "live-desktop" }) },
                  results: [{
                    status: "failed",
                    errors: [{ message: "private assertion text", location: { file: spec, line: 2, column: 3 } }],
                    attachments: [attachment],
                  }],
                  outcome: () => "unexpected",
                };
                const reporter = new Reporter({ gateRoot, sourceRoot });
                reporter.onBegin({}, { allTests: () => [test] });
                if (reporter.onEnd({ status: "failed" })?.status === "failed") process.exitCode = 1;
                process.env.PLATFORM_QA_BROWSER_GATE_DIR = unavailableGate;
                process.env.PLATFORM_LIVE_USER_QA_SESSIONS = path.join(unavailableGate, "browser-sessions.json");
                const privateAttachmentPayload = { ...JSON.parse(attachment.body.toString("ascii")), private_url: "https://private.example/token" };
                const unavailableTest = {
                  ...test,
                  results: [{
                    ...test.results[0],
                    attachments: [{ ...attachment, body: Buffer.from(JSON.stringify(privateAttachmentPayload), "ascii") }],
                  }],
                };
                const unavailableReporter = new Reporter({ gateRoot, sourceRoot });
                unavailableReporter.onBegin({}, { allTests: () => [unavailableTest] });
                if (unavailableReporter.onEnd({ status: "failed" })?.status === "failed") process.exitCode = 1;
                """
            )
            env = {
                **os.environ,
                "PLATFORM_QA_BROWSER_GATE_DIR": str(gate),
                "PLATFORM_LIVE_QA_TARGET_SHA": app_sha,
                "PLATFORM_LIVE_QA_RUNNER_SHA": source_sha,
                "PLATFORM_LIVE_QA_MARKER_SHA256": marker_sha,
                "PLATFORM_LIVE_USER_QA_SESSIONS": str(gate / "browser-sessions.json"),
            }
            result = subprocess.run(
                [str(node_wrapper), "-e", node_program, str(reporter_path), str(run_root), str(web_root), str(attachment_path), str(unavailable_gate)],
                cwd=web_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            ephemeral_path = results / platform_live_qa_guard.LIVE_USER_FAILURES_FILE
            ephemeral = json.loads(ephemeral_path.read_text(encoding="ascii"))
            record = ephemeral["failures"][0]
            self.assertEqual(record["browser_gate_state"], "captured")
            self.assertEqual(record["browser_gate"], _browser_gate_payload())
            unavailable_ephemeral_path = unavailable_results / platform_live_qa_guard.LIVE_USER_FAILURES_FILE
            unavailable_ephemeral_bytes = unavailable_ephemeral_path.read_bytes()
            unavailable_record = json.loads(unavailable_ephemeral_bytes)["failures"][0]
            self.assertEqual(unavailable_record["browser_gate_state"], "unavailable")
            self.assertIsNone(unavailable_record["browser_gate"])
            self.assertNotIn(b"private.example", unavailable_ephemeral_bytes)
            self.assertNotIn(b"private assertion text", unavailable_ephemeral_bytes)

            failure_root = secret / "diagnostics"
            with ExitStack() as stack:
                for context in (
                    patch.object(platform_live_qa_guard, "TRUSTED_SECRET_ROOT", secret),
                    patch.object(platform_live_qa_guard, "TRUSTED_PAYLOAD_ROOT", releases),
                    patch.object(platform_live_qa_guard, "TRUSTED_ACTIVE_MANIFEST", active_manifest),
                    patch.object(platform_live_qa_guard, "TRUSTED_ACTIVE_POINTER", secret / "active"),
                    patch.object(platform_live_qa_guard, "TRUSTED_FAILURE_DIAGNOSTICS_ROOT", failure_root),
                    patch.object(platform_live_qa_guard, "RUN_GATE_ROOT", run_root),
                    patch.object(platform_live_qa_guard, "liveqa_identity", return_value=(0, 0)),
                    patch.dict(os.environ, {
                        "PLATFORM_LIVE_QA_INSTALL_ROOT": str(payload),
                        "PLATFORM_LIVE_QA_TARGET_SHA": app_sha,
                    }),
                ):
                    stack.enter_context(context)
                platform_live_qa_guard.prepare_live_user_failure_diagnostics(
                    gate, source_sha=source_sha, app_sha=app_sha, marker_sha256=marker_sha,
                )
                with self.assertRaises(platform_live_qa_guard.BrowserFailureCounts) as failed_counts:
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                        fail_on_failure_counts=True,
                    )
                self.assertNotIn("private", failed_counts.exception.count_line)
                retained_path = failure_root / f"{gate.name}.failure-v3.json"
                retained_bytes = retained_path.read_bytes()
                retained = json.loads(retained_bytes)
                platform_live_qa_guard._validate_retained_failure_report(
                    retained_path.name, retained_bytes,
                )
                self.assertEqual(retained["failures"][0]["browser_gate_state"], "captured")
                self.assertEqual(retained["failures"][0]["browser_gate"], _browser_gate_payload())
                for private_value in ("private.example", "private_url", "private assertion text"):
                    self.assertNotIn(private_value.encode("ascii"), retained_bytes)

                legacy = json.loads(retained_bytes)
                legacy["failures"][0].pop("browser_gate_state")
                legacy["failures"][0].pop("browser_gate")
                platform_live_qa_guard._validate_retained_failure_report(
                    retained_path.name,
                    (json.dumps(legacy, separators=(",", ":")) + "\n").encode("ascii"),
                )
                malformed = json.loads(retained_bytes)
                malformed["failures"][0]["browser_gate"]["private_url"] = "https://private.example/token"
                with self.assertRaises(platform_live_qa_guard.GuardError):
                    platform_live_qa_guard._validate_retained_failure_report(
                        retained_path.name,
                        (json.dumps(malformed, separators=(",", ":")) + "\n").encode("ascii"),
                    )

                platform_live_qa_guard.prepare_live_user_failure_diagnostics(
                    unavailable_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                with self.assertRaises(platform_live_qa_guard.BrowserFailureCounts):
                    platform_live_qa_guard.public_browser_counts_line(
                        unavailable_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                        fail_on_failure_counts=True,
                    )
                unavailable_retained_path = failure_root / f"{unavailable_gate.name}.failure-v3.json"
                unavailable_retained_bytes = unavailable_retained_path.read_bytes()
                unavailable_retained = json.loads(unavailable_retained_bytes)
                platform_live_qa_guard._validate_retained_failure_report(
                    unavailable_retained_path.name, unavailable_retained_bytes,
                )
                self.assertEqual(unavailable_retained["failures"][0]["browser_gate_state"], "unavailable")
                self.assertIsNone(unavailable_retained["failures"][0]["browser_gate"])


if __name__ == "__main__":
    unittest.main()
