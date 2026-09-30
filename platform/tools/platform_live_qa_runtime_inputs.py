#!/usr/bin/env python3
"""Load the closed, digest-bound live-QA runtime input contract.

The adjacent JSON file is the only owner of the runtime source, package,
browser and classifier-sensitive path lists.  This module deliberately uses
only the standard library because it is loaded from staged release builders,
trusted artifact validators and the production-side installer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping


SCHEMA = 1
VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_LIST_ITEMS = 256
MAX_ITEM_LENGTH = 512
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
FORBIDDEN_PATH_CHARS = frozenset("*?[]{}")

MANIFEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "runtime_source_files",
        "build_input_files",
        "runtime_packages",
        "payload_tool_files",
        "payload_source_trees",
        "payload_source_files",
        "browser_roots",
        "required_runtime_files",
        "classifier_sensitive_paths",
        "digest",
    }
)


class RuntimeInputsError(ValueError):
    """The checked-in runtime input manifest is invalid or tampered."""


def _reject_constant(_value: str) -> None:
    raise RuntimeInputsError("runtime input manifest contains a non-finite number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeInputsError("runtime input manifest contains duplicate keys")
        result[key] = value
    return result


def _canonical_payload(payload: Mapping[str, object]) -> bytes:
    without_digest = {key: value for key, value in payload.items() if key != "digest"}
    return json.dumps(
        without_digest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def manifest_digest(payload: Mapping[str, object]) -> str:
    """Return SHA-256 for all manifest fields except the digest itself."""

    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def _validate_relative_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ITEM_LENGTH
        or not value.isascii()
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or value.startswith("./")
        or any(character in FORBIDDEN_PATH_CHARS for character in value)
    ):
        raise RuntimeInputsError(f"runtime input {label} contains an invalid path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeInputsError(f"runtime input {label} contains a non-canonical path")
    if PurePosixPath(value).as_posix() != value or PurePosixPath(value).is_absolute():
        raise RuntimeInputsError(f"runtime input {label} contains a non-canonical path")
    return value


def _validate_path_list(
    value: object, *, label: str, max_items: int = MAX_LIST_ITEMS
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > max_items:
        raise RuntimeInputsError(f"runtime input {label} must be a bounded non-empty list")
    result = tuple(_validate_relative_path(item, label=label) for item in value)
    if len(set(result)) != len(result):
        raise RuntimeInputsError(f"runtime input {label} contains duplicate paths")
    if tuple(sorted(result, key=lambda item: PurePosixPath(item).parts)) != result:
        raise RuntimeInputsError(f"runtime input {label} is not in deterministic order")
    return result


def _validate_package_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_LIST_ITEMS:
        raise RuntimeInputsError("runtime package list must be a bounded non-empty list")
    result: list[str] = []
    for package in value:
        if (
            not isinstance(package, str)
            or not package
            or len(package) > MAX_ITEM_LENGTH
            or not package.isascii()
            or PACKAGE_RE.fullmatch(package) is None
        ):
            raise RuntimeInputsError("runtime package name is invalid")
        result.append(package)
    if len(set(result)) != len(result):
        raise RuntimeInputsError("runtime package list contains duplicates")
    if tuple(sorted(result)) != tuple(result):
        raise RuntimeInputsError("runtime package list is not in deterministic order")
    return tuple(result)


def _validate_object(payload: Mapping[str, object]) -> None:
    if set(payload) != MANIFEST_KEYS:
        raise RuntimeInputsError("runtime input manifest schema is not closed")
    if type(payload.get("schema")) is not int or payload["schema"] != SCHEMA:
        raise RuntimeInputsError("runtime input manifest schema is invalid")
    if type(payload.get("version")) is not int or payload["version"] != VERSION:
        raise RuntimeInputsError("runtime input manifest version is invalid")
    digest = payload.get("digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise RuntimeInputsError("runtime input manifest digest is invalid")

    runtime_source_files = _validate_path_list(
        payload.get("runtime_source_files"), label="runtime source files"
    )
    build_input_files = _validate_path_list(
        payload.get("build_input_files"), label="build input files"
    )
    runtime_packages = _validate_package_list(payload.get("runtime_packages"))
    _validate_path_list(
        payload.get("payload_tool_files"), label="payload tool files"
    )
    _validate_path_list(
        payload.get("payload_source_trees"), label="payload source trees"
    )
    _validate_path_list(
        payload.get("payload_source_files"), label="payload source files"
    )
    _validate_path_list(
        payload.get("browser_roots"), label="browser roots"
    )
    required_runtime_files = _validate_path_list(
        payload.get("required_runtime_files"), label="required runtime files"
    )
    classifier_sensitive_paths = _validate_path_list(
        payload.get("classifier_sensitive_paths"),
        label="classifier-sensitive paths",
    )

    expected_required = tuple(
        sorted(
            (
                "node/bin/node",
                *(f"web/{relative}" for relative in runtime_source_files),
                *(f"web/node_modules/{package}/package.json" for package in runtime_packages),
                "browsers/chromium-1228/chrome-linux64/chrome_sandbox",
            ),
            key=lambda item: PurePosixPath(item).parts,
        )
    )
    if required_runtime_files != expected_required:
        raise RuntimeInputsError("required runtime files do not match runtime inputs")
    if "platform/tools/platform_live_qa_runtime_inputs.json" not in classifier_sensitive_paths:
        raise RuntimeInputsError("runtime input manifest is not classifier-sensitive")
    if "package.json" not in build_input_files:
        raise RuntimeInputsError("package.json must remain a build-only input")
    if set(build_input_files) & set(runtime_source_files):
        raise RuntimeInputsError("runtime and build-only inputs overlap")
    if manifest_digest(payload) != digest:
        raise RuntimeInputsError("runtime input manifest digest does not match content")


@dataclass(frozen=True, slots=True)
class RuntimeInputManifest:
    """Validated immutable view shared by all runtime consumers."""

    schema: int
    version: int
    runtime_source_files: tuple[str, ...]
    build_input_files: tuple[str, ...]
    runtime_packages: tuple[str, ...]
    payload_tool_files: tuple[str, ...]
    payload_source_trees: tuple[str, ...]
    payload_source_files: tuple[str, ...]
    browser_roots: tuple[str, ...]
    required_runtime_files: tuple[str, ...]
    classifier_sensitive_paths: tuple[str, ...]
    digest: str

    @property
    def sha256(self) -> str:
        return self.digest


def _manifest_from_payload(payload: Mapping[str, object]) -> RuntimeInputManifest:
    _validate_object(payload)
    return RuntimeInputManifest(
        schema=payload["schema"],
        version=payload["version"],
        runtime_source_files=tuple(payload["runtime_source_files"]),
        build_input_files=tuple(payload["build_input_files"]),
        runtime_packages=tuple(payload["runtime_packages"]),
        payload_tool_files=tuple(payload["payload_tool_files"]),
        payload_source_trees=tuple(payload["payload_source_trees"]),
        payload_source_files=tuple(payload["payload_source_files"]),
        browser_roots=tuple(payload["browser_roots"]),
        required_runtime_files=tuple(payload["required_runtime_files"]),
        classifier_sensitive_paths=tuple(payload["classifier_sensitive_paths"]),
        digest=payload["digest"],
    )


def _manifest_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return the path identity needed to bind lstat to the opened file."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_manifest(path: Path) -> Mapping[str, object]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > MAX_MANIFEST_BYTES
        ):
            raise RuntimeInputsError("runtime input manifest metadata is unsafe")
        lstat_identity = _manifest_identity(metadata)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, RuntimeInputsError) as exc:
        if isinstance(exc, RuntimeInputsError):
            raise
        raise RuntimeInputsError("runtime input manifest is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if _manifest_identity(before) != lstat_identity:
            raise RuntimeInputsError(
                "runtime input manifest changed before it was opened"
            )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise RuntimeInputsError("runtime input manifest metadata is unsafe")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise RuntimeInputsError("runtime input manifest is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if _manifest_identity(after) != lstat_identity:
            raise RuntimeInputsError("runtime input manifest changed while reading")
        raw = b"".join(chunks)
    except OSError as exc:
        raise RuntimeInputsError("runtime input manifest is unavailable") from exc
    finally:
        os.close(descriptor)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RuntimeInputsError("runtime input manifest is too large")
    try:
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RuntimeInputsError) as exc:
        if isinstance(exc, RuntimeInputsError):
            raise
        raise RuntimeInputsError("runtime input manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeInputsError("runtime input manifest must be a JSON object")
    _validate_object(payload)
    return payload


def load_manifest(path: Path | None = None) -> RuntimeInputManifest:
    """Read and validate the adjacent or explicitly supplied manifest."""

    manifest_path = (
        Path(__file__).with_name("platform_live_qa_runtime_inputs.json")
        if path is None
        else Path(path)
    )
    return _manifest_from_payload(_read_manifest(manifest_path))


def validate_payload(payload: Mapping[str, object]) -> RuntimeInputManifest:
    """Validate an already-decoded manifest without filesystem access."""

    if not isinstance(payload, Mapping):
        raise RuntimeInputsError("runtime input manifest must be a JSON object")
    return _manifest_from_payload(payload)


load_runtime_input_manifest = load_manifest
DEFAULT_MANIFEST = load_manifest()
INPUT_MANIFEST_SHA256 = DEFAULT_MANIFEST.digest


__all__ = [
    "DEFAULT_MANIFEST",
    "INPUT_MANIFEST_SHA256",
    "MAX_MANIFEST_BYTES",
    "RuntimeInputManifest",
    "RuntimeInputsError",
    "load_manifest",
    "load_runtime_input_manifest",
    "manifest_digest",
    "validate_payload",
]
