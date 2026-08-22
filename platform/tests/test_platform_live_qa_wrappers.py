from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest


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
        self.assertIn("PROD_SSH_HOST", source)
        self.assertIn("ssh " + "\\", source)
        self.assertIn("platform_live_browser_qa.sh public", source)
        self.assertIn("LIVE_BROWSER_QA_SUCCESS", source)
        self.assertIn("type: boolean", source)
        self.assertIn("LIVE_PROVISION", source)
        self.assertIn("LIVE_MARKER", source)
        self.assertIn("platform_provision_live_csp_qa.sh", source)
        self.assertIn("Refusing to replace the existing live QA bundle", source)
        self.assertNotIn("npm ci", source)
        self.assertNotIn("npm run test:live", source)

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
                self.assertNotIn("source ", source)
                self.assertNotIn("platform_runtime_common", source)
                self.assertNotIn("platform_load_env_file", source)
                self.assertNotIn("${PYTHONPATH", source)
                self.assertIn('TRUSTED_REPO_ROOT="/root/old_sparky"', source)
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
