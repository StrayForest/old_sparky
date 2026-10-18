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
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
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
