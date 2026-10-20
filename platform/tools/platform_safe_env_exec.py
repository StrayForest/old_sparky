#!/usr/bin/env python3
"""Safely parse platform dotenv files and exec approved production commands.

The parser deliberately does not use a shell or ``python-dotenv``. Production
paths are opened without following symlinks and configuration values are always
treated as data. The fixed production exec contour remains narrowly allowlisted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from typing import Callable, Iterable


PRODUCTION_ENV_FILE = Path("/opt/oldsparky/platform/shared/.env.platform")
PRODUCTION_SHARED_DIR = PRODUCTION_ENV_FILE.parent
PRODUCTION_RUNTIME_ROOT = Path("/opt/oldsparky/platform")
ACTIVE_PLATFORM_ROOT = PRODUCTION_RUNTIME_ROOT / "current"
LIVE_QA_ROOT = Path("/root/.oldsparky/liveqa")
LIVE_QA_RELEASE_ROOT = LIVE_QA_ROOT / "releases"
LIVE_QA_ACTIVE_MANIFEST = LIVE_QA_ROOT / "active-manifest.json"
LIVE_QA_ACTIVE_POINTER = LIVE_QA_ROOT / "active"
LIVE_QA_SANDBOX_RELATIVE = "runtime/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
LIVE_QA_PROVIDER_SANDBOX_RELATIVE = (
    "provider-runtime/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
)
LIVE_QA_SANDBOX_SIZE = 15232
LIVE_QA_SANDBOX_SHA256 = (
    "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3"
)
ACTIVE_PYTHON = PRODUCTION_SHARED_DIR / "venv/bin/python"
MAX_ENV_BYTES = 256 * 1024
MAX_RELEASE_JSON_BYTES = 64 * 1024
MAX_ACTIVE_MANIFEST_BYTES = 256 * 1024
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
ALLOWED_PREFIXES = ("PLATFORM_", "NEXT_PUBLIC_PLATFORM_")
PUBLIC_VALUE_NAMES = frozenset({"PLATFORM_ENVIRONMENT", "PLATFORM_WEB_ORIGIN"})
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TRUSTED_SYSTEM_PYTHON = Path("/usr/bin/python3.12")
TRUSTED_DB_TOOLS = frozenset(
    {
        "platform_cleanup_live_user_qa.py",
        "platform_cleanup_retained_matrix.py",
        "platform_cleanup_retained_orphan.py",
        "platform_recover_retained_report.py",
        "platform_manual_live_auth_qa.py",
        "platform_provision_live_csp_qa.py",
        "platform_recover_live_user_qa.py",
    }
)


class SafeEnvError(RuntimeError):
    """A non-sensitive platform-env boundary failure."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SafeEnvError("live-QA manifest contains duplicate keys")
        result[key] = value
    return result


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bounded_json(
    path: Path,
    *,
    metadata: os.stat_result,
    maximum: int,
    validate_metadata: Callable[[os.stat_result], None],
    unavailable_message: str,
    invalid_message: str,
) -> object:
    """Read one trusted JSON file through an identity-bound descriptor."""

    validate_metadata(metadata)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SafeEnvError(unavailable_message) from exc
    try:
        try:
            opened = os.fstat(descriptor)
            validate_metadata(opened)
            if _file_fingerprint(opened) != _file_fingerprint(metadata):
                raise SafeEnvError("trusted JSON changed while opening")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            validate_metadata(after)
        except OSError as exc:
            raise SafeEnvError(unavailable_message) from exc
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > maximum:
        raise SafeEnvError("trusted JSON exceeds its size limit")
    if _file_fingerprint(after) != _file_fingerprint(opened):
        raise SafeEnvError("trusted JSON changed while reading")
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SafeEnvError(invalid_message) from exc


def _validate_active_manifest_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o7000
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size > MAX_ACTIVE_MANIFEST_BYTES
    ):
        raise SafeEnvError("installed live-QA active manifest metadata is unsafe")


def _validate_release_json_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o7000
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > MAX_RELEASE_JSON_BYTES
    ):
        raise SafeEnvError("active production release metadata is unsafe")


def _read_liveqa_manifest() -> dict[str, object]:
    """Return the active installed payload only after pointer/SHA validation."""

    try:
        root_metadata = LIVE_QA_ROOT.lstat()
        releases_metadata = LIVE_QA_RELEASE_ROOT.lstat()
        manifest_metadata = LIVE_QA_ACTIVE_MANIFEST.lstat()
        pointer_metadata = LIVE_QA_ACTIVE_POINTER.lstat()
        pointer_target = LIVE_QA_ACTIVE_POINTER.resolve(strict=True)
    except OSError as exc:
        raise SafeEnvError("installed live-QA payload is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != 0
        or root_metadata.st_gid != 0
        or root_metadata.st_mode & 0o7000
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or not stat.S_ISDIR(releases_metadata.st_mode)
        or releases_metadata.st_uid != 0
        or releases_metadata.st_gid != 0
        or releases_metadata.st_mode & 0o7000
        or stat.S_IMODE(releases_metadata.st_mode) != 0o755
    ):
        raise SafeEnvError("installed live-QA payload root metadata is unsafe")
    if (
        not stat.S_ISLNK(pointer_metadata.st_mode)
        or pointer_metadata.st_uid != 0
        or pointer_metadata.st_gid != 0
        or pointer_metadata.st_nlink != 1
    ):
        raise SafeEnvError("installed live-QA active manifest metadata is unsafe")
    _validate_active_manifest_metadata(manifest_metadata)
    payload = _read_bounded_json(
        LIVE_QA_ACTIVE_MANIFEST,
        metadata=manifest_metadata,
        maximum=MAX_ACTIVE_MANIFEST_BYTES,
        validate_metadata=_validate_active_manifest_metadata,
        unavailable_message="installed live-QA manifest is unavailable or unsafe",
        invalid_message="installed live-QA manifest is invalid",
    )
    expected = {"version", "source_sha", "release_slug", "payload", "payload_tree_sha256", "files"}
    source_sha = payload.get("source_sha") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("version") != 1
        or not isinstance(source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or payload.get("payload") != str(LIVE_QA_RELEASE_ROOT / source_sha)
        or pointer_target != LIVE_QA_RELEASE_ROOT / source_sha
        or not isinstance(payload.get("files"), dict)
        or not isinstance(payload.get("payload_tree_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["payload_tree_sha256"])
    ):
        raise SafeEnvError("installed live-QA manifest identity is invalid")
    try:
        current_metadata = ACTIVE_PLATFORM_ROOT.lstat()
        active_release = ACTIVE_PLATFORM_ROOT.resolve(strict=True)
        releases_root = (PRODUCTION_RUNTIME_ROOT / "releases").resolve(strict=True)
        release_metadata = active_release.lstat()
        release_json = active_release / "RELEASE.json"
        release_json_metadata = release_json.lstat()
        if (
            not stat.S_ISLNK(current_metadata.st_mode)
            or current_metadata.st_uid != 0
            or current_metadata.st_gid != 0
            or current_metadata.st_nlink != 1
            or current_metadata.st_mode & 0o7000
            or active_release.parent != releases_root
            or not stat.S_ISDIR(release_metadata.st_mode)
            or release_metadata.st_uid != 0
            or release_metadata.st_gid != 0
            or release_metadata.st_mode & 0o7000
            or stat.S_IMODE(release_metadata.st_mode) & 0o022
            or not stat.S_ISREG(release_json_metadata.st_mode)
        ):
            raise SafeEnvError("active production release metadata is unsafe")
        _validate_release_json_metadata(release_json_metadata)
        release = _read_bounded_json(
            release_json,
            metadata=release_json_metadata,
            maximum=MAX_RELEASE_JSON_BYTES,
            validate_metadata=_validate_release_json_metadata,
            unavailable_message="active production release metadata is unavailable",
            invalid_message="active production release metadata is invalid",
        )
    except OSError as exc:
        raise SafeEnvError("active production release metadata is unavailable") from exc
    if (
        not isinstance(release, dict)
        or release.get("source_git_commit") != source_sha
        or payload.get("release_slug") != active_release.name
    ):
        raise SafeEnvError("installed live-QA payload does not match active release")
    payload_root = LIVE_QA_RELEASE_ROOT / source_sha
    try:
        payload_metadata = payload_root.lstat()
    except OSError as exc:
        raise SafeEnvError("installed live-QA payload root is unavailable") from exc
    if (
        stat.S_ISLNK(payload_metadata.st_mode)
        or not stat.S_ISDIR(payload_metadata.st_mode)
        or payload_metadata.st_uid != 0
        or payload_metadata.st_gid != 0
        or stat.S_IMODE(payload_metadata.st_mode) != 0o555
        or payload_root.resolve(strict=True) != payload_root
    ):
        raise SafeEnvError("installed live-QA payload root metadata is unsafe")
    _validate_liveqa_payload_tree(payload_root, payload)
    return payload


def _validate_liveqa_payload_tree(root: Path, manifest: dict[str, object]) -> None:
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise SafeEnvError("installed live-QA manifest source identity is invalid")
    allowed_links = _liveqa_payload_links(root, source_sha)
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    seen_links: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            if (
                relative not in allowed_links or target != allowed_links[relative]
                or not relative.startswith("provider-runtime/browsers/")
                or metadata.st_uid != 0 or metadata.st_gid != 0
                or metadata.st_nlink != 1
                or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            ):
                raise SafeEnvError("installed live-QA payload contains an undeclared symlink")
            try:
                provider_root = root / "provider-runtime"
                resolved = path.resolve(strict=True)
                resolved.relative_to(provider_root.resolve(strict=True))
                if not stat.S_ISREG(resolved.lstat().st_mode):
                    raise ValueError("link target is not a regular file")
            except (OSError, ValueError) as exc:
                raise SafeEnvError("installed live-QA payload symlink target is unsafe") from exc
            seen_links[relative] = target
            digest.update(relative.encode("utf-8") + b"\0l\0" + target.encode("utf-8") + b"\0")
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_mode & 0o7000
                or stat.S_IMODE(metadata.st_mode) != 0o555
            ):
                raise SafeEnvError("installed live-QA payload directory mode is unsafe")
            digest.update(b"d\0")
            continue
        sandbox = relative in {LIVE_QA_SANDBOX_RELATIVE, LIVE_QA_PROVIDER_SANDBOX_RELATIVE}
        mode = stat.S_IMODE(metadata.st_mode)
        expected_modes = {0o4755} if sandbox else {0o444, 0o555}
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or mode not in expected_modes
            or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            and not sandbox
            or path.name == "chrome_sandbox" and not sandbox
        ):
            raise SafeEnvError("installed live-QA payload file metadata is unsafe")
        content = path.read_bytes()
        total += len(content)
        if total > 2 * 1024 * 1024 * 1024:
            raise SafeEnvError("installed live-QA payload is too large")
        file_digest = hashlib.sha256(content).hexdigest()
        if sandbox and (
            metadata.st_size != LIVE_QA_SANDBOX_SIZE
            or file_digest != LIVE_QA_SANDBOX_SHA256
        ):
            raise SafeEnvError("installed Chromium sandbox checksum is invalid")
        files[relative] = file_digest
        digest.update(b"f\0" + bytes.fromhex(file_digest))
    if (
        seen_links != allowed_links
        or digest.hexdigest() != manifest["payload_tree_sha256"]
        or files != manifest["files"]
    ):
        raise SafeEnvError("installed live-QA payload digest does not match manifest")


def _liveqa_payload_links(root: Path, source_sha: str) -> dict[str, str]:
    """Parse only the exact self-owned v2 aliases permitted in a payload."""
    provider_path = root / "runtime-provider.json"
    try:
        metadata = provider_path.lstat()
    except FileNotFoundError:
        if os.path.lexists(root / "provider-runtime"):
            raise SafeEnvError("live-QA payload has an unreferenced provider tree")
        return {}
    if (
        not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
        or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size > MAX_ACTIVE_MANIFEST_BYTES
    ):
        raise SafeEnvError("installed live-QA provider metadata is unsafe")
    def validate_provider_metadata(value: os.stat_result) -> None:
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != 0 or value.st_gid != 0 or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o444
            or value.st_size > MAX_ACTIVE_MANIFEST_BYTES
        ):
            raise SafeEnvError("installed live-QA provider metadata is unsafe")

    provider = _read_bounded_json(
        provider_path,
        metadata=metadata,
        maximum=MAX_ACTIVE_MANIFEST_BYTES,
        validate_metadata=validate_provider_metadata,
        unavailable_message="installed live-QA provider is unavailable",
        invalid_message="installed live-QA provider is invalid",
    )
    if not isinstance(provider, dict):
        raise SafeEnvError("installed live-QA provider identity is invalid")
    version = provider.get("version")
    if type(version) is not int or version not in {1, 2}:
        raise SafeEnvError("installed live-QA provider identity is invalid")
    if version == 1:
        expected_v1 = {
            "version", "source_sha", "provider_sha", "engine_tree_sha256",
            "node_version", "package_lock_sha256", "engine_files",
        }
        if (
            set(provider) != expected_v1
            or provider.get("source_sha") != source_sha
            or not isinstance(provider.get("provider_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", provider["provider_sha"]) is None
            or provider.get("node_version") != "26.3.1"
            or not isinstance(provider.get("engine_tree_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", provider["engine_tree_sha256"]) is None
            or not isinstance(provider.get("package_lock_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", provider["package_lock_sha256"]) is None
            or not isinstance(provider.get("engine_files"), dict)
            or len(provider["engine_files"]) > 200_000
        ):
            raise SafeEnvError("installed live-QA provider identity is invalid")
        for relative, file_digest in provider["engine_files"].items():
            parts = relative.split("/") if isinstance(relative, str) else []
            if (
                not isinstance(relative, str)
                or not relative
                or relative.startswith("/")
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in parts)
                or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in relative)
                or relative.startswith("web/node_modules/")
                and "/node_modules/" in relative.removeprefix("web/node_modules/")
                or not (
                    relative == "node/bin/node"
                    or relative.startswith("browsers/") and len(parts) >= 3
                    and parts[1] in {"chromium-1228", "chromium_headless_shell-1228", "webkit-2311", "ffmpeg-1011"}
                    or any(relative.startswith(f"web/node_modules/{pkg}/") for pkg in ("@playwright/test", "playwright", "playwright-core"))
                )
                or not isinstance(file_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", file_digest) is None
            ):
                raise SafeEnvError("installed live-QA provider file map is invalid")
        paths = set(provider["engine_files"])
        if any("/".join(path.split("/")[:index]) in paths for path in paths for index in range(1, len(path.split("/")))):
            raise SafeEnvError("installed live-QA provider file map has a path conflict")
        if os.path.lexists(root / "provider-runtime"):
            raise SafeEnvError("legacy live-QA provider has an unexpected local tree")
        return {}
    expected = {
        "version", "source_sha", "provider_sha", "engine_tree_sha256",
        "node_version", "package_lock_sha256", "engine_files", "engine_links",
    }
    if (
        set(provider) != expected or provider.get("source_sha") != source_sha
        or not isinstance(provider.get("provider_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", provider["provider_sha"]) is None
        or not isinstance(provider.get("engine_files"), dict)
        or not isinstance(provider.get("engine_links"), dict)
        or not isinstance(provider.get("engine_tree_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", provider["engine_tree_sha256"]) is None
        or provider.get("node_version") != "26.3.1"
        or not isinstance(provider.get("package_lock_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", provider["package_lock_sha256"]) is None
    ):
        raise SafeEnvError("installed live-QA provider identity is invalid")
    external_provider = provider["provider_sha"] != source_sha
    if external_provider:
        if os.path.lexists(root / "provider-runtime"):
            raise SafeEnvError("external live-QA provider has an unexpected local tree")
    links = provider["engine_links"]
    files = provider["engine_files"]
    browser_roots = {"chromium-1228", "chromium_headless_shell-1228", "webkit-2311", "ffmpeg-1011"}
    if (
        len(links) > 200_000
        or set(links) & set(files)
        or any(
            not isinstance(path, str)
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in path)
            or path.startswith("web/node_modules/")
            and "/node_modules/" in path.removeprefix("web/node_modules/")
            or not (
                path == "node/bin/node"
                or path.startswith("browsers/") and len(path.split("/")) >= 3 and path.split("/", 2)[1] in browser_roots
                or any(path.startswith(f"web/node_modules/{pkg}/") for pkg in ("@playwright/test", "playwright", "playwright-core"))
            )
            or not isinstance(file_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", file_digest) is None
            for path, file_digest in files.items()
        )
    ):
        raise SafeEnvError("installed live-QA provider link map is invalid")
    paths = set(files) | set(links)
    if any("/".join(path.split("/")[:index]) in paths for path in paths for index in range(1, len(path.split("/")))):
        raise SafeEnvError("installed live-QA provider link map has a path conflict")
    terminals: dict[str, str] = {}
    browser_roots = {"chromium-1228", "chromium_headless_shell-1228", "webkit-2311", "ffmpeg-1011"}
    for alias, target in links.items():
        if not isinstance(alias, str) or not isinstance(target, str) or "\\" in alias or "\\" in target or alias.startswith("/") or target.startswith("/") or "\x00" in alias or "\x00" in target:
            raise SafeEnvError("installed live-QA provider link map is invalid")
        parts = alias.split("/")
        if len(parts) < 3 or parts[0] != "browsers" or parts[1] not in browser_roots or any(part in {"", ".", ".."} for part in parts) or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in alias):
            raise SafeEnvError("installed live-QA provider link map is invalid")
        resolved = parts[:-1]
        target_parts = target.split("/")
        if not target_parts or any(part in {"", "."} for part in target_parts) or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in target):
            raise SafeEnvError("installed live-QA provider link target is invalid")
        for part in target_parts:
            if part == "..":
                if len(resolved) <= 2:
                    raise SafeEnvError("installed live-QA provider link escapes browser root")
                resolved.pop()
            else:
                resolved.append(part)
        if resolved[:2] != parts[:2] or len(resolved) < 3:
            raise SafeEnvError("installed live-QA provider link escapes browser root")
        terminals[alias] = "/".join(resolved)
    for alias in links:
        current = alias
        visited: set[str] = set()
        while current in terminals:
            if current in visited:
                raise SafeEnvError("installed live-QA provider link map contains a cycle")
            visited.add(current)
            current = terminals[current]
        if current not in files:
            raise SafeEnvError("installed live-QA provider link target is not a regular file")
    if external_provider:
        return {}
    return {f"provider-runtime/{path}": target for path, target in links.items()}


def _open_component(
    name: str,
    *,
    directory_fd: int,
    directory: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise SafeEnvError("environment path is unavailable or unsafe") from exc


def _production_component_owners() -> tuple[int, ...]:
    # /, /opt, /opt/oldsparky, /opt/oldsparky/platform, .../shared
    return (0, 0, 0, 0, 0)


def _validate_directory(metadata: os.stat_result, *, expected_uid: int) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SafeEnvError("environment path ownership is unsafe")


def _validate_production_env_file(metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_size > MAX_ENV_BYTES
        or mode != 0o600
        or metadata.st_gid != 0
    ):
        raise SafeEnvError("production environment file metadata is unsafe")


# Backward-compatible private helper used by metadata regression tests.
_validate_env_file = _validate_production_env_file

def _read_env_bytes_at(path: Path, *, owners: tuple[int, ...]) -> bytes:
    """Open an absolute path one trusted component at a time."""

    parts = path.parts
    if (
        not path.is_absolute()
        or not parts
        or parts[0] != "/"
        or any(part in {"", ".", ".."} for part in parts[1:])
        or len(owners) != len(parts) - 1
    ):
        raise SafeEnvError("production environment path is invalid")
    root_fd = os.open(
        "/",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    current_fd = root_fd
    opened: list[int] = []
    try:
        _validate_directory(os.fstat(root_fd), expected_uid=owners[0])
        for index, component in enumerate(parts[1:-1], start=1):
            next_fd = _open_component(
                component,
                directory_fd=current_fd,
                directory=True,
            )
            opened.append(next_fd)
            _validate_directory(os.fstat(next_fd), expected_uid=owners[index])
            current_fd = next_fd
        file_fd = _open_component(
            parts[-1],
            directory_fd=current_fd,
            directory=False,
        )
        opened.append(file_fd)
        before = os.fstat(file_fd)
        _validate_production_env_file(before)
        raw = _read_open_file(file_fd, before)
        return raw
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
        os.close(root_fd)


def _read_open_file(file_fd: int, before: os.stat_result) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_ENV_BYTES + 1
    while remaining:
        chunk = os.read(file_fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    after = os.fstat(file_fd)
    if len(raw) > MAX_ENV_BYTES or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SafeEnvError("environment changed while reading")
    return raw


def read_production_env_bytes(path: Path = PRODUCTION_ENV_FILE) -> bytes:
    """Open the fixed production environment without following any symlink."""

    if path != PRODUCTION_ENV_FILE or path.parts != (
        "/",
        "opt",
        "oldsparky",
        "platform",
        "shared",
        ".env.platform",
    ):
        raise SafeEnvError("production environment path must be fixed")
    return _read_env_bytes_at(path, owners=_production_component_owners())


def _is_production_shared_path(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        path.relative_to(PRODUCTION_SHARED_DIR)
    except ValueError:
        return False
    return True


def _validate_generic_path(path: Path, metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_ENV_BYTES
        or mode & 0o022
    ):
        raise SafeEnvError("environment file metadata is unsafe")
    if _is_production_shared_path(path):
        if metadata.st_uid != 0:
            raise SafeEnvError("production runtime environment must be root-owned")
        current = path.parent
        while current != Path("/"):
            current_metadata = current.lstat()
            if (
                stat.S_ISLNK(current_metadata.st_mode)
                or not stat.S_ISDIR(current_metadata.st_mode)
                or current_metadata.st_uid != 0
                or stat.S_IMODE(current_metadata.st_mode) & 0o022
            ):
                raise SafeEnvError("production runtime environment path is unsafe")
            current = current.parent


def read_env_bytes(path: Path) -> bytes:
    """Read a platform dotenv file without executing it or following the final link."""

    if path == PRODUCTION_ENV_FILE:
        return read_production_env_bytes(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SafeEnvError("environment file is unavailable") from exc
    _validate_generic_path(path, metadata)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise SafeEnvError("environment file is unavailable or unsafe") from exc
    try:
        opened = os.fstat(file_fd)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SafeEnvError("environment file changed during validation")
        _validate_generic_path(path, opened)
        return _read_open_file(file_fd, opened)
    finally:
        os.close(file_fd)


def _parse_value(value: str, *, line_number: int) -> str:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise SafeEnvError(f"invalid dotenv quoting on line {line_number}") from exc
    if len(tokens) == 0 and value == "":
        return ""
    if len(tokens) != 1:
        raise SafeEnvError(f"dotenv value on line {line_number} is ambiguous")
    parsed = tokens[0]
    if "\x00" in parsed or "\n" in parsed or "\r" in parsed:
        raise SafeEnvError(f"dotenv value on line {line_number} is unsafe")
    return parsed


def parse_dotenv(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_ENV_BYTES:
        raise SafeEnvError("environment exceeds its size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SafeEnvError("environment must be UTF-8") from exc
    if "\x00" in text or "\u0085" in text or "\u2028" in text or "\u2029" in text:
        raise SafeEnvError("environment contains an unsafe line separator")
    if "\r" in text.replace("\r\n", ""):
        raise SafeEnvError("environment contains a bare carriage return")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise SafeEnvError(f"invalid dotenv assignment on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if (
            not KEY_PATTERN.fullmatch(key)
            or not key.startswith(ALLOWED_PREFIXES)
            or key in values
        ):
            raise SafeEnvError(f"invalid or duplicate dotenv key on line {line_number}")
        values[key] = _parse_value(raw_value.strip(), line_number=line_number)
    if not values:
        raise SafeEnvError("environment is empty")
    return values


def load_env_file(path: Path) -> dict[str, str]:
    return parse_dotenv(read_env_bytes(path))


def clean_child_environment(
    values: dict[str, str], *, pythonpath: Path
) -> dict[str, str]:
    payload = _read_liveqa_manifest()["payload"]
    if not isinstance(payload, str) or pythonpath != Path(payload):
        raise SafeEnvError("PYTHONPATH must be the active digest-bound live-QA payload")
    child = dict(values)
    child.update(
        {
            "LANG": "C.UTF-8",
            "HOME": "/nonexistent",
            "PATH": SAFE_PATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(pythonpath),
        }
    )
    return child


def validate_trusted_command(command: list[str], *, pythonpath: Path) -> None:
    if len(command) < 2:
        raise SafeEnvError(
            "clean exec requires the fixed root-controlled Python runtime"
        )
    python = Path(command[0])
    script = Path(command[1])
    payload_value = _read_liveqa_manifest()["payload"]
    if not isinstance(payload_value, str):
        raise SafeEnvError("installed live-QA payload identity is invalid")
    payload_root = Path(payload_value)
    if not (
        pythonpath == payload_root
        and python == ACTIVE_PYTHON
        and script.is_absolute()
        and script.parent == payload_root / "platform/tools"
        and script.name in TRUSTED_DB_TOOLS
    ):
        raise SafeEnvError("clean exec target is not an approved live QA DB tool")
    validate_active_runtime()
    for path in (payload_root, ACTIVE_PYTHON, script):
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
        except OSError as exc:
            raise SafeEnvError("clean exec target is unavailable") from exc
        if path == ACTIVE_PYTHON:
            # Symlink permission bits are not meaningful on Linux.  The
            # production venv intentionally points at the fixed system Python,
            # so validate the link owner and exact target, then validate the
            # target file's own metadata.
            if metadata.st_uid != 0 or metadata.st_nlink != 1:
                raise SafeEnvError("clean exec target ownership is unsafe")
            try:
                python_metadata = resolved.stat()
            except OSError as exc:
                raise SafeEnvError("clean exec target is unavailable") from exc
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or resolved != TRUSTED_SYSTEM_PYTHON
                or not stat.S_ISREG(python_metadata.st_mode)
                or python_metadata.st_uid != 0
                or python_metadata.st_nlink != 1
                or stat.S_IMODE(python_metadata.st_mode) & 0o022
                or python_metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
            ):
                raise SafeEnvError("clean exec Python target is unsafe")
            continue
        if path == script and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            raise SafeEnvError("clean exec script target is unsafe")
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise SafeEnvError("clean exec target ownership is unsafe")


def _validate_root_owned_tree(root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SafeEnvError("root-controlled Python import tree is unavailable") from exc
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *filenames]:
            target = directory_path / name
            try:
                metadata = target.lstat()
            except OSError as exc:
                raise SafeEnvError(
                    "root-controlled Python import tree changed"
                ) from exc
            if metadata.st_uid != 0:
                raise SafeEnvError("root-controlled Python import tree owner is unsafe")
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    resolved = target.resolve(strict=True)
                except OSError as exc:
                    raise SafeEnvError(
                        "root-controlled Python symlink is invalid"
                    ) from exc
                if resolved != TRUSTED_SYSTEM_PYTHON:
                    try:
                        resolved.relative_to(resolved_root)
                    except ValueError as exc:
                        raise SafeEnvError(
                            "root-controlled Python symlink escapes"
                        ) from exc
                elif not (
                    root == PRODUCTION_SHARED_DIR / "venv"
                    and target.parent == root / "bin"
                    and target.name in {"python", "python3", "python3.12"}
                ):
                    raise SafeEnvError(
                        "root-controlled Python symlink target is unsafe"
                    )
            elif stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise SafeEnvError("root-controlled Python directory is writable")
            elif stat.S_ISREG(metadata.st_mode):
                if (
                    metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
                ):
                    raise SafeEnvError(
                        "root-controlled Python file is writable or linked"
                    )
            else:
                raise SafeEnvError(
                    "root-controlled Python import tree has a special file"
                )


def validate_trusted_runtime() -> None:
    _read_liveqa_manifest()
    for system_path in (Path("/usr"), Path("/usr/bin"), TRUSTED_SYSTEM_PYTHON):
        metadata = system_path.lstat()
        if (
            (
                system_path == TRUSTED_SYSTEM_PYTHON
                and not stat.S_ISREG(metadata.st_mode)
            )
            or (
                system_path != TRUSTED_SYSTEM_PYTHON
                and not stat.S_ISDIR(metadata.st_mode)
            )
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (system_path == TRUSTED_SYSTEM_PYTHON and metadata.st_nlink != 1)
            or (
                system_path == TRUSTED_SYSTEM_PYTHON
                and metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
            )
        ):
            raise SafeEnvError("trusted system Python path is unsafe")


def validate_active_runtime() -> None:
    """Validate production runtime plus the active digest-bound QA payload."""

    for directory in (
        Path("/opt"),
        Path("/opt/oldsparky"),
        PRODUCTION_RUNTIME_ROOT,
        PRODUCTION_RUNTIME_ROOT / "releases",
        PRODUCTION_SHARED_DIR,
        PRODUCTION_SHARED_DIR / "venv",
    ):
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise SafeEnvError("active production runtime path is unsafe")
    current_metadata = ACTIVE_PLATFORM_ROOT.lstat()
    if not stat.S_ISLNK(current_metadata.st_mode) or current_metadata.st_uid != 0:
        raise SafeEnvError("active production release path is unsafe")
    resolved_current = ACTIVE_PLATFORM_ROOT.resolve(strict=True)
    try:
        resolved_current.relative_to(PRODUCTION_RUNTIME_ROOT / "releases")
    except ValueError as exc:
        raise SafeEnvError("active production release path is unsafe") from exc
    release_metadata = resolved_current.lstat()
    if (
        not stat.S_ISDIR(release_metadata.st_mode)
        or release_metadata.st_uid != 0
        or stat.S_IMODE(release_metadata.st_mode) & 0o022
    ):
        raise SafeEnvError("active production release metadata is unsafe")
    _validate_root_owned_tree(PRODUCTION_SHARED_DIR / "venv")
    _read_liveqa_manifest()


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely read platform dotenv data and exec approved commands."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    value = commands.add_parser("print-public-value")
    value.add_argument("name", choices=sorted(PUBLIC_VALUE_NAMES))
    commands.add_parser("validate-runtime")
    export_values = commands.add_parser("export-b64")
    export_values.add_argument("--path", required=True, type=Path)
    execute = commands.add_parser("exec")
    execute.add_argument("--pythonpath", required=True, type=Path)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def _emit_base64_assignments(values: dict[str, str]) -> None:
    for key in sorted(values):
        encoded = base64.b64encode(values[key].encode("utf-8")).decode("ascii")
        print(f"{key}\t{encoded}")


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "export-b64":
            _emit_base64_assignments(load_env_file(args.path))
            return 0
        if os.geteuid() != 0:
            raise SafeEnvError("safe production environment access requires root")
        if args.command == "validate-runtime":
            validate_trusted_runtime()
            return 0
        values = parse_dotenv(read_production_env_bytes())
        if args.command == "print-public-value":
            value = values.get(args.name)
            if value is None or "\n" in value or "\r" in value:
                raise SafeEnvError("required public production setting is unavailable")
            print(value)
            return 0
        command = list(args.argv)
        if command and command[0] == "--":
            command = command[1:]
        validate_trusted_command(command, pythonpath=args.pythonpath)
        # Executable and script are the exact root-controlled allowlist above.
        os.execve(  # nosec B606
            command[0],
            command,
            clean_child_environment(values, pythonpath=args.pythonpath),
        )
    except SafeEnvError as exc:
        print(f"Safe platform environment refused: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Safe platform environment operation failed.", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
