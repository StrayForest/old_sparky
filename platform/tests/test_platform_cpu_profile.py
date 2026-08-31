from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from python_packages.platform_infra.cpu_profile import ReadyVoteCpuProfiler


class ReadyVoteCpuProfilerTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiler_is_disabled_without_explicit_output_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ReadyVoteCpuProfiler.from_environment())

    async def test_signal_session_writes_worker_profile_and_text_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            profiler = ReadyVoteCpuProfiler(Path(temporary_dir))
            await profiler.start()
            profiler.arm()
            sum(range(10_000))
            profiler.flush()
            await profiler.stop()

            self.assertEqual(
                sorted(path.name for path in Path(temporary_dir).iterdir()),
                [
                    f"ready-vote-cprofile-{os.getpid()}.pstats",
                    f"ready-vote-cprofile-{os.getpid()}.txt",
                ],
            )
            self.assertIn("sum", Path(temporary_dir, f"ready-vote-cprofile-{os.getpid()}.txt").read_text())
