#!/usr/bin/env python3
"""AST-backed ownership catalog for the deterministic Python test contours.

The catalog deliberately owns test *IDs*, rather than importing the
application.  That keeps the ownership contract safe to run before a test
database or Redis is available and makes an accidental discovery omission
visible before a contour is executed.

``backend`` is the aggregate of the five local deterministic backend
contours.  The verification-contract tests remain an explicit external owner
because they validate the registry itself, not the application backend.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import threading
from typing import Callable, Iterable, Mapping


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = PLATFORM_ROOT / "tests"

_DISCOVERY_CACHE_LOCK = threading.Lock()
_DISCOVERY_CACHE: tuple[Path, bytes, tuple["TestCase", ...]] | None = None

BACKEND_CONTOURS: tuple[str, ...] = (
    "backend-unit",
    "backend-tool-contract",
    "backend-integration",
    "backend-privileged",
    "performance-contract",
)
BACKEND_AGGREGATE = "backend"
VERIFICATION_CONTOUR = "verification-contract"

# A small number of privileged contracts deliberately mutate real host-owned
# resources and are executable only on the designated GitHub-hosted runner.
# Keep this policy attached to exact catalog IDs so local dispatchers can fail
# before starting the rest of a long contour without changing CI membership.
TEST_ENVIRONMENT_REQUIREMENTS: Mapping[str, Mapping[str, str]] = {
    "tests.test_platform_release_audit_hardening.ReleaseHardeningContractTests.test_storage_maintenance_expected_service_exit_reaches_report_binding": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
    "tests.test_platform_backup_restore_drill.PlatformBackupRestoreDrillTests.test_synthetic_pg16_restore_diagnostic_under_root": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_supervisor_cleanup_covers_upload_failures_and_closes_locks": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_host_tools_contract_failure_emits_marker_before_locks": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_deploy_only_artifact_preparation_errors_emit_markers_without_app_writes": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_retained_lock_supervisor_preserves_callback_failure_marker": {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ENVIRONMENT": "github-hosted",
    },
}

# These cases execute the real PID-namespace supervisor and require the
# non-root Linux identity provided by the hosted Security runner. A root local
# executor cannot safely impersonate that boundary; both canonical entry
# points block before resource setup or test execution for these exact IDs.
TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS: frozenset[str] = frozenset(
    {
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_timeout_checkpoint_survives_missing_report_only_after_namespace_reap",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_success_report_publishes_only_after_child_exit",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_completed_failed_measurement_is_preserved",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_malformed_child_report_becomes_closed_failure",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_measurement_deadline_kills_hung_dns_connect_header_body_and_future",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_whole_runner_deadline_is_reachable_independently",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_sigterm_ignoring_descendant_is_killed_with_the_pid_namespace",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_double_fork_and_setsid_cannot_escape_namespace",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_forced_kill_preserves_timeout_when_child_report_is_missing",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_pdeathsig_reclaims_namespace_when_supervisor_is_sigkilled",
        "tests.test_platform_load_runtime.LoadRuntimeBudgetTests.test_external_term_is_reaped_and_reported",
    }
)


def test_environment_requirement_issues(
    test_ids: Iterable[str],
    environment: Mapping[str, str],
) -> list[str]:
    """Return missing fixed environment markers for the selected test IDs."""

    issues: list[str] = []
    for test_id in sorted(set(test_ids)):
        requirements = TEST_ENVIRONMENT_REQUIREMENTS.get(test_id)
        if requirements is None:
            continue
        missing = [
            f"{name}={expected}"
            for name, expected in sorted(requirements.items())
            if environment.get(name) != expected
        ]
        if missing:
            issues.append(f"{test_id} requires " + ", ".join(missing))
    return issues


def test_execution_requirement_issues(
    test_ids: Iterable[str],
    *,
    uid: int | None = None,
    gid: int | None = None,
    system: str | None = None,
    namespace_probe: Callable[[], str] | None = None,
) -> list[str]:
    """Return fixed refusals for tests needing a non-root PID namespace."""

    selected = sorted(set(test_ids) & TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS)
    if not selected:
        return []
    current_uid = os.getuid() if uid is None else uid
    current_gid = os.getgid() if gid is None else gid
    current_system = sys.platform if system is None else system
    capable = False
    if current_system.startswith("linux") and current_uid != 0 and current_gid != 0:
        try:
            if namespace_probe is None:
                try:
                    from tools.platform_load_runtime import require_pid_namespace_capability
                except ModuleNotFoundError:  # Direct execution from platform/tools.
                    from platform_load_runtime import require_pid_namespace_capability

                probe = require_pid_namespace_capability
            else:
                probe = namespace_probe
            probe()
            capable = True
        except Exception:
            # Never expose probe details through the catalog's public contract.
            capable = False
    if capable:
        return []
    return [
        f"{test_id} requires a non-root Linux runner with PID namespace capability"
        for test_id in selected
    ]

# CI uses one inexpensive DB-free job for the first, second and fifth contours,
# while the two serial contours remain isolated jobs. Keep this metadata next
# to the ownership catalog so the workflow and executable registry cannot
# quietly disagree about service or privilege boundaries. The privileged
# contour owns release/install and root-identity contracts even when fixtures
# are temporary: those tests exercise production paths that deliberately
# refuse an unprivileged caller or require root-owned metadata. One purge owner
# also needs the validated disposable Redis DB 15, but no PostgreSQL service.
CONTOUR_METADATA: Mapping[str, Mapping[str, object]] = {
    BACKEND_AGGREGATE: {
        "local_safe": True,
        "serial_resources": True,
        "requires_postgres": False,
        "requires_redis": False,
        "requires_root": True,
    },
    "backend-unit": {
        "local_safe": True,
        "serial_resources": False,
        "requires_postgres": False,
        "requires_redis": False,
        "requires_root": False,
    },
    "backend-tool-contract": {
        "local_safe": True,
        "serial_resources": False,
        "requires_postgres": False,
        "requires_redis": False,
        "requires_root": False,
    },
    "backend-integration": {
        "local_safe": True,
        "serial_resources": True,
        "requires_postgres": True,
        "requires_redis": True,
        "requires_root": True,
    },
    "backend-privileged": {
        "local_safe": False,
        "serial_resources": True,
        "requires_postgres": False,
        "requires_redis": True,
        "requires_root": True,
    },
    "performance-contract": {
        "local_safe": True,
        "serial_resources": False,
        "requires_postgres": False,
        "requires_redis": False,
        "requires_root": False,
    },
    VERIFICATION_CONTOUR: {
        "local_safe": True,
        "serial_resources": False,
        "requires_postgres": False,
        "requires_redis": False,
        "requires_root": False,
    },
}

CONTOUR_TIMEOUT_SECONDS: Mapping[str, int] = {
    BACKEND_AGGREGATE: 3600,
    "backend-unit": 300,
    "backend-tool-contract": 600,
    "backend-integration": 1200,
    "backend-privileged": 1200,
    "performance-contract": 900,
    VERIFICATION_CONTOUR: 300,
}

# These are intentionally explicit.  A new test module must be registered
# here, and the test-ID snapshot below must be updated in the same change.
# Otherwise the contract fails closed instead of silently assigning the new
# test to the expensive integration contour.
KNOWN_TEST_MODULES = frozenset(
    """
    test_as09_login_guessing
    test_as11_worker_error_sanitization
    test_platform_admin_api
    test_platform_admin_roster_api
    test_platform_async_test_contract
    test_platform_audit_api
    test_platform_auth_bootstrap
    test_platform_auth_identity_routes
    test_platform_auth_security
    test_platform_authenticated_read_admission
    test_platform_auto_assignment_benchmark
    test_platform_backend_audit_remediation
    test_platform_backup_offsite
    test_platform_backup_probe_workflow_contract
    test_platform_backup_restore_drill
    test_platform_backup_success_guard
    test_platform_bracket_graph_api
    test_platform_cdn_check
    test_platform_ci_classifier
    test_platform_cleanup_live_user_qa
    test_platform_cleanup_orphaned_integration
    test_platform_cleanup_retained_matrix
    test_platform_cleanup_retained_orphan
    test_platform_cloudflare_audit
    test_platform_cloudflare_ips
    test_platform_configure_shared_env
    test_platform_configure_ufw
    test_platform_cpu_profile
    test_platform_create_closed_auth
    test_platform_db
    test_platform_deadlock_api_flow
    test_platform_deadlock_assignment_runs
    test_platform_deadlock_auto_assignment
    test_platform_deadlock_automation_recovery
    test_platform_deadlock_automation_scheduler
    test_platform_deadlock_captain_round
    test_platform_deadlock_persistence_models
    test_platform_deadlock_web_flow
    test_platform_deadlock_workflow_state
    test_platform_deploy_smoke
    test_platform_docs
    test_platform_domain
    test_platform_email_delivery
    test_external_load_pending_origin_pipeline
    test_platform_external_content_security
    test_platform_external_load
    test_platform_external_load_workflow_contract
    test_platform_evidence_privacy
    test_platform_google_auth
    test_platform_health_monitor
    test_platform_home_content_runtime
    test_platform_host_tools_bundle
    test_platform_http_transport
    test_platform_install_nginx
    test_platform_invite_rate_limit
    test_platform_live_qa_guard
    test_platform_live_qa_mailbox_helper
    test_platform_live_qa_runtime_install
    test_platform_live_qa_wrappers
    test_platform_maintenance_qa_evidence
    test_platform_load_acceptance
    test_platform_load_runtime
    test_platform_logging
    test_platform_manual_live_auth_qa
    test_platform_match_progression_api
    test_platform_media_hard_delete
    test_platform_media_models
    test_platform_media_processor
    test_platform_media_rate_limit
    test_platform_media_runtime_cleanup
    test_platform_media_service
    test_platform_media_storage
    test_platform_media_worker
    test_platform_migrate_legacy_r2
    test_platform_migrate_media
    test_platform_model_indexes
    test_platform_noop_source_binding
    test_platform_object_storage
    test_platform_participant_manage_security
    test_platform_password_hash_contract
    test_platform_patch_detail
    test_platform_patch_miss_security
    test_platform_patch_sitemap
    test_platform_patch_translation
    test_platform_patch_translation_operators
    test_platform_patch_translation_persistence
    test_platform_patch_translation_prompt
    test_platform_patch_translation_qa_privacy
    test_platform_patch_translation_real_regressions
    test_platform_patch_translation_retry
    test_platform_performance_contract
    test_platform_persistence_concurrency_remediation
    test_platform_pip_pin_contract
    test_platform_player_commitments
    test_platform_prepare_test_runtime
    test_platform_production_config
    test_platform_production_qa_contract
    test_platform_production_qa_write_burst_profile
    test_platform_profile_read_models
    test_platform_profile_workspace
    test_platform_profiles_api
    test_platform_provision_live_csp_qa
    test_platform_public_contact_privacy
    test_platform_public_content
    test_platform_public_data_boundary
    test_platform_r2_smoke
    test_platform_ready_vote_admission
    test_platform_ready_vote_window
    test_platform_recover_live_user_qa
    test_platform_recover_retained_report
    test_platform_redis_lifecycle
    test_platform_release_audit_hardening
    test_platform_release_active_artifact_proof
    test_platform_release_build_diagnostics
    test_platform_release_build_contract
    test_platform_release_capture_probe
    test_platform_release_recovery_boundaries
    test_platform_release_retention
    test_platform_release_systemd_state
    test_platform_release_venv_rollback
    test_platform_retained_load_export_executor
    test_platform_remote_workflow_guards
    test_platform_request_performance
    test_platform_recovery_bootstrap
    test_platform_safe_env_exec
    test_platform_secret_artifact_boundary
    test_platform_secret_scan
    test_platform_secret_job_isolation
    test_platform_service_recovery_workflow
    test_platform_security_reports
    test_platform_ssh_host_key_scan
    test_platform_slugs
    test_platform_source_binding_dispatch
    test_platform_ssr_observability
    test_platform_stats_api
    test_platform_stats_query_count
    test_platform_steam_auth
    test_platform_steam_https_claim
    test_platform_storage_maintenance
    test_platform_storage_evidence_privacy
    test_platform_timeout_diagnostics
    test_platform_tournament_catalog_cache
    test_platform_tournament_catalog_read_models
    test_platform_tournament_concurrency_integration
    test_platform_tournament_concurrency_races
    test_platform_tournament_conditional_headers
    test_platform_tournament_inactive_workspace_access
    test_platform_tournament_inactive_workspace_integration
    test_platform_tournament_pagination_api
    test_platform_tournament_pagination_integration
    test_platform_tournament_participant_exclusion_integration
    test_platform_tournament_policy_api
    test_platform_tournament_read_models
    test_platform_tournament_teams
    test_platform_tournament_visibility_api
    test_platform_tournament_workflow
    test_platform_tournament_workspace_hot_path
    test_platform_ttfb_probe
    test_platform_update_shared_env
    test_platform_user_account_read_models
    test_platform_validate_edge_policy
    test_platform_validate_release_artifact
    test_platform_validate_wheelhouse
    test_platform_verification_contract
    test_platform_web_shutdown_guard
    test_platform_web_hermetic_browsers
    test_platform_recovery_workflow_caller
    test_platform_workflow_provenance
    test_platform_worker_runtime
    test_runtime_isolation
    test_platform_backend_test_catalog
    """.split()
)

# These tests have a different deterministic owner and intentionally do not
# enter the backend aggregate.  They are still discovered and checked for a
# unique owner, so adding one requires updating this declaration.
EXTERNAL_MODULE_OWNERS: Mapping[str, str] = {
    "test_platform_ci_classifier": "verification-contract",
    "test_platform_verification_contract": "verification-contract",
    "test_platform_recovery_workflow_caller": "verification-contract",
}

# This test is intentionally conditional: it validates production shared-env
# metadata only when that deployed-host path exists.  The skip is declared in
# the catalog and remains visible in the runner summary; all root/Pillow/
# service-identity prerequisites are fail-closed by the privileged preflight.
INTENTIONAL_SKIP_IDS: Mapping[str, str] = {
    "tests.test_platform_live_qa_mailbox_helper.MailboxHelperTests.test_live_shared_env_metadata_matches_reviewed_contour_when_present": (
        "production shared env path is absent"
    ),
}

PERFORMANCE_MODULES = frozenset(
    {
        "test_platform_auto_assignment_benchmark",
        "test_platform_cpu_profile",
        "test_platform_external_load",
        "test_platform_load_acceptance",
        "test_platform_load_runtime",
        "test_platform_performance_contract",
        "test_platform_production_qa_write_burst_profile",
        "test_platform_request_performance",
        "test_platform_ssr_observability",
        "test_platform_timeout_diagnostics",
        "test_platform_ttfb_probe",
    }
)

PRIVILEGED_MODULES = frozenset(
    {
        "test_platform_cleanup_live_user_qa",
        "test_platform_cleanup_retained_matrix",
        "test_platform_live_qa_guard",
        "test_platform_live_qa_mailbox_helper",
        "test_platform_live_qa_runtime_install",
        "test_platform_live_qa_wrappers",
        "test_platform_manual_live_auth_qa",
        "test_platform_media_processor",
        "test_platform_provision_live_csp_qa",
        "test_platform_recover_live_user_qa",
        "test_platform_recover_retained_report",
        "test_platform_release_audit_hardening",
        "test_platform_release_build_diagnostics",
        "test_platform_release_build_contract",
        "test_platform_release_recovery_boundaries",
        "test_platform_release_retention",
        "test_platform_release_systemd_state",
        "test_platform_release_venv_rollback",
        "test_platform_retained_load_export_executor",
        "test_platform_service_recovery_workflow",
        "test_platform_recovery_bootstrap",
        "test_platform_remote_workflow_guards",
        "test_platform_safe_env_exec",
        "test_platform_storage_maintenance",
        "test_platform_validate_release_artifact",
    }
)

TOOL_MODULES = frozenset(
    {
        "test_platform_backup_offsite",
        "test_platform_backup_probe_workflow_contract",
        "test_platform_backup_restore_drill",
        "test_platform_backup_success_guard",
        "test_platform_backend_test_catalog",
        "test_platform_cdn_check",
        "test_platform_cleanup_orphaned_integration",
        "test_platform_cleanup_retained_orphan",
        "test_platform_cloudflare_audit",
        "test_platform_cloudflare_ips",
        "test_platform_configure_shared_env",
        "test_platform_configure_ufw",
        "test_platform_deploy_smoke",
        "test_platform_evidence_privacy",
        "test_platform_docs",
        "test_external_load_pending_origin_pipeline",
        "test_platform_external_load_workflow_contract",
        "test_platform_install_nginx",
        "test_platform_host_tools_bundle",
        "test_platform_maintenance_qa_evidence",
        "test_platform_source_binding_dispatch",
        "test_platform_media_hard_delete",
        "test_platform_media_models",
        "test_platform_media_runtime_cleanup",
        "test_platform_media_storage",
        "test_platform_media_worker",
        "test_platform_migrate_legacy_r2",
        "test_platform_model_indexes",
        "test_platform_noop_source_binding",
        "test_platform_object_storage",
        "test_platform_pip_pin_contract",
        "test_platform_patch_translation_qa_privacy",
        "test_platform_prepare_test_runtime",
        "test_platform_production_config",
        "test_platform_production_qa_contract",
        "test_platform_r2_smoke",
        "test_platform_release_active_artifact_proof",
        "test_platform_release_capture_probe",
        "test_platform_secret_artifact_boundary",
        "test_platform_secret_scan",
        "test_platform_secret_job_isolation",
        "test_platform_security_reports",
        "test_platform_ssh_host_key_scan",
        "test_platform_storage_evidence_privacy",
        "test_platform_update_shared_env",
        "test_platform_validate_edge_policy",
        "test_platform_validate_wheelhouse",
        "test_platform_web_shutdown_guard",
        "test_platform_web_hermetic_browsers",
        "test_platform_workflow_provenance",
        "test_runtime_isolation",
    }
)

# Conservative unit ownership.  Modules not listed here remain integration
# owned unless a more specific tool/performance/privileged rule applies.
UNIT_MODULES = frozenset(
    {
        "test_platform_async_test_contract",
        "test_platform_auth_bootstrap",
        "test_platform_authenticated_read_admission",
        "test_platform_backend_audit_remediation",
        "test_platform_create_closed_auth",
        "test_platform_deadlock_assignment_runs",
        "test_platform_deadlock_auto_assignment",
        "test_platform_deadlock_automation_scheduler",
        "test_platform_deadlock_captain_round",
        "test_platform_deadlock_persistence_models",
        "test_platform_deadlock_web_flow",
        "test_platform_deadlock_workflow_state",
        "test_platform_domain",
        "test_platform_external_content_security",
        "test_platform_health_monitor",
        "test_platform_home_content_runtime",
        "test_platform_http_transport",
        "test_platform_logging",
        "test_platform_media_rate_limit",
        "test_platform_patch_detail",
        "test_platform_password_hash_contract",
        "test_platform_patch_sitemap",
        "test_platform_patch_translation",
        "test_platform_patch_translation_operators",
        "test_platform_patch_translation_prompt",
        "test_platform_patch_translation_real_regressions",
        "test_platform_patch_translation_retry",
        "test_platform_persistence_concurrency_remediation",
        "test_platform_public_contact_privacy",
        "test_platform_public_content",
        "test_platform_ready_vote_admission",
        "test_platform_ready_vote_window",
        "test_platform_slugs",
        "test_platform_tournament_catalog_cache",
        "test_platform_tournament_catalog_read_models",
        "test_platform_tournament_conditional_headers",
        "test_platform_tournament_inactive_workspace_access",
        "test_platform_tournament_pagination_api",
        "test_platform_tournament_read_models",
        "test_platform_tournament_workspace_hot_path",
        "test_platform_user_account_read_models",
        "test_platform_worker_runtime",
    }
)

CLASS_CONTOUR_OVERRIDES: Mapping[tuple[str, str], str] = {
    ("test_platform_auth_identity_routes", "AuthIdentityRouteTests"): "backend-unit",
    ("test_platform_auth_security", "AuthSecurityUnitTests"): "backend-unit",
    ("test_platform_db", "PlatformDatabaseConfigurationTests"): "backend-unit",
    ("test_platform_google_auth", "GoogleOAuthUnitTests"): "backend-unit",
    (
        "test_platform_manual_live_auth_qa",
        "ManualLiveAuthQaIntegrationTests",
    ): "backend-integration",
    ("test_platform_migrate_media", "PlatformMediaMigrationUnitTests"): "backend-tool-contract",
    ("test_platform_patch_translation_persistence", "PatchTranslationPersistenceModelTests"): "backend-unit",
    (
        "test_platform_provision_live_csp_qa",
        "LiveCspQaProvisionerIntegrationTests",
    ): "backend-integration",
    ("test_platform_steam_auth", "SteamOpenIDUnitTests"): "backend-unit",
}

TEST_CONTOUR_OVERRIDES: Mapping[tuple[str, str, str], str] = {
    (
        "test_platform_backup_restore_drill",
        "PlatformBackupRestoreDrillTests",
        "test_scratch_postgres_memory_setting_is_paired_and_safety_options_stay_fixed",
    ): "backend-tool-contract",
    (
        "test_platform_backup_restore_drill",
        "PlatformBackupRestoreDrillTests",
        "test_scratch_postgres_directory_setup_without_cap_fowner",
    ): "backend-privileged",
    (
        "test_platform_backup_restore_drill",
        "PlatformBackupRestoreDrillTests",
        "test_isolated_restore_signal_cleanup_under_root",
    ): "backend-privileged",
    (
        "test_platform_backup_restore_drill",
        "PlatformBackupRestoreDrillTests",
        "test_disk_image_fixture_directory_validator_checks_identity_and_mode",
    ): "backend-privileged",
    (
        "test_platform_backup_restore_drill",
        "PlatformBackupRestoreDrillTests",
        "test_synthetic_pg16_restore_diagnostic_under_root",
    ): "backend-privileged",
    (
        "test_platform_host_tools_bundle",
        "HostToolsBundleTests",
        "test_active_release_baseline_reader_returns_stable_closed_tuple",
    ): "backend-privileged",
}

# Filled after the implementation snapshot is stable.  The contract compares
# the sorted current IDs to these digests; a new test therefore needs an
# explicit catalog update instead of silently inheriting a module default.
EXPECTED_SNAPSHOT: Mapping[str, object] = {
    "module_count": 171,
    "module_digest": "c0f368cefc4f365a817e2ec2b61e64f4f733a5a56568242c8236cdc3094ce21c",
    "test_count": 2162,
    "test_id_digest": "2a2afbbc8efc9ae179d671c7a496937dfa1b4a7a3a868454d82d0bed4c2bd1c7",
    "backend_test_count": 2085,
    "backend_test_id_digest": "eec04bff94c36c5ced4fd318981b0d29bdd359cbce8efbe74118c3c35c409c01",
    "verification_test_count": 77,
    "verification_test_id_digest": "b8fdeb78a3ac6733a0a47fc6b671bde197fda652a7064a6296c9b7ad36f0cea7",
    "verification_classifier_test_count": 41,
    "verification_classifier_test_id_digest": "3e754b6d27fce28cc94285bfd4be6b9cfcae84988b8f9befc892615f8547d32e",
}
# Keep each executable contour's boundary independently snapshotted.  The
# aggregate snapshot proves total ownership, while these entries make a
# seemingly harmless module move fail closed instead of silently changing the
# privilege/resource contract.  Rationale is part of the machine-readable
# registry so a future owner change must explain itself in the same change.
CONTOUR_RATIONALE: Mapping[str, str] = {
    "backend-unit": "pure unit/domain/backend behavior without external operator resources",
    "backend-tool-contract": "hermetic repository tools, backup restore diagnostics, source-bound artifact proofs and read-only release-capture diagnostics without root-owned host state",
    "backend-integration": "PostgreSQL/Redis workflows and concurrency behavior",
    "backend-privileged": "root/service-identity, release/install/systemd, artifact ownership, privileged wrappers, verified ephemeral scratch PostgreSQL diagnostics and root-owned API/worker service-recovery fixtures",
    "performance-contract": "deterministic load, observer and acceptance contracts",
    VERIFICATION_CONTOUR: "registry, workflow and profile ownership self-tests",
}
EXPECTED_CONTOUR_SNAPSHOT: Mapping[str, Mapping[str, object]] = {
    "backend-unit": {
        "module_count": 48,
        "module_digest": "97da81814d431484c5023f96dab71a84df718c6a84c13b548142621d599335f6",
        "test_count": 314,
        "test_id_digest": "45936172568eeaacb907ba21a872d4222e1a42d4ef9375a62517165a051b9f27",
    },
    "backend-tool-contract": {
        "module_count": 52,
        "module_digest": "88caec1d7501a955a7d663309dcffb8ed6b0c178445690524f3597b9a892f2c4",
        "test_count": 565,
        "test_id_digest": "3a400c3eca38027ac114deeec4abcdcdbdf712d83390e549602011b532ae9f09",
    },
    "backend-integration": {
        "module_count": 41,
        "module_digest": "c5547c1d61ed9824ad5d58b11a7f5b08a409c50e1cfd47a2a44107c22ee0e6ee",
        "test_count": 262,
        "test_id_digest": "d6058717285a42ff732a43c18b4a181b01f487bd82e2691a87b1703aa8cf6066",
    },
    "backend-privileged": {
        "module_count": 27,
        "module_digest": "60b3e4ffd2556af3a341b6f18a33740f75dcad5e97fa53b77a9a4ed70cd3aed0",
        "test_count": 704,
        "test_id_digest": "3b74563c1a50ac6d6530c9030771d03f5aae29a53e719c2c0bd6acfa2c61dcf2",
    },
    "performance-contract": {
        "module_count": 11,
        "module_digest": "ac879f1b292f00df28f399af5e34ed8995b39878e1961386b8eb055bb1d025e0",
        "test_count": 240,
        "test_id_digest": "7a80d937350966059e2bbf5cd4d961b7c98d42d0cd72f0935199f929731fa5df",
    },
    VERIFICATION_CONTOUR: {
        "module_count": 3,
        "module_digest": "bc3b5eb1bc4e61842407a5e9edd6ab07f4bb21581ac2663b4754f6dcdaf5c02c",
        "test_count": 77,
        "test_id_digest": "b8fdeb78a3ac6733a0a47fc6b671bde197fda652a7064a6296c9b7ad36f0cea7",
    },
}
EXPECTED_MODULE_COUNT = int(EXPECTED_SNAPSHOT["module_count"])
EXPECTED_MODULE_DIGEST = str(EXPECTED_SNAPSHOT["module_digest"])
EXPECTED_TEST_COUNT = int(EXPECTED_SNAPSHOT["test_count"])
EXPECTED_TEST_ID_DIGEST = str(EXPECTED_SNAPSHOT["test_id_digest"])


@dataclass(frozen=True, slots=True)
class TestCase:
    """One unittest-discoverable test method and its catalog metadata."""

    test_id: str
    module: str
    class_name: str
    method_name: str
    line: int
    is_async: bool
    contour: str


def _module_name(path: Path) -> str:
    return f"tests.{path.stem}"


def _source_module(path: Path) -> str:
    return path.stem


def discover_test_cases(tests_root: Path = TESTS_ROOT) -> tuple[TestCase, ...]:
    """Discover test IDs without importing application modules."""

    global _DISCOVERY_CACHE
    root = Path(tests_root).resolve()
    sources = tuple(
        (path.name, path.read_bytes())
        for path in sorted(root.glob("test_*.py"))
    )
    fingerprint = hashlib.sha256()
    for name, source in sources:
        fingerprint.update(name.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(source)
        fingerprint.update(b"\0")
    fingerprint.update(
        repr(
            (
                tuple(sorted(EXTERNAL_MODULE_OWNERS.items())),
                tuple(sorted(PERFORMANCE_MODULES)),
                tuple(sorted(CLASS_CONTOUR_OVERRIDES.items())),
                tuple(sorted(TEST_CONTOUR_OVERRIDES.items())),
                tuple(sorted(PRIVILEGED_MODULES)),
                tuple(sorted(TOOL_MODULES)),
                tuple(sorted(UNIT_MODULES)),
                tuple(sorted(KNOWN_TEST_MODULES)),
            )
        ).encode("utf-8")
    )
    digest = fingerprint.digest()

    # Dispatch validates the inventory before every command and may also
    # validate it again while rendering timeout diagnostics. Hash the current
    # source bytes on each call so edits are never hidden, but avoid reparsing
    # the same hundreds of modules into ASTs repeatedly in one process.
    with _DISCOVERY_CACHE_LOCK:
        if (
            _DISCOVERY_CACHE is not None
            and _DISCOVERY_CACHE[0] == root
            and _DISCOVERY_CACHE[1] == digest
        ):
            return _DISCOVERY_CACHE[2]

    cases: list[TestCase] = []
    for name, source in sources:
        path = root / name
        source_module = _source_module(path)
        module = _module_name(path)
        tree = ast.parse(source.decode("utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not method.name.startswith("test_"):
                    continue
                test_id = f"{module}.{node.name}.{method.name}"
                cases.append(
                    TestCase(
                        test_id=test_id,
                        module=source_module,
                        class_name=node.name,
                        method_name=method.name,
                        line=method.lineno,
                        is_async=isinstance(method, ast.AsyncFunctionDef),
                        contour=_contour_for(source_module, node, method),
                    )
                )
    result = tuple(cases)
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE = (root, digest, result)
    return result


def _contour_for(module: str, class_node: ast.ClassDef, method: ast.AST) -> str:
    external = EXTERNAL_MODULE_OWNERS.get(module)
    if external is not None:
        return external
    if module in PERFORMANCE_MODULES:
        return "performance-contract"
    override = CLASS_CONTOUR_OVERRIDES.get((module, class_node.name))
    if override is not None:
        return override
    if module in PRIVILEGED_MODULES:
        return "backend-privileged"
    method_override = TEST_CONTOUR_OVERRIDES.get(
        (module, class_node.name, getattr(method, "name", ""))
    )
    if method_override is not None:
        return method_override
    if module in TOOL_MODULES:
        return "backend-tool-contract"
    if module in UNIT_MODULES:
        return "backend-unit"
    if module in KNOWN_TEST_MODULES:
        return "backend-integration"
    return "unowned"


def module_digest(modules: Iterable[str]) -> str:
    payload = "\n".join(sorted(modules)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_id_digest(test_ids: Iterable[str]) -> str:
    payload = "\n".join(sorted(test_ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def intentional_skip_issues(
    cases: Iterable[TestCase],
    *,
    tests_root: Path = TESTS_ROOT,
) -> list[str]:
    """Validate intentional skip IDs against their source-level reason.

    A skip is allowed to be visible in a strict contour only when its exact
    unittest ID is present in the AST catalog and the test declares the same
    stable string reason.  This keeps a stale ID or a changed ``skipTest``
    message from becoming an unnoticed false-green result.
    """

    selected = tuple(cases)
    by_id = {case.test_id: case for case in selected}
    issues: list[str] = []
    for test_id, expected_reason in INTENTIONAL_SKIP_IDS.items():
        case = by_id.get(test_id)
        if case is None:
            # ``catalog_issues`` reports the complete list of unknown IDs;
            # avoid duplicating that diagnostic here.
            continue
        if (
            not isinstance(expected_reason, str)
            or not expected_reason
            or expected_reason != expected_reason.strip()
        ):
            issues.append(f"intentional skip reason is not stable for {test_id}")
            continue
        source_path = tests_root / f"{case.module}.py"
        try:
            tree = ast.parse(
                source_path.read_text(encoding="utf-8"),
                filename=str(source_path),
            )
        except (OSError, UnicodeError, SyntaxError) as exc:
            issues.append(f"intentional skip source is unreadable for {test_id}: {exc}")
            continue

        declared_reasons: list[str] = []
        dynamic_reason = False
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != case.class_name:
                continue
            method_node = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == case.method_name
                ),
                None,
            )
            if method_node is None:
                continue
            for child in ast.walk(method_node):
                if not isinstance(child, ast.Call):
                    continue
                function = child.func
                if not isinstance(function, ast.Attribute) or function.attr != "skipTest":
                    continue
                if (
                    len(child.args) == 1
                    and isinstance(child.args[0], ast.Constant)
                    and isinstance(child.args[0].value, str)
                    and child.keywords == []
                ):
                    declared_reasons.append(child.args[0].value)
                else:
                    dynamic_reason = True
        if dynamic_reason:
            issues.append(f"intentional skip reason is not stable for {test_id}")
        elif not declared_reasons or set(declared_reasons) != {expected_reason}:
            actual = ", ".join(repr(reason) for reason in declared_reasons) or "<none>"
            issues.append(
                f"intentional skip reason changed for {test_id}: "
                f"expected {expected_reason!r}, found {actual}"
            )
    return issues


def backend_cases(cases: Iterable[TestCase]) -> tuple[TestCase, ...]:
    return tuple(case for case in cases if case.contour in BACKEND_CONTOURS)


def cases_for_contour(
    contour: str,
    cases: Iterable[TestCase] | None = None,
) -> tuple[TestCase, ...]:
    selected = discover_test_cases() if cases is None else tuple(cases)
    if contour == BACKEND_AGGREGATE:
        return backend_cases(selected)
    if contour in (*BACKEND_CONTOURS, VERIFICATION_CONTOUR):
        return tuple(case for case in selected if case.contour == contour)
    raise ValueError(f"unknown backend contour: {contour}")


def contour_snapshot(
    contour: str,
    cases: Iterable[TestCase] | None = None,
) -> dict[str, object]:
    """Return stable ownership facts for one executable contour."""

    selected = cases_for_contour(contour, cases)
    modules = {case.module for case in selected}
    return {
        "module_count": len(modules),
        "module_digest": module_digest(modules),
        "test_count": len(selected),
        "test_id_digest": test_id_digest(case.test_id for case in selected),
    }


def catalog_issues(
    cases: Iterable[TestCase] | None = None,
    *,
    enforce_snapshot: bool = True,
) -> list[str]:
    selected = tuple(discover_test_cases() if cases is None else cases)
    issues: list[str] = []
    module_names = {case.module for case in selected}
    unknown_modules = sorted(module_names - KNOWN_TEST_MODULES)
    if unknown_modules:
        issues.append(f"unowned test modules: {', '.join(unknown_modules)}")
    duplicate_ids = sorted(
        test_id for test_id in {case.test_id for case in selected}
        if sum(item.test_id == test_id for item in selected) > 1
    )
    if duplicate_ids:
        issues.append(f"duplicate test IDs: {', '.join(duplicate_ids)}")
    invalid_contours = sorted(
        {case.test_id for case in selected if case.contour not in (*BACKEND_CONTOURS, *EXTERNAL_MODULE_OWNERS.values())}
    )
    if invalid_contours:
        issues.append(f"tests without one owner contour: {', '.join(invalid_contours)}")
    owner_contours = (
        *BACKEND_CONTOURS,
        *dict.fromkeys(EXTERNAL_MODULE_OWNERS.values()),
    )
    owner_sets = [
        {
            case.test_id
            for case in selected
            if case.contour == contour
        }
        for contour in owner_contours
    ]
    overlapping_owner_ids = sorted(
        test_id
        for index, current in enumerate(owner_sets)
        for other in owner_sets[index + 1 :]
        for test_id in current.intersection(other)
    )
    if overlapping_owner_ids:
        issues.append(
            "test IDs have overlapping owner contours: "
            + ", ".join(overlapping_owner_ids)
        )
    owned_ids = set().union(*owner_sets) if owner_sets else set()
    selected_ids = {case.test_id for case in selected}
    if owned_ids != selected_ids:
        issues.append("AST test IDs are not a complete disjoint owner union")
    if len(module_names) != EXPECTED_MODULE_COUNT:
        issues.append(
            f"module snapshot changed: expected {EXPECTED_MODULE_COUNT}, got {len(module_names)}"
        )
    if enforce_snapshot:
        current_module_digest = module_digest(module_names)
        if EXPECTED_MODULE_DIGEST != "__SET_AFTER_SNAPSHOT__" and current_module_digest != EXPECTED_MODULE_DIGEST:
            issues.append(
                "module ownership snapshot changed: "
                f"expected {EXPECTED_MODULE_DIGEST}, got {current_module_digest}"
            )
        if EXPECTED_TEST_COUNT and len(selected) != EXPECTED_TEST_COUNT:
            issues.append(
                f"test ID snapshot changed: expected {EXPECTED_TEST_COUNT}, got {len(selected)}"
            )
        if EXPECTED_TEST_ID_DIGEST != "__SET_AFTER_SNAPSHOT__":
            current_test_digest = test_id_digest(case.test_id for case in selected)
            if current_test_digest != EXPECTED_TEST_ID_DIGEST:
                issues.append(
                    "test ID ownership snapshot changed: "
                    f"expected {EXPECTED_TEST_ID_DIGEST}, got {current_test_digest}"
                )
        current_snapshot = {
            "backend_test_count": len(backend_cases(selected)),
            "backend_test_id_digest": test_id_digest(
                case.test_id for case in backend_cases(selected)
            ),
            "verification_test_count": len(
                cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_test_id_digest": test_id_digest(
                case.test_id
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_classifier_test_count": sum(
                case.module == "test_platform_ci_classifier"
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_classifier_test_id_digest": test_id_digest(
                case.test_id
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
                if case.module == "test_platform_ci_classifier"
            ),
        }
        for field, expected in current_snapshot.items():
            expected_value = EXPECTED_SNAPSHOT[field]
            if expected_value != expected:
                issues.append(
                    f"{field} snapshot changed: expected {expected_value}, got {expected}"
                )
        for contour, expected in EXPECTED_CONTOUR_SNAPSHOT.items():
            actual = contour_snapshot(contour, selected)
            for field, expected_value in expected.items():
                actual_value = actual[field]
                if actual_value != expected_value:
                    issues.append(
                        f"{contour} {field} snapshot changed: "
                        f"expected {expected_value}, got {actual_value}"
                    )
    contours = {case.contour for case in selected}
    missing = sorted(set(BACKEND_CONTOURS) - contours)
    if missing:
        issues.append(f"empty backend contours: {', '.join(missing)}")
    known_ids = {case.test_id for case in selected}
    unknown_skip_ids = sorted(set(INTENTIONAL_SKIP_IDS) - known_ids)
    if unknown_skip_ids:
        issues.append(
            "intentional skip IDs are not discovered: "
            + ", ".join(unknown_skip_ids)
        )
    issues.extend(intentional_skip_issues(selected))
    return issues


def summary(cases: Iterable[TestCase] | None = None) -> dict[str, object]:
    selected = tuple(discover_test_cases() if cases is None else cases)
    by_contour: dict[str, int] = {}
    for case in selected:
        by_contour[case.contour] = by_contour.get(case.contour, 0) + 1
    return {
        "schema": 1,
        "modules": len({case.module for case in selected}),
        "tests": len(selected),
        "async_tests": sum(case.is_async for case in selected),
        "sync_tests": sum(not case.is_async for case in selected),
        "by_contour": dict(sorted(by_contour.items())),
        "external_owners": dict(EXTERNAL_MODULE_OWNERS),
        "intentional_skips": dict(INTENTIONAL_SKIP_IDS),
    }


def registry_payload() -> dict[str, object]:
    selected = discover_test_cases()
    contour_metadata = CONTOUR_METADATA
    return {
        "schema": 1,
        "aggregate": BACKEND_AGGREGATE,
        "snapshot": {
            "module_count": len({case.module for case in selected}),
            "module_digest": module_digest({case.module for case in selected}),
            "test_count": len(selected),
            "test_id_digest": test_id_digest(case.test_id for case in selected),
            "backend_test_count": len(backend_cases(selected)),
            "backend_test_id_digest": test_id_digest(
                case.test_id for case in backend_cases(selected)
            ),
            "verification_test_count": len(
                cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_test_id_digest": test_id_digest(
                case.test_id
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_classifier_test_count": sum(
                case.module == "test_platform_ci_classifier"
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
            ),
            "verification_classifier_test_id_digest": test_id_digest(
                case.test_id
                for case in cases_for_contour(VERIFICATION_CONTOUR, selected)
                if case.module == "test_platform_ci_classifier"
            ),
        },
        "contours": [
            {
                "id": contour,
                "deterministic": True,
                "runner": "tools/platform_test_runner.py",
                "timeout_class": "short" if contour in {"backend-unit", "backend-tool-contract", "performance-contract"} else "long",
                "timeout_seconds": CONTOUR_TIMEOUT_SECONDS[contour],
                **dict(contour_metadata[contour]),
                **contour_snapshot(contour, selected),
                "rationale": CONTOUR_RATIONALE[contour],
            }
            for contour in BACKEND_CONTOURS
        ],
        "external_owners": dict(EXTERNAL_MODULE_OWNERS),
        "test_environment_requirements": {
            test_id: dict(sorted(requirements.items()))
            for test_id, requirements in sorted(TEST_ENVIRONMENT_REQUIREMENTS.items())
        },
        "test_execution_requirements": {
            test_id: {
                "non_root": True,
                "linux": True,
                "pid_namespace_capability": True,
            }
            for test_id in sorted(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS)
        },
        "external_contours": [
            {
                "id": VERIFICATION_CONTOUR,
                "deterministic": True,
                "runner": "tools/platform_test_runner.py",
                "timeout_seconds": CONTOUR_TIMEOUT_SECONDS[VERIFICATION_CONTOUR],
                **dict(contour_metadata[VERIFICATION_CONTOUR]),
                **contour_snapshot(VERIFICATION_CONTOUR, selected),
                "rationale": CONTOUR_RATIONALE[VERIFICATION_CONTOUR],
            }
        ],
    }
