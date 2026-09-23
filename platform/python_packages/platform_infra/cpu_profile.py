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
import tempfile
import time
import pstats


def _process_start_time_ticks() -> int | None:
    """Return this process' Linux start-time identity for artifact binding."""

    try:
        raw_stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        comm_end = raw_stat.rfind(")")
        columns = raw_stat[comm_end + 2 :].split()
        if comm_end < 0 or len(columns) < 20:
            return None
        value = int(columns[19])
    except (OSError, UnicodeError, ValueError):
        return None
    return value if value > 0 else None


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ReadyVoteCpuProfiler:
    """Keep one API worker's cProfile session bounded and operator controlled."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._profile = cProfile.Profile()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._armed = False
        self._flushed = False
        self._armed_at: float | None = None
        self._start_time_ticks = _process_start_time_ticks()

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
        if not self._armed:
            # Normal API shutdown must not leave an empty artifact that a
            # later observer could mistake for a profiled window.
            self._flushed = True
            return
        if self._armed:
            self._profile.disable()
        self._flushed = True
        pid = os.getpid()
        if self._start_time_ticks is None:
            # A profile without a process-generation identity is not safe to
            # attribute, so fail closed and preserve the observer's other
            # evidence.
            return
        stem = self.output_dir / f"ready-vote-cprofile-{pid}-{self._start_time_ticks}"
        temporary_paths: list[Path] = []
        try:
            pstats_fd, pstats_temporary = tempfile.mkstemp(
                prefix=f".{stem.name}-",
                suffix=".pstats.tmp",
                dir=self.output_dir,
            )
            pstats_temporary_path = Path(pstats_temporary)
            temporary_paths.append(pstats_temporary_path)
            os.close(pstats_fd)
            self._profile.dump_stats(str(pstats_temporary_path))
            _fsync_file(pstats_temporary_path)

            text_fd, text_temporary = tempfile.mkstemp(
                prefix=f".{stem.name}-",
                suffix=".txt.tmp",
                dir=self.output_dir,
            )
            text_temporary_path = Path(text_temporary)
            temporary_paths.append(text_temporary_path)
            with os.fdopen(text_fd, "w", encoding="utf-8") as stream:
                stats = pstats.Stats(self._profile, stream=stream)
                stats.strip_dirs().sort_stats("cumulative").print_stats(100)
                profiled_seconds = (
                    max(0.0, time.monotonic() - self._armed_at)
                    if self._armed_at is not None
                    else 0.0
                )
                stream.write(f"profiled_seconds={profiled_seconds:.6f}\n")
                stream.flush()
                os.fsync(stream.fileno())

            pstats_path = stem.with_suffix(".pstats")
            text_path = stem.with_suffix(".txt")
            os.replace(pstats_temporary_path, pstats_path)
            temporary_paths.remove(pstats_temporary_path)
            _fsync_directory(self.output_dir)
            os.replace(text_temporary_path, text_path)
            temporary_paths.remove(text_temporary_path)
            _fsync_directory(self.output_dir)
        finally:
            for temporary_path in temporary_paths:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    async def stop(self) -> None:
        self.flush()
        if self._loop is not None:
            for signum in (signal.SIGUSR1, signal.SIGUSR2):
                with suppress(NotImplementedError):
                    self._loop.remove_signal_handler(signum)
