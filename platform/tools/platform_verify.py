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
        description="Self-test for registry, workflow, suite and load-profile ownership.",
        deterministic=True,
        local_safe=True,
        ci_required=True,
        environment_requirements=("repository checkout",),
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
            elif timeout_cleanup is not None:
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
            if os.geteuid() != 0:
                raise VerificationError("backend-privileged requires the root test user")
            wrapper_command = _backend_contour_command(gate_id, arguments)
            runner_arguments = wrapper_command[3:]
            child_env, resource_settings = _validated_privileged_environment()
            command = [
                _privileged_runner_python(child_env),
                _tool("platform_test_runner.py"),
                "--contour",
                gate_id,
                *runner_arguments,
            ]

            def timeout_cleanup() -> None:
                _cleanup_timed_out_backend_privileged_resources(resource_settings)

            return _run(
                gate_id,
                command,
                env=child_env,
                timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
                timeout_cleanup=timeout_cleanup,
            )
        return _run(
            gate_id,
            _backend_contour_command(gate_id, arguments),
            timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
        )
    if gate_id == "release-runtime":
        if arguments:
            raise VerificationError("release-runtime does not accept extra arguments.")
        if os.geteuid() != 0:
            raise VerificationError("backend-privileged requires the root test user")
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


def dispatch_ci() -> int:
    """Run the deterministic aggregate and refuse production-only contours."""

    if any(not GATES_BY_ID[gate_id].deterministic for gate_id in CI_GATE_IDS):
        raise VerificationError("CI aggregate contains a non-deterministic gate.")
    # A local host may provide one shared platformdb_test/Redis pair. Bootstrap
    # its schema before the backend aggregate; the contour lock then keeps the
    # migration and resource-bearing backend contours mutually exclusive.
    ordered_gate_ids = (
        "migration",
        "backend",
        *(gate_id for gate_id in CI_GATE_IDS if gate_id not in {"migration", "backend"}),
    )
    for gate_id in ordered_gate_ids:
        status = dispatch(gate_id)
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
        choices=("list", "ci", *GATES_BY_ID, *BACKEND_CONTOURS),
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
    return dispatch(args.gate, args.gate_arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
