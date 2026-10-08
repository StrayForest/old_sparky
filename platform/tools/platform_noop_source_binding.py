#!/usr/bin/env python3
"""Bind runner provenance to an unchanged production app release.

The runner SHA remains the identity for checked-out workflow and measurement
code. A different app target is accepted only from a closed, exact
baseline-reconcile no-op receipt whose active release tuple still matches.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

# The active shell invokes this file with Python isolated mode. Add only the
# fixed application root derived from this immutable release path before
# importing its sibling package; isolated mode does not add the checkout root.
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from tools.platform_ci_classifier import FULL_GATE_IDS
from tools.platform_ci_classifier import ClassifierError
from tools.platform_deploy_baseline import (
    BASELINE_KEYS,
    HEX_DIGEST_RE,
    RELEASE_SLUG_RE,
    SHA_RE,
    ProvenanceError,
    classify_cumulative_baseline,
    validate_cumulative_reconcile_route,
)


RECEIPT_KIND = "platform-production-baseline-noop"
RECEIPT_FILE = "platform-production-noop-source-receipt.json"
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BINDING_BYTES = 16 * 1024
MAX_WORKFLOW_HANDOFF_BYTES = MAX_SOURCE_BINDING_BYTES + 4096
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
SHA256_ARTIFACT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class _SafeArtifactRedirect(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS artifact redirects without forwarding credentials cross-host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or not new.netloc or new.username is not None or new.password is not None:
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if old.netloc.casefold() != new.netloc.casefold():
            sensitive = {"authorization", "proxy-authorization", "cookie", "cookie2"}
            for collection in (redirected.headers, redirected.unredirected_hdrs):
                for key in tuple(collection):
                    if key.casefold() in sensitive:
                        collection.pop(key, None)
        return redirected


class _SameOriginAPIRedirect(urllib.request.HTTPRedirectHandler):
    """Allow metadata redirects only within the authenticated API origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (
            old.scheme != "https"
            or new.scheme != "https"
            or not old.netloc
            or old.netloc.casefold() != new.netloc.casefold()
            or new.username is not None
            or new.password is not None
        ):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fail(message: str) -> ProvenanceError:
    return ProvenanceError(message)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _fail("no-op receipt is not canonical JSON") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _positive_run_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise _fail(f"no-op receipt {field} is malformed")
    return value


def _validate_expected_identity(value: object, expected: object, field: str) -> str:
    parsed = _positive_run_identity(value, field)
    if not isinstance(expected, str) or RUN_ID_RE.fullmatch(expected) is None or parsed != expected:
        raise _fail(f"no-op receipt {field} does not match the authenticated run")
    return parsed


def _validate_baseline_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != BASELINE_KEYS:
        raise _fail("no-op receipt active baseline schema is invalid")
    source_sha = value.get("source_sha")
    release_slug = value.get("release_slug")
    release_digest = value.get("release_json_sha256")
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        raise _fail("no-op receipt baseline source SHA is invalid")
    if not isinstance(release_slug, str) or RELEASE_SLUG_RE.fullmatch(release_slug) is None:
        raise _fail("no-op receipt baseline release slug is invalid")
    if not isinstance(release_digest, str) or HEX_DIGEST_RE.fullmatch(release_digest) is None:
        raise _fail("no-op receipt baseline RELEASE digest is invalid")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise _fail("no-op receipt baseline schema version is invalid")
    for field in ("current_link_dev", "release_dev"):
        number = value.get(field)
        if type(number) is not int or number < 0 or number > 2**63 - 1:
            raise _fail(f"no-op receipt baseline {field} is invalid")
    for field in ("current_link_ino", "release_ino"):
        number = value.get(field)
        if type(number) is not int or number <= 0 or number > 2**63 - 1:
            raise _fail(f"no-op receipt baseline {field} is invalid")
    if value.get("pending_operation") is not False:
        raise _fail("no-op receipt baseline has a pending or unknown operation")
    return dict(value)


def _bounded_archive_member(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    *,
    max_bytes: int = MAX_RECEIPT_BYTES,
) -> bytes:
    """Read a receipt member without trusting ZIP size headers for allocation."""

    chunks: list[bytes] = []
    total = 0
    with archive.open(entry, "r") as stream:
        while total <= max_bytes:
            chunk = stream.read(min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    if total > max_bytes:
        raise _fail("archive member exceeds its bound")
    return b"".join(chunks)


def validate_noop_receipt_document(
    document: object,
    *,
    expected_runner_sha: str,
    expected_deploy_run_id: str,
    expected_deploy_attempt: str,
    expected_security_run_id: str,
    expected_security_attempt: str,
    expected_autodeploy_run_id: str,
    expected_autodeploy_attempt: str,
) -> dict[str, Any]:
    """Validate the closed document and return its dual-source binding."""

    expected = {
        "schema",
        "kind",
        "runner_sha",
        "mode",
        "production_deploy",
        "source_security",
        "autodeploy",
        "baseline_identity",
        "route",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise _fail("no-op receipt document schema is invalid")
    if type(document.get("schema")) is not int or document["schema"] != 1:
        raise _fail("no-op receipt schema version is invalid")
    if document.get("kind") != RECEIPT_KIND or document.get("mode") != "baseline-reconcile":
        raise _fail("no-op receipt route identity is invalid")
    runner_sha = document.get("runner_sha")
    if (
        not isinstance(expected_runner_sha, str)
        or SHA_RE.fullmatch(expected_runner_sha) is None
        or runner_sha != expected_runner_sha
    ):
        raise _fail("no-op receipt runner SHA does not match the checked-out source")

    run_groups = (
        ("production_deploy", expected_deploy_run_id, expected_deploy_attempt),
        ("source_security", expected_security_run_id, expected_security_attempt),
        ("autodeploy", expected_autodeploy_run_id, expected_autodeploy_attempt),
    )
    for name, run_id, attempt in run_groups:
        row = document.get(name)
        if not isinstance(row, Mapping) or set(row) != {"run_id", "run_attempt"}:
            raise _fail(f"no-op receipt {name} identity is malformed")
        _validate_expected_identity(row.get("run_id"), run_id, f"{name} run id")
        _validate_expected_identity(row.get("run_attempt"), attempt, f"{name} attempt")

    baseline = _validate_baseline_identity(document.get("baseline_identity"))
    if baseline["source_sha"] == runner_sha:
        raise _fail("no-op receipt cannot replace the ordinary same-source binding")

    route = document.get("route")
    route_keys = {
        "no_op",
        "runtime_required",
        "cumulative_manifest_sha256",
        "incremental_manifest",
        "cumulative_manifest",
    }
    if not isinstance(route, Mapping) or set(route) != route_keys:
        raise _fail("no-op receipt route schema is invalid")
    if route.get("no_op") is not True or route.get("runtime_required") is not False:
        raise _fail("no-op receipt does not prove a completed non-runtime route")
    manifest = route.get("cumulative_manifest")
    incremental = route.get("incremental_manifest")
    manifest_digest = route.get("cumulative_manifest_sha256")
    if not isinstance(manifest, Mapping) or not isinstance(incremental, Mapping):
        raise _fail("no-op receipt classifier manifests are malformed")
    if not isinstance(manifest_digest, str) or HEX_DIGEST_RE.fullmatch(manifest_digest) is None:
        raise _fail("no-op receipt cumulative manifest digest is malformed")
    if hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest() != manifest_digest:
        raise _fail("no-op receipt cumulative manifest digest does not match")
    if (
        manifest.get("target_sha") != runner_sha
        or manifest.get("deployable") is not False
        or manifest.get("runtime_sensitive") is not False
        or manifest.get("fallback") is not False
        or manifest.get("class") != "full"
        or manifest.get("expected_gates") != list(FULL_GATE_IDS)
    ):
        raise _fail("no-op receipt cumulative route is deployable or runtime-sensitive")
    try:
        from tools.platform_ci_classifier import validate_manifest

        validate_manifest(incremental, expected_target_sha=runner_sha)
        cumulative_result = classify_cumulative_baseline(
            incremental,
            manifest.get("files", ()),
            expected_target_sha=runner_sha,
        )
        route_result = validate_cumulative_reconcile_route(cumulative_result)
    except (ClassifierError, ProvenanceError, KeyError, TypeError, ValueError) as exc:
        raise _fail("no-op receipt cumulative route could not be revalidated") from exc
    if (
        cumulative_result.get("no_op") is not True
        or cumulative_result.get("manifest") != dict(manifest)
        or route_result != {"no_op": True, "runtime_required": False}
    ):
        raise _fail("no-op receipt cumulative route does not match canonical classification")

    document_digest = hashlib.sha256(_canonical_json_bytes(document)).hexdigest()
    return {
        "schema": 1,
        "binding_mode": "verified-noop",
        "runner_sha": runner_sha,
        "app_target_sha": baseline["source_sha"],
        "baseline_identity": baseline,
        "cumulative_manifest_sha256": manifest_digest,
        "source_security_run_id": document["source_security"]["run_id"],
        "source_security_run_attempt": document["source_security"]["run_attempt"],
        "autodeploy_run_id": document["autodeploy"]["run_id"],
        "autodeploy_run_attempt": document["autodeploy"]["run_attempt"],
        "production_deploy_run_id": document["production_deploy"]["run_id"],
        "production_deploy_run_attempt": document["production_deploy"]["run_attempt"],
        "receipt_document_sha256": document_digest,
    }


def build_noop_receipt_document(
    *,
    runner_sha: str,
    production_deploy_run_id: str,
    production_deploy_run_attempt: str,
    source_security_run_id: str,
    source_security_run_attempt: str,
    autodeploy_run_id: str,
    autodeploy_run_attempt: str,
    baseline_identity: object,
    incremental_manifest: object,
    cumulative_result: object,
) -> dict[str, Any]:
    """Build one receipt only for a canonical completed non-runtime no-op."""

    baseline = _validate_baseline_identity(baseline_identity)
    if not isinstance(cumulative_result, Mapping) or set(cumulative_result) != {"manifest", "no_op"}:
        raise _fail("no-op receipt cumulative result is malformed")
    manifest = cumulative_result.get("manifest")
    if (
        cumulative_result.get("no_op") is not True
        or not isinstance(manifest, Mapping)
        or manifest.get("target_sha") != runner_sha
        or manifest.get("deployable") is not False
        or manifest.get("runtime_sensitive") is not False
    ):
        raise _fail("no-op receipt may describe only a nondeployable non-runtime route")
    route_result = validate_cumulative_reconcile_route(cumulative_result)
    if route_result != {"no_op": True, "runtime_required": False}:
        raise _fail("no-op receipt cumulative route is not an exact no-op")
    if not isinstance(incremental_manifest, Mapping):
        raise _fail("no-op receipt incremental manifest is malformed")
    manifest_dict = dict(manifest)
    document: dict[str, Any] = {
        "schema": 1,
        "kind": RECEIPT_KIND,
        "runner_sha": runner_sha,
        "mode": "baseline-reconcile",
        "production_deploy": {
            "run_id": production_deploy_run_id,
            "run_attempt": production_deploy_run_attempt,
        },
        "source_security": {
            "run_id": source_security_run_id,
            "run_attempt": source_security_run_attempt,
        },
        "autodeploy": {
            "run_id": autodeploy_run_id,
            "run_attempt": autodeploy_run_attempt,
        },
        "baseline_identity": baseline,
        "route": {
            "no_op": True,
            "runtime_required": False,
            "cumulative_manifest_sha256": hashlib.sha256(
                _canonical_json_bytes(manifest_dict)
            ).hexdigest(),
            "incremental_manifest": dict(incremental_manifest),
            "cumulative_manifest": manifest_dict,
        },
    }
    validate_noop_receipt_document(
        document,
        expected_runner_sha=runner_sha,
        expected_deploy_run_id=production_deploy_run_id,
        expected_deploy_attempt=production_deploy_run_attempt,
        expected_security_run_id=source_security_run_id,
        expected_security_attempt=source_security_run_attempt,
        expected_autodeploy_run_id=autodeploy_run_id,
        expected_autodeploy_attempt=autodeploy_run_attempt,
    )
    return document


def validate_noop_receipt_artifact(
    metadata: object,
    archive_bytes: bytes,
    *,
    expected_runner_sha: str,
    expected_deploy_run_id: str,
    expected_deploy_attempt: str,
    expected_security_run_id: str,
    expected_security_attempt: str,
    expected_autodeploy_run_id: str,
    expected_autodeploy_attempt: str,
    expected_artifact_id: str,
    expected_artifact_name: str,
) -> dict[str, Any]:
    """Verify the exact API artifact and its one-document ZIP payload."""

    if not isinstance(metadata, Mapping):
        raise _fail("no-op receipt artifact metadata is malformed")
    artifact_id = _positive_run_identity(expected_artifact_id, "artifact id")
    if type(metadata.get("id")) is not int or str(metadata["id"]) != artifact_id:
        raise _fail("no-op receipt artifact id does not match")
    if metadata.get("name") != expected_artifact_name:
        raise _fail("no-op receipt artifact name does not match")
    expected_name = f"platform-production-noop-source-receipt-{expected_deploy_run_id}-{expected_deploy_attempt}"
    if expected_artifact_name != expected_name:
        raise _fail("no-op receipt artifact name is not canonical")
    if metadata.get("expired") is not False:
        raise _fail("no-op receipt artifact is expired or expiry is unknown")
    size = metadata.get("size_in_bytes")
    if type(size) is not int or size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise _fail("no-op receipt artifact size is invalid")
    digest = metadata.get("digest")
    if not isinstance(digest, str) or SHA256_ARTIFACT_RE.fullmatch(digest) is None:
        raise _fail("no-op receipt artifact digest is malformed")
    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        raise _fail("no-op receipt artifact is missing its workflow run binding")
    if (
        type(workflow_run.get("id")) is not int
        or str(workflow_run["id"]) != expected_deploy_run_id
        or workflow_run.get("head_sha") != expected_runner_sha
        or workflow_run.get("head_branch") != "dev"
        or (
            "run_attempt" in workflow_run
            and (
                type(workflow_run["run_attempt"]) is not int
                or str(workflow_run["run_attempt"]) != expected_deploy_attempt
            )
        )
    ):
        raise _fail("no-op receipt artifact is not bound to the exact production attempt")
    if not isinstance(archive_bytes, bytes) or not archive_bytes or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise _fail("no-op receipt archive is missing or exceeds its bound")
    if hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:"):
        raise _fail("no-op receipt archive digest does not match API metadata")
    if type(size) is int and len(archive_bytes) != size:
        raise _fail("no-op receipt archive size does not match API metadata")

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != RECEIPT_FILE:
                raise _fail("no-op receipt archive does not contain one canonical member")
            entry = entries[0]
            mode = (entry.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or entry.is_dir() or entry.file_size > MAX_RECEIPT_BYTES:
                raise _fail("no-op receipt archive member metadata is unsafe")
            raw_document = _bounded_archive_member(archive, entry)
    except (
        EOFError,
        OSError,
        RuntimeError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise _fail("no-op receipt archive is malformed") from exc
    try:
        document = json.loads(
            raw_document.decode("ascii"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise _fail("no-op receipt document is malformed") from None
    if not isinstance(document, Mapping) or raw_document != _canonical_json_bytes(document) + b"\n":
        raise _fail("no-op receipt document encoding is not canonical")
    binding = validate_noop_receipt_document(
        document,
        expected_runner_sha=expected_runner_sha,
        expected_deploy_run_id=expected_deploy_run_id,
        expected_deploy_attempt=expected_deploy_attempt,
        expected_security_run_id=expected_security_run_id,
        expected_security_attempt=expected_security_attempt,
        expected_autodeploy_run_id=expected_autodeploy_run_id,
        expected_autodeploy_attempt=expected_autodeploy_attempt,
    )
    binding.update(
        {
            "receipt_artifact_id": artifact_id,
            "receipt_artifact_name": expected_artifact_name,
            "receipt_artifact_digest": digest,
            "receipt_archive_sha256": digest.removeprefix("sha256:"),
        }
    )
    return binding


def validate_active_source_binding(
    *,
    runner_sha: str,
    active_baseline: object,
    receipt_binding: object | None = None,
) -> dict[str, Any]:
    """Preserve same-source dispatch and require an exact no-op receipt otherwise."""

    if not isinstance(runner_sha, str) or SHA_RE.fullmatch(runner_sha) is None:
        raise _fail("runner source SHA is malformed")
    baseline = _validate_baseline_identity(active_baseline)
    if baseline["source_sha"] == runner_sha:
        if receipt_binding is not None:
            raise _fail("same-source dispatch must not substitute a no-op receipt")
        return {
            "schema": 1,
            "binding_mode": "same-source",
            "runner_sha": runner_sha,
            "app_target_sha": runner_sha,
            "baseline_identity": None,
            "receipt_document_sha256": None,
            "receipt_artifact_id": None,
            "receipt_artifact_name": None,
            "receipt_artifact_digest": None,
            "receipt_archive_sha256": None,
            "cumulative_manifest_sha256": None,
            "source_security_run_id": None,
            "source_security_run_attempt": None,
            "autodeploy_run_id": None,
            "autodeploy_run_attempt": None,
            "production_deploy_run_id": None,
            "production_deploy_run_attempt": None,
        }
    if not isinstance(receipt_binding, Mapping):
        raise _fail("runner and active app sources differ without a verified no-op receipt")
    if (
        receipt_binding.get("binding_mode") != "verified-noop"
        or receipt_binding.get("runner_sha") != runner_sha
        or receipt_binding.get("app_target_sha") != baseline["source_sha"]
        or receipt_binding.get("baseline_identity") != baseline
        or not isinstance(receipt_binding.get("receipt_document_sha256"), str)
        or HEX_DIGEST_RE.fullmatch(receipt_binding["receipt_document_sha256"]) is None
        or not isinstance(receipt_binding.get("receipt_artifact_id"), str)
        or RUN_ID_RE.fullmatch(receipt_binding["receipt_artifact_id"]) is None
        or not isinstance(receipt_binding.get("receipt_artifact_digest"), str)
        or SHA256_ARTIFACT_RE.fullmatch(receipt_binding["receipt_artifact_digest"]) is None
    ):
        raise _fail("verified no-op receipt is not bound to the current active release")
    return {
        "schema": 1,
        **dict(receipt_binding),
    }


def validate_source_binding_handoff(
    binding: object,
    *,
    expected_runner_sha: str,
    expected_app_target_sha: str,
    expected_security_run_id: str | None,
    expected_security_attempt: str | None,
    expected_autodeploy_run_id: str | None,
    expected_autodeploy_attempt: str | None,
    expected_deploy_run_id: str | None,
    expected_deploy_attempt: str | None,
    expected_artifact_id: str | None,
    expected_artifact_name: str | None,
    expected_artifact_digest: str | None,
) -> dict[str, Any]:
    """Validate the serializable binding carried through closed workflow input."""

    keys = {
        "schema",
        "binding_mode",
        "runner_sha",
        "app_target_sha",
        "baseline_identity",
        "receipt_document_sha256",
        "receipt_artifact_id",
        "receipt_artifact_name",
        "receipt_artifact_digest",
        "receipt_archive_sha256",
        "cumulative_manifest_sha256",
        "source_security_run_id",
        "source_security_run_attempt",
        "autodeploy_run_id",
        "autodeploy_run_attempt",
        "production_deploy_run_id",
        "production_deploy_run_attempt",
    }
    if not isinstance(binding, Mapping) or set(binding) != keys:
        raise _fail("source-binding handoff schema is invalid")
    if (
        type(binding.get("schema")) is not int
        or binding["schema"] != 1
        or binding.get("runner_sha") != expected_runner_sha
        or binding.get("app_target_sha") != expected_app_target_sha
        or not isinstance(expected_runner_sha, str)
        or SHA_RE.fullmatch(expected_runner_sha) is None
        or not isinstance(expected_app_target_sha, str)
        or SHA_RE.fullmatch(expected_app_target_sha) is None
    ):
        raise _fail("source-binding handoff SHA identity is invalid")
    mode = binding.get("binding_mode")
    if mode == "same-source":
        if expected_app_target_sha != expected_runner_sha or any(
            binding.get(field) is not None
            for field in keys - {"schema", "binding_mode", "runner_sha", "app_target_sha"}
        ) or any(
            value is not None
            for value in (
                expected_security_run_id,
                expected_security_attempt,
                expected_autodeploy_run_id,
                expected_autodeploy_attempt,
                expected_deploy_run_id,
                expected_deploy_attempt,
                expected_artifact_id,
                expected_artifact_name,
                expected_artifact_digest,
            )
        ):
            raise _fail("same-source binding contains a receipt substitution")
    elif mode == "verified-noop":
        baseline = _validate_baseline_identity(binding.get("baseline_identity"))
        if (
            expected_runner_sha == expected_app_target_sha
            or baseline["source_sha"] != expected_app_target_sha
        ):
            raise _fail("no-op source-binding baseline SHA is invalid")
        for field in ("receipt_document_sha256", "cumulative_manifest_sha256", "receipt_archive_sha256"):
            value = binding.get(field)
            if not isinstance(value, str) or HEX_DIGEST_RE.fullmatch(value) is None:
                raise _fail("no-op source-binding digest is invalid")
        artifact_id = binding.get("receipt_artifact_id")
        if not isinstance(artifact_id, str) or RUN_ID_RE.fullmatch(artifact_id) is None:
            raise _fail("no-op source-binding artifact id is invalid")
        artifact_name = binding.get("receipt_artifact_name")
        deploy_id = binding.get("production_deploy_run_id")
        deploy_attempt = binding.get("production_deploy_run_attempt")
        if (
            not isinstance(deploy_id, str)
            or not isinstance(deploy_attempt, str)
            or artifact_name != f"platform-production-noop-source-receipt-{deploy_id}-{deploy_attempt}"
        ):
            raise _fail("no-op source-binding artifact name is invalid")
        digest = binding.get("receipt_artifact_digest")
        if (
            not isinstance(digest, str)
            or SHA256_ARTIFACT_RE.fullmatch(digest) is None
            or digest.removeprefix("sha256:") != binding.get("receipt_archive_sha256")
        ):
            raise _fail("no-op source-binding artifact digest is invalid")
        for prefix in ("source_security", "autodeploy", "production_deploy"):
            run_id = _positive_run_identity(
                binding.get(f"{prefix}_run_id"), f"{prefix} run id"
            )
            attempt = _positive_run_identity(
                binding.get(f"{prefix}_run_attempt"), f"{prefix} run attempt"
            )
            expected_run_id = {
                "source_security": expected_security_run_id,
                "autodeploy": expected_autodeploy_run_id,
                "production_deploy": expected_deploy_run_id,
            }[prefix]
            expected_attempt = {
                "source_security": expected_security_attempt,
                "autodeploy": expected_autodeploy_attempt,
                "production_deploy": expected_deploy_attempt,
            }[prefix]
            _validate_expected_identity(
                run_id, expected_run_id, f"handoff {prefix} run id"
            )
            _validate_expected_identity(
                attempt, expected_attempt, f"handoff {prefix} attempt"
            )
        if (
            not isinstance(expected_artifact_id, str)
            or _positive_run_identity(
                binding.get("receipt_artifact_id"), "handoff artifact id"
            )
            != expected_artifact_id
            or binding.get("receipt_artifact_name") != expected_artifact_name
            or binding.get("receipt_artifact_digest") != expected_artifact_digest
        ):
            raise _fail("no-op source-binding artifact is not the authenticated artifact")
    else:
        raise _fail("source-binding mode is invalid")
    return dict(binding)


def validate_active_runtime_binding(
    binding: object,
    actual_baseline: object,
    *,
    expected_runner_sha: str,
    expected_app_target_sha: str,
    expected_security_run_id: str | None,
    expected_security_attempt: str | None,
    expected_autodeploy_run_id: str | None,
    expected_autodeploy_attempt: str | None,
    expected_deploy_run_id: str | None,
    expected_deploy_attempt: str | None,
    expected_artifact_id: str | None,
    expected_artifact_name: str | None,
    expected_artifact_digest: str | None,
) -> dict[str, Any]:
    """Rebind runner/app identity to the live release tuple under its lock."""

    if not isinstance(binding, Mapping):
        raise _fail("active source binding is malformed")
    parsed = validate_source_binding_handoff(
        binding,
        expected_runner_sha=expected_runner_sha,
        expected_app_target_sha=expected_app_target_sha,
        expected_security_run_id=expected_security_run_id,
        expected_security_attempt=expected_security_attempt,
        expected_autodeploy_run_id=expected_autodeploy_run_id,
        expected_autodeploy_attempt=expected_autodeploy_attempt,
        expected_deploy_run_id=expected_deploy_run_id,
        expected_deploy_attempt=expected_deploy_attempt,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_name=expected_artifact_name,
        expected_artifact_digest=expected_artifact_digest,
    )
    actual = _validate_baseline_identity(actual_baseline)
    if parsed["binding_mode"] == "same-source":
        if actual["source_sha"] != parsed["runner_sha"]:
            raise _fail("active release does not match the checked-out runner source")
        return {**parsed, "baseline_identity": actual}
    expected = _validate_baseline_identity(parsed.get("baseline_identity"))
    if actual != expected or actual["source_sha"] != parsed["app_target_sha"]:
        raise _fail("active release tuple changed after no-op receipt validation")
    return parsed


def validate_active_runtime_tuple(
    binding: object,
    actual_baseline: object,
    *,
    expected_runner_sha: str,
) -> dict[str, Any]:
    """Validate the no-op handoff against the live tuple under retained lock.

    Artifact API/ZIP provenance is verified on the workflow runner before the
    binding enters the closed handoff. The installed helper revalidates the
    complete typed binding and compares every active-release identity field
    while holding the already-acquired retained-load lock.
    """

    keys = {
        "schema",
        "binding_mode",
        "runner_sha",
        "app_target_sha",
        "baseline_identity",
        "receipt_document_sha256",
        "receipt_artifact_id",
        "receipt_artifact_name",
        "receipt_artifact_digest",
        "receipt_archive_sha256",
        "cumulative_manifest_sha256",
        "source_security_run_id",
        "source_security_run_attempt",
        "autodeploy_run_id",
        "autodeploy_run_attempt",
        "production_deploy_run_id",
        "production_deploy_run_attempt",
    }
    if not isinstance(binding, Mapping) or set(binding) != keys:
        raise _fail("active runtime source-binding schema is invalid")
    if (
        type(binding.get("schema")) is not int
        or binding["schema"] != 1
        or binding.get("binding_mode") != "verified-noop"
        or not isinstance(expected_runner_sha, str)
        or SHA_RE.fullmatch(expected_runner_sha) is None
        or binding.get("runner_sha") != expected_runner_sha
    ):
        raise _fail("active runtime runner identity is invalid")
    app_target_sha = binding.get("app_target_sha")
    if (
        not isinstance(app_target_sha, str)
        or SHA_RE.fullmatch(app_target_sha) is None
        or app_target_sha == expected_runner_sha
    ):
        raise _fail("active runtime app identity is invalid")
    expected_baseline = _validate_baseline_identity(binding.get("baseline_identity"))
    actual = _validate_baseline_identity(actual_baseline)
    if expected_baseline["source_sha"] != app_target_sha or actual != expected_baseline:
        raise _fail("active release tuple changed after no-op receipt validation")

    for field in (
        "receipt_document_sha256",
        "receipt_archive_sha256",
        "cumulative_manifest_sha256",
    ):
        value = binding.get(field)
        if not isinstance(value, str) or HEX_DIGEST_RE.fullmatch(value) is None:
            raise _fail("active runtime receipt digest is invalid")
    artifact_id = _positive_run_identity(binding.get("receipt_artifact_id"), "artifact id")
    deploy_id = _positive_run_identity(
        binding.get("production_deploy_run_id"), "production deploy run id"
    )
    deploy_attempt = _positive_run_identity(
        binding.get("production_deploy_run_attempt"), "production deploy attempt"
    )
    if binding.get("receipt_artifact_name") != (
        f"platform-production-noop-source-receipt-{deploy_id}-{deploy_attempt}"
    ):
        raise _fail("active runtime receipt artifact name is invalid")
    artifact_digest = binding.get("receipt_artifact_digest")
    if (
        not isinstance(artifact_digest, str)
        or SHA256_ARTIFACT_RE.fullmatch(artifact_digest) is None
        or artifact_digest.removeprefix("sha256:") != binding["receipt_archive_sha256"]
    ):
        raise _fail("active runtime receipt artifact digest is invalid")
    for prefix in ("source_security", "autodeploy", "production_deploy"):
        _positive_run_identity(binding.get(f"{prefix}_run_id"), f"{prefix} run id")
        _positive_run_identity(binding.get(f"{prefix}_run_attempt"), f"{prefix} attempt")
    return {**dict(binding), "baseline_identity": actual, "receipt_artifact_id": artifact_id}


def parse_source_binding_argument(value: object) -> dict[str, Any]:
    """Decode one bounded, canonical source-binding argv value."""

    encoded_maximum = 4 * ((MAX_SOURCE_BINDING_BYTES + 2) // 3)
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value
        or len(value) > encoded_maximum
    ):
        raise _fail("source-binding argument is malformed or oversized")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise _fail("source-binding argument is malformed") from None
    if not raw or len(raw) > MAX_SOURCE_BINDING_BYTES:
        raise _fail("source-binding document is oversized")
    try:
        binding = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise _fail("source-binding document is malformed") from None
    if not isinstance(binding, dict) or raw != _canonical_json_bytes(binding):
        raise _fail("source-binding document encoding is not canonical")
    return binding


def create_live_handoff(
    *,
    runner_sha: str,
    base_url: str,
    provision: str,
    marker: str,
    source_binding_path: str,
    output_path: str,
) -> dict[str, Any]:
    """Validate resolver output with the frozen C guard and write its handoff."""

    if not isinstance(runner_sha, str) or SHA_RE.fullmatch(runner_sha) is None:
        raise _fail("live handoff runner SHA is malformed")
    binding_path = Path(source_binding_path)
    if not binding_path.is_absolute():
        raise _fail("live source-binding path must be absolute")
    try:
        before = binding_path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_WORKFLOW_HANDOFF_BYTES
        ):
            raise _fail("live source-binding input metadata is unsafe")
        descriptor = os.open(
            binding_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
            ):
                raise _fail("live source-binding input changed while opening")
            raw = os.read(descriptor, MAX_WORKFLOW_HANDOFF_BYTES + 1)
            after = os.fstat(descriptor)
            if (
                len(raw) > MAX_WORKFLOW_HANDOFF_BYTES
                or (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise _fail("live source-binding input changed while reading")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _fail("live source-binding input is unavailable") from exc
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise _fail("live source-binding input terminator is invalid")
    encoded = raw[:-1]
    try:
        handoff = json.loads(
            encoded.decode("ascii"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail("live source-binding input is malformed") from exc
    if not isinstance(handoff, dict) or encoded != _canonical_json_bytes(handoff):
        raise _fail("live source-binding input is not canonical")
    if (
        set(handoff) != {"schema", "runner_sha", "app_target_sha", "source_binding"}
        or type(handoff.get("schema")) is not int
        or handoff.get("schema") != 1
        or handoff.get("runner_sha") != runner_sha
    ):
        raise _fail("live source-binding input schema is invalid")
    app_target_sha = handoff.get("app_target_sha")
    if not isinstance(app_target_sha, str) or SHA_RE.fullmatch(app_target_sha) is None:
        raise _fail("live source-binding app target is malformed")
    binding = handoff.get("source_binding")
    if binding is None:
        if app_target_sha != runner_sha:
            raise _fail("same-source live handoff identities differ")
    else:
        checked = validate_active_runtime_tuple(
            binding,
            binding.get("baseline_identity") if isinstance(binding, Mapping) else None,
            expected_runner_sha=runner_sha,
        )
        if checked.get("app_target_sha") != app_target_sha:
            raise _fail("live handoff app SHA differs from its verified receipt")
    payload: dict[str, Any] = {
        "schema": 1 if binding is None else 2,
        "base_url": base_url,
        "provision": provision,
        "marker": marker,
        "target_sha": runner_sha,
    }
    if binding is not None:
        payload["source_binding"] = binding
    try:
        from tools.platform_workflow_input_guard import (
            _write_private_json,
            validate_live_payload,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise _fail("frozen live input validator is unavailable") from exc
    validated = validate_live_payload(payload)
    _write_private_json(Path(output_path), validated)
    return {**validated, "app_target_sha": app_target_sha}


def validate_active_runtime_binding_argument(
    value: object, *, expected_runner_sha: str
) -> dict[str, Any]:
    """Read the live release tuple and validate one shell-provided binding."""

    binding = parse_source_binding_argument(value)
    return validate_active_runtime_tuple(
        binding,
        read_active_release_identity(),
        expected_runner_sha=expected_runner_sha,
    )


def main(argv: list[str] | None = None) -> int:
    """Expose fixed validation and workflow resolution operations."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 3 and arguments[0] == "validate-active-runtime-binding":
        runner_sha, encoded = arguments[1:]
        try:
            validated = validate_active_runtime_binding_argument(
                encoded,
                expected_runner_sha=runner_sha,
            )
        except (ProvenanceError, OSError, RuntimeError, ValueError, TypeError):
            print("ACTIVE_RUNTIME_BINDING status=failed", file=sys.stderr)
            return 2
        print(
            "ACTIVE_RUNTIME_BINDING schema=1 status=matched "
            f"runner_sha={validated['runner_sha']} app_target_sha={validated['app_target_sha']}"
        )
        return 0
    if len(arguments) == 6 and arguments[0] == "resolve-workflow-source-binding":
        _, runner_sha, repository, api_base, output_dir, github_output = arguments
        # Keep this interface exact: additional targets or caller-selected
        # artifact names would turn a provenance resolver into a target API.
        try:
            handoff = resolve_workflow_source_binding(
                runner_sha=runner_sha,
                repository=repository,
                token=os.environ.get("GH_TOKEN", ""),
                api_base=api_base,
            )
            root = Path(output_dir)
            if not root.is_absolute():
                raise _fail("source-binding output directory must be absolute")
            root.mkdir(mode=0o700, parents=False, exist_ok=False)
            root_metadata = root.lstat()
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_ISLNK(root_metadata.st_mode)
                or root_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise _fail("source-binding output directory is unsafe")
            output_path = root / "source-binding.json"
            raw = _canonical_json_bytes(handoff) + b"\n"
            descriptor = os.open(
                output_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise _fail("source-binding handoff file is unsafe")
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            output_path_github = Path(github_output)
            output_metadata = output_path_github.lstat()
            if (
                not stat.S_ISREG(output_metadata.st_mode)
                or stat.S_ISLNK(output_metadata.st_mode)
                or output_metadata.st_uid != os.geteuid()
                or output_metadata.st_nlink != 1
                or output_metadata.st_size > 1024 * 1024
                or stat.S_IMODE(output_metadata.st_mode) & 0o022
            ):
                raise _fail("workflow output file is unsafe")
            output_fd = os.open(
                output_path_github,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                source_binding = handoff.get("source_binding")
                binding_digest = (
                    hashlib.sha256(_canonical_json_bytes(source_binding)).hexdigest()
                    if isinstance(source_binding, dict)
                    else ""
                )
                output = (
                    f"app_target_sha={handoff['app_target_sha']}\n"
                    f"source_binding_sha256={binding_digest}\n"
                ).encode("ascii")
                os.write(output_fd, output)
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
        except (OSError, ProvenanceError, RuntimeError, ValueError, TypeError):
            print("SOURCE_BINDING_RESOLUTION status=failed", file=sys.stderr)
            return 1
        print("SOURCE_BINDING_RESOLUTION status=verified")
        return 0
    if len(arguments) == 7 and arguments[0] == "create-live-handoff":
        _, runner_sha, base_url, provision, marker, binding_path, output_path = arguments
        try:
            create_live_handoff(
                runner_sha=runner_sha,
                base_url=base_url,
                provision=provision,
                marker=marker,
                source_binding_path=binding_path,
                output_path=output_path,
            )
        except (OSError, ProvenanceError, RuntimeError, ValueError, TypeError):
            print("LIVE_HANDOFF status=failed", file=sys.stderr)
            return 1
        print("LIVE_HANDOFF status=created")
        return 0
    print("SOURCE_BINDING_OPERATION status=invalid", file=sys.stderr)
    return 2


def read_active_release_identity() -> dict[str, object]:
    """Read the tuple using the existing active-release reader, without C2 CLI."""

    try:
        from tools.platform_workflow_remote_dispatch import _release_baseline

        result = _release_baseline()
    except (ImportError, OSError, RuntimeError) as exc:
        raise _fail("active release tuple is unavailable") from exc
    return _validate_baseline_identity(result)


def resolve_workflow_source_binding(
    *,
    runner_sha: str,
    repository: str,
    token: str,
    api_base: str = "https://api.github.com",
    opener: Any = urllib.request,
) -> dict[str, Any]:
    """Resolve one exact deploy receipt using authenticated GitHub API records.

    No artifact means the ordinary same-source path. If a receipt is present,
    every run/artifact identity is revalidated before its app SHA can be used.
    """

    if not isinstance(runner_sha, str) or SHA_RE.fullmatch(runner_sha) is None:
        raise _fail("runner source SHA is malformed")
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or not isinstance(token, str)
        or not token
        or not isinstance(api_base, str)
        or not api_base.startswith("https://")
        or api_base.endswith("/")
    ):
        raise _fail("source-binding API identity is malformed")
    api_root = f"{api_base}/repos/{repository}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "OldSparky-source-binding-resolver",
    }
    if callable(getattr(opener, "build_opener", None)):
        api_opener = opener.build_opener(_SameOriginAPIRedirect())
        artifact_opener = opener.build_opener(_SafeArtifactRedirect())
    else:
        # The injectable test transport can model redirects itself; production
        # uses the two explicit urllib openers above.
        api_opener = artifact_opener = opener
    open_api_request = getattr(api_opener, "open", None)
    if not callable(open_api_request):
        open_api_request = api_opener.urlopen
    open_artifact_request = getattr(artifact_opener, "open", None)
    if not callable(open_artifact_request):
        open_artifact_request = artifact_opener.urlopen

    def api_json(path: str) -> dict[str, Any]:
        request = urllib.request.Request(api_root + path, headers=headers)
        try:
            with open_api_request(request, timeout=30) as response:
                if response.status != 200:
                    raise ValueError("status")
                raw = response.read(8 * 1024 * 1024 + 1)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            raise _fail("source-binding API request failed") from exc
        if len(raw) > 8 * 1024 * 1024:
            raise _fail("source-binding API response exceeds its bound")
        try:
            parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise _fail("source-binding API response is malformed") from exc
        if not isinstance(parsed, dict):
            raise _fail("source-binding API response is malformed")
        return parsed

    def paged(path: str, key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        expected_total: int | None = None
        for page in range(1, 6):
            separator = "&" if "?" in path else "?"
            result = api_json(f"{path}{separator}per_page=100&page={page}")
            values = result.get(key)
            total = result.get("total_count")
            if (
                not isinstance(values, list)
                or type(total) is not int
                or total < 0
                or (expected_total is not None and total != expected_total)
                or any(not isinstance(item, dict) for item in values)
            ):
                raise _fail("source-binding API listing is malformed")
            expected_total = total
            rows.extend(values)
            if len(rows) >= total:
                if len(rows) != total:
                    raise _fail("source-binding API listing changed during pagination")
                return rows
        raise _fail("source-binding API listing exceeds its bound")

    def positive_identity(value: object, field: str) -> str:
        if type(value) is not int or value <= 0 or value > 2**63 - 1:
            raise _fail(f"source-binding {field} is malformed")
        return str(value)

    workflow_specs = (
        (".github/workflows/platform-production-deploy.yml", "deploy"),
        (".github/workflows/platform-security.yml", "security"),
        (".github/workflows/platform-production-autodeploy.yml", "autodeploy"),
    )
    workflow_ids: dict[str, int] = {}
    for expected_path, key in workflow_specs:
        workflow = api_json(f"/actions/workflows/{Path(expected_path).name}")
        workflow_id = workflow.get("id")
        if (
            workflow.get("path") != expected_path
            or type(workflow_id) is not int
            or workflow_id <= 0
            or workflow.get("state") != "active"
        ):
            raise _fail(f"source-binding {key} workflow identity is invalid")
        workflow_ids[key] = workflow_id

    query = urllib.parse.urlencode(
        {
            "branch": "dev",
            "event": "workflow_dispatch",
            "head_sha": runner_sha,
            "status": "completed",
        }
    )
    deploy_runs = paged(
        f"/actions/workflows/platform-production-deploy.yml/runs?{query}",
        "workflow_runs",
    )
    artifact_candidates: list[tuple[dict[str, Any], str, str, str, dict[str, Any]]] = []
    for run in deploy_runs:
        if (
            type(run.get("workflow_id")) is not int
            or run.get("workflow_id") != workflow_ids["deploy"]
            or run.get("head_sha") != runner_sha
            or run.get("head_branch") != "dev"
            or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
        ):
            continue
        deploy_id = positive_identity(run.get("id"), "deploy run id")
        deploy_attempt = positive_identity(run.get("run_attempt"), "deploy run attempt")
        artifact_name = f"platform-production-noop-source-receipt-{deploy_id}-{deploy_attempt}"
        for artifact in paged(f"/actions/runs/{deploy_id}/artifacts", "artifacts"):
            if artifact.get("name") == artifact_name:
                artifact_candidates.append(
                    (run, deploy_id, deploy_attempt, artifact_name, artifact)
                )
    if len(artifact_candidates) > 1:
        raise _fail("multiple exact no-op receipt artifacts match this source")
    if not artifact_candidates:
        return {
            "schema": 1,
            "runner_sha": runner_sha,
            "app_target_sha": runner_sha,
            "source_binding": None,
        }

    deploy_run, deploy_id, deploy_attempt, artifact_name, artifact_row = artifact_candidates[0]
    artifact_id = positive_identity(artifact_row.get("id"), "deploy receipt artifact id")
    artifact_metadata = api_json(f"/actions/artifacts/{artifact_id}")
    request = urllib.request.Request(f"{api_root}/actions/artifacts/{artifact_id}/zip", headers=headers)
    try:
        with open_artifact_request(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError("status")
            archive_bytes = response.read(MAX_ARCHIVE_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise _fail("no-op receipt archive could not be downloaded") from exc
    if not archive_bytes or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise _fail("no-op receipt archive exceeds its bound")

    def exact_source_run(section: str, key: str, expected_event: str) -> tuple[str, str]:
        row = None
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                entries = archive.infolist()
                if len(entries) != 1 or entries[0].filename != RECEIPT_FILE:
                    raise _fail("no-op receipt archive members are invalid")
                raw = _bounded_archive_member(archive, entries[0])
                document = parse_receipt_json(raw)
        except (OSError, EOFError, ValueError, zipfile.BadZipFile, ProvenanceError) as exc:
            raise _fail("no-op receipt archive is malformed") from exc
        row = document.get(section)
        if not isinstance(row, dict) or set(row) != {"run_id", "run_attempt"}:
            raise _fail("no-op receipt upstream identity is malformed")
        source_id = row.get("run_id")
        source_attempt = row.get("run_attempt")
        if (
            not isinstance(source_id, str)
            or RUN_ID_RE.fullmatch(source_id) is None
            or not isinstance(source_attempt, str)
            or RUN_ID_RE.fullmatch(source_attempt) is None
        ):
            raise _fail("no-op receipt upstream identity is malformed")
        run = api_json(f"/actions/runs/{source_id}")
        if (
            type(run.get("id")) is not int
            or str(run["id"]) != source_id
            or type(run.get("run_attempt")) is not int
            or str(run["run_attempt"]) != source_attempt
            or type(run.get("workflow_id")) is not int
            or run.get("workflow_id") != workflow_ids[key]
            or run.get("head_sha") != runner_sha
            or run.get("head_branch") != "dev"
            or run.get("event") != expected_event
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
        ):
            raise _fail("no-op receipt upstream run is not exact and successful")
        return source_id, source_attempt

    # Parse once before any API identity checks; validate the same bounded ZIP
    # again through the canonical artifact validator below.
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != RECEIPT_FILE:
                raise _fail("no-op receipt archive members are invalid")
            parse_receipt_json(_bounded_archive_member(archive, entries[0]))
    except (OSError, EOFError, ValueError, zipfile.BadZipFile, ProvenanceError) as exc:
        raise _fail("no-op receipt archive is malformed") from exc
    security_id, security_attempt = exact_source_run(
        "source_security", "security", "push"
    )
    autodeploy_id, autodeploy_attempt = exact_source_run(
        "autodeploy", "autodeploy", "workflow_run"
    )
    binding = validate_noop_receipt_artifact(
        artifact_metadata,
        archive_bytes,
        expected_runner_sha=runner_sha,
        expected_deploy_run_id=deploy_id,
        expected_deploy_attempt=deploy_attempt,
        expected_security_run_id=security_id,
        expected_security_attempt=security_attempt,
        expected_autodeploy_run_id=autodeploy_id,
        expected_autodeploy_attempt=autodeploy_attempt,
        expected_artifact_id=artifact_id,
        expected_artifact_name=artifact_name,
    )
    if (
        type(deploy_run.get("id")) is not int
        or str(deploy_run["id"]) != deploy_id
        or type(deploy_run.get("run_attempt")) is not int
        or str(deploy_run["run_attempt"]) != deploy_attempt
    ):
        raise _fail("no-op receipt deploy run identity changed")
    binding = validate_active_source_binding(
        runner_sha=runner_sha,
        active_baseline=binding["baseline_identity"],
        receipt_binding=binding,
    )
    binding = validate_source_binding_handoff(
        binding,
        expected_runner_sha=runner_sha,
        expected_app_target_sha=binding["app_target_sha"],
        expected_security_run_id=security_id,
        expected_security_attempt=security_attempt,
        expected_autodeploy_run_id=autodeploy_id,
        expected_autodeploy_attempt=autodeploy_attempt,
        expected_deploy_run_id=deploy_id,
        expected_deploy_attempt=deploy_attempt,
        expected_artifact_id=artifact_id,
        expected_artifact_name=artifact_name,
        expected_artifact_digest=binding["receipt_artifact_digest"],
    )
    return {
        "schema": 1,
        "runner_sha": runner_sha,
        "app_target_sha": binding["app_target_sha"],
        "source_binding": binding,
    }


def parse_receipt_json(raw: bytes) -> dict[str, Any]:
    """Parse one bounded receipt document without accepting duplicate keys."""

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RECEIPT_BYTES:
        raise _fail("no-op receipt document is missing or oversized")
    try:
        document = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise _fail("no-op receipt document is malformed") from None
    if not isinstance(document, dict) or raw != _canonical_json_bytes(document) + b"\n":
        raise _fail("no-op receipt document encoding is not canonical")
    return document


def write_receipt_artifact_file(path: Path, document: Mapping[str, Any]) -> str:
    """Create the one private, canonical receipt file and return its digest."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise _fail("no-op receipt artifact path must be absolute")
    raw = _canonical_json_bytes(document) + b"\n"
    if len(raw) > MAX_RECEIPT_BYTES:
        raise _fail("no-op receipt document exceeds its bound")
    try:
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise _fail("no-op receipt artifact directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise _fail("no-op receipt artifact directory metadata is unsafe")
    required_flags = ("O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required_flags):
        raise _fail("no-op receipt exclusive file flags are unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _fail("no-op receipt file could not be created exclusively") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise _fail("no-op receipt file metadata is unsafe")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
