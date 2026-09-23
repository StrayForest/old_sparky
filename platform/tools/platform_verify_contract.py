#!/usr/bin/env python3
"""Verify that repository verification ownership has not drifted."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Iterable

try:
    from tools.platform_test_catalog import (
        BACKEND_CONTOURS,
        BACKEND_AGGREGATE,
        CONTOUR_TIMEOUT_SECONDS,
        VERIFICATION_CONTOUR,
        CONTOUR_METADATA,
        EXPECTED_SNAPSHOT,
        catalog_issues,
        cases_for_contour,
        discover_test_cases,
        registry_payload as backend_registry_payload,
    )
    from tools.platform_test_runner import TEST_ENV_CONTOURS
    from tools.platform_load import load_profiles
    from tools.platform_ci_classifier import (
        DOCS_ONLY_GATE_IDS,
        FULL_GATE_IDS,
        OUT_OF_SCOPE_GATE_IDS,
    )
    from tools.platform_verify import (
        CI_GATE_IDS,
        DETERMINISTIC_GATE_IDS,
        GATES_BY_ID,
        _backend_command,
        _backend_contour_command,
        _verification_contract_commands,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_test_catalog import (
        BACKEND_CONTOURS,
        BACKEND_AGGREGATE,
        CONTOUR_TIMEOUT_SECONDS,
        VERIFICATION_CONTOUR,
        CONTOUR_METADATA,
        EXPECTED_SNAPSHOT,
        catalog_issues,
        cases_for_contour,
        discover_test_cases,
        registry_payload as backend_registry_payload,
    )
    from platform_test_runner import TEST_ENV_CONTOURS
    from platform_load import load_profiles
    from platform_ci_classifier import (
        DOCS_ONLY_GATE_IDS,
        FULL_GATE_IDS,
        OUT_OF_SCOPE_GATE_IDS,
    )
    from platform_verify import (
        CI_GATE_IDS,
        DETERMINISTIC_GATE_IDS,
        GATES_BY_ID,
        _backend_command,
        _backend_contour_command,
        _verification_contract_commands,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = REPO_ROOT / "platform"
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"
COMPOSITE_ACTION_ROOT = REPO_ROOT / ".github" / "actions"
SECURITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-security.yml"
GOVERNANCE_DOC = PLATFORM_ROOT / "docs" / "test-suite-governance.md"
CI_REQUIREMENTS_INPUT = PLATFORM_ROOT / "requirements-ci.in"
CI_REQUIREMENTS_LOCK = PLATFORM_ROOT / "requirements-ci.lock.txt"
CI_REQUIREMENTS_LOCKER = PLATFORM_ROOT / "requirements-ci-locker.lock.txt"
CI_REQUIREMENTS_METADATA = PLATFORM_ROOT / "requirements-ci.lock.meta.json"
CI_INSTALLER = PLATFORM_ROOT / "tools" / "platform_install_ci_python.sh"
CI_LOCK_GENERATOR = PLATFORM_ROOT / "tools" / "platform_generate_ci_lock.sh"
CI_PIP_ENV = PLATFORM_ROOT / "tools" / "platform_ci_pip_env.sh"
WEB_PACKAGE = PLATFORM_ROOT / "apps" / "platform_web" / "package.json"
WEB_PLAYWRIGHT_CONFIG = PLATFORM_ROOT / "apps" / "platform_web" / "playwright.config.ts"
WEB_PARTICIPANT_CONFIG = PLATFORM_ROOT / "apps" / "platform_web" / "playwright.participant.config.ts"
WEB_SOURCE_CONTRACT_CONFIG = PLATFORM_ROOT / "apps" / "platform_web" / "playwright.source-contract.config.ts"
WEB_HERMETIC_RUNNER = PLATFORM_ROOT / "tools" / "platform_web_hermetic.sh"
TEST_RUNNER = PLATFORM_ROOT / "tools" / "platform_test_runner.py"
LEGACY_MANIFEST = PLATFORM_ROOT / "tests" / "test-suite-manifest.json"
EXTERNAL_LOAD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-production-external-load.yml"
CLASSIFIER_TOOL = PLATFORM_ROOT / "tools" / "platform_ci_classifier.py"
AUTO_DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-production-autodeploy.yml"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-production-deploy.yml"

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

# The repository currently consumes only first-party actions from the official
# ``actions`` organization.  Keep this allowlist explicit so a new remote
# action cannot be introduced alongside a pinned but unreviewed fork.
ALLOWED_ACTION_OWNERS = frozenset({"actions"})
_ACTION_USE_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<value>[^\s#]+)",
)
_REMOTE_ACTION_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+)"
    r"(?:/(?P<path>[^@\s]+))?@(?P<ref>[^@\s]+)$",
)
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SETUP_PYTHON_USE_RE = re.compile(
    r"^\s*(?:-\s+)?uses:\s*actions/setup-python@",
    re.MULTILINE,
)


def _action_definition_paths() -> tuple[Path, ...]:
    """Return every workflow and composite-action YAML definition."""

    paths: set[Path] = set()
    for root in (WORKFLOW_ROOT, COMPOSITE_ACTION_ROOT):
        if not root.is_dir():
            continue
        paths.update(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        )
    return tuple(sorted(paths))


def action_pin_issues(
    paths: Iterable[Path] | None = None,
) -> list[str]:
    """Fail closed when a remote action is mutable or outside the allowlist.

    Local ``./`` actions intentionally remain path references.  Every remote
    action, including reusable workflow references, must use an exact
    lower-case 40-character commit SHA and an approved owner.
    """

    selected = _action_definition_paths() if paths is None else tuple(paths)
    issues: list[str] = []
    for path in selected:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"{path}: cannot read action definition: {exc}")
            continue
        try:
            display_path = path.relative_to(REPO_ROOT)
        except ValueError:
            display_path = path
        for line_number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            match = _ACTION_USE_RE.match(line)
            if match is None:
                continue
            value = match.group("value").strip("'\"")
            if value.startswith("./"):
                continue
            remote = _REMOTE_ACTION_RE.fullmatch(value)
            location = f"{display_path}:{line_number}"
            if remote is None:
                issues.append(
                    f"{location}: remote action {value!r} must be owner/repository@40-char-commit-sha"
                )
                continue
            owner = remote.group("owner")
            if owner not in ALLOWED_ACTION_OWNERS:
                issues.append(
                    f"{location}: action owner {owner!r} is not approved; "
                    f"allowed owners: {', '.join(sorted(ALLOWED_ACTION_OWNERS))}"
                )
            ref = remote.group("ref")
            if _FULL_COMMIT_SHA_RE.fullmatch(ref) is None:
                issues.append(
                    f"{location}: remote action ref must be a lower-case 40-character commit SHA"
                )
    return issues


def _runner_main_has_environment_guard(source: str) -> bool:
    """Check the executable AST, rather than a comment/string marker."""

    try:
        tree = ast.parse(source, filename=str(TEST_RUNNER))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "main":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not isinstance(call.func, ast.Name) or call.func.id != "_require_test_environment":
                continue
            if len(call.args) != 1 or call.keywords:
                continue
            argument = call.args[0]
            if (
                isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.value.id == "args"
                and argument.attr == "contour"
            ):
                return True
    return False


def _workflow_texts() -> Iterable[tuple[Path, str]]:
    for path in sorted(WORKFLOW_ROOT.glob("*.y*ml")):
        yield path, path.read_text(encoding="utf-8")


def workflow_level_permission_issues() -> list[str]:
    """Reject broad write permissions inherited by every job in a workflow."""

    issues: list[str] = []
    permission_line = re.compile(
        r"^  (?P<permission>[A-Za-z0-9_-]+):\s*(?P<value>write)\s*$",
        re.MULTILINE,
    )
    for path, text in _workflow_texts():
        match = re.search(
            r"^permissions:\n(?P<body>(?:^  [^\n]+\n?)*)",
            text,
            re.MULTILINE,
        )
        if match is None:
            continue
        for permission in permission_line.finditer(match.group("body")):
            issues.append(
                f"{path.relative_to(REPO_ROOT)} workflow-level "
                f"permission {permission.group('permission')}: write is too broad; "
                "scope it to the writing job"
            )
    return issues


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


def _workflow_job_block(text: str, job_id: str) -> str:
    match = re.search(
        rf"^  {re.escape(job_id)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return "" if match is None else match.group("body")


def security_status_permission_issues(security_text: str) -> list[str]:
    """Keep the CI status token on the two jobs that publish its result."""

    issues: list[str] = []
    job_blocks = {
        match.group("job_id"): match.group("body")
        for match in re.finditer(
            r"^  (?P<job_id>[A-Za-z0-9_-]+):\n"
            r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            security_text,
            re.MULTILINE | re.DOTALL,
        )
    }
    expected_publishers = {"status-start", "status-final"}
    for job_id in expected_publishers:
        block = job_blocks.get(job_id, "")
        if not block:
            issues.append(f"platform-security.yml is missing status publisher job {job_id}")
            continue
        if not re.search(
            r"^    permissions:\n(?:(?:^      [^\n]+\n?))*?"
            r"^      statuses:\s*write\s*$",
            block,
            re.MULTILINE,
        ):
            issues.append(f"platform-security.yml {job_id} must have statuses: write")
        if "/statuses/" not in block:
            issues.append(f"platform-security.yml {job_id} must publish a commit status")
    for job_id, block in job_blocks.items():
        if re.search(
            r"^    permissions:\n(?:(?:^      [^\n]+\n?))*?"
            r"^      statuses:\s*write\s*$",
            block,
            re.MULTILINE,
        ) and job_id not in expected_publishers:
            issues.append(
                f"platform-security.yml job {job_id} must not receive statuses: write"
            )
    return issues


def _workflow_step_blocks(job_block: str) -> tuple[str, ...]:
    """Return top-level GitHub Actions step blocks for one job."""

    lines = job_block.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^      - ", line)
    ]
    return tuple(
        "".join(lines[start:end])
        for start, end in zip(starts, (*starts[1:], len(lines)))
    )


def _checkout_credential_issues(workflow_name: str, workflow_text: str) -> list[str]:
    """Ensure every checkout step disables the persistent git credential."""

    issues: list[str] = []
    checkout_steps = [
        step
        for job_match in re.finditer(
            r"^  [A-Za-z0-9_-]+:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow_text,
            re.MULTILINE | re.DOTALL,
        )
        for step in _workflow_step_blocks(job_match.group("body"))
        if re.search(
            r"^\s*(?:-\s*)?uses:\s*actions/checkout@",
            step,
            re.MULTILINE,
        )
    ]
    if not checkout_steps:
        issues.append(f"{workflow_name} must enumerate at least one checkout step")
        return issues
    for index, step in enumerate(checkout_steps, start=1):
        if not re.search(
            r"^\s+persist-credentials:\s*false\s*$",
            step,
            re.MULTILINE,
        ):
            issues.append(
                f"{workflow_name} checkout step {index} must set persist-credentials: false"
            )
    return issues


def _workflow_job_needs(job_block: str) -> set[str]:
    """Return the dependency IDs declared by a job's needs field."""

    match = re.search(
        r"^    needs:[ \t]*(?P<inline>[^\n]*)\n?(?P<items>(?:^      -[ \t]*[^\n]+\n?)*)",
        job_block,
        re.MULTILINE,
    )
    if match is None:
        return set()
    dependencies: set[str] = set()
    inline = match.group("inline").strip()
    if inline:
        if inline.startswith("[") and inline.endswith("]"):
            dependencies.update(
                item.strip().strip("'\"")
                for item in inline[1:-1].split(",")
                if item.strip()
            )
        else:
            dependencies.add(inline.strip("'\""))
    dependencies.update(
        item.strip()[2:].strip("'\"")
        for item in match.group("items").splitlines()
        if item.strip().startswith("-")
    )
    return dependencies


def _production_job_secret_names(job_block: str) -> set[str]:
    """Return explicit production secret references in one job body."""

    return set(re.findall(r"secrets\.([A-Za-z0-9_]+)", job_block))


def _production_secret_scope_issues(production_text: str) -> list[str]:
    """Verify the immutable-candidate to secret-bearing production DAG.

    The release artifact is constructed in a separate, non-environment job.
    The production job is an artifact consumer only: it receives the
    production environment and SSH secrets, but never checks out or executes
    candidate source. Keep these checks textual and dependency-free so the
    contract can run before any CI environment is provisioned.
    """

    issues: list[str] = []
    jobs = {
        job_id: _workflow_job_block(production_text, job_id)
        for job_id in ("validate-dispatch", "build-release", "preflight", "production")
    }
    for job_id, job in jobs.items():
        if not job:
            issues.append(f"production deploy workflow is missing its {job_id} job")
    if not jobs["production"]:
        return issues

    validator = jobs["validate-dispatch"]
    build = jobs["build-release"]
    production = jobs["production"]

    # Dispatch validation must execute even when a caller supplies malformed
    # inputs. Every mode branch must depend on its successful result.
    if validator:
        if "if: ${{ always() }}" not in validator:
            issues.append("deployment dispatch validation must always run")
        if "platform_workflow_input_guard.py deployment" not in validator:
            issues.append("deployment dispatch validation must create the canonical handoff")
        if 'case "$DEPLOY_MODE" in' not in validator:
            issues.append("deployment dispatch validation must validate mode before branches")
    for branch in ("build-release", "preflight", "production"):
        branch_job = jobs[branch]
        if branch_job and "needs: validate-dispatch" not in branch_job and "- validate-dispatch" not in branch_job:
            issues.append(f"production deploy {branch} job must depend on dispatch validation")
        if branch_job and "needs.validate-dispatch.result == 'success'" not in branch_job:
            issues.append(f"production deploy {branch} job must propagate dispatch-validation failure")

    # Candidate checkout/build/publish belongs only to a fresh, non-secret job.
    if build:
        build_step = next(
            (
                step
                for step in _workflow_step_blocks(build)
                if re.search(
                    r"^      - name:\s*Build immutable release artifact in CI\s*$",
                    step,
                    re.MULTILINE,
                )
            ),
            "",
        )
        if "environment: production" in build:
            issues.append("candidate build job must not use the production environment")
        if _production_job_secret_names(build) or re.search(r"\bPROD_SSH_(?:HOST|USER|KEY)\b", build):
            issues.append("candidate build job must not receive production secrets")
        if "actions/checkout@" not in build:
            issues.append("candidate build job must checkout the reviewed source")
        if "platform_build_release.sh" not in build:
            issues.append("candidate build job must construct the immutable release artifact")
        if "actions/upload-artifact@" not in build:
            issues.append("candidate build job must publish an immutable release artifact")
        if "if-no-files-found: error" not in build:
            issues.append("candidate release artifact upload must fail on missing files")
        if "artifact_id:" not in build or "artifact_digest:" not in build:
            issues.append("candidate build job must expose exact artifact ID and digest outputs")
        if not build_step:
            issues.append("candidate build job must name its immutable artifact build step")
        else:
            if "env -i" not in build_step:
                issues.append("candidate build must use an allowlisted env -i")
            if re.search(r"\bsudo\s+-[^\n]*E", build_step):
                issues.append("candidate build must not use sudo -E")
        if "id: publish-release-artifact" not in build:
            issues.append("candidate build job must identify the published release artifact")
        if "platform-release-artifact-${{ github.run_id }}-${{ github.run_attempt }}" not in build:
            issues.append("candidate build job artifact name must be bound to this run")
        if "if: ${{ always() }}" not in build or "Remove candidate build tree" not in build:
            issues.append("candidate build job must always remove its build tree")

    job_env = re.search(
        r"^    env:\n(?P<body>.*?)(?=^    steps:\n)",
        production,
        re.MULTILINE | re.DOTALL,
    )
    if job_env and re.search(r"PROD_SSH_(?:HOST|USER|KEY)|secrets\.", job_env.group("body")):
        issues.append("production job-level env must not expose production SSH secrets")

    if "environment: production" not in production:
        issues.append("production deploy job must own the production environment")
    if "actions/checkout@" in production:
        issues.append("production secret job must not checkout candidate source")
    candidate_markers = (
        "platform_build_release.sh",
        "platform_build_release.py",
        "platform_load.py",
        "platform_live_launch_report.py",
    )
    for marker in candidate_markers:
        if marker in production:
            issues.append(f"production secret job must not execute candidate code: {marker}")
    if "actions/download-artifact@" not in production:
        issues.append("production secret job must consume the immutable artifact via download-artifact")
    if "id-token: write" in production or "attestations: write" in production:
        issues.append("production secret job must not receive candidate-build signing permissions")

    # The production consumer must wait for both validation and a successful
    # candidate build. A skipped or failed build must never become a deploy.
    production_needs = _workflow_job_needs(production)
    for dependency in ("validate-dispatch", "build-release"):
        if dependency not in production_needs:
            issues.append(f"production deploy job must need {dependency}")
    for expression in (
        "needs.validate-dispatch.result == 'success'",
        "needs.build-release.result == 'success'",
        "inputs.mode == 'deploy'",
    ):
        if expression not in production:
            issues.append(f"production deploy job must require {expression}")
    if "continue-on-error" in production or (build and "continue-on-error" in build):
        issues.append("production candidate/build jobs must propagate failures")

    exact_artifact_name = "platform-release-artifact-${{ github.run_id }}-${{ github.run_attempt }}"
    if exact_artifact_name not in production:
        issues.append("production deploy must download the exact current-run release artifact")
    for marker in (
        "PUBLISHED_ARTIFACT_ID: ${{ needs.build-release.outputs.artifact_id }}",
        "PUBLISHED_ARTIFACT_DIGEST: ${{ needs.build-release.outputs.artifact_digest }}",
        "PUBLISHED_ARTIFACT_ID",
        "PUBLISHED_ARTIFACT_DIGEST",
        "sha256sum -c",
        "RELEASE.provenance.json",
        "source_git_commit",
        "artifact_sha256",
    ):
        if marker not in production:
            issues.append(f"production deploy must verify exact published artifact field: {marker}")

    allowed_secret_steps = {
        "Validate deployment secrets": {"PROD_SSH_HOST", "PROD_SSH_USER", "PROD_SSH_KEY"},
        "Configure SSH": {"PROD_SSH_HOST", "PROD_SSH_KEY"},
        "Verify SSH connection": {"PROD_SSH_HOST", "PROD_SSH_USER"},
        "Upload verified CI artifact": {"PROD_SSH_HOST", "PROD_SSH_USER"},
        "Run production preflight or deployment": {"PROD_SSH_HOST", "PROD_SSH_USER"},
    }
    for step in _workflow_step_blocks(production):
        name_match = re.search(r"^      - name:\s*(.*?)\s*$", step, re.MULTILINE)
        name = "" if name_match is None else name_match.group(1)
        secret_names = set(re.findall(r"PROD_SSH_(?:HOST|USER|KEY)", step))
        other_secret_names = {
            secret
            for secret in _production_job_secret_names(step)
            if not secret.startswith("PROD_SSH_")
        }
        if other_secret_names:
            issues.append(
                f"production step {name or '<unnamed>'} receives unapproved secrets: "
                + ", ".join(sorted(other_secret_names))
            )
        if not secret_names:
            continue
        allowed = allowed_secret_steps.get(name)
        if allowed is None:
            issues.append(
                f"production step {name or '<unnamed>'} must not receive production SSH secrets"
            )
            continue
        unexpected = sorted(secret_names - allowed)
        if unexpected:
            issues.append(
                f"production step {name} receives unnecessary SSH secrets: {', '.join(unexpected)}"
            )

    return issues


BACKEND_CI_INSTALL_BUFFER_SECONDS = 600


def _normalise_requirement_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_CI_REQUIREMENT_INCLUDE_RE = re.compile(
    r"^(?:-r|--requirement)\s+(?P<path>[^\s#]+)"
)
_CI_LOCK_LINE_RE = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)"
    r" --hash=sha256:[0-9a-f]{64}"
)


def _ci_display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _ci_requirement_input_paths(issues: list[str]) -> list[Path]:
    """Resolve every nested -r input, rejecting escapes and symlinks."""

    paths: list[Path] = []
    seen: set[Path] = set()
    pending = [CI_REQUIREMENTS_INPUT]
    while pending:
        path = pending.pop(0)
        if path.is_symlink() or not path.is_file():
            issues.append(
                f"CI requirement input is missing or a symlink: {_ci_display_path(path)}"
            )
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(PLATFORM_ROOT)
        except ValueError:
            issues.append(
                f"CI requirement include escapes platform root: {_ci_display_path(path)}"
            )
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"CI dependency input is unreadable: {resolved.name}: {exc}")
            continue
        for raw_line in lines:
            match = _CI_REQUIREMENT_INCLUDE_RE.match(raw_line.strip())
            if match:
                pending.append(resolved.parent / match.group("path"))
    return sorted(paths)


def _ci_file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, UnicodeError):
        return None


def _ci_dependency_issues(security_text: str) -> list[str]:
    """Keep every non-editable Python CI job on the hashed lock contract."""

    issues: list[str] = []
    for path, label in (
        (CI_REQUIREMENTS_INPUT, "CI requirement input"),
        (CI_REQUIREMENTS_LOCK, "CI requirement lock"),
        (CI_REQUIREMENTS_LOCKER, "CI toolchain lock"),
        (CI_REQUIREMENTS_METADATA, "CI lock metadata"),
        (CI_INSTALLER, "CI dependency installer"),
        (CI_LOCK_GENERATOR, "CI lock generator"),
        (CI_PIP_ENV, "CI pip environment wrapper"),
    ):
        if not path.is_file() or path.is_symlink():
            issues.append(f"{label} is missing or a symlink: {path.relative_to(REPO_ROOT)}")

    input_paths = _ci_requirement_input_paths(issues)
    if CI_REQUIREMENTS_INPUT.is_file() and not CI_REQUIREMENTS_INPUT.is_symlink():
        input_text = CI_REQUIREMENTS_INPUT.read_text(encoding="utf-8")
        for marker in (
            "-r requirements-platform.txt",
            "-r requirements-quality.txt",
            "setuptools==84.0.0",
            "wheel==0.48.0",
        ):
            if marker not in input_text:
                issues.append(f"CI requirement input is missing marker: {marker}")
    direct_pins: dict[str, str] = {}
    for path in input_paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"CI dependency input is unreadable: {path.name}: {exc}")
            continue
        for raw_line in lines:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if _CI_REQUIREMENT_INCLUDE_RE.match(line):
                continue
            match = re.fullmatch(
                r"(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
                r"==(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)",
                line,
            )
            if match is None:
                issues.append(f"CI dependency input is not an exact pin: {path.name}: {line}")
                continue
            name = _normalise_requirement_name(match.group("name"))
            if name in direct_pins:
                issues.append(f"CI dependency inputs duplicate package: {name}")
            direct_pins[name] = match.group("version")
    for name, version in {"setuptools": "84.0.0", "wheel": "0.48.0"}.items():
        if direct_pins.get(name) != version:
            issues.append(f"CI requirement input must pin {name}=={version}")

    lock_pins: dict[str, str] = {}
    if CI_REQUIREMENTS_LOCK.is_file() and not CI_REQUIREMENTS_LOCK.is_symlink():
        try:
            lock_lines = CI_REQUIREMENTS_LOCK.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"CI requirement lock is unreadable: {exc}")
            lock_lines = []
        if lock_lines != sorted(lock_lines):
            issues.append("CI requirement lock is not canonically sorted")
        for line in lock_lines:
            match = _CI_LOCK_LINE_RE.fullmatch(line)
            if match is None:
                issues.append("CI requirement lock must hash every exact package pin")
                continue
            name = _normalise_requirement_name(match.group("name"))
            if name in lock_pins:
                issues.append(f"CI requirement lock contains duplicate package: {name}")
            lock_pins[name] = match.group("version")
    for name, version in direct_pins.items():
        if lock_pins.get(name) != version:
            issues.append(f"CI requirement lock omits or changes direct pin: {name}")
    for name in ("pip", "setuptools", "wheel"):
        if name not in lock_pins:
            issues.append(f"CI requirement lock omits bootstrap package: {name}")

    locker_pins: dict[str, str] = {}
    if CI_REQUIREMENTS_LOCKER.is_file() and not CI_REQUIREMENTS_LOCKER.is_symlink():
        try:
            locker_lines = CI_REQUIREMENTS_LOCKER.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"CI toolchain lock is unreadable: {exc}")
            locker_lines = []
        if locker_lines != sorted(locker_lines):
            issues.append("CI toolchain lock is not canonically sorted")
        for line in locker_lines:
            match = _CI_LOCK_LINE_RE.fullmatch(line)
            if match is None:
                issues.append("CI toolchain lock must hash every exact package pin")
                continue
            name = _normalise_requirement_name(match.group("name"))
            if name in locker_pins:
                issues.append(f"CI toolchain lock contains duplicate package: {name}")
            locker_pins[name] = match.group("version")
    expected_locker = {
        "build": "1.6.1",
        "click": "8.5.0",
        "packaging": "26.3",
        "pip-tools": "7.6.1",
        "pip": "26.2.1",
        "pyproject-hooks": "1.2.0",
        "setuptools": "84.0.0",
        "wheel": "0.48.0",
    }
    for name, version in expected_locker.items():
        if locker_pins.get(_normalise_requirement_name(name)) != version:
            issues.append(f"CI toolchain lock omits or changes {name}=={version}")

    if CI_REQUIREMENTS_METADATA.is_file() and not CI_REQUIREMENTS_METADATA.is_symlink():
        try:
            metadata = json.loads(CI_REQUIREMENTS_METADATA.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append(f"CI lock metadata is unreadable: {exc}")
            metadata = {}
        expected_input_files = [
            {
                "path": str(path.relative_to(PLATFORM_ROOT)),
                "sha256": _ci_file_sha256(path),
            }
            for path in input_paths
        ]
        if metadata.get("schema") != 1:
            issues.append("CI lock metadata schema must be 1")
        if metadata.get("target") != {
            "architecture": "x86_64",
            "platform": "linux",
            "python": "3.12",
        }:
            issues.append("CI lock metadata target does not match the runner contract")
        if metadata.get("index_url") != "https://pypi.org/simple":
            issues.append("CI lock metadata must use the canonical PyPI index")
        if metadata.get("input_files") != expected_input_files:
            issues.append("CI lock metadata input closure is stale or incomplete")
        if metadata.get("lock_file") != "requirements-ci.lock.txt":
            issues.append("CI lock metadata names the wrong CI lock")
        if metadata.get("lock_sha256") != _ci_file_sha256(CI_REQUIREMENTS_LOCK):
            issues.append("CI lock metadata digest does not match the CI lock")
        if metadata.get("toolchain_lock_file") != "requirements-ci-locker.lock.txt":
            issues.append("CI lock metadata names the wrong toolchain lock")
        if metadata.get("toolchain_lock_sha256") != _ci_file_sha256(CI_REQUIREMENTS_LOCKER):
            issues.append("CI lock metadata toolchain digest is stale")
        if metadata.get("toolchain") != {
            "pip": "26.2.1",
            "pip-tools": "7.6.1",
            "setuptools": "84.0.0",
            "wheel": "0.48.0",
        }:
            issues.append("CI lock metadata toolchain versions are not pinned")
        if metadata.get("freshness_policy") != {
            "default": "reuse-existing-locked-versions",
            "update": "explicit --update required for newer resolution",
        }:
            issues.append("CI lock metadata freshness policy is missing or changed")

    if CI_INSTALLER.is_file() and not CI_INSTALLER.is_symlink():
        installer_text = CI_INSTALLER.read_text(encoding="utf-8")
        for marker in (
            "requirements-ci.lock.txt",
            "--require-hashes",
            "--isolated",
            "--only-binary=:all:",
            "--index-url https://pypi.org/simple",
            "platform_ci_pip_env.sh",
            "pip check",
        ):
            if marker not in installer_text:
                issues.append(f"CI dependency installer is missing lock marker: {marker}")
        for marker in ("requirements-platform.txt", "requirements-quality.txt"):
            if marker in installer_text:
                issues.append(f"CI dependency installer must not resolve input file: {marker}")

    if CI_LOCK_GENERATOR.is_file() and not CI_LOCK_GENERATOR.is_symlink():
        generator_text = CI_LOCK_GENERATOR.read_text(encoding="utf-8")
        for marker in (
            "requirements-ci.in",
            "requirements-ci.lock.txt",
            "requirements-ci-locker.lock.txt",
            "requirements-ci.lock.meta.json",
            "--generate-hashes",
            "--only-binary=:all:",
            "--isolated",
            "--constraint",
            "--update",
            "platform_ci_pip_env.sh",
        ):
            if marker not in generator_text:
                issues.append(f"CI lock generator is missing marker: {marker}")
        if "PLATFORM_PIP_COMPILE" in generator_text:
            issues.append("CI lock generator must not use an ambient pip-compile path")

    if CI_PIP_ENV.is_file() and not CI_PIP_ENV.is_symlink():
        pip_env_text = CI_PIP_ENV.read_text(encoding="utf-8")
        for marker in ("env -i", "PIP_CONFIG_FILE=/dev/null", "PATH=/usr/bin:/bin"):
            if marker not in pip_env_text:
                issues.append(f"CI pip environment wrapper is missing marker: {marker}")

    job_blocks = {
        match.group("job_id"): match.group("body")
        for match in re.finditer(
            r"^  (?P<job_id>[A-Za-z0-9_-]+):\n"
            r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            security_text,
            re.MULTILINE | re.DOTALL,
        )
    }
    setup_python_jobs = {
        job_id: block
        for job_id, block in job_blocks.items()
        if _SETUP_PYTHON_USE_RE.search(block)
    }
    if not setup_python_jobs:
        issues.append("platform-security.yml must contain a Python setup job")
    for job_id, block in sorted(setup_python_jobs.items()):
        steps = _workflow_step_blocks(block)
        setup_steps = tuple(
            step
            for step in steps
            if _SETUP_PYTHON_USE_RE.search(step)
        )
        if len(setup_steps) != 1:
            issues.append(f"Python CI job {job_id} must have exactly one setup-python step")
            setup_step = ""
        else:
            setup_step = setup_steps[0]
        installer_steps = tuple(
            step
            for step in steps
            if re.search(
                r"^\s*run:\s*platform/tools/platform_install_ci_python\.sh\s*$",
                step,
                re.MULTILINE,
            )
        )
        if len(installer_steps) != 1:
            issues.append(f"Python CI job {job_id} must invoke the canonical installer exactly once")
        if len(re.findall(r'^\s+python-version:\s*"3\.12"\s*$', setup_step, re.MULTILINE)) != 1:
            issues.append(f"Python CI job {job_id} must use the Python 3.12 lock target")
        if len(re.findall(r"^\s+cache:\s*pip\s*$", setup_step, re.MULTILINE)) != 1:
            issues.append(f"Python CI job {job_id} must enable the pip cache")
        if len(
            re.findall(
                r"^\s+cache-dependency-path:\s*platform/requirements-ci\.lock\.txt\s*$",
                setup_step,
                re.MULTILINE,
            )
        ) != 1:
            issues.append(f"Python CI job {job_id} cache must use the CI lock digest")
        for marker in (
            "requirements-platform.txt",
            "requirements-quality.txt",
            "requirements-platform.lock.txt",
            "pip install",
            "python -m pip install",
            "python -m venv platform/.venv_platform",
        ):
            if marker in block:
                issues.append(f"Python CI job {job_id} must not contain direct dependency path/installer: {marker}")
    return issues


def _backend_timeout_budget_issues(blocks: dict[str, str]) -> list[str]:
    """Require job deadlines to cover sequential tests plus setup overhead."""

    contour_jobs = {
        "backend-static": (
            "backend-unit",
            "backend-tool-contract",
            "performance-contract",
        ),
        "backend-integration": ("backend-integration",),
        "backend-privileged": ("backend-privileged",),
    }
    budgets = {
        job_id: sum(CONTOUR_TIMEOUT_SECONDS[item] for item in contours)
        + BACKEND_CI_INSTALL_BUFFER_SECONDS
        for job_id, contours in contour_jobs.items()
    }
    budgets["verification-contract"] = (
        2 * CONTOUR_TIMEOUT_SECONDS[VERIFICATION_CONTOUR]
        + BACKEND_CI_INSTALL_BUFFER_SECONDS
    )
    issues: list[str] = []
    for job_id, seconds in budgets.items():
        block = blocks.get(job_id, "")
        match = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", block, re.MULTILINE)
        if match is None:
            issues.append(f"{job_id} must declare an explicit timeout-minutes budget")
            continue
        actual_minutes = int(match.group(1))
        required_minutes = (seconds + 59) // 60
        if actual_minutes < required_minutes:
            issues.append(
                f"{job_id} timeout {actual_minutes}m is below required {required_minutes}m "
                f"({seconds}s contour/setup budget)"
            )
    return issues


def _backend_workflow_issues(security_text: str) -> list[str]:
    """Check the backend DAG without executing any CI jobs."""

    issues: list[str] = []
    expected_jobs = {
        "backend-static": ("backend-unit", "backend-tool-contract", "performance-contract"),
        "backend-integration": ("backend-integration",),
        "backend-privileged": ("backend-privileged",),
        "backend": (),
    }
    blocks = {job_id: _workflow_job_block(security_text, job_id) for job_id in expected_jobs}
    for job_id, contours in expected_jobs.items():
        block = blocks[job_id]
        if not block:
            issues.append(f"platform-security.yml is missing backend job {job_id}")
            continue
        if job_id != "backend" and "needs: classifier" not in block:
            issues.append(f"backend job {job_id} must depend on classifier")
        if "continue-on-error" in block:
            issues.append(f"backend job {job_id} must not continue on error")
        if re.search(r"\|\|\s*(?:true|:)(?:\s|$)", block):
            issues.append(f"backend job {job_id} must not mask a component failure")
        if re.search(r"^\s*continue\s*$", block, re.MULTILINE):
            issues.append(f"backend job {job_id} must not continue past a component failure")
        if re.search(r"^\s{4,}retries?\s*:", block, re.IGNORECASE | re.MULTILINE):
            issues.append(f"backend job {job_id} must not retry component execution")
        for contour in contours:
            if block.count(f"platform_verify.py {contour}") != 1:
                issues.append(
                    f"backend job {job_id} must invoke {contour} exactly once"
                )
        if job_id != "backend" and "if: ${{ needs.classifier.outputs.class == 'full' }}" not in block:
            issues.append(f"backend job {job_id} must be full-route gated")

    static = blocks["backend-static"]
    integration = blocks["backend-integration"]
    privileged = blocks["backend-privileged"]
    aggregate = blocks["backend"]
    if "services:" in static or "services:" in privileged:
        issues.append("DB-free and privileged backend jobs must not declare services")
    if "services:" not in integration or "postgres:" not in integration or "redis:" not in integration:
        issues.append("backend-integration must be the only backend contour with PostgreSQL/Redis services")
    for marker in (
        "id -u",
        "Pillow",
        "/usr/bin/runuser",
        "/usr/bin/setpriv",
        "oldsparky-media",
    ):
        if marker not in privileged:
            issues.append(f"backend-privileged preflight is missing {marker}")
    if "PLATFORM_SECRET_KEY: ci-only-" not in privileged:
        issues.append("backend-privileged must use a non-production test secret")
    if "secrets." in privileged:
        issues.append("backend-privileged must not receive production credentials")
    if "--component-dir" not in aggregate:
        issues.append("backend aggregate must verify component artifacts with --component-dir")
    if "always()" not in aggregate:
        issues.append("backend aggregate must remain an always-run result verifier")
    for job_id in ("backend-static", "backend-integration", "backend-privileged"):
        if (
            re.search(
                r"actions/upload-artifact@[0-9a-f]{40}(?:\s|$)",
                blocks[job_id],
            ) is None
            or "if: ${{ always() }}" not in blocks[job_id]
        ):
            issues.append(f"backend job {job_id} must retain component results even on failure")
    if not all(
        component in aggregate
        for component in (
            "backend-static",
            "backend-integration",
            "backend-privileged",
        )
    ):
        issues.append("backend aggregate needs all backend component jobs")
    return issues


def release_runtime_workflow_issues(security_text: str) -> list[str]:
    """Keep fixture and trusted-dev release contours separate and fail-closed."""

    issues: list[str] = []
    fixture = _workflow_job_block(security_text, "release-runtime")
    real = _workflow_job_block(security_text, "release-runtime-real")
    if not fixture:
        issues.append("platform-security.yml is missing release-runtime fixture")
    if not real:
        issues.append("platform-security.yml is missing release-runtime-real")
    if not fixture or not real:
        return issues

    if "name: Conditional release runtime fixture" not in fixture:
        issues.append("release-runtime must remain the fixture job")
    if "needs: classifier" not in fixture:
        issues.append("release-runtime fixture must depend on classifier")
    if "timeout-minutes: 15" not in fixture:
        issues.append("release-runtime fixture must retain a 15-minute timeout")
    if "permissions:\n      contents: read" not in fixture:
        issues.append("release-runtime fixture must have contents: read permissions")
    fixture_checkout = next(
        (step for step in _workflow_step_blocks(fixture) if "actions/checkout@" in step),
        "",
    )
    for marker, message in (
        ("ref: ${{ github.sha }}", "exact checkout SHA"),
        ("fetch-depth: 0", "full checkout history"),
        ("persist-credentials: false", "checkout credential isolation"),
    ):
        if marker not in fixture_checkout:
            issues.append(f"release-runtime fixture checkout must set {message}")
    if fixture.count("platform_verify.py release-runtime") != 1:
        issues.append("release-runtime fixture must invoke the canonical gate exactly once")
    if "platform_build_release.sh" in fixture or "RELEASE_RUNTIME_BUILD schema=1" in fixture:
        issues.append("release-runtime fixture must not run the full release builder")
    if "real-runtime" in fixture or "release-runtime-real" in fixture:
        issues.append("release-runtime fixture must not expose the real builder contour")

    if "name: Trusted dev immutable release runtime" not in real:
        issues.append("release-runtime-real must be named as the trusted-dev builder")
    if "needs: classifier" not in real:
        issues.append("release-runtime-real must depend on classifier")
    if "timeout-minutes: 45" not in real:
        issues.append("release-runtime-real must retain a 45-minute job timeout")
    if "permissions:\n      contents: read" not in real:
        issues.append("release-runtime-real must have contents: read permissions")
    for marker, message in (
        ("github.event_name == 'push'", "push route condition"),
        ("github.event_name == 'workflow_dispatch'", "manual route condition"),
        ("github.ref == 'refs/heads/dev'", "canonical dev ref condition"),
        ("needs.classifier.outputs.runtime_sensitive == 'true'", "runtime-sensitive condition"),
        ("needs.classifier.outputs.fallback == 'true'", "fallback condition"),
    ):
        if marker not in real:
            issues.append(f"release-runtime-real is missing {message}")
    real_checkout = next(
        (step for step in _workflow_step_blocks(real) if "actions/checkout@" in step),
        "",
    )
    for marker, message in (
        ("ref: ${{ github.sha }}", "exact checkout SHA"),
        ("fetch-depth: 0", "full checkout history"),
        ("persist-credentials: false", "checkout credential isolation"),
    ):
        if marker not in real_checkout:
            issues.append(f"release-runtime-real checkout must set {message}")
    if "TARGET_SHA: ${{ github.sha }}" not in real:
        issues.append("release-runtime-real must bind the builder to the exact workflow SHA")
    real_step = next(
        (step for step in _workflow_step_blocks(real) if "id: real-runtime-build" in step),
        "",
    )
    if not real_step:
        issues.append("release-runtime-real must define the canonical builder step")
        return issues
    if "timeout-minutes: 40" not in real_step:
        issues.append("release-runtime-real builder step must have a fixed timeout")
    for marker, message in (
        ("platform_build_release.sh", "canonical release builder"),
        ("PLATFORM_RELEASE_OUTPUT_DIR=\"$release_output\"", "task-owned release output"),
        ("PLATFORM_WEB_NEXT_COMPRESSION=true", "canonical web compression mode"),
        ("/usr/bin/python3 -m venv", "root-isolated Python environment"),
        ("platform_validate_release_artifact.py", "read-only artifact validation"),
        ("sha256sum -c", "checksum validation"),
        ("RELEASE.json", "release provenance validation"),
        ("platform_release_build_diagnostics.py", "bounded builder diagnostics parser"),
        ("RELEASE_RUNTIME_BUILD_DIAGNOSTIC", "bounded workflow diagnostic"),
        ("canonical_builder_rc", "canonical builder result capture"),
        ("parser_rc", "diagnostic parser result capture"),
        ("consistency", "builder marker/result consistency"),
        ("diagnostic_sanitizer", "diagnostic sanitizer failure route"),
        ('/usr/bin/install -o root -g root -m 0600 /dev/null "$build_log"', "root-only diagnostic log"),
        ("min_free_bytes", "disk preflight"),
        ("disk_after_bytes", "post-cleanup disk check"),
        ("trap cleanup EXIT", "guaranteed cleanup"),
        ("mktemp -d", "task-owned temporary root"),
        ('/bin/rm -rf -- "$release_root"', "identity-checked cleanup"),
        ("RELEASE_RUNTIME_BUILD schema=1", "bounded build diagnostic"),
    ):
        if marker not in real_step:
            issues.append(f"release-runtime-real builder is missing {message}")
    if "sudo -n /usr/bin/env -i" not in real_step:
        issues.append("release-runtime-real builder must use root env isolation")
    if "actions/setup-python@" in real:
        issues.append("release-runtime-real must use its own clean production-style venv setup")
    if "platform_verify.py release-runtime" in real:
        issues.append("release-runtime-real must not run the fixture gate")
    if "tee" in real or 'cat "$build_log"' in real or "BASH_COMMAND" in real:
        issues.append("release-runtime-real must not expose raw builder diagnostics")
    builder_path = PLATFORM_ROOT / "tools" / "platform_build_release.sh"
    try:
        builder_source = builder_path.read_text(encoding="utf-8")
    except OSError:
        builder_source = ""
    if "RELEASE_BUILD_PHASE" not in builder_source:
        issues.append("canonical release builder must emit machine-readable phases")
    if "secrets." in real or "PROD_SSH_" in real or "SSH_PRIVATE_KEY" in real:
        issues.append("release-runtime-real must not receive production credentials")
    if "actions/upload-artifact@" in real or "actions/attest-build-provenance@" in real:
        issues.append("release-runtime-real must not publish or attest an artifact")
    if "GITHUB_WORKSPACE/platform/dist/releases" in real or "/root/old_sparky" in real:
        issues.append("release-runtime-real must use task-owned output, not production paths")
    if "platform_build_live_qa_runtime.py" in real_step:
        issues.append("release-runtime-real must call the full canonical builder, not a partial imitation")
    return issues


def collect_issues() -> list[str]:
    issues: list[str] = []

    issues.extend(f"action pin: {issue}" for issue in action_pin_issues())
    issues.extend(workflow_level_permission_issues())
    for workflow_path, workflow_text in _workflow_texts():
        if workflow_path in {SECURITY_WORKFLOW, PRODUCTION_WORKFLOW}:
            continue
        if re.search(r"^\s*(?:-\s*)?uses:\s*actions/checkout@", workflow_text, re.MULTILINE):
            issues.extend(
                _checkout_credential_issues(
                    str(workflow_path.relative_to(REPO_ROOT)),
                    workflow_text,
                )
            )

    if not SECURITY_WORKFLOW.is_file():
        issues.append("platform-security.yml is missing")
        return issues
    security_text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
    issues.extend(security_status_permission_issues(security_text))
    if re.search(r"^\s{4}paths(?:-ignore)?:", security_text, re.MULTILINE):
        issues.append("platform-security.yml must not use top-level path filters")
    if "merge_group:" not in security_text:
        issues.append("platform-security.yml must run for merge_group")
    if "platform_ci_classifier.py" not in security_text:
        issues.append("platform-security.yml must invoke the canonical CI classifier")
    if "classifier-manifest.json" not in security_text:
        issues.append("platform-security.yml must publish the classifier manifest")
    if "if: ${{ always() }}" not in security_text:
        issues.append("platform-security.yml aggregate must run with always()")
    if "platform-ci-summary-" not in security_text:
        issues.append("platform-security.yml must publish a machine-readable summary")
    issues.extend(_checkout_credential_issues("platform-security.yml", security_text))
    issues.extend(_ci_dependency_issues(security_text))
    invocations = extract_gate_invocations(security_text)
    missing = sorted(set(CI_GATE_IDS) - set(invocations))
    if missing:
        issues.append(f"CI does not invoke required canonical gates: {', '.join(missing)}")
    for gate_id in (*BACKEND_CONTOURS, BACKEND_AGGREGATE, VERIFICATION_CONTOUR):
        if invocations.count(gate_id) != 1:
            issues.append(
                f"CI must invoke catalog gate {gate_id} exactly once; "
                f"found {invocations.count(gate_id)}"
            )
    issues.extend(_backend_workflow_issues(security_text))
    issues.extend(release_runtime_workflow_issues(security_text))
    issues.extend(
        _backend_timeout_budget_issues(
            {
                job_id: _workflow_job_block(security_text, job_id)
                for job_id in (
                    "backend-static",
                    "backend-integration",
                    "backend-privileged",
                    "verification-contract",
                )
            }
        )
    )
    unknown = sorted(
        gate_id
        for path, text in _workflow_texts()
        for gate_id in extract_gate_invocations(text)
        if gate_id not in GATES_BY_ID
        and gate_id not in (*BACKEND_CONTOURS, BACKEND_AGGREGATE, VERIFICATION_CONTOUR)
        and gate_id != "ci"
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

    catalog_cases = discover_test_cases()
    catalog_failures = catalog_issues(catalog_cases)
    if catalog_failures:
        issues.extend(f"backend catalog: {item}" for item in catalog_failures)
    backend_payload = backend_registry_payload()
    snapshot = backend_payload.get("snapshot", {})
    if not isinstance(snapshot, dict):
        issues.append("backend catalog snapshot metadata is missing")
    else:
        if snapshot.get("test_count") != len(catalog_cases):
            issues.append("backend catalog snapshot test count is stale")
        if snapshot.get("backend_test_count") != len(
            cases_for_contour(BACKEND_AGGREGATE, catalog_cases)
        ):
            issues.append("backend catalog aggregate count is stale")
    contour_payload = {
        item.get("id"): item
        for item in backend_payload.get("contours", [])
        if isinstance(item, dict)
    }
    for contour in BACKEND_CONTOURS:
        metadata = contour_payload.get(contour)
        expected_metadata = CONTOUR_METADATA.get(contour)
        if metadata is None or expected_metadata is None:
            issues.append(f"backend catalog metadata is missing for {contour}")
            continue
        for field, expected_value in expected_metadata.items():
            if metadata.get(field) != expected_value:
                issues.append(f"backend catalog metadata drift for {contour}: {field}")

    run_tests_path = PLATFORM_ROOT / "tools" / "platform_run_tests.sh"
    run_tests = run_tests_path.read_text(encoding="utf-8")
    actual_runner_invocation = re.compile(
        r"^exec\s+\"\$PLATFORM_PYTHON_BIN\"\s+tools/platform_test_runner\.py\s+\"\$@\"\s*$",
        re.MULTILINE,
    )
    if not actual_runner_invocation.search(run_tests):
        issues.append(
            "backend runner must execute platform_test_runner.py with the caller arguments"
        )
    if set(TEST_ENV_CONTOURS) != {BACKEND_AGGREGATE, *BACKEND_CONTOURS}:
        issues.append(
            "every backend contour must use the fail-closed test environment guard"
        )
    if "validate_test_resource_configuration" not in run_tests:
        issues.append(
            "platform_run_tests.sh must invoke the strict test resource validator"
        )
    try:
        runner_source = TEST_RUNNER.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(f"catalog test runner is unreadable: {exc}")
    else:
        if not _runner_main_has_environment_guard(runner_source):
            issues.append(
                "catalog test runner main must execute the fail-closed test environment guard"
            )
        if "def validate_test_resource_configuration" not in runner_source:
            issues.append(
                "catalog test runner must define the strict pure test resource validator"
            )

    expected_backend_command = [
        str(run_tests_path),
        "--contour",
        "backend",
    ]
    if _backend_command(()) != expected_backend_command:
        issues.append(
            "backend registry must invoke platform_run_tests.sh with --contour backend"
        )
    for contour in BACKEND_CONTOURS:
        expected_command = [str(run_tests_path), "--contour", contour]
        if _backend_contour_command(contour, ()) != expected_command:
            issues.append(
                f"{contour} registry command must invoke platform_run_tests.sh with its contour"
            )

    verification_cases = cases_for_contour(VERIFICATION_CONTOUR, discover_test_cases())
    verification_ids = {case.test_id for case in verification_cases}
    classifier_ids = {
        case.test_id
        for case in verification_cases
        if case.module == "test_platform_ci_classifier"
    }
    if not classifier_ids:
        issues.append(
            "verification-contract catalog must own test_platform_ci_classifier IDs"
        )
    contract_command, verification_runner = _verification_contract_commands()
    expected_contract_command = [sys.executable, "tools/platform_verify_contract.py"]
    expected_verification_runner = [
        sys.executable,
        str(Path(__file__).resolve().parent / "platform_test_runner.py"),
        "--contour",
        VERIFICATION_CONTOUR,
    ]
    if contract_command != expected_contract_command:
        issues.append(
            "verification-contract must invoke its executable self-test command"
        )
    expected_classifier_count = int(EXPECTED_SNAPSHOT["verification_classifier_test_count"])
    expected_classifier_digest = str(
        EXPECTED_SNAPSHOT["verification_classifier_test_id_digest"]
    )
    if not verification_ids or len(classifier_ids) != expected_classifier_count:
        issues.append(
            "verification-contract catalog must include all "
            f"{expected_classifier_count} CI classifier test IDs"
        )
    classifier_digest = hashlib.sha256(
        "\n".join(sorted(classifier_ids)).encode("utf-8")
    ).hexdigest()
    if classifier_digest != expected_classifier_digest:
        issues.append("verification-contract CI classifier ID snapshot is stale")
    if verification_runner != expected_verification_runner:
        issues.append(
            "verification-contract must have an executable catalog runner invocation"
        )

    try:
        package = json.loads(WEB_PACKAGE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"web package metadata is unreadable: {exc}")
    else:
        scripts = package.get("scripts", {})
        hermetic = scripts.get("test:hermetic")
        if hermetic != "../../tools/platform_web_hermetic.sh":
            issues.append("web hermetic gate must use the single-build hermetic runner")
        if not isinstance(hermetic, str) or any(marker in hermetic for marker in FORBIDDEN_EXCLUSION_MARKERS):
            issues.append("web hermetic scripts contain an exclusion bypass")
        if isinstance(hermetic, str) and ".spec." in hermetic:
            issues.append("web hermetic gate names individual spec files")
        source_contract_script = scripts.get("test:source-contract")
        if not isinstance(source_contract_script, str) or "playwright.source-contract.config.ts" not in source_contract_script:
            issues.append("source-contract assertions must have a dedicated Playwright runner")

    try:
        web_config = WEB_PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(f"web Playwright config is unreadable: {exc}")
    else:
        ignore_match = re.search(r"const sharedIgnoredSpecs = \[(.*?)\]", web_config, re.DOTALL)
        ignored_specs = (
            set(re.findall(r'"([^"]+\.spec\.ts)"', ignore_match.group(1)))
            if ignore_match
            else set()
        )
        expected_ignored_specs = {
            "frontend-audit-regressions.spec.ts",
            "live-launch.spec.ts",
            "live-user-journey.spec.ts",
            "tournament-participant-progressive.spec.ts",
        }
        if ignored_specs != expected_ignored_specs:
            issues.append(
                "web hermetic config must isolate source, live and participant specialized suites"
            )
        if re.search(r"retries:\s*process\.env\.CI", web_config):
            issues.append("web hermetic deterministic config must not retry CI failures")
        if "PLATFORM_WEB_HERMETIC_BUILD_DIR" not in web_config:
            issues.append("web hermetic config must support the prepared standalone build")
        if "trace: \"retain-on-failure\"" not in web_config:
            issues.append("web hermetic failures must retain traces with retries disabled")
        for project_name in ("desktop", "wide-1300", "tablet-820", "mobile-layout"):
            if f'name: "{project_name}"' not in web_config:
                issues.append(f"web hermetic project matrix is missing {project_name}")
        for spec_name in (
            "account-email-flow.spec.ts",
            "admin-progressive-tournaments.spec.ts",
            "bracket-manual-refresh.spec.ts",
            "info-server-boundary.spec.ts",
            "password-change-flow.spec.ts",
            "password-manager-auth-form.spec.ts",
            "password-reset-autofill.spec.ts",
            "ready-check-timer.spec.ts",
            "tournament-list-concurrency.spec.ts",
        ):
            if spec_name not in web_config:
                issues.append(f"desktop-only web regression is missing from project routing: {spec_name}")
        if "reuseExistingServer: true" in web_config:
            issues.append("web hermetic servers must not reuse an ambient process")
    try:
        participant_config = WEB_PARTICIPANT_CONFIG.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(f"participant Playwright config is unreadable: {exc}")
    else:
        if "PLATFORM_WEB_HERMETIC_BUILD_DIR" not in participant_config:
            issues.append("participant hermetic config must consume the prepared build")
        if "apiPort = 3199" not in participant_config:
            issues.append("participant hermetic fixture must use the shared prepared-build API destination")

    try:
        source_contract_config = WEB_SOURCE_CONTRACT_CONFIG.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(f"source-contract Playwright config is unreadable: {exc}")
    else:
        if 'testMatch: "frontend-audit-regressions.spec.ts"' not in source_contract_config:
            issues.append("source-contract runner must own frontend-audit-regressions.spec.ts")
        if "webServer" in source_contract_config:
            issues.append("source-contract runner must not boot browser/API servers")

    if not WEB_HERMETIC_RUNNER.is_file():
        issues.append("single-build web hermetic runner is missing")
    else:
        runner_text = WEB_HERMETIC_RUNNER.read_text(encoding="utf-8")
        if 'PLATFORM_API_BASE_URL="http://127.0.0.1:3199/api/v1"' not in runner_text:
            issues.append("web hermetic build must serialize the shared API rewrite destination")
        if "test:source-contract" not in runner_text:
            issues.append("web hermetic runner must execute source-contract assertions once")

    if not CLASSIFIER_TOOL.is_file():
        issues.append("platform_ci_classifier.py is missing")
    else:
        if tuple(CI_GATE_IDS) != FULL_GATE_IDS:
            issues.append("classifier full route must equal the canonical CI gate registry")
        if not DOCS_ONLY_GATE_IDS or not OUT_OF_SCOPE_GATE_IDS:
            issues.append("classifier reduced routes must have explicit gate ownership")

    auto_text = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    production_text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
    issues.extend(
        _checkout_credential_issues("platform-production-deploy.yml", production_text)
    )
    issues.extend(_production_secret_scope_issues(production_text))
    for workflow_name, workflow_text in (
        ("auto-deploy", auto_text),
        ("production deploy", production_text),
    ):
        for marker in (
            "platform-ci-route-",
            "classifier-manifest.json",
            "target_sha",
            "digest",
        ):
            if marker not in workflow_text:
                issues.append(f"{workflow_name} must validate classifier artifact marker: {marker}")
    for marker in ("classifier_run_id", "classifier_run_attempt"):
        if marker not in auto_text or marker not in production_text:
            issues.append(f"exact classifier provenance input is missing: {marker}")
    if "require_deployable" not in production_text:
        issues.append("production deploy must independently require a deployable classifier route")

    if not GOVERNANCE_DOC.is_file():
        issues.append("test-suite-governance.md is missing")
    else:
        governance_text = GOVERNANCE_DOC.read_text(encoding="utf-8")
        gate_table = governance_text.split("## Verification layers", 1)[1].split("##", 1)[0]
        documented_ids = {
            match.group(1)
            for line in gate_table.splitlines()
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
    conditional = {
        gate_id for gate_id in DETERMINISTIC_GATE_IDS if GATES_BY_ID[gate_id].conditional
    }
    if set(CI_GATE_IDS) != set(DETERMINISTIC_GATE_IDS) - conditional:
        issues.append(
            "CI gate list must equal deterministic registry gates minus conditional gates"
        )

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
        if (
            profile.get("execution", {}).get("generator") == "GitHub-hosted external runner"
            and profile.get("portfolio", {}).get("status") == "active"
            and profile.get("portfolio", {}).get("class") in {"default", "diagnostic"}
        )
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
