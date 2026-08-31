#!/usr/bin/env python3
"""Verify that repository verification ownership has not drifted."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from typing import Iterable

try:
    from tools.platform_load import load_profiles
    from tools.platform_verify import CI_GATE_IDS, DETERMINISTIC_GATE_IDS, GATES_BY_ID
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_load import load_profiles
    from platform_verify import CI_GATE_IDS, DETERMINISTIC_GATE_IDS, GATES_BY_ID


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = REPO_ROOT / "platform"
SECURITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-security.yml"
GOVERNANCE_DOC = PLATFORM_ROOT / "docs" / "test-suite-governance.md"
WEB_PACKAGE = PLATFORM_ROOT / "apps" / "platform_web" / "package.json"
WEB_PLAYWRIGHT_CONFIG = PLATFORM_ROOT / "apps" / "platform_web" / "playwright.config.ts"
LEGACY_MANIFEST = PLATFORM_ROOT / "tests" / "test-suite-manifest.json"
EXTERNAL_LOAD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-production-external-load.yml"

DIRECT_CANONICAL_COMMANDS = (
    "platform_run_tests.sh",
    "platform_migration_scenario.py",
    "platform_docs_check.py",
    "platform_secret_scan.py",
    "ruff check",
    "pip_audit",
    "bandit",
    "npm audit",
    "npm run typecheck",
    "npm run lint",
    "npm run build",
    "npm run test:hermetic",
)
FORBIDDEN_EXCLUSION_MARKERS = (
    "grep -v",
    "grep -E -v",
    "--grep-invert",
    "testPathIgnorePatterns",
)
GOVERNANCE_TABLE_ID_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def _workflow_texts() -> Iterable[tuple[Path, str]]:
    workflow_root = REPO_ROOT / ".github" / "workflows"
    for path in sorted(workflow_root.glob("*.y*ml")):
        yield path, path.read_text(encoding="utf-8")


def extract_gate_invocations(text: str) -> list[str]:
    """Extract gate IDs from shell tokens, independent of YAML formatting."""

    gates: list[str] = []
    for line in text.splitlines():
        if "platform_verify.py" not in line:
            continue
        try:
            tokens = shlex.split(line.strip())
        except ValueError:
            tokens = []
        for index, token in enumerate(tokens):
            if token.endswith("platform_verify.py") and index + 1 < len(tokens):
                candidate = tokens[index + 1].strip("'\"")
                if not candidate.startswith("-"):
                    gates.append(candidate)
        if not tokens:
            match = re.search(r"platform_verify\.py\s+([^\s'\"]+)", line)
            if match:
                gates.append(match.group(1))
    return gates


def collect_issues() -> list[str]:
    issues: list[str] = []

    if not SECURITY_WORKFLOW.is_file():
        issues.append("platform-security.yml is missing")
        return issues
    security_text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
    invocations = extract_gate_invocations(security_text)
    missing = sorted(set(CI_GATE_IDS) - set(invocations))
    if missing:
        issues.append(f"CI does not invoke required canonical gates: {', '.join(missing)}")
    unknown = sorted(
        gate_id
        for path, text in _workflow_texts()
        for gate_id in extract_gate_invocations(text)
        if gate_id not in GATES_BY_ID and gate_id != "ci"
    )
    if unknown:
        issues.append(f"workflows reference unknown canonical gates: {', '.join(unknown)}")

    for marker in DIRECT_CANONICAL_COMMANDS:
        if marker in security_text:
            issues.append(f"platform-security.yml directly defines canonical command: {marker}")
    for marker in FORBIDDEN_EXCLUSION_MARKERS:
        if marker in security_text:
            issues.append(f"platform-security.yml contains an exclusion bypass: {marker}")

    if LEGACY_MANIFEST.exists():
        issues.append("legacy test-suite-manifest.json must not be maintained beside the registry")

    run_tests = (PLATFORM_ROOT / "tools" / "platform_run_tests.sh").read_text(encoding="utf-8")
    registry = (PLATFORM_ROOT / "tools" / "platform_verify.py").read_text(encoding="utf-8")
    if "-m unittest \"$@\"" not in run_tests or '"discover", "-s", "tests"' not in registry:
        issues.append("backend runner no longer supports unittest discovery")

    try:
        package = json.loads(WEB_PACKAGE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"web package metadata is unreadable: {exc}")
    else:
        scripts = package.get("scripts", {})
        hermetic = scripts.get("test:hermetic")
        if hermetic != "npm run test:smoke && npm run test:participant-progressive":
            issues.append("web hermetic gate must compose the auto-discovered smoke suites")
        if not isinstance(hermetic, str) or any(marker in hermetic for marker in FORBIDDEN_EXCLUSION_MARKERS):
            issues.append("web hermetic scripts contain an exclusion bypass")
        if isinstance(hermetic, str) and ".spec." in hermetic:
            issues.append("web hermetic gate names individual spec files")

    try:
        web_config = WEB_PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(f"web Playwright config is unreadable: {exc}")
    else:
        ignore_match = re.search(r"testIgnore:\s*\[(.*?)\]", web_config, re.DOTALL)
        ignored_specs = (
            set(re.findall(r'"([^"]+\.spec\.ts)"', ignore_match.group(1)))
            if ignore_match
            else set()
        )
        expected_ignored_specs = {
            "live-launch.spec.ts",
            "live-user-journey.spec.ts",
            "tournament-participant-progressive.spec.ts",
        }
        if ignored_specs != expected_ignored_specs:
            issues.append(
                "web hermetic config must isolate only live and participant specialized suites"
            )
        if re.search(r"retries:\s*process\.env\.CI", web_config):
            issues.append("web hermetic deterministic config must not retry CI failures")

    if not GOVERNANCE_DOC.is_file():
        issues.append("test-suite-governance.md is missing")
    else:
        documented_ids = {
            match.group(1)
            for line in GOVERNANCE_DOC.read_text(encoding="utf-8").splitlines()
            if (match := GOVERNANCE_TABLE_ID_RE.match(line))
        }
        unknown_documented = sorted(documented_ids - set(GATES_BY_ID))
        missing_documented = sorted(set(GATES_BY_ID) - documented_ids)
        if unknown_documented:
            issues.append(f"governance documents unknown gate IDs: {', '.join(unknown_documented)}")
        if missing_documented:
            issues.append(f"governance omits registry gate IDs: {', '.join(missing_documented)}")

    for path, text in _workflow_texts():
        for marker in ("--p95-budget-ms", "--p99-budget-ms", "p95_budget_ms", "p99_budget_ms"):
            if marker in text:
                issues.append(f"{path.relative_to(REPO_ROOT)} duplicates load budgets: {marker}")

    if any(not GATES_BY_ID[gate_id].deterministic for gate_id in DETERMINISTIC_GATE_IDS):
        issues.append("deterministic registry contains a production-only gate")
    if set(CI_GATE_IDS) != set(DETERMINISTIC_GATE_IDS):
        issues.append("CI gate list must equal the deterministic registry gates")

    try:
        profiles = load_profiles()
    except Exception as exc:  # Keep the contract self-test's failure concise.
        issues.append(f"canonical load profiles are invalid: {exc}")
    else:
        if len(profiles) < 4:
            issues.append("canonical load profile registry must contain the four baseline profiles")

    external_text = EXTERNAL_LOAD_WORKFLOW.read_text(encoding="utf-8")
    if "runs-on: ubuntu-latest" not in external_text:
        issues.append("external load workflow must use an external GitHub runner")
    if "platform_load.py" not in external_text:
        issues.append("external load workflow must dispatch platform_load.py")
    profile_options_match = re.search(
        r"profile_id:\n(?P<options>.*?)(?:\n\npermissions:)",
        external_text,
        re.DOTALL,
    )
    profile_options = (
        set(re.findall(r"^\s+-\s+([a-z0-9-]+-v[0-9]+)\s*$", profile_options_match.group("options"), re.MULTILINE))
        if profile_options_match
        else set()
    )
    external_profile_ids = {
        profile_id
        for profile_id, profile in (profiles.items() if "profiles" in locals() else [])
        if profile.get("execution", {}).get("generator") == "GitHub-hosted external runner"
    }
    if "profiles" in locals() and profile_options != external_profile_ids:
        issues.append("external load workflow profile choices drift from canonical profiles")
    supervisor = (PLATFORM_ROOT / "tools" / "platform_production_external_fixture_qa.sh").read_text(encoding="utf-8")
    if "measured HTTP generator runs on the" not in supervisor:
        issues.append("origin fixture supervisor must document that measurement stays external")
    if "platform_external_load.py" in supervisor:
        issues.append("origin fixture supervisor must not execute the HTTP generator")
    return issues


def main() -> int:
    issues = collect_issues()
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("platform verification contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
