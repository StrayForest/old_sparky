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

    def test_ssr_stream_diagnostics_records_headers_and_first_chunk_only(self) -> None:
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
        self.assertEqual(completed.stdout.count("stage=first_chunk_emitted"), 1)
        self.assertIn("request_id=request-1", completed.stdout)
        self.assertIn("cf_ray=ray-1", completed.stdout)


if __name__ == "__main__":
    unittest.main()
