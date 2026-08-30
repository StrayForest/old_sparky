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
import subprocess
import sys
from typing import Sequence


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PLATFORM_ROOT / "tools"
WEB_ROOT = PLATFORM_ROOT / "apps" / "platform_web"


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
        description="Backend unit and integration tests with normal unittest discovery.",
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


class VerificationError(RuntimeError):
    """Raised for invalid gate arguments or an unavailable local contour."""


def registry_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "purpose": "OldSparky canonical verification registry",
        "gates": [gate.as_json() for gate in GATES],
        "ci_gate_ids": list(CI_GATE_IDS),
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
) -> int:
    print(f"[GATE START] {label}", flush=True)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, check=False)
    except FileNotFoundError as exc:
        raise VerificationError(
            f"LOCAL GATE BLOCKED: required executable is unavailable: {exc.filename}"
        ) from exc
    if result.returncode:
        print(f"[GATE FAIL] {label} (exit {result.returncode})", file=sys.stderr)
        return result.returncode
    print(f"[GATE PASS] {label}", flush=True)
    return 0


def _backend_command(arguments: Sequence[str]) -> list[str]:
    if not arguments:
        return [_tool("platform_run_tests.sh"), "discover", "-s", "tests"]
    if arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments or arguments[0] != "--focused":
        raise VerificationError(
            "backend accepts no arguments or --focused <unittest selector> [...]."
        )
    selectors = list(arguments[1:])
    if not selectors:
        raise VerificationError("backend --focused requires at least one selector.")
    return [_tool("platform_run_tests.sh"), *selectors]


def _dispatch_deterministic(gate_id: str, arguments: Sequence[str]) -> int:
    if arguments and gate_id != "backend":
        raise VerificationError(f"{gate_id} does not accept extra arguments.")
    if gate_id == "backend":
        return _run(gate_id, _backend_command(arguments))
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
                [_python(), "-m", "pip_audit", "-r", "requirements-platform.txt"],
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
        return _run(
            gate_id,
            [_python(), "tools/platform_migration_scenario.py"],
        )
    if gate_id == "docs":
        return _run(gate_id, [_python(), "tools/platform_docs_check.py"])
    if gate_id == "web-quality":
        commands = (
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
        return _run(gate_id, [_python(), "tools/platform_verify_contract.py"])
    raise VerificationError(f"Unknown deterministic gate: {gate_id}")


def dispatch(gate_id: str, arguments: Sequence[str] = ()) -> int:
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
    for gate_id in CI_GATE_IDS:
        status = dispatch(gate_id)
        if status:
            return status
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch a canonical OldSparky verification gate."
    )
    parser.add_argument("gate", choices=("list", "ci", *GATES_BY_ID))
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
