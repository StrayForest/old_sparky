#!/usr/bin/env python3
"""Install and verify the immutable secret-bearing live-QA runtime.

This tool is the only release-side writer for ``/root/.oldsparky/liveqa``.
The release tree is an input only: no helper from the candidate checkout is
executed while credentials are present.  A release first publishes an
immutable, digest-manifested payload and then atomically replaces the fixed
root dispatcher/helper.  The dispatcher verifies this manifest against the
active release before it starts the browser contour.

The runtime is intentionally an artifact member (``liveqa-runtime``).  The
production host never runs npm, downloads browsers, or resolves dependencies.
An artifact without that member is a bootstrap failure, including the first
deployment; there is no host-image or source-checkout fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from uuid import uuid4


APP_DIR = Path("/opt/oldsparky/platform")
RELEASE_LOCK_PATH = Path("/run/lock/oldsparky-platform-release.lock")
TRUSTED_ROOT = Path("/root/.oldsparky/liveqa")
PAYLOAD_ROOT = TRUSTED_ROOT / "releases"
ACTIVE_MANIFEST = TRUSTED_ROOT / "active-manifest.json"
ACTIVE_POINTER = TRUSTED_ROOT / "active"
HELPER_PATH = TRUSTED_ROOT / "platform_live_user_qa_trusted.sh"
LAUNCH_HELPER_PATH = TRUSTED_ROOT / "platform_live_launch_trusted.sh"
DISPATCHER_PATH = TRUSTED_ROOT / "platform_live_user_qa_dispatch.py"
REMOTE_DISPATCHER_PATH = TRUSTED_ROOT / "platform_workflow_remote_dispatch.py"
REMOTE_INPUT_GUARD_PATH = TRUSTED_ROOT / "platform_workflow_input_guard.py"
RELEASE_LOCK_EXEC_PATH = TRUSTED_ROOT / "platform_release_lock_exec.sh"
RELEASE_LOCK_PATH = TRUSTED_ROOT / "platform_release_lock.sh"
MAILBOX_HELPER_PATH = TRUSTED_ROOT / "platform_live_qa_mailbox_helper.py"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
PAYLOAD_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STAGE_PATTERN = re.compile(r"^\.[0-9a-f]{40}\.install-[0-9a-f]{32}$")
ENTRYPOINT_TEMP_PATTERN = re.compile(
    r"^\.platform_live_(?:user_qa_trusted\.sh|user_qa_dispatch\.py|qa_mailbox_helper\.py)\.[0-9a-f]{32}\.tmp$"
)
MANIFEST_TEMP_PATTERN = re.compile(r"^\.active-manifest\.json\.[0-9a-f]{32}\.tmp$")
POINTER_TEMP_PATTERN = re.compile(r"^\.active\.[0-9a-f]{32}\.tmp$")
MAX_RELEASE_JSON_BYTES = 64 * 1024
MAX_RUNTIME_MANIFEST_BYTES = 256 * 1024
MAX_ACTIVE_MANIFEST_BYTES = 256 * 1024
MAX_FILE_BYTES = 768 * 1024 * 1024
MAX_RUNTIME_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 200_000
MAX_ADDITIONAL_PAYLOADS = 1
CHROMIUM_SANDBOX_RELATIVE = PurePosixPath(
    "runtime/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
)
# The immutable release member is rooted at ``liveqa-runtime``.  Once copied
# into the active payload it is rooted at ``runtime`` instead.  Keep these
# coordinates distinct: using the payload path while validating the artifact
# source makes the legitimate 04755 sandbox look like an unexpected set-id
# file and causes its tree digest to drift.
RUNTIME_SANDBOX_RELATIVE = PurePosixPath(
    "browsers/chromium-1228/chrome-linux64/chrome_sandbox"
)
CHROMIUM_SANDBOX_SIZE = 15232
CHROMIUM_SANDBOX_SHA256 = (
    "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3"
)
RUNTIME_MANIFEST_RELATIVE = PurePosixPath("runtime-manifest.json")
RUNTIME_BROWSER_ROOTS = frozenset(
    {
        "chromium-1228",
        "chromium_headless_shell-1228",
        "webkit-2311",
        "ffmpeg-1011",
    }
)
RUNTIME_REQUIRED_FILES = (
    "node/bin/node",
    "web/package-lock.json",
    "web/playwright.live.config.ts",
    "web/tests/smoke/live-user-journey.spec.ts",
    "web/tests/support/live-qa-origin.ts",
    "web/tests/support/live-qa-sandbox.ts",
    "web/node_modules/@playwright/test/package.json",
    "web/node_modules/playwright/package.json",
    "web/node_modules/playwright-core/package.json",
    "browsers/chromium-1228/chrome-linux64/chrome_sandbox",
)

# All executable/source files used by the trusted wrapper.  The API source
# and platform package trees are copied into the payload so DB fixture tools
# do not import a candidate release through current/tools or a checkout.
TOOL_FILES = (
    "platform_live_user_qa_trusted.sh",
    "platform_live_launch_trusted.sh",
    "platform_live_launch_supervisor.sh",
    "platform_live_user_qa.sh",
    "platform_live_browser_qa.sh",
    "platform_install_live_qa_user.sh",
    "platform_live_qa_guard.py",
    "platform_live_user_qa_dispatch.py",
    "platform_workflow_remote_dispatch.py",
    "platform_workflow_input_guard.py",
    "platform_release_lock_exec.sh",
    "platform_release_lock.sh",
    "platform_live_qa_runtime_install.py",
    "platform_safe_env_exec.py",
    "platform_live_qa_mailbox_helper.py",
    "platform_provision_live_csp_qa.py",
    "platform_recover_live_user_qa.py",
    "platform_cleanup_live_user_qa.py",
)
SOURCE_TREES = (
    "apps/platform_api",
    "python_packages",
)
SOURCE_FILES = (
    "deploy/apparmor/oldsparky-liveqa-chromium",
)
SECRET_NAME_PATTERN = re.compile(
    r"(?:^|/)(?:\.env(?:\.|$)|.*\.(?:pem|key|p12|pfx|sqlite|db))$",
    re.IGNORECASE,
)


class InstallerError(RuntimeError):
    """A trusted live-QA install cannot be proven safe."""


def _release_lock_supervisor_pid() -> int | None:
    """Return a live pathname-form flock owner in this process' ancestry."""

    try:
        lock_root_path = RELEASE_LOCK_PATH.parent
        lock_root = RELEASE_LOCK_PATH.parent.lstat()
        lock_file = RELEASE_LOCK_PATH.lstat()
        if (
            not stat.S_ISDIR(lock_root.st_mode)
            or lock_root.st_uid != 0
            or lock_root_path.resolve(strict=True) != lock_root_path
            or (
                stat.S_IMODE(lock_root.st_mode) & 0o022
                and not stat.S_IMODE(lock_root.st_mode) & stat.S_ISVTX
            )
            or stat.S_ISLNK(lock_file.st_mode)
            or not stat.S_ISREG(lock_file.st_mode)
            or lock_file.st_uid != 0
            or lock_file.st_gid != 0
            or lock_file.st_nlink != 1
            or stat.S_IMODE(lock_file.st_mode) != 0o600
        ):
            return None
        device = f"{os.major(lock_file.st_dev):x}:{os.minor(lock_file.st_dev):x}"
        inode = str(lock_file.st_ino)
    except OSError:
        return None

    process_pid = os.getpid()
    try:
        locks = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    while process_pid > 1:
        try:
            executable = Path(f"/proc/{process_pid}/exe").resolve()
            stat_text = Path(f"/proc/{process_pid}/stat").read_text(encoding="ascii")
            after_command = stat_text.rsplit(")", 1)[1].split()
            parent_pid = int(after_command[1])
        except (OSError, UnicodeError, ValueError, IndexError):
            return None
        if executable == Path("/usr/bin/flock"):
            for line in locks:
                fields = line.split()
                if len(fields) < 6 or fields[1:5] != ["FLOCK", "ADVISORY", "WRITE", str(process_pid)]:
                    continue
                lock_device = fields[5].split(":")
                if len(lock_device) != 3:
                    continue
                normalized_device = (
                    f"{int(lock_device[0], 16):x}:{int(lock_device[1], 16):x}"
                )
                if normalized_device == device and lock_device[2].lstrip("0") == inode.lstrip("0"):
                    return process_pid
        if parent_pid == process_pid:
            break
        process_pid = parent_pid
    return None


def _require_release_lock() -> None:
    if _release_lock_supervisor_pid() is None:
        raise InstallerError("canonical release lock is required")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstallerError("live-QA runtime manifest contains duplicate keys")
        result[key] = value
    return result


def _metadata(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise InstallerError("live-QA install path is unavailable") from exc


def _regular(
    path: Path,
    *,
    mode: int | None = None,
    maximum: int = MAX_FILE_BYTES,
    allow_sandbox: bool = False,
) -> os.stat_result:
    metadata = _metadata(path)
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
    maximum: int = MAX_FILE_BYTES,
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
        raise InstallerError("trusted live-QA file metadata is unsafe")


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
    """Read a root-owned regular file through an identity-bound descriptor."""

    _validate_regular_metadata(path, metadata, mode=mode, maximum=maximum)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise InstallerError("trusted live-QA file is unavailable or unsafe") from exc
    try:
        try:
            opened = os.fstat(descriptor)
            _validate_regular_metadata(path, opened, mode=mode, maximum=maximum)
            if _file_fingerprint(opened) != _file_fingerprint(metadata):
                raise InstallerError("trusted live-QA file changed while opening")
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
            raise InstallerError("trusted live-QA file is unavailable or unsafe") from exc
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > maximum:
        raise InstallerError("trusted live-QA file exceeds its size bound")
    if _file_fingerprint(after) != _file_fingerprint(opened):
        raise InstallerError("trusted live-QA file changed while reading")
    return raw


def _sandbox_digest(path: Path) -> str:
    metadata = _regular(path, allow_sandbox=True, maximum=MAX_FILE_BYTES)
    if (
        stat.S_IMODE(metadata.st_mode) != 0o4755
        or metadata.st_size != CHROMIUM_SANDBOX_SIZE
    ):
        raise InstallerError("Chromium sandbox metadata is unsafe")
    descriptor, opened = _open_source(path, allow_sandbox=True)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if os.fstat(descriptor).st_size != opened.st_size:
            raise InstallerError("Chromium sandbox changed while hashing")
    finally:
        os.close(descriptor)
    value = digest.hexdigest()
    if value != CHROMIUM_SANDBOX_SHA256:
        raise InstallerError("Chromium sandbox checksum is invalid")
    return value


def _directory(path: Path, *, mode: int | None = None) -> os.stat_result:
    metadata = _metadata(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise InstallerError("trusted live-QA directory metadata is unsafe")
    return metadata


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _trusted_chain(path: Path) -> None:
    if not path.is_absolute():
        raise InstallerError("trusted live-QA path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        _directory(current)


def _safe_release(app_dir: Path, release: Path) -> tuple[Path, str, str]:
    if not app_dir.is_absolute() or Path(os.path.abspath(app_dir)) != app_dir:
        raise InstallerError("application path is not canonical")
    _directory(app_dir)
    releases = app_dir / "releases"
    _directory(releases)
    try:
        resolved = release.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("release is unavailable") from exc
    if release != resolved or resolved.parent != releases:
        raise InstallerError("release is outside the production releases directory")
    if SLUG_PATTERN.fullmatch(resolved.name) is None:
        raise InstallerError("release slug is invalid")
    _directory(resolved)
    release_json = resolved / "RELEASE.json"
    release_json_metadata = _regular(
        release_json,
        maximum=MAX_RELEASE_JSON_BYTES,
    )
    try:
        payload = json.loads(
            _read_bounded_regular(
                release_json,
                metadata=release_json_metadata,
                maximum=MAX_RELEASE_JSON_BYTES,
            ).decode("ascii")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("release metadata is invalid") from exc
    source_sha = payload.get("source_git_commit") if isinstance(payload, dict) else None
    if not isinstance(source_sha, str) or SHA_PATTERN.fullmatch(source_sha) is None:
        raise InstallerError("release source SHA is invalid")
    if payload.get("release_slug") != resolved.name:
        raise InstallerError("release slug does not match release metadata")
    return resolved, source_sha, resolved.name


def _active_release(app_dir: Path) -> tuple[Path, str, str]:
    pointer = app_dir / "current"
    metadata = _metadata(pointer)
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
    ):
        raise InstallerError("active production release pointer is unsafe")
    return _safe_release(app_dir, pointer.resolve(strict=True))


def _open_source(path: Path, *, allow_sandbox: bool = False) -> tuple[int, os.stat_result]:
    metadata = _regular(path, allow_sandbox=allow_sandbox)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise InstallerError("trusted live-QA source is unavailable") from exc
    if (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns):
        os.close(descriptor)
        raise InstallerError("trusted live-QA source changed while opening")
    return descriptor, opened


def _digest_regular(path: Path, *, allow_sandbox: bool = False) -> str:
    descriptor, metadata = _open_source(path, allow_sandbox=allow_sandbox)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise InstallerError("trusted live-QA file exceeds its bound")
            digest.update(chunk)
        if os.fstat(descriptor).st_size != metadata.st_size:
            raise InstallerError("trusted live-QA file changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _copy_regular(
    source: Path,
    destination: Path,
    *,
    executable: bool | None = None,
    allow_sandbox: bool = False,
) -> str:
    descriptor, source_metadata = _open_source(source, allow_sandbox=allow_sandbox)
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise InstallerError("trusted live-QA file exceeds its bound")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output, view)
                    if written <= 0:
                        raise InstallerError("trusted live-QA file could not be copied")
                    view = view[written:]
            if os.fstat(descriptor).st_size != source_metadata.st_size:
                raise InstallerError("trusted live-QA source changed while copying")
            os.fchmod(output, 0o555 if executable else 0o444)
            os.fchown(output, 0, 0)
            os.fsync(output)
        finally:
            os.close(output)
    except OSError as exc:
        raise InstallerError("trusted live-QA file could not be copied") from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _validate_source_tree(source: Path, relative: str) -> None:
    root = source / relative
    _directory(root)
    count = 0
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(source).as_posix()
        if SECRET_NAME_PATTERN.search(rel):
            raise InstallerError("trusted live-QA source contains a secret-shaped file")
        metadata = _metadata(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise InstallerError("trusted live-QA source tree contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise InstallerError("trusted live-QA source directory metadata is unsafe")
            continue
        relative_name = PurePosixPath(rel).as_posix()
        allow_sandbox = relative_name == "liveqa-runtime/" + RUNTIME_SANDBOX_RELATIVE.as_posix()
        if path.name == "chrome_sandbox" and not allow_sandbox:
            raise InstallerError("trusted live-QA source contains an unexpected sandbox helper")
        _regular(path, allow_sandbox=allow_sandbox)
        count += 1
        total += metadata.st_size
        if count > MAX_FILES or total > MAX_RUNTIME_BYTES:
            raise InstallerError("trusted live-QA source tree exceeds its bound")


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    allow_sandbox: bool = False,
) -> dict[str, str]:
    _validate_source_tree(source.parent, source.name)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    digests: dict[str, str] = {}
    source_sandbox_relative = (
        RUNTIME_SANDBOX_RELATIVE if allow_sandbox else CHROMIUM_SANDBOX_RELATIVE
    )
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        metadata = _metadata(path)
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(mode=0o700)
            continue
        relative_name = PurePosixPath(relative.as_posix()).as_posix()
        is_sandbox = (
            allow_sandbox
            and PurePosixPath(relative_name)
            == source_sandbox_relative
        )
        digest = _copy_regular(
            path,
            target,
            executable=bool(metadata.st_mode & 0o111),
            allow_sandbox=is_sandbox,
        )
        digests[PurePosixPath(relative.as_posix()).as_posix()] = digest
    return digests


def _runtime_path_order_key(relative: str) -> tuple[str, ...]:
    """Order manifest paths by POSIX components, independent of host OS.

    Keep this tiny contract local because the installer is copied into the
    trusted runtime and must not rely on ambient imports.
    """

    return PurePosixPath(relative).parts


def _tree_digest(
    root: Path,
    *,
    maximum: int = MAX_RUNTIME_BYTES,
    ignored: frozenset[str] = frozenset(),
    sandbox_relative: PurePosixPath = CHROMIUM_SANDBOX_RELATIVE,
) -> tuple[str, dict[str, str]]:
    _directory(root)
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    count = 0
    total = 0
    for path in sorted(
        root.rglob("*"),
        key=lambda candidate: _runtime_path_order_key(
            candidate.relative_to(root).as_posix()
        ),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in ignored:
            continue
        metadata = _metadata(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise InstallerError("trusted live-QA payload contains a symlink")
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise InstallerError("trusted live-QA payload directory metadata is unsafe")
            digest.update(b"d\0")
            continue
        relative_name = PurePosixPath(relative).as_posix()
        is_sandbox = PurePosixPath(relative_name) == sandbox_relative
        _regular(path, allow_sandbox=is_sandbox)
        if path.name == "chrome_sandbox" and not is_sandbox:
            raise InstallerError("trusted live-QA payload contains an unexpected sandbox helper")
        count += 1
        total += metadata.st_size
        if count > MAX_FILES or total > maximum:
            raise InstallerError("trusted live-QA payload exceeds its bound")
        file_digest = (
            _sandbox_digest(path)
            if is_sandbox
            else _digest_regular(path, allow_sandbox=False)
        )
        files[relative] = file_digest
        digest.update(b"f\0" + bytes.fromhex(file_digest))
    return digest.hexdigest(), files


def _validate_runtime_source(root: Path) -> None:
    """Recheck the artifact member before any secret-bearing promotion."""

    _directory(root, mode=0o555)
    _validate_source_tree(root.parent, root.name)
    required = [*RUNTIME_REQUIRED_FILES]
    for relative in required:
        path = root / relative
        _regular(path, allow_sandbox=relative == RUNTIME_SANDBOX_RELATIVE.as_posix())
    for browser_root in RUNTIME_BROWSER_ROOTS:
        _directory(root / "browsers" / browser_root)
    allowed_top = {"node", "web", "browsers", RUNTIME_MANIFEST_RELATIVE.name}
    allowed_web_files = {
        "web/package-lock.json",
        "web/playwright.live.config.ts",
        "web/tests/smoke/live-user-journey.spec.ts",
        "web/tests/support/live-qa-origin.ts",
        "web/tests/support/live-qa-sandbox.ts",
    }
    allowed_package_roots = (
        "web/node_modules/@playwright/test",
        "web/node_modules/playwright",
        "web/node_modules/playwright-core",
    )
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        top = relative.split("/", 1)[0]
        if top not in allowed_top:
            raise InstallerError("live-QA runtime contains an unreviewed top-level member")
        if relative.startswith("node/") and relative not in {"node/bin", "node/bin/node"}:
            raise InstallerError("live-QA runtime contains an unreviewed Node member")
        if relative.startswith("browsers/"):
            parts = relative.split("/")
            if len(parts) < 2 or parts[1] not in RUNTIME_BROWSER_ROOTS:
                raise InstallerError("live-QA runtime contains an unreviewed browser")
        if relative.startswith("web/"):
            package_relative = relative.removeprefix("web/node_modules/")
            if relative in allowed_web_files or relative in {
                "web/tests",
                "web/tests/smoke",
                "web/tests/support",
                "web/node_modules",
                "web/node_modules/@playwright",
            }:
                continue
            if not any(
                package_relative == package_root.removeprefix("web/node_modules/")
                or package_relative.startswith(
                    package_root.removeprefix("web/node_modules/") + "/"
                )
                for package_root in allowed_package_roots
            ) or "/node_modules/" in package_relative:
                raise InstallerError("live-QA runtime contains an unreviewed web member")
    manifest_path = root / RUNTIME_MANIFEST_RELATIVE
    manifest_metadata = _regular(
        manifest_path,
        mode=0o444,
        maximum=MAX_RUNTIME_MANIFEST_BYTES,
    )
    try:
        manifest = json.loads(
            _read_bounded_regular(
                manifest_path,
                metadata=manifest_metadata,
                maximum=MAX_RUNTIME_MANIFEST_BYTES,
                mode=0o444,
            ).decode("ascii"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("live-QA runtime manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "node_version", "package_lock_sha256", "tree_sha256", "files"}
        or manifest.get("version") != 1
        or manifest.get("node_version") != "26.3.1"
        or not isinstance(manifest.get("package_lock_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["package_lock_sha256"])
        or not isinstance(manifest.get("tree_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["tree_sha256"])
        or not isinstance(manifest.get("files"), dict)
    ):
        raise InstallerError("live-QA runtime manifest schema is invalid")
    lock_digest = _digest_regular(root / "web/package-lock.json")
    tree_digest, files = _tree_digest(
        root,
        ignored=frozenset({RUNTIME_MANIFEST_RELATIVE.as_posix()}),
        sandbox_relative=RUNTIME_SANDBOX_RELATIVE,
    )
    if (
        manifest["package_lock_sha256"] != lock_digest
        or manifest["tree_sha256"] != tree_digest
        or manifest["files"] != files
    ):
        raise InstallerError("live-QA runtime manifest digest is invalid")


def _normalize_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        metadata = _metadata(path)
        if stat.S_ISDIR(metadata.st_mode):
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, 0o555, follow_symlinks=False)
        elif stat.S_ISREG(metadata.st_mode):
            os.chown(path, 0, 0, follow_symlinks=False)
            # Chromium's SUID sandbox is the only executable allowed to retain
            # set-id authority.  Its digest is checked by the runtime manifest.
            if (
                path.relative_to(root).as_posix()
                == CHROMIUM_SANDBOX_RELATIVE.as_posix()
            ):
                os.chmod(path, 0o4755, follow_symlinks=False)
            else:
                os.chmod(path, 0o555 if metadata.st_mode & 0o111 else 0o444, follow_symlinks=False)
    os.chown(root, 0, 0)
    os.chmod(root, 0o555)


def _write_manifest(path: Path, payload: dict[str, object], *, mode: int = 0o444) -> None:
    raw = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(raw) > MAX_ACTIVE_MANIFEST_BYTES:
        raise InstallerError("trusted live-QA manifest exceeds its bound")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise InstallerError("trusted live-QA manifest could not be written")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, 0)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_manifest() -> dict[str, object]:
    _trusted_chain(TRUSTED_ROOT)
    metadata = _regular(
        ACTIVE_MANIFEST,
        mode=0o444,
        maximum=MAX_ACTIVE_MANIFEST_BYTES,
    )
    try:
        payload = json.loads(
            _read_bounded_regular(
                ACTIVE_MANIFEST,
                metadata=metadata,
                maximum=MAX_ACTIVE_MANIFEST_BYTES,
                mode=0o444,
            ).decode("ascii"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("trusted live-QA manifest is invalid") from exc
    expected = {"version", "source_sha", "release_slug", "payload", "payload_tree_sha256", "files"}
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("version") != 1
        or not isinstance(payload.get("source_sha"), str)
        or SHA_PATTERN.fullmatch(payload["source_sha"]) is None
        or not isinstance(payload.get("release_slug"), str)
        or SLUG_PATTERN.fullmatch(payload["release_slug"]) is None
        or payload.get("payload") != str(PAYLOAD_ROOT / payload["source_sha"])
        or not isinstance(payload.get("payload_tree_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["payload_tree_sha256"])
        or not isinstance(payload.get("files"), dict)
    ):
        raise InstallerError("trusted live-QA manifest schema is invalid")
    files = payload["files"]
    if any(
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(component in {"", ".", ".."} for component in relative.split("/"))
        or not isinstance(file_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", file_digest)
        for relative, file_digest in files.items()
    ):
        raise InstallerError("trusted live-QA manifest file map is invalid")
    _validate_active_pointer(str(payload["source_sha"]))
    return payload


def _write_active_pointer(target: Path) -> None:
    if target.parent != PAYLOAD_ROOT or PAYLOAD_PATTERN.fullmatch(target.name) is None:
        raise InstallerError("trusted live-QA active generation is invalid")
    _directory(target, mode=0o555)
    temporary = TRUSTED_ROOT / f".active.{uuid4().hex}.tmp"
    try:
        # The pointer lives beside ``releases``.  Store the exact relative
        # generation path so both readlink consumers and resolved-path
        # validators agree on the active payload identity.
        pointer_target = f"{PAYLOAD_ROOT.name}/{target.name}"
        os.symlink(pointer_target, temporary)
        metadata = temporary.lstat()
        if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
            raise InstallerError("trusted live-QA active pointer metadata is unsafe")
        os.replace(temporary, ACTIVE_POINTER)
        _fsync_directory(TRUSTED_ROOT)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _validate_active_pointer(source_sha: str) -> None:
    try:
        metadata = ACTIVE_POINTER.lstat()
        pointer_target = os.readlink(ACTIVE_POINTER)
        resolved = ACTIVE_POINTER.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("trusted live-QA active generation pointer is unavailable") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or pointer_target != f"{PAYLOAD_ROOT.name}/{source_sha}"
        or resolved != PAYLOAD_ROOT / source_sha
    ):
        raise InstallerError("trusted live-QA active generation pointer is invalid")


def _validate_payload(payload: dict[str, object]) -> None:
    source_sha = str(payload["source_sha"])
    root = Path(str(payload["payload"]))
    if root != PAYLOAD_ROOT / source_sha:
        raise InstallerError("trusted live-QA payload path is invalid")
    _directory(root, mode=0o555)
    tree_digest, files = _tree_digest(root)
    if tree_digest != payload["payload_tree_sha256"] or files != payload["files"]:
        raise InstallerError("trusted live-QA payload digest does not match manifest")
    manifest_files = payload["files"]
    if not isinstance(manifest_files, dict):
        raise InstallerError("trusted live-QA manifest file map is invalid")
    required_files = {
        "platform/tools/platform_live_user_qa_trusted.sh",
        "platform/tools/platform_live_launch_trusted.sh",
        "platform/tools/platform_live_launch_supervisor.sh",
        "platform/tools/platform_live_user_qa_dispatch.py",
        "platform/tools/platform_workflow_remote_dispatch.py",
        "platform/tools/platform_workflow_input_guard.py",
        "platform/tools/platform_release_lock_exec.sh",
        "platform/tools/platform_release_lock.sh",
        "platform/tools/platform_live_browser_qa.sh",
        "platform/tools/platform_install_live_qa_user.sh",
        "platform/tools/platform_live_qa_guard.py",
        "platform/tools/platform_safe_env_exec.py",
        "platform/tools/platform_live_qa_mailbox_helper.py",
    }
    if not required_files.issubset(manifest_files):
        raise InstallerError("trusted live-QA manifest is missing a required entrypoint")
    for path, mode, relative in (
        (HELPER_PATH, 0o755, "platform/tools/platform_live_user_qa_trusted.sh"),
        (LAUNCH_HELPER_PATH, 0o755, "platform/tools/platform_live_launch_trusted.sh"),
        (DISPATCHER_PATH, 0o500, "platform/tools/platform_live_user_qa_dispatch.py"),
        (REMOTE_DISPATCHER_PATH, 0o555, "platform/tools/platform_workflow_remote_dispatch.py"),
        (REMOTE_INPUT_GUARD_PATH, 0o555, "platform/tools/platform_workflow_input_guard.py"),
        (RELEASE_LOCK_EXEC_PATH, 0o555, "platform/tools/platform_release_lock_exec.sh"),
        (RELEASE_LOCK_PATH, 0o444, "platform/tools/platform_release_lock.sh"),
        (MAILBOX_HELPER_PATH, 0o500, "platform/tools/platform_live_qa_mailbox_helper.py"),
    ):
        metadata = _regular(path, mode=mode)
        if metadata.st_nlink != 1:
            raise InstallerError("trusted live-QA entrypoint is hard-linked")
        if _digest_regular(path) != manifest_files.get(relative):
            raise InstallerError("trusted live-QA entrypoint is not bound to payload")


def _cleanup_temporary_files() -> int:
    """Remove only installer-owned atomic-rename leftovers."""

    if not os.path.lexists(TRUSTED_ROOT):
        return 0
    _directory(TRUSTED_ROOT, mode=0o700)
    removed = 0
    for entry in TRUSTED_ROOT.iterdir():
        if not (
            ENTRYPOINT_TEMP_PATTERN.fullmatch(entry.name)
            or MANIFEST_TEMP_PATTERN.fullmatch(entry.name)
            or POINTER_TEMP_PATTERN.fullmatch(entry.name)
        ):
            continue
        if POINTER_TEMP_PATTERN.fullmatch(entry.name):
            metadata = _metadata(entry)
            if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0:
                raise InstallerError("interrupted trusted live-QA pointer is unsafe")
        else:
            _regular(entry, maximum=MAX_ACTIVE_MANIFEST_BYTES)
        os.unlink(entry)
        removed += 1
    if removed:
        _fsync_directory(TRUSTED_ROOT)
    return removed


def _cleanup_staging(*, apply: bool) -> int:
    if not PAYLOAD_ROOT.exists():
        return 0
    _directory(PAYLOAD_ROOT)
    stages = [entry for entry in PAYLOAD_ROOT.iterdir() if STAGE_PATTERN.fullmatch(entry.name)]
    if len(stages) > 8:
        raise InstallerError("too many interrupted trusted live-QA installs")
    if not apply:
        return len(stages)
    for stage in stages:
        _directory(stage)
        for path in stage.rglob("*"):
            metadata = _metadata(path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_nlink != 1
            ):
                raise InstallerError("interrupted trusted live-QA install is unsafe")
            if stat.S_ISDIR(metadata.st_mode):
                os.chmod(path, 0o700)
            elif stat.S_ISREG(metadata.st_mode):
                os.chmod(path, 0o600)
        shutil.rmtree(stage)
    _fsync_directory(PAYLOAD_ROOT)
    return len(stages)


def _protected_shas(app_dir: Path) -> set[str]:
    values: set[str] = set()
    for pointer_name in ("current", "previous"):
        pointer = app_dir / pointer_name
        if not os.path.lexists(pointer):
            continue
        _metadata(pointer)
        try:
            _release, source_sha, _slug = _safe_release(app_dir, pointer.resolve(strict=True))
        except (OSError, InstallerError):
            raise InstallerError("release pointer cannot protect live-QA payload")
        values.add(source_sha)
    if os.path.lexists(ACTIVE_POINTER):
        metadata = _metadata(ACTIVE_POINTER)
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0:
            raise InstallerError("active live-QA generation pointer is unsafe")
        try:
            pointer_target = os.readlink(ACTIVE_POINTER)
            target = ACTIVE_POINTER.resolve(strict=True)
        except OSError as exc:
            raise InstallerError("active live-QA generation pointer is unavailable") from exc
        if (
            target.parent != PAYLOAD_ROOT
            or PAYLOAD_PATTERN.fullmatch(target.name) is None
            or pointer_target != f"{PAYLOAD_ROOT.name}/{target.name}"
        ):
            raise InstallerError("active live-QA generation pointer is invalid")
        values.add(target.name)
    return values


def _retention(app_dir: Path, *, apply: bool) -> int:
    if not PAYLOAD_ROOT.exists():
        return 0
    _directory(PAYLOAD_ROOT)
    protected = _protected_shas(app_dir)
    entries: list[Path] = []
    for entry in PAYLOAD_ROOT.iterdir():
        if STAGE_PATTERN.fullmatch(entry.name):
            continue
        if PAYLOAD_PATTERN.fullmatch(entry.name) is None:
            raise InstallerError("unexpected trusted live-QA payload entry")
        _directory(entry, mode=0o555)
        entries.append(entry)
    unprotected = sorted(
        (entry for entry in entries if entry.name not in protected),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )
    candidates = unprotected[MAX_ADDITIONAL_PAYLOADS:]
    if not apply:
        return len(candidates)
    for entry in candidates:
        _directory(entry, mode=0o555)
        for path in [entry, *entry.rglob("*")]:
            metadata = _metadata(path)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
                raise InstallerError("trusted live-QA retention target is unsafe")
            if stat.S_ISDIR(metadata.st_mode):
                os.chmod(path, 0o700)
            elif stat.S_ISREG(metadata.st_mode):
                os.chmod(path, 0o600)
        shutil.rmtree(entry)
    _fsync_directory(PAYLOAD_ROOT)
    return len(candidates)


def install(app_dir: Path, release: Path) -> dict[str, object]:
    _require_release_lock()
    resolved_release, source_sha, release_slug = _safe_release(app_dir, release)
    _trusted_chain(Path("/root"))
    trusted_parent = TRUSTED_ROOT.parent
    if os.path.lexists(trusted_parent):
        _directory(trusted_parent, mode=0o700)
    else:
        os.mkdir(trusted_parent, 0o700)
        os.chown(trusted_parent, 0, 0)
    if os.path.lexists(TRUSTED_ROOT):
        _directory(TRUSTED_ROOT, mode=0o700)
    else:
        os.mkdir(TRUSTED_ROOT, 0o700)
        os.chown(TRUSTED_ROOT, 0, 0)
    if os.path.lexists(PAYLOAD_ROOT):
        _directory(PAYLOAD_ROOT, mode=0o755)
    else:
        os.mkdir(PAYLOAD_ROOT, 0o755)
        os.chown(PAYLOAD_ROOT, 0, 0)
    _cleanup_temporary_files()
    _cleanup_staging(apply=True)

    source_platform = resolved_release
    runtime_source = source_platform / "liveqa-runtime"
    _validate_runtime_source(runtime_source)
    for relative in TOOL_FILES:
        source = source_platform / "tools" / relative
        _regular(source)
    for relative in SOURCE_TREES:
        _validate_source_tree(source_platform, relative)
    for relative in SOURCE_FILES:
        _regular(source_platform / relative)

    stage = PAYLOAD_ROOT / f".{source_sha}.install-{uuid4().hex}"
    stage.mkdir(mode=0o700)
    temporary_paths: list[Path] = []
    try:
        platform = stage / "platform"
        (platform / "tools").mkdir(mode=0o700, parents=True)
        files: dict[str, str] = {}
        for relative in TOOL_FILES:
            source = source_platform / "tools" / relative
            target = platform / "tools" / relative
            digest = _copy_regular(source, target, executable=relative.endswith(".sh"))
            files[f"platform/tools/{relative}"] = digest
        for relative in SOURCE_TREES:
            files.update({f"platform/{relative}/{name}": digest for name, digest in _copy_tree(source_platform / relative, platform / relative).items()})
        for relative in SOURCE_FILES:
            source = source_platform / relative
            target_file = platform / relative
            digest = _copy_regular(source, target_file, executable=False)
            files[f"platform/{relative}"] = digest
        runtime_digests = _copy_tree(
            runtime_source,
            stage / "runtime",
            allow_sandbox=True,
        )
        files.update({f"runtime/{name}": digest for name, digest in runtime_digests.items()})
        (stage / "source-sha").write_text(source_sha + "\n", encoding="ascii")
        os.chmod(stage / "source-sha", 0o444)
        files["source-sha"] = hashlib.sha256((source_sha + "\n").encode("ascii")).hexdigest()
        _normalize_tree(stage)
        for directory in [stage, *sorted((path for path in stage.rglob("*") if path.is_dir()), reverse=True)]:
            _fsync_directory(directory)
        tree_digest, actual_files = _tree_digest(stage)
        if actual_files != files:
            raise InstallerError("trusted live-QA staged digest bookkeeping drifted")
        target = PAYLOAD_ROOT / source_sha
        if os.path.lexists(target):
            _directory(target, mode=0o555)
            existing_digest, existing_files = _tree_digest(target)
            if existing_digest != tree_digest or existing_files != actual_files:
                raise InstallerError("existing trusted live-QA payload is stale or wrong")
            for path in [stage, *stage.rglob("*")]:
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o700)
                elif not path.is_symlink():
                    os.chmod(path, 0o600)
            shutil.rmtree(stage)
        else:
            os.rename(stage, target)
            _fsync_directory(PAYLOAD_ROOT)
        manifest = {
            "version": 1,
            "source_sha": source_sha,
            "release_slug": release_slug,
            "payload": str(target),
            "payload_tree_sha256": tree_digest,
            "files": actual_files,
        }
        # Entrypoints are replaced only after the complete payload is durable.
        for source_name, destination, mode in (
            ("platform_live_user_qa_trusted.sh", HELPER_PATH, 0o755),
            ("platform_live_launch_trusted.sh", LAUNCH_HELPER_PATH, 0o755),
            ("platform_live_user_qa_dispatch.py", DISPATCHER_PATH, 0o500),
            ("platform_workflow_remote_dispatch.py", REMOTE_DISPATCHER_PATH, 0o555),
            ("platform_workflow_input_guard.py", REMOTE_INPUT_GUARD_PATH, 0o555),
            ("platform_release_lock_exec.sh", RELEASE_LOCK_EXEC_PATH, 0o555),
            ("platform_release_lock.sh", RELEASE_LOCK_PATH, 0o444),
            ("platform_live_qa_mailbox_helper.py", MAILBOX_HELPER_PATH, 0o500),
        ):
            source = target / "platform/tools" / source_name
            temporary = TRUSTED_ROOT / f".{destination.name}.{uuid4().hex}.tmp"
            temporary_paths.append(temporary)
            _copy_regular(source, temporary, executable=source_name.endswith(".sh"))
            os.replace(temporary, destination)
            temporary_paths.remove(temporary)
            os.chmod(destination, mode)
            _fsync_directory(TRUSTED_ROOT)
        _write_manifest(ACTIVE_MANIFEST, manifest)
        # The generation directory is complete and its helper/dispatcher are
        # already durable before the single active-generation pointer switch.
        # A crash before this rename leaves the old pointer with the new
        # manifest/entrypoints, which every verifier rejects closed; a crash
        # after it leaves the new generation fully addressable.
        _write_active_pointer(target)
        _retention(app_dir, apply=True)
        _cleanup_staging(apply=True)
        _validate_payload(manifest)
        return manifest
    except BaseException:
        for temporary in temporary_paths:
            try:
                if os.path.lexists(temporary):
                    _regular(temporary, maximum=MAX_ACTIVE_MANIFEST_BYTES)
                    os.unlink(temporary)
            except (OSError, InstallerError):
                # A replacement or unexpected object is never followed or
                # removed during failure cleanup; the next reconcile fails
                # closed rather than guessing ownership.
                pass
        if temporary_paths:
            try:
                _fsync_directory(TRUSTED_ROOT)
            except OSError:
                pass
        if stage.exists() and not stage.is_symlink():
            for path in [stage, *stage.rglob("*")]:
                try:
                    if path.is_dir() and not path.is_symlink():
                        os.chmod(path, 0o700)
                    elif not path.is_symlink():
                        os.chmod(path, 0o600)
                except OSError:
                    pass
            shutil.rmtree(stage, ignore_errors=True)
        raise


def verify(app_dir: Path, target_sha: str) -> dict[str, object]:
    _require_release_lock()
    if SHA_PATTERN.fullmatch(target_sha) is None:
        raise InstallerError("live-QA target SHA is invalid")
    release, source_sha, release_slug = _active_release(app_dir)
    del release
    if source_sha != target_sha:
        raise InstallerError("active release does not match live-QA target SHA")
    manifest = _read_manifest()
    if manifest.get("source_sha") != target_sha:
        raise InstallerError("trusted live-QA manifest is stale")
    if manifest.get("release_slug") != release_slug:
        raise InstallerError("trusted live-QA manifest release is stale")
    _validate_payload(manifest)
    helper_metadata = _regular(HELPER_PATH, mode=0o755)
    dispatcher_metadata = _regular(DISPATCHER_PATH, mode=0o500)
    mailbox_metadata = _regular(MAILBOX_HELPER_PATH, mode=0o500)
    if (
        helper_metadata.st_nlink != 1
        or dispatcher_metadata.st_nlink != 1
        or mailbox_metadata.st_nlink != 1
    ):
        raise InstallerError("trusted live-QA entrypoint link count is unsafe")
    return manifest


def reconcile(app_dir: Path) -> dict[str, object]:
    """Rebuild the active generation from the immutable production release."""

    _require_release_lock()
    release, _source_sha, _release_slug = _active_release(app_dir)
    return install(app_dir, release)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Install trusted live-QA runtime")
    subparsers = command.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--app-dir", type=Path, default=APP_DIR)
    install_parser.add_argument("--release", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--app-dir", type=Path, default=APP_DIR)
    verify_parser.add_argument("--target-sha", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--app-dir", type=Path, default=APP_DIR)
    retention_parser = subparsers.add_parser("retention")
    retention_parser.add_argument("--app-dir", type=Path, default=APP_DIR)
    retention_parser.add_argument("--apply", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _require_release_lock()
        if args.command == "install":
            manifest = install(args.app_dir, args.release)
            print(f"LIVE_QA_RUNTIME_INSTALL status=passed source_sha={manifest['source_sha']}")
        elif args.command == "verify":
            manifest = verify(args.app_dir, args.target_sha)
            print(f"LIVE_QA_RUNTIME_VERIFY status=passed source_sha={manifest['source_sha']}")
        elif args.command == "reconcile":
            manifest = reconcile(args.app_dir)
            print(f"LIVE_QA_RUNTIME_RECONCILE status=passed source_sha={manifest['source_sha']}")
        else:
            removed = _retention(args.app_dir, apply=args.apply)
            print(f"LIVE_QA_RUNTIME_RETENTION status={'applied' if args.apply else 'preview'} removed={removed}")
        return 0
    except (InstallerError, OSError, ValueError) as exc:
        print(f"LIVE_QA_RUNTIME status=failed reason={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
