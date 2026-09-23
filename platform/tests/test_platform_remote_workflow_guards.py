"""Static and adversarial contracts for production SSH workflow boundaries."""

from __future__ import annotations

import json
import re
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import textwrap
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"
sys.path.insert(0, str(PLATFORM_ROOT))

from tools.platform_workflow_input_guard import (  # noqa: E402
    WorkflowInputError,
    _write_private_json,
    validate_control_email,
    validate_target_sha,
    validate_utc_timestamp,
)
from tools import (  # noqa: E402
    platform_media_migration_diagnostics_summary,
    platform_prepare_artifact_dir,
)


WORKFLOWS = {
    "as12": "platform-production-as12-proof.yml",
    "runtime": "platform-production-web-runtime-diagnostics.yml",
    "profile": "platform-production-profile-review-fixture.yml",
    "live": "platform-live-user-qa.yml",
    "translation": "platform-patch-translation-qa.yml",
}


def _job_block(source: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow job is missing: {name}")
    return match.group("body")


def _media_report_payload() -> dict[str, object]:
    return {
        "ok": True,
        "mode": "check",
        "mutated": False,
        "inventory_before": {
            "legacy_upload_references": 2,
            "packaged_asset_references": 3,
            "manual_conflicts": 0,
        },
        "inventory_after": {
            "legacy_upload_references": 0,
            "packaged_asset_references": 3,
            "manual_conflicts": 0,
        },
        "source_locations": {"r2": 1},
        "operations": {"r2_gets": 4},
    }


def _media_projection_script() -> str:
    """Extract the runner-side scalar projection for deterministic tests."""

    workflow = (
        WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
    ).read_text(encoding="utf-8")
    start = workflow.index("      - name: Inspect production legacy media sources")
    end = workflow.index("      - name: Remove private media diagnostic capture", start)
    block = workflow[start:end]
    marker = (
        "/usr/bin/python3 - \"$public_line\" \"$precondition_line\" "
        '\"$remote_status\" \"$remote_stderr_bytes\" '
        '\"$TARGET_SHA\" \"$deployed_sha\" <<\'PY\'\n'
    )
    script = block.split(marker, 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(script)


def _run_media_projection(
    public_line: str,
    *,
    remote_status: str = "0",
    remote_stderr_bytes: str = "0",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-",
            public_line,
            "",
            remote_status,
            remote_stderr_bytes,
            "a" * 40,
            "a" * 40,
        ],
        input=_media_projection_script(),
        text=True,
        capture_output=True,
        check=False,
    )


def _media_summary_script() -> str:
    """Extract the final safe step-summary renderer for adversarial tests."""

    workflow = (
        WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
    ).read_text(encoding="utf-8")
    block = workflow[workflow.index("      - name: Write media inventory summary") :]
    marker = (
        "/usr/bin/python3 - \"$MEDIA_SUMMARY\" \"$TARGET_SHA\" "
        ">> \"$GITHUB_STEP_SUMMARY\" <<'PY'\n"
    )
    script = block.split(marker, 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(script)


class RemoteWorkflowGuardContractTests(unittest.TestCase):
    def _source(self, key: str) -> str:
        return (WORKFLOW_ROOT / WORKFLOWS[key]).read_text(encoding="utf-8")

    def test_secret_jobs_are_fresh_no_checkout_jobs_with_data_only_handoffs(self) -> None:
        secret_jobs = {
            "as12": "proof",
            "runtime": "collect",
            "profile": "create-fixture",
            "live": "live-user-qa",
            "translation": "regression",
        }
        for key, job_name in secret_jobs.items():
            with self.subTest(workflow=key):
                job = _job_block(self._source(key), job_name)
                self.assertIn("environment: production", job)
                self.assertNotIn("actions/checkout@", job)
                self.assertIn("actions/download-artifact@", job)
                self.assertIn("closed", job.lower())
                self.assertNotIn("platform_live_launch_report.py", job)
                self.assertNotIn("platform/tools/platform_workflow_input_guard.py", job)

        for key, validator_job in {
            "as12": "validate-proof-inputs",
            "runtime": "validate-runtime-inputs",
            "profile": "validate-fixture-inputs",
            "live": "validate-live-user-inputs",
            "translation": "validate-translation-inputs",
        }.items():
            with self.subTest(validator=key):
                validator = _job_block(self._source(key), validator_job)
                self.assertIn("actions/checkout@", validator)
                self.assertNotIn("secrets.PROD_SSH_", validator)
                self.assertIn("persist-credentials: false", validator)

    def test_handoffs_have_strict_schema_and_no_shell_newline_surfaces(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                self.assertIn('"schema": 1', source)
                self.assertIn("set(payload)", source)
                self.assertIn("test ! -L", source)
                self.assertIn("install -m 600", source)
                self.assertRegex(source, r"fullmatch\(r?['\"]\[0-9a-f\]\{40\}")
                if key == "runtime":
                    self.assertIn("utc-timestamp", source)
                    self.assertIn("timestamp_re", source)
                else:
                    self.assertIn("handoff schema is invalid", source)

    def test_installed_sha_and_runtime_window_are_checked_before_lock(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    # Live-user QA enters through the fixed root-owned helper.
                    # That helper verifies the installed generation and its
                    # manifest before acquiring the canonical release lock;
                    # the generic current-release dispatcher is intentionally
                    # not the secret-bearing entrypoint for this contour.
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
                        source,
                    )
                    trusted_helper = (
                        PLATFORM_ROOT / "tools/platform_live_user_qa_trusted.sh"
                    ).read_text(encoding="utf-8")
                    verify_position = trusted_helper.index(
                        '/usr/bin/python3.12 -I "$DISPATCHER" verify "$TARGET_SHA"'
                    )
                    lock_position = trusted_helper.index(
                        'exec "$RELEASE_LOCK_EXEC" --expected-sha "$TARGET_SHA"'
                    )
                    self.assertLess(verify_position, lock_position)
                    continue
                guard_positions = [
                    position
                    for position in range(len(source))
                    if source.startswith('/bin/bash "$guard"', position)
                ]
                self.assertTrue(guard_positions)
                first_guard = guard_positions[0]
                self.assertLess(source.index('test -f "$guard"'), first_guard)
                self.assertLess(source.index('test ! -L "$guard"'), first_guard)
                sha_check = re.search(
                    r'test "\$active_sha" = "\$(?:expected_sha|target_sha)"',
                    source,
                )
                self.assertIsNotNone(sha_check)
                self.assertLess(sha_check.start(), first_guard)
                self.assertIn('"source_git_commit"', source)
        runtime = self._source("runtime")
        self.assertLess(runtime.index("utc-timestamp"), runtime.index('/bin/bash "$guard"'))
        self.assertLess(runtime.index("diagnostic window is invalid"), runtime.index('/bin/bash "$guard"'))

    def test_every_remote_helper_path_is_regular_and_not_a_symlink(self) -> None:
        helper_paths = {
            "as12": ("$input_guard", "$edge_policy_tool"),
            "runtime": ("$input_guard", "$summary_tool", "$nginx_summary_tool", "$nginx_error_log"),
            "profile": ("$input_guard", "$runtime_common", "$seed_tool"),
            "live": ("$input_guard", "$qa_script", "$reviewed_helper", "$installed_helper"),
            "translation": ("$input_guard", "$runtime_common", "$translation_summary"),
        }
        for key, paths in helper_paths.items():
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
                        source,
                    )
                    self.assertNotIn("/opt/oldsparky/platform/current/tools/", source)
                    continue
                for path in paths:
                    self.assertIn(f'test -f "{path}"', source, path)
                    self.assertIn(f'test ! -L "{path}"', source, path)
        for key in WORKFLOWS:
            source = self._source(key)
            if key == "live":
                continue
            self.assertRegex(source, r'test -f "\$guard" && test ! -L "\$guard" && test -x "\$guard"')

    def test_adversarial_values_and_symlink_handoffs_fail_closed(self) -> None:
        for value in (
            "a" * 39,
            "A" * 40,
            "a" * 40 + "\n",
            "a" * 20 + "\n" + "a" * 19,
            "$(id)",
            "a" * 39 + "\x00",
        ):
            with self.subTest(sha=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_target_sha(value)
        for value in (
            "2026-09-09T09:33:00Z\n",
            "2026-09-09T09:33:00+00:00",
            "2026-09-09T09:33:00Z$(id)",
            "2026-09-09T09:33:00Z\x00",
        ):
            with self.subTest(timestamp=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_utc_timestamp(value)
        for value in ("operator@example.com\n", "operator@example.com$(id)", "é@example.com"):
            with self.subTest(email=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_control_email(value)

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            real = root / "real.json"
            real.write_text("{}\n", encoding="utf-8")
            symlink = root / "handoff.json"
            symlink.symlink_to(real)
            with self.assertRaises(WorkflowInputError):
                _write_private_json(symlink, {"schema": "1"})

    def test_missing_helper_contracts_fail_before_remote_side_effects(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    self.assertIn("/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh", source)
                    self.assertNotIn("bash -s", source)
                    continue
                self.assertRegex(
                    source,
                    r"missing(?: or unsafe|)\"?",
                )
                self.assertNotIn("test -x \"$guard\" ||", source)
                self.assertIn("test -f \"$guard\" && test ! -L \"$guard\"", source)

    def test_remote_artifact_directory_uses_atomic_nofollow_helper(self) -> None:
        source = (PLATFORM_ROOT / "tools/platform_workflow_remote_dispatch.py").read_text(
            encoding="utf-8"
        )
        helper = (PLATFORM_ROOT / "tools/platform_prepare_artifact_dir.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ARTIFACT_DIR_HELPER", source)
        self.assertNotIn('"/usr/bin/install"', source)
        self.assertIn("os.mkdir(leaf, 0o700, dir_fd=parent_fd)", helper)
        self.assertIn("os.O_NOFOLLOW", helper)
        self.assertIn("os.fstat(child_fd)", helper)

        run_id = str(os.getpid())
        path = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
        if path.exists() or path.is_symlink():
            self.skipTest("test artifact directory name is already occupied")
        try:
            platform_prepare_artifact_dir.prepare(str(path))
            metadata = path.lstat()
            self.assertTrue(metadata.st_mode & 0o700 == 0o700)
            self.assertFalse(path.is_symlink())
        finally:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()

        target = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-2")
        if target.exists() or target.is_symlink():
            self.skipTest("test symlink artifact directory name is already occupied")
        target.mkdir()
        link = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-3")
        try:
            link.symlink_to(target)
            with self.assertRaises((OSError, RuntimeError)):
                platform_prepare_artifact_dir.prepare(str(link))
            self.assertTrue(link.is_symlink())
        finally:
            if link.is_symlink():
                link.unlink()
            if target.is_dir() and not target.is_symlink():
                target.rmdir()

    def test_media_remote_exit_and_stderr_bytes_are_actual_values(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=_media_report_payload(),
            producer_exit_code=1,
            stderr_bytes=17,
            remote_exit_code=1,
            remote_stderr_bytes=23,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["producer_exit_code"], 1)
        self.assertEqual(report["stderr_bytes"], 17)
        self.assertEqual(report["remote_exit_code"], 1)
        self.assertEqual(report["remote_stderr_bytes"], 23)
        self.assertFalse(report["mutated"])
        self.assertEqual(report["inventory_before_legacy_upload_references"], 2)
        self.assertNotIn('"remote_exit_code":255', json.dumps(report))

    def test_media_transport_exit_255_is_distinct_from_precondition(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=None,
            producer_exit_code=None,
            stderr_bytes=None,
            remote_exit_code=255,
            remote_stderr_bytes=31,
            precondition_error_class="remote_precondition",
            parse_ok=False,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_class"], "remote_or_transport")
        self.assertIsNone(report["producer_exit_code"])
        self.assertIsNone(report["stderr_bytes"])
        self.assertEqual(report["remote_exit_code"], 255)
        self.assertEqual(report["remote_stderr_bytes"], 31)

    def test_media_remote_precondition_exit_is_explicit(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=None,
            producer_exit_code=None,
            stderr_bytes=None,
            remote_exit_code=1,
            remote_stderr_bytes=7,
            precondition_error_class="remote_precondition",
            parse_ok=False,
        )

        self.assertEqual(report["error_class"], "remote_precondition")
        self.assertEqual(report["remote_exit_code"], 1)
        self.assertEqual(report["remote_stderr_bytes"], 7)
        self.assertIsNone(report["producer_exit_code"])
        self.assertIsNone(report["stderr_bytes"])

    def test_media_projection_rejects_malformed_old_schema_and_boundary_values(self) -> None:
        valid_report = platform_media_migration_diagnostics_summary.public_summary(
            payload=_media_report_payload(),
            producer_exit_code=0,
            stderr_bytes=0,
            remote_exit_code=0,
            remote_stderr_bytes=0,
        )
        valid_line = "MEDIA_INVENTORY " + json.dumps(
            valid_report,
            separators=(",", ":"),
        )
        accepted = _run_media_projection(valid_line)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(len(accepted.stdout.splitlines()), 1)

        old_schema = dict(valid_report)
        old_schema.pop("remote_exit_code")
        old_schema.pop("remote_stderr_bytes")
        cases: dict[str, str] = {
            "old-schema": "MEDIA_INVENTORY " + json.dumps(old_schema),
            "malformed": 'MEDIA_INVENTORY {"secret":"must-not-leak"',
        }
        for field, value in (
            ("schema", True),
            ("producer_exit_code", False),
            ("stderr_bytes", None),
            ("remote_exit_code", 256),
            ("remote_stderr_bytes", -1),
            ("processed_count", 1_000_001),
            ("mutated", 1),
            ("status", "unknown"),
        ):
            invalid = dict(valid_report)
            invalid[field] = value
            cases[f"invalid-{field}"] = "MEDIA_INVENTORY " + json.dumps(
                invalid,
                separators=(",", ":"),
            )

        for name, public_line in cases.items():
            with self.subTest(case=name):
                rejected = _run_media_projection(public_line)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("must-not-leak", rejected.stdout)
                self.assertEqual(len(rejected.stdout.splitlines()), 1)
                safe_report = json.loads(rejected.stdout.split(" ", 1)[1])
                self.assertEqual(safe_report["status"], "failed")
                self.assertEqual(safe_report["remote_exit_code"], 0)
                self.assertEqual(safe_report["remote_stderr_bytes"], 0)

        for status, stderr_bytes in (
            ("256", "0"),
            ("-1", "0"),
            ("0", "1000001"),
            ("0", "-1"),
        ):
            with self.subTest(remote_status=status, remote_stderr=stderr_bytes):
                rejected = _run_media_projection(
                    valid_line,
                    remote_status=status,
                    remote_stderr_bytes=stderr_bytes,
                )
                self.assertNotEqual(rejected.returncode, 0)
                safe_report = json.loads(rejected.stdout.split(" ", 1)[1])
                self.assertEqual(safe_report["status"], "failed")
                self.assertEqual(safe_report["error_class"], "internal")
                self.assertIsNone(safe_report["remote_exit_code"])
                self.assertIsNone(safe_report["remote_stderr_bytes"])

        transport = _run_media_projection(
            valid_line,
            remote_status="255",
            remote_stderr_bytes="31",
        )
        self.assertNotEqual(transport.returncode, 0)
        transport_report = json.loads(transport.stdout.split(" ", 1)[1])
        self.assertEqual(transport_report["status"], "failed")
        self.assertEqual(transport_report["error_class"], "remote_or_transport")
        self.assertEqual(transport_report["remote_exit_code"], 255)
        self.assertEqual(transport_report["remote_stderr_bytes"], 31)

    def test_media_workflow_is_manual_sha_locked_and_safe(self) -> None:
        workflow = (
            WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  workflow_dispatch:\n    inputs:\n      expected_sha:", workflow)
        self.assertIn("        required: true", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertNotIn("statuses: write", workflow)
        self.assertNotIn("platform-media-inventory", workflow)
        self.assertNotIn("/statuses/", workflow)
        self.assertNotIn("Mark media inventory", workflow)
        self.assertNotIn("GH_TOKEN", workflow)
        self.assertNotIn("curl --fail-with-body", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("Write media inventory summary", workflow)
        self.assertIn("MEDIA_SUMMARY", workflow)
        self.assertIn("Expected SHA:", workflow)
        self.assertIn("Deployed SHA:", workflow)
        self.assertIn("Aggregate counter", workflow)
        self.assertIn("group: platform-production-deploy", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn('test -n "${PROD_SSH_USER:-}"', workflow)
        self.assertIn(
            '[[ "$PROD_SSH_USER" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,31}$ ]]',
            workflow,
        )
        self.assertNotIn('echo "$PROD_SSH_USER"', workflow)
        self.assertNotIn('printf \'%s\\n\' "$PROD_SSH_USER"', workflow)

        sha_read = workflow.index(
            'active_sha="$(/usr/bin/python3.12 -I - "$release_json"'
        )
        sha_compare = workflow.index(
            'test "$active_sha" = "$expected_sha" || precondition'
        )
        helper_check = workflow.index('test -f "$lock_guard"')
        lock_call = workflow.index('/bin/bash "$lock_guard"')
        producer_call = workflow.index('producer_exit="$?"')
        self.assertLess(sha_read, sha_compare)
        self.assertLess(sha_compare, helper_check)
        self.assertLess(helper_check, lock_call)
        self.assertLess(lock_call, producer_call)
        self.assertIn('"source_git_commit"', workflow)
        self.assertIn("remote_precondition", workflow)
        self.assertIn(
            'test -f "$producer_tool" && test ! -L "$producer_tool" && test -x "$producer_tool" || precondition',
            workflow,
        )
        self.assertIn(
            'test -f "$summary_tool" && test ! -L "$summary_tool" || precondition',
            workflow,
        )
        self.assertIn('remote_status="$?"', workflow)
        self.assertIn('remote_stderr_bytes="$(wc -c <"$remote_error"', workflow)
        self.assertIn('"remote_exit_code"', workflow)
        self.assertIn('"remote_stderr_bytes"', workflow)
        self.assertIn(
            'rm -f -- "$private_report" "$private_error" "$projector_error"',
            workflow,
        )
        self.assertIn('rm -f -- "$public_report"', workflow)
        self.assertIn('if: ${{ always() }}', workflow)
        self.assertIn('"mutated"', workflow)
        self.assertIn('"inventory_before_legacy_upload_references"', workflow)
        self.assertNotIn('"producer_exit_code":255', workflow)
        self.assertNotIn('"producer_exit_code":0,"stderr_bytes":0', workflow)

        hostile_summary = json.dumps(
            {
                "expected_sha": "a" * 40,
                "deployed_sha": "b" * 40,
                "status": "passed",
                "error_class": "none",
                "producer_exit_code": None,
                "stderr_bytes": None,
                "remote_exit_code": None,
                "remote_stderr_bytes": None,
                "failed_count": 10**40,
                "raw_stderr": "secret-stderr",
                "ssh_user": "operator",
                "ssh_host": "production.example.test",
            },
            separators=(",", ":"),
        )
        rendered = subprocess.run(
            [sys.executable, "-", hostile_summary, "a" * 40],
            input=_media_summary_script(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("Status: **failed**", rendered.stdout)
        self.assertIn("Error class: `internal`", rendered.stdout)
        self.assertNotIn("secret-stderr", rendered.stdout)
        self.assertNotIn("operator", rendered.stdout)
        self.assertNotIn("production.example.test", rendered.stdout)


if __name__ == "__main__":
    unittest.main()
