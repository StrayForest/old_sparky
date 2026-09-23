"""Static and adversarial contracts for production SSH workflow boundaries."""

from __future__ import annotations

import re
import os
from pathlib import Path
import sys
import tempfile
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
from tools import platform_prepare_artifact_dir  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
