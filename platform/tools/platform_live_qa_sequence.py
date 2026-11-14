#!/usr/bin/env python3
"""Dispatch production browser QA only after an authenticated exact deploy."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from tools.platform_workflow_provenance import (  # noqa: E402
    DEPLOY_WORKFLOW_NAME,
    _expected_deployment_run_name,
    validate_deployment_event,
    validate_workflow_run,
)


API_VERSION = "2026-03-10"
API_LIMIT = 1024 * 1024
ARCHIVE_LIMIT = 2 * 1024 * 1024
RECEIPT_LIMIT = 20 * 1024
REPO = "StrayForest/old_sparky"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
RUN_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
RELEASE_RE = re.compile(r"gha-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[0-9a-f]{12}\Z")
API_HOST_RE = re.compile(r"[A-Za-z0-9.-]+\Z")
API_VERSION_HEADER = "X-GitHub-Api-Version"
CHILD_PENDING = {"queued", "requested", "waiting", "pending", "in_progress"}
CHILD_TIMEOUT_SECONDS = 55 * 60
POLL_INTERVAL_SECONDS = 15
DEPLOY_WORKFLOW_PATH = ".github/workflows/platform-production-deploy.yml"
SEQUENCER_WORKFLOW_PATH = ".github/workflows/platform-postdeploy-live-qa.yml"
PUBLIC_WORKFLOW_PATH = ".github/workflows/platform-live-launch.yml"
USER_QA_WORKFLOW_PATH = ".github/workflows/platform-live-user-qa.yml"
DEPLOY_RECEIPT_KIND = "platform-actual-deployment-mode"
DEPLOY_RECEIPT_NAME_PREFIX = "platform-actual-deployment-"
DEPLOY_RECEIPT_FILE = "actual-deployment-receipt.json"
HANDOFF_AUTH_KIND = "platform-postdeploy-live-qa-authorization"
HANDOFF_AUTH_NAME_PREFIX = "platform-postdeploy-live-qa-authorization-"
HANDOFF_AUTH_FILE = "dispatch-authorization.json"
CHILD_RECEIPT_PREFIX = "platform-postdeploy-live-qa-child-"
CHILD_RECEIPT_FILE = "child-dispatch-receipt.json"
SEQUENCE_JOB_NAME = "Validate deployment and run ordered live QA"


class SequenceError(ValueError):
    """A fixed, non-sensitive refusal reason."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


class SequenceSkipped(Exception):
    """A successfully authenticated deploy that does not require live QA."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unique_object_pairs(rows: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise SequenceError("api", "response_invalid") from None


def _positive_id(value: object, stage: str = "dispatch") -> int:
    if type(value) is not int or value <= 0 or value > 10**32 - 1:
        raise SequenceError(stage, "run_id_invalid")
    return value


def _environment_id(name: str) -> int:
    value = os.environ.get(name, "")
    if RUN_RE.fullmatch(value) is None:
        raise SequenceError("child_receipt", "run_id_invalid")
    return int(value)


def _api_root(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SequenceError("parent", "api_origin_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or API_HOST_RE.fullmatch(parsed.hostname or "") is None
        or port not in {None, 443}
    ):
        raise SequenceError("parent", "api_origin_invalid")
    return value.rstrip("/")


def _run_name(parent_id: str, parent_attempt: str, target_sha: str) -> str:
    return f"Automatic post-deploy QA parent={parent_id}.{parent_attempt} source={target_sha}"


def _marker(parent_id: str, parent_attempt: str, target_sha: str) -> str:
    material = f"{REPO}/{parent_id}/{parent_attempt}/{target_sha}"
    suffix = hashlib.sha256(material.encode("ascii")).hexdigest()[:24]
    return f"liveqa-auto-{suffix}"


def parse_dispatch_ack(
    document: object, *, repository: str = REPO, api_host: str = "api.github.com"
) -> int:
    """Return the exact ID from GitHub's documented dispatch response."""

    if not isinstance(document, Mapping) or set(document) != {
        "workflow_run_id", "run_url", "html_url"
    }:
        raise SequenceError("dispatch", "ack_schema_invalid")
    run_id = _positive_id(document.get("workflow_run_id"))
    run_url = document.get("run_url")
    html_url = document.get("html_url")
    if not isinstance(run_url, str) or not isinstance(html_url, str):
        raise SequenceError("dispatch", "ack_url_invalid")
    try:
        run_parts = urllib.parse.urlsplit(run_url)
        html_parts = urllib.parse.urlsplit(html_url)
    except ValueError:
        raise SequenceError("dispatch", "ack_url_invalid") from None
    if (
        run_parts.scheme != "https"
        or run_parts.hostname != api_host
        or html_parts.scheme != "https"
        or html_parts.hostname != "github.com"
        or run_parts.path != f"/repos/{repository}/actions/runs/{run_id}"
        or html_parts.path != f"/{repository}/actions/runs/{run_id}"
        or run_parts.query
        or run_parts.fragment
        or html_parts.query
        or html_parts.fragment
        or run_parts.username is not None
        or html_parts.username is not None
    ):
        raise SequenceError("dispatch", "ack_url_invalid")
    return run_id


def validate_workflow(document: object, expected_path: str) -> int:
    if not isinstance(document, Mapping):
        raise SequenceError("dispatch", "workflow_identity_invalid")
    workflow_id = document.get("id")
    if (
        document.get("path") != expected_path
        or type(workflow_id) is not int
        or workflow_id <= 0
        or document.get("state") != "active"
    ):
        raise SequenceError("dispatch", "workflow_identity_invalid")
    return workflow_id


def validate_run_identity(
    document: object,
    *,
    run_id: int,
    workflow_id: int,
    target_sha: str,
) -> tuple[str, str | None]:
    if not isinstance(document, Mapping):
        raise SequenceError("child", "run_identity_invalid")
    attempt = document.get("run_attempt")
    if (
        document.get("id") != run_id
        or type(document.get("id")) is not int
        or document.get("workflow_id") != workflow_id
        or type(document.get("workflow_id")) is not int
        or attempt != 1
        or type(attempt) is not int
        or document.get("head_sha") != target_sha
        or document.get("head_branch") != "dev"
        or document.get("event") != "workflow_dispatch"
    ):
        raise SequenceError("child", "run_identity_invalid")
    status = document.get("status")
    conclusion = document.get("conclusion")
    if status in CHILD_PENDING and conclusion is None:
        return status, None
    if status != "completed" or conclusion != "success":
        raise SequenceError("child", "run_not_successful")
    return status, conclusion


def validate_required_jobs(document: object, expected_names: tuple[str, ...]) -> None:
    if not isinstance(document, Mapping):
        raise SequenceError("child", "jobs_response_invalid")
    jobs = document.get("jobs")
    total = document.get("total_count")
    if not isinstance(jobs, list) or type(total) is not int or total != len(jobs):
        raise SequenceError("child", "jobs_response_invalid")
    by_name: dict[str, Mapping[str, object]] = {}
    for job in jobs:
        if not isinstance(job, Mapping) or not isinstance(job.get("name"), str):
            raise SequenceError("child", "jobs_response_invalid")
        name = job["name"]
        if name in by_name:
            raise SequenceError("child", "jobs_ambiguous")
        by_name[name] = job
    for name in expected_names:
        row = by_name.get(name)
        if (
            row is None
            or row.get("status") != "completed"
            or row.get("conclusion") != "success"
        ):
            raise SequenceError("child", "required_job_not_successful")


def validate_actual_deploy_receipt(
    document: object,
    *,
    repository: str,
    run_id: int,
    attempt: int,
    source_sha: str,
) -> str:
    keys = {
        "schema", "kind", "repository", "workflow_path", "run_id",
        "run_attempt", "runner_sha", "mode", "source_sha", "release_slug",
    }
    if not isinstance(document, Mapping) or set(document) != keys:
        raise SequenceError("receipt", "receipt_schema_invalid")
    slug = document.get("release_slug")
    if (
        type(document.get("schema")) is not int
        or document.get("schema") != 1
        or document.get("kind") != DEPLOY_RECEIPT_KIND
        or document.get("repository") != repository
        or document.get("workflow_path") != DEPLOY_WORKFLOW_PATH
        or document.get("run_id") != str(run_id)
        or document.get("run_attempt") != str(attempt)
        or document.get("runner_sha") != source_sha
        or document.get("source_sha") != source_sha
        or SHA_RE.fullmatch(source_sha) is None
        or document.get("mode") not in {"deploy", "recovery-deploy", "baseline-reconcile"}
        or not isinstance(slug, str)
        or RELEASE_RE.fullmatch(slug) is None
        or not slug.endswith(f"-{source_sha[:12]}")
    ):
        raise SequenceError("receipt", "receipt_identity_invalid")
    return str(document["mode"])


def handoff_authorization_name(parent_id: int, attempt: int) -> str:
    return f"{HANDOFF_AUTH_NAME_PREFIX}{parent_id}-{attempt}"


def validate_handoff_authorization(
    document: object,
    *,
    repository: str,
    parent_run_id: int,
    parent_attempt: int,
    target_sha: str,
    release_slug: str,
    sequence_run_id: int | None,
    sequence_attempt: int | None,
    sequence_workflow_id: int,
) -> dict[str, object]:
    keys = {
        "schema", "kind", "repository", "deploy_workflow_path",
        "deploy_run_id", "deploy_run_attempt", "source_sha", "mode",
        "release_slug", "sequence_workflow_path", "sequence_workflow_id",
        "sequence_run_id", "sequence_run_attempt",
    }
    if not isinstance(document, Mapping) or set(document) != keys:
        raise SequenceError("authorization", "receipt_schema_invalid")
    if (
        type(document.get("schema")) is not int
        or document.get("schema") != 1
        or document.get("kind") != HANDOFF_AUTH_KIND
        or document.get("repository") != repository
        or document.get("deploy_workflow_path") != DEPLOY_WORKFLOW_PATH
        or document.get("deploy_run_id") != str(parent_run_id)
        or document.get("deploy_run_attempt") != str(parent_attempt)
        or document.get("source_sha") != target_sha
        or SHA_RE.fullmatch(target_sha) is None
        or document.get("mode") != "deploy"
        or document.get("release_slug") != release_slug
        or RELEASE_RE.fullmatch(release_slug) is None
        or not release_slug.endswith(f"-{target_sha[:12]}")
        or document.get("sequence_workflow_path") != SEQUENCER_WORKFLOW_PATH
        or type(document.get("sequence_workflow_id")) is not int
        or document.get("sequence_workflow_id") != sequence_workflow_id
        or not isinstance(document.get("sequence_run_id"), str)
        or RUN_RE.fullmatch(document["sequence_run_id"]) is None
        or not isinstance(document.get("sequence_run_attempt"), str)
        or document.get("sequence_run_attempt") != "1"
        or (
            sequence_run_id is not None
            and document.get("sequence_run_id") != str(sequence_run_id)
        )
        or (sequence_attempt is not None and sequence_attempt != 1)
    ):
        raise SequenceError("authorization", "receipt_identity_invalid")
    return dict(document)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class _SafeArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(new_url)
        if (
            new.scheme != "https"
            or not new.netloc
            or new.username is not None
            or new.password is not None
        ):
            return None
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is not None and old.netloc.casefold() != new.netloc.casefold():
            sensitive = {"authorization", "proxy-authorization", "cookie", "cookie2"}
            for collection in (redirected.headers, redirected.unredirected_hdrs):
                for key in tuple(collection):
                    if key.casefold() in sensitive:
                        collection.pop(key, None)
        return redirected


class GitHubActions:
    def __init__(self, api_root: str, repository: str, token: str) -> None:
        self.api_root = _api_root(api_root)
        if REPO_RE.fullmatch(repository) is None or not token:
            raise SequenceError("parent", "api_credentials_invalid")
        self.repository = repository
        self.token = token
        self.prefix = f"{self.api_root}/repos/{repository}"
        self.api_host = urllib.parse.urlsplit(self.api_root).hostname or ""
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.artifact_opener = urllib.request.build_opener(_SafeArtifactRedirect())

    def request(self, method: str, path: str, payload: object | None = None) -> object:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        request = urllib.request.Request(
            self.prefix + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                API_VERSION_HEADER: API_VERSION,
                "User-Agent": "OldSparky-automatic-live-qa",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != request.full_url:
                    raise SequenceError("api", "response_origin_invalid")
                raw = response.read(API_LIMIT + 1)
        except SequenceError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
            raise SequenceError("api", "request_failed") from None
        if len(raw) > API_LIMIT:
            raise SequenceError("api", "response_too_large")
        return _strict_json(raw)

    def artifact_bytes(self, path: str) -> bytes:
        request = urllib.request.Request(
            self.prefix + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                API_VERSION_HEADER: API_VERSION,
                "User-Agent": "OldSparky-automatic-live-qa",
            },
        )
        try:
            with self.artifact_opener.open(request, timeout=30) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if response.status != 200 or final.scheme != "https" or final.username or final.password:
                    raise SequenceError("receipt", "artifact_origin_invalid")
                data = response.read(ARCHIVE_LIMIT + 1)
        except SequenceError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
            raise SequenceError("receipt", "artifact_download_failed") from None
        if len(data) > ARCHIVE_LIMIT:
            raise SequenceError("receipt", "artifact_too_large")
        return data

    def current_dev_sha(self) -> str:
        branch = self.request("GET", "/branches/dev")
        try:
            sha = branch["commit"]["sha"]  # type: ignore[index]
        except (KeyError, TypeError):
            raise SequenceError("parent", "dev_head_unavailable") from None
        if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
            raise SequenceError("parent", "dev_head_invalid")
        return sha

    def workflow_id(self, path: str) -> int:
        filename = path.rsplit("/", 1)[1]
        workflow = self.request("GET", f"/actions/workflows/{filename}")
        return validate_workflow(workflow, path)

    def dispatch(self, path: str, workflow_id: int, inputs: dict[str, str]) -> int:
        filename = path.rsplit("/", 1)[1]
        ack = self.request(
            "POST",
            f"/actions/workflows/{filename}/dispatches",
            {"ref": "dev", "inputs": inputs},
        )
        return parse_dispatch_ack(
            ack, repository=self.repository, api_host=self.api_host
        )

    def wait_for_run(
        self,
        *,
        run_id: int,
        workflow_id: int,
        target_sha: str,
        expected_jobs: tuple[str, ...],
    ) -> None:
        deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
        while True:
            run = self.request("GET", f"/actions/runs/{run_id}")
            status, conclusion = validate_run_identity(
                run, run_id=run_id, workflow_id=workflow_id, target_sha=target_sha
            )
            if status == "completed" and conclusion == "success":
                break
            if time.monotonic() >= deadline:
                raise SequenceError("child", "run_timeout")
            time.sleep(POLL_INTERVAL_SECONDS)
        jobs = self.request(
            "GET", f"/actions/runs/{run_id}/jobs?per_page=100&page=1"
        )
        validate_required_jobs(jobs, expected_jobs)

    def exact_deployment_receipt(
        self, *, parent_id: int, attempt: int, target_sha: str
    ) -> dict[str, object]:
        expected_name = f"{DEPLOY_RECEIPT_NAME_PREFIX}{parent_id}-{attempt}"
        payload = self.request(
            "GET", f"/actions/runs/{parent_id}/artifacts?per_page=100&page=1"
        )
        if not isinstance(payload, Mapping):
            raise SequenceError("receipt", "artifact_list_invalid")
        rows = payload.get("artifacts")
        total = payload.get("total_count")
        if not isinstance(rows, list) or type(total) is not int or total != len(rows) or total >= 100:
            raise SequenceError("receipt", "artifact_list_invalid")
        if any(
            not isinstance(row, Mapping)
            or not isinstance(row.get("name"), str)
            or type(row.get("id")) is not int
            or row.get("id") <= 0
            for row in rows
        ):
            raise SequenceError("receipt", "artifact_list_invalid")
        matches = [row for row in rows if row.get("name") == expected_name]
        if len(matches) != 1:
            raise SequenceError("receipt", "artifact_missing_or_ambiguous")
        row = matches[0]
        artifact_id = _positive_id(row.get("id"), "receipt")
        metadata = self.request("GET", f"/actions/artifacts/{artifact_id}")
        if not isinstance(metadata, Mapping):
            raise SequenceError("receipt", "artifact_metadata_invalid")
        size = metadata.get("size_in_bytes")
        digest = metadata.get("digest")
        workflow_run = metadata.get("workflow_run")
        if (
            metadata.get("id") != artifact_id
            or metadata.get("name") != expected_name
            or metadata.get("expired") is not False
            or type(size) is not int
            or not 0 < size <= ARCHIVE_LIMIT
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or not isinstance(workflow_run, Mapping)
            or workflow_run.get("id") != parent_id
            or workflow_run.get("head_sha") != target_sha
            or (
                workflow_run.get("run_attempt") is not None
                and (type(workflow_run.get("run_attempt")) is not int or workflow_run.get("run_attempt") != attempt)
            )
            or row.get("expired") is not False
            or row.get("id") != artifact_id
            or row.get("size_in_bytes") != size
            or row.get("digest") != digest
        ):
            raise SequenceError("receipt", "artifact_metadata_invalid")
        archive_bytes = self.artifact_bytes(f"/actions/artifacts/{artifact_id}/zip")
        if len(archive_bytes) != size or hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:"):
            raise SequenceError("receipt", "artifact_digest_invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                infos = archive.infolist()
                if len(infos) != 1 or infos[0].filename != DEPLOY_RECEIPT_FILE:
                    raise SequenceError("receipt", "artifact_member_invalid")
                info = infos[0]
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or mode & 0o170000 == 0o120000 or not 1 <= info.file_size <= RECEIPT_LIMIT:
                    raise SequenceError("receipt", "artifact_member_invalid")
                with archive.open(info) as member:
                    raw = member.read(RECEIPT_LIMIT + 1)
        except SequenceError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise SequenceError("receipt", "artifact_archive_invalid") from None
        if len(raw) != info.file_size or len(raw) > RECEIPT_LIMIT or not raw.endswith(b"\n"):
            raise SequenceError("receipt", "receipt_size_invalid")
        document = _strict_json(raw[:-1])
        if not isinstance(document, Mapping) or json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n" != raw:
            raise SequenceError("receipt", "receipt_encoding_invalid")
        mode = validate_actual_deploy_receipt(
            document,
            repository=self.repository,
            run_id=parent_id,
            attempt=attempt,
            source_sha=target_sha,
        )
        if not isinstance(document, dict):
            raise SequenceError("receipt", "receipt_schema_invalid")
        return {**document, "verified_mode": mode}

    def verify_parent_handoff_ready(
        self, *, parent_id: int, attempt: int, target_sha: str
    ) -> tuple[int, str]:
        workflow = self.request("GET", "/actions/workflows/platform-production-deploy.yml")
        workflow_id = validate_workflow(workflow, DEPLOY_WORKFLOW_PATH)
        run = self.request("GET", f"/actions/runs/{parent_id}")
        if not isinstance(run, Mapping):
            raise SequenceError("handoff", "parent_run_invalid")
        try:
            run_name = _expected_deployment_run_name(run, target_sha)
        except (TypeError, ValueError, KeyError):
            raise SequenceError("handoff", "parent_run_invalid") from None
        if (
            run.get("id") != parent_id
            or type(run.get("id")) is not int
            or run.get("run_attempt") != attempt
            or type(run.get("run_attempt")) is not int
            or run.get("workflow_id") != workflow_id
            or type(run.get("workflow_id")) is not int
            or run.get("path") != DEPLOY_WORKFLOW_PATH
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "dev"
            or run.get("head_sha") != target_sha
            or run.get("name") != DEPLOY_WORKFLOW_NAME
            or run_name != DEPLOY_WORKFLOW_NAME
            or run.get("status") != "in_progress"
            or run.get("conclusion") is not None
        ):
            raise SequenceError("handoff", "parent_run_identity_invalid")
        payload = self.request(
            "GET", f"/actions/runs/{parent_id}/attempts/{attempt}/jobs?per_page=100&page=1"
        )
        if (
            not isinstance(payload, Mapping)
            or not isinstance(payload.get("jobs"), list)
            or type(payload.get("total_count")) is not int
            or payload.get("total_count") != len(payload["jobs"])
            or len(payload["jobs"]) >= 100
        ):
            raise SequenceError("handoff", "parent_jobs_invalid")
        if any(
            not isinstance(job, Mapping) or not isinstance(job.get("name"), str)
            for job in payload["jobs"]
        ):
            raise SequenceError("handoff", "parent_jobs_invalid")
        deploy_jobs = [
            job for job in payload["jobs"] if job.get("name") == "Deploy production"
        ]
        if (
            len(deploy_jobs) != 1
            or deploy_jobs[0].get("status") != "completed"
            or deploy_jobs[0].get("conclusion") != "success"
        ):
            raise SequenceError("handoff", "production_job_not_successful")
        receipt = self.exact_deployment_receipt(
            parent_id=parent_id, attempt=attempt, target_sha=target_sha
        )
        if receipt.get("verified_mode") != "deploy":
            raise SequenceSkipped("mode_not_normal_deploy")
        if self.current_dev_sha() != target_sha:
            raise SequenceError("handoff", "dev_head_mismatch")
        return workflow_id, str(receipt["release_slug"])

    def exact_handoff_authorization(
        self,
        *,
        parent_id: int,
        attempt: int,
        target_sha: str,
        release_slug: str,
        sequence_run_id: int | None,
        sequence_attempt: int | None,
        sequence_workflow_id: int,
        timeout_seconds: int = 300,
    ) -> dict[str, object]:
        expected_name = handoff_authorization_name(parent_id, attempt)
        deadline = time.monotonic() + timeout_seconds
        while True:
            listing = self.request(
                "GET", f"/actions/runs/{parent_id}/artifacts?per_page=100&page=1"
            )
            if not isinstance(listing, Mapping):
                raise SequenceError("authorization", "artifact_list_invalid")
            rows = listing.get("artifacts")
            total = listing.get("total_count")
            if (
                not isinstance(rows, list)
                or type(total) is not int
                or total != len(rows)
                or total >= 100
                or any(
                    not isinstance(row, Mapping)
                    or not isinstance(row.get("name"), str)
                    or type(row.get("id")) is not int
                    or row.get("id") <= 0
                    for row in rows
                )
            ):
                raise SequenceError("authorization", "artifact_list_invalid")
            matches = [row for row in rows if row.get("name") == expected_name]
            if matches:
                break
            if time.monotonic() >= deadline:
                raise SequenceError("authorization", "artifact_unavailable")
            time.sleep(POLL_INTERVAL_SECONDS)
        if len(matches) != 1:
            raise SequenceError("authorization", "artifact_ambiguous")
        row = matches[0]
        artifact_id = _positive_id(row.get("id"), "authorization")
        metadata = self.request("GET", f"/actions/artifacts/{artifact_id}")
        if not isinstance(metadata, Mapping):
            raise SequenceError("authorization", "artifact_metadata_invalid")
        size = metadata.get("size_in_bytes")
        digest = metadata.get("digest")
        parent_run = metadata.get("workflow_run")
        if (
            metadata.get("id") != artifact_id
            or metadata.get("name") != expected_name
            or metadata.get("expired") is not False
            or type(size) is not int
            or not 1 <= size <= ARCHIVE_LIMIT
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or not isinstance(parent_run, Mapping)
            or parent_run.get("id") != parent_id
            or parent_run.get("head_sha") != target_sha
            or (
                parent_run.get("run_attempt") is not None
                and (
                    type(parent_run.get("run_attempt")) is not int
                    or parent_run.get("run_attempt") != attempt
                )
            )
            or row.get("id") != artifact_id
            or row.get("expired") is not False
            or row.get("size_in_bytes") != size
            or row.get("digest") != digest
        ):
            raise SequenceError("authorization", "artifact_metadata_invalid")
        archive_bytes = self.artifact_bytes(f"/actions/artifacts/{artifact_id}/zip")
        if (
            len(archive_bytes) != size
            or hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:")
        ):
            raise SequenceError("authorization", "artifact_digest_invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                infos = archive.infolist()
                if len(infos) != 1 or infos[0].filename != HANDOFF_AUTH_FILE:
                    raise SequenceError("authorization", "artifact_member_invalid")
                info = infos[0]
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or mode & 0o170000 == 0o120000
                    or not 1 <= info.file_size <= RECEIPT_LIMIT
                ):
                    raise SequenceError("authorization", "artifact_member_invalid")
                with archive.open(info) as member:
                    raw = member.read(RECEIPT_LIMIT + 1)
        except SequenceError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise SequenceError("authorization", "artifact_archive_invalid") from None
        if len(raw) != info.file_size or len(raw) > RECEIPT_LIMIT or not raw.endswith(b"\n"):
            raise SequenceError("authorization", "receipt_size_invalid")
        document = _strict_json(raw[:-1])
        if not isinstance(document, Mapping) or json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n" != raw:
            raise SequenceError("authorization", "receipt_encoding_invalid")
        return validate_handoff_authorization(
            document,
            repository=self.repository,
            parent_run_id=parent_id,
            parent_attempt=attempt,
            target_sha=target_sha,
            release_slug=release_slug,
            sequence_run_id=sequence_run_id,
            sequence_attempt=sequence_attempt,
            sequence_workflow_id=sequence_workflow_id,
        )

    def wait_for_parent_success(
        self, *, parent_id: int, attempt: int, target_sha: str, timeout_seconds: int = 300
    ) -> None:
        workflow = self.request("GET", "/actions/workflows/platform-production-deploy.yml")
        workflow_id = validate_workflow(workflow, DEPLOY_WORKFLOW_PATH)
        deadline = time.monotonic() + timeout_seconds
        while True:
            run = self.request("GET", f"/actions/runs/{parent_id}")
            if not isinstance(run, Mapping):
                raise SequenceError("parent", "run_identity_invalid")
            try:
                run_name = _expected_deployment_run_name(run, target_sha)
            except (TypeError, ValueError, KeyError):
                raise SequenceError("parent", "run_identity_invalid") from None
            if (
                run.get("id") != parent_id
                or type(run.get("id")) is not int
                or run.get("run_attempt") != attempt
                or type(run.get("run_attempt")) is not int
                or run.get("workflow_id") != workflow_id
                or type(run.get("workflow_id")) is not int
                or run.get("path") != DEPLOY_WORKFLOW_PATH
                or run.get("event") != "workflow_dispatch"
                or run.get("head_branch") != "dev"
                or run.get("head_sha") != target_sha
                or run.get("name") != DEPLOY_WORKFLOW_NAME
                or run_name != DEPLOY_WORKFLOW_NAME
            ):
                raise SequenceError("parent", "run_identity_invalid")
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status == "completed":
                if conclusion != "success":
                    raise SequenceError("parent", "run_not_successful")
                return
            if status not in CHILD_PENDING or conclusion is not None:
                raise SequenceError("parent", "run_state_invalid")
            if time.monotonic() >= deadline:
                raise SequenceError("parent", "run_timeout")
            time.sleep(POLL_INTERVAL_SECONDS)

    def exact_child_receipt(
        self,
        *,
        sequence_run_id: int,
        sequence_attempt: int,
        role: str,
        target_sha: str,
        timeout_seconds: int = 300,
    ) -> dict[str, object]:
        if role not in {"public", "user-qa"}:
            raise SequenceError("child_receipt", "role_invalid")
        expected_name = f"{CHILD_RECEIPT_PREFIX}{role}-{sequence_run_id}-{sequence_attempt}"
        deadline = time.monotonic() + timeout_seconds
        while True:
            listing = self.request(
                "GET", f"/actions/runs/{sequence_run_id}/artifacts?per_page=100&page=1"
            )
            if not isinstance(listing, Mapping):
                raise SequenceError("child_receipt", "artifact_list_invalid")
            rows = listing.get("artifacts")
            total = listing.get("total_count")
            if (
                not isinstance(rows, list)
                or type(total) is not int
                or total != len(rows)
                or total >= 100
                or any(
                    not isinstance(row, Mapping)
                    or not isinstance(row.get("name"), str)
                    or type(row.get("id")) is not int
                    or row.get("id") <= 0
                    for row in rows
                )
            ):
                raise SequenceError("child_receipt", "artifact_list_invalid")
            matches = [row for row in rows if row.get("name") == expected_name]
            if matches:
                break
            if time.monotonic() >= deadline:
                raise SequenceError("child_receipt", "artifact_unavailable")
            time.sleep(POLL_INTERVAL_SECONDS)
        if len(matches) != 1:
            raise SequenceError("child_receipt", "artifact_ambiguous")
        row = matches[0]
        artifact_id = int(row["id"])
        metadata = self.request("GET", f"/actions/artifacts/{artifact_id}")
        if not isinstance(metadata, Mapping):
            raise SequenceError("child_receipt", "artifact_metadata_invalid")
        size = metadata.get("size_in_bytes")
        digest = metadata.get("digest")
        run = metadata.get("workflow_run")
        if (
            metadata.get("id") != artifact_id
            or metadata.get("name") != expected_name
            or metadata.get("expired") is not False
            or type(size) is not int
            or not 1 <= size <= ARCHIVE_LIMIT
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or not isinstance(run, Mapping)
            or run.get("id") != sequence_run_id
            or run.get("head_branch") != "dev"
            or (
                run.get("run_attempt") is not None
                and (type(run.get("run_attempt")) is not int or run.get("run_attempt") != sequence_attempt)
            )
            or run.get("head_sha") != target_sha
            or row.get("id") != artifact_id
            or row.get("expired") is not False
            or row.get("size_in_bytes") != size
            or row.get("digest") != digest
        ):
            raise SequenceError("child_receipt", "artifact_metadata_invalid")
        archive_bytes = self.artifact_bytes(f"/actions/artifacts/{artifact_id}/zip")
        if (
            len(archive_bytes) != size
            or hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:")
        ):
            raise SequenceError("child_receipt", "artifact_digest_invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                infos = archive.infolist()
                if len(infos) != 1 or infos[0].filename != CHILD_RECEIPT_FILE:
                    raise SequenceError("child_receipt", "artifact_member_invalid")
                info = infos[0]
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or mode & 0o170000 == 0o120000
                    or not 1 <= info.file_size <= RECEIPT_LIMIT
                ):
                    raise SequenceError("child_receipt", "artifact_member_invalid")
                with archive.open(info) as member:
                    raw = member.read(RECEIPT_LIMIT + 1)
        except SequenceError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise SequenceError("child_receipt", "artifact_archive_invalid") from None
        if len(raw) != info.file_size or len(raw) > RECEIPT_LIMIT or not raw.endswith(b"\n"):
            raise SequenceError("child_receipt", "receipt_size_invalid")
        document = _strict_json(raw[:-1])
        if (
            not isinstance(document, dict)
            or json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n" != raw
        ):
            raise SequenceError("child_receipt", "receipt_encoding_invalid")
        return document

    def verify_parent_deploy(
        self, *, parent_id: int, attempt: int, target_sha: str
    ) -> tuple[str, int, str]:
        workflow = self.request("GET", "/actions/workflows/platform-production-deploy.yml")
        workflow_id = validate_workflow(workflow, DEPLOY_WORKFLOW_PATH)
        run = self.request("GET", f"/actions/runs/{parent_id}")
        if not isinstance(run, Mapping):
            raise SequenceError("parent", "run_identity_invalid")
        try:
            expected_name = _expected_deployment_run_name(run, target_sha)
            if expected_name != DEPLOY_WORKFLOW_NAME:
                validate_workflow_run(
                    workflow,
                    run,
                    expected_run_id=parent_id,
                    expected_attempt=attempt,
                    expected_target_sha=target_sha,
                    expected_event="workflow_dispatch",
                    expected_branch="dev",
                    expected_path=DEPLOY_WORKFLOW_PATH,
                    expected_name=DEPLOY_WORKFLOW_NAME,
                    expected_run_name=expected_name,
                )
                raise SequenceSkipped("baseline_reconcile")
        except SequenceSkipped:
            raise
        except (TypeError, ValueError, KeyError):
            raise SequenceError("parent", "run_identity_invalid") from None
        jobs_payload = self.request(
            "GET", f"/actions/runs/{parent_id}/attempts/{attempt}/jobs?per_page=100&page=1"
        )
        if not isinstance(jobs_payload, Mapping):
            raise SequenceError("parent", "jobs_response_invalid")
        jobs = jobs_payload.get("jobs")
        if (
            not isinstance(jobs, list)
            or type(jobs_payload.get("total_count")) is not int
            or jobs_payload["total_count"] != len(jobs)
            or len(jobs) >= 100
        ):
            raise SequenceError("parent", "jobs_response_invalid")
        statuses = self.request(
            "GET", f"/commits/{target_sha}/statuses?per_page=100&page=1"
        )
        if not isinstance(statuses, list) or len(statuses) >= 100:
            raise SequenceError("parent", "status_list_invalid")
        try:
            actual_deploy = validate_deployment_event(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=parent_id,
                expected_attempt=attempt,
                expected_target_sha=target_sha,
            )
        except (TypeError, ValueError, KeyError):
            raise SequenceError("parent", "deployment_provenance_invalid") from None
        if not actual_deploy:
            raise SequenceSkipped("no_actual_deployment")
        receipt = self.exact_deployment_receipt(
            parent_id=parent_id, attempt=attempt, target_sha=target_sha
        )
        return str(receipt["verified_mode"]), workflow_id, str(receipt["release_slug"])

    def verify_sequence_first_delivery(
        self,
        *,
        current_run_id: int,
        current_attempt: int,
        workflow_source_sha: str,
        expected_name: str,
        parent_run_id: str,
        parent_attempt: str,
        target_sha: str,
        release_slug: str,
    ) -> bool:
        if current_attempt != 1:
            raise SequenceError("dedupe", "successor_rerun_refused")
        path = SEQUENCER_WORKFLOW_PATH
        workflow_id = self.workflow_id(path)
        current = self.request("GET", f"/actions/runs/{current_run_id}")
        if (
            not isinstance(current, Mapping)
            or current.get("id") != current_run_id
            or type(current.get("run_attempt")) is not int
            or current.get("run_attempt") != 1
            or current.get("workflow_id") != workflow_id
            or current.get("path") != path
            or current.get("event") != "workflow_dispatch"
            or current.get("head_branch") != "dev"
            or current.get("head_sha") != workflow_source_sha
            or current.get("name") != "Platform post-deploy live QA"
            or current.get("display_title") != expected_name
        ):
            raise SequenceError("dedupe", "successor_identity_invalid")
        lower_id = current_run_id
        earlier: list[int] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {"event": "workflow_dispatch", "branch": "dev", "per_page": 100, "page": page}
            )
            listing = self.request(
                "GET", f"/actions/workflows/platform-postdeploy-live-qa.yml/runs?{query}"
            )
            if not isinstance(listing, Mapping):
                raise SequenceError("dedupe", "successor_listing_invalid")
            rows = listing.get("workflow_runs")
            total = listing.get("total_count")
            if not isinstance(rows, list) or type(total) is not int or total < 0 or len(rows) > 100:
                raise SequenceError("dedupe", "successor_listing_invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise SequenceError("dedupe", "successor_listing_invalid")
                # GitHub exposes the workflow's static name separately from
                # the dynamic `run-name` value in `display_title`.
                if row.get("name") != "Platform post-deploy live QA":
                    continue
                run_name = row.get("display_title")
                if run_name != expected_name:
                    continue
                if (
                    row.get("workflow_id") != workflow_id
                    or row.get("path") != path
                    or row.get("event") != "workflow_dispatch"
                    or row.get("head_branch") != "dev"
                    or type(row.get("run_attempt")) is not int
                    or row.get("run_attempt") != 1
                ):
                    raise SequenceError("dedupe", "successor_parent_collision")
                candidate_id = _positive_id(row.get("id"), "dedupe")
                lower_id = min(lower_id, candidate_id)
                if candidate_id < current_run_id:
                    earlier.append(candidate_id)
            if page * 100 >= total:
                break
            page += 1
            if page > 100:
                raise SequenceError("dedupe", "successor_listing_exceeded")
        if lower_id != current_run_id:
            for prior_id in sorted(set(earlier)):
                prior = self.request("GET", f"/actions/runs/{prior_id}")
                if (
                    not isinstance(prior, Mapping)
                    or prior.get("id") != prior_id
                    or prior.get("workflow_id") != workflow_id
                    or prior.get("path") != path
                    or prior.get("event") != "workflow_dispatch"
                    or prior.get("head_branch") != "dev"
                    or prior.get("head_sha") != workflow_source_sha
                    or prior.get("name") != "Platform post-deploy live QA"
                    or prior.get("display_title") != expected_name
                    or type(prior.get("run_attempt")) is not int
                    or prior.get("run_attempt") != 1
                ):
                    raise SequenceError("dedupe", "prior_run_identity_invalid")
                authorized = self.exact_handoff_authorization(
                    parent_id=int(parent_run_id),
                    attempt=int(parent_attempt),
                    target_sha=target_sha,
                    release_slug=release_slug,
                    sequence_run_id=None,
                    sequence_attempt=None,
                    sequence_workflow_id=workflow_id,
                )
                if authorized.get("sequence_run_id") != str(prior_id):
                    continue
                if prior.get("status") != "completed":
                    raise SequenceError("dedupe", "prior_run_state_ambiguous")
                prior_claimed = self._has_prior_child_receipt(
                    sequence_run_id=prior_id,
                    sequence_attempt=1,
                    parent_run_id=parent_run_id,
                    parent_attempt=parent_attempt,
                    target_sha=target_sha,
                    sequence_source_sha=workflow_source_sha,
                )
                if prior_claimed:
                    return False
                if prior.get("conclusion") not in {
                    "success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped",
                }:
                    raise SequenceError("dedupe", "prior_run_conclusion_ambiguous")
                raise SequenceError("dedupe", "prior_authorized_without_child_receipt")
            # Unauthorised direct dispatches cannot claim a parent tuple. The
            # unique parent-issued receipt authorizes exactly one sequence ID.
            return True
        # The run-name itself carries the authenticated deployment tuple; reject
        # malformed or accidentally truncated names before any live dispatch.
        expected_parent_fragment = f"parent={parent_run_id}.{parent_attempt} source={target_sha}"
        if expected_parent_fragment not in expected_name:
            raise SequenceError("dedupe", "successor_parent_binding_invalid")
        return True

    def _has_prior_child_receipt(
        self,
        *,
        sequence_run_id: int,
        sequence_attempt: int,
        parent_run_id: str,
        parent_attempt: str,
        target_sha: str,
        sequence_source_sha: str,
    ) -> bool:
        listing = self.request(
            "GET", f"/actions/runs/{sequence_run_id}/artifacts?per_page=100&page=1"
        )
        if not isinstance(listing, Mapping):
            raise SequenceError("dedupe", "prior_artifact_list_invalid")
        rows = listing.get("artifacts")
        total = listing.get("total_count")
        if (
            not isinstance(rows, list)
            or type(total) is not int
            or total != len(rows)
            or total >= 100
            or any(
                not isinstance(row, Mapping)
                or not isinstance(row.get("name"), str)
                or type(row.get("id")) is not int
                or row.get("id") <= 0
                for row in rows
            )
        ):
            raise SequenceError("dedupe", "prior_artifact_list_invalid")
        claimed_roles = [
            role for role in ("public", "user-qa")
            if sum(
                row.get("name") == child_receipt_name(role, sequence_run_id, sequence_attempt)
                for row in rows
            )
        ]
        if not claimed_roles:
            return False
        for role in claimed_roles:
            document = self.exact_child_receipt(
                sequence_run_id=sequence_run_id,
                sequence_attempt=sequence_attempt,
                role=role,
                target_sha=sequence_source_sha,
                timeout_seconds=0,
            )
            try:
                validate_child_receipt(
                    document,
                    repository=self.repository,
                    role=role,
                    sequence_run_id=sequence_run_id,
                    sequence_attempt=sequence_attempt,
                    sequence_source_sha=sequence_source_sha,
                    deploy_run_id=int(parent_run_id),
                    deploy_attempt=int(parent_attempt),
                    child_run_id=_positive_id(document.get("child_run_id"), "dedupe"),
                    child_attempt=1,
                    child_workflow_id=_positive_id(document.get("child_workflow_id"), "dedupe"),
                    target_sha=target_sha,
                )
            except SequenceError:
                raise SequenceError("dedupe", "prior_child_receipt_invalid") from None
        return True

    def verify_active_sequence(
        self,
        *,
        sequence_run_id: int,
        sequence_attempt: int,
        target_sha: str,
        deploy_run_id: int,
        deploy_attempt: int,
        check_current_dev: bool = True,
    ) -> None:
        path = SEQUENCER_WORKFLOW_PATH
        workflow_id = self.workflow_id(path)
        run = self.request("GET", f"/actions/runs/{sequence_run_id}")
        expected_title = _run_name(str(deploy_run_id), str(deploy_attempt), target_sha)
        if (
            not isinstance(run, Mapping)
            or run.get("id") != sequence_run_id
            or type(run.get("run_attempt")) is not int
            or run.get("run_attempt") != sequence_attempt
            or run.get("workflow_id") != workflow_id
            or run.get("path") != path
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "dev"
            or run.get("head_sha") != target_sha
            or run.get("name") != "Platform post-deploy live QA"
            or run.get("display_title") != expected_title
            or run.get("status") != "in_progress"
            or run.get("conclusion") is not None
        ):
            raise SequenceError("child_receipt", "sequence_identity_invalid")
        jobs = self.request(
            "GET", f"/actions/runs/{sequence_run_id}/attempts/{sequence_attempt}/jobs?per_page=100&page=1"
        )
        if not isinstance(jobs, Mapping):
            raise SequenceError("child_receipt", "sequence_jobs_invalid")
        rows = jobs.get("jobs")
        total = jobs.get("total_count")
        if (
            not isinstance(rows, list)
            or type(total) is not int
            or total != len(rows)
            or len(rows) >= 100
        ):
            raise SequenceError("child_receipt", "sequence_jobs_invalid")
        if any(
            not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
            for row in rows
        ):
            raise SequenceError("child_receipt", "sequence_jobs_invalid")
        matches = [
            row for row in rows if row.get("name") == SEQUENCE_JOB_NAME
        ]
        if len(matches) != 1 or matches[0].get("status") != "in_progress":
            raise SequenceError("child_receipt", "sequence_job_not_active")
        mode, _workflow_id, _release_slug = self.verify_parent_deploy(
            parent_id=deploy_run_id,
            attempt=deploy_attempt,
            target_sha=target_sha,
        )
        if mode != "deploy":
            raise SequenceError("child_receipt", "parent_mode_invalid")
        self.exact_handoff_authorization(
            parent_id=deploy_run_id,
            attempt=deploy_attempt,
            target_sha=target_sha,
            release_slug=_release_slug,
            sequence_run_id=sequence_run_id,
            sequence_attempt=sequence_attempt,
            sequence_workflow_id=workflow_id,
        )
        if check_current_dev and self.current_dev_sha() != target_sha:
            raise SequenceError("child_receipt", "dev_head_mismatch")


def child_receipt_name(role: str, sequence_run_id: int, sequence_attempt: int) -> str:
    if role not in {"public", "user-qa"}:
        raise SequenceError("child_receipt", "role_invalid")
    return f"{CHILD_RECEIPT_PREFIX}{role}-{sequence_run_id}-{sequence_attempt}"


def validate_child_receipt(
    document: object,
    *,
    repository: str,
    role: str,
    sequence_run_id: int,
    sequence_attempt: int,
    sequence_source_sha: str,
    deploy_run_id: int,
    deploy_attempt: int,
    child_run_id: int,
    child_attempt: int,
    child_workflow_id: int,
    target_sha: str,
    expected_public_run_id: str | None = None,
) -> dict[str, object]:
    keys = {
        "schema", "kind", "repository", "sequence_workflow_path",
        "sequence_run_id", "sequence_run_attempt", "sequence_source_sha",
        "deploy_run_id", "deploy_run_attempt", "source_sha", "role",
        "child_workflow_path", "child_workflow_id", "child_run_id",
        "child_run_attempt", "expected_target_sha", "public_run_id", "marker",
    }
    expected_path = PUBLIC_WORKFLOW_PATH if role == "public" else USER_QA_WORKFLOW_PATH
    if (
        role not in {"public", "user-qa"}
        or not isinstance(document, Mapping)
        or set(document) != keys
        or type(document.get("schema")) is not int
        or document.get("schema") != 1
        or document.get("kind") != "platform-live-qa-child-dispatch"
        or document.get("repository") != repository
        or document.get("sequence_workflow_path") != SEQUENCER_WORKFLOW_PATH
        or document.get("sequence_run_id") != str(sequence_run_id)
        or document.get("sequence_run_attempt") != str(sequence_attempt)
        or document.get("sequence_source_sha") != sequence_source_sha
        or document.get("deploy_run_id") != str(deploy_run_id)
        or document.get("deploy_run_attempt") != str(deploy_attempt)
        or document.get("source_sha") != target_sha
        or document.get("role") != role
        or document.get("child_workflow_path") != expected_path
        or type(document.get("child_workflow_id")) is not int
        or document.get("child_workflow_id") != child_workflow_id
        or document.get("child_run_id") != str(child_run_id)
        or document.get("child_run_attempt") != str(child_attempt)
        or document.get("expected_target_sha") != target_sha
        or SHA_RE.fullmatch(target_sha) is None
        or SHA_RE.fullmatch(sequence_source_sha) is None
    ):
        raise SequenceError("child_receipt", "receipt_identity_invalid")
    public_run_id = document.get("public_run_id")
    marker = document.get("marker")
    expected_marker = _marker(str(deploy_run_id), str(deploy_attempt), target_sha)
    if role == "public":
        if public_run_id != str(child_run_id) or marker != expected_marker:
            raise SequenceError("child_receipt", "receipt_role_invalid")
    elif (
        not isinstance(public_run_id, str)
        or RUN_RE.fullmatch(public_run_id) is None
        or marker != expected_marker
        or (expected_public_run_id is not None and public_run_id != expected_public_run_id)
    ):
        raise SequenceError("child_receipt", "receipt_role_invalid")
    return dict(document)


def validate_active_child_from_environment() -> None:
    sequence_run_id = _environment_id("SEQUENCE_RUN_ID")
    sequence_attempt = _environment_id("SEQUENCE_RUN_ATTEMPT")
    deploy_run_id = _environment_id("SEQUENCE_DEPLOY_RUN_ID")
    deploy_attempt = _environment_id("SEQUENCE_DEPLOY_RUN_ATTEMPT")
    child_run_id = _environment_id("GITHUB_RUN_ID")
    child_attempt = _environment_id("GITHUB_RUN_ATTEMPT")
    target_sha = os.environ.get("GITHUB_SHA", "")
    role = os.environ.get("SEQUENCE_CHILD_ROLE", "")
    if (
        role not in {"public", "user-qa"}
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/dev"
        or os.environ.get("GITHUB_REPOSITORY") != REPO
        or os.environ.get("EXPECTED_TARGET_SHA") != target_sha
        or SHA_RE.fullmatch(target_sha) is None
    ):
        raise SequenceError("child_receipt", "child_event_invalid")
    gh = GitHubActions(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GH_TOKEN", ""),
    )
    expected_path = PUBLIC_WORKFLOW_PATH if role == "public" else USER_QA_WORKFLOW_PATH
    workflow_id = gh.workflow_id(expected_path)
    child_run = gh.request("GET", f"/actions/runs/{child_run_id}")
    if (
        not isinstance(child_run, Mapping)
        or child_run.get("id") != child_run_id
        or type(child_run.get("run_attempt")) is not int
        or child_run.get("run_attempt") != child_attempt
        or child_run.get("workflow_id") != workflow_id
        or child_run.get("path") != expected_path
        or child_run.get("event") != "workflow_dispatch"
        or child_run.get("head_branch") != "dev"
        or child_run.get("head_sha") != target_sha
        or child_run.get("status") not in CHILD_PENDING
        or child_run.get("conclusion") is not None
    ):
        raise SequenceError("child_receipt", "child_run_identity_invalid")
    gh.verify_active_sequence(
        sequence_run_id=sequence_run_id,
        sequence_attempt=sequence_attempt,
        target_sha=target_sha,
        deploy_run_id=deploy_run_id,
        deploy_attempt=deploy_attempt,
    )
    document = gh.exact_child_receipt(
        sequence_run_id=sequence_run_id,
        sequence_attempt=sequence_attempt,
        role=role,
        target_sha=os.environ.get("SEQUENCE_SOURCE_SHA", ""),
    )
    receipt = validate_child_receipt(
        document,
        repository=gh.repository,
        role=role,
        sequence_run_id=sequence_run_id,
        sequence_attempt=sequence_attempt,
        sequence_source_sha=os.environ.get("SEQUENCE_SOURCE_SHA", ""),
        deploy_run_id=deploy_run_id,
        deploy_attempt=deploy_attempt,
        child_run_id=child_run_id,
        child_attempt=child_attempt,
        child_workflow_id=workflow_id,
        target_sha=target_sha,
        expected_public_run_id=os.environ.get("SEQUENCE_PUBLIC_RUN_ID") or None,
    )
    if role == "user-qa" and os.environ.get("SEQUENCE_PUBLIC_RUN_ID") != receipt["public_run_id"]:
        raise SequenceError("child_receipt", "public_child_binding_invalid")
    if role == "public" and (
        os.environ.get("LIVE_PROVISION") != "true"
        or os.environ.get("LIVE_MARKER") != receipt["marker"]
    ):
        raise SequenceError("child_receipt", "public_marker_binding_invalid")


def _event_context() -> tuple[str, str, str, int, int, str]:
    target = os.environ.get("DEPLOY_TARGET_SHA", "")
    parent_id_text = os.environ.get("DEPLOY_RUN_ID", "")
    parent_attempt_text = os.environ.get("DEPLOY_RUN_ATTEMPT", "")
    current_run_text = os.environ.get("GITHUB_RUN_ID", "")
    current_attempt_text = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    workflow_source = os.environ.get("GITHUB_SHA", "")
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/dev"
        or os.environ.get("GITHUB_REPOSITORY") != REPO
        or SHA_RE.fullmatch(target) is None
        or SHA_RE.fullmatch(workflow_source) is None
        or workflow_source != target
        or RUN_RE.fullmatch(parent_id_text) is None
        or RUN_RE.fullmatch(parent_attempt_text) is None
        or RUN_RE.fullmatch(current_run_text) is None
        or RUN_RE.fullmatch(current_attempt_text) is None
    ):
        raise SequenceError("parent", "workflow_dispatch_event_invalid")
    return (
        target,
        parent_id_text,
        parent_attempt_text,
        int(current_run_text),
        int(current_attempt_text),
        workflow_source,
    )


def _actions_client() -> GitHubActions:
    return GitHubActions(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GH_TOKEN", ""),
    )


def _verified_parent_context(
    gh: GitHubActions, *, check_current_dev: bool = True
) -> tuple[str, str, str, int, int, str, str]:
    target_sha, parent_id_text, parent_attempt_text, current_run_id, current_attempt, workflow_source = _event_context()
    parent_id, parent_attempt = int(parent_id_text), int(parent_attempt_text)
    expected_name = _run_name(parent_id_text, parent_attempt_text, target_sha)
    # Authenticate the parent's actual-deploy receipt and the parent-issued
    # exact-child authorization before waiting on terminal workflow state.
    # This makes a manual workflow_dispatch harmless without borrowing or
    # fabricating workflow_run context.
    initial_receipt = gh.exact_deployment_receipt(
        parent_id=parent_id, attempt=parent_attempt, target_sha=target_sha
    )
    if initial_receipt.get("verified_mode") != "deploy":
        raise SequenceSkipped("mode_not_normal_deploy")
    sequence_workflow_id = gh.workflow_id(SEQUENCER_WORKFLOW_PATH)
    gh.exact_handoff_authorization(
        parent_id=parent_id,
        attempt=parent_attempt,
        target_sha=target_sha,
        release_slug=str(initial_receipt["release_slug"]),
        sequence_run_id=current_run_id,
        sequence_attempt=current_attempt,
        sequence_workflow_id=sequence_workflow_id,
    )
    gh.wait_for_parent_success(
        parent_id=parent_id,
        attempt=parent_attempt,
        target_sha=target_sha,
    )
    # Only the exact terminal successful normal-deploy parent and its source
    # status can authorize production QA.
    mode, _deploy_workflow_id, _release_slug = gh.verify_parent_deploy(
        parent_id=parent_id, attempt=parent_attempt, target_sha=target_sha
    )
    if mode != "deploy":
        raise SequenceSkipped("mode_not_normal_deploy")
    if not gh.verify_sequence_first_delivery(
        current_run_id=current_run_id,
        current_attempt=current_attempt,
        workflow_source_sha=workflow_source,
        expected_name=expected_name,
        parent_run_id=parent_id_text,
        parent_attempt=parent_attempt_text,
        target_sha=target_sha,
        release_slug=_release_slug,
    ):
        raise SequenceSkipped("duplicate_parent_delivery")
    if check_current_dev and gh.current_dev_sha() != target_sha:
        raise SequenceError("parent", "dev_head_mismatch")
    return (
        target_sha,
        parent_id_text,
        parent_attempt_text,
        current_run_id,
        current_attempt,
        workflow_source,
        expected_name,
    )


def _set_output(name: str, value: str) -> None:
    if not re.fullmatch(r"[a-z_]+", name) or "\n" in value or "\r" in value:
        raise SequenceError("output", "output_invalid")
    path_text = os.environ.get("GITHUB_OUTPUT", "")
    if not path_text:
        raise SequenceError("output", "output_unavailable")
    fd = os.open(path_text, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.write(fd, f"{name}={value}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def dispatch_public() -> int:
    gh = _actions_client()
    target_sha, parent_id, parent_attempt, current_id, current_attempt, source_sha, _ = _verified_parent_context(gh)
    public_workflow_id = gh.workflow_id(PUBLIC_WORKFLOW_PATH)
    public_run_id = gh.dispatch(
        PUBLIC_WORKFLOW_PATH,
        public_workflow_id,
        {
            "base_url": "https://old-sparky.com",
            "provision": "true",
            "marker": _marker(parent_id, parent_attempt, target_sha),
            "expected_target_sha": target_sha,
            "sequence_run_id": str(current_id),
            "sequence_run_attempt": str(current_attempt),
            "sequence_deploy_run_id": parent_id,
            "sequence_deploy_run_attempt": parent_attempt,
            "sequence_source_sha": source_sha,
        },
    )
    _set_output("run_id", str(public_run_id))
    _set_output("workflow_id", str(public_workflow_id))
    return public_run_id


def finish_public_and_dispatch_userqa() -> int:
    gh = _actions_client()
    target_sha, parent_id, parent_attempt, current_id, current_attempt, source_sha, _ = _verified_parent_context(gh)
    public_run_id = _environment_id("PUBLIC_CHILD_RUN_ID")
    public_workflow_id = gh.workflow_id(PUBLIC_WORKFLOW_PATH)
    gh.wait_for_run(
        run_id=public_run_id,
        workflow_id=public_workflow_id,
        target_sha=target_sha,
        expected_jobs=(
            "Validate live-launch handoff",
            "live-sanity",
            "Sanitize live-launch report",
        ),
    )

    # Public launch intentionally leaves the exact roster for UserQA cleanup.
    # If dev advanced, fail before starting the credential-bearing journey.
    if gh.current_dev_sha() != target_sha:
        raise SequenceError("user_qa", "dev_head_mismatch")
    user_workflow_id = gh.workflow_id(USER_QA_WORKFLOW_PATH)
    user_run_id = gh.dispatch(
        USER_QA_WORKFLOW_PATH,
        user_workflow_id,
        {
            "confirmation": "RUN-LIVE-USER-QA",
            "expected_target_sha": target_sha,
            "sequence_run_id": str(current_id),
            "sequence_run_attempt": str(current_attempt),
            "sequence_deploy_run_id": parent_id,
            "sequence_deploy_run_attempt": parent_attempt,
            "sequence_source_sha": source_sha,
            "sequence_public_run_id": str(public_run_id),
        },
    )
    _set_output("run_id", str(user_run_id))
    _set_output("workflow_id", str(user_workflow_id))
    _set_output("public_run_id", str(public_run_id))
    _set_output("marker", _marker(parent_id, parent_attempt, target_sha))
    return user_run_id


def wait_userqa() -> tuple[int, str]:
    gh = _actions_client()
    target_sha, parent_id, parent_attempt, current_id, current_attempt, _source_sha, _ = _verified_parent_context(
        gh, check_current_dev=False
    )
    gh.verify_active_sequence(
        sequence_run_id=current_id,
        sequence_attempt=current_attempt,
        target_sha=target_sha,
        deploy_run_id=int(parent_id),
        deploy_attempt=int(parent_attempt),
        check_current_dev=False,
    )
    user_run_id = _environment_id("USER_QA_CHILD_RUN_ID")
    user_workflow_id = gh.workflow_id(USER_QA_WORKFLOW_PATH)
    gh.wait_for_run(
        run_id=user_run_id,
        workflow_id=user_workflow_id,
        target_sha=target_sha,
        expected_jobs=(
            "Validate live-user QA handoff",
            "Automated live-user QA on production server",
        ),
    )
    return user_run_id, target_sha


def _write_handoff_authorization(document: Mapping[str, object]) -> Path:
    raw = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(raw) > RECEIPT_LIMIT:
        raise SequenceError("handoff", "receipt_size_invalid")
    temp_root = Path(os.environ.get("RUNNER_TEMP", ""))
    if not temp_root.is_absolute() or temp_root.resolve(strict=True) != temp_root:
        raise SequenceError("handoff", "receipt_parent_invalid")
    receipt_dir = temp_root / "postdeploy-handoff-authorization"
    os.mkdir(receipt_dir, 0o700)
    path = receipt_dir / HANDOFF_AUTH_FILE
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise SequenceError("handoff", "receipt_file_invalid")
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise SequenceError("handoff", "receipt_write_failed")
            offset += written
        os.fsync(fd)
        completed = os.fstat(fd)
        if completed.st_size != len(raw) or stat.S_IMODE(completed.st_mode) != 0o600:
            raise SequenceError("handoff", "receipt_file_invalid")
    finally:
        os.close(fd)
    return path


def dispatch_handoff() -> int:
    parent_id = _environment_id("GITHUB_RUN_ID")
    parent_attempt = _environment_id("GITHUB_RUN_ATTEMPT")
    target_sha = os.environ.get("GITHUB_SHA", "")
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/dev"
        or os.environ.get("GITHUB_REPOSITORY") != REPO
        or os.environ.get("WORKFLOW_MODE") != "deploy"
        or SHA_RE.fullmatch(target_sha) is None
    ):
        raise SequenceError("handoff", "parent_event_invalid")
    gh = _actions_client()
    _deploy_workflow_id, release_slug = gh.verify_parent_handoff_ready(
        parent_id=parent_id,
        attempt=parent_attempt,
        target_sha=target_sha,
    )
    sequence_workflow_id = gh.workflow_id(SEQUENCER_WORKFLOW_PATH)
    sequence_run_id = gh.dispatch(
        SEQUENCER_WORKFLOW_PATH,
        sequence_workflow_id,
        {
            "deploy_run_id": str(parent_id),
            "deploy_run_attempt": str(parent_attempt),
            "target_sha": target_sha,
        },
    )
    document = {
        "schema": 1,
        "kind": HANDOFF_AUTH_KIND,
        "repository": REPO,
        "deploy_workflow_path": DEPLOY_WORKFLOW_PATH,
        "deploy_run_id": str(parent_id),
        "deploy_run_attempt": str(parent_attempt),
        "source_sha": target_sha,
        "mode": "deploy",
        "release_slug": release_slug,
        "sequence_workflow_path": SEQUENCER_WORKFLOW_PATH,
        "sequence_workflow_id": sequence_workflow_id,
        "sequence_run_id": str(sequence_run_id),
        "sequence_run_attempt": "1",
    }
    validate_handoff_authorization(
        document,
        repository=REPO,
        parent_run_id=parent_id,
        parent_attempt=parent_attempt,
        target_sha=target_sha,
        release_slug=str(document["release_slug"]),
        sequence_run_id=sequence_run_id,
        sequence_attempt=1,
        sequence_workflow_id=sequence_workflow_id,
    )
    receipt_path = _write_handoff_authorization(document)
    _set_output("run_id", str(sequence_run_id))
    _set_output("workflow_id", str(sequence_workflow_id))
    _set_output("receipt_path", str(receipt_path))
    return sequence_run_id


def write_child_receipt(role: str) -> Path:
    target_sha, parent_id, parent_attempt, current_id, current_attempt, source_sha, _ = _event_context()
    child_id = _environment_id("CHILD_RUN_ID")
    if (
        current_attempt != 1
        or role not in {"public", "user-qa"}
        or SHA_RE.fullmatch(source_sha) is None
        or source_sha != target_sha
    ):
        raise SequenceError("child_receipt", "receipt_input_invalid")
    child_path = PUBLIC_WORKFLOW_PATH if role == "public" else USER_QA_WORKFLOW_PATH
    gh = _actions_client()
    child_workflow_id = gh.workflow_id(child_path)
    marker = _marker(parent_id, parent_attempt, target_sha)
    public_run_id = str(child_id) if role == "public" else str(_environment_id("PUBLIC_CHILD_RUN_ID"))
    document = {
        "schema": 1,
        "kind": "platform-live-qa-child-dispatch",
        "repository": REPO,
        "sequence_workflow_path": SEQUENCER_WORKFLOW_PATH,
        "sequence_run_id": str(current_id),
        "sequence_run_attempt": str(current_attempt),
        "sequence_source_sha": source_sha,
        "deploy_run_id": parent_id,
        "deploy_run_attempt": parent_attempt,
        "source_sha": target_sha,
        "role": role,
        "child_workflow_path": child_path,
        "child_workflow_id": child_workflow_id,
        "child_run_id": str(child_id),
        "child_run_attempt": "1",
        "expected_target_sha": target_sha,
        "public_run_id": public_run_id,
        "marker": marker,
    }
    raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(raw) > RECEIPT_LIMIT:
        raise SequenceError("child_receipt", "receipt_size_invalid")
    temp_root = Path(os.environ.get("RUNNER_TEMP", ""))
    if not temp_root.is_absolute() or temp_root.resolve(strict=True) != temp_root:
        raise SequenceError("child_receipt", "receipt_parent_invalid")
    receipt_dir = temp_root / f"child-receipt-{role}"
    os.mkdir(receipt_dir, 0o700)
    path = receipt_dir / CHILD_RECEIPT_FILE
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise SequenceError("child_receipt", "receipt_file_invalid")
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise SequenceError("child_receipt", "receipt_write_failed")
            offset += written
        os.fsync(fd)
        completed = os.fstat(fd)
        if completed.st_size != len(raw) or stat.S_IMODE(completed.st_mode) != 0o600:
            raise SequenceError("child_receipt", "receipt_file_invalid")
    finally:
        os.close(fd)
    return path


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments == ["dispatch-handoff"]:
            child_id = dispatch_handoff()
            print(f"LIVE_QA_HANDOFF schema=1 status=authorized sequence_run_id={child_id}")
            return 0
        if arguments == ["dispatch-public"]:
            child_id = dispatch_public()
            print(f"LIVE_QA_SEQUENCE schema=1 status=public_dispatched run_id={child_id}")
            return 0
        if arguments == ["finish-public"]:
            child_id = finish_public_and_dispatch_userqa()
            print(f"LIVE_QA_SEQUENCE schema=1 status=userqa_dispatched run_id={child_id}")
            return 0
        if arguments == ["wait-userqa"]:
            child_id, target = wait_userqa()
            print(f"LIVE_QA_SEQUENCE schema=1 status=passed userqa_run_id={child_id} source_sha={target}")
            return 0
        if len(arguments) == 2 and arguments[0] == "write-receipt":
            write_child_receipt(arguments[1])
            print(f"LIVE_QA_SEQUENCE schema=1 status=receipt_ready role={arguments[1]}")
            return 0
        if arguments == ["validate-child"]:
            validate_active_child_from_environment()
            print(
                "LIVE_QA_SEQUENCE schema=1 status=child_authorized "
                f"role={os.environ.get('SEQUENCE_CHILD_ROLE')} source_sha={os.environ.get('EXPECTED_TARGET_SHA')}"
            )
            return 0
        raise SequenceError("input", "command_invalid")
    except SequenceSkipped as skipped:
        print(
            "LIVE_QA_SEQUENCE schema=1 status=skipped "
            f"reason={skipped.reason} source_sha={os.environ.get('DEPLOY_TARGET_SHA', 'unavailable')}"
        )
        return 0
    except SequenceError as exc:
        print(
            f"LIVE_QA_SEQUENCE schema=1 status=failed stage={exc.stage} "
            f"reason={exc.reason} source_sha={os.environ.get('DEPLOY_TARGET_SHA', 'unavailable')}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        print(
            "LIVE_QA_SEQUENCE schema=1 status=failed stage=internal "
            f"reason=unexpected source_sha={os.environ.get('DEPLOY_TARGET_SHA', 'unavailable')}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
