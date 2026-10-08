#!/usr/bin/env python3
"""Run the installed live-user QA helper without a source checkout.

The workflow's secret-bearing runner invokes this file through the fixed
remote dispatcher.  The helper and CSP bundle are both host-installed, root-
owned files under fixed paths.  A target SHA is data-only and is checked
against the active release metadata before the browser/mailbox helper starts.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
import subprocess
import sys


RUNTIME = Path("/opt/oldsparky/platform")
CURRENT = RUNTIME / "current"
# This file is deliberately outside the candidate release.  The production
# release may contain the application under test, but it must never supply the
# browser/mailbox supervisor that runs while the workflow's SSH credential is
# present.  Provision this helper from the host image and fail closed when it
# is absent or replaced.
TRUSTED_LIVE_QA_ROOT = Path("/root/.oldsparky/liveqa")
QA_HELPER = TRUSTED_LIVE_QA_ROOT / "platform_live_user_qa_trusted.sh"
LAUNCH_HELPER = TRUSTED_LIVE_QA_ROOT / "platform_live_launch_trusted.sh"
BUNDLE = Path("/root/.oldsparky/liveqa/csp-live-qa.json")
MAILBOX_HELPER = TRUSTED_LIVE_QA_ROOT / "platform_live_qa_mailbox_helper.py"
REMOTE_DISPATCHER = TRUSTED_LIVE_QA_ROOT / "platform_workflow_remote_dispatch.py"
REMOTE_INPUT_GUARD = TRUSTED_LIVE_QA_ROOT / "platform_workflow_input_guard.py"
RELEASE_LOCK_EXEC = TRUSTED_LIVE_QA_ROOT / "platform_release_lock_exec.sh"
RELEASE_LOCK = TRUSTED_LIVE_QA_ROOT / "platform_release_lock.sh"
ACTIVE_MANIFEST = TRUSTED_LIVE_QA_ROOT / "active-manifest.json"
PAYLOAD_ROOT = TRUSTED_LIVE_QA_ROOT / "releases"
ACTIVE_POINTER = TRUSTED_LIVE_QA_ROOT / "active"
PAYLOAD_FILE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
MAX_RELEASE_JSON_BYTES = 64 * 1024
MAX_ACTIVE_MANIFEST_BYTES = 256 * 1024
MAX_PAYLOAD_FILE_BYTES = 768 * 1024 * 1024
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_PAYLOAD_FILES = 200_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MARKER_RE = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
CHROMIUM_SANDBOX_RELATIVE = "runtime/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
CHROMIUM_SANDBOX_SIZE = 15232
CHROMIUM_SANDBOX_SHA256 = (
    "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3"
)


def _regular(
    path: Path,
    *,
    mode: int | None = None,
    maximum: int = 1024 * 1024,
    allow_sandbox: bool = False,
) -> os.stat_result:
    metadata = path.lstat()
    _validate_regular_metadata(
        path,
        metadata,
        mode=mode,
        maximum=maximum,
        allow_sandbox=allow_sandbox,
    )
    return metadata


def _validate_regular_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    mode: int | None = None,
    maximum: int = 1024 * 1024,
    allow_sandbox: bool = False,
) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (
            metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            and not (
                allow_sandbox
                and path.name == "chrome_sandbox"
                and stat.S_IMODE(metadata.st_mode) == 0o4755
            )
        )
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
        or metadata.st_size > maximum
    ):
        raise RuntimeError("installed live-user helper metadata is unsafe")


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bounded_regular(
    path: Path,
    *,
    metadata: os.stat_result,
    maximum: int,
    mode: int | None = None,
) -> bytes:
    """Read a trusted regular file through an identity-bound descriptor."""

    _validate_regular_metadata(path, metadata, mode=mode, maximum=maximum)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeError("installed live-user helper is unavailable or unsafe") from exc
    try:
        try:
            opened = os.fstat(descriptor)
            _validate_regular_metadata(path, opened, mode=mode, maximum=maximum)
            if _file_fingerprint(opened) != _file_fingerprint(metadata):
                raise RuntimeError("installed live-user helper changed while opening")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            _validate_regular_metadata(path, after, mode=mode, maximum=maximum)
        except OSError as exc:
            raise RuntimeError("installed live-user helper is unavailable or unsafe") from exc
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > maximum:
        raise RuntimeError("installed live-user helper exceeds its size bound")
    if _file_fingerprint(after) != _file_fingerprint(opened):
        raise RuntimeError("installed live-user helper changed while reading")
    return raw


def _directory(path: Path, *, mode: int | None = None) -> os.stat_result:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise RuntimeError("installed live-QA directory metadata is unsafe")
    return metadata


def _trusted_directory_chain(path: Path) -> None:
    """Require every fixed helper parent to be a root-controlled directory."""

    if not path.is_absolute():
        raise RuntimeError("trusted live-user helper path is not absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink < 2
            or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RuntimeError("trusted live-user helper directory is unsafe")


def _active_release_identity() -> tuple[str, str]:
    _trusted_directory_chain(RUNTIME)
    _trusted_directory_chain(RUNTIME / "releases")
    if not CURRENT.is_symlink():
        raise RuntimeError("active production release is not a symlink")
    current_metadata = CURRENT.lstat()
    if (
        current_metadata.st_uid != 0
        or current_metadata.st_gid != 0
        or current_metadata.st_nlink != 1
    ):
        raise RuntimeError("active production release pointer is unsafe")
    release = CURRENT.resolve(strict=True)
    if release.parent != RUNTIME / "releases" or SLUG_RE.fullmatch(release.name) is None:
        raise RuntimeError("active production release path is invalid")
    metadata = release.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("active production release metadata is unsafe")
    release_json = release / "RELEASE.json"
    release_json_metadata = _regular(
        release_json,
        maximum=MAX_RELEASE_JSON_BYTES,
    )
    payload = json.loads(
        _read_bounded_regular(
            release_json,
            metadata=release_json_metadata,
            maximum=MAX_RELEASE_JSON_BYTES,
        ).decode("ascii")
    )
    value = payload.get("source_git_commit") if isinstance(payload, dict) else None
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise RuntimeError("active release source identity is invalid")
    return value, release.name


def _active_source_sha() -> str:
    return _active_release_identity()[0]


def _open_and_hash(path: Path, *, maximum: int, allow_sandbox: bool = False) -> str:
    metadata = _regular(path, maximum=maximum, allow_sandbox=allow_sandbox)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        ):
            raise RuntimeError("installed live-QA file changed while opening")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise RuntimeError("installed live-QA file exceeds its bound")
            digest.update(chunk)
        if os.fstat(descriptor).st_size != metadata.st_size:
            raise RuntimeError("installed live-QA file changed while reading")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_manifest(target_sha: str) -> dict[str, object]:
    _trusted_directory_chain(TRUSTED_LIVE_QA_ROOT)
    _directory(PAYLOAD_ROOT, mode=0o755)
    metadata = _regular(
        ACTIVE_MANIFEST,
        mode=0o444,
        maximum=MAX_ACTIVE_MANIFEST_BYTES,
    )
    raw = _read_bounded_regular(
        ACTIVE_MANIFEST,
        metadata=metadata,
        maximum=MAX_ACTIVE_MANIFEST_BYTES,
        mode=0o444,
    )
    try:
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=lambda pairs: _strict_object(pairs),
        )
    except (UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError("installed live-QA manifest is invalid") from exc
    expected = {
        "version",
        "source_sha",
        "release_slug",
        "payload",
        "payload_tree_sha256",
        "files",
    }
    files = payload.get("files") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("version") != 1
        or payload.get("source_sha") != target_sha
        or not isinstance(payload.get("release_slug"), str)
        or SLUG_RE.fullmatch(payload["release_slug"]) is None
        or payload.get("payload") != str(PAYLOAD_ROOT / target_sha)
        or not isinstance(payload.get("payload_tree_sha256"), str)
        or PAYLOAD_FILE_PATTERN.fullmatch(payload["payload_tree_sha256"]) is None
        or not isinstance(files, dict)
    ):
        raise RuntimeError("installed live-QA manifest schema is invalid")
    try:
        pointer_metadata = ACTIVE_POINTER.lstat()
        pointer_target = ACTIVE_POINTER.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("installed live-QA active generation pointer is unavailable") from exc
    if (
        not stat.S_ISLNK(pointer_metadata.st_mode)
        or pointer_metadata.st_uid != 0
        or pointer_metadata.st_gid != 0
        or pointer_metadata.st_nlink != 1
        or pointer_target != PAYLOAD_ROOT / target_sha
    ):
        raise RuntimeError("installed live-QA active generation pointer is invalid")
    if any(
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not isinstance(digest, str)
        or PAYLOAD_FILE_PATTERN.fullmatch(digest) is None
        for path, digest in files.items()
    ):
        raise RuntimeError("installed live-QA manifest file map is invalid")
    for required in (
        "platform/tools/platform_live_user_qa_trusted.sh",
        "platform/tools/platform_live_launch_trusted.sh",
        "platform/tools/platform_live_launch_supervisor.sh",
        "platform/tools/platform_live_user_qa_dispatch.py",
        "platform/tools/platform_workflow_remote_dispatch.py",
        "platform/tools/platform_workflow_input_guard.py",
        "platform/tools/platform_release_lock_exec.sh",
        "platform/tools/platform_release_lock.sh",
    ):
        if required not in files:
            raise RuntimeError("installed live-QA manifest entrypoint is missing")
    return payload


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("installed live-QA manifest contains duplicate keys")
        result[key] = value
    return result


def _payload_tree_digest(root: Path) -> tuple[str, dict[str, str]]:
    _directory(root, mode=0o555)
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    count = 0
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("installed live-QA payload contains a symlink")
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            _directory(path, mode=0o555)
            digest.update(b"d\0")
            continue
        is_sandbox = relative == CHROMIUM_SANDBOX_RELATIVE
        if path.name == "chrome_sandbox" and not is_sandbox:
            raise RuntimeError("installed live-QA payload contains an unexpected sandbox helper")
        _regular(path, allow_sandbox=is_sandbox, maximum=MAX_PAYLOAD_FILE_BYTES)
        count += 1
        total += metadata.st_size
        if count > MAX_PAYLOAD_FILES or total > MAX_PAYLOAD_BYTES:
            raise RuntimeError("installed live-QA payload exceeds its bound")
        file_digest = _open_and_hash(
            path,
            maximum=MAX_PAYLOAD_FILE_BYTES,
            allow_sandbox=is_sandbox,
        )
        if is_sandbox and (
            metadata.st_size != CHROMIUM_SANDBOX_SIZE
            or file_digest != CHROMIUM_SANDBOX_SHA256
        ):
            raise RuntimeError("installed Chromium sandbox checksum is invalid")
        files[relative] = file_digest
        digest.update(b"f\0" + bytes.fromhex(file_digest))
    return digest.hexdigest(), files


def _verify_install(target_sha: str) -> dict[str, object]:
    active_sha, active_slug = _active_release_identity()
    if active_sha != target_sha:
        raise RuntimeError("active release does not match live-QA target SHA")
    manifest = _read_manifest(target_sha)
    if manifest.get("release_slug") != active_slug:
        raise RuntimeError("installed live-QA manifest release is not active")
    payload_root = Path(str(manifest["payload"]))
    if payload_root != PAYLOAD_ROOT / target_sha:
        raise RuntimeError("installed live-QA payload path is invalid")
    tree_digest, files = _payload_tree_digest(payload_root)
    if (
        tree_digest != manifest["payload_tree_sha256"]
        or files != manifest["files"]
    ):
        raise RuntimeError("installed live-QA payload digest does not match manifest")
    manifest_files = manifest["files"]
    assert isinstance(manifest_files, dict)
    bound_paths = (
        (QA_HELPER, 0o755, "platform/tools/platform_live_user_qa_trusted.sh"),
        (LAUNCH_HELPER, 0o755, "platform/tools/platform_live_launch_trusted.sh"),
        (
            TRUSTED_LIVE_QA_ROOT / "platform_live_user_qa_dispatch.py",
            0o500,
            "platform/tools/platform_live_user_qa_dispatch.py",
        ),
        (REMOTE_DISPATCHER, 0o555, "platform/tools/platform_workflow_remote_dispatch.py"),
        (REMOTE_INPUT_GUARD, 0o555, "platform/tools/platform_workflow_input_guard.py"),
        (RELEASE_LOCK_EXEC, 0o555, "platform/tools/platform_release_lock_exec.sh"),
        (RELEASE_LOCK, 0o444, "platform/tools/platform_release_lock.sh"),
        (MAILBOX_HELPER, 0o500, "platform/tools/platform_live_qa_mailbox_helper.py"),
    )
    for path, mode, relative in bound_paths:
        _regular(path, mode=mode, maximum=MAX_PAYLOAD_FILE_BYTES)
        expected_digest = manifest_files.get(relative)
        if not isinstance(expected_digest, str) or _open_and_hash(
            path, maximum=MAX_PAYLOAD_FILE_BYTES
        ) != expected_digest:
            raise RuntimeError("installed live-QA trusted entrypoint is not bound to the manifest")
    return manifest


def _source_binding_context(
    runner_sha: str, arguments: list[str]
) -> tuple[str, dict[str, object] | None, str | None]:
    """Decode a bounded, canonical suffix to select the installed app payload."""

    if SHA_RE.fullmatch(runner_sha) is None:
        raise RuntimeError("runner source identity is invalid")
    if not arguments:
        return runner_sha, None, None
    if len(arguments) != 2 or arguments[0] != "--source-binding-base64":
        raise RuntimeError("live-QA source-binding arguments are invalid")
    encoded = arguments[1]
    try:
        maximum_encoded = 4 * ((16 * 1024 + 2) // 3)
        if not encoded.isascii() or not encoded or len(encoded) > maximum_encoded:
            raise ValueError("binding argument is oversized")
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > 16 * 1024:
            raise ValueError("binding document is oversized")
        binding = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
        canonical = json.dumps(
            binding,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if not isinstance(binding, dict) or raw != canonical:
            raise ValueError("binding document is not canonical")
        app_target_sha = binding.get("app_target_sha")
        if not isinstance(app_target_sha, str) or SHA_RE.fullmatch(app_target_sha) is None:
            raise ValueError("app target is invalid")
        if (
            binding.get("schema") != 1
            or binding.get("binding_mode") != "verified-noop"
            or binding.get("runner_sha") != runner_sha
            or app_target_sha == runner_sha
            or not isinstance(binding.get("baseline_identity"), dict)
            or binding["baseline_identity"].get("source_sha") != app_target_sha
        ):
            raise ValueError("binding identity is invalid")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError("live-QA source binding failed closed validation") from exc
    return app_target_sha, binding, encoded


def _load_verified_remote_dispatcher(
    manifest: dict[str, object],
) -> object:
    """Load the C2 validator only after the app manifest binds its exact bytes."""

    files = manifest.get("files")
    if not isinstance(files, dict) or not isinstance(files.get("platform/tools/platform_workflow_remote_dispatch.py"), str):
        raise RuntimeError("installed C2 dispatcher is absent from the live-QA manifest")
    metadata = _regular(
        REMOTE_DISPATCHER,
        mode=0o555,
        maximum=MAX_PAYLOAD_FILE_BYTES,
    )
    if metadata.st_nlink != 1 or _open_and_hash(
        REMOTE_DISPATCHER, maximum=MAX_PAYLOAD_FILE_BYTES
    ) != files["platform/tools/platform_workflow_remote_dispatch.py"]:
        raise RuntimeError("installed C2 dispatcher differs from the active app manifest")
    spec = importlib.util.spec_from_file_location(
        "_verified_platform_workflow_remote_dispatch",
        REMOTE_DISPATCHER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("installed C2 dispatcher cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_source_binding_schema(
    remote_dispatcher: object,
    runner_sha: str,
    app_target_sha: str,
    binding: dict[str, object] | None,
) -> dict[str, object] | None:
    payload: dict[str, object] = {"target_sha": runner_sha}
    if binding is not None:
        payload["source_binding"] = binding
    try:
        active_app_sha, baseline, _arguments = remote_dispatcher._source_binding_context(payload)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("C2 source-binding validation failed") from exc
    if active_app_sha != app_target_sha:
        raise RuntimeError("C2 source-binding app target differs from installed app")
    return baseline


def _validate_source_binding_under_lock(
    remote_dispatcher: object,
    runner_sha: str,
    app_target_sha: str,
    binding: dict[str, object] | None,
) -> None:
    """Re-read and compare the complete release tuple while the lock is held."""

    try:
        actual = remote_dispatcher._release_baseline()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError("active live-QA release tuple is unavailable") from exc
    if binding is None:
        if runner_sha != app_target_sha or actual.get("source_sha") != app_target_sha:
            raise RuntimeError("same-source live-QA binding is inconsistent")
        return
    try:
        validated_app_sha, expected_baseline, _arguments = remote_dispatcher._source_binding_context(
            {"target_sha": runner_sha, "source_binding": binding}
        )
        matches = remote_dispatcher._baseline_identity_matches(expected_baseline, actual)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("active live-QA release tuple changed") from exc
    if validated_app_sha != app_target_sha or not matches:
        raise RuntimeError("active live-QA release tuple differs from the authenticated binding")


def _require_release_lock_supervisor() -> None:
    """Prove the canonical lock is held using the manifest-bound lock helper.

    The environment marker is only a routing hint.  This fixed Bash invocation
    reuses the installed lock helper's ancestor, /proc/locks, and inode checks;
    it does not acquire or nest another flock.
    """

    _trusted_directory_chain(TRUSTED_LIVE_QA_ROOT)
    metadata = _regular(RELEASE_LOCK, mode=0o444, maximum=MAX_PAYLOAD_FILE_BYTES)
    if metadata.st_nlink != 1:
        raise RuntimeError("canonical release lock helper metadata is unsafe")
    script = 'source "$1" && platform_release_lock_supervisor_holds'
    try:
        result = subprocess.run(
            ["/usr/bin/bash", "-c", script, "platform-release-lock-check", str(RELEASE_LOCK)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"HOME": "/root", "LANG": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("canonical release lock supervisor could not be checked") from exc
    if result.returncode != 0:
        raise RuntimeError("canonical release lock is not held by its supervisor")


def _validate_bundle_and_mailbox() -> None:
    """Validate every secret-bearing input before executing the supervisor."""

    bundle_metadata = _regular(
        BUNDLE,
        mode=0o600,
        maximum=MAX_RELEASE_JSON_BYTES,
    )
    _regular(MAILBOX_HELPER, mode=0o500, maximum=256 * 1024)
    try:
        bundle = json.loads(
            _read_bounded_regular(
                BUNDLE,
                metadata=bundle_metadata,
                maximum=MAX_RELEASE_JSON_BYTES,
                mode=0o600,
            ).decode("ascii"),
            object_pairs_hook=lambda pairs: _strict_object(pairs),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError("live-QA bundle is invalid") from exc
    expected = {
        "version",
        "marker",
        "created_at",
        "email",
        "password",
        "mailbox_helper",
        "roster_accounts",
    }
    if not isinstance(bundle, dict) or set(bundle) != expected or bundle.get("version") != 1:
        raise RuntimeError("live-QA bundle schema is invalid")
    marker = bundle.get("marker")
    if not isinstance(marker, str) or MARKER_RE.fullmatch(marker) is None:
        raise RuntimeError("live-QA bundle marker is invalid")
    if bundle.get("mailbox_helper") != str(MAILBOX_HELPER):
        raise RuntimeError("live-QA bundle mailbox helper is not fixed")
    created_at = bundle.get("created_at")
    if not isinstance(created_at, str):
        raise RuntimeError("live-QA bundle timestamp is invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at):
        raise RuntimeError("live-QA bundle timestamp is invalid")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("live-QA bundle timestamp is invalid") from exc
    if created.tzinfo is None:
        raise RuntimeError("live-QA bundle timestamp is invalid")
    age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    if age < -60 or age > 4 * 60 * 60:
        raise RuntimeError("live-QA bundle is stale")
    email = bundle.get("email")
    if (
        not isinstance(email, str)
        or not email.isascii()
        or len(email) > 254
        or EMAIL_RE.fullmatch(email) is None
        or email.rsplit("@", 1)[1].lower() != "auth.old-sparky.com"
        or marker in email.lower()
    ):
        raise RuntimeError("live-QA bundle email is invalid")
    for name in ("password",):
        value = bundle.get(name)
        if not isinstance(value, str) or not 10 <= len(value) <= 128 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise RuntimeError("live-QA bundle password is invalid")
    accounts = bundle.get("roster_accounts")
    if not isinstance(accounts, list) or len(accounts) != 13:
        raise RuntimeError("live-QA bundle roster is invalid")
    seen: set[str] = set()
    seen_emails: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {"id", "email", "password"}:
            raise RuntimeError("live-QA bundle roster account is invalid")
        account_id = account.get("id")
        if not isinstance(account_id, str) or UUID_RE.fullmatch(account_id) is None:
            raise RuntimeError("live-QA bundle roster account ID is invalid")
        if account_id in seen:
            raise RuntimeError("live-QA bundle roster IDs are not unique")
        seen.add(account_id)
        account_email = account.get("email")
        if (
            not isinstance(account_email, str)
            or not account_email.isascii()
            or len(account_email) > 254
            or EMAIL_RE.fullmatch(account_email) is None
            or account_email.rsplit("@", 1)[1].lower() != "auth.old-sparky.com"
            or account_email.lower().count(marker) != 1
            or account_email in seen_emails
        ):
            raise RuntimeError("live-QA bundle roster email is invalid")
        seen_emails.add(account_email)
        account_password = account.get("password")
        if (
            not isinstance(account_password, str)
            or not 10 <= len(account_password) <= 128
            or any(ord(character) < 32 or ord(character) == 127 for character in account_password)
        ):
            raise RuntimeError("live-QA bundle roster password is invalid")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and SHA_RE.fullmatch(arguments[0]):
        mode = "direct-user"
        runner_sha = arguments[0]
        suffix = arguments[1:]
        payload_args: list[str] = []
        base_url = "https://old-sparky.com"
        provision = "false"
        marker = ""
    elif len(arguments) >= 2 and arguments[0] in {"resolve", "verify", "run", "run-locked", "run-launch"}:
        mode = arguments[0]
        runner_sha = arguments[1]
        tail = arguments[2:]
        if mode == "run-launch":
            if len(tail) not in {3, 5}:
                return 2
            base_url, provision, marker = tail[:3]
            suffix = tail[3:]
            payload_args = []
        else:
            suffix = tail[:2] if tail and tail[0] == "--source-binding-base64" else []
            payload_args = tail[2:] if suffix else (tail if mode == "run" else [])
            if (mode in {"resolve", "verify", "run-locked"} and tail and not suffix):
                return 2
            base_url = "https://old-sparky.com"
            provision = "false"
            marker = ""
    else:
        return 2
    if SHA_RE.fullmatch(runner_sha) is None:
        return 2
    try:
        app_target_sha, source_binding, encoded_binding = _source_binding_context(
            runner_sha, suffix
        )
    except RuntimeError:
        return 2
    if mode == "run-launch" and (
        base_url != "https://old-sparky.com"
        or provision not in {"true", "false"}
        or (provision == "true" and MARKER_RE.fullmatch(marker) is None)
        or (provision == "false" and marker != "")
    ):
        return 2
    try:
        if os.geteuid() != 0:
            return 1
        manifest = _verify_install(app_target_sha)
        remote_dispatcher = _load_verified_remote_dispatcher(manifest)
        _validate_source_binding_schema(
            remote_dispatcher,
            runner_sha,
            app_target_sha,
            source_binding,
        )
        if mode == "direct-user":
            _trusted_directory_chain(TRUSTED_LIVE_QA_ROOT)
            source_arguments = (
                [] if encoded_binding is None
                else ["--source-binding-base64", encoded_binding]
            )
            os.execve(
                str(RELEASE_LOCK_EXEC),
                [
                    str(RELEASE_LOCK_EXEC),
                    "--app-dir",
                    str(RUNTIME),
                    "--expected-sha",
                    app_target_sha,
                    "--",
                    "/usr/bin/python3.12",
                    "-I",
                    "-B",
                    str(Path(__file__).resolve()),
                    "run-locked",
                    runner_sha,
                    *source_arguments,
                ],
                {
                    "HOME": "/root",
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
            return 0
        if mode == "run":
            _trusted_directory_chain(TRUSTED_LIVE_QA_ROOT)
            source_arguments = (
                [] if encoded_binding is None
                else ["--source-binding-base64", encoded_binding]
            )
            os.execve(
                str(RELEASE_LOCK_EXEC),
                [
                    str(RELEASE_LOCK_EXEC),
                    "--app-dir",
                    str(RUNTIME),
                    "--expected-sha",
                    app_target_sha,
                    "--",
                    "/usr/bin/python3.12",
                    "-I",
                    "-B",
                    str(Path(__file__).resolve()),
                    "run-locked",
                    runner_sha,
                    *source_arguments,
                ],
                {
                    "HOME": "/root",
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
            return 0
        if mode in {"resolve", "verify"}:
            if mode == "resolve":
                print(
                    "LIVE_SOURCE_BINDING schema=1 "
                    f"runner_sha={runner_sha} app_target_sha={app_target_sha}"
                )
            return 0
        if mode in {"run-locked", "run-launch"}:
            if os.environ.get("PLATFORM_RELEASE_LOCK_SUPERVISED") != "1":
                return 1
            _require_release_lock_supervisor()
            _validate_source_binding_under_lock(
                remote_dispatcher,
                runner_sha,
                app_target_sha,
                source_binding,
            )
        _trusted_directory_chain(TRUSTED_LIVE_QA_ROOT)
        manifest = _read_manifest(app_target_sha)
        payload = Path(str(manifest["payload"]))
        encoded_digest = (
            None
            if encoded_binding is None
            else hashlib.sha256(
                json.dumps(
                    source_binding,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
        )
        source_arguments = (
            [] if encoded_binding is None
            else ["--source-binding-base64", encoded_binding]
        )
        if mode == "run-launch":
            wrapper = payload / "platform/tools/platform_live_launch_supervisor.sh"
            _regular(wrapper, mode=0o555, maximum=MAX_PAYLOAD_FILE_BYTES)
            environment = {
                "HOME": "/root",
                "LANG": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "PLATFORM_APP_DIR": str(RUNTIME),
                "PLATFORM_LIVE_CSP_QA_BUNDLE": str(BUNDLE),
                "PLATFORM_LIVE_QA_TARGET_SHA": app_target_sha,
                "PLATFORM_LIVE_QA_RUNNER_SHA": runner_sha,
                "PLATFORM_LIVE_QA_SOURCE_BINDING_BASE64": encoded_binding or "",
                "PLATFORM_LIVE_QA_SOURCE_BINDING_SHA256": encoded_digest or "",
                "PLATFORM_LIVE_QA_INSTALL_ROOT": str(payload),
                "PLATFORM_LIVE_PROVISION": provision,
                "PLATFORM_LIVE_MARKER": marker,
                "PLAYWRIGHT_LIVE_BASE_URL": base_url,
            }
            os.execve(
                str(wrapper),
                [str(wrapper), base_url, provision, marker, runner_sha, *source_arguments],
                environment,
            )
            return 0
        _validate_bundle_and_mailbox()
        wrapper = payload / "platform/tools/platform_live_user_qa.sh"
        _regular(wrapper, mode=0o555, maximum=MAX_PAYLOAD_FILE_BYTES)
        environment = {
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PLATFORM_APP_DIR": str(RUNTIME),
            "PLATFORM_LIVE_CSP_QA_BUNDLE": str(BUNDLE),
            "PLATFORM_LIVE_QA_TARGET_SHA": app_target_sha,
            "PLATFORM_LIVE_QA_RUNNER_SHA": runner_sha,
            "PLATFORM_LIVE_QA_SOURCE_BINDING_BASE64": encoded_binding or "",
            "PLATFORM_LIVE_QA_SOURCE_BINDING_SHA256": encoded_digest or "",
            "PLAYWRIGHT_LIVE_BASE_URL": "https://old-sparky.com",
            "PLATFORM_LIVE_QA_INSTALL_ROOT": str(payload),
        }
        os.execve(str(wrapper), [str(wrapper), *payload_args], environment)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
