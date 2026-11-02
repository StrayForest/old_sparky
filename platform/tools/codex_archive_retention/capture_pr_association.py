#!/usr/bin/env python3
"""Capture authenticated workflow-to-open-PR identity metadata before merge."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


REPOSITORY = "StrayForest/old_sparky"
WORKFLOWS = {
    (
        "Platform Codex archive retention companion build",
        ".github/workflows/platform-codex-archive-retention-build.yml",
    ),
    ("Platform security and build", ".github/workflows/platform-security.yml"),
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
POSITIVE = re.compile(r"[1-9][0-9]{0,31}\Z")


class CaptureError(ValueError):
    """An authenticated API response cannot form a closed association capture."""


def _api_json(endpoint: str, jq: str) -> dict[str, object]:
    result = subprocess.run(
        ["gh", "api", endpoint, "--jq", jq],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )
    if len(result.stdout.encode("utf-8")) > 32 * 1024:
        raise CaptureError("GitHub API identity response exceeded its bound")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise CaptureError("GitHub API identity response is malformed")
    return payload


def _capture(run: dict[str, object], pr: dict[str, object], *, run_id: str, attempt: str, pr_number: str) -> dict[str, object]:
    name, path = run.get("name"), run.get("path")
    if (name, path) not in WORKFLOWS:
        raise CaptureError("workflow identity is not an approved companion workflow")
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status not in {"queued", "pending", "waiting", "requested", "in_progress", "completed"}:
        raise CaptureError("workflow attempt is not capturable")
    if (status in {"queued", "pending", "waiting", "requested", "in_progress"} and conclusion is not None) or (
        status == "completed"
        and conclusion not in {"success", "failure", "cancelled", "skipped", "timed_out", "action_required", "neutral", "stale"}
    ):
        raise CaptureError("workflow attempt status and conclusion disagree")
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) != 1 or not isinstance(pull_requests[0], dict):
        raise CaptureError("workflow attempt does not have one open-PR association")
    association = pull_requests[0]
    association_head_repository = association.get("head_repository_full_name")
    association_base_repository = association.get("base_repository_full_name")
    if association_head_repository is None:
        association_head_repository = run.get("head_repository_full_name")
    if association_base_repository is None:
        association_base_repository = run.get("repository_full_name")
    if any(
        (
            str(run.get("id")) != run_id,
            str(run.get("run_attempt")) != attempt,
            run.get("event") != "pull_request",
            run.get("repository_full_name") != REPOSITORY,
            run.get("head_repository_full_name") != REPOSITORY,
            not isinstance(run.get("head_sha"), str) or HEX40.fullmatch(run["head_sha"]) is None,
            str(association.get("number")) != pr_number,
            association.get("head_sha") != run.get("head_sha"),
            association_head_repository != REPOSITORY,
            association.get("base_ref") != "dev",
            association_base_repository != REPOSITORY,
            not isinstance(association.get("base_sha"), str)
            or HEX40.fullmatch(association["base_sha"]) is None,
            pr.get("number") != int(pr_number),
            pr.get("state") != "open",
            pr.get("merged_at") is not None,
            pr.get("head_sha") != run.get("head_sha"),
            pr.get("head_repository_full_name") != REPOSITORY,
            pr.get("base_ref") != "dev",
            pr.get("base_sha") != association.get("base_sha"),
            pr.get("base_repository_full_name") != REPOSITORY,
        )
    ):
        raise CaptureError("workflow and open-PR API identities do not match")
    return {
        "schema_version": 1,
        "run": {
            "id": int(run_id),
            "run_attempt": int(attempt),
            "name": name,
            "path": path,
            "event": run["event"],
            "status": status,
            "conclusion": conclusion,
            "repository": run["repository_full_name"],
            "head_repository": run["head_repository_full_name"],
            "head_sha": run["head_sha"],
            "pull_request_number": int(pr_number),
            "pull_request_head_sha": association["head_sha"],
            "pull_request_head_repository": association_head_repository,
            "pull_request_base_ref": association["base_ref"],
            "pull_request_base_sha": association["base_sha"],
            "pull_request_base_repository": association_base_repository,
        },
        "pull_request": {
            "number": int(pr_number),
            "state": pr["state"],
            "merged_at": pr["merged_at"],
            "head_sha": pr["head_sha"],
            "head_repository": pr["head_repository_full_name"],
            "base_ref": pr["base_ref"],
            "base_sha": pr["base_sha"],
            "base_repository": pr["base_repository_full_name"],
        },
    }


def capture(run_id: str, attempt: str, pr_number: str, output: Path) -> None:
    if not POSITIVE.fullmatch(run_id) or not POSITIVE.fullmatch(attempt) or not re.fullmatch(r"[1-9][0-9]{0,8}", pr_number):
        raise CaptureError("run, attempt, or PR number is malformed")
    run_jq = (
        "{id,run_attempt,name,path,event,status,conclusion,head_sha,"
        "repository_full_name:.repository.full_name,"
        "head_repository_full_name:.head_repository.full_name,"
        "pull_requests:[.pull_requests[] | {number,head_sha:.head.sha,"
        "head_repository_full_name:.head.repo.full_name,base_ref:.base.ref,"
        "base_sha:.base.sha,base_repository_full_name:.base.repo.full_name}]}"
    )
    pr_jq = (
        "{number,state,merged_at,head_sha:.head.sha,"
        "head_repository_full_name:.head.repo.full_name,base_ref:.base.ref,"
        "base_sha:.base.sha,base_repository_full_name:.base.repo.full_name}"
    )
    subprocess.run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    run = _api_json(
        f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}", run_jq
    )
    pr = _api_json(f"repos/{REPOSITORY}/pulls/{pr_number}", pr_jq)
    snapshot = _capture(run, pr, run_id=run_id, attempt=attempt, pr_number=pr_number)

    if not output.is_absolute():
        raise CaptureError("capture output path must be absolute")
    parent = output.parent
    if parent.resolve(strict=True) != parent:
        raise CaptureError("capture output parent path must not traverse symlinks")
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise CaptureError("capture output parent must be an owned mode-0700 directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(output, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise CaptureError("capture output file identity is unsafe")
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            descriptor = -1
            stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = output.lstat()
        except OSError:
            current = None
        if current is not None and stat.S_ISREG(current.st_mode) and current.st_uid == os.geteuid():
            output.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        capture(args.run_id, args.attempt, args.pull_request, args.output)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, CaptureError) as exc:
        print(f"Codex PR association capture rejected: {exc}", file=sys.stderr)
        return 1
    print("Authenticated workflow-to-open-PR identity captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
