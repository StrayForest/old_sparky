from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = PLATFORM_ROOT / "apps" / "platform_web" / "server-shutdown-guard.cjs"
RUN_WEB_PATH = PLATFORM_ROOT / "tools" / "platform_run_web.sh"


class PlatformWebShutdownGuardTests(unittest.TestCase):
    def test_shutdown_runtime_contract_is_owned_by_pinned_web_gate(self) -> None:
        package = json.loads(
            (PLATFORM_ROOT / "apps" / "platform_web" / "package.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            package["scripts"]["test:shutdown-guard"],
            "../../tools/platform_node.sh tests/shutdown-guard-contract.cjs",
        )

        verifier = (PLATFORM_ROOT / "tools" / "platform_verify.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(verifier.count('"web-quality/shutdown-guard"'), 1)
        self.assertEqual(verifier.count('"test:shutdown-guard"'), 1)
        catalog = (PLATFORM_ROOT / "tools" / "platform_test_catalog.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("shutdown-guard-contract.cjs", catalog)

        workflow = (PLATFORM_ROOT.parent / ".github" / "workflows" / "platform-security.yml").read_text(
            encoding="utf-8"
        )
        web_quality = workflow.split("  web-quality:\n", 1)[1].split("\n  docs:\n", 1)[0]
        self.assertIn('node-version: "26.3.1"', web_quality)
        self.assertIn("platform_verify.py web-quality", web_quality)
        self.assertNotIn("test:shutdown-guard", web_quality)

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

    def test_ssr_stream_diagnostics_records_headers_and_lifecycle(self) -> None:
        env = os.environ.copy()
        env["PLATFORM_SSR_PERF_LOG_ENABLED"] = "true"
        env["PLATFORM_SSR_PERF_SAMPLE_RATE"] = "1"
        completed = subprocess.run(
            ["node", "--require", str(GUARD_PATH), "-e", """
const http = require('node:http');
const server = http.createServer((request, response) => {
  response.writeHead(200, {'content-type': 'text/html'});
  response.write('<html>');
  response.end('</html>');
});
server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  const response = await fetch('http://127.0.0.1:' + port + '/tournaments/private-slug', {
    headers: {
      accept: 'text/html',
      'x-request-id': 'request-1',
      'cf-ray': 'ray-1',
    },
  });
  await response.text();
  server.close();
});
"""],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=8,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.count("stage=response_stream_start"), 1)
        self.assertEqual(completed.stdout.count("stage=first_body_write_attempt"), 1)
        self.assertEqual(completed.stdout.count("stage=response_finish"), 1)
        self.assertEqual(completed.stdout.count("stage=response_close"), 1)
        self.assertNotIn("stage=response_error", completed.stdout)
        self.assertIn("writable_finished=0", completed.stdout)
        self.assertIn("writable_finished=1", completed.stdout)
        self.assertIn("write_count=2", completed.stdout)
        self.assertIn("body_bytes=13", completed.stdout)
        self.assertIn("request_id=request-1", completed.stdout)
        self.assertIn("cf_ray=ray-1", completed.stdout)

    def test_ssr_stream_diagnostics_disabled_path_is_inert(self) -> None:
        env = os.environ.copy()
        env.pop("PLATFORM_SSR_PERF_LOG_ENABLED", None)
        env["PLATFORM_SSR_PERF_SAMPLE_RATE"] = "1"
        completed = subprocess.run(
            ["node", "--require", str(GUARD_PATH), "-e", """
const http = require('node:http');
const server = http.createServer((request, response) => {
  response.writeHead(200, {'content-type': 'text/html'});
  response.end('<html></html>');
});
server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  const response = await fetch('http://127.0.0.1:' + port + '/tournaments/private-slug', {
    headers: {accept: 'text/html', 'x-request-id': 'request-disabled'},
  });
  await response.text();
  server.close();
});
"""],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=8,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ssr_stream", completed.stdout)


if __name__ == "__main__":
    unittest.main()
