from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
import json
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

from tools import platform_workflow_input_guard, platform_workflow_remote_dispatch
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
        self.assertIn("LIVE_LAUNCH_STATUS schema=1", supervisor)
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
        self.assertIn("workflow_input_guard.py live", source)
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
        self.assertIn('rb"LIVE_LAUNCH_STATUS schema=1 status=(passed|failed) "', source)
        self.assertIn('stage == "complete"', source)
        self.assertIn('child_status == 0', source)
        self.assertLess(
            source.index("platform_workflow_input_guard.py live"),
            source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
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
        emitter_script = emitter_prologue + 'launch_stage="identity"\nexit 1\n'
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
            "LIVE_LAUNCH_STATUS schema=1 status=failed stage=identity "
            f"child_exit=1 source_sha={status_sha}\n",
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
            "LIVE_LAUNCH_STATUS schema=1 status=passed stage=complete "
            f"child_exit=0 source_sha={status_sha}"
        )
        failed_status = (
            "LIVE_LAUNCH_STATUS schema=1 status=failed stage=identity "
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
            self.assertTrue(child_pid.exists())
            heartbeat_size = heartbeat.stat().st_size
            time.sleep(0.05)
            self.assertEqual(heartbeat.stat().st_size, heartbeat_size)

        sanitizer_match = re.search(
            r'/usr/bin/python3 - "\$raw_report" "\$safe_report" '
            r'"\$SUPERVISOR_STATUS" "\$GITHUB_SHA" <<\'PY\'\n(.*?)\n          PY',
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
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(safe_path.read_text(encoding="utf-8"))

        passed_report = sanitize_status((good_status + "\n").encode(), 0)
        self.assertEqual(passed_report["status"], "passed")
        self.assertEqual(passed_report["test_count"], 1)
        self.assertEqual(passed_report["stage"], "complete")
        failed_report = sanitize_status((failed_status + "\n").encode(), 1)
        self.assertEqual(failed_report["status"], "failed")
        self.assertEqual(failed_report["stage"], "identity")
        for malformed in (
            (good_status.replace(status_sha, "b" * 40) + "\n").encode(),
            (good_status + "\nPRIVATE_OUTPUT\n").encode(),
            b"x" * 300,
        ):
            report = sanitize_status(malformed, 0)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["test_count"], 0)
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
                        "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
                        source,
                    )
                    self.assertIn("PLATFORM_LIVE_QA_TARGET_SHA", source)
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
                        "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
                        source,
                    )
                    self.assertIn("PLATFORM_LIVE_QA_TARGET_SHA", source)
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
            "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
            source,
        )
        self.assertIn(
            "live-user-qa",
            source,
        )
        self.assertNotIn("bash -s", source)
        self.assertNotIn("/root/old_sparky", source)
        self.assertIn("TRUSTED_LIVE_QA_ROOT", dispatcher)
        self.assertIn("platform_live_user_qa_trusted.sh", dispatcher)
        self.assertIn("platform_live_qa_mailbox_helper.py", dispatcher)
        self.assertIn("PLATFORM_LIVE_QA_TARGET_SHA", dispatcher)

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
