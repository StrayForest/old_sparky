from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from tools.platform_load import (
    LoadProfileError,
    get_profile,
    load_profiles,
    profile_digest,
    run_profile,
    validate_profile,
)
from tools.platform_verify import (
    CI_GATE_IDS,
    DETERMINISTIC_GATE_IDS,
    GATES_BY_ID,
    VerificationError,
    dispatch,
    registry_payload,
)
from tools.platform_verify_contract import collect_issues, extract_gate_invocations


class PlatformVerificationContractTests(unittest.TestCase):
    def test_registry_exposes_deterministic_and_workflow_only_contours(self) -> None:
        self.assertEqual(set(CI_GATE_IDS), set(DETERMINISTIC_GATE_IDS))
        self.assertIn("backend", CI_GATE_IDS)
        self.assertIn("verification-contract", CI_GATE_IDS)
        self.assertFalse(GATES_BY_ID["external-load"].deterministic)
        self.assertFalse(GATES_BY_ID["external-load"].local_safe)
        self.assertEqual(registry_payload()["ci_gate_ids"], list(CI_GATE_IDS))

    def test_production_contours_are_not_dispatchable_as_local_gates(self) -> None:
        with self.assertRaises(VerificationError):
            dispatch("external-load")

    def test_backend_and_unknown_workflow_gate_extraction(self) -> None:
        text = """
          run: python3 platform/tools/platform_verify.py backend
          run: python3 platform/tools/platform_verify.py no-such-gate
        """
        self.assertEqual(
            extract_gate_invocations(text),
            ["backend", "no-such-gate"],
        )

    def test_contract_self_test_is_clean(self) -> None:
        self.assertEqual(collect_issues(), [])

    def test_load_profiles_are_unique_and_have_stable_digests(self) -> None:
        profiles = load_profiles()
        self.assertEqual(
            set(profiles),
            {
                "ready-vote-slo-v2",
                "ready-vote-capacity-ramp-v2",
                "ready-vote-saturation-ramp-v1",
                "ready-vote-saturation-ramp-v2",
                "ready-vote-saturation-ramp-v3",
                "ready-vote-saturation-ramp-v4",
                "ready-vote-stress-15k-v2",
                "ready-vote-stress-20k-v2",
                "ready-vote-spike-v1",
                "read-mix-human-v2",
                "read-mix-stress-v2",
                "tournament-lifecycle-capacity-v1",
                "tournament-lifecycle-scale-v1",
                "tournament-lifecycle-slo-v1",
            },
        )
        profile = get_profile("ready-vote-slo-v2")
        self.assertEqual(profile_digest(profile), profile_digest(profile))
        self.assertEqual(profile["execution"]["generator"], "GitHub-hosted external runner")
        self.assertEqual(profile["acceptance"]["kind"], "slo")
        self.assertEqual(
            profile["acceptance"]["accepted_request_latency"],
            {"p50_ms": 250, "p90_ms": 400, "p95_ms": 600, "p99_ms": 1000},
        )

    def test_stress_and_capacity_profiles_have_distinct_semantics(self) -> None:
        stress = get_profile("ready-vote-stress-15k-v2")
        capacity = get_profile("ready-vote-capacity-ramp-v2")
        self.assertEqual(stress["acceptance"]["kind"], "stress")
        self.assertNotIn("logical_final_failure_percent", stress["acceptance"])
        self.assertEqual(capacity["acceptance"]["kind"], "capacity")
        self.assertEqual(len(capacity["traffic"]["phases"]), 7)
        self.assertEqual(
            capacity["acceptance"]["capacity"]["target_logical_actions_per_second"],
            [20, 30, 40, 50, 60, 70, 80],
        )
        saturation = get_profile("ready-vote-saturation-ramp-v1")
        self.assertEqual(saturation["acceptance"]["kind"], "stress")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation["traffic"]["phases"]],
            [80, 90, 100, 110, 120],
        )
        saturation_v2 = get_profile("ready-vote-saturation-ramp-v2")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v2["traffic"]["phases"]],
            [120, 135, 150, 165],
        )
        saturation_v3 = get_profile("ready-vote-saturation-ramp-v3")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v3["traffic"]["phases"]],
            [105, 110, 115, 120],
        )
        saturation_v4 = get_profile("ready-vote-saturation-ramp-v4")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v4["traffic"]["phases"]],
            [120, 125, 130, 135],
        )

    def test_tournament_lifecycle_profiles_use_the_local_qa_harness(self) -> None:
        for profile_id in (
            "tournament-lifecycle-slo-v1",
            "tournament-lifecycle-scale-v1",
            "tournament-lifecycle-capacity-v1",
        ):
            profile = get_profile(profile_id)
            self.assertEqual(profile["mode"], "tournament-lifecycle")
            self.assertEqual(profile["fixture"]["tournament_count"], 20)
            self.assertEqual(profile["fixture"]["users_per_tournament"], 500)
            self.assertEqual(profile["execution"]["generator"], "platform_production_qa.py")
            self.assertTrue(profile["execution"]["external_runner_forbidden"])

        with self.assertRaisesRegex(LoadProfileError, "external runner"):
            run_profile(
                get_profile("tournament-lifecycle-slo-v1"),
                Path("/tmp/unused-lifecycle-manifest.json"),
                Path("/tmp/unused-lifecycle-report.json"),
            )

    def test_load_profile_rejects_missing_cleanup_contract(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        invalid = dict(profile)
        invalid["correctness"] = dict(profile["correctness"])
        invalid["correctness"]["cleanup_required"] = False
        with self.assertRaises(LoadProfileError):
            validate_profile(invalid)

    def test_profile_dispatcher_records_contract_and_source_sha(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        fake_report = {
            "schema": 1,
            "mode": "ready-vote",
            "acceptance": {"passed": True},
            "raw_http": {"requests": 2},
            "logical": {"actions": 1},
            "phases": {"primary": {"logical": {"actions": 1}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            with (
                patch(
                    "tools.platform_external_load.load_manifest",
                    return_value=({}, [object()]),
                ),
                patch(
                    "tools.platform_external_load.run_load",
                    return_value=fake_report,
                ),
                patch(
                    "tools.platform_load._source_git_sha",
                    return_value="a" * 40,
                ),
            ):
                self.assertEqual(
                    run_profile(profile, Path(directory) / "manifest.json", report_path),
                    0,
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["source_git_sha"], "a" * 40)
        self.assertEqual(report["load_contract"]["profile_id"], "ready-vote-slo-v2")
        self.assertEqual(report["load_contract"]["http_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
