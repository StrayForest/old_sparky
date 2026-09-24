from __future__ import annotations

import json
from pathlib import Path
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
RUN_WEB_PATH = PLATFORM_ROOT / "tools" / "platform_run_web.sh"


class PlatformWebShutdownGuardTests(unittest.TestCase):
    def test_web_runtime_contracts_are_owned_by_pinned_web_gate(self) -> None:
        package = json.loads(
            (PLATFORM_ROOT / "apps" / "platform_web" / "package.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            package["scripts"]["test:shutdown-guard"],
            "../../tools/platform_node.sh tests/shutdown-guard-contract.cjs",
        )
        self.assertEqual(
            package["scripts"]["test:ssr-stream-diagnostics"],
            "../../tools/platform_node.sh tests/ssr-stream-diagnostics-contract.cjs",
        )

        verifier = (PLATFORM_ROOT / "tools" / "platform_verify.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(verifier.count('"web-quality/shutdown-guard"'), 1)
        self.assertEqual(verifier.count('"web-quality/ssr-stream-diagnostics"'), 1)
        self.assertEqual(verifier.count('"test:shutdown-guard"'), 1)
        self.assertEqual(verifier.count('"test:ssr-stream-diagnostics"'), 1)
        catalog = (PLATFORM_ROOT / "tools" / "platform_test_catalog.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("shutdown-guard-contract.cjs", catalog)
        self.assertNotIn("ssr-stream-diagnostics-contract.cjs", catalog)

        workflow = (PLATFORM_ROOT.parent / ".github" / "workflows" / "platform-security.yml").read_text(
            encoding="utf-8"
        )
        web_quality = workflow.split("  web-quality:\n", 1)[1].split("\n  docs:\n", 1)[0]
        self.assertIn('node-version: "26.3.1"', web_quality)
        self.assertIn("platform_verify.py web-quality", web_quality)
        self.assertNotIn("test:shutdown-guard", web_quality)
        self.assertNotIn("test:ssr-stream-diagnostics", web_quality)

    def test_web_runner_preloads_shutdown_guard(self) -> None:
        runner = RUN_WEB_PATH.read_text(encoding="utf-8")

        self.assertIn("--require", runner)
        self.assertIn("server-shutdown-guard.cjs", runner)
        self.assertIn("PLATFORM_WEB_WORKERS", runner)
        self.assertIn("server-cluster.cjs", runner)

    def test_cluster_runner_is_bounded_to_two_workers(self) -> None:
        runner = (PLATFORM_ROOT / "apps" / "platform_web" / "server-cluster.cjs").read_text(
            encoding="utf-8"
        )

        self.assertIn("workerCount < 2 || workerCount > 2", runner)
        self.assertIn("maxWorkerRestartsPerMinute = 4", runner)
        self.assertIn("cluster.fork()", runner)

if __name__ == "__main__":
    unittest.main()
