from __future__ import annotations

from contextlib import redirect_stderr
from io import BytesIO, StringIO, TextIOWrapper
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import platform_workflow_input_guard, platform_workflow_remote_dispatch
from tools.platform_workflow_input_guard import (
    WorkflowInputError,
    validate_confirmation,
    validate_control_email,
    validate_deployment_payload,
    validate_external_payload,
    validate_live_payload,
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
        self.assertIn("LIVE_BROWSER_QA_SUCCESS", supervisor)
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
        self.assertIn("Provisioning requires a fresh liveqa marker", supervisor)
        self.assertNotIn("Refusing to replace the existing live QA bundle", source)
        self.assertIn("PLATFORM_LIVE_QA_INSTALL_ROOT", supervisor)
        self.assertIn("platform_live_user_qa_dispatch.py verify", supervisor)
        self.assertIn("platform_release_lock_exec.sh", supervisor)
        self.assertNotIn("/root/old_sparky", supervisor)
        self.assertNotIn("npm ci", source)
        self.assertNotIn("npm run test:live", source)
        self.assertNotIn('bash -s -- "$LIVE_BASE_URL"', source)
        self.assertLess(
            source.index("platform_workflow_input_guard.py live"),
            source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )

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
            external_source.index("platform_workflow_input_guard.py external"),
            external_source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        self.assertLess(
            cleanup_source.index("platform-retained-cleanup-input.json"),
            cleanup_source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        for workflow_source, modes in workflow_modes:
            expected_dispatcher = (
                dispatcher
                if workflow_source is source
                else '"$HOST_TOOLS_DISPATCHER"'
                if workflow_source is deploy_source
                else "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py"
            )
            for mode in modes:
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
                    platform_workflow_remote_dispatch.subprocess,
                    "run",
                    return_value=type("Result", (), {"returncode": 0})(),
                ) as run:
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["live-launch"]),
                    0,
                )
            self.assertEqual(
                run.call_args.args[0],
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
            (
                "platform-production-release-abort.yml",
                "ABORT-RETAINED-RELEASE-MIGRATION-NOT-REVERSED",
            ),
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

        with tempfile.TemporaryDirectory() as directory:
            tools_root = Path(directory)
            helper = tools_root / "platform_production_deploy_supervisor.sh"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o555)
            stdin = TextIOWrapper(
                BytesIO((json.dumps(valid) + "\n").encode("utf-8")),
                encoding="utf-8",
            )
            with patch.object(platform_workflow_remote_dispatch.sys, "stdin", stdin), \
                patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_root), \
                patch.object(platform_workflow_remote_dispatch, "DEPLOY_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True), \
                patch.object(
                    platform_workflow_remote_dispatch.subprocess,
                    "run",
                    return_value=type("Result", (), {"returncode": 0})(),
                ) as run:
                self.assertEqual(
                    platform_workflow_remote_dispatch.main(["production-deploy"]),
                    0,
                )
            self.assertEqual(
                run.call_args.args[0],
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
                ],
            )

    def test_cleanup_export_inventory_is_closed_and_idempotent(self) -> None:
        def build_root(prefix: str, run_id: str, names: tuple[str, ...]) -> Path:
            root = Path(f"{prefix}{run_id}")
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            if os.geteuid() == 0:
                os.chown(root, 1000, 1000)
            owner = root.stat().st_uid
            for name in names:
                entry = root / name
                entry.write_text("{}\n", encoding="utf-8")
                os.chmod(entry, 0o600)
                if os.geteuid() == 0:
                    os.chown(entry, owner, owner)
            return root

        load_names = (
            "complete",
            "ready",
            "manifest.json",
            "matrix-summary.json",
            "canonical.log",
            "server-observability.json",
            "qa-command.log",
            "server-observer.log",
            "timeout-diagnostic-ids.json",
            "supervisor.exit",
        )
        cleanup_names = ("cleanup-summary.json", "canonical.log", "cleanup.log")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            load_prefix = f"{base}/load-"
            cleanup_prefix = f"{base}/cleanup-"
            expected_uid = 1000 if os.geteuid() == 0 else os.getuid()
            with patch.object(platform_workflow_remote_dispatch, "EXTERNAL_EXPORT_PREFIX", load_prefix), \
                patch.object(platform_workflow_remote_dispatch, "CLEANUP_EXPORT_PREFIX", cleanup_prefix), \
                patch.object(platform_workflow_remote_dispatch.os, "getuid", return_value=expected_uid):
                load_root = build_root(load_prefix, "41", load_names)
                cleanup_root = build_root(cleanup_prefix, "42", cleanup_names)
                self.assertEqual(
                    platform_workflow_remote_dispatch._remove_exports(
                        load_run_id="41", cleanup_run_id="42"
                    ),
                    0,
                )
                self.assertFalse(load_root.exists())
                self.assertFalse(cleanup_root.exists())
                # A retry after both roots have been removed is deliberately
                # idempotent and does not turn an already-complete cleanup red.
                self.assertEqual(
                    platform_workflow_remote_dispatch._remove_exports(
                        load_run_id="41", cleanup_run_id="42"
                    ),
                    0,
                )

                unknown_root = build_root(load_prefix, "43", ("unknown",))
                self.assertEqual(
                    platform_workflow_remote_dispatch._remove_exports(
                        load_run_id="43", cleanup_run_id="44"
                    ),
                    1,
                )
                self.assertTrue(unknown_root.exists())
                shutil.rmtree(unknown_root)

                symlink_root = build_root(load_prefix, "45", ())
                target = base / "symlink-target"
                target.write_text("{}\n", encoding="utf-8")
                (symlink_root / "canonical.log").symlink_to(target)
                self.assertEqual(
                    platform_workflow_remote_dispatch._remove_exports(
                        load_run_id="45", cleanup_run_id="46"
                    ),
                    1,
                )
                symlink_root.unlink() if symlink_root.is_symlink() else None
                shutil.rmtree(symlink_root)

                socket_root = build_root(load_prefix, "47", ())
                socket_path = socket_root / "canonical.log"
                unix_socket = socket.socket(socket.AF_UNIX)
                try:
                    unix_socket.bind(str(socket_path))
                    self.assertEqual(
                        platform_workflow_remote_dispatch._remove_exports(
                            load_run_id="47", cleanup_run_id="48"
                        ),
                        1,
                    )
                finally:
                    unix_socket.close()
                shutil.rmtree(socket_root)

                rmdir_load = build_root(load_prefix, "49", ("canonical.log",))
                build_root(cleanup_prefix, "50", ("canonical.log",))
                with patch.object(Path, "rmdir", side_effect=OSError("blocked")):
                    self.assertEqual(
                        platform_workflow_remote_dispatch._remove_exports(
                            load_run_id="49", cleanup_run_id="50"
                        ),
                        1,
                    )
                self.assertTrue(rmdir_load.exists())

                identity_root = build_root(load_prefix, "51", ())
                if os.geteuid() == 0:
                    os.chown(identity_root, 0, 0)
                    self.assertEqual(
                        platform_workflow_remote_dispatch._remove_exports(
                            load_run_id="51", cleanup_run_id="52"
                        ),
                        1,
                    )
                    self.assertTrue(identity_root.exists())

                root_file_root = build_root(load_prefix, "53", ("canonical.log",))
                if os.geteuid() == 0:
                    os.chown(root_file_root / "canonical.log", 0, 0)
                    self.assertEqual(
                        platform_workflow_remote_dispatch._remove_exports(
                            load_run_id="53", cleanup_run_id="54"
                        ),
                        1,
                    )
                self.assertTrue(root_file_root.exists())

                root_barrier_root = build_root(load_prefix, "56", ("supervisor.exit",))
                if os.geteuid() == 0:
                    os.chown(root_barrier_root / "supervisor.exit", 0, 0)
                    self.assertEqual(
                        platform_workflow_remote_dispatch._remove_exports(
                            load_run_id="56", cleanup_run_id="57"
                        ),
                        1,
                    )
                self.assertTrue(root_barrier_root.exists())
                shutil.rmtree(root_barrier_root)

                # Inventory both roots before deleting either one: an
                # unexpected cleanup-export entry must not partially erase a
                # valid load-export root.
                valid_load_root = build_root(load_prefix, "54", ("canonical.log",))
                invalid_cleanup_root = build_root(cleanup_prefix, "55", ("unknown",))
                self.assertEqual(
                    platform_workflow_remote_dispatch._remove_exports(
                        load_run_id="54", cleanup_run_id="55"
                    ),
                    1,
                )
                self.assertTrue(valid_load_root.exists())
                self.assertTrue(invalid_cleanup_root.exists())
                shutil.rmtree(valid_load_root)
                shutil.rmtree(invalid_cleanup_root)

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
