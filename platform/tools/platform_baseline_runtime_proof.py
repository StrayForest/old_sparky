#!/usr/bin/env python3
"""Validate the exact parent-correlated baseline runtime proof receipt."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

platform_root = Path(__file__).resolve().parents[1]
target = os.environ.get("TARGET_SHA", "")
workspace = Path(os.environ.get("GITHUB_WORKSPACE", ""))
requirement_sha = re.compile(r"[0-9a-f]{40}\Z")
decimal = re.compile(r"[1-9][0-9]{0,31}\Z")
timestamp_pattern = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def identity(name):
    value = os.environ.get(name, "")
    require(decimal.fullmatch(value) is not None, f"{name} is malformed")
    return value


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "receipt contains duplicate JSON keys")
        result[key] = value
    return result


def parse_timestamp(value):
    require(isinstance(value, str) and timestamp_pattern.fullmatch(value) is not None, "workflow timestamp is malformed")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit("workflow timestamp is malformed") from None


def api_json(url, *, max_bytes=4 * 1024 * 1024):
    require(url.startswith(api_base + "/"), "GitHub API URL is not canonical")
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with opener.open(request, timeout=30) as response:
            final_url = urlsplit(response.geturl())
            require(
                final_url.scheme == "https"
                and final_url.hostname == api_host
                and final_url.username is None
                and final_url.password is None,
                "GitHub API response redirected outside the canonical API host",
            )
            body = response.read(max_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise SystemExit("GitHub API request failed") from None
    require(len(body) <= max_bytes, "GitHub API response exceeded its bound")
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=strict_object)
    except (UnicodeError, json.JSONDecodeError):
        raise SystemExit("GitHub API JSON is malformed") from None


def paginated_object(url, key, *, maximum=1000):
    rows = []
    expected_total = None
    for page in range(1, 21):
        separator = "&" if "?" in url else "?"
        payload = api_json(f"{url}{separator}{urlencode({'per_page': 100, 'page': page})}")
        require(isinstance(payload, dict) and isinstance(payload.get(key), list), "GitHub API page is malformed")
        current = payload[key]
        total = payload.get("total_count")
        require(type(total) is int and 0 <= total <= maximum, "GitHub API total count is invalid")
        if expected_total is None:
            expected_total = total
        require(total == expected_total, "GitHub API pagination changed during validation")
        rows.extend(current)
        require(len(rows) <= maximum, "GitHub API result exceeded its bound")
        if len(rows) == expected_total:
            return rows
        require(len(current) == 100, "GitHub API pagination is incomplete")
    raise SystemExit("GitHub API pagination exceeded its bound")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        target_url = urlsplit(new_url)
        require(
            target_url.scheme == "https"
            and target_url.hostname is not None
            and target_url.username is None
            and target_url.password is None
            and target_url.fragment == "",
            "artifact redirect URL is unsafe",
        )
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if redirected is not None and target_url.hostname != api_host:
            redirected.remove_header("Authorization")
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


api_base = os.environ.get("GITHUB_API_URL", "").rstrip("/")
api_host = urlsplit(api_base).hostname
server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
repository = os.environ.get("GITHUB_REPOSITORY", "")
token = os.environ.get("GH_TOKEN", "")
require(
    api_base == "https://api.github.com"
    and api_host == "api.github.com"
    and server == "https://github.com"
    and repository == "StrayForest/old_sparky"
    and bool(token),
    "GitHub API environment is not canonical",
)
opener = build_opener(SafeRedirect())

requirement = re.compile(r"[0-9a-f]{40}\Z")
require(requirement.fullmatch(target) is not None, "target SHA is malformed")
require(os.environ.get("GITHUB_SHA") == target, "parent target does not equal workflow SHA")
require(os.environ.get("GITHUB_REF") == "refs/heads/dev", "parent is not running on dev")
parent_id = identity("GITHUB_RUN_ID")
parent_attempt = identity("GITHUB_RUN_ATTEMPT")
security_id = identity("SOURCE_SECURITY_RUN_ID")
security_attempt = identity("SOURCE_SECURITY_RUN_ATTEMPT")
auto_id = identity("AUTODEPLOY_RUN_ID")
auto_attempt = identity("AUTODEPLOY_RUN_ATTEMPT")

sys.path.insert(0, str(platform_root))
from tools.platform_deploy_baseline import (  # noqa: E402
    BASELINE_RUNTIME_TITLE_RE,
    validate_baseline_runtime_proof,
)
from tools.platform_workflow_provenance import (  # noqa: E402
    BASELINE_RUNTIME_STATUS_CONTEXT,
    SECURITY_SUCCESS_DESCRIPTION,
    latest_context_status,
    validate_actions_bot_status,
)

git_head = subprocess.run(
    ["git", "-C", str(workspace), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
    timeout=10,
).stdout.strip()
git_dirty = subprocess.run(
    ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all", "--", "platform"],
    check=True,
    capture_output=True,
    text=True,
    timeout=10,
).stdout
require(git_head == target and not git_dirty, "validator checkout is not the clean exact target")

parent_title = (
    f"Platform production deploy mode=baseline-reconcile target={target} "
    f"source={security_id}.{security_attempt} auto={auto_id}.{auto_attempt}"
)
parent_workflow = api_json(f"{api_base}/repos/{repository}/actions/workflows/platform-production-deploy.yml")
require(
    isinstance(parent_workflow, dict)
    and parent_workflow.get("path") == ".github/workflows/platform-production-deploy.yml"
    and parent_workflow.get("name") == "Platform production deploy"
    and type(parent_workflow.get("id")) is int
    and parent_workflow["id"] > 0,
    "parent workflow identity is invalid",
)
parent = api_json(
    f"{api_base}/repos/{repository}/actions/runs/{parent_id}/attempts/{parent_attempt}"
)
parent_repo = parent.get("repository") if isinstance(parent, dict) else None
require(
    isinstance(parent, dict)
    and type(parent.get("id")) is int and str(parent["id"]) == parent_id
    and type(parent.get("run_attempt")) is int and str(parent["run_attempt"]) == parent_attempt
    and parent.get("workflow_id") == parent_workflow["id"]
    and parent.get("path") in {
        ".github/workflows/platform-production-deploy.yml@dev",
        ".github/workflows/platform-production-deploy.yml@refs/heads/dev",
    }
    and parent.get("name") == "Platform production deploy"
    and parent.get("event") == "workflow_dispatch"
    and parent.get("head_branch") == "dev"
    and parent.get("head_sha") == target
    and parent.get("display_title") == parent_title
    and parent.get("status") == "in_progress"
    and parent.get("actor", {}).get("login") == "github-actions[bot]"
    and isinstance(parent_repo, dict) and parent_repo.get("full_name") == repository,
    "parent run is not the exact active baseline reconciler",
)
parent_jobs = paginated_object(
    f"{api_base}/repos/{repository}/actions/runs/{parent_id}/attempts/{parent_attempt}/jobs",
    "jobs",
    maximum=500,
)
dispatch_name = "Dispatch exact-target baseline runtime proof"
dispatch_jobs = [row for row in parent_jobs if isinstance(row, dict) and row.get("name") == dispatch_name]
require(
    len(dispatch_jobs) == 1
    and dispatch_jobs[0].get("status") == "completed"
    and dispatch_jobs[0].get("conclusion") == "success",
    "exact child-dispatch parent job did not succeed exactly once",
)
steps = dispatch_jobs[0].get("steps")
dispatch_steps = [row for row in steps if isinstance(row, dict) and row.get("name") == dispatch_name] if isinstance(steps, list) else []
require(
    len(dispatch_steps) == 1
    and dispatch_steps[0].get("status") == "completed"
    and dispatch_steps[0].get("conclusion") == "success",
    "exact child-dispatch parent step did not succeed exactly once",
)
dispatch_started = parse_timestamp(dispatch_steps[0].get("started_at"))

security_workflow = api_json(f"{api_base}/repos/{repository}/actions/workflows/platform-security.yml")
require(
    isinstance(security_workflow, dict)
    and security_workflow.get("path") == ".github/workflows/platform-security.yml"
    and security_workflow.get("name") == "Platform security and build"
    and type(security_workflow.get("id")) is int
    and security_workflow["id"] > 0,
    "child workflow identity is invalid",
)
runs_url = (
    f"{api_base}/repos/{repository}/actions/workflows/{security_workflow['id']}/runs?"
    + urlencode({"branch": "dev", "event": "workflow_dispatch", "head_sha": target})
)
deadline = time.monotonic() + 90 * 60
child = None
while time.monotonic() < deadline:
    candidates = []
    for row in paginated_object(runs_url, "workflow_runs", maximum=1000):
        if not isinstance(row, dict) or not isinstance(row.get("display_title"), str):
            continue
        marker = BASELINE_RUNTIME_TITLE_RE.fullmatch(row["display_title"])
        if marker is None:
            continue
        fields = marker.groupdict()
        if (
            fields["target"] == target
            and fields["source_id"] == security_id
            and fields["source_attempt"] == security_attempt
            and fields["auto_id"] == auto_id
            and fields["auto_attempt"] == auto_attempt
            and fields["parent_id"] == parent_id
            and fields["parent_attempt"] == parent_attempt
        ):
            candidates.append((row, fields))
    require(len(candidates) <= 1, "multiple child workflow runs match the exact parent correlation")
    if candidates:
        row, fields = candidates[0]
        require(
            type(row.get("id")) is int and str(row["id"]) == fields["proof_id"]
            and type(row.get("run_attempt")) is int and str(row["run_attempt"]) == fields["proof_attempt"]
            and row.get("workflow_id") == security_workflow["id"]
            and row.get("path") in {
                ".github/workflows/platform-security.yml@dev",
                ".github/workflows/platform-security.yml@refs/heads/dev",
            }
            and row.get("name") == "Platform security and build"
            and row.get("event") == "workflow_dispatch"
            and row.get("head_branch") == "dev"
            and row.get("head_sha") == target
            and row.get("actor", {}).get("login") == "github-actions[bot]"
            and parse_timestamp(row.get("created_at")) >= dispatch_started,
            "child run does not bind to the exact dispatch job and target",
        )
        if row.get("status") == "completed":
            require(row.get("conclusion") == "success", "exact baseline runtime child did not succeed")
            child = (str(row["id"]), str(row["run_attempt"]))
            break
        require(row.get("status") in {"queued", "in_progress", "waiting", "pending"}, "child workflow state is invalid")
    time.sleep(min(15, max(0, deadline - time.monotonic())))
require(child is not None, "timed out waiting for exact baseline runtime child")
child_id, child_attempt = child
child_attempt_url = f"{api_base}/repos/{repository}/actions/runs/{child_id}/attempts/{child_attempt}"
child_run = api_json(child_attempt_url)
require(
    isinstance(child_run, dict)
    and child_run.get("status") == "completed"
    and child_run.get("conclusion") == "success"
    and child_run.get("display_title") == row["display_title"]
    and child_run.get("id") == int(child_id)
    and child_run.get("run_attempt") == int(child_attempt)
    and child_run.get("workflow_id") == security_workflow["id"]
    and child_run.get("path") in {
        ".github/workflows/platform-security.yml@dev",
        ".github/workflows/platform-security.yml@refs/heads/dev",
    }
    and child_run.get("event") == "workflow_dispatch"
    and child_run.get("head_branch") == "dev"
    and child_run.get("head_sha") == target
    and child_run.get("actor", {}).get("login") == "github-actions[bot]",
    "exact child workflow attempt metadata is mismatched",
)
child_jobs = paginated_object(
    f"{api_base}/repos/{repository}/actions/runs/{child_id}/attempts/{child_attempt}/jobs",
    "jobs",
    maximum=500,
)
def fetch_statuses():
    rows = []
    for page in range(1, 101):
        url = f"{api_base}/repos/{repository}/commits/{target}/statuses?{urlencode({'per_page': 100, 'page': page})}"
        statuses_page = api_json(url)
        require(
            isinstance(statuses_page, list) and len(statuses_page) <= 100,
            "commit status page is malformed",
        )
        rows.extend(statuses_page)
        require(len(rows) <= 10000, "commit status response exceeded its bound")
        if len(statuses_page) < 100:
            return rows
    raise SystemExit("commit status pagination exceeded its page bound")


# For this internal lane, the external workflow_run finalizer is the sole
# terminal writer. The child status-start row remains pending until that
# finalizer publishes its attempt-bound terminal marker. Treat that marker as
# required evidence, while the child run/job/receipt validators remain the
# authority for runtime-gate success.
terminal_url = (
    f"https://github.com/{repository}/actions/runs/{child_id}/attempts/{child_attempt}"
)
terminal_deadline = time.monotonic() + 10 * 60
status_rows = []
while time.monotonic() < terminal_deadline:
    status_rows = fetch_statuses()
    try:
        marker = latest_context_status(
            status_rows,
            context=BASELINE_RUNTIME_STATUS_CONTEXT,
            max_age=None,
        )
    except ValueError as exc:
        if str(exc) != f"{BASELINE_RUNTIME_STATUS_CONTEXT} status is missing":
            raise SystemExit("baseline runtime terminal status is malformed or ambiguous") from None
        marker = None
    if marker is None:
        time.sleep(min(5, max(0, terminal_deadline - time.monotonic())))
        continue
    require(
        marker.get("target_url") == terminal_url,
        "baseline runtime status targets a different attempt",
    )
    state = marker.get("state")
    if state == "pending":
        validate_actions_bot_status(
            marker,
            expected_context=BASELINE_RUNTIME_STATUS_CONTEXT,
            expected_state="pending",
            expected_target_url=terminal_url,
            expected_description="Platform security and build is running",
        )
        time.sleep(min(5, max(0, terminal_deadline - time.monotonic())))
        continue
    if state == "success":
        validate_actions_bot_status(
            marker,
            expected_context=BASELINE_RUNTIME_STATUS_CONTEXT,
            expected_state="success",
            expected_target_url=terminal_url,
            expected_description=SECURITY_SUCCESS_DESCRIPTION,
        )
        break
    if state == "failure":
        validate_actions_bot_status(
            marker,
            expected_context=BASELINE_RUNTIME_STATUS_CONTEXT,
            expected_state="failure",
            expected_target_url=terminal_url,
            expected_description="Platform security or build failed",
        )
        raise SystemExit("baseline runtime finalizer published failure")
    raise SystemExit("baseline runtime status state is invalid")
else:
    raise SystemExit("timed out waiting for the exact baseline runtime terminal marker")
statuses_complete = True

artifact_name = f"platform-baseline-runtime-receipt-{child_id}-{child_attempt}"
artifacts = paginated_object(
    f"{api_base}/repos/{repository}/actions/runs/{child_id}/artifacts",
    "artifacts",
    maximum=500,
)
matching_artifacts = [row for row in artifacts if isinstance(row, dict) and row.get("name") == artifact_name]
require(len(matching_artifacts) == 1, "exact receipt artifact is absent or duplicated")
listed_artifact = matching_artifacts[0]
require(
    type(listed_artifact.get("id")) is int
    and listed_artifact["id"] > 0
    and listed_artifact.get("expired") is False
    and type(listed_artifact.get("size_in_bytes")) is int
    and 0 < listed_artifact["size_in_bytes"] <= 2 * 1024 * 1024,
    "receipt artifact list metadata is invalid or oversized",
)
artifact_id = str(listed_artifact["id"])
artifact = api_json(f"{api_base}/repos/{repository}/actions/artifacts/{artifact_id}")
artifact_run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
require(
    isinstance(artifact, dict)
    and type(artifact.get("id")) is int
    and artifact.get("id") == listed_artifact["id"]
    and artifact.get("name") == artifact_name
    and artifact.get("expired") is False
    and type(artifact.get("size_in_bytes")) is int
    and artifact.get("size_in_bytes") == listed_artifact["size_in_bytes"]
    and re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", ""))) is not None
    and isinstance(artifact_run, dict)
    and artifact_run.get("id") == int(child_id)
    and artifact_run.get("head_sha") == target
    and artifact_run.get("head_branch") == "dev",
    "receipt artifact metadata is not bound to the exact child run",
)
archive_url = f"{api_base}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
archive_request = Request(archive_url, headers={"Accept": "application/vnd.github+json"})
archive_request.add_unredirected_header("Authorization", "Bearer " + token)
try:
    with opener.open(archive_request, timeout=60) as response:
        archive_bytes = response.read(2 * 1024 * 1024 + 1)
except (HTTPError, URLError, TimeoutError, OSError):
    raise SystemExit("receipt artifact download failed") from None
require(
    0 < len(archive_bytes) <= 2 * 1024 * 1024
    and hashlib.sha256(archive_bytes).hexdigest() == artifact["digest"].removeprefix("sha256:"),
    "receipt artifact size or digest is invalid",
)
try:
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r", allowZip64=False) as archive:
        entries = archive.infolist()
        require(len(entries) == 1, "receipt archive inventory is invalid")
        entry = entries[0]
        mode = (entry.external_attr >> 16) & 0o170000
        require(
            entry.filename == "platform-baseline-runtime-receipt.json"
            and not entry.is_dir()
            and mode in {0, 0o100000}
            and 0 < entry.file_size <= 16 * 1024,
            "receipt archive member is invalid",
        )
        receipt_bytes = archive.read(entry)
except (OSError, zipfile.BadZipFile, RuntimeError):
    raise SystemExit("receipt ZIP is malformed") from None
require(len(receipt_bytes) <= 16 * 1024, "receipt JSON exceeded its bound")
try:
    receipt = json.loads(receipt_bytes.decode("utf-8"), object_pairs_hook=strict_object)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit("receipt JSON is malformed") from None
require(isinstance(receipt, dict), "receipt is not an object")

security_workflow_data = security_workflow
validated = validate_baseline_runtime_proof(
    security_workflow_data,
    child_run,
    child_jobs,
    status_rows,
    receipt,
    expected_target_sha=target,
    source_security_run_id=security_id,
    source_security_attempt=security_attempt,
    autodeploy_run_id=auto_id,
    autodeploy_attempt=auto_attempt,
    production_deploy_run_id=parent_id,
    production_deploy_attempt=parent_attempt,
    jobs_complete=True,
    statuses_complete=statuses_complete,
)
require(
    validated.get("attempt_url") == f"https://github.com/{repository}/actions/runs/{child_id}/attempts/{child_attempt}",
    "baseline runtime helper returned a mismatched attempt URL",
)
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
    output.write(f"baseline_runtime_child_run_id={child_id}\n")
    output.write(f"baseline_runtime_child_run_attempt={child_attempt}\n")
    output.write(f"baseline_runtime_receipt_artifact_id={artifact_id}\n")
    output.write(f"baseline_runtime_receipt_artifact_name={artifact_name}\n")
    output.write(f"baseline_runtime_receipt_digest={artifact['digest']}\n")
print("Validated exact baseline runtime child, gates, status, and parent-correlated receipt.")
