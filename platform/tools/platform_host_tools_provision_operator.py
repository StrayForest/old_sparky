#!/usr/bin/env python3
"""Root-only publisher for an exactly attested production host-tools bundle.

This operator utility is independent of the host-tools bundle closure.  It
accepts only the read-only preflight recovery route documented in the
host-tools provisioning ADR: a successful exact-SHA Security run, a signed
bundle from a preflight whose sole failure was the exact missing-generation
check, and an independently verified, current source pin.  Candidate archive
members are treated as data through verification and staging; only the fixed
installed capability command runs after no-replace publication.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import spwd
import stat
import subprocess
import tempfile
import time
import zipfile
from typing import Any, Callable


REPOSITORY = "StrayForest/old_sparky"
TARGET_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
APP = Path("/opt/oldsparky/platform")
SHARED = APP / "shared"
GENERATIONS = SHARED / "host-tools"
SOURCE_ROOT = Path("/root/old_sparky")
PRIVATE_ROOT = Path("/root/.oldsparky")
EXPECTED_PARENT_MEMBERS = {
    "platform-host-tools-bundle.zip",
    "contract/source_sha",
    "contract/toolset_version",
    "contract/manifest.sha256",
    "contract/capabilities.sha256",
    "contract/files.sha256",
    "contract/files.modes",
}
CAPABILITIES_OUTPUT_RE = re.compile(
    rb"HOST_TOOLS schema=1 source_sha=([0-9a-f]{40}) generation=([0-9a-f]{40}) "
    rb"dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 "
    rb"release_baseline=1 retained_load_export_cleanup=1 retained_load_source_binding=1 "
    rb"cpu_diagnostic_plan_control=1 python_isolated=1 python_bytecode_disabled=1\n"
)
TRUSTED_SOURCE_PATHS = (
    "platform/tools/platform_host_tools_provision_operator.py",
    "platform/tools/platform_host_tools_pin.py",
    "platform/tools/platform_host_tools_bundle.py",
    "platform/contracts/host_tools_pin.json",
)


class ProvisionError(Exception):
    """A bounded, non-sensitive refusal stage."""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(stage)


def stop(stage: str) -> None:
    raise ProvisionError(stage)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            stop("duplicate_json_key")
        result[key] = value
    return result


def bounded_json(raw: bytes, *, limit: int, stage: str) -> Any:
    if len(raw) > limit:
        stop(stage)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
    except (UnicodeError, ValueError, RecursionError):
        stop(stage)


def checked_file(path: Path, *, mode: int | None, max_bytes: int, root_owned: bool = True) -> bytes:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > max_bytes
            or (root_owned and before.st_uid != 0)
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        ):
            stop("input_file_metadata")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                stop("input_file_changed")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            ):
                stop("input_file_changed")
            data = b"".join(chunks)
            if len(data) > max_bytes:
                stop("input_file_size")
            return data
        finally:
            os.close(descriptor)
    except OSError:
        stop("input_file_unavailable")


def safe_run(
    argv: list[str], *, env: dict[str, str] | None = None, timeout: int = 30, output_limit: int = 2_000_000
) -> subprocess.CompletedProcess[bytes]:
    clean_env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if env:
        clean_env.update(env)
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=clean_env,
        )
    except (OSError, subprocess.SubprocessError):
        stop("fixed_command_unavailable")
    if len(result.stdout) > output_limit or len(result.stderr) > 65536:
        stop("fixed_command_output_bound")
    return result


def gh_auth_environment() -> dict[str, str]:
    """Forward GH credentials only to the official CLI; never print or persist them."""
    return {name: os.environ[name] for name in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(name)}


def call_gh(
    gh: Path, args: list[str], *, output: Path | None = None, timeout: int = 45,
    output_limit: int = 2_000_000,
) -> bytes:
    metadata = gh.lstat()
    parent = gh.parent.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_nlink != 1
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
        or parent.parent.lstat().st_uid != 0
        or stat.S_IMODE(parent.parent.lstat().st_mode) != 0o700
    ):
        stop("pinned_gh_metadata")
    if output is None:
        result = safe_run([str(gh), *args], timeout=timeout, output_limit=output_limit, env=gh_auth_environment())
        if result.returncode != 0 or result.stderr:
            stop("github_api_read_failed")
        return result.stdout
    result = safe_run(
        [str(gh), *args, "--output", str(output)], timeout=timeout,
        output_limit=0, env=gh_auth_environment(),
    )
    if result.returncode != 0 or result.stderr:
        stop("github_api_download_failed")
    return b""


def verify_gh(gh: Path) -> None:
    version = safe_run([str(gh), "version"], output_limit=4096)
    if version.returncode != 0 or not version.stdout.startswith(b"gh version 2.105.0 "):
        stop("official_gh_version_mismatch")
    supported = safe_run([str(gh), "attestation", "verify", "--help"], output_limit=65536)
    if supported.returncode != 0:
        stop("official_gh_attestation_unavailable")


def api_json(gh: Path, endpoint: str, *, limit: int = 2_000_000) -> dict[str, Any]:
    raw = call_gh(gh, ["api", "--hostname", "github.com", endpoint], output_limit=limit)
    payload = bounded_json(raw, limit=limit, stage="github_api_response_invalid")
    if not isinstance(payload, dict):
        stop("github_api_response_invalid")
    return payload


def id_value(value: object, *, stage: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        stop(stage)
    return value


def validate_workflow_run(
    payload: dict[str, Any],
    *,
    run_id: str,
    attempt: str,
    target_sha: str,
    workflow_name: str,
    workflow_path: str,
    require_success: bool,
) -> None:
    if (
        type(payload.get("id")) is not int
        or payload.get("id") != int(run_id)
        or type(payload.get("run_attempt")) is not int
        or payload.get("run_attempt") != int(attempt)
        or payload.get("name") != workflow_name
        or payload.get("path") != workflow_path
        or payload.get("head_sha") != target_sha
        or payload.get("head_branch") != "dev"
        or payload.get("event") != ("push" if workflow_name == "Platform security and build" else "workflow_dispatch")
        or payload.get("status") != "completed"
        or (require_success and payload.get("conclusion") != "success")
        or not isinstance(payload.get("repository"), dict)
        or payload["repository"].get("full_name") != REPOSITORY
    ):
        stop("workflow_run_binding")


def exact_jobs(payload: dict[str, Any], *, run_id: str, attempt: str, target_sha: str) -> list[dict[str, Any]]:
    jobs = payload.get("jobs")
    if (
        not isinstance(jobs, list) or not jobs or len(jobs) > 100
        or type(payload.get("total_count")) is not int
        or payload.get("total_count") != len(jobs)
    ):
        stop("workflow_jobs_response_invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in jobs:
        if not isinstance(row, dict):
            stop("workflow_jobs_response_invalid")
        name = row.get("name")
        if (
            not isinstance(name, str)
            or name in seen
            or row.get("run_id") != int(run_id)
            or row.get("run_attempt") != int(attempt)
            or row.get("head_sha") != target_sha
        ):
            stop("workflow_job_binding")
        seen.add(name)
        result.append(row)
    return result


def require_success_job(jobs: list[dict[str, Any]], name: str, *, required_step: str | None = None) -> dict[str, Any]:
    matches = [row for row in jobs if row.get("name") == name]
    if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
        stop("required_workflow_job_missing_or_failed")
    if required_step is not None:
        steps = matches[0].get("steps")
        if not isinstance(steps, list) or not any(
            isinstance(row, dict)
            and row.get("name") == required_step
            and row.get("status") == "completed"
            and row.get("conclusion") == "success"
            for row in steps
        ):
            stop("required_workflow_step_missing_or_failed")
    return matches[0]


PREFLIGHT_ALLOWED_SKIPPED_JOBS = frozenset({
    "Validate exact classifier artifact before build",
    "Authenticate active production baseline and cumulative route",
    "Publish exact non-runtime no-op source receipt",
    "Authenticate exact report-only failed release and active baseline",
    "Dispatch exact-target baseline runtime proof",
    "Validate exact-target baseline runtime proof",
    "Build immutable release artifact in CI",
    "Validate exact security provenance",
    "Production preflight",
    "Deploy production",
    "Dispatch automatic post-deploy live QA",
})


def verify_preflight_job_set(jobs: list[dict[str, Any]], *, mode: str) -> dict[str, Any]:
    if mode != "preflight":
        stop("preflight_mode_not_proven")
    names = [row.get("name") for row in jobs]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        stop("preflight_job_names_invalid")
    validate = require_success_job(jobs, "Validate deployment dispatch")
    build = require_success_job(jobs, "Build deterministic host-tools bundle in CI")
    build_steps = build.get("steps")
    required_builder_steps = {
        "Build and verify deterministic host-tools bundle": "success",
        "Publish exact host-tools bundle": "success",
        "Attest exact host-tools bundle": "success",
    }
    if not isinstance(build_steps, list) or any(
        sum(
            isinstance(step, dict)
            and step.get("name") == name
            and step.get("status") == "completed"
            and step.get("conclusion") == conclusion
            for step in build_steps
        ) != 1
        for name, conclusion in required_builder_steps.items()
    ):
        stop("preflight_builder_steps_invalid")
    failed = [row for row in jobs if row.get("conclusion") == "failure"]
    host_gate = [row for row in jobs if row.get("name") == "Verify immutable production host capabilities"]
    if len(failed) != 1 or len(host_gate) != 1 or failed[0] is not host_gate[0]:
        stop("preflight_failure_set")
    if host_gate[0].get("status") != "completed" or host_gate[0].get("conclusion") != "failure":
        stop("preflight_failure_set")
    skipped = [row for row in jobs if row.get("conclusion") == "skipped"]
    if any(row.get("name") not in PREFLIGHT_ALLOWED_SKIPPED_JOBS for row in skipped):
        stop("preflight_unexpected_skipped_job")
    # This job is intentionally not applicable to a read-only preflight. The
    # dispatch-input artifact proves the mode before this function is called.
    not_applicable = [row for row in skipped if row.get("name") == "Validate exact classifier artifact before build"]
    if len(not_applicable) != 1 or not_applicable[0].get("status") != "completed":
        stop("preflight_classifier_skip_mismatch")
    if any(
        row is not host_gate[0]
        and (row.get("status") != "completed" or row.get("conclusion") not in {"success", "skipped"})
        for row in jobs
    ):
        stop("preflight_nonfailure_job_state")
    steps = host_gate[0].get("steps")
    if not isinstance(steps, list):
        stop("preflight_failed_steps_invalid")
    failed_steps = [
        row for row in steps
        if isinstance(row, dict) and row.get("status") == "completed" and row.get("conclusion") == "failure"
    ]
    expected_name = "Validate root SSH identity and installed generation"
    if len(failed_steps) != 1 or failed_steps[0].get("name") != expected_name:
        stop("preflight_failed_step_mismatch")
    other_bad = [
        row for row in steps
        if isinstance(row, dict)
        and row.get("status") == "completed"
        and row.get("name") != expected_name
        and row.get("conclusion") not in {"success", "skipped"}
    ]
    if other_bad:
        stop("preflight_other_step_failure")
    return {"validate_job_id": validate.get("id"), "build_job_id": build.get("id"), "failed_job_id": host_gate[0].get("id")}


def extract_failed_step_log(archive: Path, *, expected_step: str) -> tuple[bytes, str]:
    raw_archive = checked_file(archive, mode=0o600, max_bytes=16 * 1024 * 1024)
    digest = sha256(raw_archive)
    try:
        with zipfile.ZipFile(archive, "r", allowZip64=False) as zipped:
            names = [name for name in zipped.namelist() if name.endswith("/" + expected_step + ".txt")]
            if len(names) != 1 or len(zipped.infolist()) > 256:
                stop("failed_step_log_inventory")
            info = zipped.getinfo(names[0])
            if info.file_size > 2 * 1024 * 1024 or info.is_dir():
                stop("failed_step_log_size")
            return zipped.read(info), digest
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        stop("failed_step_log_archive_invalid")


def parse_missing_generation_log(raw: bytes) -> str:
    """Accept the exact diagnostic only as runtime output, outside echoed script groups."""
    try:
        lines = raw.decode("utf-8", "strict").splitlines()
    except UnicodeError:
        stop("failed_step_log_encoding")
    in_group = False
    diagnostic_count = 0
    exit_count = 0
    group_seen = False
    for line in lines:
        body = re.sub(r"^[0-9T:.Z+-]+\s+", "", line)
        if body.startswith("##[group]Run "):
            if in_group:
                stop("failed_step_log_group_malformed")
            in_group = True
            group_seen = True
            continue
        if in_group:
            if body == "##[endgroup]":
                in_group = False
            continue
        if body == "Exact host-tools generation is not installed yet.":
            diagnostic_count += 1
        elif body == "##[error]Process completed with exit code 10.":
            exit_count += 1
        elif body.startswith("##[error]") or body == "HOST_TOOLS_VERIFY schema=1 status=failed":
            stop("failed_step_log_unexpected_error")
    if in_group or not group_seen or diagnostic_count != 1 or exit_count != 1:
        stop("missing_generation_refusal_unproven")
    return sha256(raw)


def validate_source(source_root: Path, *, target_sha: str) -> str:
    if os.geteuid() != 0:
        stop("root_required")
    if source_root != SOURCE_ROOT:
        stop("source_root_not_canonical")
    root_info = source_root.lstat()
    helper_info = Path(__file__).lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != 0
        or stat.S_IMODE(root_info.st_mode) & 0o022
        or helper_info.st_uid != 0
        or not stat.S_ISREG(helper_info.st_mode)
        or stat.S_IMODE(helper_info.st_mode) & 0o022
    ):
        stop("trusted_operator_source_metadata")
    _verify_source_checkout(source_root, target_sha)
    authoritative = safe_run([
        "/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "8",
        "--max-filesize", "65536", "--header", "Accept: application/vnd.github+json",
        "--header", "Cache-Control: no-cache", f"https://api.github.com/repos/{REPOSITORY}/commits/dev",
    ])
    document = bounded_json(authoritative.stdout, limit=65536, stage="authoritative_dev_head_invalid")
    if authoritative.returncode or not isinstance(document, dict) or document.get("sha") != target_sha:
        stop("authoritative_dev_head_changed")
    resolver = safe_run([
        "/usr/bin/python3.12", "-I", "-B", str(source_root / "platform/tools/platform_host_tools_pin.py"),
        "resolve", "--source-root", str(source_root), "--target-sha", target_sha,
        "--expected-repository", REPOSITORY,
    ], output_limit=4096)
    if resolver.returncode or resolver.stderr:
        stop("target_pin_resolution_failed")
    host_tools_sha = resolver.stdout.decode("ascii", "strict").strip()
    if TARGET_RE.fullmatch(host_tools_sha) is None:
        stop("target_pin_resolution_invalid")
    return host_tools_sha


def _verify_source_checkout(source_root: Path, target_sha: str, *, trusted_uid: int = 0) -> None:
    head = safe_run(["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"])
    origin = safe_run(["/usr/bin/git", "-C", str(source_root), "rev-parse", "origin/dev"])
    status = safe_run([
        "/usr/bin/git", "-C", str(source_root), "status", "--porcelain=v1", "--untracked-files=no",
    ])
    if (
        head.returncode or origin.returncode or status.returncode
        or head.stdout != (target_sha + "\n").encode("ascii")
        or origin.stdout != head.stdout
        or status.stdout.strip()
    ):
        stop("reviewed_source_checkout_changed")
    _verify_trusted_source_files(source_root, target_sha, trusted_uid=trusted_uid)


def _verify_trusted_source_files(source_root: Path, target_sha: str, *, trusted_uid: int = 0) -> None:
    """Bind executable source paths to regular files in the selected commit."""
    for relative in TRUSTED_SOURCE_PATHS:
        path = source_root / relative
        try:
            parents = [source_root / Path(*Path(relative).parts[:index]) for index in range(1, len(Path(relative).parts))]
            parent_infos = [parent.lstat() for parent in parents]
            metadata = path.lstat()
        except OSError:
            stop("trusted_operator_source_unavailable")
        if (
            any(
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != trusted_uid
                or stat.S_IMODE(info.st_mode) & 0o022
                for info in parent_infos
            )
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or metadata.st_size > 2_000_000
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            stop("trusted_operator_source_metadata")
        tree_entry = safe_run([
            "/usr/bin/git", "-C", str(source_root), "ls-tree", "-z", target_sha, "--", relative,
        ], output_limit=4096)
        if tree_entry.returncode != 0:
            stop("trusted_operator_source_tree_unavailable")
        record = tree_entry.stdout[:-1].split(b"\t", 1) if tree_entry.stdout.endswith(b"\0") else []
        if len(record) != 2 or record[1] != relative.encode("ascii"):
            stop("trusted_operator_source_untracked")
        fields = record[0].split()
        if len(fields) != 3 or fields[0] not in (b"100644", b"100755") or fields[1] != b"blob":
            stop("trusted_operator_source_tree_metadata")
        expected_mode = 0o755 if fields[0] == b"100755" else 0o644
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            stop("trusted_operator_source_mode_changed")
        blob = safe_run([
            "/usr/bin/git", "-C", str(source_root), "cat-file", "blob", f"{target_sha}:{relative}",
        ], output_limit=2_000_000)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    metadata.st_dev, metadata.st_ino, metadata.st_size,
                ):
                    stop("trusted_operator_source_changed")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 2_000_000:
                        stop("trusted_operator_source_size")
                after = os.fstat(descriptor)
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
                ):
                    stop("trusted_operator_source_changed")
                source_bytes = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError:
            stop("trusted_operator_source_unavailable")
        if blob.returncode != 0 or blob.stdout != source_bytes:
            stop("trusted_operator_source_changed")


def verify_security_and_preflight(
    gh: Path,
    *,
    target_sha: str,
    security_run_id: str,
    security_attempt: str,
    preflight_run_id: str,
    preflight_attempt: str,
    preflight_mode: str,
    scratch: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    security = api_json(gh, f"repos/{REPOSITORY}/actions/runs/{security_run_id}/attempts/{security_attempt}")
    validate_workflow_run(
        security, run_id=security_run_id, attempt=security_attempt, target_sha=target_sha,
        workflow_name="Platform security and build", workflow_path=".github/workflows/platform-security.yml",
        require_success=True,
    )
    security_jobs = exact_jobs(
        api_json(gh, f"repos/{REPOSITORY}/actions/runs/{security_run_id}/attempts/{security_attempt}/jobs?per_page=100"),
        run_id=security_run_id, attempt=security_attempt, target_sha=target_sha,
    )
    if any(row.get("conclusion") not in {"success", "skipped"} for row in security_jobs):
        stop("security_job_failure")
    preflight = api_json(gh, f"repos/{REPOSITORY}/actions/runs/{preflight_run_id}/attempts/{preflight_attempt}")
    validate_workflow_run(
        preflight, run_id=preflight_run_id, attempt=preflight_attempt, target_sha=target_sha,
        workflow_name="Platform production deploy", workflow_path=".github/workflows/platform-production-deploy.yml",
        require_success=False,
    )
    if preflight.get("conclusion") != "failure":
        stop("preflight_not_expected_failure")
    preflight_jobs = exact_jobs(
        api_json(gh, f"repos/{REPOSITORY}/actions/runs/{preflight_run_id}/attempts/{preflight_attempt}/jobs?per_page=100"),
        run_id=preflight_run_id, attempt=preflight_attempt, target_sha=target_sha,
    )
    job_ids = verify_preflight_job_set(preflight_jobs, mode=preflight_mode)
    logs_path = scratch / "failed-job-logs.zip"
    call_gh(gh, ["api", "--hostname", "github.com", f"repos/{REPOSITORY}/actions/jobs/{job_ids['failed_job_id']}/logs"], output=logs_path)
    raw_log, logs_sha256 = extract_failed_step_log(logs_path, expected_step="Validate root SSH identity and installed generation")
    failed_step_log_sha256 = parse_missing_generation_log(raw_log)
    logs_path.unlink()
    return security, preflight, job_ids, logs_sha256, failed_step_log_sha256


def validate_dispatch_input_artifact(
    payload: dict[str, Any], *, artifact_id: str, artifact_name: str, preflight_run_id: str,
    preflight_attempt: str, target_sha: str,
) -> None:
    if (
        type(payload.get("id")) is not int or payload.get("id") != int(artifact_id)
        or payload.get("name") != artifact_name or payload.get("expired") is not False
        or not isinstance(payload.get("workflow_run"), dict)
        or payload["workflow_run"].get("id") != int(preflight_run_id)
        or payload["workflow_run"].get("head_sha") != target_sha
        or payload["workflow_run"].get("head_branch") != "dev"
        or type(payload.get("size_in_bytes")) is not int
        or not 0 < payload["size_in_bytes"] <= 1024 * 1024
        or not isinstance(payload.get("digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", payload["digest"]) is None
    ):
        stop("dispatch_input_artifact_binding")


def download_artifact(gh: Path, *, artifact_id: str, destination: Path) -> tuple[dict[str, Any], str, str]:
    metadata = api_json(gh, f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}", limit=65536)
    if (
        type(metadata.get("id")) is not int or metadata.get("id") != int(artifact_id)
        or metadata.get("expired") is not False
        or type(metadata.get("size_in_bytes")) is not int
        or not 0 < metadata["size_in_bytes"] <= 8 * 1024 * 1024
        or not isinstance(metadata.get("digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["digest"]) is None
    ):
        stop("artifact_metadata_invalid")
    call_gh(gh, ["api", "--hostname", "github.com", f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"], output=destination)
    outer = checked_file(destination, mode=0o600, max_bytes=8 * 1024 * 1024)
    digest = sha256(outer)
    if len(outer) != metadata["size_in_bytes"] or f"sha256:{digest}" != metadata["digest"]:
        stop("artifact_archive_digest_mismatch")
    return metadata, digest, metadata["name"] if isinstance(metadata.get("name"), str) else ""


def read_dispatch_input_artifact(
    gh: Path, *, artifact_id: str, preflight_run_id: str, preflight_attempt: str,
    target_sha: str, classifier_run_id: str, classifier_run_attempt: str, destination: Path,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    artifact_name = f"platform-production-deploy-input-{preflight_run_id}-{preflight_attempt}"
    metadata = api_json(gh, f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}", limit=65536)
    validate_dispatch_input_artifact(
        metadata, artifact_id=artifact_id, artifact_name=artifact_name,
        preflight_run_id=preflight_run_id, preflight_attempt=preflight_attempt,
        target_sha=target_sha,
    )
    downloaded_metadata, digest, downloaded_name = download_artifact(
        gh, artifact_id=artifact_id, destination=destination,
    )
    validate_dispatch_input_artifact(
        downloaded_metadata, artifact_id=artifact_id, artifact_name=artifact_name,
        preflight_run_id=preflight_run_id, preflight_attempt=preflight_attempt,
        target_sha=target_sha,
    )
    if downloaded_name != artifact_name:
        stop("dispatch_input_artifact_name")
    try:
        with zipfile.ZipFile(destination, "r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != 1 or infos[0].filename != "platform-production-deploy-input.json":
                stop("dispatch_input_inventory")
            info = infos[0]
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in {0, stat.S_IFREG} or info.file_size > 65536:
                stop("dispatch_input_member_metadata")
            raw = archive.read(info)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        stop("dispatch_input_archive_invalid")
    payload = bounded_json(raw, limit=65536, stage="dispatch_input_invalid")
    expected_keys = {
        "schema", "mode", "runtime_profile", "release_slug", "target_sha",
        "artifact_remote_dir", "classifier_run_id", "classifier_run_attempt", "web_compression",
    }
    if (
        not isinstance(payload, dict) or set(payload) != expected_keys
        or payload.get("schema") not in (1, "1") or payload.get("mode") != "preflight"
        or payload.get("target_sha") != target_sha
        or payload.get("classifier_run_id") != classifier_run_id
        or payload.get("classifier_run_attempt") != classifier_run_attempt
    ):
        stop("dispatch_input_mode_or_source_mismatch")
    return payload, downloaded_metadata, digest


def unpack_outer_artifact(outer_path: Path, destination: Path) -> tuple[Path, dict[str, bytes]]:
    try:
        with zipfile.ZipFile(outer_path, "r", allowZip64=False) as archive:
            infos = archive.infolist()
            names = {item.filename for item in infos if not item.is_dir()}
            directory_entries = [item for item in infos if item.is_dir()]
            if names != EXPECTED_PARENT_MEMBERS or len(directory_entries) > 1 or any(item.filename != "contract/" for item in directory_entries):
                stop("outer_artifact_inventory")
            contents: dict[str, bytes] = {}
            for item in infos:
                kind = stat.S_IFMT(item.external_attr >> 16)
                mode = (item.external_attr >> 16) & 0o777
                if item.is_dir():
                    continue
                if kind not in {0, stat.S_IFREG} or mode not in {0, 0o600}:
                    stop("outer_artifact_member_metadata")
                if item.file_size > 4 * 1024 * 1024:
                    stop("outer_artifact_member_size")
                contents[item.filename] = archive.read(item)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        stop("outer_artifact_invalid")
    if set(contents) != EXPECTED_PARENT_MEMBERS:
        stop("outer_artifact_inventory")
    bundle_path = destination / "platform-host-tools-bundle.zip"
    contract_dir = destination / "contract"
    contract_dir.mkdir(mode=0o700)
    for name, data in contents.items():
        if name.startswith("contract/"):
            (contract_dir / name.removeprefix("contract/")).write_bytes(data)
            os.chmod(contract_dir / name.removeprefix("contract/"), 0o600)
        else:
            bundle_path.write_bytes(data)
            os.chmod(bundle_path, 0o600)
    return bundle_path, contents


def validate_bundle_contract(bundle_path: Path, raw_contracts: dict[str, bytes], host_tools_sha: str, source_root: Path) -> tuple[dict[str, Any], dict[str, bytes], str, str]:
    module_path = source_root / "platform/tools/platform_host_tools_bundle.py"
    spec = importlib.util.spec_from_file_location("trusted_host_tools_bundle_verifier", module_path)
    if spec is None or spec.loader is None:
        stop("trusted_bundle_verifier_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    summary = module.verify_bundle(bundle_path, expected_source_sha=host_tools_sha)
    manifest = summary["manifest"]
    members = summary["members"]
    toolset = manifest.get("toolset_version")
    if toolset != "production-host-tools-v4" or len(members) != 17:
        stop("host_tools_layout_not_supported")
    if raw_contracts.get("contract/source_sha") != (host_tools_sha + "\n").encode("ascii"):
        stop("outer_contract_source_mismatch")
    if raw_contracts.get("contract/toolset_version") != (str(toolset) + "\n").encode("ascii"):
        stop("outer_contract_toolset_mismatch")
    if raw_contracts.get("contract/manifest.sha256", b"").strip().decode("ascii", "strict") != summary["manifest_sha256"]:
        stop("outer_manifest_digest_mismatch")
    if raw_contracts.get("contract/capabilities.sha256", b"").strip().decode("ascii", "strict") != summary["capabilities_sha256"]:
        stop("outer_capabilities_digest_mismatch")
    rows = manifest["files"]
    expected_files = {row["path"]: row for row in rows}
    file_contract: dict[str, str] = {}
    for line in raw_contracts.get("contract/files.sha256", b"").decode("ascii", "strict").splitlines():
        digest, path = line.split("  ", 1)
        if path in file_contract:
            stop("outer_files_contract_duplicate")
        file_contract[path] = digest
    expected_hashes = {path: row["sha256"] for path, row in expected_files.items()}
    if file_contract != expected_hashes:
        stop("outer_files_contract_mismatch")
    mode_contract: dict[str, int] = {}
    for line in raw_contracts.get("contract/files.modes", b"").decode("ascii", "strict").splitlines():
        mode, path = line.split(" ", 1)
        if path in mode_contract:
            stop("outer_modes_contract_duplicate")
        mode_contract[path] = int(mode, 8)
    expected_modes = {path: row["mode"] for path, row in expected_files.items()}
    if mode_contract != expected_modes:
        stop("outer_modes_contract_mismatch")
    return manifest, members, str(toolset), sha256(raw_contracts["contract/files.sha256"])


def verify_attestation(
    gh: Path, bundle_path: Path, *, target_sha: str, preflight_run_id: str, preflight_attempt: str,
    bundle_sha256: str, scratch: Path,
) -> tuple[str, str]:
    result = safe_run([
        str(gh), "attestation", "verify", str(bundle_path), "--repo", REPOSITORY,
        "--signer-workflow", "StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml",
        "--source-ref", "refs/heads/dev", "--source-digest", target_sha, "--format", "json",
    ], output_limit=2 * 1024 * 1024, env=gh_auth_environment())
    if result.returncode != 0 or result.stderr:
        stop("signed_attestation_verification_failed")
    rows = bounded_json(result.stdout, limit=2 * 1024 * 1024, stage="attestation_json_invalid")
    if not isinstance(rows, list) or len(rows) != 1:
        stop("attestation_row_count")
    row = rows[0]
    vr = row.get("verificationResult") if isinstance(row, dict) else None
    if not isinstance(vr, dict) or not isinstance(vr.get("verifiedTimestamps"), list) or not vr["verifiedTimestamps"]:
        stop("attestation_timestamp_missing")
    statement = vr.get("statement")
    signature = vr.get("signature")
    certificate = signature.get("certificate") if isinstance(signature, dict) else None
    if not isinstance(statement, dict) or not isinstance(certificate, dict):
        stop("attestation_statement_missing")
    subjects = statement.get("subject")
    invocation = f"https://github.com/{REPOSITORY}/actions/runs/{preflight_run_id}/attempts/{preflight_attempt}"
    if (
        not isinstance(subjects, list) or len(subjects) != 1
        or subjects[0].get("name") != "platform-host-tools-bundle.zip"
        or subjects[0].get("digest") != {"sha256": bundle_sha256}
        or statement.get("predicateType") != "https://slsa.dev/provenance/v1"
        or certificate.get("sourceRepositoryURI") != f"https://github.com/{REPOSITORY}"
        or certificate.get("sourceRepositoryRef") != "refs/heads/dev"
        or certificate.get("sourceRepositoryDigest") != target_sha
        or certificate.get("githubWorkflowRepository") != REPOSITORY
        or certificate.get("githubWorkflowName") != "Platform production deploy"
        or certificate.get("githubWorkflowRef") != "refs/heads/dev"
        or certificate.get("githubWorkflowSHA") != target_sha
        or certificate.get("githubWorkflowTrigger") != "workflow_dispatch"
        or certificate.get("runInvocationURI") != invocation
    ):
        stop("attestation_certificate_binding")
    predicate = statement.get("predicate", {})
    build = predicate.get("buildDefinition", {}) if isinstance(predicate, dict) else {}
    workflow = build.get("externalParameters", {}).get("workflow", {}) if isinstance(build, dict) else {}
    dependencies = build.get("resolvedDependencies", []) if isinstance(build, dict) else []
    if (
        workflow != {"repository": f"https://github.com/{REPOSITORY}", "path": ".github/workflows/platform-production-deploy.yml", "ref": "refs/heads/dev"}
        or not isinstance(dependencies, list) or len(dependencies) != 1
        or dependencies[0].get("uri") != f"git+https://github.com/{REPOSITORY}@refs/heads/dev"
        or dependencies[0].get("digest", {}).get("gitCommit") != target_sha
        or predicate.get("runDetails", {}).get("metadata", {}).get("invocationId") != invocation
    ):
        stop("attestation_workflow_binding")
    # Keep the parsed verifier output only in memory and bind its digest in the receipt.
    return sha256(result.stdout), invocation


def validate_account_contract(old_host_tools_sha: str) -> tuple[str, str]:
    user_name = group_name = "oldsparky-load-artifacts"
    try:
        user = pwd.getpwnam(user_name)
    except KeyError:
        user = None
    try:
        group = grp.getgrnam(group_name)
    except KeyError:
        group = None
    if user is None and group is None:
        for argv in (
            ["/usr/sbin/groupadd", "--system", group_name],
            ["/usr/sbin/useradd", "--system", "--gid", group_name, "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", user_name],
            ["/usr/sbin/passwd", "--lock", user_name],
        ):
            if safe_run(argv, timeout=10).returncode != 0:
                stop("artifact_owner_creation_failed_inspect")
        try:
            user = pwd.getpwnam(user_name)
            group = grp.getgrnam(group_name)
        except KeyError:
            stop("artifact_owner_postcreate_contract_failed")
        status = "created_and_verified"
    else:
        status = "verified"
    if user is None or group is None:
        stop("artifact_owner_partial_account")
    if (
        user.pw_uid <= 0 or group.gr_gid <= 0 or user.pw_gid != group.gr_gid
        or user.pw_dir != "/nonexistent" or user.pw_shell != "/usr/sbin/nologin"
        or os.getgrouplist(user_name, user.pw_gid) != [group.gr_gid]
        or group.gr_mem
    ):
        stop("artifact_owner_existing_contract_invalid")
    try:
        shadow = spwd.getspnam(user_name)
    except (KeyError, PermissionError):
        stop("artifact_owner_password_state_unavailable")
    if not shadow.sp_pwd or shadow.sp_pwd[0] not in {"!", "*"}:
        stop("artifact_owner_password_unlocked")
    sudo = safe_run(["/usr/bin/sudo", "-n", "-l", "-U", user_name], output_limit=65536)
    if sudo.returncode == 0 or b"not allowed" not in sudo.stdout.lower():
        stop("artifact_owner_sudo_policy_invalid")
    reserved_users = {"oldsparky", "oldsparky-api", "oldsparky-web", "oldsparky-worker", "oldsparky-liveqa"}
    reserved_uids: set[int] = set()
    for reserved in reserved_users:
        try:
            reserved_uids.add(pwd.getpwnam(reserved).pw_uid)
        except KeyError:
            pass
    reserved_gids: set[int] = set()
    for reserved in {"oldsparky-api", "oldsparky-web", "oldsparky-worker", "oldsparky-liveqa"}:
        try:
            reserved_gids.add(grp.getgrnam(reserved).gr_gid)
        except KeyError:
            pass
    if user_name in reserved_users or user.pw_uid == 0 or user.pw_uid in reserved_uids or group.gr_gid in reserved_gids:
        stop("artifact_owner_identity_collision")
    generation = GENERATIONS / old_host_tools_sha
    helper_path = generation / "platform_retained_load_export_executor.py"
    manifest_path = generation / "manifest.json"
    helper_bytes = checked_file(helper_path, mode=0o555, max_bytes=512 * 1024)
    manifest = bounded_json(checked_file(manifest_path, mode=0o444, max_bytes=65536), limit=65536, stage="artifact_owner_manifest_invalid")
    if not isinstance(manifest, dict) or manifest.get("source_sha") != old_host_tools_sha or manifest.get("generation") != old_host_tools_sha:
        stop("artifact_owner_manifest_invalid")
    matches = [
        row for row in manifest.get("files", [])
        if isinstance(row, dict) and row.get("path") == "platform_retained_load_export_executor.py"
    ]
    if len(matches) != 1 or matches[0].get("mode") != 0o555 or matches[0].get("sha256") != sha256(helper_bytes):
        stop("artifact_owner_helper_unbound")
    owner = safe_run([
        "/usr/bin/python3.12", "-I", "-B", str(helper_path), "owner",
    ], timeout=10, output_limit=4096)
    if owner.returncode != 0 or owner.stderr:
        stop("artifact_owner_command_failed")
    identity = bounded_json(owner.stdout, limit=4096, stage="artifact_owner_result_invalid")
    if identity != {"uid": user.pw_uid, "gid": group.gr_gid}:
        stop("artifact_owner_identity_mismatch")
    return status, sha256(helper_bytes)


def rename_noreplace(source: Path, destination: Path) -> None:
    operation = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if operation is None:
        stop("renameat2_unavailable")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        stop("no_replace_destination_exists" if error == errno.EEXIST else "no_replace_rename_failed")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_stage(
    staging: Path, manifest: dict[str, Any], files: dict[str, bytes], *, owner: tuple[int, int] = (0, 0)
) -> tuple[int, int]:
    os.mkdir(staging, 0o700)
    stage = staging.lstat()
    entries = {entry["path"]: entry for entry in manifest["files"]}
    expected_names = set(entries) | {"manifest.json"}
    if set(files) != expected_names:
        stop("staging_inventory_source_mismatch")
    try:
        for name, data in files.items():
            mode = 0o444 if name in {"manifest.json", "capabilities.txt"} else 0o555
            descriptor = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                if (os.geteuid(), os.getegid()) != owner:
                    os.fchown(descriptor, *owner)
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            info = (staging / name).lstat()
            if (
                not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink) != (*owner, mode, 1)
                or sha256((staging / name).read_bytes()) != sha256(data)
                or (name != "manifest.json" and entries[name]["sha256"] != sha256(data))
            ):
                stop("staged_member_verification")
        if {item.name for item in staging.iterdir()} != expected_names:
            stop("staging_inventory_mismatch")
        os.chmod(staging, 0o555)
        fsync_directory(staging)
    except Exception:
        cleanup_stage(staging, files, device=stage.st_dev, inode=stage.st_ino, owner=owner)
        raise
    return stage.st_dev, stage.st_ino


def cleanup_stage(
    staging: Path, files: dict[str, bytes], *, device: int, inode: int, owner: tuple[int, int] = (0, 0)
) -> None:
    try:
        root = staging.lstat()
        if (
            not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode)
            or (root.st_dev, root.st_ino, root.st_uid, root.st_gid) != (device, inode, *owner)
        ):
            stop("staging_cleanup_identity_uncertain")
        children = list(staging.iterdir())
        if any(item.name not in files for item in children):
            stop("staging_cleanup_inventory_uncertain")
        for child in children:
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or (info.st_uid, info.st_gid) != owner or info.st_nlink != 1:
                stop("staging_cleanup_member_uncertain")
            if child.read_bytes() != files[child.name]:
                stop("staging_cleanup_digest_uncertain")
        for child in children:
            os.chmod(child, 0o600)
            child.unlink()
        os.chmod(staging, 0o700)
        staging.rmdir()
        fsync_directory(staging.parent)
    except OSError:
        stop("staging_cleanup_failed")


def tree_matches(path: Path, files: dict[str, bytes], *, owner: tuple[int, int] = (0, 0)) -> bool:
    try:
        root = path.lstat()
        if (
            not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode)
            or (root.st_uid, root.st_gid, stat.S_IMODE(root.st_mode), root.st_nlink) != (*owner, 0o555, 2)
            or {item.name for item in path.iterdir()} != set(files)
        ):
            return False
        for name, data in files.items():
            mode = 0o444 if name in {"manifest.json", "capabilities.txt"} else 0o555
            info = (path / name).lstat()
            if (
                not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink) != (*owner, mode, 1)
                or sha256((path / name).read_bytes()) != sha256(data)
            ):
                return False
        return True
    except OSError:
        return False


def quarantine_exact(
    target: Path, quarantine: Path, *, device: int, inode: int, files: dict[str, bytes], owner: tuple[int, int] = (0, 0)
) -> None:
    try:
        info = target.lstat()
    except OSError:
        stop("post_publish_generation_missing")
    if (info.st_dev, info.st_ino) != (device, inode) or not tree_matches(target, files, owner=owner) or quarantine.exists() or quarantine.is_symlink():
        stop("post_publish_identity_uncertain")
    rename_noreplace(target, quarantine)
    fsync_directory(target.parent)


def publish(
    generation_root: Path, host_tools_sha: str, run_id: str, manifest: dict[str, Any], files: dict[str, bytes], *,
    fail_after_rename: bool = False, rehearsal: bool = False, owner: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    target = generation_root / host_tools_sha
    stage = generation_root / f".stage-{host_tools_sha}-{run_id}"
    quarantine = generation_root / f".failed-{host_tools_sha}-{run_id}"
    if any(path.exists() or path.is_symlink() for path in (target, stage, quarantine)):
        stop("publication_path_already_exists")
    device, inode = write_stage(stage, manifest, files, owner=owner)
    published = False
    try:
        rename_noreplace(stage, target)
        published = True
        if fail_after_rename:
            stop("injected_post_rename_failure")
        fsync_directory(generation_root)
        info = target.lstat()
        if (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink) != (device, inode, *owner, 0o555, 2) or not tree_matches(target, files, owner=owner):
            stop("published_tree_identity")
    except Exception as exc:
        if published:
            quarantine_exact(target, quarantine, device=device, inode=inode, files=files, owner=owner)
            stop("post_publish_failure_quarantined")
        cleanup_stage(stage, files, device=device, inode=inode, owner=owner)
        raise exc
    if rehearsal:
        return {
            "path": str(target), "device": device, "inode": inode,
            "manifest_sha256": sha256(files["manifest.json"]),
            "capabilities_sha256": sha256(files["capabilities.txt"]),
            "self_test": "not_run_rehearsal",
        }
    self_test = safe_run([
        "/usr/bin/python3.12", "-I", "-B", str(target / "platform_workflow_remote_dispatch.py"), "host-capabilities",
    ], timeout=20, output_limit=4096)
    match = CAPABILITIES_OUTPUT_RE.fullmatch(self_test.stdout)
    if self_test.returncode != 0 or self_test.stderr or match is None or match.group(1).decode() != host_tools_sha or match.group(2).decode() != host_tools_sha:
        quarantine_exact(target, quarantine, device=device, inode=inode, files=files, owner=owner)
        stop("installed_self_test_failed_quarantined")
    return {
        "path": str(target), "device": device, "inode": inode,
        "manifest_sha256": sha256(files["manifest.json"]),
        "capabilities_sha256": sha256(files["capabilities.txt"]),
        "self_test": "passed",
    }


def source_release_state(host_tools_sha: str) -> dict[str, Any]:
    if os.geteuid() != 0 or os.environ.get("PLATFORM_RELEASE_LOCK_SUPERVISED") != "1":
        stop("release_lock_context")
    def pointer(label: str) -> tuple[str, tuple[int, int], str]:
        link = APP / label
        info = link.lstat()
        resolved = link.resolve(strict=True)
        if not stat.S_ISLNK(info.st_mode) or resolved.parent != APP / "releases" or resolved.is_symlink():
            stop("release_pointer_mismatch")
        raw = checked_file(resolved / "RELEASE.json", mode=0o444, max_bytes=65536)
        payload = bounded_json(raw, limit=65536, stage="release_receipt_invalid")
        if not isinstance(payload, dict) or payload.get("release_slug") != resolved.name or not isinstance(payload.get("source_git_commit"), str) or not TARGET_RE.fullmatch(payload["source_git_commit"]):
            stop("release_receipt_invalid")
        return str(resolved), (info.st_dev, info.st_ino), sha256(raw)
    current = pointer("current")
    previous = pointer("previous")
    pin_path = Path(current[0]) / "contracts/host_tools_pin.json"
    pin = bounded_json(checked_file(pin_path, mode=0o444, max_bytes=65536), limit=65536, stage="installed_pin_invalid")
    old_sha = pin.get("host_tools_sha") if isinstance(pin, dict) else None
    if not isinstance(old_sha, str) or not TARGET_RE.fullmatch(old_sha) or old_sha == host_tools_sha:
        stop("installed_application_pin_changed")
    old_generation = GENERATIONS / old_sha
    old_info = old_generation.lstat()
    if not stat.S_ISDIR(old_info.st_mode) or stat.S_ISLNK(old_info.st_mode) or (old_info.st_uid, old_info.st_gid, stat.S_IMODE(old_info.st_mode)) != (0, 0, 0o555):
        stop("previous_generation_metadata")
    if old_info.st_nlink != 2:
        stop("previous_generation_link_count")
    for name in (".release-operation.json", ".release-systemd-state.json", ".release-quiesce-state.json"):
        path = SHARED / name
        if path.exists() or path.is_symlink():
            stop("release_transaction_present")
    jobs = safe_run(["/usr/bin/systemctl", "list-jobs", "--no-legend", "--no-pager"])
    if jobs.returncode != 0 or jobs.stdout.strip():
        stop("systemd_jobs_present")
    expected_units = {
        "deadlock-maintenance.timer": {"ActiveState": {"active"}, "UnitFileState": {"enabled"}},
        "deadlock-maintenance.service": {"MainPID": {"0"}, "SubState": {"dead", "failed"}},
        "oldsparky-liveqa-browser.service": {"MainPID": {"0"}, "ActiveState": {"inactive", "failed"}, "SubState": {"dead", "failed"}},
    }
    for unit, expected in expected_units.items():
        status = safe_run(["/usr/bin/systemctl", "show", unit, "--property=ActiveState,SubState,MainPID,UnitFileState"])
        fields = dict(
            line.decode("ascii", "strict").split("=", 1)
            for line in status.stdout.splitlines() if b"=" in line
        )
        if status.returncode != 0 or any(fields.get(key) not in values for key, values in expected.items()):
            stop("systemd_unit_state_changed")
    for url in ("http://127.0.0.1:8010/api/v1/health/ready", "http://127.0.0.1:3000/"):
        health = safe_run([
            "/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "5",
            "--output", "/dev/null", "--write-out", "%{http_code}", url,
        ], output_limit=128)
        if health.returncode != 0 or health.stdout not in {b"200", b"200\n"}:
            stop("local_health_changed")
    space = os.statvfs("/")
    available = space.f_bavail * space.f_frsize
    total = space.f_blocks * space.f_frsize
    required = max(5 * 1024**3, (total + 9) // 10)
    if available < required:
        stop("disk_reserve_changed")
    target = GENERATIONS / host_tools_sha
    if target.exists() or target.is_symlink():
        stop("generation_already_present")
    return {
        "current": current, "previous": previous, "old_sha": old_sha,
        "old_inode": (old_info.st_dev, old_info.st_ino),
        "available_bytes": available, "required_free_bytes": required,
    }


def _load_trusted_source_module(name: str, path: Path, *, source_root: Path, target_sha: str) -> Any:
    try:
        _verify_trusted_source_files(source_root, target_sha)
        if path not in {source_root / "platform/tools/platform_host_tools_pin.py", source_root / "platform/tools/platform_host_tools_bundle.py"}:
            stop("trusted_source_verifier_path")
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            stop("trusted_source_verifier_metadata")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            stop("trusted_source_verifier_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except ProvisionError:
        raise
    except Exception:
        stop("trusted_source_verifier_unavailable")


def active_lock_bootstrap(
    source_root: Path, *, target_sha: str, target_host_tools_sha: str,
    app_root: Path = APP, generation_root: Path = GENERATIONS,
) -> dict[str, Any]:
    """Bind the release lock executable to the active pin and installed generation."""
    if (
        generation_root != app_root / "shared/host-tools"
        or TARGET_RE.fullmatch(target_sha) is None
        or TARGET_RE.fullmatch(target_host_tools_sha) is None
    ):
        stop("active_lock_bootstrap_paths_invalid")
    for directory in (app_root, app_root / "releases", app_root / "shared", generation_root):
        try:
            directory_info = directory.lstat()
        except OSError:
            stop("active_lock_bootstrap_directory_unavailable")
        if (
            stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode)
            or (directory_info.st_uid, directory_info.st_gid) != (0, 0)
            or stat.S_IMODE(directory_info.st_mode) & 0o022
        ):
            stop("active_lock_bootstrap_directory_metadata")
    current = app_root / "current"
    try:
        current_info = current.lstat()
        release_root = current.resolve(strict=True)
        app_info = app_root.lstat()
        release_info = release_root.lstat()
    except OSError:
        stop("active_release_unavailable")
    if (
        not stat.S_ISLNK(current_info.st_mode)
        or release_root.parent != app_root / "releases"
        or stat.S_ISLNK(release_info.st_mode) or not stat.S_ISDIR(release_info.st_mode)
        or (app_info.st_uid, app_info.st_gid) != (0, 0)
        or stat.S_IMODE(app_info.st_mode) & 0o022
        or (release_info.st_uid, release_info.st_gid) != (0, 0)
        or stat.S_IMODE(release_info.st_mode) & 0o022
    ):
        stop("active_release_metadata")
    release_bytes = checked_file(release_root / "RELEASE.json", mode=0o444, max_bytes=65536)
    release = bounded_json(release_bytes, limit=65536, stage="active_release_receipt_invalid")
    if (
        not isinstance(release, dict)
        or release.get("release_slug") != release_root.name
        or not isinstance(release.get("source_git_commit"), str)
        or TARGET_RE.fullmatch(release["source_git_commit"]) is None
        or type(release.get("artifact_format_version")) is not int
        or release.get("artifact_format_version") != 1
    ):
        stop("active_release_receipt_invalid")
    source_relation = safe_run([
        "/usr/bin/git", "-C", str(source_root), "merge-base", "--is-ancestor",
        release["source_git_commit"], target_sha,
    ], output_limit=0)
    if source_relation.returncode != 0 or source_relation.stdout or source_relation.stderr:
        stop("active_release_source_not_in_target_history")
    pin_path = release_root / "contracts/host_tools_pin.json"
    pin_bytes = checked_file(pin_path, mode=0o444, max_bytes=65536)
    pin_payload = bounded_json(pin_bytes, limit=65536, stage="active_pin_invalid")
    active_sha = pin_payload.get("host_tools_sha") if isinstance(pin_payload, dict) else None
    if not isinstance(active_sha, str) or TARGET_RE.fullmatch(active_sha) is None or active_sha == target_host_tools_sha:
        stop("active_pin_target_mismatch")

    pin_module = _load_trusted_source_module(
        "trusted_operator_host_tools_pin", source_root / "platform/tools/platform_host_tools_pin.py",
        source_root=source_root, target_sha=target_sha,
    )
    try:
        validated_pin = pin_module.validate_release_pin(
            release_root, expected_host_tools_sha=active_sha, expected_repository=REPOSITORY,
        )
    except Exception:
        stop("active_release_pin_validation")
    if validated_pin != pin_payload:
        stop("active_release_pin_changed")

    bundle_module = _load_trusted_source_module(
        "trusted_operator_host_tools_bundle", source_root / "platform/tools/platform_host_tools_bundle.py",
        source_root=source_root, target_sha=target_sha,
    )
    generation = generation_root / active_sha
    try:
        installed = bundle_module.verify_installed_generation(
            generation, expected_source_sha=active_sha,
            expected_toolset_version=bundle_module.TOOLSET_VERSION,
        )
    except Exception:
        stop("active_host_generation_validation")
    closure = validated_pin.get("closure")
    if not isinstance(closure, list):
        stop("active_pin_closure_invalid")
    expected_members: dict[str, str] = {}
    for record in closure:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            stop("active_pin_closure_invalid")
        name = Path(record["path"]).name
        digest = record.get("sha256")
        if name in expected_members or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            stop("active_pin_closure_invalid")
        expected_members[name] = digest
    actual_members = {
        path.name for path in generation.iterdir()
        if path.name not in {"capabilities.txt", "manifest.json"}
    }
    if actual_members != set(expected_members):
        stop("active_generation_pin_inventory")
    for name, expected_digest in expected_members.items():
        data = checked_file(generation / name, mode=0o555, max_bytes=512 * 1024)
        if sha256(data) != expected_digest:
            stop("active_generation_pin_digest")
    lock_name = "platform_release_lock.sh"
    lock_digest = expected_members.get(lock_name)
    lock_path = generation / lock_name
    lock_info = lock_path.lstat()
    lock_bytes = checked_file(lock_path, mode=0o555, max_bytes=512 * 1024)
    final_lock_info = lock_path.lstat()
    if (
        lock_digest is None or sha256(lock_bytes) != lock_digest
        or (lock_info.st_uid, lock_info.st_gid, lock_info.st_nlink) != (0, 0, 1)
        or (lock_info.st_dev, lock_info.st_ino, lock_info.st_mtime_ns, lock_info.st_ctime_ns)
        != (final_lock_info.st_dev, final_lock_info.st_ino, final_lock_info.st_mtime_ns, final_lock_info.st_ctime_ns)
    ):
        stop("active_lock_helper_binding")
    generation_info = generation.lstat()
    return {
        "active_release": str(release_root),
        "active_release_source_sha": release["source_git_commit"],
        "current_link_device": current_info.st_dev,
        "current_link_inode": current_info.st_ino,
        "release_receipt_sha256": sha256(release_bytes),
        "active_pin_sha": active_sha,
        "active_pin_sha256": sha256(pin_bytes),
        "generation_path": str(generation),
        "generation_device": generation_info.st_dev,
        "generation_inode": generation_info.st_ino,
        "generation_manifest_sha256": installed["manifest_sha256"],
        "generation_capabilities_sha256": installed["capabilities_sha256"],
        "lock_helper_path": str(lock_path),
        "lock_helper_device": lock_info.st_dev,
        "lock_helper_inode": lock_info.st_ino,
        "lock_helper_sha256": lock_digest,
        "toolset_version": installed["toolset_version"],
    }


def revalidate_active_lock_bootstrap(
    source_root: Path, *, target_sha: str, target_host_tools_sha: str, expected: dict[str, Any],
    app_root: Path = APP, generation_root: Path = GENERATIONS,
) -> None:
    actual = active_lock_bootstrap(
        source_root, target_sha=target_sha, target_host_tools_sha=target_host_tools_sha,
        app_root=app_root, generation_root=generation_root,
    )
    if actual != expected:
        stop("active_lock_bootstrap_changed")


def assert_release_lock_held(bootstrap: dict[str, Any]) -> None:
    script = 'source "$1" && platform_release_lock_supervisor_holds'
    result = safe_run(
        ["/usr/bin/bash", "-c", script, "operator-release-lock-check", bootstrap["lock_helper_path"]],
        env={"PLATFORM_RELEASE_LOCK_SUPERVISED": "1"}, timeout=5, output_limit=0,
    )
    if result.returncode != 0 or result.stdout or result.stderr:
        stop("release_lock_supervisor_not_verified")


def create_receipt(receipt_dir: Path, receipt: dict[str, Any]) -> tuple[bytes, str]:
    parent_info = receipt_dir.parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
        or (parent_info.st_uid, parent_info.st_gid, stat.S_IMODE(parent_info.st_mode)) != (0, 0, 0o700)
    ):
        stop("receipt_parent_metadata")
    os.mkdir(receipt_dir, 0o700)
    directory_identity = receipt_dir.stat(follow_symlinks=False)
    path = receipt_dir / "provisioning-receipt.json"
    encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        fsync_directory(receipt_dir)
        fsync_directory(receipt_dir.parent)
        if checked_file(path, mode=0o600, max_bytes=65536) != encoded:
            stop("receipt_postwrite_verification")
    except Exception:
        try:
            current_dir = receipt_dir.lstat()
            if (current_dir.st_dev, current_dir.st_ino, current_dir.st_uid, current_dir.st_gid) != (
                directory_identity.st_dev, directory_identity.st_ino, 0, 0
            ) or not stat.S_ISDIR(current_dir.st_mode) or stat.S_IMODE(current_dir.st_mode) != 0o700:
                stop("receipt_cleanup_identity_uncertain")
            if path.exists() or path.is_symlink():
                item = path.lstat()
                if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode) or item.st_uid != 0 or item.st_nlink != 1:
                    stop("receipt_cleanup_file_uncertain")
                path.unlink()
            receipt_dir.rmdir()
            fsync_directory(receipt_dir.parent)
        except OSError:
            stop("receipt_cleanup_failed")
        raise
    return encoded, sha256(encoded)


def publish_with_receipt(
    generation_root: Path,
    host_tools_sha: str,
    run_id: str,
    manifest: dict[str, Any],
    files: dict[str, bytes],
    receipt_dir: Path,
    make_receipt: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    rehearsal: bool = False,
    owner: tuple[int, int] = (0, 0),
) -> tuple[dict[str, Any], bytes, str]:
    result = publish(generation_root, host_tools_sha, run_id, manifest, files, rehearsal=rehearsal, owner=owner)
    try:
        encoded, digest = create_receipt(receipt_dir, make_receipt(result))
    except Exception:
        quarantine = generation_root / f".failed-{host_tools_sha}-{run_id}"
        quarantine_exact(
            Path(result["path"]), quarantine, device=result["device"], inode=result["inode"], files=files, owner=owner,
        )
        stop("receipt_failure_quarantined")
    return result, encoded, digest


def provision_locked(args: argparse.Namespace, expected_bootstrap: dict[str, Any]) -> None:
    if os.geteuid() != 0:
        stop("root_required")
    if os.environ.get("PLATFORM_RELEASE_LOCK_SUPERVISED") != "1":
        stop("release_lock_context")
    os.umask(0o077)
    target_sha = args.target_sha
    if TARGET_RE.fullmatch(target_sha) is None:
        stop("target_sha_invalid")
    if args.bundle_artifact_id == args.dispatch_input_artifact_id:
        stop("artifact_ids_must_be_distinct")
    host_tools_sha = validate_source(Path(args.source_root), target_sha=target_sha)
    revalidate_active_lock_bootstrap(
        Path(args.source_root), target_sha=target_sha, target_host_tools_sha=host_tools_sha,
        expected=expected_bootstrap,
    )
    assert_release_lock_held(expected_bootstrap)
    gh = Path(args.gh)
    verify_gh(gh)
    scratch = Path(tempfile.mkdtemp(prefix="oldsparky-host-tools-operator.", dir="/tmp"))
    os.chmod(scratch, 0o700)
    try:
        input_artifact_id = args.dispatch_input_artifact_id
        input_payload, input_metadata, input_digest = read_dispatch_input_artifact(
            gh, artifact_id=input_artifact_id,
            preflight_run_id=args.preflight_run_id, preflight_attempt=args.preflight_run_attempt,
            target_sha=target_sha, classifier_run_id=args.security_run_id,
            classifier_run_attempt=args.security_run_attempt,
            destination=scratch / "dispatch-input.zip",
        )
        security, preflight, job_ids, failed_logs_sha, failed_step_sha = verify_security_and_preflight(
            gh, target_sha=target_sha,
            security_run_id=args.security_run_id, security_attempt=args.security_run_attempt,
            preflight_run_id=args.preflight_run_id, preflight_attempt=args.preflight_run_attempt,
            preflight_mode=input_payload["mode"],
            scratch=scratch,
        )
        artifact_id = args.bundle_artifact_id
        metadata, outer_digest, artifact_name = download_artifact(
            gh, artifact_id=artifact_id, destination=scratch / "bundle-artifact.zip",
        )
        expected_name = f"platform-host-tools-bundle-{args.preflight_run_id}-{args.preflight_run_attempt}"
        bundle_run = metadata.get("workflow_run")
        if (
            artifact_name != expected_name or not isinstance(bundle_run, dict)
            or bundle_run.get("id") != int(args.preflight_run_id)
            or bundle_run.get("head_sha") != target_sha
            or bundle_run.get("head_branch") != "dev"
            or (bundle_run.get("run_attempt") is not None and bundle_run.get("run_attempt") != int(args.preflight_run_attempt))
        ):
            stop("bundle_artifact_binding")
        unpack = scratch / "bundle"
        unpack.mkdir(mode=0o700)
        bundle_path, contracts = unpack_outer_artifact(scratch / "bundle-artifact.zip", unpack)
        bundle_sha = sha256(checked_file(bundle_path, mode=0o600, max_bytes=4 * 1024 * 1024))
        attestation_sha, invocation = verify_attestation(
            gh, bundle_path, target_sha=target_sha,
            preflight_run_id=args.preflight_run_id, preflight_attempt=args.preflight_run_attempt,
            bundle_sha256=bundle_sha, scratch=scratch,
        )
        manifest, member_bytes, toolset, files_contract_sha = validate_bundle_contract(
            bundle_path, contracts, host_tools_sha, Path(args.source_root),
        )
        # The only external mutation before publication is the ADR-required
        # account creation, after all evidence and archive bytes are verified.
        before = source_release_state(host_tools_sha)
        after = source_release_state(host_tools_sha)
        if before != after:
            stop("release_state_changed_before_lock")
        # The caller owns the release lock. The second canonical LiveQA lock is
        # opened from the active installed release and held through receipt.
        guard_path = Path(before["current"][0]) / "tools/platform_live_qa_guard.py"
        spec = importlib.util.spec_from_file_location("trusted_liveqa_operator_guard", guard_path)
        if spec is None or spec.loader is None:
            stop("liveqa_guard_unavailable")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        try:
            qa_fd = guard._open_machine_lock()
            guard.assert_liveqa_idle()
        except Exception:
            stop("liveqa_machine_lock_or_idle_check")
        try:
            locked = source_release_state(host_tools_sha)
            if locked != before:
                stop("locked_release_state_changed")
            # Rebind the authoritative source, pin and producer immediately before writes.
            locked_host_sha = validate_source(Path(args.source_root), target_sha=target_sha)
            if locked_host_sha != host_tools_sha:
                stop("locked_target_pin_changed")
            revalidate_active_lock_bootstrap(
                Path(args.source_root), target_sha=target_sha, target_host_tools_sha=host_tools_sha,
                expected=expected_bootstrap,
            )
            artifact_owner_status, owner_helper_sha = validate_account_contract(before["old_sha"])
            generation_root = GENERATIONS
            gen_info = generation_root.lstat()
            if not stat.S_ISDIR(gen_info.st_mode) or stat.S_ISLNK(gen_info.st_mode) or (gen_info.st_uid, gen_info.st_gid, stat.S_IMODE(gen_info.st_mode)) != (0, 0, 0o755):
                stop("generation_root_metadata")
            stage_files = dict(member_bytes)
            receipt_dir = PRIVATE_ROOT / f"host-tools-provision-{args.preflight_run_id}-{args.preflight_run_attempt}"
            try:
                private_info = PRIVATE_ROOT.lstat()
                if not stat.S_ISDIR(private_info.st_mode) or stat.S_ISLNK(private_info.st_mode) or (private_info.st_uid, private_info.st_gid, stat.S_IMODE(private_info.st_mode)) != (0, 0, 0o700):
                    stop("receipt_parent_metadata")
            except OSError:
                stop("receipt_parent_unavailable")
            if receipt_dir.exists() or receipt_dir.is_symlink():
                stop("receipt_path_already_exists")
            def make_receipt(result: dict[str, Any]) -> dict[str, Any]:
                return {
                    "schema": 1, "status": "passed", "operator_change_id": args.operator_change_id,
                    "target_sha": target_sha, "host_tools_sha": host_tools_sha,
                    "previous_host_tools_sha": before["old_sha"],
                    "security_run_id": args.security_run_id, "security_run_attempt": int(args.security_run_attempt),
                    "preflight_run_id": args.preflight_run_id, "preflight_run_attempt": int(args.preflight_run_attempt),
                    "preflight_failed_job_id": job_ids["failed_job_id"],
                    "preflight_failure_log_sha256": failed_logs_sha,
                    "missing_generation_step_sha256": failed_step_sha,
                    "bundle_artifact_id": artifact_id, "bundle_artifact_name": artifact_name,
                    "outer_artifact_sha256": outer_digest, "inner_bundle_sha256": bundle_sha,
                    "attestation_json_sha256": attestation_sha, "attestation_invocation": invocation,
                    "toolset_version": toolset, "manifest_sha256": result["manifest_sha256"],
                    "capabilities_sha256": result["capabilities_sha256"],
                    "files_contract_sha256": files_contract_sha,
                    "dispatch_input_artifact_id": input_artifact_id,
                    "dispatch_input_artifact_digest": input_metadata.get("digest"),
                    "dispatch_input_archive_sha256": input_digest,
                    "artifact_owner_status": artifact_owner_status,
                    "artifact_owner_helper_sha256": owner_helper_sha,
                    "active_app_source_sha": expected_bootstrap["active_release_source_sha"],
                    "active_pin_sha": expected_bootstrap["active_pin_sha"],
                    "active_pin_sha256": expected_bootstrap["active_pin_sha256"],
                    "active_generation_manifest_sha256": expected_bootstrap["generation_manifest_sha256"],
                    "release_lock_helper_sha256": expected_bootstrap["lock_helper_sha256"],
                    "release_lock_helper_device": expected_bootstrap["lock_helper_device"],
                    "release_lock_helper_inode": expected_bootstrap["lock_helper_inode"],
                    "published_generation": result["path"], "published_device": result["device"],
                    "published_inode": result["inode"], "self_test": result["self_test"],
                    "prior_generation_preserved": True,
                    "current_release_receipt_sha256": before["current"][2],
                    "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            result, receipt_bytes, receipt_digest = publish_with_receipt(
                generation_root, host_tools_sha, args.preflight_run_id,
                manifest, stage_files, receipt_dir, make_receipt,
            )
            print(
                f"HOST_TOOLS_PROVISION status=passed target_sha={target_sha} host_tools_sha={host_tools_sha} "
                f"generation_inode={result['device']}:{result['inode']} self_test=passed "
                f"receipt_sha256={receipt_digest} receipt_path={receipt_dir / 'provisioning-receipt.json'}"
            )
        finally:
            os.close(qa_fd)
    finally:
        shutil.rmtree(scratch)


def provision(args: argparse.Namespace) -> int:
    """Verify the active installed lock helper, then re-exec under its lock."""
    if os.geteuid() != 0:
        stop("root_required")
    os.umask(0o077)
    target_sha = args.target_sha
    if TARGET_RE.fullmatch(target_sha) is None:
        stop("target_sha_invalid")
    if args.bundle_artifact_id == args.dispatch_input_artifact_id:
        stop("artifact_ids_must_be_distinct")
    source_root = Path(args.source_root)
    host_tools_sha = validate_source(source_root, target_sha=target_sha)
    bootstrap = active_lock_bootstrap(
        source_root, target_sha=target_sha, target_host_tools_sha=host_tools_sha,
    )
    gh = Path(args.gh)
    verify_gh(gh)
    command = [
        bootstrap["lock_helper_path"], "--run", "/usr/bin/python3.12", "-I", "-B",
        str(SOURCE_ROOT / "platform/tools/platform_host_tools_provision_operator.py"),
        "_provision_locked",
    ]
    for key, flag in (
        ("target_sha", "--target-sha"), ("source_root", "--source-root"), ("gh", "--gh"),
        ("security_run_id", "--security-run-id"), ("security_run_attempt", "--security-run-attempt"),
        ("preflight_run_id", "--preflight-run-id"), ("preflight_run_attempt", "--preflight-run-attempt"),
        ("bundle_artifact_id", "--bundle-artifact-id"),
        ("dispatch_input_artifact_id", "--dispatch-input-artifact-id"),
        ("operator_change_id", "--operator-change-id"),
    ):
        command.extend((flag, str(getattr(args, key))))
    command.extend(("--_expected-bootstrap", json.dumps(bootstrap, sort_keys=True, separators=(",", ":"))))
    result = safe_run(command, env=gh_auth_environment(), timeout=3600, output_limit=65536)
    if result.stderr:
        stop("locked_operator_stderr")
    try:
        output = result.stdout.decode("ascii", "strict")
    except UnicodeError:
        stop("locked_operator_output_invalid")
    if not output.endswith("\n") or output.count("\n") != 1:
        stop("locked_operator_output_invalid")
    line = output[:-1]
    if result.returncode == 0 and re.fullmatch(
        r"HOST_TOOLS_PROVISION status=passed target_sha=[0-9a-f]{40} host_tools_sha=[0-9a-f]{40} generation_inode=[0-9]+:[0-9]+ self_test=passed receipt_sha256=[0-9a-f]{64} receipt_path=/root/\.oldsparky/host-tools-provision-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}/provisioning-receipt\.json",
        line,
    ):
        print(line)
        return 0
    if result.returncode != 0 and re.fullmatch(
        r"HOST_TOOLS_PROVISION status=blocked stage=[a-z0-9_]{1,96}", line,
    ):
        print(line)
        return result.returncode if 1 <= result.returncode <= 255 else 2
    stop("locked_operator_output_invalid")


def rehearsal(root: Path, *, fail_after_rename: bool = False) -> None:
    """Synthetic fixture entrypoint; production callers cannot supply a path."""
    if os.geteuid() != 0:
        stop("rehearsal_requires_root_for_metadata")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        stop("rehearsal_root_metadata")
    # Kept intentionally private to tests; the CLI exposes no destination arg.
    stop("rehearsal_fixture_only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("provision")
    locked = sub.add_parser("_provision_locked", help=argparse.SUPPRESS)
    for command_parser in (run, locked):
        command_parser.add_argument("--target-sha", required=True)
        command_parser.add_argument("--source-root", default=str(SOURCE_ROOT))
        command_parser.add_argument("--gh", required=True, help="official isolated gh v2.105.0 binary prepared per owner ADR")
        command_parser.add_argument("--security-run-id", required=True)
        command_parser.add_argument("--security-run-attempt", required=True)
        command_parser.add_argument("--preflight-run-id", required=True)
        command_parser.add_argument("--preflight-run-attempt", required=True)
        command_parser.add_argument("--bundle-artifact-id", required=True)
        command_parser.add_argument("--dispatch-input-artifact-id", required=True)
        command_parser.add_argument("--operator-change-id", required=True)
    locked.add_argument("--_expected-bootstrap", required=True, help=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
        for value in (args.security_run_id, args.security_run_attempt, args.preflight_run_id, args.preflight_run_attempt, args.bundle_artifact_id, args.dispatch_input_artifact_id):
            if ID_RE.fullmatch(value) is None:
                stop("workflow_input_id_invalid")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", args.operator_change_id):
            stop("operator_change_id_invalid")
        if args.command == "provision":
            return provision(args)
        if args.command == "_provision_locked":
            expected_bootstrap = bounded_json(
                args._expected_bootstrap.encode("ascii"), limit=16384,
                stage="lock_bootstrap_input_invalid",
            )
            if not isinstance(expected_bootstrap, dict):
                stop("lock_bootstrap_input_invalid")
            provision_locked(args, expected_bootstrap)
            return 0
        stop("unsupported_command")
    except ProvisionError as exc:
        print(f"HOST_TOOLS_PROVISION status=blocked stage={exc.stage}")
        return 2
    except Exception:
        print("HOST_TOOLS_PROVISION status=blocked stage=unexpected_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
