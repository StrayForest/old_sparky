#!/usr/bin/env python3
"""Validate the data-only classifier artifact at the production boundary.

This tool is executed only from an immutable trusted ``dev`` checkout.  It
is deliberately independent of the deployment candidate: its inputs are
bounded GitHub API JSON snapshots and a bounded classifier ZIP.  No input is
ever evaluated as Python source, and failures expose only a fixed reason.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping, Sequence
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from types import ModuleType
from typing import Any

if __package__:
    from .platform_safe_zip import UnsafeZipError, extract_single_manifest
    from .platform_ci_classifier import (
        CANDIDATE_PACKAGING_FILES,
        CANDIDATE_PACKAGING_REASON,
        STORAGE_OPERATIONS_REASON,
        _is_storage_operations_only,
    )
else:  # Executed as a script under Python isolated mode on a runner.
    _TRUSTED_TOOLS_PACKAGE = "_oldsparky_trusted_platform_tools"
    _TRUSTED_TOOLS_DIR = str(Path(__file__).resolve().parent)
    _TRUSTED_TOOLS_MODULE = ModuleType(_TRUSTED_TOOLS_PACKAGE)
    _TRUSTED_TOOLS_MODULE.__package__ = _TRUSTED_TOOLS_PACKAGE
    _TRUSTED_TOOLS_MODULE.__path__ = [_TRUSTED_TOOLS_DIR]
    sys.modules[_TRUSTED_TOOLS_PACKAGE] = _TRUSTED_TOOLS_MODULE

    _SAFE_ZIP_MODULE = import_module(
        f"{_TRUSTED_TOOLS_PACKAGE}.platform_safe_zip"
    )
    UnsafeZipError = _SAFE_ZIP_MODULE.UnsafeZipError
    extract_single_manifest = _SAFE_ZIP_MODULE.extract_single_manifest
    _CI_CLASSIFIER_MODULE = import_module(
        f"{_TRUSTED_TOOLS_PACKAGE}.platform_ci_classifier"
    )
    CANDIDATE_PACKAGING_FILES = _CI_CLASSIFIER_MODULE.CANDIDATE_PACKAGING_FILES
    CANDIDATE_PACKAGING_REASON = _CI_CLASSIFIER_MODULE.CANDIDATE_PACKAGING_REASON
    STORAGE_OPERATIONS_REASON = _CI_CLASSIFIER_MODULE.STORAGE_OPERATIONS_REASON
    _is_storage_operations_only = _CI_CLASSIFIER_MODULE._is_storage_operations_only


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARTIFACT_ROWS = 10_000
MAX_PAGE_ROWS = 100
MAX_PAGES = 100
EXPECTED_MANIFEST_GATES = [
    "backend",
    "python-quality",
    "security",
    "migration",
    "docs",
    "web-quality",
    "web-hermetic",
    "verification-contract",
]
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
PAGE_RE = re.compile(r"page-([1-9][0-9]{0,2})\.json\Z")
EXPECTED_NAME_RE = re.compile(
    r"platform-ci-route-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}\Z"
)
RECOVERY_BOOTSTRAP_FILES = frozenset(
    {
        ".github/workflows/platform-production-recovery-bootstrap-build.yml",
        ".github/workflows/platform-production-recovery-bootstrap-publish.yml",
        ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
        ".github/workflows/platform-production-deploy.yml",
        ".github/workflows/platform-production-release-abort.yml",
        ".github/workflows/platform-production-release-recover.yml",
        ".github/workflows/platform-production-autodeploy.yml",
        "platform/tools/platform_recovery_bootstrap.py",
        "platform/tools/platform_tournament_list_read_model_recovery.py",
        "platform/alembic/env.py",
        "platform/tools/platform_abort_retained_only.sh",
        "platform/tools/platform_release_lock.sh",
        "platform/tools/platform_release_deploy.sh",
        "platform/tools/platform_release_preflight.sh",
        "platform/tools/platform_install_systemd_units.sh",
        "platform/tools/platform_install_logging.sh",
        "platform/tools/platform_install_nginx.py",
        "platform/tools/platform_release_restore_runtime.sh",
        "platform/tools/platform_update_cloudflare_ips.py",
        "platform/tools/platform_deploy_smoke_impl.py",
        "platform/tools/platform_validate_edge_policy.py",
        "platform/tools/platform_health_monitor.py",
        "platform/tools/platform_release_rollback.sh",
        "platform/tools/platform_release_systemd_state.py",
        "platform/tools/platform_recover_pending.sh",
        "platform/tools/platform_release_transaction.py",
        "platform/tools/platform_run_alembic.sh",
        "platform/tools/platform_live_qa_guard.py",
        "platform/tools/platform_build_live_qa_runtime.py",
        "platform/tests/test_platform_recovery_bootstrap.py",
        "platform/tests/test_platform_db.py",
        "platform/tests/test_platform_release_audit_hardening.py",
        "platform/tests/test_platform_release_build_contract.py",
        "platform/tests/test_platform_release_recovery_boundaries.py",
        "platform/tests/test_platform_storage_maintenance.py",
        "platform/tests/test_platform_release_systemd_state.py",
        "platform/tests/test_platform_release_venv_rollback.py",
        "platform/tests/test_platform_live_qa_guard.py",
        "platform/tests/test_platform_live_qa_runtime_install.py",
        "platform/tests/test_platform_live_qa_wrappers.py",
        "platform/tests/test_platform_ssh_host_key_scan.py",
        "platform/tests/test_platform_release_build_diagnostics.py",
        "platform/tests/test_platform_ci_classifier.py",
        "platform/tools/platform_ci_classifier.py",
        "platform/tools/platform_production_classifier_artifact.py",
        "platform/tools/platform_deploy_baseline.py",
        "platform/tools/platform_baseline_runtime_proof.py",
        "platform/tools/platform_workflow_provenance.py",
        "platform/tools/platform_verify_contract.py",
        "platform/tools/platform_test_catalog.py",
        "platform/tools/platform_workflow_input_guard.py",
        "platform/tools/platform_host_tools_bundle.py",
        "platform/tools/platform_validate_release_artifact.py",
        "platform/tests/test_platform_host_tools_bundle.py",
        "platform/tests/test_platform_workflow_provenance.py",
        "platform/tests/test_platform_recovery_workflow_caller.py",
        "platform/tests/test_platform_cloudflare_ips.py",
        "platform/python_packages/platform_infra/db.py",
        "platform/tests/test_platform_install_nginx.py",
        "platform/contracts/host_tools_pin.json",
        "platform/deploy/systemd/deadlock-cloudflare-ips.service",
        "platform/deploy/systemd/deadlock-health-monitor.service",
    }
)
DOCS_PREFIX = "platform/docs/"
RECOVERY_BOOTSTRAP_REASON = (
    "retained-release recovery-bootstrap change requires full verification and is non-deployable"
)
ALLOWED_REASONS = frozenset(
    {
        "metadata",
        "duplicate_keys",
        "encoding",
        "json",
        "oversized",
        "identity",
        "schema",
        "provenance",
        "pagination",
        "archive",
        "manifest",
    }
)


class ClassifierArtifactError(ValueError):
    """A bounded, non-sensitive classifier validation failure."""

    def __init__(self, reason: str) -> None:
        if reason not in ALLOWED_REASONS:
            reason = "schema"
        super().__init__(reason)
        self.reason = reason


class _DuplicateKeyError(ValueError):
    pass


def _fail(reason: str) -> ClassifierArtifactError:
    return ClassifierArtifactError(reason)


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _safe_read_json(path: Path, *, limit: int) -> object:
    """Read one mode-0600 regular file without following a symlink."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise _fail("metadata")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    except OSError as exc:
        raise _fail("metadata") from exc
    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise _fail("metadata") from exc
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 0
            or before.st_size > limit
        ):
            raise _fail("oversized" if before.st_size > limit else "metadata")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            try:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
            except OSError as exc:
                raise _fail("metadata") from exc
            if not chunk:
                raise _fail("identity")
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _fail("metadata") from exc
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != identity:
            raise _fail("identity")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("encoding") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyError as exc:
        raise _fail("duplicate_keys") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _fail("json") from exc


def _mapping(value: object, reason: str = "schema") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(reason)
    return value


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _canonical_decimal(value: str) -> bool:
    return RUN_ID_RE.fullmatch(value) is not None


def _validate_artifact_row(row: object, *, target_sha: str) -> None:
    payload = _mapping(row)
    if not _positive_int(payload.get("id")):
        raise _fail("schema")
    name = payload.get("name")
    if not isinstance(name, str) or not name.isascii() or not 1 <= len(name) <= 256:
        raise _fail("schema")
    if type(payload.get("expired")) is not bool:
        raise _fail("schema")
    workflow_run = _mapping(payload.get("workflow_run"), "provenance")
    if not _positive_int(workflow_run.get("id")):
        raise _fail("provenance")
    head_sha = workflow_run.get("head_sha")
    if not isinstance(head_sha, str) or SHA_RE.fullmatch(head_sha) is None:
        raise _fail("provenance")
    if workflow_run.get("head_branch") != "dev":
        raise _fail("provenance")
    if "run_attempt" in workflow_run and not _positive_int(workflow_run["run_attempt"]):
        raise _fail("provenance")
    if "workflow_id" in workflow_run and not _positive_int(workflow_run["workflow_id"]):
        raise _fail("provenance")
    if head_sha != target_sha:
        raise _fail("provenance")


def _page_paths(pages_dir: Path) -> list[Path]:
    try:
        metadata = pages_dir.lstat()
    except OSError as exc:
        raise _fail("metadata") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise _fail("metadata")
    try:
        candidates = sorted(
            (
                (int(match.group(1)), path)
                for path in pages_dir.iterdir()
                if (match := PAGE_RE.fullmatch(path.name)) is not None
            ),
            key=lambda item: item[0],
        )
    except OSError as exc:
        raise _fail("metadata") from exc
    if not candidates or len(candidates) > MAX_PAGES:
        raise _fail("pagination")
    if [number for number, _path in candidates] != list(range(1, len(candidates) + 1)):
        raise _fail("pagination")
    return [path for _number, path in candidates]


def _load_snapshot(pages_dir: Path, *, target_sha: str) -> list[dict[str, Any]]:
    paths = _page_paths(pages_dir)
    expected_total: int | None = None
    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, path in enumerate(paths):
        payload = _mapping(_safe_read_json(path, limit=MAX_JSON_BYTES))
        if set(payload) != {"total_count", "artifacts"}:
            raise _fail("schema")
        total = payload.get("total_count")
        if type(total) is not int or not 0 <= total <= MAX_ARTIFACT_ROWS:
            raise _fail("schema")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise _fail("pagination")
        page_rows = payload.get("artifacts")
        if not isinstance(page_rows, list) or len(page_rows) > MAX_PAGE_ROWS:
            raise _fail("schema")
        if index < len(paths) - 1 and len(page_rows) != MAX_PAGE_ROWS:
            raise _fail("pagination")
        for row in page_rows:
            _validate_artifact_row(row, target_sha=target_sha)
            row_mapping = dict(_mapping(row))
            row_id = row_mapping["id"]
            if row_id in seen_ids:
                raise _fail("pagination")
            seen_ids.add(row_id)
            rows.append(row_mapping)
    if expected_total is None or len(rows) != expected_total:
        raise _fail("pagination")
    return rows


def _artifact_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    workflow_run = _mapping(row.get("workflow_run"), "provenance")
    return (
        row.get("id"),
        row.get("name"),
        row.get("expired"),
        workflow_run.get("id"),
        workflow_run.get("head_branch"),
        workflow_run.get("head_sha"),
        workflow_run.get("run_attempt"),
    )


def validate_metadata(
    first_pages: Path,
    second_pages: Path,
    *,
    expected_name: str,
    run_id: str,
    run_attempt: str,
    target_sha: str,
) -> int:
    if (
        EXPECTED_NAME_RE.fullmatch(expected_name) is None
        or not _canonical_decimal(run_id)
        or not _canonical_decimal(run_attempt)
        or SHA_RE.fullmatch(target_sha) is None
    ):
        raise _fail("provenance")
    first = _load_snapshot(first_pages, target_sha=target_sha)
    second = _load_snapshot(second_pages, target_sha=target_sha)
    if sorted(map(_artifact_identity, first)) != sorted(map(_artifact_identity, second)):
        raise _fail("provenance")
    matches = [
        row
        for row in second
        if row.get("name") == expected_name and row.get("expired") is False
    ]
    if len(matches) != 1:
        raise _fail("provenance")
    selected = matches[0]
    workflow_run = _mapping(selected.get("workflow_run"), "provenance")
    if workflow_run.get("id") != int(run_id) or workflow_run.get("head_sha") != target_sha:
        raise _fail("provenance")
    if "run_attempt" in workflow_run and workflow_run.get("run_attempt") != int(run_attempt):
        raise _fail("provenance")
    return int(selected["id"])


def _bounded_ascii(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    fields = (
        "schema",
        "version",
        "target_sha",
        "event",
        "class",
        "expected_gates",
        "runtime_sensitive",
        "deployable",
        "fallback",
        "reason",
        "files",
    )
    return hashlib.sha256(
        json.dumps(
            {field: manifest[field] for field in fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _is_recovery_bootstrap_only(files: Sequence[object]) -> bool:
    return bool(
        any(path in RECOVERY_BOOTSTRAP_FILES for path in files)
        and all(
            path in RECOVERY_BOOTSTRAP_FILES
            or (isinstance(path, str) and path.startswith(DOCS_PREFIX))
            for path in files
        )
    )


def _is_candidate_packaging_only(files: Sequence[object]) -> bool:
    return bool(
        any(path in CANDIDATE_PACKAGING_FILES for path in files)
        and all(
            path in CANDIDATE_PACKAGING_FILES
            or (isinstance(path, str) and path.startswith(DOCS_PREFIX))
            for path in files
        )
    )


def validate_manifest(
    archive: Path,
    *,
    target_sha: str,
    require_recovery_bootstrap: bool = False,
    require_reconcile_source: bool = False,
    require_runtime_sensitive: bool = False,
) -> dict[str, Any]:
    if SHA_RE.fullmatch(target_sha) is None:
        raise _fail("provenance")
    with tempfile.TemporaryDirectory(prefix="platform-classifier-") as temporary:
        destination = Path(temporary) / "manifest"
        try:
            manifest_path = extract_single_manifest(archive, destination)
        except UnsafeZipError as exc:
            raise _fail("archive") from exc
        manifest = _mapping(
            _safe_read_json(manifest_path, limit=MAX_MANIFEST_BYTES),
            "manifest",
        )
    fields = {
        "schema",
        "version",
        "target_sha",
        "event",
        "class",
        "expected_gates",
        "runtime_sensitive",
        "deployable",
        "fallback",
        "reason",
        "files",
        "digest",
    }
    if set(manifest) != fields:
        raise _fail("manifest")
    if type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise _fail("manifest")
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise _fail("manifest")
    if manifest.get("target_sha") != target_sha:
        raise _fail("provenance")
    if manifest.get("event") != "push" or manifest.get("class") != "full":
        raise _fail("manifest")
    if manifest.get("expected_gates") != EXPECTED_MANIFEST_GATES:
        raise _fail("manifest")
    if type(manifest.get("runtime_sensitive")) is not bool:
        raise _fail("manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 10_000 or any(
        not _bounded_ascii(value, maximum=512) for value in files
    ):
        raise _fail("manifest")
    recovery_bootstrap_only = _is_recovery_bootstrap_only(files)
    candidate_packaging_only = _is_candidate_packaging_only(files)
    storage_operations_only = _is_storage_operations_only(files)
    if type(require_recovery_bootstrap) is not bool:
        raise _fail("manifest")
    if type(require_reconcile_source) is not bool or (
        require_recovery_bootstrap and require_reconcile_source
    ):
        raise _fail("manifest")
    if type(require_runtime_sensitive) is not bool:
        raise _fail("manifest")
    if require_runtime_sensitive and manifest.get("runtime_sensitive") is not True:
        raise _fail("manifest")
    if require_recovery_bootstrap and (
        not recovery_bootstrap_only
        or manifest.get("reason") != RECOVERY_BOOTSTRAP_REASON
        or manifest.get("class") != "full"
        or manifest.get("deployable") is not False
    ):
        raise _fail("manifest")
    if type(manifest.get("deployable")) is not bool:
        raise _fail("manifest")
    if recovery_bootstrap_only:
        if (
            manifest["deployable"] is not False
            or manifest.get("reason") != RECOVERY_BOOTSTRAP_REASON
        ):
            raise _fail("manifest")
    elif candidate_packaging_only:
        if (
            not require_reconcile_source
            or manifest["runtime_sensitive"] is not False
            or manifest["deployable"] is not False
            or manifest.get("reason") != CANDIDATE_PACKAGING_REASON
        ):
            raise _fail("manifest")
    elif storage_operations_only:
        if (
            manifest["deployable"] is not False
            or manifest["runtime_sensitive"] is not True
            or manifest.get("reason") != STORAGE_OPERATIONS_REASON
        ):
            raise _fail("manifest")
    elif manifest["deployable"] is not True:
        raise _fail("manifest")
    if manifest.get("reason") == STORAGE_OPERATIONS_REASON and not storage_operations_only:
        raise _fail("manifest")
    if require_reconcile_source and not (recovery_bootstrap_only or candidate_packaging_only):
        raise _fail("manifest")
    if type(manifest.get("fallback")) is not bool or manifest["fallback"] is not False:
        raise _fail("manifest")
    if not _bounded_ascii(manifest.get("reason"), maximum=512):
        raise _fail("manifest")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise _fail("manifest")
    if digest != _manifest_digest(manifest):
        raise _fail("manifest")
    return dict(manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    page = subparsers.add_parser("page")
    page.add_argument("path", type=Path)
    metadata = subparsers.add_parser("metadata")
    metadata.add_argument("first_pages", type=Path)
    metadata.add_argument("second_pages", type=Path)
    metadata.add_argument("--expected-name", required=True)
    metadata.add_argument("--run-id", required=True)
    metadata.add_argument("--run-attempt", required=True)
    metadata.add_argument("--target-sha", required=True)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("archive", type=Path)
    manifest.add_argument("--target-sha", required=True)
    manifest.add_argument("--require-recovery-bootstrap", action="store_true")
    manifest.add_argument("--require-reconcile-source", action="store_true")
    manifest.add_argument("--require-runtime-sensitive", action="store_true")
    manifest.add_argument("--emit-manifest-base64", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "page":
            payload = _mapping(_safe_read_json(arguments.path, limit=MAX_JSON_BYTES))
            if set(payload) != {"total_count", "artifacts"}:
                raise _fail("schema")
            total = payload.get("total_count")
            rows = payload.get("artifacts")
            if type(total) is not int or not 0 <= total <= MAX_ARTIFACT_ROWS:
                raise _fail("schema")
            if not isinstance(rows, list) or len(rows) > MAX_PAGE_ROWS:
                raise _fail("schema")
            for row in rows:
                _validate_artifact_row(row, target_sha=_mapping(row.get("workflow_run"), "provenance").get("head_sha", ""))
            print(f"count={len(rows)} total={total}")
        elif arguments.command == "metadata":
            artifact_id = validate_metadata(
                arguments.first_pages,
                arguments.second_pages,
                expected_name=arguments.expected_name,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                target_sha=arguments.target_sha,
            )
            print(artifact_id)
        else:
            validated_manifest = validate_manifest(
                arguments.archive,
                target_sha=arguments.target_sha,
                require_recovery_bootstrap=arguments.require_recovery_bootstrap,
                require_reconcile_source=arguments.require_reconcile_source,
                require_runtime_sensitive=arguments.require_runtime_sensitive,
            )
            if arguments.emit_manifest_base64:
                encoded = base64.b64encode(
                    json.dumps(
                        validated_manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).decode("ascii")
                print(encoded)
            else:
                print("classifier manifest accepted")
    except ClassifierArtifactError as exc:
        print(f"classifier validation rejected: {exc.reason}", file=sys.stderr)
        return 1
    except (OSError, UnsafeZipError) as exc:
        del exc
        print("classifier validation rejected: archive", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
