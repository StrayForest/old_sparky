#!/usr/bin/env python3
"""Canonical deterministic verification registry and dispatcher.

The registry is the public contract for repository verification.  Workflow
files provide runners, services and permissions; they do not re-define the
commands owned here.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Sequence


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PLATFORM_ROOT / "tools"
WEB_ROOT = PLATFORM_ROOT / "apps" / "platform_web"
_PROCESS_GROUP_TERM_GRACE_SECONDS = 1.0
_BACKEND_PROGRESS_MAX_BYTES = 2 * 1024 * 1024


def _create_private_migration_diagnostic() -> tuple[Path, Path]:
    """Create a private, task-owned file for incremental migration stages."""

    directory = Path(tempfile.mkdtemp(prefix="platform-migration-progress-", dir="/tmp"))
    report = directory / "progress.log"
    descriptor = -1
    try:
        directory_stat = os.lstat(directory)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise OSError("private migration report directory failed validation")
        descriptor = os.open(
            report,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        report_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(report_stat.st_mode)
            or report_stat.st_uid != os.geteuid()
            or report_stat.st_nlink != 1
            or stat.S_IMODE(report_stat.st_mode) != 0o600
        ):
            raise OSError("private migration report file failed validation")
        return directory, report
    except BaseException:
        try:
            report.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_backend_progress_path(
    arguments: Sequence[str],
) -> tuple[Path, Path | None]:
    """Create one private runner checkpoint path, adjacent to summaries when supplied."""

    summary_path: Path | None = None
    normalized = tuple(arguments[1:]) if arguments[:1] == ("--",) else tuple(arguments)
    for index, argument in enumerate(normalized):
        if argument == "--summary" and index + 1 < len(normalized):
            summary_path = Path(normalized[index + 1])
        elif argument.startswith("--summary="):
            summary_path = Path(argument.partition("=")[2])

    private_directory: Path | None = None
    path: Path | None = None
    descriptor = -1
    try:
        if summary_path is None:
            private_directory = Path(
                tempfile.mkdtemp(prefix="platform-backend-progress-", dir="/tmp")
            )
            path = private_directory / "progress.json"
            directory_stat = os.lstat(private_directory)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
            ):
                raise OSError("private backend progress directory failed validation")
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        else:
            if not summary_path.is_absolute():
                summary_path = PLATFORM_ROOT / summary_path
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="backend-privileged-progress-",
                suffix=".json",
                dir=summary_path.parent,
            )
            path = Path(name)
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or file_stat.st_nlink != 1
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise OSError("backend progress file failed validation")
        return path, private_directory
    except BaseException:
        try:
            if path is not None:
                path.unlink(missing_ok=True)
            if private_directory is not None:
                private_directory.rmdir()
        except OSError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_backend_progress(path: Path) -> dict[str, object] | None:
    """Read a bounded diagnostic checkpoint without following or trusting paths."""

    descriptor = -1
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > _BACKEND_PROGRESS_MAX_BYTES
        ):
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return None
        chunks: list[bytes] = []
        remaining = _BACKEND_PROGRESS_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _BACKEND_PROGRESS_MAX_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    expected_keys = {
        "schema",
        "contour",
        "status",
        "execution_complete",
        "selected_count",
        "expected_ids",
        "completed_timings",
        "active_test_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        return None
    if (
        isinstance(payload.get("schema"), bool)
        or payload.get("schema") != 1
        or payload.get("contour") != "backend-privileged"
        or payload.get("status") != "diagnostic_only"
        or payload.get("execution_complete") is not False
    ):
        return None

    catalog = _backend_catalog_module()
    registered_ids = {
        case.test_id for case in catalog.cases_for_contour("backend-privileged")
    }
    expected_ids = payload.get("expected_ids")
    selected_count = payload.get("selected_count")
    completed = payload.get("completed_timings")
    active_id = payload.get("active_test_id")
    if (
        not isinstance(expected_ids, list)
        or not expected_ids
        or any(not isinstance(test_id, str) for test_id in expected_ids)
        or expected_ids != sorted(set(expected_ids))
        or not set(expected_ids).issubset(registered_ids)
        or isinstance(selected_count, bool)
        or not isinstance(selected_count, int)
        or selected_count != len(expected_ids)
        or not isinstance(completed, list)
        or len(completed) > selected_count
        or (active_id is not None and not isinstance(active_id, str))
        or (active_id is not None and active_id not in expected_ids)
    ):
        return None

    completed_ids: list[str] = []
    for timing in completed:
        if not isinstance(timing, dict) or set(timing) != {
            "test_id",
            "duration_ms",
            "outcome",
        }:
            return None
        test_id = timing.get("test_id")
        duration = timing.get("duration_ms")
        outcome = timing.get("outcome")
        if (
            not isinstance(test_id, str)
            or test_id not in expected_ids
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration < 0
            or duration
            > catalog.CONTOUR_TIMEOUT_SECONDS["backend-privileged"] * 2000
            or not math.isfinite(float(duration))
            or not isinstance(outcome, str)
            or outcome not in {
                "passed",
                "failed",
                "error",
                "skipped",
                "expected-failure",
                "unexpected-success",
            }
        ):
            return None
        completed_ids.append(test_id)
    if (
        len(set(completed_ids)) != len(completed_ids)
        or (active_id is not None and active_id in completed_ids)
    ):
        return None
    payload["last_completed_test_id"] = completed_ids[-1] if completed_ids else None
    return payload


def _emit_backend_progress_timeout(path: Path) -> bool:
    progress = _read_backend_progress(path)
    if progress is None:
        print(
            "[GATE TIMEOUT DETAIL] backend-privileged progress=unavailable",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(
        "[GATE TIMEOUT DETAIL] backend-privileged "
        f"selected={progress['selected_count']} "
        f"completed={len(progress['completed_timings'])} "
        f"active_test_id={progress['active_test_id'] or 'none'} "
        f"last_completed_test_id={progress['last_completed_test_id'] or 'none'}",
        file=sys.stderr,
        flush=True,
    )
    return True


def _remove_backend_progress_path(path: Path) -> bool:
    """Remove only our private regular progress file after it is no longer needed."""

    try:
        current = os.lstat(path)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            return False
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _remove_backend_progress_temps(path: Path) -> bool:
    """Remove only atomic-write temps uniquely associated with this progress path."""

    prefix = f".{path.name}."
    directory_fd = -1
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
                    continue
                try:
                    current = entry.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or current.st_uid != os.geteuid()
                        or current.st_nlink != 1
                        or stat.S_IMODE(current.st_mode) != 0o600
                    ):
                        return False
                    os.unlink(entry.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
    except OSError:
        return False
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
    return True


class _VerifierTermination(BaseException):
    """A TERM delivered while the verifier owns a running gate process group."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


def _backend_catalog_module():
    try:
        from tools import platform_test_catalog
    except ModuleNotFoundError:
        import platform_test_catalog

        return platform_test_catalog
    return platform_test_catalog


def _preflight_backend_test_environment(
    contour: str,
    arguments: Sequence[str],
) -> None:
    """Apply exact catalog-owned host requirements before dispatching tests."""

    catalog = _backend_catalog_module()
    normalized = tuple(arguments[1:]) if arguments[:1] == ("--",) else tuple(arguments)
    if normalized[:1] == ("--list",):
        return
    selectors = normalized[1:] if normalized[:1] == ("--focused",) else ()
    cases = catalog.cases_for_contour(contour)
    if selectors:
        selected_ids = {
            case.test_id
            for case in cases
            if any(
                case.test_id == selector
                or case.test_id.startswith(selector + ".")
                for selector in selectors
            )
        }
    else:
        selected_ids = {case.test_id for case in cases}
    issues = catalog.test_environment_requirement_issues(selected_ids, os.environ)
    issues.extend(catalog.test_execution_requirement_issues(selected_ids))
    if issues:
        raise VerificationError("LOCAL GATE BLOCKED: " + " | ".join(issues))


def _require_backend_privileged_root() -> None:
    """Require the real root identity before dispatching privileged tests."""

    if os.geteuid() != 0:
        raise VerificationError("backend-privileged requires the root test user")


@dataclass(frozen=True, slots=True)
class Gate:
    """Metadata and ownership for one stable verification contour."""

    id: str
    description: str
    deterministic: bool
    local_safe: bool
    ci_required: bool
    environment_requirements: tuple[str, ...]
    canonical_runner: str
    timeout_class: str
    owner: str
    conditional: bool = False

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["environment_requirements"] = list(self.environment_requirements)
        return payload


# This is the only authored verification inventory.  Keep production contours
# visible for discovery, but without a local dispatcher: they belong to their
# protected GitHub/production workflows.
GATES: tuple[Gate, ...] = (
    Gate(
        id="backend",
        description="Backend unit and integration tests through the ownership catalog.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=(
            "PLATFORM_ENVIRONMENT=test",
            "database=platformdb_test",
            "isolated PostgreSQL and Redis",
        ),
        canonical_runner="tools/platform_run_tests.sh",
        timeout_class="long",
        owner="backend/domain",
    ),
    Gate(
        id="python-quality",
        description="Repository-owned Python lint and static quality checks.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("pinned quality dependencies",),
        canonical_runner="ruff",
        timeout_class="medium",
        owner="backend/tooling",
    ),
    Gate(
        id="security",
        description="Dependency, Bandit and repository secret security checks.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("pinned platform and quality dependencies",),
        canonical_runner="pip-audit + bandit + platform_secret_scan.py",
        timeout_class="medium",
        owner="security",
    ),
    Gate(
        id="migration",
        description="Populated disposable-database migration scenario.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=(
            "PLATFORM_ENVIRONMENT=test",
            "disposable PostgreSQL only",
        ),
        canonical_runner="tools/platform_migration_scenario.py",
        timeout_class="long",
        owner="persistence",
    ),
    Gate(
        id="docs",
        description="Platform documentation index and local-link validation.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("repository checkout",),
        canonical_runner="tools/platform_docs_check.py",
        timeout_class="short",
        owner="platform-maintainers",
    ),
    Gate(
        id="web-quality",
        description="Frontend dependency audit, typecheck, lint and production build.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("Node 26.3.1", "npm lockfile and dependencies"),
        canonical_runner="tools/platform_web_npm.sh",
        timeout_class="long",
        owner="web",
    ),
    Gate(
        id="web-hermetic",
        description="Hermetic Playwright browser suite with mocked/local dependencies.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("Node 26.3.1", "Chromium", "local/mocked API"),
        canonical_runner="npm run test:hermetic",
        timeout_class="long",
        owner="web",
    ),
    Gate(
        id="verification-contract",
        description=(
            "Self-test for registry, workflow, suite and load-profile ownership, "
            "including pinned actionlint workflow-expression validation."
        ),
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=(
            "repository checkout",
            "actionlint 1.7.12 (checksum-verified in canonical CI)",
        ),
        canonical_runner="tools/platform_verify_contract.py",
        timeout_class="short",
        owner="platform-tooling",
    ),
    Gate(
        id="release-runtime",
        description=(
            "Privileged hermetic live-QA runtime build and browser-link materialization fixture."
        ),
        deterministic=True,
        local_safe=True,
        ci_required=False,
        environment_requirements=(
            "root test user",
            "disposable staged checkout and local pinned ZIP fixtures",
            "no production network or credentials",
        ),
        canonical_runner=(
            "tools/platform_test_runner.py --contour backend-privileged --focused "
            "release build contract IDs"
        ),
        timeout_class="medium",
        owner="release",
        conditional=True,
    ),
    Gate(
        id="server-smoke",
        description="Small critical-interface smoke after an immutable deployment.",
        deterministic=False,
        local_safe=False,
        ci_required=False,
        environment_requirements=("deployed production release", "protected SSH"),
        canonical_runner="platform-production-deploy.yml",
        timeout_class="short",
        owner="release",
    ),
    Gate(
        id="live-public",
        description="Bounded real-origin browser validation.",
        deterministic=False,
        local_safe=False,
        ci_required=False,
        environment_requirements=("https://old-sparky.com", "dedicated QA identity"),
        canonical_runner="platform-live-launch.yml",
        timeout_class="long",
        owner="production-operator",
    ),
    Gate(
        id="live-user-destructive",
        description="Marked production user journey with explicit cleanup.",
        deterministic=False,
        local_safe=False,
        ci_required=False,
        environment_requirements=("production", "operator confirmation", "exact cleanup"),
        canonical_runner="platform-live-user-qa.yml",
        timeout_class="long",
        owner="production-operator",
    ),
    Gate(
        id="external-load",
        description="Explicit external-runner load, stress and capacity experiment.",
        deterministic=False,
        local_safe=False,
        ci_required=False,
        environment_requirements=(
            "external GitHub runner",
            "production origin fixture/observer",
            "exact cleanup or abort",
        ),
        canonical_runner="platform-production-external-load.yml",
        timeout_class="extended",
        owner="production-performance-operator",
    ),
)

GATES_BY_ID = {gate.id: gate for gate in GATES}
DETERMINISTIC_GATE_IDS = tuple(gate.id for gate in GATES if gate.deterministic)
CI_GATE_IDS = tuple(gate.id for gate in GATES if gate.ci_required)

RELEASE_RUNTIME_TEST_IDS: tuple[str, ...] = (
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_host_tools_contract_failure_emits_marker_before_locks",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_cloudflare_failed_oneshot_is_quiescent_only_with_empty_cgroup_contract",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_staged_live_qa_build_materializes_validated_browser_links",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_staged_live_qa_builder_output_passes_standalone_artifact_validator",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_staged_live_qa_build_fails_closed_for_browser_link_inputs",
    "tests.test_platform_release_build_contract.PlatformReleaseBuildContractTests.test_browser_materializer_rejects_filesystem_metadata_and_specials",
    "tests.test_platform_validate_release_artifact.PlatformReleaseArtifactValidationTests.test_runtime_manifest_order_is_explicit_and_shared",
    "tests.test_platform_validate_release_artifact.PlatformReleaseArtifactValidationTests.test_runtime_manifest_digest_rejects_content_or_digest_tampering",
)


class VerificationError(RuntimeError):
    """Raised for invalid gate arguments or an unavailable local contour."""


def registry_payload() -> dict[str, object]:
    backend_registry_payload = _backend_catalog_module().registry_payload

    return {
        "schema": 1,
        "purpose": "OldSparky canonical verification registry",
        "gates": [gate.as_json() for gate in GATES],
        "ci_gate_ids": list(CI_GATE_IDS),
        "backend_test_catalog": backend_registry_payload(),
        "impact_planning": {
            "command": "python3 tools/platform_verify.py impact --changed-path <repo-relative-path>",
            "skip_authority": False,
            "unknown_path_behavior": "require full canonical CI; never return an empty plan",
        },
    }


_IMPACT_SOURCE_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("platform/tools/platform_live_qa_", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_live_user_qa_", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_maintenance_qa_", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_production_qa.py", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_release_", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_backup_", ("backend-privileged", "backend-tool-contract")),
    ("platform/tools/platform_external_load_", ("backend-tool-contract", "performance-contract")),
    ("platform/tools/platform_verify", ("verification-contract", "backend-tool-contract")),
    ("platform/tools/platform_test_catalog.py", ("verification-contract", "backend-tool-contract")),
    ("platform/tools/platform_test_runner.py", ("verification-contract", "backend-tool-contract", "backend-privileged")),
    ("platform/tools/platform_ci_classifier.py", ("verification-contract", "backend-tool-contract")),
    (".github/workflows/platform-security.yml", ("verification-contract", "backend-tool-contract", "backend-privileged")),
    (".github/workflows/platform-live-launch.yml", ("backend-privileged", "backend-tool-contract")),
    (".github/workflows/platform-live-user-qa.yml", ("backend-privileged", "backend-tool-contract")),
    (".github/workflows/platform-production-external-load.yml", ("backend-tool-contract", "performance-contract")),
)


def impact_plan(changed_paths: Sequence[str]) -> dict[str, object]:
    """Route changed paths to complete owner contours; this never authorizes skips."""

    catalog = _backend_catalog_module()
    normalized: list[str] = []
    for raw_path in changed_paths:
        path = raw_path.strip()
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            return {
                "schema": 1,
                "status": "broad_ci_required",
                "changed_paths": list(changed_paths),
                "required_gates": list(CI_GATE_IDS),
                "owner_contours": list(catalog.BACKEND_CONTOURS),
                "owner_modules": [],
                "skip_authority": False,
                "reason": "malformed path; run the full canonical CI route",
            }
        normalized.append(path)
    if not normalized:
        raise VerificationError("impact requires at least one --changed-path")

    selected_contours: set[str] = set()
    selected_gates: set[str] = set()
    selected_modules: set[str] = set()
    unmapped: list[str] = []
    cases = catalog.discover_test_cases()
    module_to_contours: dict[str, set[str]] = {}
    for case in cases:
        module_to_contours.setdefault(case.module, set()).add(case.contour)
    for path in normalized:
        if path.startswith("platform/docs/"):
            selected_gates.add("docs")
            selected_contours.add("verification-contract")
            selected_modules.add("test_platform_verification_contract")
            continue
        if path.startswith("platform/tests/") and path.endswith(".py"):
            module = Path(path).stem
            contours = module_to_contours.get(module)
            external_owner = catalog.EXTERNAL_MODULE_OWNERS.get(module)
            if contours:
                selected_contours.update(contours)
                selected_modules.add(module)
            elif external_owner:
                selected_contours.add(external_owner)
                selected_modules.add(module)
            else:
                unmapped.append(path)
            continue
        route = next(
            (contours for prefix, contours in _IMPACT_SOURCE_ROUTES if path.startswith(prefix)),
            None,
        )
        if route is None:
            unmapped.append(path)
            continue
        selected_contours.update(route)
        if "live_qa" in path or "live_user_qa" in path:
            selected_modules.update(
                module for module in module_to_contours
                if module.startswith(("test_platform_live_qa_", "test_platform_cleanup_live_user_qa", "test_platform_recover_live_user_qa"))
            )
            selected_modules.update({"test_platform_maintenance_qa_evidence", "test_platform_noop_source_binding", "test_platform_remote_workflow_guards"})
        if path.endswith("platform-security.yml"):
            selected_modules.update({
                "test_platform_ci_classifier",
                "test_platform_verification_contract",
            })
        if path.startswith((
            "platform/tools/platform_verify",
            "platform/tools/platform_test_runner.py",
            "platform/tools/platform_test_catalog.py",
        )):
            selected_modules.add("test_platform_verification_contract")
        if path.endswith("platform_ci_classifier.py"):
            selected_modules.add("test_platform_ci_classifier")
        if path.endswith("platform-production-external-load.yml"):
            selected_modules.update({"test_platform_external_load_workflow_contract", "test_platform_maintenance_qa_evidence"})

    if unmapped:
        return {
            "schema": 1,
            "status": "broad_ci_required",
            "changed_paths": normalized,
            "unmapped_paths": sorted(unmapped),
            "required_gates": list(CI_GATE_IDS),
            "owner_contours": list(catalog.BACKEND_CONTOURS),
            "owner_modules": [],
            "skip_authority": False,
            "reason": "unknown or unmapped path; run the full canonical CI route",
        }

    required_gates = sorted(selected_gates | {
        "backend" if contour in catalog.BACKEND_CONTOURS else contour
        for contour in selected_contours
    })
    contour_modules = {
        case.module for case in cases if case.contour in selected_contours
    }
    contour_modules.update(selected_modules)
    return {
        "schema": 1,
        "status": "routed",
        "changed_paths": normalized,
        "required_gates": required_gates,
        "owner_contours": sorted(selected_contours),
        "owner_modules": sorted(contour_modules),
        "skip_authority": False,
        "reason": "planning aid only; exact-SHA CI remains required",
    }


def _python() -> str:
    return sys.executable


def _tool(name: str) -> str:
    return str(TOOLS_ROOT / name)


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop only the session/process group created for one verifier command.

    The direct child is intentionally left unreaped until after both signals;
    its PID therefore cannot be reused as another process group's ID during
    cleanup. This covers ordinary descendants that stay in the command's
    group. A descendant that creates a new session must own its own cleanup.
    """

    if process.returncode is not None:
        return False

    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return True
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        return False

    cleanup_complete = True
    try:
        time.sleep(_PROCESS_GROUP_TERM_GRACE_SECONDS)
    finally:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_complete = False
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
    return cleanup_complete


def _cleanup_timed_out_backend_privileged_resources(
    resource_settings: dict[str, str],
) -> None:
    """Revalidate and empty only disposable Redis DB 15 after child reaping."""

    from tools.platform_test_runner import _teardown_privileged_redis_resource
    from tools.platform_verification_lock import verification_resource_lock

    with verification_resource_lock("backend-privileged"):
        _teardown_privileged_redis_resource(resource_settings)


def _validated_privileged_environment() -> tuple[dict[str, str], dict[str, str]]:
    """Resolve one safe test environment for both child and timeout cleanup.

    This mirrors the shell runner's dotenv precedence without asking a later
    child process to reread a mutable file.  The returned resource mapping is
    syntax-validated before a child can import a Redis client or access a
    service.
    """

    child_env = os.environ.copy()
    app_parent = PLATFORM_ROOT.parent
    if app_parent.name == "releases" and (app_parent.parent / "shared").is_dir():
        app_dir = app_parent.parent
    else:
        app_dir = app_parent
    child_env["PLATFORM_ROOT_DIR"] = str(PLATFORM_ROOT)
    child_env["PLATFORM_APP_DIR"] = child_env.get("PLATFORM_APP_DIR") or str(app_dir)
    child_env["PLATFORM_SHARED_DIR"] = str(PLATFORM_ROOT)
    if not child_env.get("PLATFORM_NODE_BIN"):
        node_26 = PLATFORM_ROOT / "node-v26.3.1/bin/node"
        node_current = PLATFORM_ROOT / "node-current/bin/node"
        if os.access(node_26, os.X_OK):
            child_env["PLATFORM_NODE_BIN"] = str(node_26)
        elif os.access(node_current, os.X_OK):
            child_env["PLATFORM_NODE_BIN"] = str(node_current)
        else:
            child_env["PLATFORM_NODE_BIN"] = "/usr/bin/node"
    if child_env.get("PLATFORM_TEST_AGGREGATE_ONLY") == "1":
        child_env["PLATFORM_PYTHON_BIN"] = (
            child_env.get("PLATFORM_PYTHON_BIN") or "/usr/bin/python3"
        )
    else:
        child_env["PLATFORM_PYTHON_BIN"] = str(
            PLATFORM_ROOT / ".venv_platform" / "bin" / "python"
        )
    env_file_value = child_env.get("PLATFORM_ENV_FILE") or str(
        PLATFORM_ROOT / ".env.platform"
    )
    env_file = Path(env_file_value)
    if not env_file.is_absolute():
        env_file = PLATFORM_ROOT / env_file
    child_env["PLATFORM_ENV_FILE"] = str(env_file)

    try:
        try:
            from tools.platform_safe_env_exec import load_env_file
            from tools.platform_test_runner import validate_test_resource_configuration
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_safe_env_exec import load_env_file
            from platform_test_runner import validate_test_resource_configuration

        if os.path.lexists(env_file):
            child_env.update(load_env_file(env_file))
        resource_settings = {
            "platform_environment": child_env.get("PLATFORM_ENVIRONMENT"),
            "platform_database_url": child_env.get("PLATFORM_DATABASE_URL"),
            "platform_db_schema": child_env.get("PLATFORM_DB_SCHEMA"),
            "platform_redis_url": child_env.get("PLATFORM_REDIS_URL"),
        }
        validate_test_resource_configuration(resource_settings)
    except Exception as exc:
        # Parser and validator errors are intentionally kept free of dotenv
        # values and are reported as a single local-boundary failure.
        raise VerificationError(
            "privileged test environment is unsafe or does not target the "
            "disposable platformdb_test/Redis DB 15 resources"
        ) from exc
    return child_env, resource_settings


def _privileged_runner_python(child_env: dict[str, str]) -> str:
    """Mirror the wrapper's pinned interpreter selection and executable check."""

    python = child_env.get("PLATFORM_PYTHON_BIN") or ""
    if not os.access(python, os.X_OK):
        raise VerificationError("pinned test Python runtime is unavailable")
    return python


def _wait_for_owned_process_exit(
    process: subprocess.Popen[bytes],
    timeout_seconds: float | None,
) -> None:
    """Observe direct-child exit without reaping its PID/process-group ID."""

    deadline = (
        time.monotonic() + timeout_seconds
        if timeout_seconds is not None
        else None
    )
    wait_options = os.WEXITED | os.WNOWAIT
    if deadline is not None:
        wait_options |= os.WNOHANG
    while True:
        result = os.waitid(os.P_PID, process.pid, wait_options)
        if result is not None and result.si_pid == process.pid:
            return
        if deadline is None:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        time.sleep(min(0.01, remaining))


def _run(
    label: str,
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = PLATFORM_ROOT,
    timeout_seconds: float | None = None,
    timeout_cleanup: Callable[[], None] | None = None,
    timeout_diagnostic: Callable[[], None] | None = None,
) -> int:
    print(f"[GATE START] {label}", flush=True)
    previous_term_handler = signal.getsignal(signal.SIGTERM)
    process: subprocess.Popen[bytes] | None = None
    pending_term: int | None = None
    cleaning_up = False
    term_handler_installed = False

    def handle_term(signum: int, _frame: object) -> None:
        nonlocal pending_term
        if process is None:
            pending_term = signum
            return
        if cleaning_up:
            return
        raise _VerifierTermination(signum)

    signal.signal(signal.SIGTERM, handle_term)
    term_handler_installed = True
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        if pending_term is not None:
            cleaning_up = True
            cleanup_complete = _terminate_owned_process_group(process)
            if not cleanup_complete:
                print(
                    f"[GATE INTERRUPTION CLEANUP INCOMPLETE] {label}",
                    file=sys.stderr,
                )
            return 128 + pending_term
        try:
            _wait_for_owned_process_exit(process, timeout_seconds)
        except subprocess.TimeoutExpired:
            cleaning_up = True
            cleanup_complete = _terminate_owned_process_group(process)
            if not cleanup_complete:
                print(
                    f"[GATE TIMEOUT CLEANUP INCOMPLETE] {label}",
                    file=sys.stderr,
                )
            else:
                if timeout_diagnostic is not None:
                    try:
                        timeout_diagnostic()
                    except Exception as exc:
                        print(
                            f"[GATE TIMEOUT DIAGNOSTIC FAIL] {label} "
                            f"class={type(exc).__name__}",
                            file=sys.stderr,
                            flush=True,
                        )
                if timeout_cleanup is not None:
                    try:
                        timeout_cleanup()
                    except Exception as exc:
                        print(
                            f"[GATE TIMEOUT RESOURCE CLEANUP FAIL] {label} "
                            f"class={type(exc).__name__}",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[GATE TIMEOUT RESOURCE CLEANUP] {label} status=passed",
                            flush=True,
                        )
            timeout_label = (
                f"{timeout_seconds:g}s" if timeout_seconds is not None else "the configured timeout"
            )
            print(
                f"[GATE TIMEOUT] {label} exceeded {timeout_label}",
                file=sys.stderr,
            )
            return 124
        except _VerifierTermination as interrupted:
            cleaning_up = True
            cleanup_complete = _terminate_owned_process_group(process)
            if not cleanup_complete:
                print(
                    f"[GATE INTERRUPTION CLEANUP INCOMPLETE] {label}",
                    file=sys.stderr,
                )
            return 128 + interrupted.signum
        except BaseException:
            cleaning_up = True
            _terminate_owned_process_group(process)
            raise
        # Once the command exits, stop intercepting cancellation before reap.
        # Until this point WNOWAIT keeps the PGID pinned to the owned session.
        signal.signal(signal.SIGTERM, previous_term_handler)
        term_handler_installed = False
        returncode = process.wait()
    except _VerifierTermination as interrupted:
        if process is None:
            return 128 + interrupted.signum
        cleaning_up = True
        cleanup_complete = _terminate_owned_process_group(process)
        if not cleanup_complete:
            print(
                f"[GATE INTERRUPTION CLEANUP INCOMPLETE] {label}",
                file=sys.stderr,
            )
        return 128 + interrupted.signum
    except FileNotFoundError as exc:
        raise VerificationError(
            f"LOCAL GATE BLOCKED: required executable is unavailable: {exc.filename}"
        ) from exc
    finally:
        if term_handler_installed:
            signal.signal(signal.SIGTERM, previous_term_handler)
    if returncode:
        print(f"[GATE FAIL] {label} (exit {returncode})", file=sys.stderr)
        return returncode
    print(f"[GATE PASS] {label}", flush=True)
    return 0


def _backend_command(arguments: Sequence[str]) -> list[str]:
    if not arguments:
        return [_tool("platform_run_tests.sh"), "--contour", "backend"]
    if arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        return [_tool("platform_run_tests.sh"), "--contour", "backend"]
    if arguments[0] == "--focused":
        selectors = list(arguments[1:])
        if not selectors:
            raise VerificationError("backend --focused requires at least one selector.")
        runner_arguments = ["--focused", *selectors]
    elif arguments[0] in {
        "--list",
        "--manifest",
        "--summary",
        "--component-dir",
        "--quiet",
    }:
        runner_arguments = list(arguments)
    else:
        raise VerificationError(
            "backend accepts no arguments, --focused <selector> [...], "
            "or --list/--manifest/--summary."
        )
    return [
        _tool("platform_run_tests.sh"),
        "--contour",
        "backend",
        *runner_arguments,
    ]


def _backend_contour_command(
    contour: str,
    arguments: Sequence[str],
) -> list[str]:
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if arguments and arguments[0] not in {
        "--focused",
        "--list",
        "--manifest",
        "--summary",
        "--quiet",
    }:
        raise VerificationError(
            f"{contour} accepts --focused, --list, --manifest, --summary or --quiet."
        )
    runner_arguments: list[str] = []
    if arguments and arguments[0] == "--focused":
        selectors = list(arguments[1:])
        if not selectors:
            raise VerificationError(f"{contour} --focused requires at least one selector.")
        runner_arguments = ["--focused", *selectors]
    elif arguments:
        runner_arguments = list(arguments)
    command = [_tool("platform_run_tests.sh")]
    command.extend(["--contour", contour, *runner_arguments])
    return command


def _verification_contract_commands(
    arguments: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """Return the self-test and catalog-owned unittest invocations."""

    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if arguments and arguments[0] not in {"--manifest", "--summary", "--quiet"}:
        raise VerificationError(
            "verification-contract accepts --manifest, --summary or --quiet."
        )
    return (
        [_python(), "tools/platform_verify_contract.py"],
        [
            _python(),
            _tool("platform_test_runner.py"),
            "--contour",
            "verification-contract",
            *arguments,
        ],
    )


def _dispatch_deterministic(gate_id: str, arguments: Sequence[str]) -> int:
    BACKEND_CONTOURS = _backend_catalog_module().BACKEND_CONTOURS

    if gate_id in BACKEND_CONTOURS:
        if gate_id == "backend-privileged":
            _require_backend_privileged_root()
            _preflight_backend_test_environment(gate_id, arguments)
            wrapper_command = _backend_contour_command(gate_id, arguments)
            runner_arguments = wrapper_command[3:]
            child_env, resource_settings = _validated_privileged_environment()
            progress_path, private_progress_directory = _create_backend_progress_path(
                runner_arguments
            )
            command = [
                _privileged_runner_python(child_env),
                _tool("platform_test_runner.py"),
                "--contour",
                gate_id,
                *runner_arguments,
                "--progress",
                str(progress_path),
            ]

            def timeout_cleanup() -> None:
                _cleanup_timed_out_backend_privileged_resources(resource_settings)

            diagnostic_ran = False
            diagnostic_read = False

            def timeout_diagnostic() -> None:
                nonlocal diagnostic_ran, diagnostic_read
                diagnostic_ran = True
                diagnostic_read = _emit_backend_progress_timeout(progress_path)

            result: int | None = None
            try:
                result = _run(
                    gate_id,
                    command,
                    env=child_env,
                    timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
                    timeout_cleanup=timeout_cleanup,
                    timeout_diagnostic=timeout_diagnostic,
                )
                return result
            finally:
                # A valid explicit-summary checkpoint is uploaded with the
                # component artifact only after the outer timeout stopped its
                # owned process group. Private /tmp checkpoints never persist.
                remove_progress = (
                    private_progress_directory is not None
                    and (result != 124 or diagnostic_ran)
                ) or (
                    private_progress_directory is None
                    and (result != 124 or (diagnostic_ran and not diagnostic_read))
                )
                process_group_stopped = result != 124 or diagnostic_ran
                temporary_files_removed = (
                    process_group_stopped
                    and _remove_backend_progress_temps(progress_path)
                )
                if (
                    remove_progress
                    and temporary_files_removed
                    and _remove_backend_progress_path(progress_path)
                ):
                    if private_progress_directory is not None:
                        try:
                            private_progress_directory.rmdir()
                        except OSError:
                            pass
        _preflight_backend_test_environment(gate_id, arguments)
        return _run(
            gate_id,
            _backend_contour_command(gate_id, arguments),
            timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
        )
    if gate_id == "release-runtime":
        if arguments:
            raise VerificationError("release-runtime does not accept extra arguments.")
        _require_backend_privileged_root()
        child_env, resource_settings = _validated_privileged_environment()

        def timeout_cleanup() -> None:
            _cleanup_timed_out_backend_privileged_resources(resource_settings)

        return _run(
            gate_id,
            [
                _privileged_runner_python(child_env),
                _tool("platform_test_runner.py"),
                "--contour",
                "backend-privileged",
                "--focused",
                *RELEASE_RUNTIME_TEST_IDS,
            ],
            env=child_env,
            timeout_seconds=600,
            timeout_cleanup=timeout_cleanup,
        )
    if arguments and gate_id not in {"backend", "verification-contract"}:
        raise VerificationError(f"{gate_id} does not accept extra arguments.")
    if gate_id == "backend":
        return _run(
            gate_id,
            _backend_command(arguments),
            timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
        )
    if gate_id == "python-quality":
        return _run(
            gate_id,
            [
                _python(),
                "-m",
                "ruff",
                "check",
                "apps/platform_api",
                "apps/platform_worker",
                "python_packages",
                "tools",
                "tests",
            ],
        )
    if gate_id == "security":
        commands = (
            (
                "security/dependency-audit",
                [_python(), "-m", "pip_audit", "-r", "requirements-ci.lock.txt"],
            ),
            (
                "security/bandit",
                [
                    _python(),
                    "-m",
                    "bandit",
                    "-q",
                    "-r",
                    "apps/platform_api",
                    "apps/platform_worker",
                    "python_packages",
                    "tools",
                    "-x",
                    "tests",
                    "-lll",
                ],
            ),
            (
                "security/secrets",
                [_python(), "tools/platform_secret_scan.py", "--root", ".."],
            ),
        )
        for label, command in commands:
            status = _run(label, command)
            if status:
                return status
        return 0
    if gate_id == "migration":
        try:
            from tools.platform_migration_support import (
                MIGRATION_DIAGNOSTIC_ENV,
                MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_migration_support import (
                MIGRATION_DIAGNOSTIC_ENV,
                MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
            )
        child_env = os.environ.copy()
        child_env.pop(MIGRATION_DIAGNOSTIC_ENV, None)
        try:
            diagnostic_directory, diagnostic_file = _create_private_migration_diagnostic()
        except OSError:
            print(
                "[GATE MIGRATION DIAGNOSTIC UNAVAILABLE] private report setup failed",
                file=sys.stderr,
                flush=True,
            )
            return _run(
                gate_id,
                [_python(), "tools/platform_migration_scenario.py"],
                env=child_env,
                timeout_seconds=MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
            )
        child_env[MIGRATION_DIAGNOSTIC_ENV] = str(diagnostic_file)
        status = _run(
            gate_id,
            [_python(), "tools/platform_migration_scenario.py"],
            env=child_env,
            timeout_seconds=MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
        )
        if status == 0:
            diagnostic_file.unlink(missing_ok=True)
            diagnostic_directory.rmdir()
        else:
            print(
                f"[GATE MIGRATION DIAGNOSTIC] private progress retained at {diagnostic_file}",
                flush=True,
            )
        return status
    if gate_id == "docs":
        return _run(gate_id, [_python(), "tools/platform_docs_check.py"])
    if gate_id == "web-quality":
        commands = (
            (
                "web-quality/shutdown-guard",
                [
                    _tool("platform_web_npm.sh"),
                    "--prefix",
                    "apps/platform_web",
                    "run",
                    "test:shutdown-guard",
                ],
            ),
            (
                "web-quality/ssr-stream-diagnostics",
                [
                    _tool("platform_web_npm.sh"),
                    "--prefix",
                    "apps/platform_web",
                    "run",
                    "test:ssr-stream-diagnostics",
                ],
            ),
            (
                "web-quality/ssr-heap-metrics",
                [
                    _tool("platform_web_npm.sh"),
                    "--prefix",
                    "apps/platform_web",
                    "run",
                    "test:ssr-heap-metrics",
                ],
            ),
            (
                "web-quality/next-rootdir-glob",
                [
                    _tool("platform_web_npm.sh"),
                    "--prefix",
                    "apps/platform_web",
                    "run",
                    "test:next-rootdir-glob",
                ],
            ),
            (
                "web-quality/dependency-audit",
                [_tool("platform_web_npm.sh"), "--prefix", "apps/platform_web", "audit", "--audit-level=high"],
            ),
            (
                "web-quality/typecheck",
                [_tool("platform_web_npm.sh"), "--prefix", "apps/platform_web", "run", "typecheck"],
            ),
            (
                "web-quality/lint",
                [_tool("platform_web_npm.sh"), "--prefix", "apps/platform_web", "run", "lint"],
            ),
            (
                "web-quality/build",
                [_tool("platform_web_npm.sh"), "--prefix", "apps/platform_web", "run", "build"],
            ),
        )
        for label, command in commands:
            status = _run(label, command)
            if status:
                return status
        return 0
    if gate_id == "web-hermetic":
        env = os.environ.copy()
        env["CI"] = "true"
        return _run(
            gate_id,
            [_tool("platform_web_npm.sh"), "--prefix", "apps/platform_web", "run", "test:hermetic"],
            env=env,
        )
    if gate_id == "verification-contract":
        timeout_seconds = _backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[
            "verification-contract"
        ]
        contract_command, test_command = _verification_contract_commands(arguments)
        status = _run(
            gate_id,
            contract_command,
            timeout_seconds=timeout_seconds,
        )
        if status:
            return status
        status = _run(
            f"{gate_id}/actionlint",
            [sys.executable, _tool("platform_actionlint.py")],
            timeout_seconds=timeout_seconds,
        )
        if status:
            return status
        return _run(
            f"{gate_id}/tests",
            test_command,
            timeout_seconds=timeout_seconds,
        )
    raise VerificationError(f"Unknown deterministic gate: {gate_id}")


def dispatch(gate_id: str, arguments: Sequence[str] = ()) -> int:
    BACKEND_CONTOURS = _backend_catalog_module().BACKEND_CONTOURS

    if gate_id in BACKEND_CONTOURS:
        return _dispatch_deterministic(gate_id, arguments)
    gate = GATES_BY_ID.get(gate_id)
    if gate is None:
        raise VerificationError(f"Unknown gate: {gate_id}")
    if not gate.deterministic:
        raise VerificationError(
            f"{gate_id} is workflow-only; use {gate.canonical_runner}. "
            "Production/live/load contours are not local deterministic gates."
        )
    return _dispatch_deterministic(gate_id, arguments)


def _prepare_ci_backend_database() -> int:
    """Recreate the migrated disposable schema after migration cleanup."""

    child_env, resource_settings = _validated_privileged_environment()
    if child_env.get("PLATFORM_TEST_AGGREGATE_ONLY") == "1":
        raise VerificationError(
            "CI database preparation is unavailable in aggregate-only manifest mode"
        )
    try:
        try:
            from tools.platform_migration_support import (
                MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
                validate_disposable_migration_target,
            )
            from tools.platform_verification_lock import (
                VerificationLockError,
                verification_resource_lock,
            )
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_migration_support import (
                MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
                validate_disposable_migration_target,
            )
            from platform_verification_lock import (  # type: ignore[no-redef]
                VerificationLockError,
                verification_resource_lock,
            )

        validate_disposable_migration_target(
            resource_settings.get("platform_database_url"),
            environment=resource_settings.get("platform_environment"),
            schema=resource_settings.get("platform_db_schema"),
        )
        python = _privileged_runner_python(child_env)
        with verification_resource_lock("migration"):
            return _run(
                "ci-backend-database-bootstrap",
                [python, "-m", "alembic", "upgrade", "head"],
                env=child_env,
                timeout_seconds=MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
            )
    except VerificationLockError as exc:
        raise VerificationError(
            "CI backend database preparation could not acquire its migration lock"
        ) from exc
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(
            "CI backend database preparation is unsafe or unavailable"
        ) from exc


def dispatch_ci() -> int:
    """Run the deterministic aggregate and refuse production-only contours."""

    if any(not GATES_BY_ID[gate_id].deterministic for gate_id in CI_GATE_IDS):
        raise VerificationError("CI aggregate contains a non-deterministic gate.")
    # The migration scenario always clears its disposable schema in its
    # finally block. Reapply the source head before backend preflight, using
    # the same validated test target and migration lock.
    ordered_gate_ids = (
        "migration",
        "backend",
        *(gate_id for gate_id in CI_GATE_IDS if gate_id not in {"migration", "backend"}),
    )
    for gate_id in ordered_gate_ids:
        status = dispatch(gate_id)
        if status:
            return status
        if gate_id == "migration":
            status = _prepare_ci_backend_database()
            if status:
                return status
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    BACKEND_CONTOURS = _backend_catalog_module().BACKEND_CONTOURS

    parser = argparse.ArgumentParser(
        description="Dispatch a canonical OldSparky verification gate."
    )
    parser.add_argument(
        "gate",
        choices=("list", "ci", "impact", *GATES_BY_ID, *BACKEND_CONTOURS),
    )
    parser.add_argument("gate_arguments", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.gate == "list":
        arguments = list(args.gate_arguments)
        if arguments == ["--json"]:
            print(json.dumps(registry_payload(), indent=2, ensure_ascii=False))
            return 0
        if arguments:
            raise VerificationError("list accepts only --json.")
        for gate in GATES:
            contour = "deterministic" if gate.deterministic else "workflow-only"
            ci = ", ci-required" if gate.ci_required else ""
            print(f"{gate.id}: {contour}{ci} — {gate.description}")
        BACKEND_CONTOURS = _backend_catalog_module().BACKEND_CONTOURS

        for contour_id in BACKEND_CONTOURS:
            print(f"{contour_id}: deterministic sub-contour — catalog-owned backend tests")
        return 0
    if args.gate == "ci":
        if args.gate_arguments:
            raise VerificationError("ci accepts no extra arguments.")
        return dispatch_ci()
    if args.gate == "impact":
        paths: list[str] = []
        arguments = list(args.gate_arguments)
        index = 0
        while index < len(arguments):
            if arguments[index] != "--changed-path" or index + 1 >= len(arguments):
                raise VerificationError("impact accepts repeated --changed-path PATH arguments only.")
            paths.append(arguments[index + 1])
            index += 2
        print(json.dumps(impact_plan(paths), indent=2, ensure_ascii=False))
        return 0
    return dispatch(args.gate, args.gate_arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
