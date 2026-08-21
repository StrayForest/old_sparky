from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = PLATFORM_ROOT / "apps" / "platform_web" / "server-shutdown-guard.cjs"
RUN_WEB_PATH = PLATFORM_ROOT / "tools" / "platform_run_web.sh"


class PlatformWebShutdownGuardTests(unittest.TestCase):
    def test_guard_bounds_a_stuck_sigterm_handler(self) -> None:
        env = os.environ.copy()
        env["PLATFORM_WEB_SHUTDOWN_GRACE_MS"] = "1000"

        completed = subprocess.run(
            [
                "node",
                "--require",
                str(GUARD_PATH),
                "-e",
                (
                    "process.on('SIGTERM', () => {}); "
                    "setTimeout(() => process.kill(process.pid, 'SIGTERM'), 20); "
                    "setInterval(() => {}, 1000);"
                ),
            ],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=8,
        )

        self.assertEqual(completed.returncode, 143)
        self.assertIn("shutdown grace period", completed.stdout)

    def test_web_runner_preloads_shutdown_guard(self) -> None:
        runner = RUN_WEB_PATH.read_text(encoding="utf-8")

        self.assertIn("--require", runner)
        self.assertIn("server-shutdown-guard.cjs", runner)


if __name__ == "__main__":
    unittest.main()