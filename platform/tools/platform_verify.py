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
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Sequence


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PLATFORM_ROOT / "tools"
WEB_ROOT = PLATFORM_ROOT / "apps" / "platform_web"

# Keep the dependency-security surface explicit. These locks are owned by
# different contours and intentionally cannot be merged into one pip-audit
# input because several packages are pinned to different versions between
# contours. A new lock owner must update this list and its contract tests;
# silently discovering files would make a new install surface auditable only
# by accident.
SECURITY_DEPENDENCY_LOCKS: tuple[str, ...] = (
    "requirements-platform.lock.txt",
    "requirements-ci.lock.txt",
    "requirements-ci-locker.lock.txt",
    "apps/platform_draft/requirements-assets.lock.txt",
)
PIP_AUDIT_SOCKET_TIMEOUT_SECONDS = 10
SECURITY_DEPENDENCY_AUDIT_TIMEOUT_SECONDS = 120
# Lock files are authored as small, ASCII, one-package-per-line files. Keep
# the preflight bounded even if a path is replaced between stat and open.
SECURITY_DEPENDENCY_LOCK_MAX_BYTES = 1024 * 1024
SECURITY_DEPENDENCY_LOCK_LINE = re.compile(
    r"^[A-Za-z0-9_.-]+=="
    r"[A-Za-z0-9][A-Za-z0-9_.+!-]* --hash=sha256:[0-9a-f]{64}$"
)
PIP_AUDIT_FLAGS: tuple[str, ...] = (
    "--disable-pip",
    "--require-hashes",
    "--strict",
    "--format",
    "columns",
    "--progress-spinner",
    "off",
    "--timeout",
    str(PIP_AUDIT_SOCKET_TIMEOUT_SECONDS),
)


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


def _run(
    label: str,
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = PLATFORM_ROOT,
    timeout_seconds: float | None = None,
) -> int:
    print(f"[GATE START] {label}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timeout_label = (
            f"{timeout_seconds:g}s" if timeout_seconds is not None else "the configured timeout"
        )
        print(
            f"[GATE TIMEOUT] {label} exceeded {timeout_label}",
            file=sys.stderr,
        )
        return 124
    except FileNotFoundError as exc:
        raise VerificationError(
            f"LOCAL GATE BLOCKED: required executable is unavailable: {exc.filename}"
        ) from exc
    if result.returncode:
        print(f"[GATE FAIL] {label} (exit {result.returncode})", file=sys.stderr)
        return result.returncode
    print(f"[GATE PASS] {label}", flush=True)
    return 0


def _security_dependency_lock_preflight(candidate: Path) -> str | None:
    """Validate one lock without reopening its mutable repository pathname."""

    try:
        relative_path = candidate.relative_to(PLATFORM_ROOT).as_posix()
    except ValueError:
        return "outside-root"
    _, failure = _read_stable_security_dependency_lock(PLATFORM_ROOT, relative_path)
    return failure


def _security_lock_metadata_matches(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    """Compare identity and every metadata field that can change during a read."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _validate_security_dependency_lock_payload(payload: bytes) -> str | None:
    """Validate the exact ASCII lock grammar after reading stable bytes."""

    if not payload:
        return "empty"
    if len(payload) > SECURITY_DEPENDENCY_LOCK_MAX_BYTES:
        return "oversized"
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        return "not-ascii"
    if not text.endswith("\n"):
        return "missing-final-newline"
    lines = text[:-1].split("\n")
    if not lines or any(SECURITY_DEPENDENCY_LOCK_LINE.fullmatch(line) is None for line in lines):
        return "malformed-line"
    return None


def _read_stable_security_dependency_lock(
    root: Path,
    relative_path: str,
) -> tuple[bytes | None, str | None]:
    """Read and validate one lock through a stable descriptor walk.

    Every parent component is opened with ``O_NOFOLLOW`` and retained by file
    descriptor, so replacing a repository parent with a symlink cannot redirect
    the final open.  The parser and returned bytes come from the same descriptor
    whose identity and metadata are checked before and after the bounded read.
    """

    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or "\\" in relative_path
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None, "unsafe-path"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        return None, "nofollow-unavailable"
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | nofollow
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(root, directory_flags)
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            return None, "unsafe-root"
        for component in path.parts[:-1]:
            next_directory = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            try:
                if not stat.S_ISDIR(os.fstat(next_directory).st_mode):
                    return None, "unsafe-parent"
                previous_directory = directory_descriptor
                directory_descriptor = next_directory
                next_directory = None
                try:
                    os.close(previous_directory)
                except OSError:
                    pass
            finally:
                if next_directory is not None:
                    try:
                        os.close(next_directory)
                    except OSError:
                        pass
        candidate = os.stat(
            path.parts[-1],
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(candidate.st_mode)
            or candidate.st_nlink != 1
            or candidate.st_size == 0
            or candidate.st_size > SECURITY_DEPENDENCY_LOCK_MAX_BYTES
        ):
            return None, "unsafe-metadata"
        descriptor = os.open(
            path.parts[-1],
            file_flags,
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not _security_lock_metadata_matches(candidate, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size == 0
            or before.st_size > SECURITY_DEPENDENCY_LOCK_MAX_BYTES
        ):
            return None, "changed-before-read"
        data = bytearray()
        while len(data) <= SECURITY_DEPENDENCY_LOCK_MAX_BYTES:
            chunk = os.read(
                descriptor,
                SECURITY_DEPENDENCY_LOCK_MAX_BYTES + 1 - len(data),
            )
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            not _security_lock_metadata_matches(before, after)
            or len(data) != after.st_size
            or len(data) > SECURITY_DEPENDENCY_LOCK_MAX_BYTES
        ):
            return None, "changed-during-read"
        payload = bytes(data)
        return payload, _validate_security_dependency_lock_payload(payload)
    except OSError:
        return None, "unreadable"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


def _write_security_dependency_snapshot(
    directory: Path,
    index: int,
    payload: bytes,
) -> Path:
    """Write one private immutable audit input from already validated bytes."""

    path = directory / f"lock-{index:02d}.txt"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("security dependency snapshot write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise OSError("security dependency snapshot metadata is unsafe")
        return path
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run_security_dependency_audits() -> int:
    """Audit every authored Python lock surface and retain the first failure."""

    first_failure = 0
    try:
        with tempfile.TemporaryDirectory(prefix=".platform-security-audit-") as temporary:
            snapshot_directory = Path(temporary)
            for index, lock_path in enumerate(SECURITY_DEPENDENCY_LOCKS):
                label = f"security/dependency-audit/{lock_path}"
                payload, preflight_failure = _read_stable_security_dependency_lock(
                    PLATFORM_ROOT,
                    lock_path,
                )
                if preflight_failure is not None or payload is None:
                    print(
                        f"[GATE FAIL] {label} lock preflight failed: "
                        f"{preflight_failure or 'empty-read'}",
                        file=sys.stderr,
                    )
                    first_failure = first_failure or 1
                    continue
                try:
                    snapshot = _write_security_dependency_snapshot(
                        snapshot_directory,
                        index,
                        payload,
                    )
                except OSError:
                    print(
                        f"[GATE FAIL] {label} audit snapshot could not be created",
                        file=sys.stderr,
                    )
                    first_failure = first_failure or 1
                    continue
                status = _run(
                    label,
                    [
                        _python(),
                        "-m",
                        "pip_audit",
                        "-r",
                        str(snapshot),
                        *PIP_AUDIT_FLAGS,
                    ],
                    timeout_seconds=SECURITY_DEPENDENCY_AUDIT_TIMEOUT_SECONDS,
                )
                first_failure = first_failure or status
    except OSError:
        print(
            "[GATE FAIL] security dependency audit snapshot cleanup failed",
            file=sys.stderr,
        )
        first_failure = first_failure or 1
    return first_failure


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
        return _run(
            gate_id,
            _backend_contour_command(gate_id, arguments),
            timeout_seconds=_backend_catalog_module().CONTOUR_TIMEOUT_SECONDS[gate_id],
        )
    if gate_id == "release-runtime":
        if arguments:
            raise VerificationError("release-runtime does not accept extra arguments.")
        return _run(
            gate_id,
            [
                _python(),
                _tool("platform_test_runner.py"),
                "--contour",
                "backend-privileged",
                "--focused",
                *RELEASE_RUNTIME_TEST_IDS,
            ],
            timeout_seconds=600,
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
        dependency_status = _run_security_dependency_audits()
        if dependency_status:
            return dependency_status
        for label, command in commands:
            status = _run(label, command)
            if status:
                return status
        return 0
    if gate_id == "migration":
        return _run(
            gate_id,
            [_python(), "tools/platform_migration_scenario.py"],
        )
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
