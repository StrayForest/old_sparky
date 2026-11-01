from __future__ import annotations

import cProfile
from pathlib import Path
import pstats
import sys
import time
import unittest
from unittest.mock import patch


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT / "python_packages"))

from platform_infra import performance_diagnostic as api_diagnostic
from tools import platform_cpu_diagnostic_pair as cpu_pair
from tools import platform_cpu_diagnostic_plan as cpu_plan
from tools import platform_ssr_capacity_diagnostic as ssr_diagnostic


class ApiCpuProfileConservationTests(unittest.TestCase):
    def _profile(
        self, names: list[str], *, unclassified: bool = False,
        disable: bool = True,
    ) -> cProfile.Profile:
        source_root = Path(api_diagnostic.__file__).resolve().parents[2]
        namespace: dict[str, object] = {}
        call_names = list(names)
        for name in call_names:
            filename = source_root / "apps/platform_api" / f"{name}.py"
            body = (
                f"def {name}():\n"
                "    value = 0\n"
                "    index = 0\n"
                "    while index < 5000:\n"
                "        value += index\n"
                "        index += 1\n"
                "    return value\n"
            )
            exec(compile(body, str(filename), "exec"), namespace)
        if unclassified:
            body = (
                "def unclassified_work():\n"
                "    value = 0\n"
                "    index = 0\n"
                "    while index < 5000:\n"
                "        value += index\n"
                "        index += 1\n"
                "    return value\n"
            )
            exec(compile(body, "/tmp/unclassified_cpu_profile.py", "exec"), namespace)
            call_names.append("unclassified_work")

        runner_name = next(name for name in sorted(api_diagnostic.API_FUNCTION_ALLOWLIST) if name not in names)
        calls = "".join(f"    {name}()\n" for name in call_names)
        runner = (
            f"def {runner_name}(profile):\n"
            "    profile.enable()\n"
            f"{calls}"
            + ("    profile.disable()\n" if disable else "")
        )
        exec(compile(
            runner,
            str(source_root / "apps/platform_api" / f"{runner_name}.py"),
            "exec",
        ), namespace)
        profile = cProfile.Profile(timer=time.thread_time_ns, timeunit=1e-9)
        namespace[runner_name](profile)
        return profile

    def _producer_message(self, names: list[str]) -> str:
        profile = self._profile(names, disable=False)
        plan = api_diagnostic.DiagnosticPlan(
            run_id="b" * 32,
            source_sha="a" * 40,
            release_slug="gha-profile-conservation-test",
            workload=api_diagnostic.WORKLOAD_HTML_SSR_CPU_PROFILE_20S,
            off_start_ms=1_000_000,
            off_end_ms=1_020_000,
            on_start_ms=1_025_000,
            on_end_ms=1_045_000,
            expires_at_ms=1_060_000,
            targets=(),
        )
        diagnostic = api_diagnostic.ApiCpuDiagnostic(plan)
        diagnostic.profile = profile
        diagnostic.started_monotonic = 10.0
        diagnostic.start_lag_ms = 0
        emitted: list[str] = []
        previous_active = api_diagnostic._active_profile
        api_diagnostic._active_profile = diagnostic
        try:
            with (
                patch.object(api_diagnostic, "_read_process_plan_uncached", return_value=plan),
                patch.object(
                    api_diagnostic, "_emit_cpu_diagnostic_message",
                    side_effect=lambda message: emitted.append(message) or True,
                ),
                patch.object(api_diagnostic.time, "time_ns", return_value=1_045_050_000_000),
                patch.object(api_diagnostic.time, "monotonic", return_value=30.0),
            ):
                diagnostic.stop()
        finally:
            api_diagnostic._active_profile = previous_active
        self.assertEqual(len(emitted), 1)
        return emitted[0]

    def _legacy_top_ten(self, profile: cProfile.Profile) -> tuple[list[tuple[str, int, int]], int, int]:
        """Reproduce the pre-fix projection for a counterfactual assertion."""

        stats = pstats.Stats(profile).stats
        totals: dict[str, list[int]] = {}
        total_self_us = 0
        platform_root = str(Path(api_diagnostic.__file__).resolve().parents[2])
        for (filename, _line, function), values in stats.items():
            self_us = int(max(0.0, float(values[2])) * 1_000_000)
            total_self_us += self_us
            normalized = filename.replace("\\", "/")
            label = "other"
            if normalized.startswith(platform_root + "/"):
                relative = normalized[len(platform_root) + 1:]
                if function in api_diagnostic.API_FUNCTION_ALLOWLIST and relative.startswith((
                    "apps/platform_api/", "python_packages/platform_infra/",
                )):
                    label = f"repo.{function}"
            else:
                for prefix, category in api_diagnostic.RUNTIME_PREFIX_CATEGORIES:
                    if prefix in normalized:
                        label = category
                        break
            entry = totals.setdefault(label, [0, 0])
            entry[0] += self_us
            entry[1] += int(values[1])
        ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
        return [(name, values[0], values[1]) for name, values in ordered[:10]], total_self_us, len(ordered)

    def _assert_cpu_ranked(self, rows: list[tuple[str, int, int]]) -> None:
        self.assertEqual(rows, sorted(rows, key=lambda row: (-row[1], row[0])))

    def _journal_summary(self, api_messages: list[str]) -> dict[str, object]:
        source_sha = "a" * 40
        release_slug = "gha-profile-conservation-test"
        run_id = "b" * 32
        workload = cpu_plan.HTML_CPU_PROFILE_20S_WORKLOAD
        plan_fields = {
            "run_id": run_id,
            "source_sha": source_sha,
            "release_slug": release_slug,
            "workload": workload,
            "off_start_ms": 1_000_000,
            "off_end_ms": 1_020_000,
            "on_start_ms": 1_025_000,
            "on_end_ms": 1_045_000,
        }
        plans: dict[str, dict[str, object]] = {}
        targets_by_service: dict[str, list[dict[str, object]]] = {}
        for service, pids in (("api", (101, 102)), ("web", (103,))):
            targets = [
                {"pid": pid, "start_ticks": pid + 1000, "invocation_id": "c" * 32}
                for pid in pids
            ]
            targets_by_service[service] = targets
            plans[service] = {**plan_fields, "targets": targets}

        records: list[dict[str, str]] = []

        def add_record(service: str, pid: int, timestamp_ms: int, message: str) -> None:
            records.append({
                "MESSAGE": message,
                "_PID": str(pid),
                "_SYSTEMD_INVOCATION_ID": "c" * 32,
                "_SYSTEMD_UNIT": f"deadlock-{service}.service",
                "_SYSTEMD_CGROUP": f"/system.slice/deadlock-{service}.service",
                "__REALTIME_TIMESTAMP": str(timestamp_ms * 1000),
            })

        for service, pids in (("api", (101, 102)), ("web", (103,))):
            for pid in pids:
                for phase, timestamp_ms in (("off", 1_010_000), ("on", 1_035_000)):
                    add_record(
                        service, pid, timestamp_ms,
                        f"cpu_diagnostic_usage service={service} run_id={run_id} phase={phase} "
                        "window_ms=20000 cpu_ns=5000000000 cpu_capacity_cpus=unknown "
                        "start_lag_ms=0 end_lag_ms=0 timing_complete=true",
                    )

        for pid, message in zip((101, 102), api_messages):
            add_record("api", pid, 1_044_000, message)
        web_message = (
            f"cpu_diagnostic_complete service=web run_id={run_id} timer=v8_cpu "
            "start_lag_ms=0 elapsed_ms=20000 sample_interval_us=100000 sample_count=20 "
            "categories=other:1000000:10,web_framework:1000000:10"
        )
        add_record("web", 103, 1_044_000, web_message)

        ticks = {
            int(target["pid"]): int(target["start_ticks"])
            for targets in targets_by_service.values() for target in targets
        }

        with (
            patch.object(cpu_plan, "_release_identity", return_value=(source_sha, release_slug)),
            patch.object(cpu_plan, "_systemd_unit", side_effect=lambda service, **_kw: {
                "InvocationID": "c" * 32,
            }),
            patch.object(cpu_plan, "_service_targets", side_effect=lambda service, _unit: [
                {**target, "service": service} for target in targets_by_service[service]
            ]),
            patch.object(cpu_plan, "_process_start_ticks", side_effect=lambda pid: ticks[pid]),
            patch.object(cpu_plan, "_journal_rows", return_value=("complete", records)),
        ):
            usage = cpu_plan._usage_summary(plans)
        return {"status": "expired_plans_removed", "service_count": 2, **usage}

    def test_real_cprofile_overflow_is_folded_and_conserved(self) -> None:
        names = sorted(api_diagnostic.API_FUNCTION_ALLOWLIST)[:12]
        profile = self._profile(names)
        legacy_rows, legacy_total, raw_category_count = self._legacy_top_ten(profile)
        self.assertGreater(raw_category_count, 10)
        self.assertLess(sum(row[1] for row in legacy_rows), legacy_total)

        rows, total = api_diagnostic._profile_summary(profile)
        self.assertLessEqual(len(rows), 10)
        self._assert_cpu_ranked(rows)
        self.assertEqual(len({row[0] for row in rows}), len(rows))
        self.assertIn("other", {row[0] for row in rows})
        self.assertEqual(sum(row[1] for row in rows), total)

        stats = pstats.Stats(profile).stats
        self.assertEqual(sum(row[2] for row in rows), sum(int(value[1]) for value in stats.values()))

    def test_small_profile_keeps_all_categories_without_fabricating_other(self) -> None:
        profile = self._profile(sorted(api_diagnostic.API_FUNCTION_ALLOWLIST)[:4])
        legacy_rows, legacy_total, raw_category_count = self._legacy_top_ten(profile)
        rows, total = api_diagnostic._profile_summary(profile)
        self.assertGreater(total, 0)
        self.assertLessEqual(len(rows), 10)
        self.assertEqual(len(rows), raw_category_count)
        self.assertEqual(total, legacy_total)
        self.assertEqual(rows, legacy_rows)
        self.assertEqual(sum(row[1] for row in rows), total)
        stats = pstats.Stats(profile).stats
        self.assertEqual(sum(row[2] for row in rows), sum(int(value[1]) for value in stats.values()))

    def test_overflow_without_existing_other_creates_conserving_other(self) -> None:
        totals = {
            f"repo.synthetic_{index:02d}": [1000 + index, 10 + index]
            for index in range(12)
        }
        projected = api_diagnostic._bounded_profile_categories(totals)
        self.assertEqual(len(projected), 10)
        self._assert_cpu_ranked(projected)
        self.assertIn("other", {row[0] for row in projected})
        self.assertEqual(sum(row[1] for row in projected), sum(row[0] for row in totals.values()))
        self.assertEqual(sum(row[2] for row in projected), sum(row[1] for row in totals.values()))

    def test_existing_other_and_overflow_conserve_both_totals(self) -> None:
        profile = self._profile(sorted(api_diagnostic.API_FUNCTION_ALLOWLIST)[:12], unclassified=True)
        rows, total = api_diagnostic._profile_summary(profile)
        self.assertLessEqual(len(rows), 10)
        self._assert_cpu_ranked(rows)
        self.assertIn("other", {row[0] for row in rows})
        self.assertEqual(sum(row[1] for row in rows), total)
        stats = pstats.Stats(profile).stats
        self.assertEqual(sum(row[2] for row in rows), sum(int(value[1]) for value in stats.values()))

    def test_producer_collector_pair_validator_and_ssr_projection_roundtrip(self) -> None:
        generated = [
            self._producer_message(sorted(api_diagnostic.API_FUNCTION_ALLOWLIST)[:12])
            for _ in range(2)
        ]
        summary = self._journal_summary(generated)
        collected = cpu_pair._validate_usage_summary(
            summary,
            expected_workload=cpu_plan.HTML_CPU_PROFILE_20S_WORKLOAD,
            allow_projected_memory_rows=True,
            allow_noop_cleanup=True,
        )
        self.assertIsNotNone(collected)
        self.assertEqual(collected["profile_status"], "complete")
        self.assertEqual(collected["profile_reason"], "none")
        self.assertEqual(sum(row["cpu_us"] for row in collected["profile_rows"][0]["categories"]),
                         collected["profile_rows"][0]["total_cpu_us"])
        self.assertEqual([row["category"] for row in collected["profile_rows"][0]["categories"]],
                         sorted(row["category"] for row in collected["profile_rows"][0]["categories"]))

        projected = ssr_diagnostic._project_server_telemetry(
            summary,
            include_cpu_profile=True,
            expected_workload=cpu_plan.HTML_CPU_PROFILE_20S_WORKLOAD,
        )
        self.assertEqual(projected["profile_status"], "complete")
        self.assertEqual(projected["profile_reason"], "none")
        self.assertEqual(len(projected["profile_rows"]), 2)
        self.assertEqual(projected["profile_rows"][0]["observed_targets"], 2)
        self.assertEqual(projected["profile_rows"][1]["observed_targets"], 1)

    def test_legacy_truncation_is_rejected_by_real_collector(self) -> None:
        old_messages = []
        for _ in range(2):
            legacy_rows, total, _raw_count = self._legacy_top_ten(
                self._profile(sorted(api_diagnostic.API_FUNCTION_ALLOWLIST)[:12])
            )
            encoded = ",".join(f"{name}:{cpu_us}:{calls}" for name, cpu_us, calls in legacy_rows)
            old_messages.append(
                f"cpu_diagnostic_complete service=api run_id={'b' * 32} timer=thread_cpu "
                f"start_lag_ms=0 elapsed_ms=20000 total_self_cpu_us={total} functions={encoded}"
            )
        summary = self._journal_summary(old_messages)
        self.assertEqual(summary["profile_status"], "incomplete")
        self.assertEqual(summary["profile_reason"], "invalid_profile")
        api_profile = summary["profile_rows"][0]
        self.assertEqual(api_profile["observed_targets"], 0)
        self.assertEqual(api_profile["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
