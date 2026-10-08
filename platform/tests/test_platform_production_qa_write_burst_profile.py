import asyncio
import unittest
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import platform_production_qa
from tools.platform_production_qa import (
    HttpMetricsRecorder,
    HttpSample,
    SystemSampler,
    _attach_server_diagnostic_sample,
    burst_offsets,
    follow_up_read_counts,
    evaluate_write_burst_profiles,
    is_compact_mutation_response,
    summarize_bottleneck_evidence,
    summarize_request_perf_logs,
)


class ProductionQaWriteBurstProfileTests(unittest.TestCase):
    def test_system_summary_keeps_sample_window_when_postgres_has_active_queries(self) -> None:
        sampler = SystemSampler.__new__(SystemSampler)
        sampler.interval_seconds = 1.0
        sampler.samples = [
            {
                "cpu_per_core_percent": {"cpu0": 10.0},
                "memory_used_bytes": 100,
                "memory_total_bytes": 200,
                "swap_used_bytes": 0,
                "swap_total_bytes": 0,
                "load_average": {"1m": 0.1, "5m": 0.1, "15m": 0.1},
                "nginx_connections": {"established": 1},
                "postgres_tcp_connections": {"established": 2},
                "redis_connections": {"established": 3},
                "gunicorn": {"workers": 2},
                "postgres_cpu_percent": 4.0,
                "processes": {
                    "api": {
                        "process_count": 2,
                        "cpu_percent": 10.0,
                        "rss_bytes": 100,
                        "read_bytes_per_second": 0,
                        "write_bytes_per_second": 0,
                    }
                },
                "postgres_waits": {
                    "lock_waiters": 0,
                    "waiting_backends": 0,
                    "ungranted_locks": 0,
                    "max_waiting_query_ms": 10,
                    "max_lock_waiting_query_ms": 0,
                    "wait_state_counts": {"Lock": 1, "IO": 2},
                    "backend_connections": 4,
                    "backend_ownership": [
                        {"application_name": "oldsparky-api", "current": 2},
                        {"application_name": "oldsparky-worker", "current": 1},
                        {"application_name": "unknown", "current": 1},
                    ],
                },
                "celery_backlog": {
                    "deadlock-platform-high": 0,
                    "deadlock-platform-default": 0,
                    "deadlock-platform-low": 0,
                },
            }
        ]

        result = sampler.summary()

        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["gunicorn_workers"]["last"], 2)
        self.assertEqual(result["postgres_waits"]["wait_state_counts"]["lock"]["max"], 1)
        self.assertEqual(result["postgres_waits"]["wait_state_counts"]["io"]["max"], 2)
        self.assertEqual(
            result["postgres_backend_ownership"]["api"]["max"],
            2,
        )
        self.assertEqual(
            result["postgres_backend_ownership"]["worker"]["last"],
            1,
        )
        self.assertEqual(result["postgres_backend_connections"]["max"], 4)
        self.assertEqual(
            sum(
                entry["max"]
                for entry in result["postgres_backend_ownership"].values()
            ),
            result["postgres_backend_connections"]["max"],
        )
        self.assertEqual(result["postgres_backend_ownership"]["other"]["max"], 1)
        self.assertTrue(result["postgres_backend_ownership_consistency"]["all_match"])

    def test_scoped_process_resources_preserve_reported_groups_and_skip_unclassified_io(self) -> None:
        labels = {
            "deadlock-api": (
                "python3",
                "gunicorn apps.platform_api.app.main:app",
            ),
            "deadlock-web": (
                "node",
                "node /platform/apps/platform_web/server.js",
            ),
            "deadlock-worker": (
                "celery",
                "celery -A apps.platform_worker.worker:celery_app worker",
            ),
            "postgresql": ("postgres", "postgres: platformdb idle"),
            "redis-server": ("redis-server", "redis-server 127.0.0.1:6379"),
            "nginx": ("nginx", "nginx: worker process"),
            "load-generator": (
                "python3",
                "python platform_production_qa.py --profile write-burst",
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_proc = Path(temporary_directory) / "proc"
            fake_proc.mkdir()
            (fake_proc / "loadavg").write_text(
                "0.10 0.20 0.30 1/1 1\n", encoding="utf-8"
            )
            process_count = 189
            entries = list(labels.values()) + [
                ("python3", f"unrelated-task-{index}")
                for index in range(process_count - len(labels))
            ]

            def write_process(
                root: Path,
                pid: int,
                comm: str,
                cmdline: str,
                offset: int,
            ) -> None:
                child = root / str(pid)
                child.mkdir()
                stat_columns = ["S", "1"] + ["0"] * 9 + [str(min(10 + offset, 20)), "3"] + [
                    "0"
                ] * 6 + [str(100 + offset)]
                (child / "stat").write_text(
                    f"{pid} ({comm}) {' '.join(stat_columns)}\n",
                    encoding="utf-8",
                )
                (child / "comm").write_text(comm + "\n", encoding="utf-8")
                (child / "cmdline").write_bytes(cmdline.encode() + b"\0")
                (child / "io").write_text(
                    f"read_bytes: {100 + offset}\nwrite_bytes: {200 + offset}\n",
                    encoding="utf-8",
                )
                (child / "status").write_text(
                    f"Name:\t{comm}\nVmRSS:\t{10 + offset} kB\n",
                    encoding="utf-8",
                )

            for offset, (comm, cmdline) in enumerate(entries):
                pid = 1000 + offset
                write_process(fake_proc, pid, comm, cmdline, offset)

            original_path = Path
            def path_factory(value: str | Path) -> Path:
                raw_path = str(value)
                if raw_path == "/proc":
                    return fake_proc
                if raw_path.startswith("/proc/"):
                    return fake_proc / raw_path.removeprefix("/proc/")
                return original_path(value)
            with (
                patch.object(platform_production_qa, "Path", new=path_factory),
                patch.object(platform_production_qa, "read_boot_time_epoch", return_value=1_000.0),
                patch.object(
                    platform_production_qa,
                    "read_process_io",
                    wraps=platform_production_qa.read_process_io,
                ) as read_io,
                patch.object(
                    platform_production_qa,
                    "read_process_rss_bytes",
                    wraps=platform_production_qa.read_process_rss_bytes,
                ) as read_rss,
            ):
                full = platform_production_qa.iter_processes()
                self.assertEqual(read_io.call_count, process_count)
                self.assertEqual(read_rss.call_count, process_count)

                read_io.reset_mock()
                read_rss.reset_mock()
                scoped = platform_production_qa.iter_processes(
                    resource_metrics_for_labels=frozenset(
                        platform_production_qa.PROCESS_LABELS
                    )
                )
                self.assertEqual(read_io.call_count, len(labels))
                self.assertEqual(read_rss.call_count, len(labels))

                original_iter_processes = platform_production_qa.iter_processes

                def sample_twice(*, collect_all_process_resources: bool) -> list[dict[str, object]]:
                    original_api_process = fake_proc / "1000"
                    replacement_api_process = fake_proc / "2000"
                    if replacement_api_process.exists():
                        shutil.rmtree(replacement_api_process)
                    if not original_api_process.exists():
                        write_process(
                            fake_proc,
                            1000,
                            *labels["deadlock-api"],
                            0,
                        )
                    reads = 0

                    def sample_iter_processes(**kwargs: object) -> list[dict[str, object]]:
                        nonlocal reads
                        reads += 1
                        if reads == 2:
                            shutil.rmtree(original_api_process)
                            write_process(
                                fake_proc,
                                2000,
                                *labels["deadlock-api"],
                                2000,
                            )
                        if collect_all_process_resources:
                            return original_iter_processes()
                        return original_iter_processes(
                            resource_metrics_for_labels=frozenset(
                                platform_production_qa.PROCESS_LABELS
                            )
                        )

                    sampler = SystemSampler.__new__(SystemSampler)
                    sampler.interval_seconds = 1.0
                    sampler.samples = []
                    sampler._task = None
                    sampler._previous_cpu = None
                    sampler._previous_cpu_steal = None
                    sampler._previous_postgres_ticks = None
                    sampler._previous_process_groups = None
                    sampler._previous_monotonic = None
                    sampler._api_port = 8010
                    sampler._celery_redis = None

                    async def sample_twice_async() -> None:
                        await sampler.sample()
                        await sampler.sample()

                    with (
                        patch.object(
                            platform_production_qa,
                            "iter_processes",
                            side_effect=sample_iter_processes,
                        ),
                        patch.object(
                            platform_production_qa,
                            "time",
                            new=SimpleNamespace(monotonic=iter((10.0, 11.0)).__next__),
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_cpu_totals",
                            side_effect=(
                                {"cpu0": (100, 20)},
                                {"cpu0": (120, 22)},
                            ),
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_cpu_steal_ticks",
                            side_effect=({"cpu0": 1}, {"cpu0": 2}),
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_meminfo",
                            return_value={
                                "MemTotal": 1_000_000,
                                "MemAvailable": 400_000,
                                "SwapTotal": 100_000,
                                "SwapFree": 80_000,
                            },
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_tcp_connection_counts",
                            side_effect=lambda _port: {"total": 3, "established": 2},
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_tcp_socket_states",
                            return_value={"established": 2, "listen": 1},
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_tcp_listen_counters",
                            return_value={"ListenOverflows": 0, "ListenDrops": 0},
                        ),
                        patch.object(
                            platform_production_qa,
                            "read_conntrack_utilization",
                            return_value={"available": True, "current": 2, "max": 10, "percent": 20.0},
                        ),
                        patch.object(
                            platform_production_qa,
                            "sample_postgres_waits",
                            new=AsyncMock(return_value={"lock_waiters": 0}),
                        ),
                        patch.object(
                            SystemSampler,
                            "celery_backlog",
                            new=AsyncMock(return_value={"deadlock-platform-default": 0}),
                        ),
                    ):
                        asyncio.run(sample_twice_async())
                    return sampler.samples

                full_sampler_samples = sample_twice(collect_all_process_resources=True)
                scoped_sampler_samples = sample_twice(collect_all_process_resources=False)

            self.assertEqual(len(full), process_count)
            self.assertEqual(len(scoped), process_count)
            self.assertEqual(
                platform_production_qa.process_group_snapshot(full),
                platform_production_qa.process_group_snapshot(scoped),
            )
            self.assertEqual(
                platform_production_qa.process_cpu_total(
                    full, platform_production_qa.is_postgres_process
                ),
                platform_production_qa.process_cpu_total(
                    scoped, platform_production_qa.is_postgres_process
                ),
            )
            self.assertEqual(
                platform_production_qa.gunicorn_counts(full),
                platform_production_qa.gunicorn_counts(scoped),
            )
            self.assertEqual(len(full_sampler_samples), 2)
            self.assertEqual(len(scoped_sampler_samples), 2)
            for full_sample, scoped_sample in zip(
                full_sampler_samples, scoped_sampler_samples, strict=True
            ):
                self.assertEqual(
                    {key: value for key, value in full_sample.items() if key != "timestamp"},
                    {key: value for key, value in scoped_sample.items() if key != "timestamp"},
                )
            api_lifecycle = scoped_sampler_samples[1]["process_lifecycle"]["deadlock-api"]
            self.assertEqual(api_lifecycle["new_processes"], [2000])
            self.assertEqual(api_lifecycle["missing_processes"], [1000])
            self.assertEqual(api_lifecycle["new_process_starts"][0]["pid"], 2000)
            for process in scoped:
                self.assertEqual(
                    set(process),
                    {
                        "pid",
                        "ppid",
                        "comm",
                        "cmdline",
                        "utime",
                        "stime",
                        "start_time_ticks",
                        "start_time",
                        "rss_bytes",
                        "read_bytes",
                        "write_bytes",
                    },
                )
                if platform_production_qa.process_label(process) is None:
                    self.assertEqual(
                        (process["rss_bytes"], process["read_bytes"], process["write_bytes"]),
                        (0, 0, 0),
                    )

    def test_burst_offsets_are_even_and_do_not_exceed_window(self) -> None:
        offsets = burst_offsets(count=5, spread_seconds=10)

        self.assertEqual(offsets, [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(burst_offsets(count=0, spread_seconds=10), [])
        self.assertEqual(burst_offsets(count=2, spread_seconds=0), [0.0, 0.0])

    def test_compact_mutation_response_rejects_nested_payloads(self) -> None:
        self.assertTrue(
            is_compact_mutation_response(
                {"id": "participant", "status": "registered", "changed": True},
                max_fields=3,
            )
        )
        self.assertFalse(
            is_compact_mutation_response(
                {"id": "participant", "workspace": {"participants": []}},
                max_fields=3,
            )
        )

    def test_http_summary_can_isolate_one_burst_phase(self) -> None:
        recorder = HttpMetricsRecorder()
        recorder.record(
            phase="write_join_burst_10s",
            method="POST",
            path="/tournaments/{slug}/join",
            status_code=201,
            elapsed_seconds=0.1,
            ok=True,
            started_at=1.0,
            finished_at=1.1,
            response_bytes=200,
        )
        recorder.record(
            phase="write_join_burst_30s",
            method="POST",
            path="/tournaments/{slug}/join",
            status_code=201,
            elapsed_seconds=0.2,
            ok=True,
            started_at=2.0,
            finished_at=2.2,
            response_bytes=220,
        )

        summary = recorder.summary(phases={"write_join_burst_10s"})

        self.assertEqual(summary["scope"], "full_population")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["overall"]["p95_ms"], 100.0)
        self.assertEqual(summary["overall"]["response_bytes"]["max_bytes"], 200)

    def test_follow_up_counts_only_requested_phases(self) -> None:
        samples = [
            HttpSample(
                phase="write_ready_burst_5s_followup",
                method="GET",
                path="/tournaments/{slug}/deadlock/ready-check",
                status_code=200,
                elapsed_ms=10,
                ok=True,
                started_at=1,
                finished_at=1.01,
                response_bytes=100,
            ),
            HttpSample(
                phase="write_ready_setup",
                method="GET",
                path="/tournaments/{slug}",
                status_code=200,
                elapsed_ms=10,
                ok=True,
                started_at=2,
                finished_at=2.01,
                response_bytes=100,
            ),
        ]

        counts = follow_up_read_counts(
            samples,
            phase_prefix="write_",
            phase_token="_followup",
        )

        self.assertEqual(
            counts,
            {"GET ready_check_state": 1},
        )

    def test_request_perf_summary_includes_method_and_response_bytes(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=POST path=/api/v1/tournaments/demo/join "
                "route=/api/v1/tournaments/{slug}/join status=201 total_ms=25.00 "
                "request_ms=25.00 db_sql_ms=8.00 sql_ms=8.00 sql_count=3 max_sql_ms=5.00 compute_ms=0.00 "
                "compute_blocks=0 response_bytes=320"
                " qa_phase=write_join_burst_10s"
            ],
            tournament_slug="demo",
        )

        route = summary["by_method_route"]["POST tournament_participants"]
        self.assertEqual(route["avg_sql_queries_per_request"], 3.0)
        self.assertEqual(route["response_bytes"]["max_bytes"], 320)
        self.assertEqual(
            summary["by_qa_phase"]["write_burst"]["requests"],
            1,
        )

    def test_request_perf_sampling_summary_preserves_legacy_rows_and_scope(
        self,
    ) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf method=GET path=/api/v1/users/me status=200 "
                "request_ms=10.00 pool_checkout_wait_ms=1.00 "
                "request_perf_selection=interval "
                "request_perf_completion_count=16 request_perf_sample_interval=16",
                "request_perf method=GET path=/api/v1/users/me status=200 "
                "request_ms=20.00 pool_checkout_wait_ms=2.00 "
                "request_perf_selection=trigger_and_interval "
                "request_perf_completion_count=32 request_perf_sample_interval=16",
                # Pre-sampler rows remain parseable and contribute their real
                # measurements without being assigned synthetic selection data.
                "request_perf method=GET path=/api/v1/users/me status=200 "
                "request_ms=30.00 pool_checkout_wait_ms=3.00",
            ],
            tournament_slug=None,
        )

        self.assertEqual(summary["scope"]["kind"], "diagnostic_sample")
        self.assertEqual(summary["scope"]["full_population_source"], "http_client")
        self.assertEqual(summary["logged_requests"], 3)
        self.assertEqual(summary["pool_checkout_wait_ms"]["count"], 3)
        self.assertEqual(
            summary["request_perf_sampling"],
            {
                "selection_reason_counts": {
                    "interval": 1,
                    "trigger_and_interval": 1,
                },
                "annotated_rows": 2,
                "interval_selected_rows": 2,
                "sample_interval": 16,
            },
        )

    def test_request_perf_summary_exposes_ready_vote_spans(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=POST path=/api/v1/tournaments/demo/deadlock/ready-check/vote "
                "route=/{slug}/deadlock/ready-check/vote status=200 total_ms=40.00 sql_ms=20.00 "
                "sql_count=3 max_sql_ms=10.00 compute_ms=2.00 compute_blocks=1 response_bytes=180 "
                "pool_wait_ms=3.00 qa_phase=write_ready_burst_5s "
                "ready_vote_auth_ms=4.00 ready_vote_checkout_count=1 ready_vote_checkout_ms=5.00 "
                "ready_vote_admission_inflight=3 ready_vote_admission_limit=4 "
                "ready_vote_admission_wait_ms=1.00 ready_vote_admitted_total=12 "
                "ready_vote_shed_total=2 ready_vote_controller_limit_changes=3 "
                "ready_vote_controller_state=pressure "
                "ready_vote_cpu_pressure=81.50 ready_vote_pool_wait_ms=3.00 "
                "ready_vote_cpu_monitor_sample_ms=0.12 ready_vote_cpu_monitor_samples=20 "
                "ready_vote_preflight_ms=6.00 ready_vote_upsert_ms=7.00 "
                "ready_vote_commit_ms=2.00 ready_vote_response_ms=0.10",
            ],
            tournament_slug="demo",
        )

        ready_vote = summary["by_route"]["ready_vote"]["ready_vote"]
        self.assertEqual(summary["scope"]["kind"], "diagnostic_sample")
        self.assertEqual(ready_vote["ready_vote_auth_ms"]["avg_ms"], 4.0)
        self.assertEqual(ready_vote["ready_vote_checkout_count"]["avg_ms"], 1.0)
        self.assertEqual(ready_vote["ready_vote_checkout_ms"]["avg_ms"], 5.0)
        self.assertEqual(ready_vote["ready_vote_admission_limit"]["avg_ms"], 4.0)
        self.assertEqual(ready_vote["ready_vote_cpu_pressure"]["avg_ms"], 81.5)
        self.assertEqual(ready_vote["ready_vote_cpu_monitor_samples"]["avg_ms"], 20.0)
        self.assertEqual(
            summary["by_route"]["ready_vote"]
            ["ready_vote_controller_state_counts"],
            {"pressure": 1},
        )
        self.assertEqual(ready_vote["ready_vote_commit_ms"]["p95_ms"], 2.0)
        self.assertNotIn("ready_vote_counter_ms", ready_vote)
        self.assertNotIn("ready_vote_db_checkout_ms", ready_vote)

    def test_external_http_summary_marks_full_population(self) -> None:
        recorder = HttpMetricsRecorder()
        self.assertEqual(recorder.summary()["scope"], "full_population")

    def test_nested_write_burst_server_reports_require_diagnostic_scope(self) -> None:
        server_by_phase = {
            "write_ready_burst_5s": {"requests": 2, "overall": {"count": 2}},
        }
        write_burst = {
            "profiles": [{"phase": "write_ready_burst_5s"}],
        }
        _attach_server_diagnostic_sample(write_burst, server_by_phase)

        self.assertEqual(write_burst["server_by_phase"]["scope"], "diagnostic_sample")
        self.assertEqual(write_burst["server_by_phase"]["by_phase"], server_by_phase)
        self.assertEqual(write_burst["profiles"][0]["server"]["scope"], "diagnostic_sample")
        self.assertEqual(write_burst["profiles"][0]["server"]["summary"], server_by_phase["write_ready_burst_5s"])

    def test_request_perf_summary_keeps_workspace_pressure_in_route_breakdown(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=GET path=/api/v1/tournaments/demo/workspace "
                "route=/tournaments/{slug}/workspace status=200 total_ms=125.00 sql_ms=80.00 "
                "sql_count=6 max_sql_ms=30.00 compute_ms=5.00 compute_blocks=1 "
                "workspace_auth_ms=3.00 workspace_tournament_base_ms=20.00 "
                "workspace_media_ms=4.00 workspace_access_ms=8.00 "
                "workspace_invite_ms=5.00 workspace_bracket_ms=10.00 "
                "workspace_ready_check_ms=12.00 workspace_serialization_ms=2.00 "
                "workspace_etag_ms=0.10 response_bytes=640 qa_phase=- pool_wait_ms=12.00",
                "request_perf request_id=two method=GET path=/api/v1/tournaments/demo/workspace "
                "route=/tournaments/{slug}/workspace status=200 total_ms=250.00 sql_ms=160.00 "
                "sql_count=6 max_sql_ms=40.00 compute_ms=8.00 compute_blocks=1 "
                "workspace_auth_ms=4.00 workspace_tournament_base_ms=30.00 "
                "workspace_media_ms=5.00 workspace_access_ms=9.00 "
                "workspace_invite_ms=6.00 workspace_bracket_ms=11.00 "
                "workspace_ready_check_ms=13.00 workspace_serialization_ms=3.00 "
                "workspace_etag_ms=0.20 response_bytes=640 qa_phase=- pool_wait_ms=20.00",
            ],
            tournament_slug=None,
        )

        workspace = summary["by_method_route"]["GET tournament_workspace"]
        self.assertEqual(workspace["requests"], 2)
        self.assertEqual(workspace["total"]["p95_ms"], 243.75)
        self.assertEqual(workspace["avg_sql_queries_per_request"], 6.0)
        self.assertEqual(workspace["pool_checkout_wait_ms"]["p99_ms"], 19.92)
        self.assertEqual(workspace["workspace"]["workspace_auth_ms"]["avg_ms"], 3.5)
        self.assertEqual(workspace["workspace"]["workspace_bracket_ms"]["p95_ms"], 10.95)

    def test_request_perf_summary_exposes_connection_hold_and_read_admission(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=GET path=/api/v1/users/me "
                "route=/users/me status=200 total_ms=900.00 request_ms=900.00 "
                "sql_ms=229.00 db_sql_ms=229.00 sql_count=5 max_sql_ms=100.00 "
                "compute_ms=10.00 compute_blocks=1 response_bytes=420 "
                "pool_checkout_wait_ms=100.00 pool_connection_hold_ms=900.00 "
                "pool_connection_hold_count=1 authenticated_read_admission_wait_ms=2.00 "
                "qa_phase=scale_external_read_mix_c64 pool_wait_ms=100.00"
            ],
            tournament_slug=None,
        )

        route = summary["by_route"]["users_me"]
        self.assertEqual(summary["pool_connection_hold_ms"]["avg_ms"], 900.0)
        self.assertEqual(route["pool_connection_hold_ms"]["avg_ms"], 900.0)
        self.assertEqual(summary["authenticated_read_admission_wait_ms"]["avg_ms"], 2.0)

    def test_request_perf_summary_exposes_auth_bootstrap_breakdown(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=GET path=/api/v1/auth/bootstrap "
                "route=/auth/bootstrap status=200 total_ms=900.00 request_ms=900.00 "
                "sql_ms=229.00 db_sql_ms=229.00 sql_count=2 max_sql_ms=130.00 "
                "compute_ms=10.00 compute_blocks=1 response_bytes=368 "
                "pool_checkout_wait_ms=100.00 pool_connection_hold_ms=600.00 "
                "auth_bootstrap_auth_query_ms=120.00 "
                "auth_bootstrap_avatar_query_ms=90.00 "
                "auth_bootstrap_response_build_ms=5.00",
            ],
            tournament_slug=None,
        )

        route = summary["by_route"]["auth_bootstrap"]
        self.assertEqual(
            route["auth_bootstrap"]["auth_bootstrap_auth_query_ms"]["avg_ms"],
            120.0,
        )
        self.assertEqual(
            route["auth_bootstrap"]["auth_bootstrap_avatar_query_ms"]["p95_ms"],
            90.0,
        )
        self.assertEqual(route["non_sql_after_pool_time"]["avg_ms"], 561.0)
        self.assertEqual(route["connection_after_sql_ms"]["avg_ms"], 371.0)
        self.assertEqual(summary["auth_bootstrap"]["auth_bootstrap_response_build_ms"]["avg_ms"], 5.0)

    def test_write_burst_acceptance_separates_target_budget(self) -> None:
        acceptance = evaluate_write_burst_profiles(
            [
                {
                    "name": "mixed",
                    "http": {"overall": {"p95_ms": 105, "p99_ms": 175}},
                    "system": {
                        "samples": 10,
                        "cpu_per_core": {
                            "cpu0": {"avg_percent": 32},
                            "cpu1": {"avg_percent": 29},
                        },
                        "postgres_waits": {
                            "max_lock_waiters": 0,
                            "max_lock_waiting_query_ms": 0,
                        },
                    },
                }
            ]
        )

        self.assertTrue(acceptance["healthy"])
        self.assertEqual(acceptance["failures"], [])

    def test_transient_cpu_peak_is_not_sustained_saturation(self) -> None:
        summary = summarize_bottleneck_evidence(
            http_summary={"by_phase": {}, "by_route": {}},
            server_summary={"by_route": {}},
            system_summary={
                "cpu_per_core": {
                    "cpu0": {"avg_percent": 25, "max_percent": 100},
                    "cpu1": {"avg_percent": 24, "max_percent": 100},
                },
                "load_average_1m": {"max": 1.5},
                "postgres_backend_connections": {"max": 40},
                "postgres_waits": {
                    "max_lock_waiters": 1,
                    "max_ungranted_locks": 0,
                    "max_lock_waiting_query_ms": 60,
                    "max_waiting_backends": 0,
                    "max_waiting_query_ms": 0,
                },
                "processes": {},
            },
        )

        self.assertFalse(summary["resource_flags"]["cpu_sustained_saturation"])
        self.assertTrue(summary["resource_flags"]["cpu_peak_saturation"])
        self.assertTrue(summary["resource_flags"]["postgres_lock_wait_observed"])
        self.assertFalse(summary["resource_flags"]["postgres_lock_contention"])
        self.assertNotIn(
            "cpu_or_python_serialization_saturation",
            summary["likely_bottleneck_classes"],
        )


if __name__ == "__main__":
    unittest.main()
