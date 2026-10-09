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
import subprocess
import sys
import tempfile
import textwrap
import time
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
                    patch.dict(os.environ, {"PLATFORM_RELEASE_LOCK_SUPERVISED": "1"}, clear=True), \
                    redirect_stdout(output):
                    self.assertEqual(platform_live_user_qa_dispatch.main(arguments), 1)
                lock_check.assert_called_once_with()
                tuple_check.assert_not_called()
                if arguments[0] == "run-launch":
                    self.assertEqual(
                        output.getvalue(),
                        "LIVE_LAUNCH_STATUS schema=2 status=failed "
                        f"stage=dispatch check=release_lock child_exit=1 source_sha={sha}\n",
                    )
                else:
                    self.assertEqual(output.getvalue(), "")

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
                "target_sha": "a" * 40,
                "control_email": "control@example.invalid",
                "setup_concurrency": "8",
                "run_id": "123456",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "14",
                "timeout_diagnostics": "false",
            }
            with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", root), \
                patch.object(platform_workflow_remote_dispatch, "EXTERNAL_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch, "SUDO", "/usr/bin/sudo"), \
                patch.object(
                    platform_workflow_remote_dispatch.subprocess,
                    "Popen",
                    return_value=child,
                ) as popen:
                self.assertEqual(
                    platform_workflow_remote_dispatch._external_fixture(payload), 0
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
                    platform_workflow_remote_dispatch._external_fixture(payload), 2
                )
            terminate.assert_called_once_with(failed_child)
            self.assertTrue(failed_child.stdin.closed)
            failed_command = failed_popen.call_args.args[0]
            self.assertNotIn(payload["control_email"], failed_command)

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
        supervisor = (
            TOOLS_ROOT / "platform_live_launch_supervisor.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("PROD_SSH_HOST", source)
        self.assertIn("ssh " + "\\", source)
        self.assertIn(
            "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
            source,
        )
        self.assertIn("live-launch < \"$input_path\"", source)
        self.assertIn("platform_live_browser_qa.sh\" public", supervisor)
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
                    if workflow_source.startswith(f"{mode} <", position)
                ]
                self.assertGreaterEqual(len(mode_positions), 1, mode)
                mode_position = mode_positions[-1]
                ssh_position = workflow_source.rfind("ssh \\\n", 0, mode_position)
                if ssh_position < 0:
                    ssh_position = workflow_source.rfind("ssh ", 0, mode_position)
                self.assertGreaterEqual(ssh_position, 0)
                command = workflow_source[ssh_position : mode_position + len(mode) + 1]
                self.assertIn(expected_dispatcher, command)
                self.assertNotRegex(
                    command,
                    r"\$(?:CONTROL_EMAIL|LIVE_BASE_URL|LIVE_PROVISION|"
                    r"LIVE_MARKER|live_marker|marker|DEPLOY_MODE|RUNTIME_PROFILE|"
                    r"RELEASE_SLUG|ARTIFACT_REMOTE_DIR|release_slug)",
                )

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
                platform_workflow_remote_dispatch.main(["external-fixture"]),
                2,
            )
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(stderr.getvalue(), "remote workflow input is invalid\n")
        self.assertNotIn(invalid_emails[0], stderr.getvalue())

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
        self.assertEqual(passed_report["test_count"], 2)
        self.assertEqual(passed_report["logical_counts"]["logical_expected_fail"], 1)
        self.assertEqual(passed_report["attempt_counts"]["attempt_fail"], 1)
        self.assertEqual(passed_report["tests"], [])
        self.assertEqual(passed_report["stage"], "complete")
        self.assertEqual(passed_report["check_id"], "none")
        self.assertEqual(passed_report["source_git_sha"], status_sha)
        self.assertEqual(passed_report["app_target_sha"], status_sha)
        self.assertIsNone(passed_report["source_binding_sha256"])
        no_pass_fields = {
            **count_fields,
            "logical_pass": 0,
            "logical_expected_fail": 1,
            "logical_skip": 1,
            "attempt_total": 1,
            "attempt_pass": 0,
            "attempt_fail": 1,
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
        self.assertEqual(no_pass_report["logical_counts"]["logical_skip"], 1)
        self.assertEqual(no_pass_report["test_count"], 2)
        failed_report = sanitize_status((failed_status + "\n").encode(), 1)
        self.assertEqual(failed_report["status"], "failed")
        self.assertIsNone(failed_report["test_count"])
        self.assertEqual(failed_report["stage"], "identity")
        checked_failure_status = (
            "LIVE_LAUNCH_STATUS schema=2 status=failed stage=validation "
            "check=provision_marker child_exit=1 "
            f"source_sha={status_sha}\n"
        )
        checked_failure = sanitize_status(checked_failure_status.encode(), 1)
        self.assertEqual(checked_failure["status"], "failed")
        self.assertEqual(checked_failure["check_id"], "provision_marker")
        for malformed in (
            (counts_line.replace(status_sha, "b" * 40) + good_status + "\n").encode(),
            (good_status + "\nPRIVATE_OUTPUT\n").encode(),
            b"x" * 300,
        ):
            report = sanitize_status(malformed, 0)
            self.assertEqual(report["status"], "unavailable")
            self.assertIsNone(report["test_count"])
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

        abort = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort.yml"
        ).read_text(encoding="utf-8")
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
            self.assertNotRegex(step, r"(?:PROD_SSH|CONTROL_EMAIL|TARGET_SHA|LOAD_RUN_ID)")
            self.assertNotRegex(step, r"print\([^\n]*(?:path|raw|payload|email|secret)")

        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' && steps.normalize_abort_evidence.outcome == 'success' }}",
            abort,
        )
        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' && steps.validate_cleanup_evidence.outcome == 'success' }}",
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
        self.assertIn("live_user_qa_success", source)
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.log"
            report = root / "report.json"
            raw.write_text("LIVE_USER_QA_SUCCESS\n", encoding="utf-8")
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
            accepted = subprocess.run(
                [sys.executable, "-c", validator, str(report), runner_sha, app_sha, binding_digest],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            missing_binding = subprocess.run(
                [sys.executable, "-c", sanitizer, str(raw), str(report), "0", runner_sha, app_sha, ""],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing_binding.returncode, 0)

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


if __name__ == "__main__":
    unittest.main()
