from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
import json
import hashlib
import io
import os
from pathlib import Path
import pwd
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request
import zipfile

from tools import platform_workflow_input_guard, platform_workflow_remote_dispatch
from tools import platform_live_user_qa_dispatch
from tools import platform_live_qa_guard
from tools.platform_workflow_input_guard import (
    WorkflowInputError,
    validate_confirmation,
    validate_control_email,
    validate_deployment_payload,
    validate_external_payload,
    validate_live_payload,
    validate_host_tools_payload,
    validate_bounded_integer,
    validate_run_id,
    validate_target_sha,
    validate_utc_timestamp,
)


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOLS_ROOT = PLATFORM_ROOT / "tools"
APPARMOR_PROFILE = PLATFORM_ROOT / "deploy/apparmor/oldsparky-liveqa-chromium"
LIVE_USER_JOURNEY = (
    PLATFORM_ROOT / "apps/platform_web/tests/smoke/live-user-journey.spec.ts"
)
SANDBOX_ASSERTION = (
    PLATFORM_ROOT / "apps/platform_web/tests/support/live-qa-sandbox.ts"
)
WRAPPERS = (
    TOOLS_ROOT / "platform_install_live_qa_user.sh",
    TOOLS_ROOT / "platform_live_browser_qa.sh",
    TOOLS_ROOT / "platform_live_user_qa.sh",
    TOOLS_ROOT / "platform_provision_live_csp_qa.sh",
    TOOLS_ROOT / "platform_manual_live_auth_qa.sh",
)
SUPERVISORS = WRAPPERS[1:]
BROWSER_WRAPPERS = WRAPPERS[1:3]


class LiveQaWrapperContractTests(unittest.TestCase):
    def test_public_browser_counts_are_closed_owner_bound_and_partitioned(self) -> None:
        source_sha = "a" * 40
        app_sha = "b" * 40
        marker_sha = "c" * 64
        uid = os.getuid()
        gid = os.getgid()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o711)
            gate = root / "public-live-qa.a1b2c3d4"
            gate.mkdir(mode=0o700)
            results = gate / "test-results"
            results.mkdir(mode=0o700)
            count_path = results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE
            payload = {
                "schema": 1,
                "source_sha": source_sha,
                "app_sha": app_sha,
                "marker_sha256": marker_sha,
                "run_status": "failed",
                "logical_total": 4,
                "logical_pass": 1,
                "logical_fail": 1,
                "logical_expected_fail": 1,
                "logical_flaky": 0,
                "logical_skip": 1,
                "logical_interrupted": 0,
                "attempt_total": 4,
                "attempt_pass": 1,
                "attempt_fail": 2,
                "attempt_skip": 1,
                "attempt_interrupted": 0,
                "attempt_timedout": 0,
            }
            count_path.write_text(json.dumps(payload) + "\n", encoding="ascii")
            count_path.chmod(0o600)
            with patch.object(platform_live_qa_guard, "RUN_GATE_ROOT", root), \
                patch.object(platform_live_qa_guard, "liveqa_identity", return_value=(uid, gid)):
                line = platform_live_qa_guard.public_browser_counts_line(
                    gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                self.assertIn("LIVE_BROWSER_COUNTS schema=1 run_status=failed", line)
                self.assertIn("logical_expected_fail=1", line)
                self.assertTrue(line.endswith(f"marker_sha256={marker_sha}\n"))
                gate.rename(root / "live-user-qa.Abc123")
                gate = root / "live-user-qa.Abc123"
                count_path = gate / "test-results" / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE
                self.assertEqual(
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    ),
                    line,
                )
                with self.assertRaisesRegex(platform_live_qa_guard.GuardError, "binding"):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha="d" * 40,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    )

                payload["logical_total"] = 3
                count_path.write_text(json.dumps(payload) + "\n", encoding="ascii")
                count_path.chmod(0o600)
                with self.assertRaisesRegex(platform_live_qa_guard.GuardError, "partitions"):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    )

                count_path.unlink()
                count_path.symlink_to(results / "missing")
                with self.assertRaisesRegex(platform_live_qa_guard.GuardError, "unavailable"):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned live-QA diagnostics contract")
    def test_private_browser_failure_report_survives_gate_cleanup_and_fails_closed(self) -> None:
        source_sha = "a" * 40
        app_sha = "b" * 40
        marker_sha = "c" * 64
        with tempfile.TemporaryDirectory(prefix="liveqa-diag-", dir="/root") as temporary:
            root = Path(temporary)
            secret = root / "liveqa"
            secret.mkdir(mode=0o700)
            releases = secret / "releases"
            releases.mkdir(mode=0o755)
            payload = releases / app_sha
            payload.mkdir(mode=0o755)
            spec_path = payload / "runtime/web/tests/smoke/live-launch.spec.ts"
            user_spec_path = payload / "runtime/web/tests/smoke/live-user-journey.spec.ts"
            helper_path = payload / "runtime/web/tests/support/live-qa-sandbox.ts"
            reporter_path = payload / "runtime/web/tests/support/live-count-reporter.cjs"
            spec_bytes = (
                'import { test, expect } from "@playwright/test";\n'
                'test("public case", async () => {\n'
                '  expect(true).toBe(false);\n'
                '});\n'
            ).encode("ascii")
            user_spec_bytes = (
                'import { test, expect } from "@playwright/test";\n'
                'test("live player completes the accelerated tournament journey through visible controls", async () => {\n'
                '  expect(true).toBe(false);\n'
                '});\n'
            ).encode("ascii")
            helper_bytes = (
                'export async function checkHelper() {\n'
                '  await expect.poll(readSnapshot).toEqual({ ok: true });\n'
                '}\n'
            ).encode("ascii")
            reporter_bytes = (
                PLATFORM_ROOT / "apps/platform_web/tests/support/live-count-reporter.cjs"
            ).read_bytes()
            for path, content in (
                (spec_path, spec_bytes),
                (user_spec_path, user_spec_bytes),
                (helper_path, helper_bytes),
                (reporter_path, reporter_bytes),
            ):
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                path.write_bytes(content)
                path.chmod(0o444)
            for directory in (
                payload / "runtime/web/tests/smoke",
                payload / "runtime/web/tests/support",
                payload / "runtime/web/tests",
                payload / "runtime/web",
                payload / "runtime",
                payload,
            ):
                directory.chmod(0o555)
            file_map = {
                "runtime/web/tests/smoke/live-launch.spec.ts": hashlib.sha256(spec_bytes).hexdigest(),
                "runtime/web/tests/smoke/live-user-journey.spec.ts": hashlib.sha256(user_spec_bytes).hexdigest(),
                "runtime/web/tests/support/live-qa-sandbox.ts": hashlib.sha256(helper_bytes).hexdigest(),
                "runtime/web/tests/support/live-count-reporter.cjs": hashlib.sha256(reporter_bytes).hexdigest(),
            }
            manifest = {
                "version": 1,
                "source_sha": app_sha,
                "release_slug": "diagnostic-contract",
                "payload": str(payload),
                "payload_tree_sha256": "d" * 64,
                "files": file_map,
            }
            manifest_path = secret / "active-manifest.json"
            manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="ascii")
            manifest_path.chmod(0o444)
            active_pointer = secret / "active"
            active_pointer.symlink_to(payload)

            run_root = root / "run"
            run_root.mkdir(mode=0o711)
            gate = run_root / "public-live-qa.a1b2c3d4"
            gate.mkdir(mode=0o700)
            results = gate / "test-results"
            results.mkdir(mode=0o700)
            counts_path = results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE
            failures_path = results / platform_live_qa_guard.LIVE_BROWSER_FAILURES_FILE
            node = shutil.which("node")
            if node is None:
                self.fail("Node is required for the reporter-to-guard integration contract")
            node_script = textwrap.dedent(
                """
                const path = require("node:path");
                const Reporter = require(process.argv[1]);
                const [gateRoot, sourceRoot] = process.argv.slice(2);
                const stress = process.argv[4] === "stress";
                const projects = ["live-desktop", "live-mobile", "live-webkit-mobile"];
                const titles = [
                  ...["/", "/info", "/tournaments", "/tournaments/new", "/profile/me", "/profile/lisalexy"]
                    .map((route) => `live route ${route} renders without horizontal overflow`),
                  "production Chromium live QA keeps its process sandbox enabled",
                  "live CSP nonce is stable on soft navigation and rotates on hard reload",
                  "local enforced CSP blocks negative inline and external probes",
                  "live tournaments hub exposes a valid empty or populated list",
                  "live 1920 catalog stays contained after every card asset loads",
                  "live tournament detail and bracket routes render from the current public data",
                  "live admin route is protected for anonymous users",
                  "live home uses text-only tournament steps without overflow",
                  "live public contact surfaces do not publish the support recipient",
                  "live current patch route and objective source icons remain available",
                  "live Cloudflare Analytics is conditionally observed without widening CSP",
                  "live image currentSrc inventory stays inside the exact CSP hosts",
                ];
                const skips = new Map([
                  ["production Chromium live QA keeps its process sandbox enabled|live-webkit-mobile", "chromium_only:live-webkit-mobile"],
                  ...projects.map((project) => [
                    `local enforced CSP blocks negative inline and external probes|${project}`,
                    `local_csp_disabled:${project}`,
                  ]),
                  ...["live-mobile", "live-webkit-mobile"].map((project) => [
                    `live 1920 catalog stays contained after every card asset loads|${project}`,
                    `desktop_only:${project}`,
                  ]),
                ]);
                const tests = [];
                const maxError = new AggregateError([], "private max-size error");
                maxError.stack = "AggregateError: private max-size error\\n"
                  + "    at render (https://old-sparky.com/_next/static/chunks/turbopack-abcdefghijklm.js:1000000:1000000)";
                const maxAttachment = Reporter.createPageErrorAttachment({
                  pageUrl: "https://old-sparky.com/tournaments",
                  errors: [maxError, maxError, maxError, maxError],
                  successfulScriptResponses: [{
                    url: "https://old-sparky.com/_next/static/chunks/turbopack-abcdefghijklm.js",
                    status: 200,
                    resourceType: "script",
                  }],
                });
                for (const project of projects) {
                  for (const title of titles) {
                    const key = `${title}|${project}`;
                    const skipDescription = skips.get(key);
                    const failed = title === "live image currentSrc inventory stays inside the exact CSP hosts"
                      && project === "live-webkit-mobile";
                    const status = skipDescription ? "skipped" : failed ? "failed" : "passed";
                    const result = {
                      status,
                      annotations: skipDescription === undefined ? [] : [
                        { type: "public-live-qa-expected-skip", description: skipDescription },
                      ],
                    };
                    if (failed) {
                      result.errors = [{
                        message: "private assertion text",
                        location: {
                          file: path.join(sourceRoot, "tests/support/live-qa-sandbox.ts"),
                          line: 2,
                          column: 3,
                        },
                      }];
                      const error = new TypeError("private pageerror text and token");
                      error.stack = "TypeError: private pageerror text and token\\n"
                        + "    at render (https://old-sparky.com/_next/static/chunks/abcdefghijklm.js:12:34)";
                      result.attachments = [Reporter.createPageErrorAttachment({
                        pageUrl: "https://old-sparky.com/patches/12345?token=private",
                        errors: [error],
                        successfulScriptResponses: [{
                          url: "https://old-sparky.com/_next/static/chunks/abcdefghijklm.js",
                          status: 200,
                          resourceType: "script",
                        }],
                      })];
                    }
                    const failureResult = {
                      status: "interrupted",
                      errors: [{
                        message: "private max-size assertion text",
                        location: {
                          file: path.join(sourceRoot, "tests/support/live-qa-sandbox.ts"),
                          line: 2,
                          column: 3,
                        },
                      }],
                      attachments: [maxAttachment],
                    };
                    const results = stress
                      ? (skipDescription ? [failureResult, result] : [failureResult])
                      : [result];
                    tests.push({
                      title,
                      expectedStatus: skipDescription ? "skipped" : "passed",
                      location: {
                        file: path.join(sourceRoot, "tests/smoke/live-launch.spec.ts"),
                        line: 1,
                        column: 1,
                      },
                      parent: { project: () => ({ name: project }) },
                      results,
                      outcome: () => skipDescription ? "skipped" : failed || stress ? "unexpected" : "expected",
                    });
                  }
                }
                const reporter = new Reporter({ gateRoot, sourceRoot });
                reporter.onBegin({}, { allTests: () => tests });
                const outcome = reporter.onEnd({ status: "failed" });
                if (outcome?.status === "failed") process.exitCode = 1;
                """
            )
            reporter_env = {
                "PLATFORM_QA_BROWSER_GATE_DIR": str(gate),
                "PLATFORM_LIVE_QA_TARGET_SHA": app_sha,
                "PLATFORM_LIVE_QA_RUNNER_SHA": source_sha,
                "PLATFORM_LIVE_QA_MARKER_SHA256": marker_sha,
            }
            result = subprocess.run(
                [node, "-e", node_script, str(reporter_path), str(run_root), str(payload / "runtime/web")],
                cwd=payload / "runtime/web",
                env=reporter_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0 or result.stdout or result.stderr:
                self.fail(
                    "the immutable reporter failed its disposable onEnd integration: "
                    f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
                )
            self.assertEqual(counts_path.lstat().st_mode & 0o777, 0o600)
            self.assertEqual(failures_path.lstat().st_mode & 0o777, 0o600)
            count_payload = json.loads(counts_path.read_text(encoding="ascii"))
            failure_payload = json.loads(failures_path.read_text(encoding="ascii"))
            self.assertEqual(count_payload["logical_total"], 54)
            self.assertEqual(count_payload["logical_pass"], 47)
            self.assertEqual(count_payload["logical_fail"], 1)
            self.assertEqual(count_payload["logical_skip"], 6)
            self.assertEqual(failure_payload["failures"][0]["source_file"], "tests/support/live-qa-sandbox.ts")
            self.assertEqual(failure_payload["failures"][0]["source_line"], 2)
            self.assertEqual(failure_payload["failures"][0]["source_kind"], "assertion")
            self.assertEqual(
                failure_payload["failures"][0]["page_errors"],
                [["TypeError", "patch", "abcdefghijklm.js", 12, 34]],
            )
            self.assertNotIn("private pageerror text", failures_path.read_text(encoding="ascii"))

            env = {
                "PLATFORM_LIVE_QA_INSTALL_ROOT": str(payload),
                "PLATFORM_LIVE_QA_TARGET_SHA": app_sha,
            }
            constant_patches = (
                patch.object(platform_live_qa_guard, "TRUSTED_SECRET_ROOT", secret),
                patch.object(platform_live_qa_guard, "TRUSTED_PAYLOAD_ROOT", releases),
                patch.object(platform_live_qa_guard, "TRUSTED_ACTIVE_MANIFEST", manifest_path),
                patch.object(platform_live_qa_guard, "TRUSTED_ACTIVE_POINTER", active_pointer),
                patch.object(
                    platform_live_qa_guard,
                    "TRUSTED_FAILURE_DIAGNOSTICS_ROOT",
                    secret / "diagnostics",
                ),
                patch.object(platform_live_qa_guard, "RUN_GATE_ROOT", run_root),
                patch.object(platform_live_qa_guard, "liveqa_identity", return_value=(0, 0)),
                patch.dict(os.environ, env),
            )
            with ExitStack() as stack:
                for context in constant_patches:
                    stack.enter_context(context)
                platform_live_qa_guard.prepare_public_browser_failure_diagnostics(
                    gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                user_gate = run_root / "live-user-qa.Abc123"
                user_gate.mkdir(mode=0o700)
                user_results = user_gate / "test-results"
                user_results.mkdir(mode=0o700)
                user_counts = dict(count_payload)
                user_counts.update(
                    logical_total=1,
                    logical_pass=0,
                    logical_fail=1,
                    logical_expected_fail=0,
                    logical_flaky=0,
                    logical_skip=0,
                    logical_interrupted=0,
                    attempt_total=1,
                    attempt_pass=0,
                    attempt_fail=1,
                    attempt_skip=0,
                    attempt_interrupted=0,
                    attempt_timedout=0,
                    run_status="failed",
                )
                (user_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).write_text(
                    json.dumps(user_counts) + "\n", encoding="ascii"
                )
                (user_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).chmod(0o600)
                user_ephemeral = {
                    "schema": 3,
                    "gate": user_gate.name,
                    "source_sha": source_sha,
                    "app_sha": app_sha,
                    "marker_sha256": marker_sha,
                    "run_status": "failed",
                    "failures": [{
                        "id": "live_user_journey",
                        "project": "live-desktop",
                        "status": "failed",
                        "attempt": 1,
                        "source_file": "tests/smoke/live-user-journey.spec.ts",
                        "source_line": 3,
                        "source_column": 3,
                        "source_kind": "test_error_location",
                        "api_failure_state": "captured",
                        "api_failure": {
                            "kind": "unexpected_status",
                            "operation": "tournament_read",
                            "method": "GET",
                            "expected_status": 200,
                            "actual_status": 503,
                        },
                    }],
                }
                user_ephemeral_path = user_results / platform_live_qa_guard.LIVE_USER_FAILURES_FILE
                user_ephemeral_path.write_text(json.dumps(user_ephemeral) + "\n", encoding="ascii")
                user_ephemeral_path.chmod(0o600)
                platform_live_qa_guard.prepare_live_user_failure_diagnostics(
                    user_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                with self.assertRaises(platform_live_qa_guard.BrowserFailureCounts) as user_failed:
                    platform_live_qa_guard.public_browser_counts_line(
                        user_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                        fail_on_failure_counts=True,
                    )
                self.assertIn("run_status=failed", user_failed.exception.count_line)
                user_private_report = secret / "diagnostics" / f"{user_gate.name}.failure-v3.json"
                user_private_bytes = user_private_report.read_bytes()
                user_private = json.loads(user_private_bytes)
                self.assertEqual(user_private["schema"], 3)
                self.assertEqual(user_private["payload_manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
                self.assertEqual(user_private["failures"][0]["api_failure"]["actual_status"], 503)
                self.assertNotIn(b"private assertion text", user_private_bytes)
                mismatched_retained = dict(user_private)
                mismatched_retained["run_status"] = "timedout"
                with self.assertRaises(platform_live_qa_guard.GuardError):
                    platform_live_qa_guard._validate_retained_failure_report(
                        user_private_report.name,
                        (json.dumps(mismatched_retained) + "\n").encode("ascii"),
                    )
                unlocated_retained = dict(user_private)
                unlocated_failure = dict(unlocated_retained["failures"][0])
                unlocated_failure.update(
                    source_kind="unavailable",
                    source_file=None,
                    source_line=None,
                    source_column=None,
                )
                unlocated_retained["failures"] = [unlocated_failure]
                with self.assertRaises(platform_live_qa_guard.GuardError):
                    platform_live_qa_guard._validate_retained_failure_report(
                        user_private_report.name,
                        (json.dumps(unlocated_retained) + "\n").encode("ascii"),
                    )
                with patch.object(platform_live_qa_guard, "assert_liveqa_idle"):
                    platform_live_qa_guard.reclaim_browser_gate(user_gate)
                    platform_live_qa_guard.remove_browser_gate(user_gate)
                self.assertFalse(user_gate.exists())
                self.assertEqual(user_private_report.read_bytes(), user_private_bytes)

                malformed_gate = run_root / "live-user-qa.Def456"
                malformed_gate.mkdir(mode=0o700)
                malformed_results = malformed_gate / "test-results"
                malformed_results.mkdir(mode=0o700)
                (malformed_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).write_text(
                    json.dumps(user_counts) + "\n", encoding="ascii"
                )
                (malformed_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).chmod(0o600)
                malformed_ephemeral = dict(user_ephemeral)
                malformed_ephemeral["gate"] = malformed_gate.name
                malformed_ephemeral["failures"] = [dict(user_ephemeral["failures"][0])]
                malformed_ephemeral["failures"][0]["api_failure"] = dict(
                    malformed_ephemeral["failures"][0]["api_failure"], method="DELETE"
                )
                malformed_path = malformed_results / platform_live_qa_guard.LIVE_USER_FAILURES_FILE
                malformed_path.write_text(json.dumps(malformed_ephemeral) + "\n", encoding="ascii")
                malformed_path.chmod(0o600)
                platform_live_qa_guard.prepare_live_user_failure_diagnostics(
                    malformed_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable):
                    platform_live_qa_guard.public_browser_counts_line(
                        malformed_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse((secret / "diagnostics" / f"{malformed_gate.name}.failure-v3.json").exists())
                unlocated_ephemeral = dict(user_ephemeral)
                unlocated_ephemeral["gate"] = malformed_gate.name
                unlocated_failure = dict(unlocated_ephemeral["failures"][0])
                unlocated_failure.update(
                    source_kind="unavailable",
                    source_file=None,
                    source_line=None,
                    source_column=None,
                )
                unlocated_ephemeral["failures"] = [unlocated_failure]
                malformed_path.write_text(
                    json.dumps(unlocated_ephemeral) + "\n", encoding="ascii"
                )
                malformed_path.chmod(0o600)
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable):
                    platform_live_qa_guard.public_browser_counts_line(
                        malformed_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse((secret / "diagnostics" / f"{malformed_gate.name}.failure-v3.json").exists())
                with patch.object(platform_live_qa_guard, "assert_liveqa_idle"):
                    platform_live_qa_guard.reclaim_browser_gate(malformed_gate)
                    platform_live_qa_guard.remove_browser_gate(malformed_gate)

                multipletest_gate = run_root / "live-user-qa.Ghi789"
                multipletest_gate.mkdir(mode=0o700)
                multipletest_results = multipletest_gate / "test-results"
                multipletest_results.mkdir(mode=0o700)
                multipletest_counts = dict(user_counts)
                multipletest_counts.update(
                    logical_total=2,
                    logical_pass=1,
                    attempt_total=2,
                    attempt_pass=1,
                )
                (multipletest_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).write_text(
                    json.dumps(multipletest_counts) + "\n", encoding="ascii"
                )
                (multipletest_results / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE).chmod(0o600)
                multipletest_ephemeral = dict(user_ephemeral)
                multipletest_ephemeral["gate"] = multipletest_gate.name
                multipletest_path = multipletest_results / platform_live_qa_guard.LIVE_USER_FAILURES_FILE
                multipletest_path.write_text(
                    json.dumps(multipletest_ephemeral) + "\n", encoding="ascii"
                )
                multipletest_path.chmod(0o600)
                platform_live_qa_guard.prepare_live_user_failure_diagnostics(
                    multipletest_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable):
                    platform_live_qa_guard.public_browser_counts_line(
                        multipletest_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse(
                    (secret / "diagnostics" / f"{multipletest_gate.name}.failure-v3.json").exists()
                )
                with patch.object(platform_live_qa_guard, "assert_liveqa_idle"):
                    platform_live_qa_guard.reclaim_browser_gate(multipletest_gate)
                    platform_live_qa_guard.remove_browser_gate(multipletest_gate)
                max_gate = run_root / "public-live-qa.b1a2b3c4"
                max_gate.mkdir(mode=0o700)
                (max_gate / "test-results").mkdir(mode=0o700)
                max_counts_path = (
                    max_gate / "test-results" / platform_live_qa_guard.LIVE_BROWSER_COUNTS_FILE
                )
                max_failures_path = (
                    max_gate / "test-results" / platform_live_qa_guard.LIVE_BROWSER_FAILURES_FILE
                )
                stress_env = {
                    **reporter_env,
                    "PLATFORM_QA_BROWSER_GATE_DIR": str(max_gate),
                }
                stress_result = subprocess.run(
                    [
                        node,
                        "-e",
                        node_script,
                        str(reporter_path),
                        str(run_root),
                        str(payload / "runtime/web"),
                        "stress",
                    ],
                    cwd=payload / "runtime/web",
                    env=stress_env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    (stress_result.returncode, stress_result.stdout, stress_result.stderr),
                    (0, "", ""),
                    "the reporter emits a bounded all-54 interrupted-attempt report; "
                    f"counts_exists={max_counts_path.exists()} "
                    f"failure_report_exists={max_failures_path.exists()}",
                )
                max_ephemeral = json.loads(max_failures_path.read_text(encoding="ascii"))
                self.assertEqual(len(max_ephemeral["failures"]), 54)
                self.assertLessEqual(max_failures_path.stat().st_size, 16 * 1024 - 256)
                self.assertEqual(
                    json.loads(max_counts_path.read_text(encoding="ascii"))["attempt_interrupted"],
                    54,
                )
                platform_live_qa_guard.prepare_public_browser_failure_diagnostics(
                    max_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                )
                max_public_line = platform_live_qa_guard.public_browser_counts_line(
                    max_gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                    persist_failure_report=True,
                )
                self.assertIn("run_status=failed", max_public_line)
                max_private_report = secret / "diagnostics" / f"{max_gate.name}.failure-v2.json"
                max_private_bytes = max_private_report.read_bytes()
                max_private_payload = json.loads(max_private_bytes)
                self.assertLessEqual(len(max_private_bytes), 16 * 1024)
                self.assertEqual(len(max_private_payload["failures"]), 54)
                self.assertTrue(all(
                    item["status"] == "interrupted"
                    and item["source_file"] == "tests/support/live-qa-sandbox.ts"
                    for item in max_private_payload["failures"]
                ))
                self.assertEqual(
                    len({(item["id"], item["project"], item["attempt"])
                         for item in max_private_payload["failures"]}),
                    54,
                )
                durable_events = [
                    event
                    for item in max_private_payload["failures"]
                    for event in item["page_errors"]
                ]
                self.assertTrue(durable_events)
                self.assertTrue(any(
                    event == [
                        "AggregateError",
                        "tournaments",
                        "turbopack-abcdefghijklm.js",
                        1_000_000,
                        1_000_000,
                    ]
                    for event in durable_events
                ))
                self.assertNotIn(b"private max-size assertion text", max_private_bytes)
                self.assertNotIn(b"https://old-sparky.com", max_private_bytes)
                with patch.object(platform_live_qa_guard, "assert_liveqa_idle"):
                    platform_live_qa_guard.remove_public_browser_gate(max_gate)
                self.assertFalse(max_gate.exists())
                self.assertEqual(max_private_report.read_bytes(), max_private_bytes)
                platform_live_qa_guard._prune_browser_failure_diagnostics(reserve_slots=0)
                self.assertEqual(max_private_report.read_bytes(), max_private_bytes)
                private_report = secret / "diagnostics" / f"{gate.name}.failure-v2.json"
                success_counts = dict(count_payload)
                success_counts.update(
                    run_status="passed",
                    logical_pass=48,
                    logical_fail=0,
                    attempt_pass=48,
                    attempt_fail=0,
                )
                success_failure_payload = dict(failure_payload)
                success_failure_payload.update(run_status="passed", failures=[])
                counts_path.write_text(json.dumps(success_counts) + "\n", encoding="ascii")
                counts_path.chmod(0o600)
                failures_path.write_text(
                    json.dumps(success_failure_payload) + "\n", encoding="ascii"
                )
                failures_path.chmod(0o600)
                success_line = platform_live_qa_guard.public_browser_counts_line(
                    gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                    persist_failure_report=True,
                )
                self.assertIn("run_status=passed", success_line)
                self.assertFalse(private_report.exists())
                counts_path.write_text(json.dumps(count_payload) + "\n", encoding="ascii")
                counts_path.chmod(0o600)
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                failure_payload["source_sha"] = "d" * 40
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable) as wrong_source:
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertIn("run_status=failed", wrong_source.exception.count_line)
                self.assertFalse(private_report.exists())
                failure_payload["source_sha"] = source_sha
                failure_payload["schema"] = True
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse(private_report.exists())
                failure_payload["schema"] = 2
                failure_payload["marker_sha256"] = "e" * 64
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse(private_report.exists())
                failure_payload["marker_sha256"] = marker_sha
                for valid_evidence in (
                    {
                        "page_error_state": "absent",
                        "page_errors_truncated": False,
                        "page_errors": [],
                    },
                    {
                        "page_error_state": "unavailable",
                        "page_errors_truncated": True,
                        "page_errors": [],
                    },
                    {
                        "page_error_state": "captured",
                        "page_errors_truncated": True,
                        "page_errors": [[
                                "AggregateError",
                                "tournaments",
                                "turbopack-abcdefghijklm.js",
                                1_000_000,
                                1_000_000,
                            ]] * 4,
                    },
                ):
                    platform_live_qa_guard._validate_page_error_evidence(valid_evidence)
                for invalid_evidence in (
                    {
                        "page_error_state": "captured",
                        "page_errors_truncated": False,
                        "page_errors": [],
                    },
                    {
                        "page_error_state": "absent",
                        "page_errors_truncated": True,
                        "page_errors": [],
                    },
                    {
                        "page_error_state": "captured",
                        "page_errors_truncated": False,
                        "page_errors": [["TypeError", "patch", "abcdefghijklm.js", True, 1]],
                    },
                    {
                        "page_error_state": "captured",
                        "page_errors_truncated": False,
                        "page_errors": [["TypeError", "patch", "abcdefghijklm.js", 1, 1]] * 5,
                    },
                ):
                    with self.assertRaises(platform_live_qa_guard.GuardError):
                        platform_live_qa_guard._validate_page_error_evidence(invalid_evidence)
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                page_error_record = failure_payload["failures"][0]
                for field, invalid_value in (
                    ("page_error_state", []),
                    ("page_errors", [[{}, "patch", None, None, None]]),
                ):
                    original_value = page_error_record[field]
                    page_error_record[field] = invalid_value
                    failures_path.write_text(
                        json.dumps(failure_payload) + "\n", encoding="ascii"
                    )
                    failures_path.chmod(0o600)
                    with self.assertRaises(
                        platform_live_qa_guard.BrowserFailureReportUnavailable
                    ):
                        platform_live_qa_guard.public_browser_counts_line(
                            gate,
                            source_sha=source_sha,
                            app_sha=app_sha,
                            marker_sha256=marker_sha,
                            persist_failure_report=True,
                        )
                    page_error_record[field] = original_value
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                with patch.object(
                    platform_live_qa_guard,
                    "_write_all",
                    side_effect=platform_live_qa_guard.GuardError("synthetic write failure"),
                ):
                    with self.assertRaises(
                        platform_live_qa_guard.BrowserFailureReportUnavailable
                    ) as write_failure:
                        platform_live_qa_guard.public_browser_counts_line(
                            gate,
                            source_sha=source_sha,
                            app_sha=app_sha,
                            marker_sha256=marker_sha,
                            persist_failure_report=True,
                        )
                self.assertIn("run_status=failed", write_failure.exception.count_line)
                self.assertFalse(private_report.exists())
                self.assertEqual(
                    sorted(os.listdir(secret / "diagnostics")),
                    sorted([max_private_report.name, user_private_report.name]),
                    "a failed second publication leaves prior valid records intact",
                )
                failure_payload["failures"][0]["status"] = "interrupted"
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                with self.assertRaisesRegex(
                    platform_live_qa_guard.BrowserFailureReportUnavailable,
                    "could not be published",
                ):
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertFalse(private_report.exists())
                failure_payload["failures"][0]["status"] = "failed"
                failures_path.write_text(json.dumps(failure_payload) + "\n", encoding="ascii")
                failures_path.chmod(0o600)
                public_line = platform_live_qa_guard.public_browser_counts_line(
                    gate,
                    source_sha=source_sha,
                    app_sha=app_sha,
                    marker_sha256=marker_sha,
                    persist_failure_report=True,
                )
                private_bytes = private_report.read_bytes()
                private_metadata = private_report.lstat()
                self.assertIn("run_status=failed", public_line)
                self.assertEqual(private_metadata.st_mode & 0o777, 0o600)
                self.assertEqual(private_metadata.st_nlink, 1)
                self.assertLessEqual(private_metadata.st_size, 16 * 1024)
                private_payload = json.loads(private_bytes)
                self.assertEqual(private_payload["schema"], 2)
                self.assertEqual(private_payload["gate"], gate.name)
                self.assertEqual(private_payload["source_sha"], source_sha)
                self.assertEqual(private_payload["app_sha"], app_sha)
                self.assertEqual(private_payload["marker_sha256"], marker_sha)
                self.assertEqual(
                    private_payload["failures"][0]["page_errors"],
                    [["TypeError", "patch", "abcdefghijklm.js", 12, 34]],
                )
                self.assertEqual(
                    private_payload["failures"][0]["source_file"],
                    "tests/support/live-qa-sandbox.ts",
                )
                self.assertEqual(private_payload["failures"][0]["source_line"], 2)
                self.assertEqual(private_payload["failures"][0]["source_kind"], "assertion")
                self.assertNotIn(b"assertion text", private_bytes)
                for sensitive_value in (
                    b"private pageerror text",
                    b"token=private",
                    b"12345",
                    b"https://old-sparky.com",
                    b"abcdefghijklm.js:12:34",
                ):
                    self.assertNotIn(sensitive_value, private_bytes)

                with self.assertRaises(platform_live_qa_guard.BrowserFailureReportUnavailable) as repeated:
                    platform_live_qa_guard.public_browser_counts_line(
                        gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                        persist_failure_report=True,
                    )
                self.assertEqual(repeated.exception.count_line, public_line)
                self.assertEqual(private_report.read_bytes(), private_bytes)

                with patch.object(platform_live_qa_guard, "assert_liveqa_idle"):
                    platform_live_qa_guard.remove_public_browser_gate(gate)
                self.assertFalse(gate.exists())
                self.assertEqual(private_report.read_bytes(), private_bytes)

                diagnostic_directory = secret / "diagnostics"
                now = time.time()
                os.utime(private_report, (now + 5, now + 5))
                old_name = "public-live-qa.00000000.failure-v1.json"
                for index in range(10):
                    old_gate = f"public-live-qa.{index:08d}"
                    record = {
                        "id": "chromium_sandbox",
                        "project": "live-desktop",
                        "status": "failed",
                        "attempt": 1,
                        "source_file": "tests/support/live-qa-sandbox.ts",
                        "source_line": 2,
                        "source_column": 3,
                        "source_kind": "assertion",
                    }
                    old_payload = {
                        "schema": 1,
                        "gate": old_gate,
                        "source_sha": source_sha,
                        "app_sha": app_sha,
                        "marker_sha256": marker_sha,
                        "payload_manifest_sha256": "f" * 64,
                        "payload_tree_sha256": "d" * 64,
                        "observed_at": "2026-10-20T00:00:00Z",
                        "run_status": "failed",
                        "failures": [record],
                    }
                    old_path = diagnostic_directory / f"{old_gate}.failure-v1.json"
                    old_path.write_text(json.dumps(old_payload) + "\n", encoding="ascii")
                    old_path.chmod(0o600)
                os.utime(
                    diagnostic_directory / old_name,
                    (now - 8 * 24 * 60 * 60, now - 8 * 24 * 60 * 60),
                )
                platform_live_qa_guard._prune_browser_failure_diagnostics(reserve_slots=1)
                retained_names = os.listdir(diagnostic_directory)
                self.assertLessEqual(len(retained_names), 9)
                self.assertIn(private_report.name, retained_names)
                self.assertNotIn(old_name, retained_names)

                recovery_gate = self._recreate_diagnostic_gate(run_root, gate.name)
                unsafe_store = diagnostic_directory
                unsafe_store.chmod(0o755)
                with self.assertRaisesRegex(platform_live_qa_guard.GuardError, "metadata"):
                    platform_live_qa_guard.prepare_public_browser_failure_diagnostics(
                        recovery_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    )

                retained_directory = secret / "diagnostics-retained"
                unsafe_store.rename(retained_directory)
                unsafe_store.symlink_to(retained_directory, target_is_directory=True)
                with self.assertRaisesRegex(platform_live_qa_guard.GuardError, "metadata"):
                    platform_live_qa_guard.prepare_public_browser_failure_diagnostics(
                        recovery_gate,
                        source_sha=source_sha,
                        app_sha=app_sha,
                        marker_sha256=marker_sha,
                    )

                self.assertEqual(counts_path.exists(), False)
                self.assertTrue(unsafe_store.is_symlink())
                self.assertEqual(
                    (retained_directory / private_report.name).read_bytes(), private_bytes
                )

    @staticmethod
    def _recreate_diagnostic_gate(run_root: Path, name: str) -> Path:
        gate = run_root / name
        (gate / "test-results").mkdir(parents=True, mode=0o700)
        return gate

    def test_live_dispatcher_lock_capability_uses_manifest_bound_canonical_helper(self) -> None:
        successful = SimpleNamespace(returncode=0)
        with patch.object(platform_live_user_qa_dispatch, "_trusted_directory_chain") as chain, \
            patch.object(
                platform_live_user_qa_dispatch,
                "_regular",
                return_value=SimpleNamespace(st_nlink=1),
            ), \
            patch.object(
                platform_live_user_qa_dispatch.subprocess,
                "run",
                return_value=successful,
            ) as run:
            platform_live_user_qa_dispatch._require_release_lock_supervisor()

        chain.assert_called_once_with(platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-c"])
        self.assertEqual(argv[3], "platform-release-lock-check")
        self.assertEqual(argv[4], str(platform_live_user_qa_dispatch.RELEASE_LOCK))
        self.assertIn("platform_release_lock_supervisor_holds", argv[2])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertNotIn("flock", argv)
        self.assertEqual(
            run.call_args.kwargs["env"],
            {"HOME": "/root", "LANG": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )

        with patch.object(platform_live_user_qa_dispatch, "_trusted_directory_chain"), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_regular",
                return_value=SimpleNamespace(st_nlink=1),
            ), \
            patch.object(
                platform_live_user_qa_dispatch.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1),
            ):
            with self.assertRaisesRegex(RuntimeError, "canonical release lock is not held"):
                platform_live_user_qa_dispatch._require_release_lock_supervisor()

    def test_direct_run_locked_marker_cannot_bypass_canonical_lock_check(self) -> None:
        sha = "a" * 40
        manifest = {
            "files": {
                "platform/tools/platform_workflow_remote_dispatch.py": "b" * 64,
            }
        }
        for arguments in (
            ["run-locked", sha],
            ["run-launch", sha, "https://old-sparky.com", "false", ""],
        ):
            with self.subTest(mode=arguments[0]):
                output = StringIO()
                with patch.object(platform_live_user_qa_dispatch.os, "geteuid", return_value=0), \
                    patch.object(platform_live_user_qa_dispatch, "_verify_install", return_value=manifest), \
                    patch.object(platform_live_user_qa_dispatch, "_load_verified_remote_dispatcher", return_value=object()), \
                    patch.object(platform_live_user_qa_dispatch, "_validate_source_binding_schema"), \
                    patch.object(platform_live_user_qa_dispatch, "_require_release_lock_supervisor", side_effect=RuntimeError("no lock")) as lock_check, \
                    patch.object(platform_live_user_qa_dispatch, "_validate_source_binding_under_lock") as tuple_check, \
                    patch.object(platform_live_user_qa_dispatch.os, "execve") as execve, \
                    patch.dict(os.environ, {"PLATFORM_RELEASE_LOCK_SUPERVISED": "1"}, clear=True), \
                    redirect_stdout(output):
                    self.assertEqual(platform_live_user_qa_dispatch.main(arguments), 1)
                lock_check.assert_called_once_with()
                tuple_check.assert_not_called()
                execve.assert_not_called()
                if arguments[0] == "run-launch":
                    self.assertEqual(
                        output.getvalue(),
                        "LIVE_LAUNCH_STATUS schema=2 status=failed "
                        f"stage=dispatch check=release_lock child_exit=1 source_sha={sha}\n",
                    )
                else:
                    self.assertEqual(
                        output.getvalue(),
                        "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
                        "stage=dispatch check=release_lock stdout_bytes=0 "
                        "stderr_bytes=0 truncated=false child_exit=1\n",
                    )

    def test_live_launch_pre_supervisor_failures_emit_only_closed_stage(self) -> None:
        sha = "a" * 40
        arguments = ["run-launch", sha, "https://old-sparky.com", "false", ""]
        cases = (
            ("source binding", "validation", 2, "_source_binding_context"),
            ("source binding I/O", "validation", 1, "_source_binding_context"),
            ("identity", "identity", 1, "geteuid"),
            ("installed generation", "trusted_generation", 1, "_verify_install"),
            ("installed dispatcher", "trusted_generation", 1, "_load_verified_remote_dispatcher"),
            ("source schema", "validation", 1, "_validate_source_binding_schema"),
            ("release lock", "dispatch", 1, "_require_release_lock_supervisor"),
            ("locked binding", "validation", 1, "_validate_source_binding_under_lock"),
            ("active payload", "trusted_generation", 1, "_read_manifest"),
            ("trusted directory chain", "trusted_generation", 1, "_trusted_directory_chain"),
            ("supervisor metadata", "trusted_generation", 1, "_regular"),
            ("supervisor exec", "dispatch", 1, "execve"),
        )
        check_by_hook = {
            "_source_binding_context": "source_binding",
            "geteuid": "root_uid",
            "_verify_install": "generation_manifest",
            "_load_verified_remote_dispatcher": "trusted_entry",
            "_validate_source_binding_schema": "source_binding_schema",
            "_require_release_lock_supervisor": "release_lock",
            "_validate_source_binding_under_lock": "source_binding_recheck",
            "_read_manifest": "trusted_entry",
            "_trusted_directory_chain": "trusted_entry",
            "_regular": "trusted_entry",
            "execve": "supervisor_exec",
        }

        def _check_for_hook(hook: str) -> str:
            return (
                "source_binding_io"
                if hook == "_source_binding_context"
                and current_case == "source binding I/O"
                else check_by_hook[hook]
            )

        for name, stage, child_exit, failing_hook in cases:
            current_case = name
            with self.subTest(boundary=name):
                output = StringIO()
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, {}, clear=True))
                    stack.enter_context(
                        patch.dict(os.environ, {"PLATFORM_RELEASE_LOCK_SUPERVISED": "1"})
                    )
                    stack.enter_context(redirect_stdout(output))
                    stack.enter_context(
                        patch.object(
                            platform_live_user_qa_dispatch.os,
                            "geteuid",
                            return_value=1000 if failing_hook == "geteuid" else 0,
                        )
                    )
                    defaults = {
                        "_source_binding_context": (sha, None, None),
                        "_verify_install": {"files": {}},
                        "_load_verified_remote_dispatcher": object(),
                        "_validate_source_binding_schema": None,
                        "_require_release_lock_supervisor": None,
                        "_validate_source_binding_under_lock": None,
                        "_trusted_directory_chain": None,
                        "_read_manifest": {"payload": "/trusted/payload"},
                        "_regular": SimpleNamespace(st_nlink=1),
                        "execve": None,
                    }
                    for name_to_patch, return_value in defaults.items():
                        target = (
                            platform_live_user_qa_dispatch.os
                            if name_to_patch == "execve"
                            else platform_live_user_qa_dispatch
                        )
                        if name_to_patch == failing_hook:
                            if name == "source binding I/O" or name_to_patch == "execve":
                                exception_type = OSError
                            else:
                                exception_type = RuntimeError
                            kwargs = {"side_effect": exception_type("private detail")}
                        else:
                            kwargs = {"return_value": return_value}
                        stack.enter_context(
                            patch.object(target, name_to_patch, **kwargs)
                        )
                    result = platform_live_user_qa_dispatch.main(arguments)
                self.assertEqual(result, child_exit)
                self.assertEqual(
                    output.getvalue(),
                    "LIVE_LAUNCH_STATUS schema=2 status=failed "
                    f"stage={stage} check={_check_for_hook(failing_hook)} "
                    f"child_exit={child_exit} source_sha={sha}\n",
                )
                self.assertNotIn("private detail", output.getvalue())
                self.assertEqual(
                    platform_workflow_remote_dispatch._parse_live_launch_status(
                        output.getvalue().encode("ascii"),
                        child_status=child_exit,
                        expected_sha=sha,
                    ),
                    ("failed", stage, _check_for_hook(failing_hook), child_exit),
                )

        output = StringIO()
        with patch.object(
            platform_live_user_qa_dispatch,
            "_source_binding_context",
            return_value=(sha, None, None),
        ), redirect_stdout(output):
            result = platform_live_user_qa_dispatch.main(
                ["run-launch", sha, "https://old-sparky.com", "false", "private\nvalue"]
            )
        self.assertEqual(result, 2)
        self.assertEqual(
            output.getvalue(),
            "LIVE_LAUNCH_STATUS schema=2 status=failed "
            f"stage=validation check=input_validation child_exit=2 source_sha={sha}\n",
        )
        self.assertNotIn("private", output.getvalue())
        invalid_sha_output = StringIO()
        with redirect_stdout(invalid_sha_output):
            self.assertEqual(
                platform_live_user_qa_dispatch.main(
                    ["run-launch", "invalid", "https://old-sparky.com", "false", ""]
                ),
                2,
            )
        self.assertEqual(invalid_sha_output.getvalue(), "")

        success_output = StringIO()
        with patch.dict(os.environ, {"PLATFORM_RELEASE_LOCK_SUPERVISED": "1"}, clear=True), \
            patch.object(platform_live_user_qa_dispatch.os, "geteuid", return_value=0), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_source_binding_context",
                return_value=(sha, None, None),
            ), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_verify_install",
                return_value={"files": {}},
            ), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_load_verified_remote_dispatcher",
                return_value=object(),
            ), \
            patch.object(platform_live_user_qa_dispatch, "_validate_source_binding_schema"), \
            patch.object(platform_live_user_qa_dispatch, "_require_release_lock_supervisor"), \
            patch.object(platform_live_user_qa_dispatch, "_validate_source_binding_under_lock"), \
            patch.object(platform_live_user_qa_dispatch, "_trusted_directory_chain"), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_read_manifest",
                return_value={"payload": "/trusted/payload"},
            ), \
            patch.object(
                platform_live_user_qa_dispatch,
                "_regular",
                return_value=SimpleNamespace(st_nlink=1),
            ), \
            patch.object(platform_live_user_qa_dispatch.os, "execve"), \
            redirect_stdout(success_output):
            self.assertEqual(platform_live_user_qa_dispatch.main(arguments), 0)
        self.assertEqual(success_output.getvalue(), "")

    def test_live_launch_status_parser_rejects_malformed_or_duplicate_markers(self) -> None:
        sha = "a" * 40
        valid = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=dispatch "
            f"check=none child_exit=1 source_sha={sha}\n"
        ).encode("ascii")
        for malformed in (
            valid + valid,
            valid + b"unexpected\n",
            valid.replace(b"check=none", b"check=unknown"),
            valid.replace(b"stage=dispatch", b"stage=unknown"),
            valid.replace(b"stage=dispatch", b"stage=trusted_entry"),
            valid.replace(b"stage=dispatch", b"stage=timeout"),
            valid.replace(b"source_sha=" + sha.encode("ascii"), b"source_sha=" + b"b" * 40),
        ):
            with self.subTest(marker=malformed[:80]):
                self.assertIsNone(
                    platform_workflow_remote_dispatch._parse_live_launch_status(
                        malformed,
                        child_status=1,
                        expected_sha=sha,
                    )
                )

    def test_live_dispatcher_imports_under_isolated_no_bytecode_python(self) -> None:
        result = subprocess.run(
            [
                "/usr/bin/python3.12",
                "-I",
                "-B",
                str(TOOLS_ROOT / "platform_live_user_qa_dispatch.py"),
                "invalid-mode",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            env={"HOME": "/root", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 2, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")

    def test_control_email_json_stdin_is_closed_bounded_and_redacted(self) -> None:
        valid = '{"schema":1,"control_email":"Control+qa@example.invalid"}\n'
        stdout = StringIO()
        stderr = StringIO()
        with patch.object(
            platform_workflow_input_guard.sys,
            "stdin",
            TextIOWrapper(BytesIO(valid.encode("ascii")), encoding="ascii"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(
                platform_workflow_input_guard.main(["control-email-json-stdin"]),
                0,
            )
        self.assertEqual(stdout.getvalue(), "control+qa@example.invalid\n")
        self.assertEqual(stderr.getvalue(), "")

        invalid_payloads = (
            '{"schema":1,"control_email":"private@example.invalid","extra":0}\n',
            '{"schema":1,"schema":1,"control_email":"private@example.invalid"}\n',
            '{"schema":true,"control_email":"private@example.invalid"}\n',
            '{"schema":1,"control_email":"bad;id@example.invalid"}\n',
            "{" + " " * 1_024 + "}\n",
        )
        for raw in invalid_payloads:
            with self.subTest(length=len(raw)):
                stdout = StringIO()
                stderr = StringIO()
                with patch.object(
                    platform_workflow_input_guard.sys,
                    "stdin",
                    TextIOWrapper(BytesIO(raw.encode("ascii")), encoding="ascii"),
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(
                        platform_workflow_input_guard.main(
                            ["control-email-json-stdin"]
                        ),
                        2,
                    )
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "workflow input is invalid\n")
                self.assertNotIn("private@example.invalid", stderr.getvalue())

    def test_cpu_diagnostic_dispatch_uses_closed_canonical_stdin(self) -> None:
        prepare = {
            "schema": 1,
            "operation": "prepare",
            "run_id": "a" * 32,
            "source_sha": "b" * 40,
            "workload": "authenticated_workspace_read_pair_v1",
            "off_start_ms": 60_000,
            "off_end_ms": 80_000,
            "on_start_ms": 85_000,
            "on_end_ms": 105_000,
        }
        cleanup = {"schema": 1, "operation": "cleanup", "run_id": "a" * 32}
        for payload in (prepare, cleanup):
            raw = json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ) + "\n"
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(
                    platform_workflow_remote_dispatch.sys,
                    "stdin",
                    TextIOWrapper(BytesIO(raw.encode("ascii")), encoding="ascii"),
                ),
                patch.object(
                    platform_workflow_remote_dispatch,
                    "_run_cpu_diagnostic_plan",
                    return_value=0,
                ) as invoke,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["cpu-diagnostic-plan"]), 0
                )
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(invoke.call_args.args[0], payload)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

        for malformed in (
            b'{"schema":1,"schema":1,"operation":"cleanup","run_id":"' + b"a" * 32 + b'"}\n',
            b'{"schema":1,"operation":"cleanup","run_id":"' + b"a" * 32 + b'","extra":true}\n',
            b'{"schema":1,"operation":"prepare","run_id":"' + b"a" * 32 + b'","source_sha":"' + b"b" * 40 + b'","workload":"authenticated_workspace_read_pair_v1","off_start_ms":60000,"off_end_ms":80000,"on_start_ms":85000,"on_end_ms":105000,"release_slug":"caller"}\n',
        ):
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(
                    platform_workflow_remote_dispatch.sys,
                    "stdin",
                    TextIOWrapper(BytesIO(malformed), encoding="ascii"),
                ),
                patch.object(
                    platform_workflow_remote_dispatch, "_run_cpu_diagnostic_plan"
                ) as invoke,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["cpu-diagnostic-plan"]), 2
                )
            invoke.assert_not_called()
            self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")

        rows = []
        for service, phase, targets, cpu_ns in (
            ("api", "off", 2, 4_000_000_000),
            ("api", "on", 2, 4_200_000_000),
            ("web", "off", 1, 1_000_000_000),
            ("web", "on", 1, 1_100_000_000),
        ):
            rows.append({
                "service": service, "phase": phase, "expected_targets": targets,
                "observed_targets": targets, "event_count": targets, "cpu_ns": cpu_ns,
                "window_ms_min": 20_000, "window_ms_max": 20_000,
                "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                "duplicate_count": 0, "timing_complete": True,
            })
        complete_helper_result = {
            "status": "expired_plans_removed", "service_count": 2,
            "workload": "authenticated_workspace_read_pair_v1",
            "usage_status": "complete", "usage_reason": "none", "usage_rows": rows,
            "profile_status": "complete", "profile_reason": "none",
            "profile_rows": [
                {
                    "service": "api", "expected_targets": 2, "observed_targets": 2,
                    "event_count": 2, "timer": "thread_cpu", "observation_unit": "calls",
                    "total_cpu_us": 2_000_000, "sample_count": None,
                    "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                    "elapsed_ms_min": 20_000, "elapsed_ms_max": 20_000,
                    "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                    "categories": [{
                        "category": "repo.get_current_user", "cpu_us": 50_000,
                        "observations": 100,
                    }],
                },
                {
                    "service": "web", "expected_targets": 1, "observed_targets": 1,
                    "event_count": 1, "timer": "v8_cpu", "observation_unit": "samples",
                    "total_cpu_us": 1_000_000, "sample_count": 100,
                    "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                    "elapsed_ms_min": 20_000, "elapsed_ms_max": 20_000,
                    "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                    "categories": [{"category": "other", "cpu_us": 1_000_000, "observations": 100}],
                },
            ],
            "memory_status": "incomplete", "memory_reason": "incomplete",
            "cgroup_memory_status": "unknown",
            "cgroup_memory_reason": "hierarchy_unavailable", "memory_rows": [],
        }
        helper_result = {
            "status": "expired_plans_removed", "service_count": 0, "workload": None,
            "usage_status": "unavailable", "usage_reason": "missing", "usage_rows": [],
            "profile_status": "unavailable", "profile_reason": "missing", "profile_rows": [],
            "memory_status": "unavailable", "memory_reason": "unavailable", "memory_rows": [],
            "cgroup_memory_status": "unknown",
            "cgroup_memory_reason": "hierarchy_unavailable",
        }
        real_popen = subprocess.Popen

        def invoke_with_helper_result(result: dict[str, object]) -> tuple[int, str]:
            helper_json = (
                "CPU_DIAGNOSTIC_PLAN "
                + json.dumps(result, sort_keys=True, separators=(",", ":"))
                + "\n"
            )

            def fake_popen(command, **kwargs):
                self.assertEqual(
                    command,
                    [
                        platform_workflow_remote_dispatch.SUDO, "-n", "--",
                        platform_workflow_remote_dispatch.SYSTEM_PYTHON, "-I", "-B",
                        str(platform_workflow_remote_dispatch.CPU_DIAGNOSTIC_PLAN_HELPER),
                        "cleanup-stdin",
                    ],
                )
                return real_popen(
                    [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.stdout.write(" + repr(helper_json) + ")"],
                    **kwargs,
                )

            stdout = StringIO()
            with (
                patch.object(platform_workflow_remote_dispatch, "_trusted_python_helper", return_value=True),
                patch.object(platform_workflow_remote_dispatch.subprocess, "Popen", side_effect=fake_popen),
                redirect_stdout(stdout),
            ):
                status = platform_workflow_remote_dispatch._run_cpu_diagnostic_plan(cleanup)
            return status, stdout.getvalue()

        dispatch_status, dispatch_output = invoke_with_helper_result(helper_result)
        self.assertEqual(dispatch_status, 0)
        self.assertTrue(dispatch_output.startswith("CPU_DIAGNOSTIC_PLAN "))
        projected = json.loads(dispatch_output.removeprefix("CPU_DIAGNOSTIC_PLAN "))
        self.assertEqual(projected["service_count"], 0)
        self.assertIsNone(projected["workload"])
        self.assertEqual(projected["usage_status"], "unavailable")
        self.assertEqual(projected["usage_rows"], [])
        self.assertEqual(projected["profile_status"], "unavailable")
        self.assertEqual(projected["profile_rows"], [])

        dispatch_status, dispatch_output = invoke_with_helper_result(complete_helper_result)
        self.assertEqual(dispatch_status, 0)
        self.assertTrue(dispatch_output.startswith("CPU_DIAGNOSTIC_PLAN "))
        projected = json.loads(dispatch_output.removeprefix("CPU_DIAGNOSTIC_PLAN "))
        self.assertEqual(projected["usage_rows"], rows)

        def assert_cleanup_failure_marker(output: str, *, stage: str) -> None:
            self.assertTrue(output.startswith("CPU_DIAGNOSTIC_PLAN_FAILURE "))
            self.assertTrue(output.endswith("\n"))
            self.assertLessEqual(
                len(output.encode("ascii")),
                platform_workflow_remote_dispatch.CPU_DIAGNOSTIC_FAILURE_CAP,
            )
            marker = json.loads(output.removeprefix("CPU_DIAGNOSTIC_PLAN_FAILURE "))
            self.assertEqual(set(marker), {
                "schema", "stage", "child_return_code", "child_stdout_shape",
                "child_stdout_bytes",
            })
            self.assertIs(type(marker["schema"]), int)
            self.assertEqual(marker["schema"], 1)
            self.assertIs(type(marker["stage"]), str)
            self.assertEqual(marker["stage"], stage)
            self.assertIs(type(marker["child_return_code"]), int)
            self.assertEqual(marker["child_return_code"], 0)
            self.assertIs(type(marker["child_stdout_shape"]), str)
            self.assertEqual(marker["child_stdout_shape"], "single_line")
            self.assertIs(type(marker["child_stdout_bytes"]), int)
            self.assertGreater(marker["child_stdout_bytes"], 0)
            self.assertLessEqual(
                marker["child_stdout_bytes"],
                platform_workflow_remote_dispatch.CPU_DIAGNOSTIC_OUTPUT_CAP,
            )

        real_popen = subprocess.Popen

        def invoke_refusal(line: bytes) -> tuple[int, str]:
            def fake_popen(command, **kwargs):
                self.assertEqual(
                    command,
                    [
                        platform_workflow_remote_dispatch.SUDO, "-n", "--",
                        platform_workflow_remote_dispatch.SYSTEM_PYTHON, "-I", "-B",
                        str(platform_workflow_remote_dispatch.CPU_DIAGNOSTIC_PLAN_HELPER),
                        "prepare-stdin",
                    ],
                )
                script = (
                    "import sys; sys.stdin.buffer.read(); "
                    f"sys.stdout.buffer.write({line!r}); "
                    "sys.stderr.write('PRIVATE_SENTINEL'); sys.exit(2)"
                )
                return real_popen([sys.executable, "-c", script], **kwargs)

            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(platform_workflow_remote_dispatch, "_trusted_python_helper", return_value=True),
                patch.object(platform_workflow_remote_dispatch.subprocess, "Popen", side_effect=fake_popen),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = platform_workflow_remote_dispatch._run_cpu_diagnostic_plan(prepare)
            self.assertNotIn("PRIVATE_SENTINEL", stdout.getvalue() + stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            return status, stdout.getvalue()

        refusal_status, refusal_output = invoke_refusal(
            b"CPU_DIAGNOSTIC_PLAN status=refused class=PlanError\n"
        )
        self.assertEqual(refusal_status, 2)
        self.assertEqual(
            refusal_output,
            "CPU_DIAGNOSTIC_PLAN status=refused class=PlanError\n",
        )
        bad_refusal_status, bad_refusal_output = invoke_refusal(
            b"CPU_DIAGNOSTIC_PLAN status=refused class=PrivateError\n"
        )
        self.assertEqual(bad_refusal_status, 2)
        self.assertEqual(bad_refusal_output, "")

        duplicate_result = dict(complete_helper_result)
        duplicate_rows = [dict(row) for row in rows]
        duplicate_rows[0]["duplicate_count"] = 1
        duplicate_result["usage_rows"] = duplicate_rows
        bad_status, bad_output = invoke_with_helper_result(duplicate_result)
        self.assertEqual(bad_status, 2)
        assert_cleanup_failure_marker(bad_output, stage="usage_aggregate")
        private_label = dict(complete_helper_result)
        private_profiles = [dict(row) for row in complete_helper_result["profile_rows"]]
        private_profiles[0]["categories"] = [{
            "category": "raw_function_name", "cpu_us": 1, "observations": 1,
        }]
        private_label["profile_rows"] = private_profiles
        bad_status, bad_output = invoke_with_helper_result(private_label)
        self.assertEqual(bad_status, 2)
        assert_cleanup_failure_marker(bad_output, stage="profile_categories")
        self.assertNotIn("raw_function_name", bad_output)

    def test_cpu_python_helper_accepts_read_only_release_script_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            tools_dir.mkdir()
            helper = tools_dir / "platform_cpu_diagnostic_plan.py"
            helper.write_text("# immutable helper fixture\n", encoding="ascii")
            helper.chmod(0o644)
            real_lstat = Path.lstat

            def root_shaped_lstat(path: Path):
                actual = real_lstat(path)
                if path != helper:
                    return actual
                return SimpleNamespace(
                    st_mode=actual.st_mode,
                    st_uid=0,
                    st_gid=0,
                    st_nlink=1,
                )

            def accepted(mode: int, *, uid: int = 0, gid: int = 0,
                         nlink: int = 1, file_type: int = stat.S_IFREG) -> bool:
                with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_dir):
                    with patch.object(
                        Path,
                        "lstat",
                        return_value=SimpleNamespace(
                            st_mode=file_type | mode,
                            st_uid=uid,
                            st_gid=gid,
                            st_nlink=nlink,
                        ),
                    ):
                        return platform_workflow_remote_dispatch._trusted_python_helper(helper)

            with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_dir), \
                 patch.object(Path, "lstat", root_shaped_lstat):
                self.assertTrue(platform_workflow_remote_dispatch._trusted_python_helper(helper))
            self.assertFalse(accepted(0o644, uid=1000))
            self.assertFalse(accepted(0o644, gid=1000))
            self.assertFalse(accepted(0o644, nlink=2))
            self.assertFalse(accepted(0o664))
            self.assertFalse(accepted(0o200))
            self.assertFalse(accepted(0o644, file_type=stat.S_IFLNK))
            with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_dir.parent):
                self.assertFalse(platform_workflow_remote_dispatch._trusted_python_helper(helper))

    def test_cpu_python_helper_runs_real_0644_file_through_fixed_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools_dir = root / "tools"
            tools_dir.mkdir()
            helper = tools_dir / "platform_cpu_diagnostic_plan.py"
            helper.write_text(
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write('CPU_DIAGNOSTIC_PLAN {\\\"api_target_count\\\":2,\\\"release_slug\\\":\\\"release-123456789012\\\",\\\"status\\\":\\\"prepared\\\",\\\"web_target_count\\\":1}\\n')\n",
                encoding="ascii",
            )
            helper.chmod(0o644)
            sudo_shim = root / "sudo-shim"
            sudo_shim.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = -n ] && [ \"$2\" = -- ] || exit 125\n"
                "shift 2\nexec \"$@\"\n",
                encoding="ascii",
            )
            sudo_shim.chmod(0o755)
            real_lstat = Path.lstat

            def root_shaped_lstat(path: Path):
                actual = real_lstat(path)
                if path != helper:
                    return actual
                return SimpleNamespace(
                    st_mode=actual.st_mode, st_uid=0, st_gid=0, st_nlink=1
                )

            payload = {
                "schema": 1, "operation": "prepare", "run_id": "a" * 32,
                "source_sha": "b" * 40, "workload": "authenticated_workspace_read_pair_v1",
                "off_start_ms": 60_000, "off_end_ms": 80_000,
                "on_start_ms": 85_000, "on_end_ms": 105_000,
            }
            stdout = StringIO()
            real_popen = subprocess.Popen
            children = []

            def checked_popen(command, **kwargs):
                try:
                    child = real_popen(command, **kwargs)
                    children.append(child)
                    return child
                except OSError as exc:
                    raise AssertionError(type(exc).__name__) from None

            with (
                patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_dir),
                patch.object(platform_workflow_remote_dispatch, "CPU_DIAGNOSTIC_PLAN_HELPER", helper),
                patch.object(platform_workflow_remote_dispatch, "SUDO", str(sudo_shim)),
                patch.object(Path, "lstat", root_shaped_lstat),
                patch.object(platform_workflow_remote_dispatch.subprocess, "Popen", side_effect=checked_popen),
                redirect_stdout(stdout),
            ):
                status = platform_workflow_remote_dispatch._run_cpu_diagnostic_plan(payload)
                self.assertEqual(
                    status, 0,
                    f"stdout={stdout.getvalue()!r} child_rc={[child.returncode for child in children]!r}",
                )
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].returncode, 0)
            self.assertEqual(
                stdout.getvalue(),
                "CPU_DIAGNOSTIC_PLAN status=prepared targets=3 release_slug=release-123456789012\n",
            )

    def test_external_fixture_forwards_control_identity_only_on_stdin(self) -> None:
        class CapturedInput:
            def __init__(self) -> None:
                self.data = b""
                self.closed = False

            def write(self, value: bytes) -> int:
                self.data += value
                return len(value)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        class FailingInput(CapturedInput):
            def write(self, value: bytes) -> int:
                raise OSError("private control pipe failed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "platform_production_external_fixture_qa.sh"
            helper.write_text("#!/bin/sh\n", encoding="ascii")
            helper.chmod(0o755)
            child = Mock(pid=1234)
            child.stdin = CapturedInput()
            payload = {
                "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
                "schema": "1",
                "target_sha": "a" * 40,
                "control_email": "control@example.invalid",
                "setup_concurrency": "8",
                "run_id": "123456",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "14",
                "timeout_diagnostics": "false",
            }
            payload_sha = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
            ).hexdigest()
            with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", root), \
                patch.object(platform_workflow_remote_dispatch, "EXTERNAL_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch, "SUDO", "/usr/bin/sudo"), \
                patch.object(
                    platform_workflow_remote_dispatch.subprocess,
                    "Popen",
                    return_value=child,
                ) as popen:
                self.assertEqual(
                    platform_workflow_remote_dispatch._external_fixture(
                        payload, run_id="123456", run_attempt="2"
                    ), 0
                )
            command = popen.call_args.args[0]
            self.assertEqual(
                command,
                [
                    "/usr/bin/sudo", "-n", "--", str(helper),
                    payload["confirmation"], payload["target_sha"],
                    payload["setup_concurrency"], payload["run_id"],
                    payload["profile"], payload["tournament_count"],
                    payload["users_per_tournament"], payload["timeout_diagnostics"],
                    "--run-attempt", "2", "--input-handoff-sha256", payload_sha,
                ],
            )
            self.assertNotIn(payload["control_email"], command)
            self.assertEqual(
                json.loads(child.stdin.data.decode("ascii")),
                {"schema": 1, "control_email": payload["control_email"]},
            )
            self.assertTrue(child.stdin.closed)

            failed_child = Mock(pid=5678)
            failed_child.stdin = FailingInput()
            with patch.object(
                platform_workflow_remote_dispatch.subprocess,
                "Popen",
                return_value=failed_child,
            ) as failed_popen, patch.object(
                platform_workflow_remote_dispatch,
                "_terminate_process_group",
            ) as terminate:
                self.assertEqual(
                    platform_workflow_remote_dispatch._external_fixture(
                        payload, run_id="123456", run_attempt="2"
                    ), 2
                )
            terminate.assert_called_once_with(failed_child)
            self.assertTrue(failed_child.stdin.closed)
            failed_command = failed_popen.call_args.args[0]
            self.assertNotIn(payload["control_email"], failed_command)

            mismatched_child = Mock(pid=9012)
            mismatched_child.stdin = CapturedInput()
            with patch.object(
                platform_workflow_remote_dispatch.subprocess,
                "Popen",
                return_value=mismatched_child,
            ) as mismatched_popen:
                self.assertEqual(
                    platform_workflow_remote_dispatch._external_fixture(
                        payload, run_id="123457", run_attempt="2"
                    ), 2
                )
            mismatched_popen.assert_not_called()

            helper_source = (
                PLATFORM_ROOT / "tools" / "platform_production_external_fixture_qa.sh"
            ).read_text(encoding="utf-8")
            self.assertIn('"fixture_kind": fixture_kind', helper_source)
            self.assertIn('destination = run_root / "diagnostic-abort-binding.json"', helper_source)
            confirmation_function = re.search(
                r"(?ms)^validate_fixture_confirmation\(\) \{\n.*?^\}\n",
                helper_source,
            )
            self.assertIsNotNone(confirmation_function)
            function_source = confirmation_function.group(0)
            supervisor_function = re.search(
                r"(?ms)^write_supervisor_exit\(\) \{\n.*?^\}\n",
                helper_source,
            )
            self.assertIsNotNone(supervisor_function)
            self.assertLess(
                helper_source.index("export_created=1"),
                helper_source.index("validate_fixture_confirmation || exit 1"),
            )
            self.assertLess(
                helper_source.index("validate_fixture_confirmation || exit 1"),
                helper_source.index("platform_prepare_external_vote_fixture.py"),
            )

            with tempfile.TemporaryDirectory() as temporary:
                export_dir = Path(temporary) / "exports" / "run"
                rejected_script = "\n".join(
                    (
                        "platform_retained_load_lock_close() { :; }",
                        supervisor_function.group(0),
                        function_source,
                        'export_dir="$TEST_EXPORT_DIR"',
                        'supervisor_exit_path="$export_dir/supervisor.exit"',
                        'mkdir -p "$export_dir"',
                        'chmod 700 "$export_dir"',
                        'export_dir_identity="$(/usr/bin/stat -c \'%d:%i:%u:%g:%a:%h\' -- "$export_dir")"',
                        "export_created=1",
                        'export_uid="$(id -u)"; export_gid="$(id -g)"',
                        "progress_enabled=0",
                        "trap write_supervisor_exit EXIT",
                        'confirmation="$CPU_DIAGNOSTIC_CONFIRMATION"',
                        'fixture_kind="cpu_diagnostic_only_v1"',
                        'timeout_diagnostics=false',
                        'external_vote_tournament_count=2',
                        'external_vote_users_per_tournament=14',
                        "validate_fixture_confirmation || exit 1",
                        'printf called > "$TEST_DB_PREPARED"',
                    )
                )
                rejected_environment = {
                    **os.environ,
                    "CPU_DIAGNOSTIC_CONFIRMATION": "RUN-PRODUCTION-CPU-DIAGNOSTIC",
                    "TEST_EXPORT_DIR": str(export_dir),
                    "TEST_DB_PREPARED": str(Path(temporary) / "db-prepared"),
                }
                rejected = subprocess.run(
                    ["bash", "-c", rejected_script],
                    env=rejected_environment,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertEqual((export_dir / "supervisor.exit").read_bytes(), b"1\n")
                marker = (export_dir / "supervisor.exit").lstat()
                self.assertEqual(stat.S_IMODE(marker.st_mode), 0o600)
                self.assertEqual(marker.st_nlink, 1)
                self.assertFalse((export_dir / "ready").exists())
                self.assertFalse((export_dir / "manifest.json").exists())
                self.assertFalse(Path(rejected_environment["TEST_DB_PREPARED"]).exists())

            def run_confirmation(
                confirmation: str, timeout: str, tournaments: str, users: str,
                fixture_kind: str,
            ) -> int:
                environment = {
                    **os.environ,
                    "EXTERNAL_CONFIRMATION": "RUN-PRODUCTION-EXTERNAL-LOAD",
                    "TIMEOUT_DIAGNOSTICS_CONFIRMATION": "RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS",
                    "CPU_DIAGNOSTIC_CONFIRMATION": "RUN-PRODUCTION-CPU-DIAGNOSTIC",
                    "SSR_CAPACITY_CONFIRMATION": "RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC",
                    "confirmation": confirmation,
                    "fixture_kind": fixture_kind,
                    "timeout_diagnostics": timeout,
                    "external_vote_tournament_count": tournaments,
                    "external_vote_users_per_tournament": users,
                }
                completed = subprocess.run(
                    ["bash", "-c", function_source + "validate_fixture_confirmation\n"],
                    env=environment,
                    capture_output=True,
                    check=False,
                )
                self.assertNotIn(b"control@example.invalid", completed.stderr)
                return completed.returncode

            self.assertEqual(
                run_confirmation("RUN-PRODUCTION-EXTERNAL-LOAD", "false", "1", "14", "external_load"),
                0,
            )
            self.assertEqual(
                run_confirmation("RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS", "true", "1", "14", "external_load"),
                0,
            )
            self.assertEqual(
                run_confirmation("RUN-PRODUCTION-CPU-DIAGNOSTIC", "false", "1", "14", "cpu_diagnostic_only_v1"),
                0,
            )
            self.assertEqual(
                run_confirmation("RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC", "false", "1", "14", "ssr_capacity_diagnostic_v1"),
                0,
            )
            self.assertEqual(
                run_confirmation("RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC", "false", "1", "14", "ssr_html_capacity_diagnostic_v1"),
                0,
            )
            self.assertNotEqual(
                run_confirmation("RUN-PRODUCTION-CPU-DIAGNOSTIC", "false", "1", "14", "ssr_html_capacity_diagnostic_v1"),
                0,
            )
            self.assertNotEqual(
                run_confirmation("RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC", "false", "1", "14", "cpu_diagnostic_only_v1"),
                0,
            )
            self.assertNotEqual(
                run_confirmation("RUN-PRODUCTION-CPU-DIAGNOSTIC", "false", "2", "14", "cpu_diagnostic_only_v1"),
                0,
            )
            self.assertNotEqual(
                run_confirmation("RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC", "true", "1", "14", "ssr_capacity_diagnostic_v1"),
                0,
            )
            self.assertNotEqual(
                run_confirmation("RUN-PRODUCTION-CPU-DIAGNOSTIC", "false", "1", "15", "cpu_diagnostic_only_v1"),
                0,
            )

    def test_release_baseline_input_requires_an_exact_integer_schema(self) -> None:
        valid = {
            "schema": 1,
            "source_sha": "a" * 40,
            "release_slug": "gha-35511236041-1-87547df2abd4",
            "release_json_sha256": "b" * 64,
            "current_link_dev": 1,
            "current_link_ino": 2,
            "release_dev": 3,
            "release_ino": 4,
            "pending_operation": False,
        }
        self.assertEqual(
            platform_workflow_input_guard.validate_release_baseline_payload(valid),
            valid,
        )
        with self.assertRaises(WorkflowInputError):
            platform_workflow_input_guard.validate_release_baseline_payload(
                {**valid, "schema": True}
            )

    def test_host_tools_handoff_is_closed_and_binds_contract(self) -> None:
        valid = {
            "schema": 1,
            "target_sha": "a" * 40,
            "host_tools_sha": "b" * 40,
            "artifact_id": "123456",
            "artifact_name": "platform-host-tools-bundle-123456-2",
            "artifact_size": "4096",
            "artifact_digest": "c" * 64,
            "bundle_sha256": "d" * 64,
            "manifest_sha256": "e" * 64,
            "capabilities_sha256": "f" * 64,
            "files_contract_sha256": "0" * 64,
            "modes_contract_sha256": "1" * 64,
            "signer_workflow": platform_workflow_input_guard.HOST_TOOLS_SIGNER_WORKFLOW,
            "source_ref": "refs/heads/dev",
            "source_digest": "a" * 40,
            "attestation_run_id": "123456",
            "attestation_run_attempt": "2",
            "attestation_job_id": "654321",
        }
        self.assertEqual(
            validate_host_tools_payload(valid),
            {**{key: str(value) for key, value in valid.items()}, "schema": "1"},
        )
        for field, value in (
            ("artifact_digest", "not-a-digest"),
            ("bundle_sha256", "0" * 63),
            ("artifact_name", "platform-host-tools-bundle-123456-0"),
            ("artifact_name", "platform-host-tools-bundle-654321-2"),
            ("source_digest", "b" * 40),
            ("signer_workflow", "wrong/repository/workflow.yml"),
            ("artifact_size", "0"),
            ("attestation_job_id", None),
        ):
            with self.subTest(field=field):
                with self.assertRaises(WorkflowInputError):
                    validate_host_tools_payload({**valid, field: value})

    def test_live_launch_workflow_delegates_to_server_supervisor(self) -> None:
        source = (REPO_ROOT / ".github/workflows/platform-live-launch.yml").read_text(
            encoding="utf-8"
        )
        browser_spec = (
            REPO_ROOT / "platform/apps/platform_web/tests/smoke/live-launch.spec.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("pageErrorCollectorReady: boolean", browser_spec)
        self.assertIn("pageErrorCollectorReady: false", browser_spec)
        attachment_guard = browser_spec.index(
            "if (!evidence.pageErrorCollectorReady) return;"
        )
        self.assertLess(
            browser_spec.index("async function attachPageErrorEvidence("),
            attachment_guard,
        )
        attach_function_end = browser_spec.index(
            "async function currentIndexedPatchId(", attachment_guard
        )
        attach_function = browser_spec[
            browser_spec.index("async function attachPageErrorEvidence("):
            attach_function_end
        ]
        attachment_guard_in_function = attach_function.index(
            "if (!evidence.pageErrorCollectorReady) return;"
        )
        self.assertLess(
            attachment_guard_in_function,
            attach_function.index("LiveCountReporter.createPageErrorAttachment({"),
        )
        self.assertLess(
            attachment_guard_in_function,
            attach_function.index("testInfo.attach(attachment.name, {"),
        )
        pageerror_listener = browser_spec.index('page.on("pageerror", (error) => {')
        collector_ready = browser_spec.index(
            "evidence.pageErrorCollectorReady = true;", pageerror_listener
        )
        next_listener = browser_spec.index('page.on("requestfailed", (request) => {', pageerror_listener)
        self.assertLess(pageerror_listener, collector_ready)
        self.assertLess(collector_ready, next_listener)
        after_each_start = browser_spec.index("test.afterEach(async ({ page }, testInfo) => {")
        after_each_end = browser_spec.index("\n});", after_each_start)
        self.assertIn(
            "if (evidence) await attachPageErrorEvidence(page, testInfo, evidence);",
            browser_spec[after_each_start:after_each_end],
        )
        browser_config = (
            REPO_ROOT / "platform/apps/platform_web/playwright.live.config.ts"
        ).read_text(encoding="utf-8")
        supervisor = (
            TOOLS_ROOT / "platform_live_launch_supervisor.sh"
        ).read_text(encoding="utf-8")
        browser_wrapper = (TOOLS_ROOT / "platform_live_browser_qa.sh").read_text(
            encoding="utf-8"
        )
        engine_preflight = (
            REPO_ROOT
            / "platform/apps/platform_web/tests/support/live-engine-preflight.cjs"
        ).read_text(encoding="utf-8")
        user_wrapper = (TOOLS_ROOT / "platform_live_user_qa.sh").read_text(
            encoding="utf-8"
        )
        # The workflow embeds a second closed marker grammar. Keep every
        # emitted stage/check in lockstep with that parser and its failure gate.
        marker_pattern_start = source.index("marker_pattern = re.compile(")
        marker_pattern_end = source.index("count_fields = (", marker_pattern_start)
        marker_pattern_literals = re.findall(
            r'rb"([^"]*)"', source[marker_pattern_start:marker_pattern_end]
        )
        self.assertTrue(marker_pattern_literals)
        workflow_marker_pattern = re.compile(
            "".join(marker_pattern_literals).encode("ascii")
        )

        def marker_values(pattern: bytes, field: str) -> set[str]:
            field_bytes = field.encode("ascii")
            match = re.search(
                rb"\b" + field_bytes + rb"=\((?:\?P<" + field_bytes + rb">)?([^)]*)\)",
                pattern,
            )
            self.assertIsNotNone(match, f"missing closed {field} alternatives")
            assert match is not None
            return set(match.group(1).decode("ascii").split("|"))

        workflow_stages = marker_values(workflow_marker_pattern.pattern, "stage")
        workflow_checks = marker_values(workflow_marker_pattern.pattern, "check")
        remote_pattern = platform_workflow_remote_dispatch.LIVE_LAUNCH_STATUS_RE.pattern
        remote_stages = marker_values(remote_pattern, "stage")
        remote_checks = marker_values(remote_pattern, "check")

        canonical_failure_stages = set(
            platform_workflow_remote_dispatch.LIVE_LAUNCH_FAILURE_STAGES
        )
        canonical_stages = canonical_failure_stages | {"complete"}
        canonical_checks = set(platform_workflow_remote_dispatch.LIVE_LAUNCH_CHECK_IDS)
        self.assertSetEqual(remote_stages, canonical_stages)
        self.assertSetEqual(remote_checks, canonical_checks)
        self.assertSetEqual(workflow_stages, canonical_stages)
        self.assertSetEqual(workflow_checks, canonical_checks)
        failure_stage_start = source.index("stage in {", marker_pattern_end)
        failure_stage_end = source.index("}", failure_stage_start)
        workflow_failure_stages = set(
            re.findall(r'"([a-z_]+)"', source[failure_stage_start:failure_stage_end])
        )
        self.assertSetEqual(workflow_failure_stages, canonical_failure_stages)
        supervisor_stages = set(re.findall(r'launch_stage="([a-z_]+)"', supervisor)) | {
            "complete"
        }
        supervisor_checks = set(re.findall(r'launch_check="([a-z_]+)"', supervisor)) | {
            "none"
        }
        self.assertTrue(supervisor_stages <= workflow_stages)
        self.assertTrue(supervisor_checks <= workflow_checks)

        route_cases = re.findall(r'^  \{ path: "[^"]+", text:', browser_spec, re.MULTILINE)
        explicit_tests = re.findall(r"^test\(", browser_spec, re.MULTILINE)
        browser_projects = re.findall(r'^      name: "live-[a-z-]+",$', browser_config, re.MULTILINE)
        self.assertEqual((len(route_cases) + len(explicit_tests), len(browser_projects)), (18, 3))
        self.assertEqual((len(route_cases) + len(explicit_tests)) * len(browser_projects), 54)
        self.assertEqual(browser_spec.count('type: "public-live-qa-expected-skip"'), 6)
        self.assertIn("PROD_SSH_HOST", source)
        self.assertIn("ssh " + "\\", source)
        self.assertIn(
            "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
            source,
        )
        self.assertIn("live-launch < \"$input_path\"", source)
        self.assertIn("platform_live_browser_qa.sh\" public", supervisor)
        self.assertIn('launch_stage="engine_preflight"', supervisor)
        self.assertIn('launch_check="engine_preflight"', supervisor)
        self.assertLess(
            supervisor.index('launch_stage="engine_preflight"'),
            supervisor.index('launch_stage="account_install"'),
        )
        self.assertLess(
            supervisor.index('launch_stage="engine_preflight"'),
            supervisor.index('launch_stage="provision"'),
        )
        self.assertIn('"$TOOLS_DIR/platform_live_browser_qa.sh" engine-preflight', supervisor)
        self.assertIn('[[ "$QA_MODE" == "public" ]]', browser_wrapper)
        self.assertIn('"$QA_MODE" == "engine-preflight"', browser_wrapper)
        self.assertIn('if [[ "$QA_MODE" == "public" ]]; then', browser_wrapper)
        self.assertIn('live-engine-preflight.cjs', browser_wrapper)
        self.assertIn('browser.newContext(contextOptions)', engine_preflight)
        self.assertIn('context.newPage()', engine_preflight)
        self.assertEqual(engine_preflight.count('await page.goto("about:blank")'), 1)
        self.assertIn('checkEngine(chromium, {', engine_preflight)
        self.assertIn('checkEngine(webkit, devices["iPhone 13"])', engine_preflight)
        self.assertNotIn("csp-live-qa.json", engine_preflight)
        self.assertNotIn("provision_live_csp_qa", engine_preflight)
        self.assertIn("failure_report_unavailable", source)
        self.assertIn("failure_report_unavailable", supervisor)
        self.assertIn("failure_report_unavailable", browser_wrapper)
        self.assertIn("failure_report_unavailable", user_wrapper)
        self.assertIn("--fail-on-failure-counts", browser_wrapper)
        self.assertIn("if (( count_status != 0 )); then", browser_wrapper)
        preflight_start = browser_wrapper.index(
            "if (( TRUSTED_MODE == 1 )) && [[ \"$QA_MODE\" == \"public\" ]]; then\n"
            "  # Private diagnostics are auxiliary:"
        )
        preflight_end = browser_wrapper.index("\nSYSTEMD_BIND_ARGS=()", preflight_start)
        preflight_block = browser_wrapper[preflight_start:preflight_end]
        self.assertNotIn("failure_report_unavailable", preflight_block)
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture = Path(temporary)
            rejecting_guard = fixture / "guard"
            rejecting_guard.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
            rejecting_guard.chmod(0o700)
            shell_script = "\n".join(
                (
                    "set -euo pipefail",
                    "TRUSTED_MODE=1",
                    'QA_MODE="public"',
                    "BROWSER_GATE=public-live-qa.a1b2c3d4",
                    f"LIVE_QA_RUNNER_SHA={'a' * 40}",
                    f"LIVE_QA_TARGET_SHA={'b' * 40}",
                    f"LIVE_QA_MARKER_SHA256={'c' * 64}",
                    f"GUARD=({shlex.quote(str(rejecting_guard))})",
                    preflight_block,
                    "printf 'browser-started\\n'",
                )
            )
            preflight_result = subprocess.run(
                ["/bin/bash", "-c", shell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            self.assertEqual(preflight_result.returncode, 0, "private-store failure skipped browser execution")
            self.assertEqual(preflight_result.stdout, "browser-started\n")
            self.assertEqual(preflight_result.stderr, "")

            failed_counts_line = (
                "LIVE_BROWSER_COUNTS schema=1 run_status=failed logical_total=54 logical_pass=47 "
                "logical_fail=1 logical_expected_fail=0 logical_flaky=0 logical_skip=6 "
                "logical_interrupted=0 attempt_total=54 attempt_pass=47 attempt_fail=1 "
                "attempt_skip=6 attempt_interrupted=0 attempt_timedout=0 "
                f"source_sha={'a' * 40} app_sha={'b' * 40} marker_sha256={'c' * 64}"
            )
            passing_counts_line = failed_counts_line.replace(
                "run_status=failed logical_total=54 logical_pass=47 logical_fail=1",
                "run_status=passed logical_total=54 logical_pass=48 logical_fail=0",
            ).replace("attempt_pass=47 attempt_fail=1", "attempt_pass=48 attempt_fail=0")
            count_tail = browser_wrapper[browser_wrapper.index("count_status=0\nCOUNT_DIAGNOSTIC_ARGS=()"):]
            def run_count_tail(status: int, count_line: str, *, browser_exit: int = 0, preflight: bool = False):
                fake_guard = fixture / f"guard-{status}-{browser_exit}-{int(preflight)}"
                fake_guard.write_text(
                    "#!/bin/sh\n"
                    'if [ "$1" = prepare-public-failure-diagnostics ]; then exit 1; fi\n'
                    'if [ "$1" = emit-public-browser-counts ]; then printf "%s\\n" "$FAKE_COUNT_LINE"; exit "$FAKE_COUNT_STATUS"; fi\n'
                    "exit 2\n",
                    encoding="ascii",
                )
                fake_guard.chmod(0o700)
                count_output = fixture / f"counts-{status}-{browser_exit}-{int(preflight)}.txt"
                initial_kind = "browser_launch_error" if browser_exit else "none"
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        "TRUSTED_MODE=1",
                        'QA_MODE="public"',
                        "BROWSER_GATE=public-live-qa.a1b2c3d4",
                        f"LIVE_QA_RUNNER_SHA={'a' * 40}",
                        f"LIVE_QA_TARGET_SHA={'b' * 40}",
                        f"LIVE_QA_MARKER_SHA256={'c' * 64}",
                        f"SOURCE_COMMIT={'a' * 40}",
                        f"browser_status={browser_exit}",
                        "diagnostic_valid=1",
                        f"diagnostic_kind={initial_kind}",
                        "diagnostic_stdout_bytes=0",
                        "diagnostic_stderr_bytes=0",
                        "diagnostic_truncated=false",
                        f"diagnostic_child_exit={browser_exit}",
                        f"GUARD=({shlex.quote(str(fake_guard))})",
                        "exec 3>\"$COUNT_OUTPUT\"",
                        *(preflight_block.splitlines() if preflight else []),
                        count_tail,
                    )
                )
                return subprocess.run(
                    ["/bin/bash", "-c", script],
                    env={
                        **os.environ,
                        "FAKE_COUNT_LINE": count_line,
                        "FAKE_COUNT_STATUS": str(status),
                        "COUNT_OUTPUT": str(count_output),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                ), count_output

            persisted_failure, persisted_counts = run_count_tail(43, failed_counts_line)
            self.assertEqual(
                persisted_failure.returncode, 1,
                f"stdout={persisted_failure.stdout!r} stderr={persisted_failure.stderr!r}",
            )
            self.assertNotIn("LIVE_BROWSER_QA_SUCCESS", persisted_failure.stdout)
            self.assertNotIn("failure_report_unavailable", persisted_failure.stdout)
            self.assertEqual(persisted_counts.read_text(encoding="ascii"), failed_counts_line + "\n")

            unavailable_failure, unavailable_counts = run_count_tail(42, failed_counts_line)
            self.assertEqual(unavailable_failure.returncode, 1)
            self.assertNotIn("LIVE_BROWSER_QA_SUCCESS", unavailable_failure.stdout)
            self.assertIn("kind=failure_report_unavailable", unavailable_failure.stdout)
            self.assertIn("child_exit=0", unavailable_failure.stdout)
            self.assertEqual(unavailable_counts.read_text(encoding="ascii"), failed_counts_line + "\n")

            original_failure, _original_counts = run_count_tail(
                42, failed_counts_line, browser_exit=7
            )
            self.assertEqual(original_failure.returncode, 7)
            self.assertNotIn("LIVE_BROWSER_QA_SUCCESS", original_failure.stdout)
            self.assertIn("kind=failure_report_unavailable", original_failure.stdout)
            self.assertIn("child_exit=7", original_failure.stdout)

            passing_after_preflight, passing_counts = run_count_tail(
                0, passing_counts_line, preflight=True
            )
            self.assertEqual(passing_after_preflight.returncode, 0)
            self.assertIn("LIVE_BROWSER_QA_SUCCESS", passing_after_preflight.stdout)
            self.assertNotIn("failure_report_unavailable", passing_after_preflight.stdout)
            self.assertEqual(passing_counts.read_text(encoding="ascii"), passing_counts_line + "\n")
        self.assertIn("LIVE_LAUNCH_STATUS schema=2", supervisor)
        self.assertIn('required = ("oldsparky-platform",)', supervisor)
        self.assertIn('exec 3>&1', supervisor)
        self.assertIn('exec >/dev/null 2>&1', supervisor)
        self.assertLess(
            supervisor.index('launch_stage="account_install"'),
            supervisor.index('"$TOOLS_DIR/platform_install_live_qa_user.sh" --apply'),
        )
        self.assertGreater(
            supervisor.index('launch_stage="provision"'),
            supervisor.index('"$TOOLS_DIR/platform_install_live_qa_user.sh" --apply'),
        )
        self.assertIn("type: boolean", source)
        self.assertIn("LIVE_PROVISION", source)
        self.assertIn("LIVE_MARKER", source)
        self.assertIn("live-launch-input.json", source)
        self.assertIn("resolve-workflow-source-binding", source)
        self.assertIn("create-live-handoff", source)
        self.assertIn("SameOriginRedirect", source)
        self.assertIn("ArtifactRedirect", source)
        self.assertIn("HANDOFF_ARTIFACT_ID", source)
        self.assertIn('LIVE_HANDOFF status=verified', source)
        self.assertIn("APP_TARGET_SHA", source)
        self.assertNotIn("platform_workflow_input_guard.py live", source)
        self.assertIn('stat -c \'%a\' "$RUNNER_TEMP/live-launch-input.json"', source)
        self.assertIn("LIVE_QA_IDENTITY", supervisor)
        self.assertIn("LIVE_QA_ENV_PATH", supervisor)
        self.assertIn("uid_collision", supervisor)
        self.assertIn("platform_live_user_qa_dispatch.py verify", supervisor)
        self.assertIn("liveqa-[a-z0-9-]{6,56}", supervisor)
        self.assertIn("platform_provision_live_csp_qa.sh", supervisor)
        self.assertEqual(
            supervisor.count("/usr/bin/python3.12 -I -B - <<'PY'"),
            1,
        )
        self.assertIn("Provisioning requires a fresh liveqa marker", supervisor)
        self.assertNotIn("Refusing to replace the existing live QA bundle", source)
        self.assertIn("PLATFORM_LIVE_QA_INSTALL_ROOT", supervisor)
        self.assertIn("platform_live_user_qa_dispatch.py verify", supervisor)
        self.assertIn("platform_release_lock_exec.sh", supervisor)
        self.assertNotIn("/root/old_sparky", supervisor)
        self.assertNotIn("npm ci", source)
        self.assertNotIn("npm run test:live", source)
        self.assertNotIn('bash -s -- "$LIVE_BASE_URL"', source)
        self.assertNotIn("live_browser_qa_success", source.lower())
        self.assertIn('rb"LIVE_LAUNCH_STATUS schema=2 status=(passed|failed) "', source)
        self.assertIn('stage == "complete"', source)
        self.assertIn('child_status == 0', source)
        self.assertLess(
            source.index('LIVE_HANDOFF status=verified'),
            source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        self.assertEqual(
            os.geteuid(),
            0,
            "this wrapper execution contract belongs to backend-privileged",
        )
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture = Path(temporary)
            trusted_root = fixture / "liveqa"
            trusted_root.mkdir(mode=0o700)
            app_dir = fixture / "platform"
            app_dir.mkdir(mode=0o700)
            dispatcher = trusted_root / "platform_live_user_qa_dispatch.py"
            dispatcher.write_text(
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n",
                encoding="ascii",
            )
            os.chmod(dispatcher, 0o500)
            release_lock_exec = trusted_root / "platform_release_lock_exec.sh"
            release_lock_exec.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "[ \"$1\" = --app-dir ]\n"
                "shift 2\n"
                "[ \"$1\" = --expected-sha ]\n"
                "shift 2\n"
                "[ \"$1\" = -- ]\n"
                "shift\n"
                "exec \"$@\"\n",
                encoding="ascii",
            )
            os.chmod(release_lock_exec, 0o500)
            wrapper_source = (
                TOOLS_ROOT / "platform_live_launch_trusted.sh"
            ).read_text(encoding="utf-8")
            wrapper_source = wrapper_source.replace(
                'TRUSTED_ROOT="/root/.oldsparky/liveqa"',
                f'TRUSTED_ROOT="{trusted_root}"',
            ).replace(
                'APP_DIR="/opt/oldsparky/platform"',
                f'APP_DIR="{app_dir}"',
            ).replace("/usr/bin/python3.12", sys.executable)
            wrapper = fixture / "platform_live_launch_trusted.sh"
            wrapper.write_text(wrapper_source, encoding="utf-8")
            os.chmod(wrapper, 0o500)

            target_sha = "a" * 40
            base_url = "https://old-sparky.com"
            marker = "liveqa-contract-test"
            completed = subprocess.run(
                [str(wrapper), base_url, "true", marker, target_sha],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout.splitlines(),
                [
                    json.dumps(["verify", target_sha]),
                    json.dumps(
                        [
                            "run-launch",
                            target_sha,
                            base_url,
                            "true",
                            marker,
                        ]
                    ),
                ],
            )
            self.assertEqual(completed.stderr, "")

            malformed = subprocess.run(
                [str(wrapper), base_url, "true", marker],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(malformed.returncode, 2)
            self.assertEqual(malformed.stdout, "")
            self.assertIn("invalid argument count", malformed.stderr)
        status_sha = "a" * 40

        identity_match = re.search(
            r"/usr/bin/python3\.12 -I -B - <<'PY'\n(.*?)\nPY",
            supervisor,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(identity_match)
        assert identity_match is not None

        def run_identity_report(
            users: dict[str, SimpleNamespace],
            groups: dict[str, SimpleNamespace],
        ) -> tuple[int, dict[str, object]]:
            fake_pwd = SimpleNamespace(
                getpwnam=lambda name: users[name],
                getpwall=lambda: list(users.values()),
            )
            fake_grp = SimpleNamespace(
                getgrnam=lambda name: groups[name],
                getgrall=lambda: list(groups.values()),
            )
            output = StringIO()
            with patch.dict(sys.modules, {"pwd": fake_pwd, "grp": fake_grp}), redirect_stdout(output):
                try:
                    exec(compile(identity_match.group(1), "supervisor-identity", "exec"), {})
                except SystemExit as error:
                    status = int(error.code)
                else:
                    status = 0
            return status, json.loads(output.getvalue().removeprefix("LIVE_QA_IDENTITY "))

        def identity_fixture(*, legacy: bool = False, platform: bool = True):
            users = {
                "oldsparky-liveqa": SimpleNamespace(
                    pw_name="oldsparky-liveqa", pw_uid=998, pw_gid=998,
                    pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin",
                ),
            }
            groups = {"oldsparky-liveqa": SimpleNamespace(gr_name="oldsparky-liveqa", gr_gid=998, gr_mem=[])}
            if platform:
                users["oldsparky-platform"] = SimpleNamespace(
                    pw_name="oldsparky-platform", pw_uid=992, pw_gid=992,
                    pw_dir="/srv/oldsparky", pw_shell="/usr/sbin/nologin",
                )
                groups["oldsparky-platform"] = SimpleNamespace(
                    gr_name="oldsparky-platform", gr_gid=992, gr_mem=[]
                )
            if legacy:
                users["oldsparky"] = SimpleNamespace(
                    pw_name="oldsparky", pw_uid=991, pw_gid=991,
                    pw_dir="/srv/oldsparky-legacy", pw_shell="/usr/sbin/nologin",
                )
                groups["oldsparky"] = SimpleNamespace(
                    gr_name="oldsparky", gr_gid=991, gr_mem=[]
                )
            return users, groups

        absent_users, absent_groups = identity_fixture()
        status, identity = run_identity_report(absent_users, absent_groups)
        self.assertEqual(status, 0)
        self.assertEqual(identity["status"], "valid")
        self.assertEqual(identity["required_identities_present"], True)

        present_users, present_groups = identity_fixture(legacy=True)
        status, identity = run_identity_report(present_users, present_groups)
        self.assertEqual(status, 0)
        self.assertEqual(identity["status"], "valid")

        collision_users, collision_groups = identity_fixture(legacy=True)
        collision_users["oldsparky"] = SimpleNamespace(
            pw_name="oldsparky", pw_uid=998, pw_gid=998,
            pw_dir="/srv/oldsparky-legacy", pw_shell="/usr/sbin/nologin",
        )
        status, identity = run_identity_report(collision_users, collision_groups)
        self.assertEqual(status, 1)
        self.assertIn("uid_collision", identity["reasons"])

        group_collision_users, group_collision_groups = identity_fixture(legacy=True)
        group_collision_users["oldsparky"] = SimpleNamespace(
            pw_name="oldsparky", pw_uid=991, pw_gid=998,
            pw_dir="/srv/oldsparky-legacy", pw_shell="/usr/sbin/nologin",
        )
        group_collision_groups["oldsparky"] = SimpleNamespace(
            gr_name="oldsparky", gr_gid=998, gr_mem=[]
        )
        status, identity = run_identity_report(group_collision_users, group_collision_groups)
        self.assertEqual(status, 1)
        self.assertIn("gid_collision", identity["reasons"])

        missing_users, missing_groups = identity_fixture(platform=False)
        status, identity = run_identity_report(missing_users, missing_groups)
        self.assertEqual(status, 1)
        self.assertEqual(identity["required_identities_present"], False)
        self.assertIn("oldsparky-platform_user_missing", identity["reasons"])

        missing_group_users, missing_group_groups = identity_fixture()
        missing_group_groups.pop("oldsparky-liveqa")
        status, identity = run_identity_report(missing_group_users, missing_group_groups)
        self.assertEqual(status, 1)
        self.assertIn("group_missing", identity["reasons"])

        emitter_prologue = supervisor.split('EXPECTED_ORIGIN="https://old-sparky.com"', 1)[0]
        emitter_script = (
            emitter_prologue
            + 'launch_stage="identity"\nlaunch_check="identity"\nexit 1\n'
        )
        emitter = subprocess.run(
            [
                "/bin/bash", "-c", emitter_script, "live-launch-test",
                "https://old-sparky.com", "true", "liveqa-test-marker", status_sha,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(emitter.returncode, 1)
        self.assertEqual(
            emitter.stdout,
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=identity "
            f"check=identity child_exit=1 source_sha={status_sha}\n",
        )
        self.assertEqual(emitter.stderr, "")

        # OpenSSH concatenates command arguments into a remote shell command,
        # so every workflow SSH command must end at a fixed dispatcher mode.
        dispatcher = "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py"
        workflow_modes = (
            (source, ("live-launch",)),
            (
                (REPO_ROOT / ".github/workflows/platform-production-external-load.yml")
                .read_text(encoding="utf-8"),
                (
                    "external-fixture",
                    "external-finalize",
                    "external-cleanup",
                    "external-cleanup-exports",
                ),
            ),
            (
                (REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml")
                .read_text(encoding="utf-8"),
                ("retained-cleanup", "retained-cleanup-exports"),
            ),
            (
                (REPO_ROOT / ".github/workflows/platform-production-deploy.yml")
                .read_text(encoding="utf-8"),
                ("production-prepare-artifact", "production-deploy"),
            ),
        )
        external_source = workflow_modes[1][0]
        cleanup_source = workflow_modes[2][0]
        deploy_source = workflow_modes[3][0]
        self.assertLess(
            external_source.index("validate_external_payload(payload)"),
            external_source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        self.assertLess(
            cleanup_source.index("platform-retained-cleanup-input.json"),
            cleanup_source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        for workflow_source, modes in workflow_modes:
            for mode in modes:
                dispatch_mode = (
                    "external-cleanup --run-attempt"
                    if workflow_source is external_source and mode == "external-cleanup"
                    else "retained-cleanup --run-attempt"
                    if workflow_source is cleanup_source and mode == "retained-cleanup"
                    else mode
                )
                if workflow_source is source:
                    expected_dispatcher = dispatcher
                elif workflow_source is deploy_source:
                    expected_dispatcher = '"$HOST_TOOLS_DISPATCHER"'
                elif (
                    workflow_source is external_source
                    and mode in {"external-finalize", "external-cleanup-exports"}
                ) or (
                    workflow_source is cleanup_source
                    and mode == "retained-cleanup-exports"
                ):
                    expected_dispatcher = '"$retained_load_dispatcher"'
                else:
                    expected_dispatcher = "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py"
                mode_positions = [
                    position
                    for position in range(len(workflow_source))
                    if workflow_source.startswith(f"{dispatch_mode} ", position)
                ]
                self.assertGreaterEqual(len(mode_positions), 1, mode)
                if workflow_source is source:
                    # The live workflow also refers to its mode in validation
                    # and report steps. Bound this assertion to the actual SSH
                    # step so private handoff/sanitizer references elsewhere
                    # cannot be mistaken for remote command arguments.
                    step_start = workflow_source.index(
                        "      - name: Run the dedicated production browser supervisor"
                    )
                    step_end = workflow_source.index(
                        "      - name: Remove production SSH material", step_start
                    )
                    ssh_position = workflow_source.index("          ssh \\\n", step_start)
                    mode_position = workflow_source.index(
                        '            live-launch < "$input_path"', ssh_position, step_end
                    )
                else:
                    mode_position = mode_positions[-1]
                    ssh_position = workflow_source.rfind("ssh \\\n", 0, mode_position)
                    if ssh_position < 0:
                        ssh_position = workflow_source.rfind("ssh ", 0, mode_position)
                self.assertGreaterEqual(ssh_position, 0)
                command = workflow_source[
                    ssh_position : mode_position + len(dispatch_mode) + 1
                ]
                self.assertIn(expected_dispatcher, command)
                self.assertNotRegex(
                    command,
                    r"\$(?:CONTROL_EMAIL|LIVE_BASE_URL|LIVE_PROVISION|"
                    r"LIVE_MARKER|live_marker|marker|DEPLOY_MODE|RUNTIME_PROFILE|"
                    r"RELEASE_SLUG|ARTIFACT_REMOTE_DIR|release_slug)",
                )
                if workflow_source is external_source and mode == "external-cleanup":
                    self.assertIn('--profile-id "$PROFILE_ID"', workflow_source)
                if workflow_source is cleanup_source and mode == "retained-cleanup":
                    self.assertIn('--load-run-id "$LOAD_RUN_ID"', workflow_source)
                    self.assertIn('rb"profile=retained-load-cleanup\\n"', workflow_source)

        # Adversarial dispatch data is rejected by the same canonical parser
        # before the remote dispatcher can invoke SSH/sudo.
        invalid_emails = (
            "x; touch /tmp/pwn #",
            'x"\'@example.invalid',
            "x\n@example.invalid",
            "x$(id)@example.invalid",
            "`id`@example.invalid",
            "--control-email@example.invalid",
            "user@éxample.invalid",
            "user\x01@example.invalid",
            "user\x00@example.invalid",
            "u" * 255 + "@example.invalid",
        )
        for value in invalid_emails:
            with self.subTest(control_email=value):
                with self.assertRaises(WorkflowInputError):
                    validate_control_email(value)

        self.assertEqual(
            validate_confirmation("RUN-LIVE-USER-QA", "RUN-LIVE-USER-QA"),
            "RUN-LIVE-USER-QA",
        )
        for value in (
            "RUN-LIVE-USER-QA ",
            "RUN-LIVE-USER-QA\n",
            "RUN-LIVE-USER-QA;id",
            "RUN-LIVE-USER-QB",
        ):
            with self.subTest(confirmation=value):
                with self.assertRaises(WorkflowInputError):
                    validate_confirmation(value, "RUN-LIVE-USER-QA")
        self.assertEqual(validate_target_sha("a" * 40), "a" * 40)
        self.assertEqual(validate_run_id("123456"), "123456")
        for value, validator in (
            ("A" * 40, validate_target_sha),
            ("a" * 39, validate_target_sha),
            ("1;id", validate_run_id),
            ("", validate_run_id),
        ):
            with self.subTest(guard_value=value):
                with self.assertRaises(WorkflowInputError):
                    validator(value)

        valid_live = {
            "schema": 1,
            "base_url": "https://old-sparky.com",
            "provision": "true",
            "marker": "liveqa-csp-test-abc123",
            "target_sha": "a" * 40,
        }
        invalid_markers = (
            "x; touch /tmp/pwn #",
            'liveqa-quote"-abc123',
            "liveqa-line\nbreak",
            "liveqa-$()abc123",
            "liveqa-`id`abc123",
            "--marker=liveqa-abc123",
            "liveqa-unicode-é",
            "liveqa-control\x01",
            "liveqa-nul\x00",
            "liveqa-" + "a" * 57,
        )
        for value in invalid_markers:
            with self.subTest(marker=value):
                invalid_payload = {**valid_live, "marker": value}
                with self.assertRaises(WorkflowInputError):
                    validate_live_payload(invalid_payload)

        invalid_payload = {**valid_live, "marker": invalid_markers[0]}
        stdin = TextIOWrapper(
            BytesIO((json.dumps(invalid_payload) + "\n").encode("utf-8")),
            encoding="utf-8",
        )
        stderr = StringIO()
        with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
            patch.object(platform_workflow_remote_dispatch.subprocess, "run") as run, \
            patch.object(platform_workflow_remote_dispatch.subprocess, "Popen") as popen, \
            redirect_stderr(stderr):
            self.assertEqual(
                platform_workflow_remote_dispatch.main(["live-launch"]),
                2,
            )
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")
        self.assertNotIn(invalid_markers[0], stderr.getvalue())

        valid_external = {
            "schema": 1,
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": "a" * 40,
            "control_email": "Control+qa@example.invalid",
            "setup_concurrency": "8",
            "run_id": "123456",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
        }
        invalid_external = {
            **valid_external,
            "control_email": invalid_emails[0],
        }
        with self.assertRaises(WorkflowInputError):
            validate_external_payload(invalid_external)
        stdin = TextIOWrapper(
            BytesIO((json.dumps(invalid_external) + "\n").encode("utf-8")),
            encoding="utf-8",
        )
        stderr = StringIO()
        with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
            patch.object(platform_workflow_remote_dispatch.subprocess, "run") as run, \
            patch.object(platform_workflow_remote_dispatch.subprocess, "Popen") as popen, \
            redirect_stderr(stderr):
            self.assertEqual(
                platform_workflow_remote_dispatch.main(
                    ["external-fixture", "--run-id", "123456", "--run-attempt", "2"]
                ),
                2,
            )
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")
        self.assertNotIn(invalid_emails[0], stderr.getvalue())

        # CLI identity is mandatory and must agree with the exact payload
        # before the helper process can be started.
        valid_external = {
            **valid_external,
            "run_id": "123456",
        }
        for argv in (
            ["external-fixture", "--run-id", "123456"],
            ["external-fixture", "--run-id", "123457", "--run-attempt", "2"],
            ["external-fixture", "--run-id", "123456", "--run-attempt", "0"],
        ):
            stdin = TextIOWrapper(
                BytesIO((json.dumps(valid_external) + "\n").encode("utf-8")),
                encoding="utf-8",
            )
            with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
                patch.object(platform_workflow_remote_dispatch.subprocess, "Popen") as popen:
                self.assertEqual(platform_workflow_remote_dispatch.main(argv), 2)
            popen.assert_not_called()

        # A valid payload still produces a fixed helper argv; shell quoting is
        # not involved at this local subprocess boundary.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "platform_live_launch_supervisor.sh"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o755)
            stdin = TextIOWrapper(
                BytesIO((json.dumps(valid_live) + "\n").encode("utf-8")),
                encoding="utf-8",
            )
            with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
                patch.object(platform_workflow_remote_dispatch, "TRUSTED_LIVE_ROOT", root), \
                patch.object(platform_workflow_remote_dispatch, "TRUSTED_LIVE_LAUNCH", helper), \
                patch.object(
                    platform_workflow_remote_dispatch,
                    "_run_bounded_child",
                    return_value=0,
                ) as run_child:
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["live-launch"]),
                    0,
                )
            self.assertEqual(
                run_child.call_args.args[0],
                [
                    "/usr/bin/sudo",
                    "-n",
                    "--",
                    str(helper),
                    valid_live["base_url"],
                    valid_live["provision"],
                    valid_live["marker"],
                    valid_live["target_sha"],
                ],
            )
            self.assertEqual(
                run_child.call_args.kwargs,
                {
                    "timeout_seconds": platform_workflow_remote_dispatch.LIVE_LAUNCH_OPERATION_TIMEOUT_SECONDS,
                    "expected_live_launch_sha": valid_live["target_sha"],
                    "expected_live_app_sha": valid_live["target_sha"],
                    "expected_live_marker_sha256": hashlib.sha256(
                        valid_live["marker"].encode("ascii")
                    ).hexdigest(),
                },
            )

        def run_status_child(lines: tuple[str, ...], exit_code: int) -> tuple[int, str]:
            script = "import sys; " + "".join(
                f"print({line!r})\n" for line in lines
            ) + f"raise SystemExit({exit_code})"
            output = StringIO()
            with redirect_stdout(output):
                child_status = platform_workflow_remote_dispatch._run_bounded_child(
                    [sys.executable, "-c", script],
                    timeout_seconds=2,
                    expected_live_launch_sha=status_sha,
                )
            return child_status, output.getvalue()

        good_status = (
            "LIVE_LAUNCH_STATUS schema=2 status=passed stage=complete check=none "
            f"child_exit=0 source_sha={status_sha}"
        )
        failed_status = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=identity check=none "
            f"child_exit=1 source_sha={status_sha}"
        )
        with self.subTest(live_status="success"):
            self.assertEqual(
                run_status_child((good_status,), 0), (0, good_status + "\n")
            )
        with self.subTest(live_status="fixed_failure"):
            self.assertEqual(
                run_status_child((failed_status,), 1), (1, failed_status + "\n")
            )
        for lines, child_exit in (
            (("PRIVATE_CHILD_OUTPUT", good_status), 0),
            ((good_status,), 1),
            ((good_status.replace(status_sha, "b" * 40),), 0),
            (("x" * 300, good_status), 0),
        ):
            with self.subTest(live_status="invalid", child_exit=child_exit):
                result, sanitized = run_status_child(lines, child_exit)
                self.assertEqual(result, child_exit or 2)
                self.assertIn("stage=trusted_entry", sanitized)
                self.assertIn("check=protocol", sanitized)
                self.assertNotIn("PRIVATE_CHILD_OUTPUT", sanitized)
                self.assertNotIn("x" * 300, sanitized)

        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat"
            child_pid = Path(directory) / "child-pid"
            fork_script = textwrap.dedent(
                f"""
                import os, signal, sys, time
                child = os.fork()
                if child == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    with open({str(child_pid)!r}, "w", encoding="ascii") as stream:
                        stream.write(str(os.getpid()))
                    while True:
                        with open({str(heartbeat)!r}, "a", encoding="ascii") as stream:
                            stream.write("x")
                        time.sleep(0.005)
                signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
                time.sleep(0.03)
                print("x" * 8192, flush=True)
                while True:
                    time.sleep(1)
                """
            )
            output = StringIO()
            with patch.object(
                platform_workflow_remote_dispatch,
                "CHILD_TERMINATION_GRACE_SECONDS",
                0.1,
            ), patch.object(
                platform_workflow_remote_dispatch,
                "LIVE_LAUNCH_STREAM_MAX_BYTES",
                1024,
            ), redirect_stdout(output):
                result = platform_workflow_remote_dispatch._run_bounded_child(
                    [sys.executable, "-c", fork_script],
                    timeout_seconds=2,
                    expected_live_launch_sha=status_sha,
                )
            self.assertEqual(result, 2)
            self.assertIn("stage=trusted_entry", output.getvalue())
            self.assertIn("check=stream_limit", output.getvalue())
            self.assertTrue(child_pid.exists())
            heartbeat_size = heartbeat.stat().st_size
            time.sleep(0.05)
            self.assertEqual(heartbeat.stat().st_size, heartbeat_size)

        sanitizer_match = re.search(
            r'/usr/bin/python3 - "\$raw_report" "\$safe_report" '
            r'"\$SUPERVISOR_STATUS" \\\s*'
            r'"\$GITHUB_SHA" "\$APP_TARGET_SHA" "\$SOURCE_BINDING_SHA256" "\$LIVE_MARKER" '
            r'<<\'PY\'\n(.*?)\n          PY',
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(sanitizer_match)
        assert sanitizer_match is not None
        sanitizer = textwrap.dedent(sanitizer_match.group(1))

        def sanitize_status(line: bytes, ssh_status: int, *, sha: str = status_sha):
            with tempfile.TemporaryDirectory() as directory:
                raw_path = Path(directory) / "raw.log"
                safe_path = Path(directory) / "safe.json"
                raw_path.write_bytes(line)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        sanitizer,
                        str(raw_path),
                        str(safe_path),
                        str(ssh_status),
                        sha,
                        sha,
                        "",
                        "liveqa-count-contract",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(safe_path.read_text(encoding="utf-8"))

        count_fields = {
            "run_status": "passed",
            "logical_total": 54,
            "logical_pass": 44,
            "logical_fail": 0,
            "logical_expected_fail": 0,
            "logical_flaky": 0,
            "logical_skip": 10,
            "logical_interrupted": 0,
            "attempt_total": 54,
            "attempt_pass": 44,
            "attempt_fail": 0,
            "attempt_skip": 10,
            "attempt_interrupted": 0,
            "attempt_timedout": 0,
            "source_sha": status_sha,
            "app_sha": status_sha,
            "marker_sha256": hashlib.sha256(b"liveqa-count-contract").hexdigest(),
        }
        counts_line = (
            "LIVE_BROWSER_COUNTS schema=1 "
            + " ".join(
                f"{name}={count_fields[name]}"
                for name in (
                    "run_status",
                    *platform_workflow_remote_dispatch.LIVE_BROWSER_COUNT_FIELDS,
                    "source_sha",
                    "app_sha",
                    "marker_sha256",
                )
            )
            + "\n"
        )
        passed_report = sanitize_status(
            (counts_line + good_status + "\n").encode(), 0
        )
        self.assertEqual(passed_report["status"], "passed")
        self.assertTrue(passed_report["success"])
        self.assertEqual(passed_report["failure_reason"], "none")
        self.assertEqual(passed_report["test_count"], 44)
        self.assertEqual(passed_report["logical_counts"]["logical_total"], 54)
        self.assertEqual(passed_report["logical_counts"]["logical_skip"], 10)
        self.assertEqual(passed_report["attempt_counts"]["attempt_pass"], 44)
        self.assertEqual(passed_report["tests"], [])
        self.assertEqual(passed_report["stage"], "complete")
        self.assertEqual(passed_report["check_id"], "none")
        self.assertEqual(passed_report["source_git_sha"], status_sha)
        self.assertEqual(passed_report["app_target_sha"], status_sha)
        self.assertIsNone(passed_report["source_binding_sha256"])
        validator_match = re.search(
            r'/usr/bin/python3 - "\$RUNNER_TEMP/live-launch-report\.json" \\\s*'
            r'"\$GITHUB_SHA" <<\'PY\'\n(.*?)\n\s*PY',
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(validator_match)
        assert validator_match is not None
        validator_script = textwrap.dedent(validator_match.group(1))
        with tempfile.TemporaryDirectory(prefix="live-launch-validator-") as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(passed_report), encoding="utf-8")
            valid_summary = subprocess.run(
                [sys.executable, "-", str(report_path), status_sha],
                input=validator_script,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid_summary.returncode, 0, valid_summary.stderr)
            inconsistent_status = {**passed_report, "status": "failed"}
            report_path.write_text(json.dumps(inconsistent_status), encoding="utf-8")
            rejected_summary = subprocess.run(
                [sys.executable, "-", str(report_path), status_sha],
                input=validator_script,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected_summary.returncode, 0)
            self.assertIn("status is not passed", rejected_summary.stderr)
        no_pass_fields = {
            **count_fields,
            "logical_pass": 0,
            "logical_expected_fail": 1,
            "logical_skip": 53,
            "attempt_total": 54,
            "attempt_pass": 0,
            "attempt_fail": 1,
            "attempt_skip": 53,
        }
        no_pass_counts_line = (
            "LIVE_BROWSER_COUNTS schema=1 "
            + " ".join(
                f"{name}={no_pass_fields[name]}"
                for name in (
                    "run_status",
                    *platform_workflow_remote_dispatch.LIVE_BROWSER_COUNT_FIELDS,
                    "source_sha",
                    "app_sha",
                    "marker_sha256",
                )
            )
            + "\n"
        )
        no_pass_report = sanitize_status(
            (no_pass_counts_line + good_status + "\n").encode(), 0
        )
        self.assertEqual(no_pass_report["status"], "failed")
        self.assertFalse(no_pass_report["success"])
        self.assertEqual(no_pass_report["logical_counts"]["logical_pass"], 0)
        self.assertEqual(no_pass_report["logical_counts"]["logical_expected_fail"], 1)
        self.assertEqual(no_pass_report["logical_counts"]["logical_skip"], 53)
        self.assertEqual(no_pass_report["test_count"], 1)
        self.assertEqual(no_pass_report["failure_reason"], "coverage_incomplete")
        failed_report = sanitize_status((failed_status + "\n").encode(), 1)
        self.assertEqual(failed_report["status"], "failed")
        self.assertIsNone(failed_report["test_count"])
        self.assertEqual(failed_report["stage"], "identity")
        self.assertEqual(failed_report["failure_reason"], "child_failed")
        checked_failure_status = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=validation "
            "check=provision_marker child_exit=1 "
            f"source_sha={status_sha}\n"
        )
        checked_failure = sanitize_status(checked_failure_status.encode(), 1)
        self.assertEqual(checked_failure["status"], "failed")
        self.assertEqual(checked_failure["check_id"], "provision_marker")
        for malformed, reason, error_classes in (
            (
                (counts_line.replace(status_sha, "b" * 40) + good_status + "\n").encode(),
                "counts_unavailable",
                {"other": 1},
            ),
            (
                (good_status + "\nPRIVATE_OUTPUT\n").encode(),
                "marker_unavailable",
                {"unclassified_child_or_dispatch_failure": 1},
            ),
            (
                b"x" * 300,
                "marker_unavailable",
                {"unclassified_child_or_dispatch_failure": 1},
            ),
            (
                b"",
                "marker_unavailable",
                {"unclassified_child_or_dispatch_failure": 1},
            ),
        ):
            report = sanitize_status(malformed, 0)
            self.assertEqual(report["status"], "unavailable")
            self.assertIsNone(report["test_count"])
            self.assertEqual(report["failure_reason"], reason)
            self.assertEqual(report["error_class_counts"], error_classes)
            self.assertNotIn("PRIVATE_OUTPUT", json.dumps(report))

        # The local handoff is an atomic private file, not a shell fragment or
        # an Actions artifact.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "input.json"
            self.assertEqual(
                platform_workflow_input_guard.main(
                    [
                        "live",
                        "--output",
                        str(output),
                        "--base-url",
                        valid_live["base_url"],
                        "--provision",
                        valid_live["provision"],
                        "--marker",
                        valid_live["marker"],
                        "--target-sha",
                        valid_live["target_sha"],
                    ]
                ),
                0,
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {
                    "schema": "1",
                    "base_url": valid_live["base_url"],
                    "provision": valid_live["provision"],
                    "marker": valid_live["marker"],
                    "target_sha": valid_live["target_sha"],
                },
            )

            symlink_target = Path(directory) / "symlink-target.json"
            symlink_target.write_text("{}\n", encoding="utf-8")
            symlink_output = Path(directory) / "symlink-output.json"
            symlink_output.symlink_to(symlink_target)
            with self.assertRaises(WorkflowInputError):
                platform_workflow_input_guard._write_private_json(
                    symlink_output,
                    {"schema": "1"},
                )
            symlink_parent = Path(directory) / "symlink-parent"
            symlink_parent_target = Path(directory) / "real-parent"
            symlink_parent_target.mkdir()
            symlink_parent.symlink_to(symlink_parent_target, target_is_directory=True)
            with self.assertRaises(WorkflowInputError):
                platform_workflow_input_guard._write_private_json(
                    symlink_parent / "input.json",
                    {"schema": "1"},
                )

        # Every production dispatch input that reaches the assigned workflow
        # set is parsed before a step receives an SSH secret.  Keep these as
        # table-driven contracts so adding another confirmation/SHA surface
        # does not create a second test identity in the catalog.
        confirmation_cases = (
            ("platform-production-release-recover.yml", "RECOVER-PENDING-RELEASE"),
            ("platform-production-service-recovery.yml", "RECOVER-DEADLOCK-WEB"),
            (
                "platform-production-storage-maintenance.yml",
                "APPLY-PRODUCTION-STORAGE-MAINTENANCE",
            ),
        )
        sha_cases = (
            "platform-production-as12-proof.yml",
            "platform-production-storage-diagnostics.yml",
            "platform-production-storage-maintenance.yml",
            "platform-production-web-runtime-diagnostics.yml",
        )
        workflow_dir = REPO_ROOT / ".github/workflows"
        legacy_abort = (workflow_dir / "platform-production-release-abort.yml").read_text(
            encoding="utf-8"
        )
        legacy_secret_position = legacy_abort.index("secrets.PROD_SSH")
        self.assertIn(
            'test "$RECOVERY_CONFIRMATION" = "ABORT-LEGACY-RELEASE"',
            legacy_abort[:legacy_secret_position],
        )
        self.assertIn(
            'test "$GITHUB_REF" = "refs/heads/dev"',
            legacy_abort[:legacy_secret_position],
        )
        for filename, expected in confirmation_cases:
            source = (workflow_dir / filename).read_text(encoding="utf-8")
            with self.subTest(workflow=filename, input="confirmation"):
                # These recovery workflows validate the exact confirmation in
                # the runner before their SSH-secret step.  Some use the
                # shared parser; the older recovery wrappers intentionally
                # keep an inline literal check because they do not checkout a
                # repository on the secret-bearing job.
                secret_position = source.index("secrets.PROD_SSH")
                validation_source = source[:secret_position]
                self.assertIn(expected, validation_source)
                if "platform_workflow_input_guard.py confirmation" in source:
                    marker = "platform_workflow_input_guard.py confirmation"
                    self.assertIn(f'--expected "{expected}"', source)
                    self.assertLess(source.index(marker), secret_position)
                else:
                    self.assertRegex(
                        validation_source,
                        rf"(?s)(?:case|test) .*{re.escape(expected)}",
                    )
                for value in (
                    expected,
                    expected + " ",
                    expected + "\n",
                    expected + ";id",
                    expected.replace("-", "_") + "$(id)",
                    "é" + expected,
                    expected + "\x01",
                    expected + "\x00",
                ):
                    if value == expected:
                        self.assertEqual(validate_confirmation(value, expected), expected)
                    else:
                        with self.assertRaises(WorkflowInputError):
                            validate_confirmation(value, expected)

        for filename in sha_cases:
            source = (workflow_dir / filename).read_text(encoding="utf-8")
            with self.subTest(workflow=filename, input="target_sha"):
                marker = "platform_workflow_input_guard.py sha"
                secret_position = source.index("secrets.PROD_SSH")
                validation_source = source[:secret_position]
                if marker in source:
                    self.assertLess(source.index(marker), secret_position)
                else:
                    # The storage wrappers validate their exact SHA inline
                    # before exposing SSH secrets; keep that fixture contract
                    # explicit instead of requiring a checkout-side parser.
                    self.assertRegex(
                        validation_source,
                        r'\[\[ "\$EXPECTED_SHA" =~ \^\[0-9a-f\]\{40\}\$ \]\]',
                    )
                for value in (
                    "a" * 40,
                    "A" * 40,
                    "a" * 39,
                    "a" * 41,
                    "a" * 20 + "\n" + "a" * 19,
                    "$(id)",
                    "é" * 40,
                    "a" * 39 + "\x01",
                    "a" * 39 + "\x00",
                ):
                    if value == "a" * 40:
                        self.assertEqual(validate_target_sha(value), value)
                    else:
                        with self.assertRaises(WorkflowInputError):
                            validate_target_sha(value)

        for filename, marker in (
            (
                "platform-production-profile-review-fixture.yml",
                '"RUN-PRODUCTION-PROFILE-REVIEW"',
            ),
            ("platform-live-user-qa.yml", '"RUN-LIVE-USER-QA"'),
        ):
            source = (workflow_dir / filename).read_text(encoding="utf-8")
            with self.subTest(workflow=filename, input="remote-revalidation"):
                if filename == "platform-live-user-qa.yml":
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        source,
                    )
                    self.assertIn("TARGET_SHA", source)
                    self.assertIn("APP_TARGET_SHA", source)
                    self.assertIn("HANDOFF_ARTIFACT_ID", source)
                    self.assertIn('metadata.get("digest")', source)
                    self.assertIn('hashlib.sha256(binding_bytes).hexdigest()', source)
                    self.assertIn("LIVE_USER_HANDOFF_VERIFIED", source)
                    self.assertNotIn("download-live-handoff", source)
                    secret_job = source.split("  live-user-qa:\n", 1)[1]
                    self.assertNotIn("actions/checkout", secret_job)
                    self.assertNotIn("platform_noop_source_binding.py", secret_job)
                    self.assertNotIn("bash -s", source)
                    continue
                self.assertIn('input_guard="$runtime/current/tools/platform_workflow_input_guard.py"', source)
                self.assertIn('"$input_guard" confirmation', source)
                self.assertIn(f"--expected {marker}", source)
                self.assertIn('"$input_guard" sha --value "$target_sha"', source)

        for filename in (
            "platform-production-profile-review-fixture.yml",
            "platform-live-user-qa.yml",
            "platform-production-storage-diagnostics.yml",
            "platform-production-storage-maintenance.yml",
            "platform-patch-translation-qa.yml",
        ):
            source = (workflow_dir / filename).read_text(encoding="utf-8")
            with self.subTest(workflow=filename, input="helper-failure"):
                if filename == "platform-live-user-qa.yml":
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        source,
                    )
                    self.assertIn("TARGET_SHA", source)
                    self.assertIn("APP_TARGET_SHA", source)
                    self.assertIn("HANDOFF_ARTIFACT_ID", source)
                    self.assertIn('metadata.get("digest")', source)
                    self.assertNotIn("download-live-handoff", source)
                    secret_job = source.split("  live-user-qa:\n", 1)[1]
                    self.assertNotIn("actions/checkout", secret_job)
                    self.assertNotIn("platform_noop_source_binding.py", secret_job)
                    continue
                self.assertIn(
                    'test -f "$input_guard" && test ! -L "$input_guard"',
                    source,
                )

        translation_source = (workflow_dir / "platform-patch-translation-qa.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform_workflow_input_guard.py bounded-int", translation_source)
        self.assertNotRegex(translation_source, r'\[\[ "\$MAX_OPENAI_CALLS" =~')

        self.assertEqual(validate_bounded_integer("4", minimum=1, maximum=4), "4")
        for value in ("0", "5", "4\n", "$(id)", "é", "04"):
            with self.subTest(bounded_integer=value):
                with self.assertRaises(WorkflowInputError):
                    validate_bounded_integer(value, minimum=1, maximum=4)
        self.assertEqual(validate_run_id("1"), "1")
        self.assertEqual(validate_run_id("9" * 32), "9" * 32)
        for value in ("0", "00", "01", "9" * 33, "1\n", "$(id)"):
            with self.subTest(run_id=value):
                with self.assertRaises(WorkflowInputError):
                    validate_run_id(value)
        self.assertEqual(
            validate_utc_timestamp("2026-09-09T09:33:00Z"),
            "2026-09-09T09:33:00Z",
        )
        for value in (
            "2026-09-09T09:33:00Z\n",
            "2026-09-09T09:33:00+00:00",
            "2026-09-09T09:33:00Z;id",
            "2026-09-09T09:33:00Z$(id)",
            "2026-09-09T09:33:00Zé",
        ):
            with self.subTest(utc_timestamp=value):
                with self.assertRaises(WorkflowInputError):
                    validate_utc_timestamp(value)

    def test_deployment_payload_is_bounded_and_dispatch_has_fixed_argv(self) -> None:
        valid = {
            "schema": 1,
            "mode": "deploy",
            "runtime_profile": "baseline",
            "release_slug": "gha-123456-2-aaaaaaaaaaaa",
            "target_sha": "a" * 40,
            "artifact_remote_dir": "/tmp/old-sparky-platform-artifact-123456-2",
            "classifier_run_id": "123456",
            "classifier_run_attempt": "2",
            "web_compression": "enabled",
        }
        host_tools = {
            "schema": 1,
            "target_sha": "a" * 40,
            "host_tools_sha": "b" * 40,
            "artifact_id": "123456",
            "artifact_name": "platform-host-tools-bundle-123456-2",
            "artifact_size": "4096",
            "artifact_digest": "c" * 64,
            "bundle_sha256": "d" * 64,
            "manifest_sha256": "e" * 64,
            "capabilities_sha256": "f" * 64,
            "files_contract_sha256": "0" * 64,
            "modes_contract_sha256": "1" * 64,
            "signer_workflow": platform_workflow_input_guard.HOST_TOOLS_SIGNER_WORKFLOW,
            "source_ref": "refs/heads/dev",
            "source_digest": "a" * 40,
            "attestation_run_id": "123456",
            "attestation_run_attempt": "2",
            "attestation_job_id": "654321",
        }
        valid_deploy = {**valid, "schema": 2, "host_tools": host_tools}
        self.assertEqual(
            validate_deployment_payload({
                **valid_deploy,
                "runtime_profile": "authenticated-read-admission-24x8-native",
            })["runtime_profile"],
            "authenticated-read-admission-24x8-native",
        )
        for invalid_payload in (
            {**valid, "schema": 2},
            {**valid_deploy, "schema": 1},
            {**valid_deploy, "schema": 3},
        ):
            with self.subTest(schema_payload=invalid_payload):
                with self.assertRaises(WorkflowInputError):
                    validate_deployment_payload(invalid_payload)
        invalid_values = {
            "mode": (
                "x; touch /tmp/pwn #",
                '"deploy"',
                "deploy\npreflight",
                "$(id)",
                "`id`",
                "--deploy",
                "déploy",
                "deploy\x01",
                "deploy\x00",
                "d" * 181,
            ),
            "runtime_profile": (
                "x; touch /tmp/pwn #",
                'baseline";id',
                "baseline\nprofile",
                "$(id)",
                "`id`",
                "--profile",
                "baseline-é",
                "baseline\x01",
                "baseline\x00",
                "p" * 181,
            ),
            "release_slug": (
                "x; touch /tmp/pwn #",
                'release"quote',
                "release\nslug",
                "$(id)",
                "`id`",
                "--release",
                "release-é",
                "release\x01",
                "release\x00",
                "r" * 181,
                "gha-123456-2-bbbbbbbbbbbb",
                "gha-123456-2-aaaaaaaaaaa",
                "gha-123456-2-aaaaaaaaaaaaa",
                "gha-123456-0-aaaaaaaaaaaa",
                "gha-123456-2-AAAAAAAAAAAA",
            ),
            "target_sha": (
                "x; touch /tmp/pwn #",
                '"' + "a" * 38 + '"',
                "a" * 20 + "\n" + "a" * 19,
                "$(id)",
                "`id`",
                "--" + "a" * 38,
                "é" * 40,
                "a" * 39 + "\x01",
                "a" * 39 + "\x00",
                "a" * 41,
            ),
            "artifact_remote_dir": (
                "/tmp/old-sparky-platform-artifact-1-2;id",
                "/tmp/old-sparky-platform-artifact-1-2\n",
                "/tmp/old-sparky-platform-artifact-$(id)-2",
                "/tmp/old-sparky-platform-artifact-`id`-2",
                "/tmp/old-sparky-platform-artifact---",
                "/tmp/old-sparky-platform-artifact-é-2",
                "/tmp/old-sparky-platform-artifact-1-\x00",
                "/tmp/old-sparky-platform-artifact-" + "1" * 33 + "-2",
            ),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(WorkflowInputError):
                        validate_deployment_payload({**valid, field: value})

        invalid_payload = {**valid, "runtime_profile": "$(id)"}
        stdin = TextIOWrapper(
            BytesIO((json.dumps(invalid_payload) + "\n").encode("utf-8")),
            encoding="utf-8",
        )
        stderr = StringIO()
        with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
            patch.object(platform_workflow_remote_dispatch.subprocess, "run") as run, \
            patch.object(platform_workflow_remote_dispatch.subprocess, "Popen") as popen, \
            redirect_stderr(stderr):
            self.assertEqual(
                platform_workflow_remote_dispatch.main(["production-deploy"]),
                2,
            )
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")
        self.assertNotIn("$(id)", stderr.getvalue())

        # A deploy handoff without the final immutable host-tools contract is
        # rejected before either privileged dispatcher command is reached.
        stdin = TextIOWrapper(
            BytesIO((json.dumps(valid) + "\n").encode("utf-8")),
            encoding="utf-8",
        )
        stderr = StringIO()
        with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
            patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True), \
            patch.object(platform_workflow_remote_dispatch.subprocess, "run") as run, \
            redirect_stderr(stderr):
            self.assertEqual(
                platform_workflow_remote_dispatch.main(["production-deploy"]),
                2,
            )
        run.assert_not_called()
        self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")

        with tempfile.TemporaryDirectory() as directory:
            tools_root = Path(directory)
            helper = tools_root / "platform_production_deploy_supervisor.sh"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o555)
            stdin = TextIOWrapper(
                BytesIO((json.dumps(valid_deploy) + "\n").encode("utf-8")),
                encoding="utf-8",
            )
            with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
                patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_root), \
                patch.object(platform_workflow_remote_dispatch, "DEPLOY_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True), \
                patch.object(platform_workflow_remote_dispatch, "_verify_host_tools_contract", return_value=True), \
                patch.object(
                    platform_workflow_remote_dispatch, "_run_bounded_child", return_value=0
                ) as run_child:
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["production-deploy"]),
                    0,
                )
            self.assertEqual(
                run_child.call_args.args[0],
                [
                    "/usr/bin/sudo",
                    "-n",
                    "--",
                    str(helper),
                    valid["target_sha"],
                    valid["release_slug"],
                    valid["mode"],
                    valid["artifact_remote_dir"],
                    valid["runtime_profile"],
                    host_tools["host_tools_sha"],
                    host_tools["manifest_sha256"],
                    host_tools["capabilities_sha256"],
                ],
            )
            self.assertEqual(
                run_child.call_args.kwargs,
                {
                    "timeout_seconds": platform_workflow_remote_dispatch.DEPLOY_OPERATION_TIMEOUT_SECONDS,
                    "expected_release_marker": (
                        valid_deploy["mode"],
                        valid_deploy["release_slug"],
                        valid_deploy["target_sha"],
                    ),
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            tools_root = Path(directory)
            helper = tools_root / "platform_prepare_artifact_dir.py"
            helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            helper.chmod(0o555)
            stdin = TextIOWrapper(
                BytesIO((json.dumps(valid_deploy) + "\n").encode("utf-8")),
                encoding="utf-8",
            )
            child = Mock(pid=1236)
            child.wait.return_value = 0
            with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
                patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_root), \
                patch.object(platform_workflow_remote_dispatch, "ARTIFACT_DIR_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True), \
                patch.object(platform_workflow_remote_dispatch, "_verify_host_tools_contract", return_value=True), \
                patch.object(platform_workflow_remote_dispatch.sys, "executable", "/usr/bin/python3.12"), \
                patch.object(
                    platform_workflow_remote_dispatch.subprocess, "Popen", return_value=child
                ) as popen:
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["production-prepare-artifact"]),
                    0,
                )
            self.assertEqual(
                popen.call_args.args[0],
                [
                    "/usr/bin/sudo",
                    "-n",
                    "--",
                    "/usr/bin/python3.12",
                    "-I",
                    "-B",
                    str(helper),
                    valid["artifact_remote_dir"],
                ],
            )
            self.assertIs(
                popen.call_args.kwargs["stdout"],
                subprocess.DEVNULL,
            )

    def test_deploy_marker_capture_is_exact_bounded_and_status_preserving(self) -> None:
        source_sha = "a" * 40
        release_slug = "gha-123456-2-aaaaaaaaaaaa"
        expected = ("deploy", release_slug, source_sha)
        passed = (
            "RELEASE_DEPLOY schema=1 status=passed class=deployment "
            f"release_slug={release_slug} source_sha={source_sha}\n"
        )
        failed = (
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            "phase=preflight reason=environment "
            f"release_slug={release_slug} source_sha={source_sha}\n"
        )
        legacy_lock_failure = (
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            "phase=preflight reason=lock "
            f"release_slug={release_slug} source_sha={source_sha}\n"
        )
        baseline_changed_failure = (
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            "phase=preflight reason=baseline_changed "
            f"release_slug={release_slug} source_sha={source_sha}\n"
        )

        def run_child(
            output: bytes,
            *,
            status: int = 0,
            stderr: bytes = b"",
            expected_marker: tuple[str, str, str] = expected,
        ) -> tuple[int, str, str]:
            child_code = (
                "import os,sys; "
                f"os.write(1, bytes.fromhex({output.hex()!r})); "
                f"os.write(2, bytes.fromhex({stderr.hex()!r})); "
                f"raise SystemExit({status})"
            )
            stdout = StringIO()
            captured_stderr = StringIO()
            popen_kwargs: list[dict[str, object]] = []
            real_popen = platform_workflow_remote_dispatch.subprocess.Popen

            def launch_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                popen_kwargs.append(kwargs)
                return real_popen(*args, **kwargs)  # type: ignore[arg-type]

            with redirect_stdout(stdout), redirect_stderr(captured_stderr):
                with patch.object(
                    platform_workflow_remote_dispatch.subprocess,
                    "Popen",
                    side_effect=launch_child,
                ):
                    child_status = platform_workflow_remote_dispatch._run_bounded_child(
                        [sys.executable, "-c", child_code],
                        timeout_seconds=5,
                        expected_release_marker=expected_marker,
                    )
            self.assertEqual(len(popen_kwargs), 1)
            self.assertIs(popen_kwargs[0]["stdin"], subprocess.DEVNULL)
            self.assertIs(popen_kwargs[0]["stdout"], subprocess.PIPE)
            self.assertIs(popen_kwargs[0]["stderr"], subprocess.DEVNULL)
            self.assertTrue(popen_kwargs[0]["start_new_session"])
            self.assertTrue(popen_kwargs[0]["close_fds"])
            return child_status, stdout.getvalue(), captured_stderr.getvalue()

        self.assertEqual(run_child(passed.encode()), (0, passed, ""))
        self.assertEqual(run_child(failed.encode(), status=7), (7, failed, ""))
        self.assertEqual(run_child(passed.encode(), stderr=b"private child stderr"), (0, passed, ""))
        preflight = (
            "RELEASE_DEPLOY schema=1 status=passed class=preflight "
            f"release_slug={release_slug} source_sha={source_sha}\n"
        )
        self.assertEqual(
            run_child(preflight.encode(), expected_marker=("preflight", release_slug, source_sha)),
            (0, preflight, ""),
        )
        self.assertEqual(
            run_child(legacy_lock_failure.encode(), status=1),
            (1, legacy_lock_failure, ""),
        )
        self.assertEqual(
            run_child(baseline_changed_failure.encode(), status=1),
            (1, baseline_changed_failure, ""),
        )
        for stage in (
            "helper_metadata",
            "release_supervise",
            "release_open",
            "retained_supervise",
            "retained_open",
        ):
            staged_lock_failure = (
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                f"phase=preflight reason=lock lock_stage={stage} "
                f"release_slug={release_slug} source_sha={source_sha}\n"
            )
            with self.subTest(lock_stage=stage):
                self.assertEqual(
                    run_child(staged_lock_failure.encode(), status=1),
                    (1, staged_lock_failure, ""),
                )

        malformed = (
            passed.replace("class=deployment", "class=preflight").encode(),
            passed.replace(release_slug, "gha-123456-2-bbbbbbbbbbbb").encode(),
            passed.replace(source_sha, "b" * 40).encode(),
            passed.encode() + passed.encode(),
            legacy_lock_failure.replace(
                "reason=lock ", "reason=lock lock_stage=unknown "
            ).encode(),
            legacy_lock_failure.replace(
                "reason=lock ", "reason=lock lock_stage=release_open lock_stage=retained_open "
            ).encode(),
            failed.replace(
                "reason=environment ", "reason=environment lock_stage=release_open "
            ).encode(),
            legacy_lock_failure.replace(
                "class=preflight", "class=deployment"
            ).encode(),
            baseline_changed_failure.replace(
                "class=preflight", "class=artifact"
            ).encode(),
            baseline_changed_failure.replace(
                "phase=preflight", "phase=candidate"
            ).encode(),
            legacy_lock_failure.replace(
                "status=failed", "status=passed"
            ).encode(),
            b"x" * (platform_workflow_remote_dispatch.RELEASE_MARKER_MAX_BYTES + 4096),
        )
        for output in malformed:
            with self.subTest(output_length=len(output)):
                observed = min(len(output), platform_workflow_remote_dispatch.RELEASE_MARKER_MAX_BYTES + 1)
                reason = (
                    "oversized_marker"
                    if observed > platform_workflow_remote_dispatch.RELEASE_MARKER_MAX_BYTES
                    else "missing_marker"
                    if observed == 0
                    else "invalid_marker"
                )
                self.assertEqual(
                    run_child(output),
                    (
                        2,
                        "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                        f"reason={reason} child_exit=0 observed_bytes={observed} "
                        "dispatcher_exit=2\n",
                        "",
                    ),
                )

        self.assertEqual(
            run_child(passed.encode(), status=9),
            (
                9,
                "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                f"reason=invalid_marker child_exit=9 observed_bytes={len(passed.encode())} "
                "dispatcher_exit=9\n",
                "",
            ),
        )
        self.assertEqual(
            run_child(failed.encode()),
            (
                2,
                "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                f"reason=invalid_marker child_exit=0 observed_bytes={len(failed.encode())} "
                "dispatcher_exit=2\n",
                "",
            ),
        )

    def test_live_launch_count_protocol_is_ordered_bounded_and_bound(self) -> None:
        source_sha = "a" * 40
        app_sha = "b" * 40
        marker_sha = "c" * 64
        counts = {
            "run_status": "passed",
            "logical_total": 2,
            "logical_pass": 1,
            "logical_fail": 0,
            "logical_expected_fail": 1,
            "logical_flaky": 0,
            "logical_skip": 0,
            "logical_interrupted": 0,
            "attempt_total": 2,
            "attempt_pass": 1,
            "attempt_fail": 1,
            "attempt_skip": 0,
            "attempt_interrupted": 0,
            "attempt_timedout": 0,
            "source_sha": source_sha,
            "app_sha": app_sha,
            "marker_sha256": marker_sha,
        }
        count_line = (
            "LIVE_BROWSER_COUNTS schema=1 "
            + " ".join(
                f"{name}={counts[name]}"
                for name in (
                    "run_status",
                    *platform_workflow_remote_dispatch.LIVE_BROWSER_COUNT_FIELDS,
                    "source_sha",
                    "app_sha",
                    "marker_sha256",
                )
            )
            + "\n"
        ).encode("ascii")
        status_line = (
            "LIVE_LAUNCH_STATUS schema=2 status=passed stage=complete check=none "
            f"child_exit=0 source_sha={source_sha}\n"
        ).encode("ascii")
        expected_status = ("passed", "complete", "none", 0)
        parsed = platform_workflow_remote_dispatch._parse_live_launch_protocol(
            count_line + status_line,
            child_status=0,
            expected_sha=source_sha,
            expected_app_sha=app_sha,
            expected_marker_sha256=marker_sha,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], expected_status)
        self.assertEqual(parsed[1], counts)
        child_script = (
            "import sys\n"
            f"print({count_line.decode('ascii').rstrip()!r})\n"
            f"print({status_line.decode('ascii').rstrip()!r})\n"
        )
        output = StringIO()
        with redirect_stdout(output):
            child_exit = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", child_script],
                timeout_seconds=3,
                expected_live_launch_sha=source_sha,
                expected_live_app_sha=app_sha,
                expected_live_marker_sha256=marker_sha,
            )
        self.assertEqual(child_exit, 0)
        self.assertEqual(output.getvalue(), (count_line + status_line).decode("ascii"))
        unavailable_diagnostic = (
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=failure_report_unavailable "
            b"stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1\n"
        )
        self.assertIsNotNone(
            platform_live_qa_guard.LIVE_QA_CHILD_MARKER_RE.fullmatch(
                unavailable_diagnostic.decode("ascii")
            )
        )
        self.assertIsNone(
            platform_live_qa_guard.LIVE_QA_CHILD_MARKER_RE.fullmatch(
                unavailable_diagnostic.decode("ascii").replace(
                    "failure_report_unavailable", "unknown"
                )
            )
        )
        failed_status_line = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=browser_qa check=browser_qa "
            f"child_exit=1 source_sha={source_sha}\n"
        ).encode("ascii")
        self.assertIsNotNone(
            platform_workflow_remote_dispatch.LIVE_QA_DIAGNOSTIC_RE.fullmatch(
                unavailable_diagnostic
            )
        )
        self.assertTrue(
            platform_workflow_remote_dispatch._valid_live_qa_diagnostic(
                unavailable_diagnostic, 1
            )
        )
        self.assertTrue(
            platform_workflow_remote_dispatch._valid_live_qa_diagnostic(
                unavailable_diagnostic.replace(b"child_exit=1", b"child_exit=0"), 1
            )
        )
        self.assertFalse(
            platform_workflow_remote_dispatch._valid_live_qa_diagnostic(
                unavailable_diagnostic.replace(b"failure_report_unavailable", b"unclassified").replace(
                    b"child_exit=1", b"child_exit=0"
                ),
                1,
            )
        )
        self.assertFalse(
            platform_workflow_remote_dispatch._valid_live_qa_diagnostic(
                unavailable_diagnostic.replace(b"failure_report_unavailable", b"unknown"), 1
            )
        )
        self.assertEqual(
            platform_workflow_remote_dispatch._parse_live_launch_protocol(
                failed_status_line,
                child_status=1,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256=marker_sha,
            ),
            (("failed", "browser_qa", "browser_qa", 1), None),
        )
        preflight_status_line = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed "
            "stage=engine_preflight check=engine_preflight child_exit=1 "
            f"source_sha={source_sha}\n"
        ).encode("ascii")
        self.assertEqual(
            platform_workflow_remote_dispatch._parse_live_launch_protocol(
                preflight_status_line,
                child_status=1,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256=marker_sha,
            ),
            (("failed", "engine_preflight", "engine_preflight", 1), None),
        )
        self.assertIsNone(
            platform_workflow_remote_dispatch._parse_live_launch_protocol(
                preflight_status_line.replace(
                    b"check=engine_preflight", b"check=unknown"
                ),
                child_status=1,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256=marker_sha,
            )
        )
        unavailable_child = (
            f"import os; os.write(1, bytes.fromhex({(unavailable_diagnostic + failed_status_line).hex()!r})); "
            "raise SystemExit(1)"
        )
        unavailable_output = StringIO()
        with redirect_stdout(unavailable_output):
            unavailable_exit = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", unavailable_child],
                timeout_seconds=3,
                expected_live_launch_sha=source_sha,
                expected_live_app_sha=app_sha,
                expected_live_marker_sha256=marker_sha,
            )
        self.assertEqual(unavailable_exit, 1)
        self.assertEqual(
            unavailable_output.getvalue(),
            (unavailable_diagnostic + failed_status_line).decode("ascii"),
        )
        passing_counts_line = count_line.replace(
            b"logical_pass=1 logical_fail=0 logical_expected_fail=1",
            b"logical_pass=2 logical_fail=0 logical_expected_fail=0",
        ).replace(b"attempt_pass=1 attempt_fail=1", b"attempt_pass=2 attempt_fail=0")
        inconsistent_success_child = (
            f"import os; os.write(1, bytes.fromhex({(unavailable_diagnostic.replace(b'child_exit=1', b'child_exit=0') + passing_counts_line + status_line).hex()!r})); "
            "raise SystemExit(0)"
        )
        inconsistent_output = StringIO()
        with redirect_stdout(inconsistent_output):
            inconsistent_exit = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", inconsistent_success_child],
                timeout_seconds=3,
                expected_live_launch_sha=source_sha,
                expected_live_app_sha=app_sha,
                expected_live_marker_sha256=marker_sha,
            )
        self.assertEqual(inconsistent_exit, 2)
        self.assertEqual(
            inconsistent_output.getvalue(),
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=trusted_entry "
            f"check=protocol child_exit=2 source_sha={source_sha}\n",
        )
        failed_count_line = count_line.replace(b"run_status=passed", b"run_status=failed")
        failed_count_status_line = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=browser_qa check=browser_qa "
            f"child_exit=1 source_sha={source_sha}\n"
        ).encode("ascii")
        failed_count_child = (
            f"import os; os.write(1, bytes.fromhex({(unavailable_diagnostic.replace(b'child_exit=1', b'child_exit=0') + failed_count_line + failed_count_status_line).hex()!r})); "
            "raise SystemExit(1)"
        )
        failed_count_output = StringIO()
        with redirect_stdout(failed_count_output):
            failed_count_exit = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", failed_count_child],
                timeout_seconds=3,
                expected_live_launch_sha=source_sha,
                expected_live_app_sha=app_sha,
                expected_live_marker_sha256=marker_sha,
            )
        self.assertEqual(failed_count_exit, 1)
        self.assertEqual(
            failed_count_output.getvalue(),
            (
                unavailable_diagnostic.replace(b"child_exit=1", b"child_exit=0")
                + failed_count_line
                + failed_count_status_line
            ).decode("ascii"),
        )
        ordinary_failure_diagnostic = unavailable_diagnostic.replace(
            b"failure_report_unavailable", b"unclassified"
        )
        valid_failure_status = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=browser_qa check=browser_qa "
            f"child_exit=1 source_sha={source_sha}\n"
        ).encode("ascii")
        invalid_count_line = count_line.replace(b"logical_total=2", b"logical_total=3")
        for child_output, expected_output in (
            (
                ordinary_failure_diagnostic + failed_count_line + valid_failure_status,
                ordinary_failure_diagnostic + failed_count_line + valid_failure_status,
            ),
            (
                ordinary_failure_diagnostic + invalid_count_line + valid_failure_status,
                ordinary_failure_diagnostic + valid_failure_status,
            ),
        ):
            child_code = (
                f"import os; os.write(1, bytes.fromhex({child_output.hex()!r})); "
                "raise SystemExit(1)"
            )
            failure_output = StringIO()
            with redirect_stdout(failure_output):
                failure_exit = platform_workflow_remote_dispatch._run_bounded_child(
                    [sys.executable, "-c", child_code],
                    timeout_seconds=3,
                    expected_live_launch_sha=source_sha,
                    expected_live_app_sha=app_sha,
                    expected_live_marker_sha256=marker_sha,
                )
            self.assertEqual(failure_exit, 1)
            self.assertEqual(failure_output.getvalue(), expected_output.decode("ascii"))
        self.assertIsNone(
            platform_workflow_remote_dispatch._parse_live_launch_protocol(
                status_line + count_line,
                child_status=0,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256=marker_sha,
            )
        )
        self.assertIsNone(
            platform_workflow_remote_dispatch._parse_live_launch_protocol(
                count_line + count_line + status_line,
                child_status=0,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256=marker_sha,
            )
        )
        invalid_count_line = count_line.replace(b"logical_total=2", b"logical_total=3")
        partial = platform_workflow_remote_dispatch._parse_live_launch_protocol(
            invalid_count_line + status_line,
            child_status=0,
            expected_sha=source_sha,
            expected_app_sha=app_sha,
            expected_marker_sha256=marker_sha,
        )
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial, (expected_status, None))
        self.assertIsNone(
            platform_workflow_remote_dispatch._parse_live_browser_counts(
                count_line,
                expected_sha=source_sha,
                expected_app_sha=app_sha,
                expected_marker_sha256="d" * 64,
            )
        )

    def test_qa_child_capture_drains_both_streams_and_retains_only_safe_summary(self) -> None:
        secret = b"private-token-sentinel"
        late_error = b"\nError: browserType.launch: private detail"
        script = (
            "import os,sys\n"
            "os.write(1, b'x' * 70000 + " + repr(secret) + ")\n"
            "os.write(2, b'y' * 70000 + " + repr(late_error) + ")\n"
            "raise SystemExit(2)\n"
        )
        status, marker = platform_live_qa_guard._capture_qa_child(
            [sys.executable, "-c", script], timeout_seconds=3.0
        )
        self.assertEqual(status, 2)
        match = platform_live_qa_guard.LIVE_QA_CHILD_MARKER_RE.fullmatch(marker)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "unclassified")
        self.assertEqual(int(match.group(2)), 70000 + len(secret))
        self.assertEqual(int(match.group(3)), 70000 + len(late_error))
        self.assertEqual(match.group(4), "true")
        self.assertNotIn(secret.decode("ascii"), marker)
        wide_count = (
            "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=unclassified "
            "stdout_bytes=123456789 stderr_bytes=987654321 "
            "truncated=true child_exit=2\n"
        )
        wide_match = platform_live_qa_guard.LIVE_QA_CHILD_MARKER_RE.fullmatch(wide_count)
        self.assertIsNotNone(wide_match)
        assert wide_match is not None
        self.assertEqual(int(wide_match.group(2)), 123456789)
        self.assertEqual(int(wide_match.group(3)), 987654321)

    def test_qa_child_capture_timeout_reaps_the_child_group(self) -> None:
        status, marker = platform_live_qa_guard._capture_qa_child(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.05,
        )
        self.assertEqual(status, 124)
        self.assertRegex(marker, r"kind=child_timeout .* child_exit=124\n$")

    def test_qa_child_timeout_stops_and_verifies_only_the_fixed_browser_unit(self) -> None:
        stop = SimpleNamespace(returncode=0, stdout=b"")
        state = SimpleNamespace(
            returncode=0,
            stdout=b"LoadState=loaded\nActiveState=inactive\n",
        )
        with (
            patch.object(platform_live_qa_guard.subprocess, "run", side_effect=(stop, state)) as run,
            patch.object(platform_live_qa_guard, "_liveqa_cgroup_process_ids", return_value=()),
        ):
            self.assertTrue(platform_live_qa_guard._stop_timed_out_qa_browser_unit())
        self.assertEqual(run.call_args_list[0].args[0], [
            "/usr/bin/systemctl",
            "--no-ask-password",
            "stop",
            "oldsparky-liveqa-browser.service",
        ])
        self.assertEqual(run.call_args_list[1].args[0][-1], "oldsparky-liveqa-browser.service")

        failed = SimpleNamespace(returncode=1, stdout=b"")
        with patch.object(platform_live_qa_guard.subprocess, "run", return_value=failed):
            self.assertFalse(platform_live_qa_guard._stop_timed_out_qa_browser_unit())

    def test_qa_child_runner_accepts_only_the_fixed_playwright_scope(self) -> None:
        runtime = "/var/lib/oldsparky-liveqa/runtime-suite-" + "a" * 40
        command = [
            "/usr/bin/systemd-run",
            "--no-ask-password",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            "--expand-environment=no",
            "--unit=oldsparky-liveqa-browser.service",
            "--uid=1001",
            "--gid=1001",
            f"--working-directory={runtime}/web",
            "--property=KillMode=control-group",
            "--property=Restart=no",
            "--property=RuntimeMaxSec=30min",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=5s",
            "--property=UMask=0077",
            "--",
            "/usr/bin/env",
            "-i",
            "LANG=C.UTF-8",
            f"{runtime}/node/bin/node",
            f"{runtime}/web/node_modules/@playwright/test/cli.js",
            "test",
            f"--config={runtime}/web/playwright.live.config.ts",
            f"{runtime}/web/tests/smoke/live-launch.spec.ts",
        ]
        self.assertTrue(platform_live_qa_guard._is_fixed_live_browser_command(command))
        arbitrary = list(command)
        arbitrary[23] = "/bin/sh"
        self.assertFalse(platform_live_qa_guard._is_fixed_live_browser_command(arbitrary))

        suite_root = (
            "/var/lib/oldsparky-liveqa/runtime-suite-"
            + "a" * 40
            + "-fixture1"
        )
        engine_root = (
            "/var/lib/oldsparky-liveqa/runtime-engine-"
            + "b" * 40
            + "-fixture1"
        )
        browser_command = [
            "/usr/bin/systemd-run",
            "--no-ask-password",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            "--expand-environment=no",
            "--unit=oldsparky-liveqa-browser.service",
            "--uid=1001",
            "--gid=1001",
            f"--working-directory={suite_root}/web",
            "--property=KillMode=control-group",
            "--property=Restart=no",
            "--property=RuntimeMaxSec=30min",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=5s",
            "--property=UMask=0077",
            f"--property=BindReadOnlyPaths={suite_root}:{suite_root}",
            f"--property=BindReadOnlyPaths={engine_root}:{engine_root}",
            "--",
            "/usr/bin/env",
            "-i",
            (
                "CHROME_DEVEL_SANDBOX="
                f"{engine_root}/{platform_live_qa_guard.CHROMIUM_SANDBOX_RELATIVE.as_posix()}"
            ),
            "LANG=C.UTF-8",
            f"NODE_PATH={engine_root}/web/node_modules",
            f"PATH={engine_root}/node/bin:/usr/bin:/bin",
            f"PLAYWRIGHT_BROWSERS_PATH={engine_root}/browsers",
            f"{engine_root}/node/bin/node",
            f"{engine_root}/web/node_modules/@playwright/test/cli.js",
            "test",
            f"--config={suite_root}/web/playwright.live.config.ts",
            f"{suite_root}/web/tests/smoke/live-user-journey.spec.ts",
            "--project=live-desktop",
        ]
        self.assertTrue(
            platform_live_qa_guard._is_fixed_live_user_browser_command(browser_command)
        )
        mismatched_engine = list(browser_command)
        node_position = mismatched_engine.index(f"{engine_root}/node/bin/node")
        mismatched_engine[node_position] = f"{suite_root}/node/bin/node"
        self.assertFalse(
            platform_live_qa_guard._is_fixed_live_user_browser_command(
                mismatched_engine
            )
        )
        with self.assertRaises(platform_live_qa_guard.GuardError):
            platform_live_qa_guard._capture_qa_child(
                browser_command, timeout_seconds=1500.0
            )
        with self.assertRaises(platform_live_qa_guard.GuardError):
            platform_live_qa_guard._capture_qa_child(
                arbitrary,
                timeout_seconds=1500.0,
                live_user_browser=True,
            )

        marker = (
            "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=1 "
            "stderr_bytes=0 truncated=false child_exit=0\n"
        )
        output = StringIO()
        with (
            patch.object(platform_live_qa_guard.os, "geteuid", return_value=0),
            patch.object(
                platform_live_qa_guard,
                "_capture_qa_child",
                return_value=(0, marker),
            ) as capture,
            redirect_stdout(output),
        ):
            result = platform_live_qa_guard.main(
                ["capture-qa-browser-child", "--", *browser_command]
            )
        self.assertEqual(result, 0)
        capture.assert_called_once_with(
            browser_command,
            timeout_seconds=1500.0,
            live_user_browser=True,
        )
        self.assertEqual(output.getvalue(), marker)

        with (
            patch.object(platform_live_qa_guard.os, "geteuid", return_value=0),
            patch.object(
                platform_live_qa_guard,
                "_capture_qa_child",
                return_value=(0, marker),
            ) as capture,
            redirect_stdout(StringIO()),
        ):
            result = platform_live_qa_guard.main(
                ["capture-qa-browser-child", "--", *command]
            )
        self.assertEqual(result, 0)
        capture.assert_called_once_with(
            command,
            timeout_seconds=300.0,
            live_user_browser=False,
        )

    def test_deploy_marker_capture_keeps_bounded_timeout_cleanup(self) -> None:
        expected = ("deploy", "gha-123456-2-aaaaaaaaaaaa", "a" * 40)
        child_code = (
            "import os,time\n"
            "while True:\n"
            " try: os.write(1, b'x' * 4096)\n"
            " except BrokenPipeError: time.sleep(30)\n"
        )
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", child_code],
                timeout_seconds=0.05,
                expected_release_marker=expected,
            )
        self.assertEqual(result, 124)
        self.assertEqual(stdout.getvalue(), "")

    def test_deploy_marker_selector_failure_terminates_and_closes_child(self) -> None:
        expected = ("deploy", "gha-123456-2-aaaaaaaaaaaa", "a" * 40)
        child_code = (
            "import os,time\n"
            "while True:\n"
            " try: os.write(1, b'x' * 4096)\n"
            " except OSError: time.sleep(30)\n"
        )
        children: list[subprocess.Popen[bytes]] = []
        real_popen = platform_workflow_remote_dispatch.subprocess.Popen

        def launch_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            child = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            return child

        with (
            patch.object(
                platform_workflow_remote_dispatch.subprocess,
                "Popen",
                side_effect=launch_child,
            ),
            patch.object(
                platform_workflow_remote_dispatch.selectors,
                "DefaultSelector",
                side_effect=OSError(24, "too many open files"),
            ),
        ):
            result = platform_workflow_remote_dispatch._run_bounded_child(
                [sys.executable, "-c", child_code],
                timeout_seconds=5,
                expected_release_marker=expected,
            )
        self.assertEqual(result, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNotNone(children[0].stdout)
        self.assertTrue(children[0].stdout.closed)

    def test_bounded_dispatch_child_terminates_process_group_on_timeout(self) -> None:
        child = Mock(pid=9876)
        child.wait.side_effect = [
            subprocess.TimeoutExpired(["helper"], 1),
            None,
        ]
        killpg_calls = 0

        def kill_group(_pgid: int, sig: int) -> None:
            nonlocal killpg_calls
            killpg_calls += 1
            if sig == 0:
                raise ProcessLookupError

        with (
            patch.object(
                platform_workflow_remote_dispatch.subprocess,
                "Popen",
                return_value=child,
            ) as popen,
            patch.object(
                platform_workflow_remote_dispatch.os,
                "killpg",
                side_effect=kill_group,
            ) as killpg,
        ):
            result = platform_workflow_remote_dispatch._run_bounded_child(
                ["helper"], timeout_seconds=1
            )
        self.assertEqual(result, 124)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(killpg_calls, 2)
        self.assertEqual(killpg.call_args_list[0].args, (9876, platform_workflow_remote_dispatch.signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (9876, 0))

    def test_cleanup_export_inventory_is_closed_and_idempotent(self) -> None:
        # The app-release dispatcher is deliberately not a privileged export
        # remover. Only the exact pinned immutable host-tools generation may
        # reach the fixed setpriv executor; filesystem inventory and deletion
        # are covered by the executor's real non-root tests.
        with patch.object(
            platform_workflow_remote_dispatch,
            "_current_pin_matches_host_generation",
            return_value=False,
        ), patch.object(
            platform_workflow_remote_dispatch.subprocess,
            "Popen",
        ) as popen:
            self.assertEqual(
                platform_workflow_remote_dispatch._remove_exports(
                    load_run_id="41",
                    cleanup_run_id="42",
                    target_sha="a" * 40,
                ),
                1,
            )
            popen.assert_not_called()
        self.assertFalse(
            platform_workflow_remote_dispatch._current_pin_matches_host_generation(
                target_sha="not-a-source-sha"
            )
        )

    def test_export_remove_dispatch_forwards_only_bounded_closed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor = root / "executor.py"
            executor.write_text(
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write(" + repr("ARTIFACT_EXPORT_REMOVE schema=1 status=passed removed_roots=2\n") + ")\n",
                encoding="ascii",
            )
            executor.chmod(0o644)
            setpriv_shim = root / "setpriv-shim"
            setpriv_shim.write_text(
                "#!/bin/sh\n"
                "while [ \"$#\" -gt 0 ] && [ \"$1\" != -- ]; do shift; done\n"
                "[ \"$#\" -gt 0 ] || exit 125\nshift\nexec \"$@\"\n",
                encoding="ascii",
            )
            setpriv_shim.chmod(0o755)

            def invoke(output: bytes, status: int = 0, *, timeout: float = 1.0) -> tuple[int, str]:
                script = (
                    "import sys; sys.stdin.buffer.read(); "
                    f"sys.stdout.buffer.write({output!r}); raise SystemExit({status})"
                )
                actual_popen = subprocess.Popen

                def shimmed_popen(command, **kwargs):
                    self.assertEqual(command[0], str(setpriv_shim))
                    self.assertIn(str(executor), command)
                    return actual_popen(
                        [sys.executable, "-c", script], **kwargs
                    )

                stdout = StringIO()
                with (
                    patch.object(platform_workflow_remote_dispatch, "SETPRIV", str(setpriv_shim)),
                    patch.object(platform_workflow_remote_dispatch, "SYSTEM_PYTHON", "/usr/bin/python3.12"),
                    patch.object(platform_workflow_remote_dispatch, "RETAINED_LOAD_EXPORT_EXECUTOR", executor),
                    patch.object(platform_workflow_remote_dispatch, "CLEANUP_OPERATION_TIMEOUT_SECONDS", timeout),
                    patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True),
                    patch.object(platform_workflow_remote_dispatch, "_trusted_export_executor", return_value=True),
                    patch.object(platform_workflow_remote_dispatch, "_retained_load_export_owner", return_value={"uid": 990, "gid": 982}),
                    patch.object(platform_workflow_remote_dispatch.os, "geteuid", return_value=0),
                    patch.object(platform_workflow_remote_dispatch.subprocess, "Popen", side_effect=shimmed_popen),
                    redirect_stdout(stdout),
                ):
                    result = platform_workflow_remote_dispatch._run_retained_export_executor(
                        "remove", {"schema": 1, "load_run_id": "41", "cleanup_run_id": "42"}
                    )
                return result, stdout.getvalue()

            passed, passed_output = invoke(
                b"ARTIFACT_EXPORT_REMOVE schema=1 status=passed removed_roots=2\n"
            )
            self.assertEqual(passed, 0)
            self.assertEqual(
                passed_output,
                "ARTIFACT_EXPORT_REMOVE schema=1 status=passed removed_roots=2\n",
            )
            failed, failed_output = invoke(
                b"ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class=export_root_changed\n",
                status=1,
            )
            self.assertEqual(failed, 1)
            self.assertEqual(
                failed_output,
                "ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class=export_root_changed\n",
            )
            for output, status in (
                (b"PRIVATE_SENTINEL\n", 1),
                (b"x" * 129, 0),
            ):
                result, forwarded = invoke(output, status=status)
                self.assertEqual(result, 1)
                self.assertEqual(
                    forwarded,
                    "ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class=dispatcher_protocol\n"
                    if len(output) <= 128
                    else "ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class=dispatcher_output_limit\n",
                )
                self.assertNotIn("PRIVATE_SENTINEL", forwarded)

            preflight_stdout = StringIO()
            with (
                patch.object(
                    platform_workflow_remote_dispatch,
                    "load_stdin_payload",
                    side_effect=platform_workflow_remote_dispatch.WorkflowInputError(
                        "private payload detail"
                    ),
                ),
                redirect_stdout(preflight_stdout),
            ):
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["retained-cleanup-exports"]),
                    1,
                )
            self.assertEqual(
                preflight_stdout.getvalue(),
                "ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class=dispatcher_preflight\n",
            )

    def test_cleanup_workflows_project_public_artifacts_before_private_deletion(self) -> None:
        external = (
            REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        ).read_text(encoding="utf-8")
        retained = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text(encoding="utf-8")
        for source, dispatch in (
            (external, "external-cleanup-exports"),
            (retained, "retained-cleanup-exports"),
        ):
            start = source.index("      - name: Exact cleanup") if dispatch.startswith("external") else source.index("      - name: Clean the exact retained load run")
            end = source.index("      - name:", start + 1)
            step = source[start:end]
            self.assertIn("project_cleanup_summary", step)
            self.assertLess(step.index("project_cleanup_summary"), step.index(dispatch))
            self.assertNotRegex(step, r"scp[^\n]*\|\| true")
            self.assertIn("summary_copy_status", step)
            self.assertIn("canonical_copy_status", step)
            self.assertIn("projection_status", step)
            self.assertIn("cleanup_artifact_status", step)
            self.assertIn("cleanup_export_status", step)
            self.assertIn("canonical.log", step)
            self.assertIn('rm -f -- "$cleanup_summary" "$public_summary"', step)
            self.assertIn("cleanup_summary_invalid", step)
            self.assertIn("cleanup_summary_unavailable", step)
        self.assertIn("steps.cleanup.outputs.cleanup_export_status", external)
        self.assertIn("steps.run-cleanup.outputs.cleanup_export_status", retained)
        for workflow in (external, retained):
            self.assertIn("RETAINED_CLEANUP_DIAGNOSTIC", workflow)
            self.assertIn("external_vote_recovery", workflow)
            self.assertIn("remote_ssh_exit_code", workflow)
            self.assertIn('>> "$GITHUB_STEP_SUMMARY"', workflow)

        project_start = retained.index("          project_cleanup_summary() {")
        project_end = retained.index(
            "\n          runner_failure_stage=ssh_invocation", project_start
        )
        project_function = textwrap.dedent(retained[project_start:project_end])
        receipt = {
            "ok": True,
            "markers": 0,
            "users_deleted": 0,
            "tournaments_deleted": 0,
            "control_account_preserved": True,
            "never_started": True,
            "load_run_id": "567890",
            "load_run_attempt": "2",
            "load_source_sha": "d" * 40,
        }
        with tempfile.TemporaryDirectory(prefix="retained-cleanup-summary-projection-") as temporary:
            root = Path(temporary)
            source = root / "source.json"
            destination = root / "projected.json"
            for mutation, expected_attempt, expected_sha, expected_status in (
                (receipt, "2", "d" * 40, 0),
                ({**receipt, "load_run_attempt": "1"}, "2", "d" * 40, 1),
                ({**receipt, "load_source_sha": "e" * 40}, "2", "d" * 40, 1),
                (receipt, "", "", 1),
            ):
                source.write_text(json.dumps(mutation), encoding="utf-8")
                destination.unlink(missing_ok=True)
                completed = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        project_function
                        + '\nproject_cleanup_summary "$1" "$2" "$3" "$4" "$5"\n',
                        "bash",
                        str(source),
                        str(destination),
                        "567890",
                        expected_attempt,
                        expected_sha,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, expected_status, completed.stderr)

        abort = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("      load_run_attempt:", abort)
        self.assertIn("LOAD_RUN_ATTEMPT: ${{ inputs.load_run_attempt }}", abort)
        remote_invocation = abort.split("sudo -n -- /usr/bin/bash -s --", 1)[1]
        remote_invocation = remote_invocation.split("<<'REMOTE'", 1)[0]
        normalized_invocation = " ".join(remote_invocation.replace("\\\n", " ").split())
        self.assertIn(
            '"$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$HELPER_SHA256" '
            '"$HELPER_SOURCE_SHA" "$TARGET_SHA" "$LOAD_RUNNER_SHA" "$LOAD_RUN_ID" '
            '"$LOAD_RUN_ATTEMPT" "$QA_CONFIRMATION"',
            normalized_invocation,
        )
        self.assertIn("id: selected-load", abort)
        self.assertIn('expected_path = ".github/workflows/platform-production-external-load.yml"', abort)
        self.assertIn('selected.get("event") != "workflow_dispatch"', abort)
        self.assertIn('selected.get("head_branch") != "dev"', abort)
        self.assertIn('repository.get("full_name") != "StrayForest/old_sparky"', abort)
        for source, marker, next_step, fields in (
            (
                external,
                "      - name: Diagnose origin evidence publication gate",
                "      - name: Publish origin evidence",
                ("ORIGIN_PUBLISH_GATE", "explicit_conditions_met=", "ORIGIN_OBSERVER_READY"),
            ),
            (
                abort,
                "      - name: Diagnose abort evidence inputs",
                "      - name: Normalize abort evidence",
                ("RETAINED_ABORT_INPUT", "process_tree", "observer"),
            ),
            (
                abort,
                "      - name: Diagnose abort evidence normalization",
                "      - name: Publish abort evidence",
                ("RETAINED_ABORT_EVIDENCE", "evidence_status", "truncated"),
            ),
            (
                retained,
                "      - name: Diagnose cleanup evidence inputs",
                "      - name: Normalize cleanup evidence",
                ("RETAINED_CLEANUP_INPUT", "canonical_raw", "summary"),
            ),
            (
                retained,
                "      - name: Diagnose normalized cleanup evidence",
                "      - name: Reject incomplete cleanup log evidence",
                ("RETAINED_CLEANUP_EVIDENCE", "summary_error", "canonical"),
            ),
        ):
            start = source.index(marker)
            end = source.index(next_step, start + 1)
            step = source[start:end]
            self.assertIn("if: ${{ always() }}", step)
            for field in fields:
                self.assertIn(field, step)
            self.assertNotIn("GITHUB_STEP_SUMMARY", step)
            # Source/run identities are consumed as private parser inputs so
            # receipts can be bound to their invocation. Secret-bearing SSH
            # and control-account values must never enter these diagnostics,
            # and identity values must not be printed to public runner output.
            self.assertNotRegex(step, r"(?:PROD_SSH|CONTROL_EMAIL)")
            self.assertNotRegex(
                step,
                r"(?im)^\s*(?:echo|printf|print\s*\()[^\n]*(?:\$TARGET_SHA|"
                r"\$LOAD_RUN_ID|TARGET_SHA|LOAD_RUN_ID)",
            )
            self.assertNotRegex(step, r"print\([^\n]*(?:path|raw|payload|email|secret)")
            if source is retained and marker == "      - name: Diagnose cleanup evidence inputs":
                self.assertIn(
                    "CLEANUP_LOAD_RUN_ID: ${{ inputs.load_run_id }}", step
                )
                self.assertIn(
                    'os.environ.get("CLEANUP_LOAD_RUN_ID", "")', step
                )

        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' && steps.normalize_abort_evidence.outcome == 'success' }}",
            abort,
        )
        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' && steps.diagnose-cleanup-inputs.outcome == 'success' && steps.validate_cleanup_evidence.outcome == 'success' }}",
            retained,
        )
        self.assertIn(
            "test \"${{ steps.run-cleanup.outputs.cleanup_artifact_status }}\" = \"0\"",
            retained,
        )
        self.assertIn('elif error_class is None:', retained)
        self.assertIn(
            'summary_error = "none" if payload.get("ok") is True else "missing"',
            retained,
        )
        self._exercise_retained_cleanup_outputs_survive_marker_parse_failure()

    def _exercise_retained_cleanup_outputs_survive_marker_parse_failure(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text(encoding="utf-8")
        step_start = workflow.index(
            "      - name: Clean the exact retained load run on production\n"
        )
        run_start = workflow.index("        run: |\n", step_start) + len("        run: |\n")
        run_end = workflow.index("\n      - name:", run_start)
        script = textwrap.dedent(workflow[run_start:run_end])
        source_sha, app_sha = "a" * 40, "b" * 40
        marker = (
            "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=complete "
            "dispatcher_exit=0 child_state=exited child_exit=0 stdout_eof=true "
            f"timed_out=false source_sha={source_sha} app_sha={app_sha} "
            "run_id=123456 run_attempt=2 load_run_id=234567 "
            "profile=retained-load-cleanup"
        )
        self.assertIn("if parsed_cleanup_diagnostic=", script)
        self.assertIn("trap cleanup_exit_trap EXIT", script)
        failure_gate = workflow.split(
            "      - name: Fail if cleanup was not verified\n", 1
        )[1].split("\n      - name:", 1)[0]
        self.assertIn("remote_exit_code", failure_gate)
        self.assertIn("remote_diagnostic_status", failure_gate)

        with tempfile.TemporaryDirectory(prefix="retained-cleanup-shell-test-") as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            fixtures = root / "fixtures"
            bin_dir.mkdir(mode=0o700)
            fixtures.mkdir(mode=0o700)
            (fixtures / "cleanup-summary.json").write_text(
                '{"schema":1,"ok":true,"markers":1,"users_deleted":2,'
                '"tournaments_deleted":3,"control_account_preserved":true}\n',
                encoding="ascii",
            )
            (fixtures / "canonical.log").write_text(
                '{"schema":1,"status":"passed","line_count":1,'
                '"retained_line_count":1,"truncated":false}\n',
                encoding="ascii",
            )
            ssh = bin_dir / "ssh"
            ssh.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/bash
                    set -euo pipefail
                    if [[ " $* " == *" retained-cleanup-exports "* ]]; then exit 0; fi
                    printf 'called\\n' >> "$SSH_INVOKED"
                    case "$SSH_MODE" in
                      valid) printf '%s\\n' "$SSH_MARKER"; exit 0 ;;
                      invalid) printf '%s\\n' 'INVALID_MARKER RAW_SENTINEL'; exit 0 ;;
                      missing) printf '%s\\n' 'Permission denied (publickey).' >&2; exit 255 ;;
                    esac
                    exit 70
                    """
                ),
                encoding="ascii",
            )
            ssh.chmod(0o700)
            scp = bin_dir / "scp"
            scp.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/bash
                    set -euo pipefail
                    previous=""
                    current=""
                    for argument in "$@"; do
                      previous="$current"
                      current="$argument"
                    done
                    destination="$current"
                    source="$previous"
                    case "$source" in
                      */cleanup-summary.json) cp "$FIXTURES/cleanup-summary.json" "$destination" ;;
                      */canonical.log) cp "$FIXTURES/canonical.log" "$destination" ;;
                      *) exit 71 ;;
                    esac
                    """
                ),
                encoding="ascii",
            )
            scp.chmod(0o700)
            install = bin_dir / "install"
            install.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/bash
                    set -euo pipefail
                    if [[ "$FAIL_INSTALL" == "1" && "$1" == "-d" ]]; then exit 73; fi
                    exec /usr/bin/install "$@"
                    """
                ),
                encoding="ascii",
            )
            install.chmod(0o700)

            for mode in ("valid", "invalid", "missing", "setup-failure"):
                with self.subTest(mode=mode):
                    case_root = root / mode
                    case_root.mkdir(mode=0o700)
                    runner_temp = case_root / "runner-temp"
                    runner_temp.mkdir(mode=0o700)
                    input_path = runner_temp / "platform-retained-cleanup-input.json"
                    input_path.write_text("{}\n", encoding="ascii")
                    input_path.chmod(0o600)
                    ssh_dir = runner_temp / "production-retained-cleanup-ssh"
                    ssh_dir.mkdir(mode=0o700)
                    for name, content in (("config", "Host *\n"), ("id_ed25519", "synthetic\n")):
                        item = ssh_dir / name
                        item.write_text(content, encoding="ascii")
                        item.chmod(0o600)
                    output_path = case_root / "github-output"
                    output_path.touch(mode=0o600)
                    summary_path = case_root / "step-summary"
                    summary_path.touch(mode=0o600)
                    invoked = case_root / "ssh-invoked"
                    env = {
                        "PATH": f"{bin_dir}:/usr/bin:/bin",
                        "RUNNER_TEMP": str(runner_temp),
                        "SSH_DIR": str(ssh_dir),
                        "GITHUB_OUTPUT": str(output_path),
                        "GITHUB_STEP_SUMMARY": str(summary_path),
                        "GITHUB_RUN_ID": "123456",
                        "GITHUB_RUN_ATTEMPT": "2",
                        "HOST_TOOLS_SHA": "c" * 40,
                        "TARGET_SHA": source_sha,
                        "APP_TARGET_SHA": app_sha,
                        "LOAD_RUN_ID": "234567",
                        "NEVER_STARTED_ATTEMPT": "",
                        "NEVER_STARTED_SOURCE_SHA": "",
                        "PROD_SSH_HOST": "fixture.invalid",
                        "PROD_SSH_USER": "synthetic-runner",
                        "SSH_MODE": (
                            "valid" if mode == "valid" else
                            "invalid" if mode == "invalid" else "missing"
                        ),
                        "SSH_MARKER": marker,
                        "SSH_INVOKED": str(invoked),
                        "FIXTURES": str(fixtures),
                        "FAIL_INSTALL": "1" if mode == "setup-failure" else "0",
                    }
                    result = subprocess.run(
                        ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=10,
                    )
                    if mode == "setup-failure":
                        self.assertEqual(result.returncode, 73)
                    else:
                        self.assertEqual(
                            result.returncode,
                            0,
                            result.stdout + result.stderr + output_path.read_text() + summary_path.read_text(),
                        )
                    fields = dict(
                        line.split("=", 1) for line in output_path.read_text().splitlines()
                    )
                    self.assertEqual(fields["remote_exit_code"], "0" if mode == "valid" else "1")
                    self.assertEqual(fields["remote_diagnostic_status"], "0" if mode == "valid" else "1")
                    self.assertEqual(fields["remote_stage"], "complete" if mode == "valid" else "unknown")
                    self.assertEqual(
                        fields["runner_failure_stage"],
                        {
                            "valid": "remote_dispatch_complete",
                            "invalid": "remote_diagnostic_invalid",
                            "missing": "remote_diagnostic_invalid",
                            "setup-failure": "artifact_directory_setup",
                        }[mode],
                    )
                    self.assertEqual(
                        fields["remote_stderr_class"],
                        {
                            "valid": "empty",
                            "invalid": "empty",
                            "missing": "authentication_denied",
                            "setup-failure": "unavailable",
                        }[mode],
                    )
                    summary = summary_path.read_text(encoding="utf-8")
                    self.assertIn("stderr_bytes=", summary)
                    self.assertNotIn("RAW_SENTINEL", summary)
                    self.assertNotIn("Permission denied", summary)
                    self.assertFalse((runner_temp / "production-retained-cleanup-ssh-output").exists())
                    self.assertFalse((runner_temp / "production-retained-cleanup-ssh-stderr").exists())
                    self.assertFalse(input_path.exists())
                    self.assertFalse(ssh_dir.exists())
                    self.assertEqual(invoked.exists(), mode != "setup-failure")

    def test_live_launch_inline_handoff_verifier_uses_api_digest_and_exact_run(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-live-launch.yml").read_text(
            encoding="utf-8"
        )
        step = workflow.split(
            "      - name: Authenticate and validate live-launch handoff artifact\n", 1
        )[1].split("      - name: Configure SSH\n", 1)[0]
        run_block = step.split("        run: |\n", 1)[1]
        inline = run_block.split("/usr/bin/python3 - <<'PY'\n", 1)[1].split(
            "\n          PY\n", 1
        )[0]
        inline = textwrap.dedent(inline)
        runner_sha = "a" * 40

        class Response:
            status = 200

            def __init__(self, content: bytes):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, maximum: int) -> bytes:
                return self.content[:maximum]

        def canonical(value: object) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")

        def run_case(case: str) -> bool:
            payload = {
                "schema": "3" if case == "wrong-schema" else (
                    "2" if case in {"valid-noop", "wrong-binding"} else "1"
                ),
                "base_url": "https://old-sparky.com",
                "provision": "false",
                "marker": "",
                "target_sha": runner_sha,
            }
            app_sha = "b" * 40
            if payload["schema"] == "2":
                source_binding = {
                    "schema": 1,
                    "binding_mode": "verified-noop",
                    "runner_sha": runner_sha,
                    "app_target_sha": app_sha,
                    "baseline_identity": {
                        "schema": 1,
                        "source_sha": app_sha,
                        "release_slug": "gha-123456-1-bbbbbbbbbbbb",
                        "release_json_sha256": "c" * 64,
                        "current_link_dev": 100,
                        "current_link_ino": 101,
                        "release_dev": 100,
                        "release_ino": 102,
                        "pending_operation": False,
                    },
                    "receipt_document_sha256": "d" * 64,
                    "receipt_artifact_id": "456789",
                    "receipt_artifact_name": "platform-production-noop-source-receipt-123456-2",
                    "receipt_artifact_digest": "sha256:" + "e" * 64,
                    "receipt_archive_sha256": "e" * 64,
                    "cumulative_manifest_sha256": "f" * 64,
                    "source_security_run_id": "234567",
                    "source_security_run_attempt": "1",
                    "autodeploy_run_id": "345678",
                    "autodeploy_run_attempt": "3",
                    "production_deploy_run_id": "123456",
                    "production_deploy_run_attempt": "2",
                }
                if case == "wrong-binding":
                    source_binding["app_target_sha"] = "c" * 40
                payload["source_binding"] = source_binding
            member_name = "other.json" if case == "wrong-member" else "live-launch-input.json"
            raw = canonical(payload) + b"\n"
            if case == "oversized-member":
                raw = b"x" * (20 * 1024 + 1)
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                zipped.writestr(member_name, raw)
            archive_bytes = archive_buffer.getvalue()
            real_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
            api_digest = "missing" if case == "missing-api-digest" else (
                "sha256:invalid" if case == "invalid-api-digest" else real_digest
            )
            metadata_run = {"id": 123456, "head_sha": runner_sha}
            if case == "wrong-present-attempt":
                metadata_run["run_attempt"] = 2
            elif case != "missing-metadata-attempt":
                metadata_run["run_attempt"] = 1
            run_record = {
                "id": 123456,
                "run_attempt": 1,
                "workflow_id": 88,
                "head_sha": runner_sha,
                "head_branch": "dev",
                "event": "workflow_dispatch",
                "status": "in_progress",
            }
            if case == "wrong-run-attempt":
                run_record["run_attempt"] = True
            workflow_record = {
                "id": 88,
                "path": ".github/workflows/platform-live-launch.yml",
                "state": "active",
            }
            name = "platform-live-launch-input-123456-1"
            row_name = "wrong-name" if case == "wrong-artifact-name" else name
            row_id = 457 if case == "wrong-artifact-id" else 456
            listing_row = {"id": row_id, "name": row_name, "size_in_bytes": len(archive_bytes),
                           "digest": real_digest, "expired": False}
            metadata = {
                "id": 456,
                "name": name,
                "expired": False,
                "size_in_bytes": len(archive_bytes),
                "digest": ("sha256:" + "0" * 64) if case == "zip-digest-mismatch" else api_digest,
                "workflow_run": metadata_run,
            }
            if case == "wrong-size":
                metadata["size_in_bytes"] = len(archive_bytes) + 1
            base = "https://api.github.com/repos/example/repo/"
            responses = {
                base + "actions/workflows/platform-live-launch.yml": workflow_record,
                base + "actions/runs/123456": run_record,
                base + "actions/runs/123456/artifacts?per_page=100&page=1": {
                    "total_count": 1,
                    "artifacts": [listing_row],
                },
                base + "actions/artifacts/456": metadata,
            }

            class FakeOpener:
                def open(self, request, timeout=30):
                    if request.full_url.endswith("/actions/artifacts/456/zip"):
                        return Response(archive_bytes)
                    value = responses.get(request.full_url)
                    if value is None:
                        raise AssertionError(f"unexpected test URL {request.full_url}")
                    return Response(json.dumps(value).encode("ascii"))

            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "live-launch-input.json"
                github_env = Path(directory) / "github-env"
                github_env.write_text("", encoding="ascii")
                expected_app_sha = app_sha if payload.get("schema") == "2" else runner_sha
                expected_binding_digest = (
                    hashlib.sha256(canonical(payload["source_binding"])).hexdigest()
                    if payload.get("schema") == "2"
                    else ""
                )
                env = {
                    "TARGET_SHA": runner_sha,
                    "GITHUB_RUN_ID": "123456",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_REPOSITORY": "example/repo",
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REF": "refs/heads/dev",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "HANDOFF_ARTIFACT_ID": "456",
                    "HANDOFF_ARTIFACT_NAME": name,
                    "HANDOFF_MEMBER_NAME": "live-launch-input.json",
                    "HANDOFF_OUTPUT_PATH": str(output),
                    "GITHUB_ENV": str(github_env),
                    "GH_TOKEN": "test-only-token",
                }
                success = False
                verifier_namespace = {"__name__": "__main__"}
                with patch.dict(os.environ, env, clear=False), \
                    patch("urllib.request.build_opener", side_effect=lambda *_handlers: FakeOpener()), \
                    redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                    try:
                        exec(compile(inline, "live-launch-inline-verifier", "exec"), verifier_namespace)
                    except SystemExit:
                        success = False
                    else:
                        success = True
                if success:
                    self.assertTrue(output.is_file())
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(output.read_bytes(), raw)
                    environment = github_env.read_text(encoding="ascii")
                    self.assertIn(f"APP_TARGET_SHA={expected_app_sha}\n", environment)
                    self.assertIn(f"SOURCE_BINDING_SHA256={expected_binding_digest}\n", environment)
                    api_handler = verifier_namespace["SameOriginRedirect"]()
                    request = urllib.request.Request(
                        "https://api.github.com/repos/example/repo/x",
                        headers={"Authorization": "Bearer test-only", "Cookie": "session=test-only"},
                    )
                    self.assertIsNotNone(
                        api_handler.redirect_request(
                            request, None, 302, "Found", {},
                            "https://api.github.com/repos/example/repo/next",
                        )
                    )
                    self.assertIsNone(
                        api_handler.redirect_request(
                            request, None, 302, "Found", {}, "https://blob.example/file",
                        )
                    )
                    artifact_handler = verifier_namespace["ArtifactRedirect"]()
                    cross_host = artifact_handler.redirect_request(
                        request, None, 302, "Found", {}, "https://blob.example/file",
                    )
                    self.assertIsNotNone(cross_host)
                    forwarded = {
                        key.casefold()
                        for collection in (cross_host.headers, cross_host.unredirected_hdrs)
                        for key in collection
                    }
                    self.assertTrue(
                        {"authorization", "cookie", "proxy-authorization", "cookie2"}.isdisjoint(forwarded)
                    )
                    self.assertIsNone(
                        artifact_handler.redirect_request(
                            request, None, 302, "Found", {}, "http://blob.example/file",
                        )
                    )
                else:
                    self.assertFalse(output.exists())
                return success

        self.assertNotIn("HANDOFF_ARTIFACT_DIGEST", step)
        self.assertTrue(run_case("valid"))
        self.assertTrue(run_case("valid-noop"))
        self.assertTrue(run_case("missing-metadata-attempt"))
        for invalid in (
            "wrong-present-attempt",
            "wrong-run-attempt",
            "missing-api-digest",
            "invalid-api-digest",
            "wrong-size",
            "wrong-artifact-id",
            "wrong-artifact-name",
            "zip-digest-mismatch",
            "wrong-member",
            "oversized-member",
            "wrong-schema",
            "wrong-binding",
        ):
            with self.subTest(case=invalid):
                self.assertFalse(run_case(invalid))

    def test_live_user_qa_is_dispatchable_and_runs_on_the_server(self) -> None:
        source = (REPO_ROOT / ".github/workflows/platform-live-user-qa.yml").read_text(
            encoding="utf-8"
        )
        dispatcher = (
            REPO_ROOT / "platform/tools/platform_live_user_qa_dispatch.py"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", source)
        self.assertIn("RUN-LIVE-USER-QA", source)
        self.assertIn("ssh", source)
        self.assertIn("LIVE_USER_QA_SUCCESS", source)
        self.assertIn("LIVE_BROWSER_COUNTS schema=1", source)
        self.assertIn(
            "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
            source,
        )
        self.assertIn("HANDOFF_ARTIFACT_ID", source)
        self.assertIn('metadata.get("digest")', source)
        self.assertIn("hashlib.sha256(archive_bytes)", source)
        self.assertIn('stream.write(f"APP_TARGET_SHA={app}\\n")', source)
        self.assertIn("SameOriginHTTPS", source)
        self.assertIn("SafeArtifactHTTPS", source)
        self.assertIn("LIVE_USER_HANDOFF_VERIFIED", source)
        self.assertIn("APP_TARGET_SHA", source)
        self.assertIn(
            "live-user-qa",
            source,
        )
        self.assertNotIn("bash -s", source)
        self.assertNotIn("/root/old_sparky", source)
        secret_job = source.split("  live-user-qa:\n", 1)[1]
        validator_outputs = source.split("    outputs:\n", 1)[1].split(
            "    permissions:\n", 1
        )[0]
        self.assertEqual(
            [line.strip().split(":", 1)[0] for line in validator_outputs.splitlines() if line.strip()],
            ["handoff_artifact_id"],
        )
        self.assertNotIn("actions/checkout", secret_job)
        self.assertNotIn("platform_noop_source_binding.py", secret_job)
        self.assertIn("contents: none", secret_job)
        self.assertLess(
            secret_job.index("LIVE_USER_HANDOFF_VERIFIED"),
            secret_job.index("- name: Configure SSH"),
        )
        self.assertIn("TRUSTED_LIVE_QA_ROOT", dispatcher)
        self.assertIn("platform_live_user_qa_trusted.sh", dispatcher)
        self.assertIn("platform_live_qa_mailbox_helper.py", dispatcher)
        self.assertIn("PLATFORM_LIVE_QA_TARGET_SHA", dispatcher)
        user_wrapper = (TOOLS_ROOT / "platform_live_user_qa.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("prepare-live-user-failure-diagnostics", user_wrapper)
        self.assertIn("--persist-failure-report --fail-on-failure-counts", user_wrapper)
        self.assertLess(
            user_wrapper.index("prepare-live-user-failure-diagnostics"),
            user_wrapper.index("capture-qa-browser-child"),
        )
        self.assertGreater(
            user_wrapper.index("printf 'LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=%s"),
            user_wrapper.index("emit-public-browser-counts"),
        )
        preflight_comment = user_wrapper.index(
            "# Retention is best-effort and must not prevent the authorized QA journey."
        )
        preflight_start = user_wrapper.rfind(
            "if (( TRUSTED_MODE == 1 ))", 0, preflight_comment
        )
        preflight_end = user_wrapper.index("\nfi", preflight_start) + len("\nfi")
        preflight = user_wrapper[preflight_start:preflight_end]
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture = Path(temporary)
            rejected = fixture / "guard"
            rejected.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
            rejected.chmod(0o700)
            result = subprocess.run(
                [
                    "/bin/bash", "-c",
                    "set -euo pipefail\nTRUSTED_MODE=1\n"
                    "BROWSER_GATE=live-user-qa.Abc123\n"
                    f"LIVE_QA_RUNNER_SHA={'a' * 40}\n"
                    f"LIVE_QA_MARKER_SHA256={'c' * 64}\n"
                    f"SOURCE_COMMIT={'b' * 40}\n"
                    f"GUARD=({shlex.quote(str(rejected))})\n"
                    + preflight + "\nprintf 'browser-started\\n'\n",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "browser-started\n", ""))

            count_start = user_wrapper.index("diagnostic_valid=0\ndiagnostic_kind=")
            count_end_marker = 'exit "$browser_status"'
            count_end = user_wrapper.rindex(count_end_marker, count_start) + len(count_end_marker)
            count_tail = user_wrapper[count_start:count_end]
            line = (
                "LIVE_BROWSER_COUNTS schema=1 run_status=failed logical_total=1 logical_pass=0 "
                "logical_fail=1 logical_expected_fail=0 logical_flaky=0 logical_skip=0 "
                "logical_interrupted=0 attempt_total=1 attempt_pass=0 attempt_fail=1 "
                "attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 "
                f"source_sha={'a' * 40} app_sha={'b' * 40} marker_sha256={'c' * 64}"
            )

            def run_user_count_tail(count_status: int, child_status: int) -> tuple[subprocess.CompletedProcess[str], str]:
                fake_guard = fixture / f"count-guard-{count_status}-{child_status}"
                trace = fixture / f"count-args-{count_status}-{child_status}"
                fake_guard.write_text(
                    "#!/bin/sh\n"
                    'if [ "$1" = emit-public-browser-counts ]; then\n'
                    '  printf "%s\\n" "$FAKE_COUNT_LINE"\n'
                    '  printf "%s\\n" "$*" > "$FAKE_COUNT_TRACE"\n'
                    '  exit "$FAKE_COUNT_STATUS"\n'
                    "fi\nexit 2\n",
                    encoding="ascii",
                )
                fake_guard.chmod(0o700)
                marker = (
                    "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=browser_launch_error "
                    f"stdout_bytes=0 stderr_bytes=0 truncated=false child_exit={child_status}"
                    if child_status
                    else "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none "
                    "stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=0"
                )
                result = subprocess.run(
                    [
                        "/bin/bash", "-c",
                        "set -euo pipefail\nTRUSTED_MODE=1\n"
                        "BROWSER_GATE=live-user-qa.Abc123\n"
                        f"LIVE_QA_RUNNER_SHA={'a' * 40}\n"
                        f"LIVE_QA_MARKER_SHA256={'c' * 64}\n"
                        f"SOURCE_COMMIT={'b' * 40}\n"
                        f"browser_status={child_status}\n"
                        f"browser_diagnostic={shlex.quote(marker)}\n"
                        f"GUARD=({shlex.quote(str(fake_guard))})\n"
                        + count_tail,
                    ],
                    env={
                        **os.environ,
                        "FAKE_COUNT_LINE": line,
                        "FAKE_COUNT_STATUS": str(count_status),
                        "FAKE_COUNT_TRACE": str(trace),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                )
                return result, trace.read_text(encoding="ascii")

            persisted_failure, count_args = run_user_count_tail(43, 0)
            self.assertEqual(
                persisted_failure.returncode, 1,
                f"stdout={persisted_failure.stdout!r} stderr={persisted_failure.stderr!r}",
            )
            self.assertIn("LIVE_BROWSER_COUNTS schema=1", persisted_failure.stdout)
            self.assertNotIn("LIVE_QA_CHILD_DIAGNOSTIC", persisted_failure.stdout)
            self.assertIn("--persist-failure-report --fail-on-failure-counts", count_args)
            unavailable, _count_args = run_user_count_tail(42, 7)
            self.assertEqual(unavailable.returncode, 7)
            self.assertIn("kind=failure_report_unavailable", unavailable.stdout)
            self.assertIn("child_exit=7", unavailable.stdout)
        self.assertEqual(
            dispatcher.splitlines()[0],
            "#!/usr/bin/env -S /usr/bin/python3.12 -I -B",
        )

        with tempfile.TemporaryDirectory(prefix="live-user-shebang-") as directory:
            root = Path(directory)
            ambient = root / "ambient"
            ambient.mkdir()
            site_sentinel = root / "sitecustomize-imported"
            (ambient / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(site_sentinel)!r}).write_text('unexpected')\n",
                encoding="utf-8",
            )
            (ambient / "ambient_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
            helper = root / "direct-entry.py"
            helper.write_text(
                dispatcher.splitlines()[0]
                + "\nimport os, pathlib, sys\n"
                + "if not sys.flags.isolated or not sys.dont_write_bytecode: raise SystemExit(31)\n"
                + "if pathlib.Path(os.environ['SITE_SENTINEL']).exists(): raise SystemExit(32)\n"
                + "if str(pathlib.Path(os.environ['AMBIENT_ROOT'])) in sys.path: raise SystemExit(33)\n"
                + "sys.path.insert(0, os.environ['AMBIENT_ROOT'])\n"
                + "import ambient_probe\n"
                + "print('isolated-bytecode-disabled')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            direct_env = {
                **os.environ,
                "PYTHONPATH": str(ambient),
                "SITE_SENTINEL": str(site_sentinel),
                "AMBIENT_ROOT": str(ambient),
            }
            direct = subprocess.run(
                [str(helper)],
                check=False,
                capture_output=True,
                text=True,
                env=direct_env,
                timeout=5,
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(direct.stdout, "isolated-bytecode-disabled\n")
            self.assertFalse(site_sentinel.exists())
            self.assertFalse((ambient / "__pycache__").exists())

    def test_live_user_dispatch_forwards_only_one_bound_playwright_count_record(self) -> None:
        dispatch = platform_workflow_remote_dispatch
        source_sha = "a" * 40
        app_sha = "b" * 40
        count = (
            "LIVE_BROWSER_COUNTS schema=1 run_status=passed logical_total=1 logical_pass=1 "
            "logical_fail=0 logical_expected_fail=0 logical_flaky=0 logical_skip=0 "
            "logical_interrupted=0 attempt_total=1 attempt_pass=1 attempt_fail=0 "
            "attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 "
            f"source_sha={source_sha} app_sha={app_sha} marker_sha256={'c' * 64}\n"
        ).encode("ascii")
        diagnostic = (
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=128 "
            b"stderr_bytes=0 truncated=false child_exit=0\n"
        )
        real_popen = subprocess.Popen

        class Capture:
            def __init__(self) -> None:
                self.buffer = BytesIO()
                self.text = StringIO()

            def write(self, value: str) -> int:
                return self.text.write(value)

            def flush(self) -> None:
                self.text.flush()

            def output(self) -> bytes:
                return self.buffer.getvalue() + self.text.getvalue().encode("ascii")

        def invoke(child_stdout: bytes, child_exit: int = 0) -> tuple[int, bytes]:
            capture = Capture()

            def local_child(_command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
                script = (
                    "import sys; "
                    f"sys.stdout.buffer.write({child_stdout!r}); "
                    f"sys.stdout.flush(); sys.stderr.write('secret-sentinel\\n'); raise SystemExit({child_exit})"
                )
                return real_popen(
                    [sys.executable, "-c", script],
                    **kwargs,
                )

            with patch.object(dispatch, "_trusted_helper", return_value=True), \
                patch.object(dispatch.subprocess, "Popen", side_effect=local_child), \
                patch.object(dispatch.sys, "stdout", capture):
                status = dispatch._run_live_user_qa_sudo(
                    [source_sha], expected_sha=source_sha, expected_app_sha=app_sha
                )
            return status, capture.output()

        status, output = invoke(diagnostic + count)
        self.assertEqual(status, 0, output.decode("ascii", errors="replace"))
        self.assertIn(count, output)
        self.assertIn(b"LIVE_USER_QA_SUCCESS\n", output)
        self.assertNotIn(b"secret-sentinel", output)

        early_dispatch = (
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            b"stage=trusted_generation check=generation_manifest "
            b"stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1\n"
        )
        dispatch_status, dispatch_output = invoke(early_dispatch, child_exit=1)
        self.assertEqual(dispatch_status, 1)
        self.assertEqual(dispatch_output, early_dispatch)
        self.assertNotIn(b"secret-sentinel", dispatch_output)

        for invalid_diagnostic in (
            early_dispatch.replace(b"stage=trusted_generation", b"stage=unknown"),
            early_dispatch.replace(b"child_exit=1", b"child_exit=2"),
            early_dispatch.replace(b"stderr_bytes=0", b"stderr_bytes=21"),
        ):
            invalid_status, invalid_output = invoke(invalid_diagnostic, child_exit=1)
            self.assertEqual(invalid_status, 1)
            self.assertIn(b"kind=unclassified", invalid_output)
            self.assertIn(b"child_exit=1\n", invalid_output)
            self.assertNotIn(b"secret-sentinel", invalid_output)

        for invalid_output in (
            diagnostic,
            diagnostic + count + count,
            diagnostic + count.replace(source_sha.encode(), b"e" * 40),
        ):
            with self.subTest(invalid_output=invalid_output[:40]):
                invalid_status, invalid_forwarded = invoke(invalid_output)
                self.assertEqual(invalid_status, 2)
                self.assertIn(b"kind=protocol_failure stage=dispatch check=child_protocol", invalid_forwarded)
                self.assertNotIn(b"LIVE_USER_QA_SUCCESS", invalid_forwarded)
                self.assertNotIn(b"secret-sentinel", invalid_forwarded)

        private_child_text = b"private child output that must not escape\n"
        unclassified_status, unclassified_output = invoke(
            private_child_text,
            child_exit=7,
        )
        self.assertEqual(unclassified_status, 7)
        expected_unclassified = dispatch._live_qa_diagnostic_line(
            kind="unclassified",
            stdout_bytes=len(private_child_text),
            stderr_bytes=len(b"secret-sentinel\n"),
            child_exit=7,
        )
        self.assertEqual(unclassified_output, expected_unclassified)
        self.assertNotIn(private_child_text, unclassified_output)
        self.assertNotIn(b"secret-sentinel", unclassified_output)

        empty_child_status, empty_child_output = invoke(b"")
        self.assertEqual(empty_child_status, 2)
        self.assertIn(
            b"kind=protocol_failure stage=dispatch check=child_protocol "
            b"stdout_bytes=0 stderr_bytes=16 truncated=false child_exit=0\n",
            empty_child_output,
        )
        self.assertNotIn(b"LIVE_USER_QA_SUCCESS", empty_child_output)

        counted_child_status, counted_child_output = invoke(count)
        self.assertEqual(counted_child_status, 2)
        self.assertIn(count, counted_child_output)
        self.assertIn(
            b"kind=protocol_failure stage=dispatch check=child_protocol",
            counted_child_output,
        )
        self.assertNotIn(b"LIVE_USER_QA_SUCCESS", counted_child_output)

        for stage, checks in dispatch.LIVE_QA_DISPATCH_FAILURE_CHECKS.items():
            for check in checks:
                candidate = dispatch._live_qa_diagnostic_line(
                    kind="dispatch_failure",
                    stage=stage,
                    check=check,
                    child_exit=2,
                )
                with self.subTest(stage=stage, check=check):
                    self.assertTrue(dispatch._valid_live_qa_diagnostic(candidate, 2), candidate)

        self.assertTrue(
            dispatch.LIVE_QA_DISPATCH_FAILURE_CHECKS["dispatch"]
            >= platform_live_user_qa_dispatch.LIVE_USER_QA_FAILURE_CHECKS["dispatch"]
        )

        failed_helper_capture = Capture()
        with (
            patch.object(dispatch, "_trusted_helper", return_value=False),
            patch.object(dispatch.sys, "stdout", failed_helper_capture),
        ):
            missing_helper_status = dispatch._run_live_user_qa_sudo(
                [source_sha], expected_sha=source_sha, expected_app_sha=app_sha
            )
        self.assertEqual(missing_helper_status, 2)
        self.assertEqual(
            failed_helper_capture.output(),
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            b"stage=trusted_generation check=trusted_entry "
            b"stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=2\n",
        )

        spawn_failure_capture = Capture()
        with (
            patch.object(dispatch, "_trusted_helper", return_value=True),
            patch.object(dispatch.subprocess, "Popen", side_effect=OSError("private")),
            patch.object(dispatch.sys, "stdout", spawn_failure_capture),
        ):
            spawn_failure_status = dispatch._run_live_user_qa_sudo(
                [source_sha], expected_sha=source_sha, expected_app_sha=app_sha
            )
        self.assertEqual(spawn_failure_status, 2)
        self.assertEqual(
            spawn_failure_capture.output(),
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            b"stage=dispatch check=child_spawn stdout_bytes=0 stderr_bytes=0 "
            b"truncated=false child_exit=2\n",
        )

        input_failure_capture = Capture()
        with (
            patch.object(dispatch, "load_stdin_payload", side_effect=ValueError("private")),
            patch.object(dispatch.sys, "stdout", input_failure_capture),
        ):
            input_failure_status = dispatch.main(["live-user-qa"])
        self.assertEqual(input_failure_status, 2)
        self.assertEqual(
            input_failure_capture.output(),
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            b"stage=validation check=input_shape stdout_bytes=0 stderr_bytes=0 "
            b"truncated=false child_exit=2\n",
        )

        source_binding_capture = Capture()
        with (
            patch.object(
                dispatch,
                "load_stdin_payload",
                return_value={
                    "base_url": "https://old-sparky.com",
                    "provision": "false",
                    "marker": "",
                    "target_sha": source_sha,
                },
            ),
            patch.object(
                dispatch,
                "_source_binding_context",
                side_effect=ValueError("private-source-binding-detail"),
            ),
            patch.object(dispatch.sys, "stdout", source_binding_capture),
        ):
            source_binding_status = dispatch.main(["live-user-qa"])
        self.assertEqual(source_binding_status, 2)
        self.assertEqual(
            source_binding_capture.output(),
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            b"stage=validation check=source_binding stdout_bytes=0 stderr_bytes=0 "
            b"truncated=false child_exit=2\n",
        )
        self.assertNotIn(b"private-source-binding-detail", source_binding_capture.output())

        all_skipped = count.replace(
            b"logical_total=1 logical_pass=1", b"logical_total=1 logical_pass=0"
        ).replace(b"logical_skip=0", b"logical_skip=1").replace(
            b"attempt_pass=1", b"attempt_pass=0"
        ).replace(b"attempt_skip=0", b"attempt_skip=1")
        skipped_status, skipped_output = invoke(diagnostic + all_skipped)
        self.assertEqual(skipped_status, 0)
        self.assertIn(all_skipped, skipped_output)
        self.assertNotIn(b"LIVE_USER_QA_SUCCESS", skipped_output)

        timed_processes: list[subprocess.Popen[bytes]] = []
        timeout_capture = Capture()

        def sleeping_child(_command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            process = real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                **kwargs,
            )
            timed_processes.append(process)
            return process

        with (
            patch.object(dispatch, "_trusted_helper", return_value=True),
            patch.object(dispatch, "LIVE_USER_QA_OPERATION_TIMEOUT_SECONDS", 0.05),
            patch.object(dispatch.subprocess, "Popen", side_effect=sleeping_child),
            patch.object(dispatch.sys, "stdout", timeout_capture),
            patch.object(
                dispatch,
                "_terminate_process_group",
                wraps=dispatch._terminate_process_group,
            ) as terminate_group,
        ):
            timed_status = dispatch._run_live_user_qa_sudo(
                [source_sha], expected_sha=source_sha, expected_app_sha=app_sha
            )
        self.assertEqual(timed_status, 124)
        terminate_group.assert_called_once()
        self.assertEqual(len(timed_processes), 1)
        self.assertIsNotNone(timed_processes[0].poll())
        self.assertEqual(
            timeout_capture.output(),
            b"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=child_timeout "
            b"stdout_bytes=0 stderr_bytes=0 truncated=true child_exit=124\n",
        )

    def test_live_user_dispatch_reports_only_fixed_early_preflight_boundary(self) -> None:
        dispatch = platform_live_user_qa_dispatch
        source_sha = "a" * 40
        app_sha = "b" * 40
        output = StringIO()
        with (
            patch.object(dispatch, "_source_binding_context", return_value=(app_sha, {}, None)),
            patch.object(dispatch.os, "geteuid", return_value=0),
            patch.object(dispatch, "_verify_install", side_effect=RuntimeError("private-path-secret")),
            patch.object(dispatch.sys, "stdout", output),
        ):
            status = dispatch.main(["run-locked", source_sha])

        self.assertEqual(status, 1)
        self.assertEqual(
            output.getvalue(),
            "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
            "stage=trusted_generation check=generation_manifest "
            "stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1\n",
        )
        self.assertNotIn("private-path-secret", output.getvalue())

    def test_live_user_inline_handoff_verifier_authenticates_and_fails_closed(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-live-user-qa.yml").read_text(
            encoding="utf-8"
        )
        step = workflow.split(
            "      - name: Authenticate and install closed live-user QA handoff\n", 1
        )[1].split("      - name: Configure SSH\n", 1)[0]
        run_block = step.split("        run: |\n", 1)[1]
        inline = run_block.split("/usr/bin/python3 - <<'PY'\n", 1)[1].split(
            "\n          PY\n", 1
        )[0]
        inline = textwrap.dedent(inline)

        runner_sha = "a" * 40
        app_sha = "b" * 40
        binding = {
            "schema": 1,
            "binding_mode": "verified-noop",
            "runner_sha": runner_sha,
            "app_target_sha": app_sha,
            "baseline_identity": {
                "schema": 1,
                "source_sha": app_sha,
                "release_slug": "gha-123456-1-bbbbbbbbbbbb",
                "release_json_sha256": "c" * 64,
                "current_link_dev": 100,
                "current_link_ino": 101,
                "release_dev": 100,
                "release_ino": 102,
                "pending_operation": False,
            },
            "receipt_document_sha256": "d" * 64,
            "receipt_artifact_id": "456789",
            "receipt_artifact_name": "platform-production-noop-source-receipt-123456-2",
            "receipt_artifact_digest": "sha256:" + "e" * 64,
            "receipt_archive_sha256": "e" * 64,
            "cumulative_manifest_sha256": "f" * 64,
            "source_security_run_id": "234567",
            "source_security_run_attempt": "1",
            "autodeploy_run_id": "345678",
            "autodeploy_run_attempt": "3",
            "production_deploy_run_id": "123456",
            "production_deploy_run_attempt": "2",
        }

        def canonical(value: object) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")

        class Response:
            status = 200

            def __init__(self, content: bytes):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, maximum: int) -> bytes:
                return self.content[:maximum]

        def run_case(case: str) -> tuple[bool, str | None, int | None]:
            case_app_sha = runner_sha if case == "same-source" else app_sha
            payload: dict[str, object] = {
                "schema": "1" if case == "same-source" else "2",
                "base_url": "https://old-sparky.com",
                "provision": "false",
                "marker": "",
                "target_sha": runner_sha,
            }
            if case != "same-source":
                payload["source_binding"] = dict(binding)
            current_binding = payload.get("source_binding")
            assert current_binding is None or isinstance(current_binding, dict)
            expected_binding_digest = (
                hashlib.sha256(canonical(current_binding)).hexdigest()
                if isinstance(current_binding, dict)
                else ""
            )
            run_record: dict[str, object] = {
                "id": 123456,
                "run_attempt": 1,
                "workflow_id": 88,
                "head_sha": runner_sha,
                "head_branch": "dev",
                "event": "workflow_dispatch",
                "status": "in_progress",
            }
            if case == "wrong-run":
                run_record["id"] = 123457
            elif case == "boolean-attempt":
                run_record["run_attempt"] = True
            elif case == "wrong-schema":
                payload["schema"] = "3"
            elif case == "wrong-binding-digest":
                assert isinstance(current_binding, dict)
                current_binding["receipt_document_sha256"] = "z" * 64
            elif case == "wrong-binding-source":
                assert isinstance(current_binding, dict)
                current_binding["app_target_sha"] = "c" * 40
            expected_binding_digest = (
                hashlib.sha256(canonical(current_binding)).hexdigest()
                if isinstance(current_binding, dict)
                else ""
            )
            member_name = "unexpected.json" if case == "wrong-member" else "live-user-qa-input.json"
            member_raw = canonical(payload) + b"\n"
            if case == "oversized-member":
                member_raw = b"x" * (20 * 1024 + 1)
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(member_name, member_raw)
            archive_bytes = zip_buffer.getvalue()
            artifact_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
            artifact_name = "wrong-name" if case == "wrong-name" else "platform-live-user-qa-input-123456-1"
            metadata_digest = "sha256:" + "0" * 64 if case == "wrong-digest" else artifact_digest
            metadata_size = len(archive_bytes) + 1 if case == "wrong-size" else len(archive_bytes)
            workflow_record = {"id": 88, "path": ".github/workflows/platform-live-user-qa.yml", "state": "active"}
            artifact_row = {"id": 456, "name": artifact_name, "size_in_bytes": len(archive_bytes),
                            "digest": artifact_digest, "expired": False}
            workflow_run = {"id": 123456, "run_attempt": 1, "head_sha": runner_sha}
            if case == "missing-metadata-attempt":
                workflow_run.pop("run_attempt")
            elif case == "wrong-metadata-attempt":
                workflow_run["run_attempt"] = 2
            artifact_metadata = {
                "id": 456,
                "name": artifact_name,
                "size_in_bytes": metadata_size,
                "digest": metadata_digest,
                "expired": False,
                "workflow_run": workflow_run,
            }
            prefix = "https://api.github.com/repos/example/repo"
            responses = {
                prefix + "/actions/workflows/platform-live-user-qa.yml": workflow_record,
                prefix + "/actions/runs/123456": run_record,
                prefix + "/actions/runs/123456/artifacts?per_page=100&page=1": {
                    "total_count": 1,
                    "artifacts": [artifact_row],
                },
                prefix + "/actions/artifacts/456": artifact_metadata,
            }

            class FakeOpener:
                def open(self, request, timeout=30):
                    self.last_timeout = timeout
                    if request.full_url.endswith("/actions/artifacts/456/zip"):
                        return Response(archive_bytes)
                    value = responses.get(request.full_url)
                    if value is None:
                        raise AssertionError(f"unexpected test URL {request.full_url}")
                    return Response(json.dumps(value).encode("ascii"))

            env_base = {
                "TARGET_SHA": runner_sha,
                "GITHUB_SHA": runner_sha,
                "APP_TARGET_SHA": case_app_sha,
                "SOURCE_BINDING_SHA256": expected_binding_digest,
                "HANDOFF_ARTIFACT_ID": "456",
                "HANDOFF_ARTIFACT_NAME": "platform-live-user-qa-input-123456-1",
                "HANDOFF_MEMBER_NAME": "live-user-qa-input.json",
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_REPOSITORY": "example/repo",
                "GITHUB_RUN_ID": "123456",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_OUTPUT": "",
                "RUNNER_TEMP": "",
                "GH_TOKEN": "test-only-token",
            }
            with tempfile.TemporaryDirectory() as directory:
                env_base["GITHUB_OUTPUT"] = str(Path(directory) / "output.txt")
                env_base["GITHUB_ENV"] = str(Path(directory) / "environment.txt")
                env_base["RUNNER_TEMP"] = directory
                Path(env_base["GITHUB_OUTPUT"]).write_text("", encoding="ascii")
                Path(env_base["GITHUB_ENV"]).write_text("", encoding="ascii")
                output = Path(directory) / "live-user-qa-input.json"
                stdout = StringIO()
                success = False
                verifier_namespace = {"__name__": "__main__"}
                with patch.dict(os.environ, env_base, clear=False), \
                    patch("urllib.request.build_opener", side_effect=lambda *_handlers: FakeOpener()), \
                    redirect_stdout(stdout):
                    try:
                        exec(compile(inline, "live-user-inline-verifier", "exec"), verifier_namespace)
                    except SystemExit:
                        success = False
                    else:
                        success = True
                if success:
                    self.assertTrue(output.is_file())
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(output.read_bytes(), member_raw)
                    self.assertEqual(
                        Path(env_base["GITHUB_OUTPUT"]).read_text(encoding="ascii"),
                        f"source_binding_sha256={expected_binding_digest}\n",
                    )
                    self.assertEqual(
                        Path(env_base["GITHUB_ENV"]).read_text(encoding="ascii"),
                        f"APP_TARGET_SHA={case_app_sha}\nSOURCE_BINDING_SHA256={expected_binding_digest}\n",
                    )
                    self.assertEqual(stdout.getvalue(), "LIVE_USER_HANDOFF_VERIFIED\n")
                    api_handler = verifier_namespace["SameOriginHTTPS"]()
                    api_request = urllib.request.Request(
                        "https://api.github.com/repos/example/repo/x",
                        headers={"Authorization": "Bearer test-only", "Cookie": "session=test-only"},
                    )
                    self.assertIsNotNone(
                        api_handler.redirect_request(
                            api_request, None, 302, "Found", {},
                            "https://api.github.com/repos/example/repo/next",
                        )
                    )
                    self.assertIsNone(
                        api_handler.redirect_request(
                            api_request, None, 302, "Found", {}, "https://blob.example/file",
                        )
                    )
                    artifact_handler = verifier_namespace["SafeArtifactHTTPS"]()
                    artifact_request = urllib.request.Request(
                        "https://api.github.com/repos/example/repo/zip",
                        headers={"Authorization": "Bearer test-only", "Cookie": "session=test-only"},
                    )
                    same_host = artifact_handler.redirect_request(
                        artifact_request, None, 302, "Found", {},
                        "https://api.github.com/repos/example/repo/zip-next",
                    )
                    self.assertIsNotNone(same_host)
                    cross_host = artifact_handler.redirect_request(
                        artifact_request, None, 302, "Found", {}, "https://blob.example/file",
                    )
                    self.assertIsNotNone(cross_host)
                    forwarded = {
                        key.casefold()
                        for collection in (cross_host.headers, cross_host.unredirected_hdrs)
                        for key in collection
                    }
                    self.assertTrue(
                        {"authorization", "cookie", "proxy-authorization", "cookie2"}.isdisjoint(forwarded)
                    )
                    self.assertIsNone(
                        artifact_handler.redirect_request(
                            artifact_request, None, 302, "Found", {}, "http://blob.example/file",
                        )
                    )
                else:
                    self.assertFalse(output.exists())
                return success, stdout.getvalue(), output.stat().st_mode & 0o777 if output.exists() else None

        self.assertTrue(run_case("same-source")[0])
        self.assertTrue(run_case("missing-metadata-attempt")[0])
        self.assertTrue(run_case("valid-schema2")[0])
        for invalid_case in (
            "wrong-run",
            "boolean-attempt",
            "wrong-metadata-attempt",
            "wrong-name",
            "wrong-digest",
            "wrong-size",
            "wrong-member",
            "oversized-member",
            "wrong-schema",
            "wrong-binding-digest",
            "wrong-binding-source",
        ):
            with self.subTest(case=invalid_case):
                self.assertFalse(run_case(invalid_case)[0])

    def test_live_user_report_preserves_required_source_binding_identity(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-live-user-qa.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            'steps.authenticate_live_user_handoff.outputs.source_binding_sha256', workflow
        )
        self.assertIn('binding_digest="${SOURCE_BINDING_SHA256:-}"', workflow)

        sanitizer_step = workflow.split(
            "      - name: Sanitize live-user QA report\n", 1
        )[1].split("      - name: Remove production SSH material\n", 1)[0]
        sanitizer = textwrap.dedent(
            sanitizer_step.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        )
        validator_step = workflow.split(
            "      - name: Reject incomplete live-user QA report\n", 1
        )[1].split("      - name: QA summary\n", 1)[0]
        validator = textwrap.dedent(
            validator_step.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        )
        runner_sha = "a" * 40
        app_sha = "b" * 40
        binding_digest = "c" * 64
        marker_sha = "d" * 64
        counts = (
            "LIVE_BROWSER_COUNTS schema=1 run_status=passed logical_total=1 logical_pass=1 "
            "logical_fail=0 logical_expected_fail=0 logical_flaky=0 logical_skip=0 "
            "logical_interrupted=0 attempt_total=1 attempt_pass=1 attempt_fail=0 "
            "attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 "
            f"source_sha={runner_sha} app_sha={app_sha} marker_sha256={marker_sha}\n"
        )
        child = (
            "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=128 "
            "stderr_bytes=0 truncated=false child_exit=0\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.log"
            report = root / "report.json"
            raw.write_text(child + counts + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            valid = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["source_binding_sha256"],
                binding_digest,
            )
            passed_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(passed_report["status"], "passed")
            self.assertEqual(passed_report["coverage_status"], "complete")
            self.assertEqual(passed_report["test_count"], 1)
            self.assertEqual(passed_report["logical_total"], 1)
            self.assertEqual(passed_report["logical_skip_count"], 0)
            accepted = subprocess.run(
                [sys.executable, "-c", validator, str(report), runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                accepted.returncode, 0,
                accepted.stderr + json.dumps(passed_report, sort_keys=True),
            )

            missing_binding = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, ""],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing_binding.returncode, 0)

            raw.write_text(
                child + counts.replace(f"app_sha={app_sha}", f"app_sha={runner_sha}")
                + "LIVE_USER_QA_SUCCESS\n",
                encoding="utf-8",
            )
            same_source = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, runner_sha, ""],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(same_source.returncode, 0, same_source.stderr)
            same_source_valid = subprocess.run(
                [sys.executable, "-c", validator, str(report), runner_sha, runner_sha, ""],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(same_source_valid.returncode, 0, same_source_valid.stderr)

            raw.write_text(child + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            marker_only = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(marker_only.returncode, 0, marker_only.stderr)
            marker_only_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(marker_only_report["success"])
            self.assertEqual(marker_only_report["coverage_status"], "unavailable")
            self.assertIsNone(marker_only_report["test_count"])
            rejected_marker_only = subprocess.run(
                [sys.executable, "-c", validator, str(report), runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(rejected_marker_only.returncode, 0)

            all_skipped = counts.replace("logical_pass=1", "logical_pass=0").replace(
                "logical_skip=0", "logical_skip=1"
            ).replace("attempt_pass=1", "attempt_pass=0").replace(
                "attempt_skip=0", "attempt_skip=1"
            )
            raw.write_text(child + all_skipped + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            skipped_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(skipped_run.returncode, 0, skipped_run.stderr)
            skipped_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(skipped_report["success"])
            self.assertEqual(skipped_report["status"], "failed")
            self.assertEqual(skipped_report["test_count"], 0)

            unexpected_skip = counts.replace("logical_total=1", "logical_total=2").replace(
                "logical_skip=0", "logical_skip=1"
            ).replace("attempt_total=1", "attempt_total=2").replace(
                "attempt_skip=0", "attempt_skip=1"
            )
            raw.write_text(child + unexpected_skip + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            skipped_case = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(skipped_case.returncode, 0, skipped_case.stderr)
            skipped_case_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(skipped_case_report["success"])
            self.assertEqual(skipped_case_report["test_count"], 1)
            self.assertEqual(skipped_case_report["logical_skip_count"], 1)
            self.assertEqual(skipped_case_report["status"], "failed")

            expected_failure = counts.replace("logical_pass=1", "logical_pass=0").replace(
                "logical_expected_fail=0", "logical_expected_fail=1"
            ).replace("attempt_pass=1", "attempt_pass=0").replace(
                "attempt_fail=0", "attempt_fail=1"
            )
            raw.write_text(child + expected_failure + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            expected_failure_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(expected_failure_run.returncode, 0, expected_failure_run.stderr)
            expected_failure_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(expected_failure_report["success"])
            self.assertEqual(expected_failure_report["logical_expected_fail_count"], 1)
            self.assertEqual(expected_failure_report["status"], "failed")

            raw.write_text(child + counts + counts + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            duplicate_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(duplicate_run.returncode, 0, duplicate_run.stderr)
            duplicate_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(duplicate_report["coverage_status"], "invalid")
            self.assertEqual(duplicate_report["status"], "error")

            stale = counts.replace(f"source_sha={runner_sha}", f"source_sha={'e' * 40}")
            raw.write_text(child + stale + "LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
            stale_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(stale_run.returncode, 0, stale_run.stderr)
            stale_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(stale_report["coverage_status"], "invalid")
            self.assertFalse(stale_report["success"])

            raw.write_text("", encoding="utf-8")
            no_cause = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "2", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(no_cause.returncode, 0, no_cause.stderr)
            no_cause_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(no_cause_report["status"], "failed")
            self.assertEqual(
                no_cause_report["error_class_counts"],
                {"unclassified_child_or_dispatch_failure": 1},
            )
            self.assertEqual(no_cause_report["failure_origin"], "unclassified")

            failed_counts = (
                counts.replace("run_status=passed", "run_status=failed")
                .replace("logical_pass=1", "logical_pass=0")
                .replace("logical_fail=0", "logical_fail=1")
                .replace("attempt_pass=1", "attempt_pass=0")
                .replace("attempt_fail=0", "attempt_fail=1")
            )
            unavailable = (
                "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=failure_report_unavailable "
                "stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=0\n"
            )
            raw.write_text(unavailable + failed_counts, encoding="ascii")
            unavailable_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "1", runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(unavailable_run.returncode, 0, unavailable_run.stderr)
            unavailable_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(unavailable_report["status"], "failed")
            self.assertFalse(unavailable_report["success"])
            self.assertEqual(unavailable_report["failure_origin"], "browser_child")
            self.assertEqual(unavailable_report["coverage_status"], "complete")
            self.assertEqual(unavailable_report["test_count"], 1)
            self.assertEqual(
                unavailable_report["error_class_counts"],
                {"browser_child_failure_report_unavailable": 1},
            )
            self.assertEqual(
                unavailable_report["child_diagnostic"]["kind"],
                "failure_report_unavailable",
            )
            self.assertNotIn("private", json.dumps(unavailable_report))
            raw.write_text(unavailable, encoding="ascii")
            invalid_unavailable = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "2", runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid_unavailable.returncode, 0, invalid_unavailable.stderr)
            invalid_unavailable_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(invalid_unavailable_report["status"], "error")
            self.assertIsNone(invalid_unavailable_report["child_diagnostic"])

            for stage, checks in platform_workflow_remote_dispatch.LIVE_QA_DISPATCH_FAILURE_CHECKS.items():
                for check in checks:
                    marker = platform_workflow_remote_dispatch._live_qa_diagnostic_line(
                        kind="dispatch_failure",
                        stage=stage,
                        check=check,
                        child_exit=2,
                    )
                    raw.write_bytes(marker)
                    accepted_stage = subprocess.run(
                        [sys.executable, "-c", sanitizer, str(raw), str(report), "2", runner_sha, app_sha, binding_digest],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(accepted_stage.returncode, 0, accepted_stage.stderr)
                    stage_report = json.loads(report.read_text(encoding="utf-8"))
                    self.assertEqual(stage_report["status"], "failed")
                    self.assertEqual(stage_report["failure_origin"], "dispatcher")
                    self.assertEqual(stage_report["child_diagnostic"]["stage"], stage)
                    self.assertEqual(stage_report["child_diagnostic"]["check"], check)

            protocol_failure = platform_workflow_remote_dispatch._live_qa_diagnostic_line(
                kind="protocol_failure",
                stage="dispatch",
                check="child_protocol",
                child_exit=0,
            )
            self.assertTrue(
                platform_workflow_remote_dispatch._valid_live_qa_diagnostic(
                    protocol_failure, 2
                )
            )
            raw.write_bytes(protocol_failure)
            protocol_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "2", runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(protocol_run.returncode, 0, protocol_run.stderr)
            protocol_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(protocol_report["status"], "failed")
            self.assertFalse(protocol_report["success"])
            self.assertEqual(protocol_report["failure_origin"], "dispatcher")
            self.assertEqual(protocol_report["child_diagnostic"]["kind"], "protocol_failure")
            for invalid_protocol, ssh_status in (
                (protocol_failure.replace(b"child_exit=0", b"child_exit=1"), 2),
                (protocol_failure, 1),
                (
                    protocol_failure.replace(b"check=child_protocol", b"check=child_spawn"),
                    2,
                ),
            ):
                raw.write_bytes(invalid_protocol)
                invalid_protocol_run = subprocess.run(
                    [sys.executable, "-c", sanitizer, str(raw), str(report), str(ssh_status), runner_sha, app_sha, binding_digest],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(invalid_protocol_run.returncode, 0, invalid_protocol_run.stderr)
                invalid_protocol_report = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(invalid_protocol_report["status"], "error")
                self.assertIsNone(invalid_protocol_report["child_diagnostic"])

            unclassified_marker = platform_workflow_remote_dispatch._live_qa_diagnostic_line(
                kind="unclassified",
                stdout_bytes=37,
                stderr_bytes=19,
                child_exit=7,
            )
            raw.write_bytes(unclassified_marker)
            unclassified_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "7", runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(unclassified_run.returncode, 0, unclassified_run.stderr)
            unclassified_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(unclassified_report["failure_origin"], "unclassified")
            self.assertEqual(unclassified_report["child_diagnostic"]["kind"], "unclassified")
            self.assertNotIn("private", json.dumps(unclassified_report))

            dispatch_failure = (
                "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
                "stage=trusted_generation check=generation_manifest "
                "stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1\n"
            )
            raw.write_text(dispatch_failure, encoding="ascii")
            dispatch_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "1", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(dispatch_run.returncode, 0, dispatch_run.stderr)
            dispatch_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(dispatch_report["status"], "failed")
            self.assertEqual(dispatch_report["failure_origin"], "dispatcher")
            self.assertEqual(dispatch_report["test_count"], None)
            self.assertIsNone(dispatch_report["browser_counts"])
            self.assertEqual(dispatch_report["error_class_counts"], {"live_user_dispatch_failure": 1})
            self.assertEqual(
                dispatch_report["child_diagnostic"]["stage"], "trusted_generation"
            )
            self.assertEqual(
                dispatch_report["child_diagnostic"]["check"], "generation_manifest"
            )

            for invalid_marker in (
                dispatch_failure.replace("stage=trusted_generation", "stage=untrusted"),
                dispatch_failure.replace("child_exit=1", "child_exit=2"),
            ):
                raw.write_text(invalid_marker, encoding="ascii")
                invalid_run = subprocess.run(
                    [sys.executable, "-c", sanitizer, str(raw), str(report), "1", runner_sha, app_sha, binding_digest],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(invalid_run.returncode, 0, invalid_run.stderr)
                invalid_report = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(invalid_report["status"], "error")
                self.assertEqual(invalid_report["failure_origin"], "unclassified")
                self.assertIsNone(invalid_report["child_diagnostic"])
                self.assertIsNone(invalid_report["test_count"])

            raw.write_text("", encoding="ascii")
            transport_run = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "255", runner_sha, app_sha, binding_digest],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(transport_run.returncode, 0, transport_run.stderr)
            transport_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                transport_report["failure_origin"], "ssh_exit_255_unclassified"
            )
            self.assertEqual(
                transport_report["error_class_counts"],
                {"ssh_exit_255_unclassified": 1},
            )

    def test_all_wrappers_disable_xtrace_before_any_work(self) -> None:
        for wrapper in WRAPPERS:
            with self.subTest(wrapper=wrapper.name):
                lines = wrapper.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines[1], "set +x")
                self.assertNotIn("set -x", lines)

    def test_root_supervisors_never_source_runtime_or_production_env(self) -> None:
        for wrapper in SUPERVISORS:
            with self.subTest(wrapper=wrapper.name):
                source = wrapper.read_text(encoding="utf-8")
                # Reject shell source commands specifically.  Plain-language
                # comments and diagnostics may legitimately mention a source
                # checkout or source SHA.
                self.assertNotRegex(source, r"(?m)^\s*(?:source|\.)\s+")
                self.assertNotIn("platform_runtime_common", source)
                self.assertNotIn("platform_load_env_file", source)
                self.assertNotIn("${PYTHONPATH", source)
                self.assertRegex(
                    source,
                    r'(?m)^\s*TRUSTED_REPO_ROOT="/root/old_sparky"\s*$',
                )
                self.assertRegex(
                    source,
                    r'(?m)^\s*PLATFORM_ROOT="\$TRUSTED_(?:REPO|INSTALL)_ROOT/platform"\s*$',
                )
                self.assertRegex(
                    source,
                    r'(?m)^\s*TOOLS_DIR="\$PLATFORM_ROOT/tools"\s*$',
                )
                self.assertIn('SYSTEM_PYTHON="/usr/bin/python3.12"', source)
                self.assertIn("platform_safe_env_exec.py", source)

    def test_trusted_database_callers_use_manifest_pythonpath(self) -> None:
        for wrapper_name in (
            "platform_provision_live_csp_qa.sh",
            "platform_live_user_qa.sh",
        ):
            with self.subTest(wrapper=wrapper_name):
                source = (TOOLS_ROOT / wrapper_name).read_text(encoding="utf-8")
                self.assertIn('SAFE_PYTHONPATH="$PLATFORM_ROOT"', source)
                self.assertIn('SAFE_PYTHONPATH="$TRUSTED_INSTALL_ROOT"', source)
                self.assertIn('--pythonpath "$SAFE_PYTHONPATH"', source)
                self.assertNotIn('--pythonpath "$PLATFORM_ROOT"', source)

    def test_all_live_operations_enter_the_machine_lock_guard(self) -> None:
        for wrapper in SUPERVISORS:
            with self.subTest(wrapper=wrapper.name):
                source = wrapper.read_text(encoding="utf-8")
                self.assertIn("PLATFORM_LIVE_QA_LOCK_FD", source)
                self.assertIn("locked-exec", source)
                self.assertIn("assert-lock", source)
        for wrapper in BROWSER_WRAPPERS:
            with self.subTest(recovery_wrapper=wrapper.name):
                self.assertIn(
                    "recovery-locked-exec",
                    wrapper.read_text(encoding="utf-8"),
                )
        live_user = (TOOLS_ROOT / "platform_live_user_qa.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$1" == "recover-bundle"', live_user)
        self.assertIn('prepare-stale-bundle-cleanup-state', live_user)
        self.assertIn('recover_exact_state "$QA_STATE_DIR"', live_user)
        self.assertIn('trap stale_bundle_cleanup_exit EXIT', live_user)
        stale_mode = live_user[
            live_user.index('if (( $# == 1 )) && [[ "$1" == "recover-bundle" ]]; then\n  if'):
            live_user.index(
                '\nif (( $# == 2 )) && [[ "$1" == "recover-setup" ]]; then'
            )
        ]
        self.assertIn("--mode provision", stale_mode)
        self.assertNotIn("prepare-browser-gate", stale_mode)

        usage_start = live_user.index("usage() {")
        usage_end = live_user.index("\n}", usage_start) + len("\n}")
        parser_start = live_user.index('case "$#" in')
        parser_end = live_user.index("\nesac", parser_start) + len("\nesac")
        argv_probe = "\n".join(
            (
                "set -euo pipefail",
                live_user[usage_start:usage_end],
                live_user[parser_start:parser_end],
                'printf "ARGV_VALIDATION_REACHED_NEXT_GUARD\\n"',
            )
        )
        argv_cases = (
            ((), True),
            (("recover-bundle",), True),
            (("recover", "/tmp/live-user-qa.fixture"), True),
            (("recover-setup", "/tmp/.live-user-qa.setup-fixture"), True),
            (("recover",), False),
            (("recover-setup",), False),
            (("recover", "relative/path"), False),
            (("recover-setup", "relative/path"), False),
            (("unknown",), False),
            (("recover-bundle", "/tmp/inventory.json"), False),
            (("recover", "/tmp/live-user-qa.fixture", "extra"), False),
        )
        for argv, accepted in argv_cases:
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [
                        "/usr/bin/bash",
                        "--noprofile",
                        "--norc",
                        "-c",
                        argv_probe,
                        "live-qa-argv-probe",
                        *argv,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={"LANG": "C", "PATH": "/usr/bin:/bin"},
                    timeout=5,
                    check=False,
                )
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(b"ARGV_VALIDATION_REACHED_NEXT_GUARD\n", result.stdout)
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn(b"ARGV_VALIDATION_REACHED_NEXT_GUARD", result.stdout)
                    self.assertIn(b"Usage:", result.stderr)

        recovery_start = live_user.index("recover_exact_state() {")
        recovery_end = live_user.index("\n}\n", recovery_start) + len("\n}\n")
        recovery_function = live_user[recovery_start:recovery_end]
        with tempfile.TemporaryDirectory(prefix="live-qa-recovery-retention-") as temporary:
            root = Path(temporary)
            stub = root / "guard-stub.sh"
            state = root / "live-user-qa.fixture"
            state.mkdir()
            (state / "inventory.json").write_text("synthetic inventory\n", encoding="ascii")
            removed_marker = root / "state-removed"
            stub.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  validate-recovery) printf 'liveqa-test-marker\\n' ;;\n"
                "  merge-browser-inventory|remove-browser-gate) exit 0 ;;\n"
                "  remove-root-state) : > \"$REMOVED_MARKER\" ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="ascii",
            )
            stub.chmod(0o700)
            shell_fixture = "\n".join(
                (
                    "set -Eeuo pipefail",
                    f"GUARD=({shlex.quote(str(stub))})",
                    f"PLATFORM_LIVE_CSP_QA_BUNDLE={shlex.quote(str(root / 'bundle.json'))}",
                    recovery_function,
                    "reconcile_and_cleanup() { return 31; }",
                    f"recover_exact_state {shlex.quote(str(state))}",
                )
            )
            result = subprocess.run(
                ["/usr/bin/bash", "--noprofile", "--norc", "-c", shell_fixture],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LANG": "C", "PATH": "/usr/bin:/bin", "REMOVED_MARKER": str(removed_marker)},
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 31)
            self.assertTrue(state.is_dir())
            self.assertTrue((state / "inventory.json").is_file())
            self.assertFalse(removed_marker.exists())

        def shell_function(source: str, name: str) -> str:
            start = source.index(f"{name}() {{")
            end = source.index("\n}\n", start) + len("\n}\n")
            return source[start:end]

        with tempfile.TemporaryDirectory(prefix="live-qa-db-argv-") as temporary:
            root = Path(temporary)
            recorder = root / "system-python-recorder"
            recorder.write_text(
                "#!/bin/sh\n"
                "n=0\n"
                "if [ -f \"$CALL_INDEX\" ]; then n=$(cat \"$CALL_INDEX\"); fi\n"
                "n=$((n + 1))\n"
                "printf '%s' \"$n\" > \"$CALL_INDEX\"\n"
                "printf '%s\\n' \"$@\" > \"$CALL_PREFIX.$n\"\n",
                encoding="ascii",
            )
            recorder.chmod(0o700)
            tools = Path("/payload/platform/tools")
            python = Path("/opt/oldsparky/platform/shared/venv/bin/python")
            pythonpath = Path("/root/.oldsparky/liveqa/releases") / ("a" * 40)
            inventory = root / "inventory.json"
            shell_fixture = "\n".join(
                (
                    "set -euo pipefail",
                    f"SYSTEM_PYTHON={shlex.quote(str(recorder))}",
                    f"TOOLS_DIR={shlex.quote(str(tools))}",
                    f"SAFE_PYTHONPATH={shlex.quote(str(pythonpath))}",
                    f"QA_PYTHON={shlex.quote(str(python))}",
                    shell_function(live_user, "safe_database_python"),
                    shell_function(live_user, "reconcile_and_cleanup"),
                    "reconcile_and_cleanup liveqa-stale-marker "
                    + shlex.quote(str(inventory)),
                )
            )
            prefix = root / "argv"
            result = subprocess.run(
                ["/usr/bin/bash", "--noprofile", "--norc", "-c", shell_fixture],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "LANG": "C",
                    "PATH": "/usr/bin:/bin",
                    "CALL_INDEX": str(root / "call-index"),
                    "CALL_PREFIX": str(prefix),
                },
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (root / "argv.1").read_text(encoding="ascii").splitlines(),
                [
                    "-I",
                    "-B",
                    str(tools / "platform_safe_env_exec.py"),
                    "exec",
                    "--pythonpath",
                    str(pythonpath),
                    "--",
                    str(python),
                    str(tools / "platform_recover_live_user_qa.py"),
                    "--marker",
                    "liveqa-stale-marker",
                    "--inventory",
                    str(inventory),
                    "--confirm",
                    "recover-live-user-qa",
                ],
            )
            self.assertEqual(
                (root / "argv.2").read_text(encoding="ascii").splitlines(),
                [
                    "-I",
                    "-B",
                    str(tools / "platform_safe_env_exec.py"),
                    "exec",
                    "--pythonpath",
                    str(pythonpath),
                    "--",
                    str(python),
                    str(tools / "platform_cleanup_live_user_qa.py"),
                    "--marker",
                    "liveqa-stale-marker",
                    "--inventory",
                    str(inventory),
                    "--confirm",
                    "cleanup-live-user-qa",
                ],
            )

        with tempfile.TemporaryDirectory(prefix="live-qa-stale-bundle-recovery-") as temporary:
            root = Path(temporary)
            stub = root / "guard-stub.sh"
            state = root / "live-user-qa.stale"
            state.mkdir()
            retained_message = f"Live QA recovery state retained at {state}"
            stub.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  preflight) exit \"${PRE_FAIL:-0}\" ;;\n"
                "  prepare-stale-bundle-cleanup-state) printf '%s\\n' \"$STATE_PATH\" ;;\n"
                "  validate-recovery) printf 'liveqa-stale-marker\\n' ;;\n"
                "  merge-browser-inventory|remove-browser-gate) exit 0 ;;\n"
                "  remove-root-state) : > \"$REMOVED_MARKER\" ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="ascii",
            )
            stub.chmod(0o700)
            shell_fixture = "\n".join(
                (
                    "set -Eeuo pipefail",
                    f"GUARD=({shlex.quote(str(stub))})",
                    "TRUSTED_MODE=1",
                    "PLATFORM_RELEASE_LOCK_SUPERVISED=1",
                    "PLATFORM_LIVE_QA_LOCK_FD=9",
                    "SOURCE_COMMIT=" + "'" + "a" * 40 + "'",
                    "PLATFORM_ROOT=/fixed/payload/platform",
                    f"PLATFORM_LIVE_CSP_QA_BUNDLE={shlex.quote(str(root / 'bundle.json'))}",
                    f"STATE_PATH={shlex.quote(str(state))}",
                    recovery_function,
                    "reconcile_and_cleanup() { return 31; }",
                    "set -- recover-bundle",
                    stale_mode,
                )
            )
            env = {
                "LANG": "C",
                "PATH": "/usr/bin:/bin",
                "STATE_PATH": str(state),
                "REMOVED_MARKER": str(root / "removed"),
            }
            failed_cleanup = subprocess.run(
                ["/usr/bin/bash", "--noprofile", "--norc", "-c", shell_fixture],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=5,
                check=False,
            )
            self.assertEqual(failed_cleanup.returncode, 31)
            self.assertIn(retained_message.encode("utf-8"), failed_cleanup.stderr)
            self.assertTrue(state.is_dir())
            self.assertFalse((root / "removed").exists())

            shutil.rmtree(state)
            refused_before_state = subprocess.run(
                ["/usr/bin/bash", "--noprofile", "--norc", "-c", shell_fixture],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**env, "PRE_FAIL": "22"},
                timeout=5,
                check=False,
            )
            self.assertEqual(refused_before_state.returncode, 22)
            self.assertNotIn(b"recovery state retained", refused_before_state.stderr)
            self.assertFalse(state.exists())
            self.assertFalse((root / "removed").exists())

    def test_browser_wrappers_use_the_fixed_nonroot_systemd_cgroup(self) -> None:
        for wrapper in BROWSER_WRAPPERS:
            with self.subTest(wrapper=wrapper.name):
                source = wrapper.read_text(encoding="utf-8")
                self.assertIn("/usr/bin/systemd-run", source)
                self.assertIn("--unit=oldsparky-liveqa-browser.service", source)
                self.assertIn('--uid="$LIVE_QA_UID"', source)
                self.assertIn('--gid="$LIVE_QA_GID"', source)
                self.assertIn("--property=KillMode=control-group", source)
                self.assertIn("--property=Restart=no", source)
                self.assertIn("--property=SendSIGKILL=yes", source)
                self.assertIn("CHROME_DEVEL_SANDBOX=", source)
                self.assertIn("/usr/bin/env -i", source)
                self.assertNotIn("/usr/bin/setpriv", source)
                self.assertNotIn("--no-sandbox", source)
                self.assertNotIn("--disable-setuid-sandbox", source)
        live_user = (TOOLS_ROOT / "platform_live_user_qa.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('LIVE_QA_RUNNER_SHA="${PLATFORM_LIVE_QA_RUNNER_SHA:-$SOURCE_COMMIT}"', live_user)
        self.assertIn('printf \'%s\' "$MARKER" | /usr/bin/sha256sum', live_user)
        self.assertIn('PLATFORM_LIVE_QA_TARGET_SHA="$SOURCE_COMMIT"', live_user)
        self.assertIn('PLATFORM_LIVE_QA_RUNNER_SHA="$LIVE_QA_RUNNER_SHA"', live_user)
        self.assertIn('PLATFORM_LIVE_QA_MARKER_SHA256="$LIVE_QA_MARKER_SHA256"', live_user)

    def test_public_browser_cleanup_reclaims_runner_ownership_before_removal(
        self,
    ) -> None:
        source = (TOOLS_ROOT / "platform_live_browser_qa.sh").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(source.count("remove-public-browser-gate"), 2)
        self.assertNotIn("remove-browser-gate", source)

    def test_secret_bearing_journey_refuses_auth_pages_and_turnstile(self) -> None:
        source = LIVE_USER_JOURNEY.read_text(encoding="utf-8")
        self.assertIn('parsed.hostname === "challenges.cloudflare.com"', source)
        self.assertIn(
            '["/auth/login", "/auth/register", "/reset-password", "/verify-email"]',
            source,
        )
        self.assertIn("forbidden-auth-automation", source)

    def test_sandbox_assertion_uses_the_dedicated_systemd_cgroup(self) -> None:
        source = SANDBOX_ASSERTION.read_text(encoding="utf-8")
        self.assertIn(
            'const LIVE_QA_CGROUP = "/system.slice/oldsparky-liveqa-browser.service"',
            source,
        )
        self.assertIn("/proc/self/cgroup", source)
        self.assertIn("/cgroup.procs", source)
        self.assertIn('statusNumbers(status, "NSpid")', source)
        self.assertIn('statusNumber(status, "NoNewPrivs") === 1', source)
        self.assertIn('statusNumber(status, "Seccomp") === 2', source)
        self.assertIn('statusName(status).startsWith("chrome")', source)
        self.assertNotIn("isDescendantOf", source)

    def test_installer_checks_service_identity_collisions_and_rolls_back_partial_work(
        self,
    ) -> None:
        source = WRAPPERS[0].read_text(encoding="utf-8")
        for identity in (
            "oldsparky",
            "oldsparky-platform",
            "oldsparky-api",
            "oldsparky-web",
            "oldsparky-worker",
        ):
            self.assertIn(identity, source)
        self.assertIn("passwd_matches", source)
        self.assertIn("group_matches", source)
        self.assertIn("supplementary", source)
        self.assertIn("rollback_partial_identity", source)
        self.assertIn("--no-user-group", source)
        self.assertIn("/usr/bin/getent", source)
        self.assertIn(
            'if production_name in {"oldsparky-platform"}',
            source,
        )
        self.assertNotIn(
            'if production_name in {"oldsparky", "oldsparky-platform"}',
            source,
        )

    def test_installer_owns_a_narrow_revision_pinned_apparmor_profile(self) -> None:
        installer = WRAPPERS[0].read_text(encoding="utf-8")
        profile = APPARMOR_PROFILE.read_text(encoding="utf-8")
        self.assertIn("apparmor_parser -Q -T", installer)
        self.assertIn("apparmor_parser -r -T", installer)
        self.assertIn("/etc/apparmor.d/$APPARMOR_PROFILE_NAME", installer)
        self.assertIn(
            "/var/lib/oldsparky-liveqa/runtime-*/browsers/"
            "chromium-1228/chrome-linux64/chrome",
            profile,
        )
        self.assertIn(
            "/var/lib/oldsparky-liveqa/runtime-*/browsers/"
            "chromium_headless_shell-1228/chrome-headless-shell-linux64/"
            "chrome-headless-shell",
            profile,
        )
        self.assertEqual(profile.count("userns,"), 2)
        self.assertNotIn("network,", profile)
        self.assertNotIn("capability,", profile)
        self.assertNotIn("--no-sandbox", profile)

    @unittest.skipUnless(
        os.geteuid() == 0 and Path("/usr/bin/setpriv").is_file(),
        "root is needed to exercise the nonroot refusal boundary",
    )
    def test_every_wrapper_refuses_nonroot_before_privileged_work(self) -> None:
        nobody = pwd.getpwnam("nobody")
        arguments = {
            "platform_install_live_qa_user.sh": [],
            "platform_live_browser_qa.sh": ["public"],
            "platform_live_user_qa.sh": [],
            "platform_provision_live_csp_qa.sh": [],
            "platform_manual_live_auth_qa.sh": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            os.chmod(temporary_root, 0o755)
            for wrapper in WRAPPERS:
                copied = temporary_root / wrapper.name
                shutil.copyfile(wrapper, copied)
                os.chmod(copied, 0o755)
                command = [
                    "/usr/bin/setpriv",
                    f"--reuid={nobody.pw_uid}",
                    f"--regid={nobody.pw_gid}",
                    "--clear-groups",
                    "/usr/bin/bash",
                    str(copied),
                    *arguments[wrapper.name],
                ]
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={"LANG": "C", "PATH": "/usr/bin:/bin"},
                    check=False,
                    timeout=10,
                )
                with self.subTest(wrapper=wrapper.name):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"root", result.stderr.lower())


    def test_retained_abort_resolves_runner_sha_from_exact_external_attempt(self) -> None:
        workflow_text = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort.yml"
        ).read_text(encoding="utf-8")
        step = workflow_text.split(
            "      - name: Resolve the selected external-load runner SHA from its exact attempt\n",
            1,
        )[1].split("      - name: Pin the exact helper bytes\n", 1)[0]
        run_block = step.split("        run: |\n", 1)[1]
        script = textwrap.dedent(
            run_block.split("<<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
        )
        expected_path = ".github/workflows/platform-production-external-load.yml"
        workflow = {"id": 71, "path": expected_path}
        selected = {
            "id": 123456,
            "run_attempt": 2,
            "workflow_id": 71,
            "path": expected_path,
            "head_branch": "dev",
            "event": "workflow_dispatch",
            "head_sha": "a" * 40,
            "repository": {"full_name": "StrayForest/old_sparky"},
        }

        def run_case(mutation: dict[str, object] | None = None,
                     workflow_mutation: dict[str, object] | None = None) -> tuple[subprocess.CompletedProcess[str], str]:
            with tempfile.TemporaryDirectory(prefix="retained-abort-selected-load-") as temporary:
                root = Path(temporary)
                workflow_path = root / "workflow.json"
                attempt_path = root / "attempt.json"
                output_path = root / "github-output"
                workflow_path.write_text(
                    json.dumps({**workflow, **(workflow_mutation or {})}), encoding="utf-8"
                )
                attempt_path.write_text(
                    json.dumps({**selected, **(mutation or {})}), encoding="utf-8"
                )
                output_path.write_text("", encoding="ascii")
                completed = subprocess.run(
                    [sys.executable, "-", str(workflow_path), str(attempt_path),
                     "123456", "2", str(output_path)],
                    input=script,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                return completed, output_path.read_text(encoding="ascii")

        accepted, output = run_case()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(output, f"runner_sha={'a' * 40}\n")
        for mutation, workflow_mutation in (
            ({"id": 123457}, None),
            ({"run_attempt": 1}, None),
            ({"workflow_id": 72}, None),
            ({"path": ".github/workflows/other.yml"}, None),
            ({"head_branch": "main"}, None),
            ({"event": "push"}, None),
            ({"repository": {"full_name": "Other/repo"}}, None),
            ({"head_sha": "A" * 40}, None),
            (None, {"path": ".github/workflows/other.yml"}),
        ):
            with self.subTest(mutation=mutation, workflow_mutation=workflow_mutation):
                self.assertNotEqual(run_case(mutation, workflow_mutation)[0].returncode, 0)

    def test_retained_abort_normalizer_accepts_only_bound_cpu_abort_proof(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort.yml"
        ).read_text(encoding="utf-8")
        marker = '/usr/bin/python3 - "$artifact_dir/canonical.log" "$artifact_dir/abort-only.json"'
        start = workflow.index(marker)
        heredoc = workflow.index("<<'PY'\n", start) + len("<<'PY'\n")
        end = workflow.index("\n          PY", heredoc)
        script = textwrap.dedent(workflow[heredoc:end])
        run_id, attempt, target_sha = "39178530966", "1", "a" * 40
        runner_sha = target_sha
        proof = {
            "schema": 1,
            "status": "safe_for_exact_fixture_cleanup",
            "fixture_kind": "cpu_diagnostic_only_v1",
            "external_run_id": run_id,
            "external_run_attempt": attempt,
            "source_sha": target_sha,
            "app_target_sha": target_sha,
            "input_handoff_sha256": "b" * 64,
            "fixture_manifest_sha256": "c" * 64,
            "remote_stage": "supervisor_terminated",
            "supervisor_selector": "fixed_script_paths_run_root_and_run_id",
            "supervisor_roots_before": 1,
            "supervisor_tree_before": 7,
            "supervisor_roots_after": 0,
            "supervisor_descendants_after": 0,
            "proc_read_errors": 0,
            "supervisor_exit_code": 0,
            "shared_lock_clear": True,
            "namespace_closed": False,
            "recovery_workflow_sha": "d" * 40,
            "recovery_helper_sha256": "e" * 64,
        }

        def execute(
            canonical: object,
            abort_proof: object | None,
            *,
            selected_attempt: str = attempt,
            selected_runner_sha: str = runner_sha,
            remote_step: str = "success",
            remote_status: str = "0",
            ssh_cleanup: str = "success",
            stage_cleanup: str = "success",
        ):
            with tempfile.TemporaryDirectory() as directory:
                canonical_path = Path(directory) / "canonical.log"
                proof_path = Path(directory) / "abort-only.json"
                canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
                if abort_proof is not None:
                    if isinstance(abort_proof, bytes):
                        proof_path.write_bytes(abort_proof)
                    else:
                        proof_path.write_bytes(
                            json.dumps(
                                abort_proof, ensure_ascii=True, sort_keys=True,
                                separators=(",", ":"),
                            ).encode("ascii") + b"\n"
                        )
                return subprocess.run(
                    [sys.executable, "-", str(canonical_path), str(proof_path), run_id,
                     selected_attempt, target_sha, selected_runner_sha],
                    input=script,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        **os.environ,
                        "ABORT_REMOTE_STEP": remote_step,
                        "ABORT_REMOTE_STATUS": remote_status,
                        "ABORT_STAGE_CLEANUP_STEP": stage_cleanup,
                        "ABORT_SSH_CLEANUP_STEP": ssh_cleanup,
                        "RECOVERY_WORKFLOW_SHA": "d" * 40,
                        "RECOVERY_HELPER_SHA256": "e" * 64,
                    },
                    check=False,
                    timeout=5,
                )

        unavailable = {"schema": 1, "status": "unavailable", "truncated": False}
        passed = {"schema": 1, "status": "passed", "truncated": False}
        for fixture_kind in (
            "cpu_diagnostic_only_v1",
            "ssr_capacity_diagnostic_v1",
            "ssr_html_capacity_diagnostic_v1",
            "ssr_html_capacity_composite_diagnostic_v1",
        ):
            with self.subTest(fixture_kind=fixture_kind):
                self.assertEqual(
                    execute(unavailable, {**proof, "fixture_kind": fixture_kind}).returncode,
                    0,
                )
        self.assertNotEqual(
            execute(unavailable, {**proof, "fixture_kind": "external_load"}).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, {**proof, "fixture_kind": []}).returncode,
            0,
        )
        self.assertEqual(execute(passed, {"schema": 1, "status": "not_applicable"}).returncode, 0)
        self.assertNotEqual(execute(unavailable, None).returncode, 0)
        self.assertNotEqual(execute(unavailable, proof, selected_attempt="2").returncode, 0)
        split_runner_sha = "f" * 40
        split_proof = {**proof, "source_sha": split_runner_sha}
        self.assertEqual(
            execute(unavailable, split_proof, selected_runner_sha=split_runner_sha).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, split_proof, selected_runner_sha="e" * 40).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, {**split_proof, "app_target_sha": "c" * 40},
                    selected_runner_sha=split_runner_sha).returncode,
            0,
        )
        self.assertNotEqual(execute(unavailable, proof, remote_status="1").returncode, 0)
        self.assertNotEqual(execute(unavailable, proof, ssh_cleanup="failure").returncode, 0)
        self.assertNotEqual(execute(unavailable, proof, stage_cleanup="failure").returncode, 0)
        already_absent = {
            **proof,
            "remote_stage": "supervisor_already_absent",
            "supervisor_roots_before": 0,
            "supervisor_tree_before": 0,
        }
        self.assertEqual(execute(unavailable, already_absent).returncode, 0)
        composite_already_absent = {
            **already_absent,
            "fixture_kind": "ssr_html_capacity_composite_diagnostic_v1",
        }
        self.assertEqual(
            execute(unavailable, composite_already_absent).returncode, 0
        )
        self.assertNotEqual(
            execute(unavailable, {**already_absent, "supervisor_tree_before": 1}).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, {**proof, "recovery_workflow_sha": "f" * 40}).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, {**proof, "recovery_helper_sha256": "f" * 64}).returncode,
            0,
        )
        self.assertNotEqual(
            execute(unavailable, {**proof, "namespace_closed": True}).returncode, 0
        )
        self.assertNotEqual(execute(unavailable, 7).returncode, 0)
        malformed = {**proof, "supervisor_exit_code": True}
        self.assertNotEqual(execute(unavailable, malformed).returncode, 0)
        for field in (
            "supervisor_roots_after", "supervisor_descendants_after", "proc_read_errors"
        ):
            malformed_zero = {**proof, field: False}
            with self.subTest(field=field):
                self.assertNotEqual(execute(unavailable, malformed_zero).returncode, 0)
        duplicate_key_proof = (
            b'{"schema":1,"status":"safe_for_exact_fixture_cleanup",'
            b'"status":"safe_for_exact_fixture_cleanup"}\n'
        )
        self.assertNotEqual(execute(unavailable, duplicate_key_proof).returncode, 0)
        oversized_proof = b" " * 4097
        self.assertNotEqual(execute(unavailable, oversized_proof).returncode, 0)


if __name__ == "__main__":
    unittest.main()
