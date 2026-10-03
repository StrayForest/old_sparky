import gc
import importlib
import json
import os
from pathlib import Path
import resource
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

if sys.argv[1:] == ["hostile"]:
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN), print("hostile-ready", flush=True)
    time.sleep(30)
    raise SystemExit(0)

sys.path.insert(0, str(Path.cwd()))
load = importlib.import_module("tools.platform_external_load")
RequestResult, VirtualUser = load.RequestResult, load.VirtualUser

CONCURRENCY, REQUESTS, ACCUMULATORS, ACCUMULATOR_REQUESTS = 512, 4096, 4, 16384
PAYLOAD_BYTES, OUTPUT_BYTES = 64 * 1024, 64 * 1024


def _rss() -> dict[str, int]:
    values = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, _, raw = line.partition(":")
        if key in {"VmRSS", "VmHWM"}:
            values[key] = int(raw.strip().split()[0]) * 1024
    values["ru_maxrss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return values


def _threads() -> tuple[list[int], list[int]]:
    rows = [thread for thread in threading.enumerate() if thread.native_id is not None]
    return (sorted(thread.native_id for thread in rows), sorted(thread.native_id for thread in rows if thread.name.startswith("external-load")))


def _children() -> list[int]:
    path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    return [int(value) for value in path.read_text(encoding="ascii").split()] if path.exists() else []


class _TrackingExecutor(ThreadPoolExecutor):
    instances: list["_TrackingExecutor"] = []
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock, self.pending = threading.Lock(), set()
        self.submitted = self.peak_pending = 0
        type(self).instances.append(self)
    def _done(self, future: object) -> None:
        with self.lock:
            self.pending.discard(future)
    def submit(self, *args, **kwargs):
        future = super().submit(*args, **kwargs)
        with self.lock:
            self.submitted += 1
            self.pending.add(future)
            self.peak_pending = max(self.peak_pending, len(self.pending))
        future.add_done_callback(self._done)
        return future


def _phase(name: str, runner, users: list[VirtualUser]) -> dict[str, object]:
    state = {"completed": 0, "live": 0, "peak": 0, "ready": False}
    lock, release = threading.Lock(), threading.Event()
    def build(_origin: str, _user: VirtualUser, phase: str, _timeout: float) -> RequestResult:
        payload = b"x" * PAYLOAD_BYTES
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            if state["live"] == CONCURRENCY:
                state["ready"] = True
                release.set()
        release.wait(2)
        return RequestResult(
            phase=phase, method="GET", path="/synthetic/rss", status=200,
            elapsed_ms=1, ok=True, response_bytes=PAYLOAD_BYTES, response_json=payload,
        )
    def consume(result: RequestResult) -> None:
        if not isinstance(result.response_json, bytes) or len(result.response_json) != PAYLOAD_BYTES:
            raise AssertionError("payload")
        with lock:
            state["completed"] += 1
            state["live"] -= 1
    before = _rss()
    args = dict(phase=name, concurrency=CONCURRENCY, timeout=1, request_builder=build, result_consumer=consume)
    if name == "run_phase":
        args["spread_seconds"] = 0
    else:
        args["duration_seconds"] = 0
    returned = runner("http://synthetic.invalid", users, **args)
    executor = _TrackingExecutor.instances[-1]
    after = _rss()
    return {
        "name": name, "submitted": executor.submitted, "completed": state["completed"],
        "peak_pending": executor.peak_pending, "pending_after": len(executor.pending), "peak_live_payloads": state["peak"],
        "live_payloads_after": state["live"], "ready": state["ready"], "returned_results": len(returned[0] if name != "run_phase" else returned),
        "hwm_delta_bytes": max(after[key] - before[key] for key in ("VmHWM", "ru_maxrss")),
    }


def _live() -> dict[str, object]:
    gc.collect()
    baseline, baseline_threads, baseline_children, baseline_fds = _rss(), _threads(), _children(), len(os.listdir("/proc/self/fd"))
    users, _TrackingExecutor.instances = [
        VirtualUser(f"rss-{index:08d}", "synthetic", "s" * 64, "c" * 64)
        for index in range(REQUESTS)
    ], []
    original = load.ThreadPoolExecutor
    load.ThreadPoolExecutor = _TrackingExecutor
    try:
        runs = [_phase("run_phase", load.run_phase, users), _phase("run_rate_phase", load.run_rate_phase, users)]
    finally:
        load.ThreadPoolExecutor = original
    gc.collect()
    after_threads, after, after_fds = _threads(), _rss(), len(os.listdir("/proc/self/fd"))
    return {
        "mode": "live", "requests": REQUESTS, "concurrency": CONCURRENCY,
        "payload_bytes": PAYLOAD_BYTES, "baseline": baseline, "baseline_fd_count": baseline_fds, "baseline_thread_ids": baseline_threads[0], "baseline_external_thread_ids": baseline_threads[1],
        "after": after, "rss_delta_bytes": after["VmRSS"] - baseline["VmRSS"], "after_fd_count": after_fds, "after_thread_ids": after_threads[0], "after_external_thread_ids": after_threads[1], "direct_children_before": baseline_children, "direct_children_after": _children(),
        "hwm_delta_bytes": max(after[key] - baseline[key] for key in ("VmHWM", "ru_maxrss")),
        "runs": runs,
    }


def _accumulators() -> dict[str, object]:
    gc.disable()
    baseline, baseline_threads, baseline_children, baseline_fds = _rss(), _threads(), _children(), len(os.listdir("/proc/self/fd"))
    accumulators = []
    timing = dict.fromkeys(("dns_ms", "tcp_connect_ms", "tls_handshake_ms", "request_write_ms", "edge_wait_ms", "ttfb_ms", "body_receive_ms", "total_ms"), 1.0) | {"transport": "http1-keepalive", "http_version": "1.1", "connection_reused": True}
    for accumulator_index in range(ACCUMULATORS):
        accumulator = load._ResultAccumulator()
        result = RequestResult(
            phase="synthetic", method="GET", path="/synthetic/rss", status=200,
            elapsed_ms=0, ok=True, response_bytes=PAYLOAD_BYTES, cf_ray="",
            transport_timing=timing, scheduled_at_monotonic=0,
            enqueued_at_monotonic=0, started_at_monotonic=0, finished_at_monotonic=1,
            executor_queue_wait_ms=0, schedule_delay_ms=0, late_start_ms=0,
            user_observed_elapsed_ms=1,
        )
        for index in range(ACCUMULATOR_REQUESTS):
            value = float(index + 1)
            result.elapsed_ms = value
            result.cf_ray = f"synthetic-ray-{accumulator_index}-{index}"
            result.scheduled_at_monotonic = value
            result.enqueued_at_monotonic = value
            result.started_at_monotonic = value
            result.finished_at_monotonic = value + 1
            accumulator.add(result)
        accumulators.append(accumulator)
    after_threads, after, after_fds = _threads(), _rss(), len(os.listdir("/proc/self/fd"))
    return {
        "mode": "accumulator", "accumulators": ACCUMULATORS,
        "requests_per_accumulator": ACCUMULATOR_REQUESTS, "unique_cf_rays": len({ray for item in accumulators for ray in item.cf_rays}),
        "completed": sum(item.requests for item in accumulators),
        "timing_complete": all(item.timing.completed_count == ACCUMULATOR_REQUESTS and not item.timing.missing_timing_context and not item.timing.invalid_timing_context for item in accumulators), "transport_field_counts": {key: sum(len(item.transport_phase_values.get(key, ())) for item in accumulators) for key in ("dns_ms", "tcp_connect_ms", "tls_handshake_ms", "request_write_ms", "edge_wait_ms", "ttfb_ms", "body_receive_ms", "total_ms")},
        "baseline": baseline, "baseline_fd_count": baseline_fds, "baseline_thread_ids": baseline_threads[0], "after": after, "rss_delta_bytes": after["VmRSS"] - baseline["VmRSS"], "after_fd_count": after_fds, "after_thread_ids": after_threads[0], "after_external_thread_ids": after_threads[1],
        "direct_children_before": baseline_children, "direct_children_after": _children(),
        "hwm_delta_bytes": max(after[key] - baseline[key] for key in ("VmHWM", "ru_maxrss")),
    }


def main() -> int:
    if sys.platform != "linux" or not Path("/proc/self/status").exists():
        return 2
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        report = _live() if mode == "live" else _accumulators() if mode == "accumulator" else None
        if report is None:
            return 2
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except BaseException:
        print(json.dumps({"mode": mode, "ok": False}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
