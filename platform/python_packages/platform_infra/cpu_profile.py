"""Bounded, signal-controlled CPU profiling for production diagnostics.

The profiler is deliberately dormant unless an operator supplies an explicit
output directory.  A diagnostic runner arms it with SIGUSR1 and flushes it
with SIGUSR2, so normal API workers do not pay cProfile's per-call cost.
"""

from __future__ import annotations

import asyncio
import cProfile
from contextlib import suppress
import os
from pathlib import Path
import signal
import time
import pstats


class ReadyVoteCpuProfiler:
    """Keep one API worker's cProfile session bounded and operator controlled."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._profile = cProfile.Profile()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._armed = False
        self._flushed = False
        self._armed_at: float | None = None

    @classmethod
    def from_environment(cls) -> "ReadyVoteCpuProfiler | None":
        raw_dir = os.environ.get("PLATFORM_READY_VOTE_CPU_PROFILE_DIR", "").strip()
        if not raw_dir:
            return None
        return cls(Path(raw_dir))

    async def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.get_running_loop()
        for signum, callback in (
            (signal.SIGUSR1, self.arm),
            (signal.SIGUSR2, self.flush),
        ):
            with suppress(NotImplementedError):
                self._loop.add_signal_handler(signum, callback)

    def arm(self) -> None:
        if self._armed or self._flushed:
            return
        self._profile.enable()
        self._armed = True
        self._armed_at = time.monotonic()

    def flush(self) -> None:
        if self._flushed:
            return
        if self._armed:
            self._profile.disable()
        self._flushed = True
        pid = os.getpid()
        stem = self.output_dir / f"ready-vote-cprofile-{pid}"
        self._profile.dump_stats(str(stem.with_suffix(".pstats")))
        with stem.with_suffix(".txt").open("w", encoding="utf-8") as stream:
            stats = pstats.Stats(self._profile, stream=stream)
            stats.strip_dirs().sort_stats("cumulative").print_stats(100)
            profiled_seconds = (
                max(0.0, time.monotonic() - self._armed_at)
                if self._armed_at is not None
                else 0.0
            )
            stream.write(f"profiled_seconds={profiled_seconds:.6f}\n")

    async def stop(self) -> None:
        self.flush()
        if self._loop is not None:
            for signum in (signal.SIGUSR1, signal.SIGUSR2):
                with suppress(NotImplementedError):
                    self._loop.remove_signal_handler(signum)
