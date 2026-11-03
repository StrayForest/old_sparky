#!/usr/bin/env python3
"""Prove exact-source Security authority for a runtime-profile switch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

import platform_workflow_provenance


REPOSITORY = "StrayForest/old_sparky"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
SECURITY_WORKFLOW_NAME = "Platform security and build"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")


class ProfileWorkflowError(ValueError):
    """A closed, non-sensitive profile-workflow validation failure."""


def _api_json(api_base: str, token: str, endpoint: str) -> object:
    if not endpoint.startswith("/") or ".." in endpoint.split("/"):
        raise ProfileWorkflowError("api_endpoint")
    request = urllib.request.Request(
        api_base.rstrip("/") + endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise ProfileWorkflowError("api_status")
            raw = response.read(1024 * 1024 + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise ProfileWorkflowError("api_unavailable") from exc
    if len(raw) > 1024 * 1024:
        raise ProfileWorkflowError("api_oversized")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileWorkflowError("api_json") from exc


def exact_successful_security_run(
    *, api_base: str, token: str, repository: str, target_sha: str,
) -> tuple[str, str]:
    """Return the sole successful push Security run and its exact attempt."""

    if repository != REPOSITORY or SHA_RE.fullmatch(target_sha) is None:
        raise ProfileWorkflowError("source_identity")
    branch = _api_json(api_base, token, f"/repos/{repository}/branches/dev")
    if not isinstance(branch, dict) or branch.get("name") != "dev":
        raise ProfileWorkflowError("branch_identity")
    commit = branch.get("commit")
    if not isinstance(commit, dict) or commit.get("sha") != target_sha:
        raise ProfileWorkflowError("branch_moved")

    workflow = _api_json(
        api_base, token, f"/repos/{repository}/actions/workflows/platform-security.yml"
    )
    if (
        not isinstance(workflow, dict)
        or workflow.get("path") != SECURITY_WORKFLOW_PATH
        or workflow.get("name") != SECURITY_WORKFLOW_NAME
        or workflow.get("state") != "active"
        or type(workflow.get("id")) is not int
        or workflow["id"] <= 0
    ):
        raise ProfileWorkflowError("security_workflow_identity")

    query = urllib.parse.urlencode(
        {"branch": "dev", "event": "push", "head_sha": target_sha, "per_page": 100}
    )
    runs = _api_json(
        api_base,
        token,
        f"/repos/{repository}/actions/workflows/{workflow['id']}/runs?{query}",
    )
    if (
        not isinstance(runs, dict)
        or type(runs.get("total_count")) is not int
        or not isinstance(runs.get("workflow_runs"), list)
        or runs["total_count"] != len(runs["workflow_runs"])
        or len(runs["workflow_runs"]) != 1
    ):
        raise ProfileWorkflowError("security_run_ambiguous")
    row = runs["workflow_runs"][0]
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("repository"), dict)
        or type(row.get("id")) is not int
        or row["id"] <= 0
        or type(row.get("run_attempt")) is not int
        or row["run_attempt"] <= 0
        or row.get("workflow_id") != workflow["id"]
        or row.get("path") != SECURITY_WORKFLOW_PATH
        or row.get("name") != SECURITY_WORKFLOW_NAME
        or row.get("repository", {}).get("full_name") != repository
        or row.get("event") != "push"
        or row.get("head_branch") != "dev"
        or row.get("head_sha") != target_sha
        or row.get("status") != "completed"
        or row.get("conclusion") != "success"
    ):
        raise ProfileWorkflowError("security_run_not_successful")

    attempt_id = str(row["id"])
    attempt = str(row["run_attempt"])
    exact = _api_json(
        api_base,
        token,
        f"/repos/{repository}/actions/runs/{attempt_id}/attempts/{attempt}",
    )
    if (
        not isinstance(exact, dict)
        or not isinstance(exact.get("repository"), dict)
        or exact.get("id") != row["id"]
        or exact.get("run_attempt") != row["run_attempt"]
        or exact.get("workflow_id") != workflow["id"]
        or exact.get("path") != SECURITY_WORKFLOW_PATH
        or exact.get("name") != SECURITY_WORKFLOW_NAME
        or exact.get("repository", {}).get("full_name") != repository
        or exact.get("event") != "push"
        or exact.get("head_branch") != "dev"
        or exact.get("head_sha") != target_sha
        or exact.get("status") != "completed"
        or exact.get("conclusion") != "success"
    ):
        raise ProfileWorkflowError("security_attempt_mismatch")
    statuses = _api_json(
        api_base,
        token,
        f"/repos/{repository}/commits/{target_sha}/statuses?per_page=100",
    )
    if not isinstance(statuses, list):
        raise ProfileWorkflowError("security_status_malformed")
    try:
        platform_workflow_provenance.validate_security_marker(
            workflow,
            exact,
            statuses,
            expected_run_id=row["id"],
            expected_attempt=row["run_attempt"],
            expected_target_sha=target_sha,
            max_age=None,
        )
    except platform_workflow_provenance.ProvenanceError as exc:
        raise ProfileWorkflowError("security_status_mismatch") from exc
    return attempt_id, attempt


def _append_outputs(path: Path, values: dict[str, str]) -> None:
    if not path.is_absolute():
        raise ProfileWorkflowError("output_path")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        for key, value in values.items():
            if re.fullmatch(r"[a-z_]+", key) is None or RUN_ID_RE.fullmatch(value) is None:
                raise ProfileWorkflowError("output_value")
            os.write(descriptor, f"{key}={value}\n".encode("ascii"))
    finally:
        os.close(descriptor)


def main() -> int:
    try:
        token = os.environ.get("GH_TOKEN", "")
        target_sha = os.environ.get("TARGET_SHA", "")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        output = Path(os.environ.get("GITHUB_OUTPUT", ""))
        if not token or not api_base.startswith("https://"):
            raise ProfileWorkflowError("api_configuration")
        run_id, run_attempt = exact_successful_security_run(
            api_base=api_base,
            token=token,
            repository=repository,
            target_sha=target_sha,
        )
        _append_outputs(
            output,
            {"security_run_id": run_id, "security_run_attempt": run_attempt},
        )
        return 0
    except (OSError, ProfileWorkflowError, TypeError, ValueError):
        print("exact-source Security proof is unavailable or invalid", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
