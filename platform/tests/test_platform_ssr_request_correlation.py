from __future__ import annotations

import json
import unittest

from tools.platform_ssr_request_correlation import (
    is_eligible_response_id,
    response_id_selection_digest,
    summarize_ssr_request_correlation,
)


RUN_ID = "0123456789abcdef0123456789abcdef"
PHASE = "on"
WINDOW_START = "2026-10-29T12:00:00+00:00"
WINDOW_END = "2026-10-29T12:03:00+00:00"
ID_ONE = "00000000000000000000000000000002"
ID_TWO = "00000000000000000000000000000005"
ID_THREE = "00000000000000000000000000000017"
INELIGIBLE_ID = "33333333333333333333333333333333"


def event(
    request_id: str,
    *,
    source_time: str = "2026-10-29T12:01:00Z",
    phase: str = PHASE,
    run_id: str = RUN_ID,
    **fields: object,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "phase": phase,
        "window_start_utc": WINDOW_START,
        "window_end_utc": WINDOW_END,
        "event_at_utc": source_time,
        "request_id": request_id,
        **fields,
    }


def summarize(**overrides: object) -> dict[str, object]:
    selected_ids = overrides.get("selected_request_ids", [ID_ONE, ID_TWO])
    values: dict[str, object] = {
        "selected_request_ids": selected_ids,
        "submitted_html_count": 7,
        "valid_response_id_count": 2,
        "eligible_response_id_count": len({
            value
            for value in selected_ids
            if isinstance(value, str) and is_eligible_response_id(RUN_ID, value)
        }),
        "selection_scheme": "sha256-runid-requestid-1of8-v1",
        "run_id": RUN_ID,
        "phase": PHASE,
        "window_start_utc": WINDOW_START,
        "window_end_utc": WINDOW_END,
        "nginx_rows": [
            event(ID_ONE, request_time_ms=100.0, cf_ray="ray-one"),
            event(ID_TWO, request_time_ms=200.0, cf_ray="ray-two"),
        ],
        "ssr_rows": [
            event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0),
            event(ID_ONE, stage="tournament_workspace", duration_ms=60.0),
            event(ID_ONE, stage="page_component", duration_ms=90.0),
            event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=80.0),
        ],
        "stream_rows": [],
        "api_rows": [
            event(
                ID_ONE,
                route_class="auth_bootstrap",
                total_ms=30.0,
                sql_ms=5.0,
                max_sql_ms=3.0,
                pool_checkout_wait_ms=1.0,
                pool_connection_hold_ms=4.0,
                compute_ms=10.0,
                sql_count=2,
            ),
            event(
                ID_ONE,
                route_class="tournament_workspace",
                total_ms=50.0,
                sql_ms=20.0,
                max_sql_ms=15.0,
                pool_checkout_wait_ms=5.0,
                pool_connection_hold_ms=10.0,
                compute_ms=20.0,
                sql_count=3,
            ),
            event(ID_TWO, route_class="auth_bootstrap", total_ms=70.0),
        ],
    }
    values.update(overrides)
    return summarize_ssr_request_correlation(**values)  # type: ignore[arg-type]


class SsrRequestCorrelationTests(unittest.TestCase):
    def test_one_html_parent_joins_all_api_calls_and_ssr_stages(self) -> None:
        report = summarize()

        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_ssr_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_rows"], 3)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_by_request_id"], 3)  # type: ignore[index]
        api_metrics = report["timing_ms"]["api_request_by_route"]  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["total_ms"]["count"], 2)  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["sql_ms"]["count"], 1)  # type: ignore[index]
        self.assertEqual(api_metrics["auth_bootstrap"]["sql_count"]["count"], 1)  # type: ignore[index]
        self.assertEqual(api_metrics["tournament_workspace"]["total_ms"]["count"], 1)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["api_metric_missing_or_invalid_by_route"]["auth_bootstrap"]["sql_ms"],
            1,  # type: ignore[index]
        )
        self.assertEqual(
            report["timing_ms"]["ssr_stage"]["auth_bootstrap_fetch"]["count"],
            # type: ignore[index]
            2,
        )
        self.assertEqual(
            report["timing_ms"]["ssr_stage"]["page_component"]["max_ms"],
            90.0,  # type: ignore[index]
        )
        serialized = json.dumps(report)
        self.assertNotIn(ID_ONE, serialized)
        self.assertNotIn(ID_TWO, serialized)

    def test_shared_selection_scheme_hash_vectors(self) -> None:
        self.assertEqual(
            response_id_selection_digest(RUN_ID, ID_ONE),
            "a078d044988363164c156d124496875ff00154849f1e9bd69494d0d35c1e5543",
        )
        self.assertTrue(is_eligible_response_id(RUN_ID, ID_ONE))
        self.assertEqual(
            response_id_selection_digest(RUN_ID, INELIGIBLE_ID),
            "afb26a2492c5078e5fbfe5479af31e66eac3813ddfa56b088e0d6a37eefee335",
        )
        self.assertFalse(is_eligible_response_id(RUN_ID, INELIGIBLE_ID))
        web_ineligible_id = "11111111111111111111111111111111"
        self.assertEqual(
            response_id_selection_digest(RUN_ID, web_ineligible_id),
            "3292b6e9f49cf13f86d5fa9119fb018d2de5d94882332e519636f1454bbe5380",
        )
        self.assertFalse(is_eligible_response_id(RUN_ID, web_ineligible_id))

    def test_stream_offsets_are_separate_from_ssr_durations(self) -> None:
        report = summarize(
            stream_rows=[
                event(
                    ID_ONE,
                    stage="first_body_write_attempt",
                    elapsed_ms=120.0,
                    headers="private-header-value",
                ),
                event(ID_ONE, stage="response_finish", elapsed_ms=240.0),
                event(ID_ONE, stage="response_close"),
                event(ID_ONE, stage="response_error", elapsed_ms=float("nan")),
            ]
        )

        ssr_stages = report["timing_ms"]["ssr_stage"]  # type: ignore[index]
        milestones = report["timing_ms"]["ssr_stream_milestone_elapsed_ms"]  # type: ignore[index]
        self.assertEqual(ssr_stages["page_component"]["max_ms"], 90.0)  # type: ignore[index]
        self.assertNotIn("first_body_write_attempt", ssr_stages)
        self.assertEqual(milestones["first_body_write_attempt"]["max_ms"], 120.0)  # type: ignore[index]
        self.assertEqual(milestones["response_finish"]["max_ms"], 240.0)  # type: ignore[index]
        self.assertEqual(milestones["response_close"]["count"], 0)  # type: ignore[index]
        self.assertIsNone(milestones["response_close"]["avg_ms"])  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["stream_milestone_missing_or_invalid"],
            {"response_close": 1, "response_error": 1},  # type: ignore[index]
        )
        serialized = json.dumps(report)
        self.assertNotIn("private-header-value", serialized)
        self.assertNotIn(ID_ONE, serialized)

    def test_distinct_repeated_attempts_remain_separate(self) -> None:
        report = summarize(
            submitted_html_count=2,
            valid_response_id_count=2,
            selected_request_ids=[ID_ONE, ID_TWO],
            nginx_rows=[
                event(ID_ONE, request_time_ms=100.0),
                event(ID_TWO, request_time_ms=100.0),
            ],
            ssr_rows=[
                event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0),
                event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=10.0),
            ],
            api_rows=[
                event(ID_ONE, route_class="auth_bootstrap", total_ms=5.0),
                event(ID_TWO, route_class="auth_bootstrap", total_ms=5.0),
            ],
        )

        self.assertEqual(report["cohort"]["submitted_html_requests"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_api_rows"], 2)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["api_request_by_route"]["auth_bootstrap"]["total_ms"]["count"],
            # type: ignore[index]
            2,
        )

    def test_late_completion_and_error_remain_in_submitted_denominator(self) -> None:
        late = "2026-10-29T12:02:59Z"
        report = summarize(
            submitted_html_count=10,
            valid_response_id_count=2,
            window_end_utc=WINDOW_END,
            nginx_rows=[
                event(ID_ONE, source_time=late, request_time_ms=800.0),
                event(ID_TWO, request_time_ms=200.0),
            ],
            ssr_rows=[
                event(
                    ID_ONE,
                    source_time=late,
                    stage="auth_bootstrap_fetch",
                    duration_ms=700.0,
                    outcome="error",
                )
            ],
            api_rows=[
                event(
                    ID_ONE,
                    source_time=late,
                    route_class="auth_bootstrap",
                    total_ms=600.0,
                    outcome="error",
                )
            ],
        )

        self.assertEqual(report["cohort"]["submitted_html_requests"], 10)  # type: ignore[index]
        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(
            report["timing_ms"]["nginx_request"]["max_ms"],
            800.0,  # type: ignore[index]
        )
        self.assertEqual(report["join"]["matched_api_rows"], 1)  # type: ignore[index]

    def test_orphans_duplicate_parent_and_duplicate_sample_ids_are_reported(self) -> None:
        orphan = "d" * 32
        report = summarize(
            selected_request_ids=[ID_ONE, ID_ONE, ID_TWO],
            nginx_rows=[
                event(ID_ONE, request_time_ms=10.0),
                event(ID_ONE, request_time_ms=11.0),
                event(ID_TWO, request_time_ms=12.0),
                event(orphan, request_time_ms=13.0),
            ],
            ssr_rows=[event(orphan, stage="auth_bootstrap_fetch", duration_ms=4.0)],
            api_rows=[event(orphan, route_class="auth_bootstrap", total_ms=3.0)],
        )

        self.assertEqual(
            report["cohort"]["selected_sample_duplicate_ids"],
            1,  # type: ignore[index]
        )
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]
        self.assertEqual(report["join"]["duplicate_nginx_request_ids"], 1)  # type: ignore[index]
        self.assertEqual(report["join"]["orphan_ssr_rows"], 1)  # type: ignore[index]
        self.assertEqual(report["join"]["orphan_api_rows"], 1)  # type: ignore[index]

    def test_cross_run_or_phase_rows_are_rejected(self) -> None:
        report = summarize(
            ssr_rows=[
                event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0, run_id="other-run"),
                event(ID_TWO, stage="auth_bootstrap_fetch", duration_ms=50.0, phase="off"),
            ],
        )

        self.assertEqual(report["join"]["matched_ssr_parents"], 0)  # type: ignore[index]
        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]
        self.assertEqual(
            report["coverage"]["source_rejections"]["ssr"]["context_mismatch"],
            # type: ignore[index]
            2,
        )

    def test_context_mismatch_cannot_replace_matching_parent_row(self) -> None:
        report = summarize(
            nginx_rows=[
                event(ID_ONE, run_id="other-run", request_time_ms=999.0),
                event(ID_ONE, request_time_ms=100.0),
                event(ID_TWO, request_time_ms=200.0),
            ],
        )

        self.assertEqual(report["join"]["matched_nginx_parents"], 2)  # type: ignore[index]
        self.assertEqual(report["timing_ms"]["nginx_request"]["max_ms"], 200.0)  # type: ignore[index]
        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]

    def test_missing_or_invalid_event_timestamps_fail_closed(self) -> None:
        report = summarize(
            nginx_rows=[
                {
                    key: value
                    for key, value in event(ID_ONE, request_time_ms=100.0).items()
                    if key != "event_at_utc"
                },
                event(ID_TWO, source_time="not-a-time", request_time_ms=200.0),
            ],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=40.0)],
            api_rows=[],
        )

        self.assertEqual(report["join"]["matched_nginx_parents"], 0)  # type: ignore[index]
        self.assertEqual(
            report["coverage"]["source_rejections"]["nginx"]["missing_or_invalid_timestamp"],
            2,  # type: ignore[index]
        )
        self.assertEqual(report["timing_ms"]["nginx_request"]["count"], 0)  # type: ignore[index]

    def test_declared_and_local_truncation_prevent_full_coverage_claim(self) -> None:
        declared = summarize(rows_truncated={"api": True})
        bounded = summarize(
            max_rows=1,
            nginx_rows=[event(ID_ONE, request_time_ms=10.0), event(ID_TWO, request_time_ms=20.0)],
        )

        self.assertFalse(declared["coverage"]["complete"])  # type: ignore[index]
        self.assertTrue(declared["coverage"]["source_rows_truncated"]["api"])  # type: ignore[index]
        self.assertFalse(bounded["coverage"]["complete"])  # type: ignore[index]
        self.assertTrue(
            bounded["coverage"]["source_rows_truncated"]["nginx"]  # type: ignore[index]
        )

    def test_malformed_ids_do_not_join_or_escape_report(self) -> None:
        secret = "request-secret-with-sensitive-value"
        report = summarize(
            selected_request_ids=[secret, ID_TWO],
            nginx_rows=[event(secret, request_time_ms=1.0), event(ID_TWO, request_time_ms=2.0)],
        )

        self.assertEqual(
            report["cohort"]["selected_sample_malformed_ids"],
            1,  # type: ignore[index]
        )
        self.assertEqual(report["join"]["matched_nginx_parents"], 1)  # type: ignore[index]
        self.assertNotIn(secret, json.dumps(report))

    def test_cf_ray_fallback_requires_explicit_exact_proof(self) -> None:
        parent = event(ID_ONE, cf_ray="ray-exact", request_time_ms=100.0)
        api = event("e" * 32, cf_ray="ray-exact", route_class="auth_bootstrap", total_ms=20.0)
        no_fallback = summarize(
            nginx_rows=[parent],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0)],
            api_rows=[api],
            valid_response_id_count=1,
            selected_request_ids=[ID_ONE],
        )
        proven_fallback = summarize(
            nginx_rows=[parent],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=10.0)],
            api_rows=[api],
            valid_response_id_count=1,
            selected_request_ids=[ID_ONE],
            cf_ray_fallback_proven=True,
        )

        self.assertEqual(no_fallback["join"]["matched_api_rows"], 0)  # type: ignore[index]
        self.assertEqual(proven_fallback["join"]["matched_api_rows"], 1)  # type: ignore[index]
        self.assertEqual(proven_fallback["join"]["matched_api_by_cf_ray"], 1)  # type: ignore[index]

    def test_component_metrics_reject_negative_nan_and_ignore_extra_fields(self) -> None:
        secret = "never-serialize-private-observer-content"
        report = summarize(
            selected_request_ids=[ID_ONE],
            submitted_html_count=1,
            valid_response_id_count=1,
            nginx_rows=[event(ID_ONE, request_time_ms=10.0)],
            ssr_rows=[event(ID_ONE, stage="auth_bootstrap_fetch", duration_ms=2.0)],
            api_rows=[
                event(
                    ID_ONE,
                    route_class="auth_bootstrap",
                    total_ms=-2.0,
                    sql_ms=float("nan"),
                    pool_checkout_wait_ms=-1.0,
                    sql_count=2,
                    arbitrary_private_field=secret,
                )
            ],
        )

        metrics = report["timing_ms"]["api_request_by_route"]["auth_bootstrap"]  # type: ignore[index]
        self.assertEqual(metrics["total_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["sql_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["pool_checkout_wait_ms"]["count"], 0)  # type: ignore[index]
        self.assertEqual(metrics["sql_count"]["count"], 1)  # type: ignore[index]
        missing = report["timing_ms"]["api_metric_missing_or_invalid_by_route"][
            "auth_bootstrap"
        ]  # type: ignore[index]
        self.assertEqual(missing["total_ms"], 1)  # type: ignore[index]
        self.assertEqual(missing["sql_ms"], 1)  # type: ignore[index]
        self.assertEqual(missing["pool_checkout_wait_ms"], 1)  # type: ignore[index]
        serialized = json.dumps(report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("nan", serialized)

    def test_duplicate_cohort_ids_are_excluded_as_ambiguous(self) -> None:
        report = summarize(
            submitted_html_count=2,
            valid_response_id_count=0,
            duplicate_response_id_count=2,
            selected_request_ids=[],
            nginx_rows=[],
            ssr_rows=[],
            api_rows=[],
        )

        self.assertEqual(report["cohort"]["valid_response_ids"], 0)  # type: ignore[index]
        self.assertEqual(report["cohort"]["duplicate_response_ids"], 2)  # type: ignore[index]
        self.assertFalse(report["coverage"]["full_cohort"])  # type: ignore[index]

    def test_caller_and_helper_bounds_are_visible_in_coverage(self) -> None:
        report = summarize(
            selected_request_ids=[ID_ONE, ID_TWO, ID_THREE],
            valid_response_id_count=3,
            sample_limit=2,
        )

        self.assertFalse(report["coverage"]["complete"])  # type: ignore[index]
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]
        self.assertEqual(report["cohort"]["selected_sample_received"], 2)  # type: ignore[index]

    def test_ineligible_sample_id_is_rejected(self) -> None:
        report = summarize(
            selected_request_ids=[ID_ONE, INELIGIBLE_ID],
            valid_response_id_count=2,
            eligible_response_id_count=2,
        )

        self.assertEqual(report["cohort"]["selected_sample_ineligible_ids"], 1)  # type: ignore[index]
        self.assertFalse(report["cohort"]["selected_sample_complete"])  # type: ignore[index]

    def test_selection_scheme_and_eligible_count_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            summarize(selection_scheme="unreviewed")
        with self.assertRaises(ValueError):
            summarize(valid_response_id_count=1, eligible_response_id_count=2)


if __name__ == "__main__":
    unittest.main()
