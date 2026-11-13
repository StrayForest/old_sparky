#!/usr/bin/env python3
"""Run the pinned GitHub Actions workflow checker and context regressions."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


PINNED_VERSION = "1.7.12"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
QUEUE_COMPATIBILITY_FILES = frozenset(
    {
        "platform-live-launch.yml",
        "platform-live-user-qa.yml",
        "platform-postdeploy-live-qa.yml",
    }
)
QUEUE_COMPATIBILITY_DIAGNOSTIC = re.compile(
    r"^(?P<path>(?:[A-Za-z0-9_./-]+/)?\.github/workflows/"
    r"(?P<name>platform-live-launch|platform-live-user-qa|platform-postdeploy-live-qa)\.yml)"
    r":[0-9]+:[0-9]+: unexpected key \"queue\" for \"concurrency\" section\. "
    r"expected one of \"cancel-in-progress\", \"group\" \[syntax-check\]$"
)


class ActionlintError(RuntimeError):
    """Raised when the required pinned actionlint tool cannot run safely."""


def _resolve_binary() -> Path:
    configured = os.environ.get("ACTIONLINT_BIN")
    candidate = configured or shutil.which("actionlint")
    if not candidate:
        raise ActionlintError(
            "LOCAL GATE BLOCKED: pinned actionlint 1.7.12 is unavailable; "
            "CI installs and checksum-verifies the official release in RUNNER_TEMP"
        )

    path = Path(candidate)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ActionlintError(
            "LOCAL GATE BLOCKED: configured actionlint binary is unavailable"
        ) from exc
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise ActionlintError(
            "LOCAL GATE BLOCKED: actionlint must be an executable regular file"
        )
    if metadata.st_size <= 0:
        raise ActionlintError("LOCAL GATE BLOCKED: actionlint binary is empty")

    result = subprocess.run(
        [str(path), "-version"], capture_output=True, text=True, check=False, timeout=10
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or first_line != PINNED_VERSION:
        raise ActionlintError(
            "LOCAL GATE BLOCKED: actionlint binary does not report pinned version 1.7.12"
        )
    return path


def _run_actionlint(binary: Path, files: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), "-shellcheck", "", "-pyflakes", "", *map(str, files)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def _filter_unsupported_queue_diagnostics(
    stdout: str, stderr: str
) -> tuple[str, str, tuple[str, ...]]:
    """Filter only the pinned linter's known native concurrency-queue gap.

    GitHub supports ``concurrency.queue: max``; actionlint 1.7.12 does not yet
    recognize that key. The independent workflow contract checks the exact
    queue policy in these three workflows, so ignore only its exact diagnostic
    when all three expected files produce one occurrence each.
    """

    combined = [("stdout", line) for line in stdout.splitlines(keepends=True)] + [
        ("stderr", line) for line in stderr.splitlines(keepends=True)
    ]
    matched: list[tuple[str, str, str]] = []
    for stream_name, line in combined:
        match = QUEUE_COMPATIBILITY_DIAGNOSTIC.fullmatch(line.rstrip("\r\n"))
        if match is not None:
            matched.append((stream_name, line, match.group("name") + ".yml"))
    names = [name for _, _, name in matched]
    if len(names) != len(QUEUE_COMPATIBILITY_FILES) or set(names) != QUEUE_COMPATIBILITY_FILES:
        return stdout, stderr, ()

    filtered: dict[str, list[str]] = {"stdout": [], "stderr": []}
    for stream_name, line in combined:
        if any(stream_name == source and line == matched_line for source, matched_line, _ in matched):
            continue
        filtered[stream_name].append(line)
    return "".join(filtered["stdout"]), "".join(filtered["stderr"]), tuple(sorted(names))


def _check_runner_context_fixtures(binary: Path) -> None:
    positive = """name: runner context is allowed in step env
on: workflow_dispatch
jobs:
  context:
    runs-on: ubuntu-24.04
    steps:
      - env:
          PLATFORM_RUNNER_ENVIRONMENT: ${{ runner.environment }}
        run: printf '%s\\n' \"$PLATFORM_RUNNER_ENVIRONMENT\"
"""
    negative = """name: runner context is not allowed in job env
on: workflow_dispatch
jobs:
  context:
    runs-on: ubuntu-24.04
    env:
      PLATFORM_RUNNER_ENVIRONMENT: ${{ runner.environment }}
    steps:
      - run: printf '%s\\n' \"$PLATFORM_RUNNER_ENVIRONMENT\"
"""
    with tempfile.TemporaryDirectory(prefix="platform-actionlint-context-") as temporary:
        root = Path(temporary)
        positive_path = root / "positive.yml"
        negative_path = root / "negative.yml"
        positive_path.write_text(positive, encoding="utf-8")
        negative_path.write_text(negative, encoding="utf-8")

        accepted = _run_actionlint(binary, [positive_path])
        if accepted.returncode != 0:
            raise ActionlintError(
                "actionlint rejected the step-scoped runner-context fixture"
            )

        rejected = _run_actionlint(binary, [negative_path])
        diagnostic = rejected.stdout + rejected.stderr
        if rejected.returncode == 0 or re.search(r"runner.*(?:not|unavailable|defined)|(?:not|unavailable|defined).*runner", diagnostic, re.IGNORECASE) is None:
            raise ActionlintError(
                "actionlint did not reject the job-scoped runner-context fixture"
            )


def run() -> int:
    try:
        binary = _resolve_binary()
        _check_runner_context_fixtures(binary)
        workflows = sorted(
            (*REPOSITORY_ROOT.joinpath(".github", "workflows").glob("*.yml"),
             *REPOSITORY_ROOT.joinpath(".github", "workflows").glob("*.yaml"))
        )
        if not workflows:
            raise ActionlintError("workflow directory contains no YAML workflow files")
        result = _run_actionlint(binary, workflows)
        stdout, stderr, suppressed = _filter_unsupported_queue_diagnostics(
            result.stdout, result.stderr
        )
        if suppressed:
            print(
                "actionlint 1.7.12 compatibility: ignored its exact unsupported "
                "concurrency.queue diagnostic for the three live-QA workflows; "
                "workflow contracts validate queue=max and cancel-in-progress=false"
            )
        if stdout:
            sys.stdout.write(stdout)
        if stderr:
            sys.stderr.write(stderr)
        if result.returncode != 0:
            if not suppressed or stdout.strip() or stderr.strip():
                return result.returncode
        print(
            f"actionlint {PINNED_VERSION}: {len(workflows)} workflows and runner-context fixtures passed"
        )
        return 0
    except (ActionlintError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
