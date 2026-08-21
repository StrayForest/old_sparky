#!/usr/bin/env python3
"""Filesystem, provenance, locking, and privilege boundaries for live QA."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import signal
import shutil
import stat

# Every subprocess call below uses a fixed executable, component argv, and no shell.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from typing import Iterable, Iterator, NamedTuple
import urllib.error
import urllib.request
import zipfile
from uuid import UUID, uuid4


APP_DIR = Path("/opt/oldsparky/platform")
TRUSTED_REPO_ROOT = Path("/root/old_sparky")
TRUSTED_PLATFORM_ROOT = TRUSTED_REPO_ROOT / "platform"
TRUSTED_TOOLS_ROOT = TRUSTED_PLATFORM_ROOT / "tools"
TRUSTED_SECRET_ROOT = Path("/root/.oldsparky/liveqa")
LIVE_QA_USER = "oldsparky-liveqa"
RUNNER_CACHE_ROOT = Path("/var/lib/oldsparky-liveqa")
BUILD_NODE_ROOT = Path("/var/lib/oldsparky-build")
RUN_GATE_ROOT = Path("/run/oldsparky-liveqa")
LIVE_QA_SYSTEMD_UNIT = "oldsparky-liveqa-browser.service"
LIVE_QA_CGROUP = Path("/sys/fs/cgroup/system.slice") / LIVE_QA_SYSTEMD_UNIT
APPARMOR_RESTRICT_USERNS = Path(
    "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
)
UNPRIVILEGED_USERNS_CLONE = Path("/proc/sys/kernel/unprivileged_userns_clone")
APPARMOR_PROFILES = Path("/sys/kernel/security/apparmor/profiles")
LIVE_QA_APPARMOR_PROFILES = (
    "oldsparky-liveqa-chromium (unconfined)",
    "oldsparky-liveqa-chromium-headless (unconfined)",
)
NODE_VERSION = "26.3.1"
NODE_ARCHIVE_NAME = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_ARCHIVE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE_NAME}"
NODE_ARCHIVE_SHA256 = "55647180e4ae58ffeaa3294e89aa4abda7c371dfbd64b44cbdb022980177aae0"
NODE_ARCHIVE_SIZE = 33_252_032
PLAYWRIGHT_ARCHIVES = (
    (
        "chromium-1228",
        "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.55/linux64/chrome-linux64.zip",
        "13113b963ac22fffdad898a677591028e4397c46c1daa9e61811258eed6e35b5",
        185_646_494,
    ),
    (
        "chromium_headless_shell-1228",
        "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.55/linux64/chrome-headless-shell-linux64.zip",
        "410c9407d5de3fea80d9398666be06f2aa09154a3fa7b327dc254e336bb4c4b7",
        119_778_157,
    ),
    (
        "webkit-2311",
        "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/webkit/2311/webkit-ubuntu-24.04.zip",
        "b15753f528e990ca941cbd6d0c61a5dac09abcf04cbd8969ea077f1901aeac92",
        105_839_953,
    ),
    (
        "ffmpeg-1011",
        "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/ffmpeg/1011/ffmpeg-linux.zip",
        "ebc74fc5b94830176a3c2914ae96bd8bc7f6a91f4f33890230f84a172ee61ccc",
        2_376_500,
    ),
)
CHROMIUM_SANDBOX_RELATIVE = Path("browsers/chromium-1228/chrome-linux64/chrome_sandbox")
CHROMIUM_SANDBOX_SIZE = 15232
CHROMIUM_SANDBOX_SHA256 = (
    "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3"
)
STATE_NAME_PATTERN = re.compile(r"^live-user-qa\.[A-Za-z0-9]{6}$")
SETUP_NAME_PATTERN = re.compile(r"^\.live-user-qa\.setup-[0-9a-f]{32}$")
PUBLIC_GATE_NAME_PATTERN = re.compile(r"^public-live-qa\.[a-z0-9_]{8}$")
PUBLICATION_TEMP_PATTERN = re.compile(r"^\.(?:inventory|phase)\.json\.[a-z0-9_]{8}$")
MARKER_PATTERN = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RUNTIME_NAME_PATTERN = re.compile(r"^runtime-([0-9a-f]{40})$")
RUNTIME_TOMBSTONE_NAME_PATTERN = re.compile(
    r"^\.runtime-([0-9a-f]{40})\.pruning-([0-9a-f]{32})$"
)
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
MAX_JSON_BYTES = 64 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_MEMBER_BYTES = 768 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_SYMLINK_BYTES = 4 * 1024
MAX_TAR_ENTRIES = 20_000
MAX_TAR_MEMBER_BYTES = 768 * 1024 * 1024
MAX_TAR_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_NPM_FILE_BYTES = 256 * 1024 * 1024
LOCK_ENV_NAME = "PLATFORM_LIVE_QA_LOCK_FD"
MACHINE_LOCK_PATH = Path("/run/lock/oldsparky-liveqa.lock")
STATE_PHASE_FILE = "phase.json"
STATE_PHASE_SETUP = "setup"
STATE_PHASE_ROOT = "root-prepared"
STATE_PHASE_BROWSER_READY = "browser-ready"
STATE_PHASE_BROWSER_PUBLISHED = "browser-published"
ROOT_STATE_PHASES = frozenset(
    {
        STATE_PHASE_ROOT,
        STATE_PHASE_BROWSER_READY,
        STATE_PHASE_BROWSER_PUBLISHED,
    }
)
TRUSTED_WRAPPERS = frozenset(
    {
        "platform_live_browser_qa.sh",
        "platform_live_user_qa.sh",
        "platform_manual_live_auth_qa.sh",
        "platform_provision_live_csp_qa.sh",
    }
)
TRUSTED_RECOVERY_WRAPPERS = frozenset(
    {
        "platform_live_browser_qa.sh",
        "platform_live_user_qa.sh",
    }
)
PRODUCTION_IDENTITY_NAMES = frozenset(
    {
        "oldsparky",
        "oldsparky-platform",
        "oldsparky-api",
        "oldsparky-web",
        "oldsparky-worker",
    }
)
REQUIRED_PRODUCTION_IDENTITY_NAMES = frozenset({"oldsparky-platform"})
PASSTHROUGH_ENV = frozenset(
    {
        "CI",
        "PLATFORM_APP_DIR",
        "PLATFORM_LIVE_CSP_QA_BUNDLE",
        "PLATFORM_LIVE_USER_QA_MARKER",
        "PLAYWRIGHT_LIVE_BASE_URL",
    }
)
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class GuardError(RuntimeError):
    """A deliberately non-sensitive live-QA guard failure."""


@contextmanager
def _release_operation_lock(app_dir: Path) -> Iterator[Path]:
    """Join the install/rollback flock without importing under Python -I."""

    try:
        resolved_app = app_dir.resolve(strict=True)
        app_metadata = app_dir.lstat()
        shared = resolved_app / "shared"
        shared_metadata = shared.lstat()
        resolved_shared = shared.resolve(strict=True)
    except OSError as exc:
        raise GuardError("platform release lock boundary is unavailable") from exc
    if (
        resolved_app != app_dir
        or stat.S_ISLNK(app_metadata.st_mode)
        or not stat.S_ISDIR(app_metadata.st_mode)
        or app_metadata.st_uid != 0
        or app_metadata.st_gid != 0
        or stat.S_IMODE(app_metadata.st_mode) & 0o022
        or resolved_shared != shared
        or stat.S_ISLNK(shared_metadata.st_mode)
        or not stat.S_ISDIR(shared_metadata.st_mode)
        or shared_metadata.st_uid != 0
        or shared_metadata.st_gid != 0
        or stat.S_IMODE(shared_metadata.st_mode) & 0o022
    ):
        raise GuardError("platform release lock boundary metadata is unsafe")
    try:
        descriptor = os.open(
            shared,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise GuardError("platform release lock could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (shared_metadata.st_dev, shared_metadata.st_ino)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise GuardError("platform release lock changed during validation")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GuardError("another platform release operation holds the lock") from exc
        if os.path.lexists(shared / ".release-operation.json"):
            raise GuardError(
                "a pending platform release operation requires recovery"
            )
        yield resolved_app
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class RuntimeCacheEntry(NamedTuple):
    path: Path
    commit: str
    modified_at_ns: int
    fingerprint: tuple[int, int, int, int, int]


class RuntimeCacheTombstone(NamedTuple):
    path: Path
    commit: str
    fingerprint: tuple[int, int, int, int, int]


class RuntimeCacheRetentionPlan(NamedTuple):
    protected: tuple[RuntimeCacheEntry, ...]
    retained: tuple[RuntimeCacheEntry, ...]
    candidates: tuple[RuntimeCacheEntry, ...]
    tombstones: tuple[RuntimeCacheTombstone, ...]


def _read_kernel_contract(path: Path, *, maximum_bytes: int) -> str:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as exc:
        raise GuardError(
            "live QA Chromium sandbox kernel contract is unavailable"
        ) from exc
    if len(payload) > maximum_bytes:
        raise GuardError("live QA Chromium sandbox kernel contract is invalid")
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GuardError("live QA Chromium sandbox kernel contract is invalid") from exc


def validate_chromium_apparmor_contract() -> None:
    """Require Ubuntu's global restriction plus the two narrow QA profiles."""

    if _read_kernel_contract(APPARMOR_RESTRICT_USERNS, maximum_bytes=16).strip() != "1":
        raise GuardError(
            "global AppArmor user namespace restriction must remain enabled"
        )
    if (
        _read_kernel_contract(UNPRIVILEGED_USERNS_CLONE, maximum_bytes=16).strip()
        != "1"
    ):
        raise GuardError("unprivileged user namespaces must remain kernel-enabled")
    profiles = _read_kernel_contract(APPARMOR_PROFILES, maximum_bytes=1024 * 1024)
    active = profiles.splitlines()
    if any(active.count(profile) != 1 for profile in LIVE_QA_APPARMOR_PROFILES):
        raise GuardError("narrow live QA Chromium AppArmor profiles are not active")


def _write_all(descriptor: int, payload: bytes, *, failure: str) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise GuardError(failure)
        view = view[written:]


def _fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_root_secret_parent(path: Path) -> None:
    if not path.is_absolute() or path.parent != TRUSTED_SECRET_ROOT:
        raise GuardError("private live QA path must use the canonical secret directory")
    chain = (
        Path("/"),
        Path("/root"),
        TRUSTED_SECRET_ROOT.parent,
        TRUSTED_SECRET_ROOT,
    )
    for directory in chain:
        try:
            metadata = directory.lstat()
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise GuardError("private live QA directory chain is unavailable") from exc
        if (
            resolved != directory
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (
                directory
                in {Path("/root"), TRUSTED_SECRET_ROOT.parent, TRUSTED_SECRET_ROOT}
                and stat.S_IMODE(metadata.st_mode) != 0o700
            )
        ):
            raise GuardError("private live QA directory chain is unsafe")


def _read_private_json(
    path: Path, *, expected_uid: int = 0, mode: int = 0o600
) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GuardError("required private JSON is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size > MAX_JSON_BYTES
    ):
        raise GuardError("private JSON metadata is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _fingerprint(opened) != _fingerprint(metadata):
            raise GuardError("private JSON changed while opening")
        raw = b""
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        if len(raw) > MAX_JSON_BYTES or _fingerprint(after) != _fingerprint(opened):
            raise GuardError("private JSON changed while reading")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GuardError("private JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise GuardError("private JSON schema is invalid")
    return payload


def _write_new_private_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise GuardError("private JSON exceeds its size limit")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(
            descriptor,
            encoded,
            failure="private JSON could not be written",
        )
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise GuardError("private JSON publication metadata is unsafe")
    finally:
        os.close(descriptor)


def _phase_payload(*, marker: str, phase: str) -> dict[str, object]:
    return {"version": 1, "marker": marker, "phase": phase}


def _validate_phase(
    payload: dict[str, object],
    *,
    marker: str,
    allowed: frozenset[str],
) -> str:
    if (
        set(payload) != {"version", "marker", "phase"}
        or payload.get("version") != 1
        or payload.get("marker") != marker
        or not isinstance(payload.get("phase"), str)
        or payload["phase"] not in allowed
    ):
        raise GuardError("live QA state phase is invalid")
    return str(payload["phase"])


def _validated_publication_temps(directory: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for target in directory.iterdir():
        if not PUBLICATION_TEMP_PATTERN.fullmatch(target.name):
            continue
        metadata = target.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_JSON_BYTES
        ):
            raise GuardError("interrupted private JSON publication is unsafe")
        result.append(target)
    return tuple(sorted(result, key=lambda path: path.name))


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bundle_marker_and_helper(bundle_path: Path) -> tuple[str, Path]:
    _validate_root_secret_parent(bundle_path)
    payload = _read_private_json(bundle_path)
    if (
        set(payload)
        != {
            "version",
            "marker",
            "created_at",
            "email",
            "password",
            "mailbox_helper",
            "roster_accounts",
        }
        or payload.get("version") != 1
    ):
        raise GuardError("bundle schema is invalid")
    marker = payload.get("marker")
    helper = payload.get("mailbox_helper")
    if not isinstance(marker, str) or not MARKER_PATTERN.fullmatch(marker):
        raise GuardError("bundle marker is invalid")
    if not isinstance(helper, str) or not Path(helper).is_absolute():
        raise GuardError("bundle helper path is invalid")
    return marker, Path(helper)


def _sha256_regular(
    path: Path, *, expected_uid: int, expected_mode: int | None = None
) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GuardError("helper file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != expected_uid
        or (
            expected_mode is not None
            and stat.S_IMODE(metadata.st_mode) != expected_mode
        )
        or metadata.st_size > 1024 * 1024
    ):
        raise GuardError("helper file metadata is unsafe")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if _fingerprint(opened) != _fingerprint(metadata):
            raise GuardError("helper file changed while opening")
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(opened):
            raise GuardError("helper file changed while reading")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    try:
        # Executable and every option are fixed by the caller; no shell is used.
        return subprocess.run(  # nosec B603
            ["/usr/bin/git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LANG": "C", "PATH": SAFE_PATH},
            close_fds=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("source checkout provenance could not be verified") from exc


def _assert_root_controlled_path(path: Path, *, directory: bool) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GuardError("trusted checkout path is unavailable") from exc
    expected_kind = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if (
        resolved != path
        or not expected_kind
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (not directory and metadata.st_mode & (stat.S_ISUID | stat.S_ISGID))
        or (not directory and metadata.st_nlink != 1)
    ):
        raise GuardError("trusted checkout path metadata is unsafe")


def _run_clean(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 900
) -> None:
    try:
        # Callers construct component-wise argv from root-controlled paths.
        result = subprocess.run(  # nosec B603
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("deterministic live QA runtime command failed") from exc
    if result.returncode != 0:
        raise GuardError("deterministic live QA runtime command failed")


def _release_pointer_commit(pointer_name: str, app_dir: Path = APP_DIR) -> str:
    if pointer_name not in {"current", "previous"}:
        raise GuardError("release pointer name is invalid")
    pointer = app_dir / pointer_name
    try:
        metadata = pointer.lstat()
        link_value = os.readlink(pointer)
    except OSError as exc:
        raise GuardError(f"{pointer_name} release link is unavailable") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
    ):
        raise GuardError(f"{pointer_name} release link metadata is unsafe")
    target = Path(link_value)
    if not target.is_absolute():
        target = pointer.parent / target
    try:
        resolved = target.resolve(strict=True)
        releases = (app_dir / "releases").resolve(strict=True)
    except OSError as exc:
        raise GuardError(f"{pointer_name} release target is unavailable") from exc
    if resolved.parent != releases or resolved.name in {"", ".", ".."}:
        raise GuardError(
            f"{pointer_name} release target is outside the releases directory"
        )
    target_metadata = resolved.lstat()
    if (
        not stat.S_ISDIR(target_metadata.st_mode)
        or target_metadata.st_uid != 0
        or target_metadata.st_gid != 0
        or stat.S_IMODE(target_metadata.st_mode) & 0o022
    ):
        raise GuardError(f"{pointer_name} release target ownership is unsafe")
    release_json = resolved / "RELEASE.json"
    try:
        release_metadata = release_json.lstat()
    except OSError as exc:
        raise GuardError(f"{pointer_name} release metadata is unavailable") from exc
    if (
        stat.S_ISLNK(release_metadata.st_mode)
        or not stat.S_ISREG(release_metadata.st_mode)
        or release_metadata.st_nlink != 1
        or release_metadata.st_uid != 0
        or release_metadata.st_gid != 0
        or stat.S_IMODE(release_metadata.st_mode) & 0o022
        or release_metadata.st_size > MAX_JSON_BYTES
    ):
        raise GuardError(f"{pointer_name} release metadata is unsafe")
    try:
        descriptor = os.open(
            release_json,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = MAX_JSON_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(raw) > MAX_JSON_BYTES
            or _fingerprint(opened) != _fingerprint(release_metadata)
            or _fingerprint(after) != _fingerprint(opened)
        ):
            raise GuardError(f"{pointer_name} release metadata changed while reading")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"{pointer_name} release metadata is invalid") from exc
    commit = payload.get("source_git_commit") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise GuardError(f"{pointer_name} release source commit is invalid")
    return commit


def _active_release_commit(app_dir: Path = APP_DIR) -> str:
    return _release_pointer_commit("current", app_dir)


def _protected_release_commits(app_dir: Path = APP_DIR) -> frozenset[str]:
    commits = frozenset(
        {
            _release_pointer_commit("current", app_dir),
            _release_pointer_commit("previous", app_dir),
        }
    )
    if any(not re.fullmatch(r"[0-9a-f]{40}", commit) for commit in commits):
        raise GuardError("protected release commits must use 40 lowercase hex characters")
    return commits


def verify_checkout_provenance(platform_root: Path, *, app_dir: Path = APP_DIR) -> str:
    try:
        resolved_platform = platform_root.resolve(strict=True)
    except OSError as exc:
        raise GuardError("source checkout is unavailable") from exc
    if (
        resolved_platform != TRUSTED_PLATFORM_ROOT
        or platform_root != TRUSTED_PLATFORM_ROOT
    ):
        raise GuardError("live QA requires the fixed root-controlled checkout")
    for trusted_directory in (
        Path("/root"),
        TRUSTED_REPO_ROOT,
        TRUSTED_PLATFORM_ROOT,
        TRUSTED_TOOLS_ROOT,
    ):
        _assert_root_controlled_path(trusted_directory, directory=True)
    repo = Path(
        _git(resolved_platform, "rev-parse", "--show-toplevel").decode().strip()
    )
    try:
        repo = repo.resolve(strict=True)
    except OSError as exc:
        raise GuardError("source checkout root is unavailable") from exc
    if repo != TRUSTED_REPO_ROOT or resolved_platform != repo / "platform":
        raise GuardError("live QA must run from the repository platform checkout")
    head = _git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if not COMMIT_PATTERN.fullmatch(head):
        raise GuardError("source checkout HEAD is invalid")
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        "platform",
    )
    if status:
        raise GuardError("platform source checkout has tracked or untracked changes")
    if head != _active_release_commit(app_dir):
        raise GuardError("source checkout HEAD does not match the active release")
    return head


def verify_helper_binding(
    *,
    platform_root: Path,
    installed_helper: Path,
) -> None:
    _validate_root_secret_parent(installed_helper)
    source_helper = platform_root / "tools/platform_live_qa_mailbox_helper.py"
    source_digest = _sha256_regular(
        source_helper,
        expected_uid=0,
        expected_mode=0o755,
    )
    installed_digest = _sha256_regular(
        installed_helper,
        expected_uid=0,
        expected_mode=0o500,
    )
    if source_digest != installed_digest:
        raise GuardError("installed mailbox helper does not match reviewed source")


def liveqa_identity() -> tuple[int, int]:
    try:
        account = pwd.getpwnam(LIVE_QA_USER)
        primary_group = grp.getgrnam(LIVE_QA_USER)
    except KeyError as exc:
        raise GuardError(
            "dedicated oldsparky-liveqa system account is unavailable"
        ) from exc
    production_accounts: list[pwd.struct_passwd] = []
    production_groups: list[grp.struct_group] = []
    for name in sorted(PRODUCTION_IDENTITY_NAMES):
        try:
            production_accounts.append(pwd.getpwnam(name))
        except KeyError:
            if name in REQUIRED_PRODUCTION_IDENTITY_NAMES:
                raise GuardError(
                    "production identity boundary is unavailable"
                ) from None
        try:
            production_groups.append(grp.getgrnam(name))
        except KeyError:
            if name in REQUIRED_PRODUCTION_IDENTITY_NAMES:
                raise GuardError(
                    "production identity boundary is unavailable"
                ) from None
    passwd_matches = [
        entry for entry in pwd.getpwall() if entry.pw_uid == account.pw_uid
    ]
    group_matches = [
        entry for entry in grp.getgrall() if entry.gr_gid == account.pw_gid
    ]
    forbidden_uids = {0, *(entry.pw_uid for entry in production_accounts)}
    forbidden_gids = {
        0,
        *(entry.pw_gid for entry in production_accounts),
        *(entry.gr_gid for entry in production_groups),
    }
    if (
        account.pw_uid in forbidden_uids
        or account.pw_gid in forbidden_gids
        or account.pw_gid != primary_group.gr_gid
        or [entry.pw_name for entry in passwd_matches] != [LIVE_QA_USER]
        or [entry.gr_name for entry in group_matches] != [LIVE_QA_USER]
        or account.pw_dir != "/nonexistent"
        or account.pw_shell != "/usr/sbin/nologin"
        or LIVE_QA_USER in primary_group.gr_mem
    ):
        raise GuardError("dedicated live QA account has an unsafe identity contract")
    supplementary = [
        group.gr_name
        for group in grp.getgrall()
        if group.gr_gid != account.pw_gid and LIVE_QA_USER in group.gr_mem
    ]
    if supplementary:
        raise GuardError("dedicated live QA account must not have supplementary groups")
    return account.pw_uid, account.pw_gid


def _lock_path(bundle_path: Path) -> Path:
    _validate_root_secret_parent(bundle_path)
    return MACHINE_LOCK_PATH


def _validate_trusted_wrapper(argv: list[str], *, recovery: bool) -> None:
    if not argv:
        raise GuardError("locked exec requires a reviewed wrapper")
    executable = Path(argv[0])
    allowed = TRUSTED_RECOVERY_WRAPPERS if recovery else TRUSTED_WRAPPERS
    expected = TRUSTED_TOOLS_ROOT / executable.name
    if executable != expected or executable.name not in allowed:
        raise GuardError("locked exec target is not a reviewed live QA wrapper")
    _assert_root_controlled_path(executable, directory=False)
    if recovery:
        if len(argv) != 3 or argv[1] not in {"recover", "recover-setup"}:
            raise GuardError("recovery locked exec arguments are invalid")
        if executable.name == "platform_live_browser_qa.sh" and argv[1] != "recover":
            raise GuardError("public browser recovery arguments are invalid")
        if not Path(argv[2]).is_absolute():
            raise GuardError("recovery path must be absolute")


def _open_machine_lock_directory() -> int:
    lock_parent = MACHINE_LOCK_PATH.parent
    descriptor: int | None = None
    try:
        path_metadata = lock_parent.lstat()
        descriptor = os.open(
            lock_parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GuardError("live QA machine lock directory is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != 0
        or (opened.st_dev, opened.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or (stat.S_IMODE(opened.st_mode) & 0o022 and not opened.st_mode & stat.S_ISVTX)
    ):
        os.close(descriptor)
        raise GuardError("live QA machine lock directory metadata is unsafe")
    return descriptor


def _open_machine_lock() -> int:
    lock_path = MACHINE_LOCK_PATH
    parent_fd = _open_machine_lock_directory()
    try:
        descriptor = os.open(
            lock_path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise GuardError("live QA lock file metadata is unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise GuardError("another live QA operation holds the machine lock") from exc
    return descriptor


def _open_bundle_lock(bundle_path: Path) -> int:
    _lock_path(bundle_path)
    return _open_machine_lock()


def assert_bundle_lock(bundle_path: Path, descriptor: int) -> None:
    if descriptor < 3:
        raise GuardError("live QA lock descriptor is invalid")
    try:
        opened = os.fstat(descriptor)
        expected = _lock_path(bundle_path).lstat()
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise GuardError("live QA bundle lock is not held") from exc
    if (
        not stat.S_ISREG(expected.st_mode)
        or expected.st_uid != 0
        or expected.st_gid != 0
        or expected.st_nlink != 1
        or stat.S_IMODE(expected.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise GuardError("live QA lock does not match the bundle")


def locked_exec(bundle_path: Path, argv: list[str]) -> None:
    _validate_trusted_wrapper(argv, recovery=False)
    descriptor = _open_bundle_lock(bundle_path)
    os.set_inheritable(descriptor, True)
    child_env = {"LANG": "C.UTF-8", "PATH": SAFE_PATH, LOCK_ENV_NAME: str(descriptor)}
    for name in PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value is not None:
            child_env[name] = value
    try:
        # Root invokes only the absolute reviewed wrapper passed by that wrapper.
        os.execve(argv[0], argv, child_env)  # nosec B606
    except OSError:
        os.close(descriptor)
        raise


def _liveqa_process_ids() -> tuple[int, ...]:
    uid, _gid = liveqa_identity()
    result: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise GuardError("live QA process boundary could not be inspected") from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            status_text = (entry / "status").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            raise GuardError("live QA process boundary could not be inspected") from exc
        effective_uid: int | None = None
        for line in status_text.splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                if len(fields) != 5 or not all(
                    value.isdecimal() for value in fields[1:]
                ):
                    raise GuardError("live QA process identity is invalid")
                effective_uid = int(fields[2])
                break
        if effective_uid is None:
            raise GuardError("live QA process identity is unavailable")
        if effective_uid == uid:
            result.append(int(entry.name))
    return tuple(sorted(result))


def _liveqa_cgroup_process_ids() -> tuple[int, ...]:
    try:
        metadata = LIVE_QA_CGROUP.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise GuardError("live QA cgroup boundary could not be inspected") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise GuardError("live QA cgroup metadata is unsafe")
    procs = LIVE_QA_CGROUP / "cgroup.procs"
    try:
        descriptor = os.open(
            procs,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            raw = os.read(descriptor, 64 * 1024 + 1)
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise GuardError("live QA cgroup membership could not be inspected") from exc
    if len(raw) > 64 * 1024 or not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0:
        raise GuardError("live QA cgroup membership metadata is unsafe")
    try:
        values = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise GuardError("live QA cgroup membership is invalid") from exc
    if any(not value.isdecimal() or int(value) <= 0 for value in values):
        raise GuardError("live QA cgroup membership is invalid")
    return tuple(sorted({int(value) for value in values}))


def assert_liveqa_idle() -> None:
    if _liveqa_cgroup_process_ids() or _liveqa_process_ids():
        raise GuardError(
            "dedicated live QA cgroup or identity still has running processes"
        )


def _kill_liveqa_cgroup(*, timeout: float = 5.0) -> None:
    if not _liveqa_cgroup_process_ids():
        return
    kill_path = LIVE_QA_CGROUP / "cgroup.kill"
    try:
        descriptor = os.open(
            kill_path,
            os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if os.write(descriptor, b"1") != 1:
                raise GuardError("stale live QA cgroup could not be killed")
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        if _liveqa_cgroup_process_ids():
            raise GuardError(
                "stale live QA cgroup kill boundary is unavailable"
            ) from None
        return
    except OSError as exc:
        raise GuardError("stale live QA cgroup could not be killed") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _liveqa_cgroup_process_ids():
            return
        time.sleep(0.05)
    if _liveqa_cgroup_process_ids():
        raise GuardError("stale live QA cgroup survived SIGKILL")


def _terminate_liveqa_processes(*, timeout: float = 10.0) -> None:
    """Boundedly stop only the dedicated QA UID during explicit recovery."""

    for signal_value, wait_seconds in (
        (signal.SIGTERM, timeout / 2),
        (signal.SIGKILL, timeout / 2),
    ):
        pids = _liveqa_process_ids()
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal_value)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise GuardError(
                    "stale live QA process could not be terminated"
                ) from exc
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not _liveqa_process_ids():
                return
            time.sleep(0.05)
    assert_liveqa_idle()


def _reset_liveqa_systemd_unit() -> None:
    try:
        # The exact fixed unit contains only the dedicated browser cgroup.
        subprocess.run(  # nosec B603
            ["/usr/bin/systemctl", "reset-failed", LIVE_QA_SYSTEMD_UNIT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"LANG": "C", "PATH": SAFE_PATH},
            close_fds=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("live QA browser unit could not be reset") from exc


def _recover_stale_liveqa() -> None:
    # cgroup.kill is an atomic SIGKILL of every descendant, including any
    # process whose parent already exited. The UID sweep then catches a stale
    # process that escaped the fixed systemd service before paths are reclaimed.
    _kill_liveqa_cgroup()
    _terminate_liveqa_processes()
    if _liveqa_cgroup_process_ids():
        raise GuardError("stale live QA cgroup remains after recovery")
    _reset_liveqa_systemd_unit()


def recovery_locked_exec(bundle_path: Path, argv: list[str]) -> None:
    """Acquire the global lock before bounded stale-runner termination."""

    _validate_trusted_wrapper(argv, recovery=True)
    _validate_root_secret_parent(bundle_path)
    descriptor = _open_bundle_lock(bundle_path)
    try:
        # Lock contention proves another reviewed operation is active. Never
        # terminate the dedicated cgroup/UID until this process owns the
        # machine-wide lock protecting that operation.
        _recover_stale_liveqa()
        os.set_inheritable(descriptor, True)
        child_env = {
            "LANG": "C.UTF-8",
            "PATH": SAFE_PATH,
            LOCK_ENV_NAME: str(descriptor),
        }
        for name in PASSTHROUGH_ENV:
            value = os.environ.get(name)
            if value is not None:
                child_env[name] = value
        # Root invokes only the exact absolute recovery wrapper supplied in argv.
        os.execve(argv[0], argv, child_env)  # nosec B606
    except BaseException:
        os.close(descriptor)
        raise


def _manual_state_paths(bundle_path: Path) -> tuple[Path, ...]:
    return (
        bundle_path.with_name(f"{bundle_path.stem}.manual-auth-state.json"),
        bundle_path.with_name(f"{bundle_path.stem}.manual-auth-inventory.json"),
        bundle_path.with_name(f"{bundle_path.stem}.manual-auth-abort-inventory.json"),
    )


def _state_directories(bundle_path: Path) -> list[Path]:
    try:
        return sorted(
            (
                entry
                for entry in bundle_path.parent.iterdir()
                if entry.name.startswith("live-user-qa.")
                or entry.name.startswith(".live-user-qa.setup-")
            ),
            key=lambda path: path.name,
        )
    except OSError as exc:
        raise GuardError("live QA recovery directory could not be inspected") from exc


def _run_gate_directories() -> list[Path]:
    if not RUN_GATE_ROOT.exists():
        return []
    try:
        metadata = RUN_GATE_ROOT.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o711
            or RUN_GATE_ROOT.resolve(strict=True) != RUN_GATE_ROOT
        ):
            raise GuardError("live QA runtime gate root is unsafe")
        return sorted(
            (
                entry
                for entry in RUN_GATE_ROOT.iterdir()
                if entry.name.startswith(("live-user-qa.", "public-live-qa."))
            ),
            key=lambda path: path.name,
        )
    except OSError as exc:
        raise GuardError("live QA runtime gates could not be inspected") from exc


def assert_no_recovery_state(bundle_path: Path, *, include_manual: bool) -> None:
    if _state_directories(bundle_path) or _run_gate_directories():
        raise GuardError("retained automated live QA state requires exact recovery")
    if include_manual and any(
        path.exists() or path.is_symlink() for path in _manual_state_paths(bundle_path)
    ):
        raise GuardError("manual live QA state excludes this operation")


def _canonical_ids(value: object, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise GuardError("live QA inventory ID list is invalid")
    result: list[str] = []
    for candidate in value:
        if not isinstance(candidate, str) or not UUID_PATTERN.fullmatch(candidate):
            raise GuardError("live QA inventory contains a non-canonical UUID")
        try:
            if str(UUID(candidate)) != candidate:
                raise ValueError
        except ValueError as exc:
            raise GuardError("live QA inventory contains a non-canonical UUID") from exc
        result.append(candidate)
    if len(set(result)) != len(result):
        raise GuardError("live QA inventory contains duplicate IDs")
    return tuple(result)


def _validate_inventory(
    payload: dict[str, object], *, marker: str
) -> dict[str, tuple[str, ...]]:
    if set(payload) != {"version", "marker", "user_ids", "tournament_ids", "media_ids"}:
        raise GuardError("live QA inventory schema is invalid")
    if payload.get("version") != 1 or payload.get("marker") != marker:
        raise GuardError("live QA inventory marker is invalid")
    return {
        "user_ids": _canonical_ids(payload.get("user_ids"), maximum=32),
        "tournament_ids": _canonical_ids(payload.get("tournament_ids"), maximum=8),
        "media_ids": _canonical_ids(payload.get("media_ids"), maximum=32),
    }


def _validate_session_payload(payload: dict[str, object], *, marker: str) -> None:
    if (
        set(payload)
        != {
            "version",
            "marker",
            "cookie_name",
            "created_at",
            "expires_at",
            "roster_sessions",
            "workflow_player",
        }
        or payload.get("version") != 1
        or payload.get("marker") != marker
    ):
        raise GuardError("browser session fixture schema is invalid")


def validate_root_state(
    bundle_path: Path, state_dir: Path
) -> tuple[str, Path, Path | None]:
    marker, _helper = _bundle_marker_and_helper(bundle_path)
    if (
        not state_dir.is_absolute()
        or state_dir.parent != bundle_path.parent
        or not STATE_NAME_PATTERN.fullmatch(state_dir.name)
    ):
        raise GuardError("recovery state path is outside the exact bundle directory")
    try:
        metadata = state_dir.lstat()
    except OSError as exc:
        raise GuardError("recovery state directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or state_dir.resolve(strict=True) != state_dir
    ):
        raise GuardError("recovery state directory metadata is unsafe")
    publication_temps = _validated_publication_temps(state_dir)
    entries = {entry.name for entry in state_dir.iterdir()} - {
        path.name for path in publication_temps
    }
    if (
        not entries.issubset(
            {"inventory.json", "browser-sessions.json", STATE_PHASE_FILE}
        )
        or "inventory.json" not in entries
        or STATE_PHASE_FILE not in entries
    ):
        raise GuardError("recovery state directory has unexpected contents")
    _validate_phase(
        _read_private_json(state_dir / STATE_PHASE_FILE),
        marker=marker,
        allowed=ROOT_STATE_PHASES,
    )
    inventory = state_dir / "inventory.json"
    _validate_inventory(_read_private_json(inventory), marker=marker)
    sessions = state_dir / "browser-sessions.json"
    if sessions.exists() or sessions.is_symlink():
        _validate_session_payload(_read_private_json(sessions), marker=marker)
        return marker, inventory, sessions
    return marker, inventory, None


def _bundle_roster_ids(bundle_path: Path) -> tuple[str, tuple[str, ...]]:
    marker, _helper = _bundle_marker_and_helper(bundle_path)
    payload = _read_private_json(bundle_path)
    accounts = payload.get("roster_accounts")
    if not isinstance(accounts, list) or len(accounts) != 13:
        raise GuardError("bundle roster schema is invalid")
    ids: list[str] = []
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {"id", "email", "password"}:
            raise GuardError("bundle roster account schema is invalid")
        account_id = account.get("id")
        if not isinstance(account_id, str) or not UUID_PATTERN.fullmatch(account_id):
            raise GuardError("bundle roster account ID is invalid")
        ids.append(account_id)
    if len(set(ids)) != 13:
        raise GuardError("bundle roster account IDs are not unique")
    return marker, tuple(ids)


def prepare_root_state(bundle_path: Path) -> Path:
    marker, roster_ids = _bundle_roster_ids(bundle_path)
    parent = bundle_path.parent
    stage = parent / f".live-user-qa.setup-{uuid4().hex}"
    final: Path | None = None
    try:
        stage.mkdir(mode=0o700)
        os.chown(stage, 0, 0)
        os.chmod(stage, 0o700)
        _sync_directory(parent)
        inventory = stage / "inventory.json"
        _write_new_private_json(
            inventory,
            {
                "version": 1,
                "marker": marker,
                "user_ids": list(roster_ids),
                "tournament_ids": [],
                "media_ids": [],
            },
        )
        phase_path = stage / STATE_PHASE_FILE
        _write_new_private_json(
            phase_path,
            _phase_payload(marker=marker, phase=STATE_PHASE_SETUP),
        )
        _sync_directory(stage)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        for _attempt in range(128):
            suffix = "".join(secrets.choice(alphabet) for _ in range(6))
            candidate = parent / f"live-user-qa.{suffix}"
            if not candidate.exists() and not candidate.is_symlink():
                final = candidate
                break
        if final is None:
            raise GuardError("could not allocate a unique live QA state name")
        _publish_private_json(
            phase_path,
            _phase_payload(marker=marker, phase=STATE_PHASE_ROOT),
        )
        os.rename(stage, final)
        _sync_directory(parent)
        validate_root_state(bundle_path, final)
        return final
    except BaseException:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            try:
                for name in ("inventory.json", STATE_PHASE_FILE):
                    (stage / name).unlink(missing_ok=True)
                stage.rmdir()
                _sync_directory(parent)
            except OSError:
                # An interrupted publication is intentionally retained for the
                # explicit setup-recovery command.
                pass
        raise


def remove_setup_state(bundle_path: Path, setup_dir: Path) -> None:
    marker, roster_ids = _bundle_roster_ids(bundle_path)
    if (
        not setup_dir.is_absolute()
        or setup_dir.parent != bundle_path.parent
        or not SETUP_NAME_PATTERN.fullmatch(setup_dir.name)
    ):
        raise GuardError("setup recovery path is unsafe")
    metadata = setup_dir.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GuardError("setup recovery directory metadata is unsafe")
    publication_temps = _validated_publication_temps(setup_dir)
    entries = {entry.name for entry in setup_dir.iterdir()} - {
        path.name for path in publication_temps
    }
    if not entries.issubset({"inventory.json", STATE_PHASE_FILE}):
        raise GuardError("setup recovery directory has unexpected contents")
    inventory = setup_dir / "inventory.json"
    if "inventory.json" in entries:
        ids = _validate_inventory(_read_private_json(inventory), marker=marker)
        if ids != {
            "user_ids": roster_ids,
            "tournament_ids": (),
            "media_ids": (),
        }:
            raise GuardError("setup recovery inventory is not pristine")
    phase_path = setup_dir / STATE_PHASE_FILE
    if STATE_PHASE_FILE in entries:
        _validate_phase(
            _read_private_json(phase_path),
            marker=marker,
            allowed=frozenset({STATE_PHASE_SETUP, STATE_PHASE_ROOT}),
        )
    if "inventory.json" in entries:
        inventory.unlink()
    if STATE_PHASE_FILE in entries:
        phase_path.unlink()
    for temporary in publication_temps:
        temporary.unlink()
    setup_dir.rmdir()
    _sync_directory(setup_dir.parent)


def _ensure_root_directory(path: Path, mode: int) -> None:
    created = False
    try:
        path.mkdir(mode=mode, parents=False, exist_ok=False)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise GuardError("required live QA directory could not be prepared") from exc
    try:
        if created:
            os.chown(path, 0, 0)
            os.chmod(path, mode)
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GuardError("required live QA directory could not be prepared") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != mode
        or resolved != path
    ):
        raise GuardError("required live QA directory metadata is unsafe")


def prepare_browser_gate(bundle_path: Path, state_dir: Path) -> Path:
    assert_liveqa_idle()
    marker, inventory, sessions = validate_root_state(bundle_path, state_dir)
    if sessions is None:
        raise GuardError("browser session fixture is required before privilege drop")
    uid, gid = liveqa_identity()
    _ensure_root_directory(RUN_GATE_ROOT, 0o711)
    gate = RUN_GATE_ROOT / state_dir.name
    _publish_private_json(
        state_dir / STATE_PHASE_FILE,
        _phase_payload(marker=marker, phase=STATE_PHASE_BROWSER_READY),
    )
    try:
        gate.mkdir(mode=0o700)
        inventory_target = gate / "inventory.json"
        sessions_target = gate / "browser-sessions.json"
        shutil.copyfile(inventory, inventory_target)
        shutil.copyfile(sessions, sessions_target)
        for target in (inventory_target, sessions_target):
            os.chown(target, uid, gid)
            os.chmod(target, 0o600)
        for name in ("home", "tmp", "test-results"):
            directory = gate / name
            directory.mkdir(mode=0o700)
            os.chown(directory, uid, gid)
            os.chmod(directory, 0o700)
        _sync_directory(gate)
        os.chown(gate, uid, gid)
        os.chmod(gate, 0o700)
        _sync_directory(RUN_GATE_ROOT)
        payload = _read_private_json(inventory_target, expected_uid=uid)
        _validate_inventory(payload, marker=marker)
        _validate_session_payload(
            _read_private_json(sessions_target, expected_uid=uid), marker=marker
        )
    except BaseException:
        if gate.exists() and gate.is_dir() and not gate.is_symlink():
            shutil.rmtree(gate)
        raise
    _publish_private_json(
        state_dir / STATE_PHASE_FILE,
        _phase_payload(marker=marker, phase=STATE_PHASE_BROWSER_PUBLISHED),
    )
    return gate


def prepare_public_browser_gate() -> Path:
    assert_liveqa_idle()
    uid, gid = liveqa_identity()
    _ensure_root_directory(RUN_GATE_ROOT, 0o711)
    gate = Path(tempfile.mkdtemp(prefix="public-live-qa.", dir=RUN_GATE_ROOT))
    try:
        if not PUBLIC_GATE_NAME_PATTERN.fullmatch(gate.name):
            raise GuardError("public browser gate name is unsafe")
        for name in ("home", "tmp", "test-results"):
            directory = gate / name
            directory.mkdir(mode=0o700)
            os.chown(directory, uid, gid)
            os.chmod(directory, 0o700)
        _sync_directory(gate)
        # Ownership of the gate itself is the final atomic publication step.
        os.chown(gate, uid, gid)
        os.chmod(gate, 0o700)
        _sync_directory(RUN_GATE_ROOT)
    except BaseException:
        if gate.exists() and gate.is_dir() and not gate.is_symlink():
            shutil.rmtree(gate)
        raise
    return gate


def _walk_nofollow(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        names.sort()
        filenames.sort()
        for name in names:
            paths.append(directory_path / name)
        for name in filenames:
            paths.append(directory_path / name)
    return paths


def reclaim_browser_gate(state_dir: Path) -> Path | None:
    assert_liveqa_idle()
    if state_dir.parent == RUN_GATE_ROOT:
        gate = state_dir
    else:
        gate = RUN_GATE_ROOT / state_dir.name
    if (
        not (
            STATE_NAME_PATTERN.fullmatch(gate.name)
            or PUBLIC_GATE_NAME_PATTERN.fullmatch(gate.name)
        )
        or gate.parent != RUN_GATE_ROOT
    ):
        raise GuardError("browser gate path is unsafe")
    if not gate.exists() and not gate.is_symlink():
        return None
    uid, _gid = liveqa_identity()
    metadata = gate.lstat()
    # Root ownership denotes an interrupted publication or a prior reclaim.
    allowed_owner = {0, uid}
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in allowed_owner
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GuardError("browser gate ownership is unsafe")
    for target in reversed(_walk_nofollow(gate)):
        item = target.lstat()
        if stat.S_ISLNK(item.st_mode):
            os.lchown(target, 0, 0)
        elif stat.S_ISDIR(item.st_mode):
            os.chown(target, 0, 0, follow_symlinks=False)
            os.chmod(target, 0o700, follow_symlinks=False)
        elif stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                raise GuardError("browser gate contains an unsafe hard link")
            os.chown(target, 0, 0, follow_symlinks=False)
            os.chmod(target, 0o600, follow_symlinks=False)
        else:
            raise GuardError("browser gate contains an unsafe special file")
    os.chown(gate, 0, 0, follow_symlinks=False)
    os.chmod(gate, 0o700, follow_symlinks=False)
    return gate


def _publish_private_json(path: Path, payload: dict[str, object]) -> None:
    before = path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != 0
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise GuardError("inventory target metadata is unsafe")
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise GuardError("inventory publication exceeds its size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        if _fingerprint(path.lstat()) != _fingerprint(before):
            raise GuardError("inventory changed before atomic publication")
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def merge_browser_inventory(bundle_path: Path, state_dir: Path) -> None:
    marker, root_inventory, _sessions = validate_root_state(bundle_path, state_dir)
    phase = _validate_phase(
        _read_private_json(state_dir / STATE_PHASE_FILE),
        marker=marker,
        allowed=ROOT_STATE_PHASES,
    )
    gate_path = RUN_GATE_ROOT / state_dir.name
    browser_was_published = phase == STATE_PHASE_BROWSER_PUBLISHED
    if gate_path.exists() and not gate_path.is_symlink():
        uid, _gid = liveqa_identity()
        browser_was_published = browser_was_published or gate_path.lstat().st_uid == uid
    gate = reclaim_browser_gate(state_dir)
    if gate is None:
        return
    if not browser_was_published:
        # The gate ownership transfer is the final publication step. A
        # root-owned partial gate was never available to the browser runner,
        # so the root inventory remains the sole exact cleanup authority.
        return
    candidate_path = gate / "inventory.json"
    root_payload = _read_private_json(root_inventory)
    candidate = _read_private_json(candidate_path)
    root_ids = _validate_inventory(root_payload, marker=marker)
    candidate_ids = _validate_inventory(candidate, marker=marker)
    if (
        candidate_ids["user_ids"] != root_ids["user_ids"]
        or not set(root_ids["tournament_ids"]).issubset(candidate_ids["tournament_ids"])
        or not set(root_ids["media_ids"]).issubset(candidate_ids["media_ids"])
    ):
        raise GuardError("browser inventory is not a monotonic exact-ID extension")
    _publish_private_json(root_inventory, candidate)


def remove_browser_gate(state_dir: Path) -> None:
    if state_dir.parent == RUN_GATE_ROOT:
        gate = state_dir
    else:
        gate = RUN_GATE_ROOT / state_dir.name
    if (
        not (
            STATE_NAME_PATTERN.fullmatch(gate.name)
            or PUBLIC_GATE_NAME_PATTERN.fullmatch(gate.name)
        )
        or gate.parent != RUN_GATE_ROOT
    ):
        raise GuardError("browser gate path is unsafe")
    if not gate.exists() and not gate.is_symlink():
        return
    metadata = gate.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GuardError("reclaimed browser gate is unsafe")
    shutil.rmtree(gate)


def remove_public_browser_gate(gate: Path) -> None:
    if gate.parent != RUN_GATE_ROOT or not PUBLIC_GATE_NAME_PATTERN.fullmatch(
        gate.name
    ):
        raise GuardError("public browser recovery gate path is unsafe")
    reclaim_browser_gate(gate)
    remove_browser_gate(gate)


def remove_root_state(bundle_path: Path, state_dir: Path) -> None:
    _marker, inventory, sessions = validate_root_state(bundle_path, state_dir)
    publication_temps = _validated_publication_temps(state_dir)
    if (RUN_GATE_ROOT / state_dir.name).exists() or (
        RUN_GATE_ROOT / state_dir.name
    ).is_symlink():
        raise GuardError("browser gate must be removed before recovery state")
    for target in (sessions, inventory, state_dir / STATE_PHASE_FILE):
        if target is not None:
            target.unlink()
    for temporary in publication_temps:
        temporary.unlink()
    state_dir.rmdir()
    _sync_directory(state_dir.parent)


def _tree_digest(
    root: Path,
    *,
    ignored_relatives: frozenset[Path] = frozenset(),
) -> str:
    digest = hashlib.sha256()
    for target in sorted(
        _walk_nofollow(root), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative_path = target.relative_to(root)
        relative = relative_path.as_posix()
        if relative_path in ignored_relatives:
            continue
        metadata = target.lstat()
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"d\0")
        elif stat.S_ISLNK(metadata.st_mode):
            link = os.readlink(target)
            try:
                resolved_link = (target.parent / link).resolve(strict=True)
                resolved_link.relative_to(root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise GuardError("runtime cache contains an escaping symlink") from exc
            if Path(link).is_absolute():
                raise GuardError("runtime cache contains an escaping symlink")
            digest.update(b"l\0" + link.encode("utf-8") + b"\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"x\0" if metadata.st_mode & 0o111 else b"f\0")
            with target.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        else:
            raise GuardError("runtime cache source contains a special file")
    return digest.hexdigest()


def _validate_cache_tree_permissions(
    root: Path,
    *,
    sandbox_relative: Path | None = None,
) -> None:
    root_metadata = root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != 0
        or root_metadata.st_gid != 0
        or stat.S_IMODE(root_metadata.st_mode) != 0o555
    ):
        raise GuardError("runtime cache root permissions are unsafe")
    for target in _walk_nofollow(root):
        metadata = target.lstat()
        if (
            metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_dev != root_metadata.st_dev
        ):
            raise GuardError("runtime cache contains unsafe ownership or device")
        if stat.S_ISLNK(metadata.st_mode):
            try:
                link = os.readlink(target)
                if Path(link).is_absolute():
                    raise ValueError
                (target.parent / link).resolve(strict=True).relative_to(
                    root.resolve(strict=True)
                )
            except (OSError, ValueError) as exc:
                raise GuardError("runtime cache symlink escapes its tree") from exc
        elif stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o555:
                raise GuardError("runtime cache directory permissions are unsafe")
        elif stat.S_ISREG(metadata.st_mode):
            expected_modes = {0o444, 0o555}
            relative = target.relative_to(root)
            if sandbox_relative is not None and relative == sandbox_relative:
                expected_modes = {0o4755}
            elif metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
                raise GuardError("runtime cache contains an unexpected set-id file")
            if target.name == "chrome_sandbox" and relative != sandbox_relative:
                raise GuardError("runtime cache contains an unexpected sandbox helper")
            if (
                metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) not in expected_modes
            ):
                raise GuardError("runtime cache file permissions are unsafe")
        else:
            raise GuardError("runtime cache contains a special file")


def _normalize_cache_tree(root: Path, *, sandbox: Path | None = None) -> None:
    for target in reversed(_walk_nofollow(root)):
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            os.lchown(target, 0, 0)
        elif stat.S_ISDIR(metadata.st_mode):
            os.chown(target, 0, 0, follow_symlinks=False)
            # Read/execute is intentional; only root retains mutation rights.
            os.chmod(target, 0o555, follow_symlinks=False)  # nosec B103
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise GuardError("runtime cache input contains an unsafe hard link")
            os.chown(target, 0, 0, follow_symlinks=False)
            if sandbox is not None and target == sandbox:
                # The checksum-verified canonical Chromium helper requires setuid.
                os.chmod(target, 0o4755, follow_symlinks=False)  # nosec B103
            else:
                if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    raise GuardError(
                        "runtime cache input contains an unexpected set-id file"
                    )
                if target.name == "chrome_sandbox":
                    raise GuardError(
                        "runtime cache input contains an unexpected sandbox helper"
                    )
                os.chmod(
                    target,
                    0o555 if metadata.st_mode & 0o111 else 0o444,
                    follow_symlinks=False,
                )
        else:
            raise GuardError("runtime cache contains a special file")
    os.chown(root, 0, 0)
    # The published runtime root is immutable but readable/executable.
    os.chmod(root, 0o555)  # nosec B103


def _write_immutable_manifest(
    path: Path,
    payload: dict[str, object],
    *,
    failure: str,
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii")
        _write_all(descriptor, raw, failure=failure)
        # Root production shells intentionally use umask 0077. The manifest is
        # non-secret provenance and must remain readable by the unprivileged QA
        # runner after the cache tree becomes root-owned and immutable.
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_tracked_web(platform_root: Path, target: Path) -> None:
    repo = platform_root.parent
    prefix = Path("platform/apps/platform_web")
    raw_paths = _git(repo, "ls-files", "-z", "--", str(prefix)).split(b"\0")
    paths = [Path(value.decode("utf-8")) for value in raw_paths if value]
    if not paths:
        raise GuardError("tracked web runner source is unavailable")
    web_target = target / "web"
    web_target.mkdir()
    for repo_relative in paths:
        try:
            web_relative = repo_relative.relative_to(prefix)
        except ValueError as exc:
            raise GuardError("tracked web runner path escaped its prefix") from exc
        if not web_relative.parts or any(
            part in {"", ".", ".."} for part in web_relative.parts
        ):
            raise GuardError("tracked web runner path is unsafe")
        source = repo / repo_relative
        destination = web_target / web_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = source.lstat()
        if stat.S_ISREG(metadata.st_mode):
            shutil.copyfile(source, destination)
            os.chmod(destination, stat.S_IMODE(metadata.st_mode))
        elif stat.S_ISLNK(metadata.st_mode):
            link = os.readlink(source)
            if Path(link).is_absolute():
                raise GuardError("tracked web runner symlink escapes its tree")
            try:
                (destination.parent / link).resolve(strict=False).relative_to(
                    web_target.resolve(strict=True)
                )
            except ValueError as exc:
                raise GuardError("tracked web runner symlink escapes its tree") from exc
            destination.symlink_to(link)
        else:
            raise GuardError("tracked web runner contains a special file")
    if not (web_target / "package-lock.json").is_file():
        raise GuardError("tracked web package lock is unavailable")


def _download_exact(
    *,
    url: str,
    expected_sha256: str,
    expected_size: int,
    archive: Path,
    label: str,
) -> None:
    digest = hashlib.sha256()
    try:
        # Every caller supplies a source-code constant HTTPS URL.
        request = urllib.request.Request(  # nosec B310
            url,
            headers={"User-Agent": "OldSparky-liveqa-runtime/1"},
        )
        # The URL, exact byte count, and digest are independently pinned above.
        with (
            urllib.request.urlopen(request, timeout=30) as response,  # nosec B310
            archive.open("xb") as output,
        ):  # nosec B310
            if response.geturl() != url:
                raise GuardError(f"{label} download redirected unexpectedly")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and (
                not content_length.isdecimal() or int(content_length) != expected_size
            ):
                raise GuardError(f"{label} download size header is invalid")
            total = 0
            while total <= expected_size:
                chunk = response.read(min(1024 * 1024, expected_size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise GuardError(f"{label} download exceeds its pinned size")
                digest.update(chunk)
                output.write(chunk)
            if total != expected_size:
                raise GuardError(f"{label} download size mismatch")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, urllib.error.URLError):
        archive.unlink(missing_ok=True)
        raise GuardError(f"{label} download failed") from None
    except GuardError:
        archive.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != expected_sha256:
        archive.unlink(missing_ok=True)
        raise GuardError(f"{label} checksum mismatch")


def _download_node_runtime(target: Path) -> None:
    archive = target / NODE_ARCHIVE_NAME
    _download_exact(
        url=NODE_ARCHIVE_URL,
        expected_sha256=NODE_ARCHIVE_SHA256,
        expected_size=NODE_ARCHIVE_SIZE,
        archive=archive,
        label="pinned Node runtime",
    )
    extract_root = target / ".node-extract"
    extract_root.mkdir()
    try:
        with tarfile.open(archive, mode="r:xz") as source:
            expected_root = f"node-v{NODE_VERSION}-linux-x64"
            members = source.getmembers()
            if len(members) > MAX_TAR_ENTRIES:
                raise GuardError("pinned Node archive has too many entries")
            seen: set[str] = set()
            total_size = 0
            for member in members:
                member_path = Path(member.name)
                canonical_name = PurePosixPath(*member_path.parts).as_posix()
                if (
                    not member_path.parts
                    or member_path.parts[0] != expected_root
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in member.name
                    or member.name.rstrip("/") != canonical_name
                    or canonical_name in seen
                ):
                    raise GuardError("pinned Node archive layout is unsafe")
                seen.add(canonical_name)
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    raise GuardError("pinned Node archive contains a special file")
                if member.mode & (stat.S_ISUID | stat.S_ISGID):
                    raise GuardError("pinned Node archive contains a set-id entry")
                if member.size < 0 or member.size > MAX_TAR_MEMBER_BYTES:
                    raise GuardError(
                        "pinned Node archive member exceeds its size limit"
                    )
                total_size += member.size
                if total_size > MAX_TAR_UNCOMPRESSED_BYTES:
                    raise GuardError("pinned Node archive exceeds its extraction limit")
            # Python's data filter is defense in depth after pinned hash/layout checks.
            source.extractall(extract_root, members=members, filter="data")
        os.rename(extract_root / expected_root, target / "node")
    finally:
        archive.unlink(missing_ok=True)
        if extract_root.exists():
            shutil.rmtree(extract_root)


def _validated_zip_members(
    source: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], str]]:
    members = source.infolist()
    if len(members) > MAX_ZIP_ENTRIES:
        raise GuardError("pinned Playwright archive has too many entries")
    seen: dict[str, str] = {}
    result: list[tuple[zipfile.ZipInfo, tuple[str, ...], str]] = []
    total_size = 0
    for member in members:
        raw_name = member.filename
        directory_entry = member.is_dir()
        if (
            not raw_name
            or raw_name.startswith(("/", "\\"))
            or "\\" in raw_name
            or "\x00" in raw_name
            or member.flag_bits & 0x1
        ):
            raise GuardError("pinned Playwright archive layout is unsafe")
        raw_parts = raw_name.split("/")
        if directory_entry and raw_parts[-1] == "":
            raw_parts = raw_parts[:-1]
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise GuardError("pinned Playwright archive layout is unsafe")
        posix_path = PurePosixPath(*raw_parts)
        canonical = posix_path.as_posix()
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise GuardError("pinned Playwright archive contains a set-id entry")
        if directory_entry:
            if member.file_size != 0 or file_type not in {0, stat.S_IFDIR}:
                raise GuardError("pinned Playwright archive directory is invalid")
            kind = "directory"
        elif file_type == stat.S_IFLNK:
            if member.file_size > MAX_ZIP_SYMLINK_BYTES:
                raise GuardError("pinned Playwright archive symlink is too large")
            kind = "symlink"
        elif file_type in {0, stat.S_IFREG}:
            kind = "file"
        else:
            raise GuardError("pinned Playwright archive contains a special file")
        if member.file_size < 0 or member.file_size > MAX_ZIP_MEMBER_BYTES:
            raise GuardError("pinned Playwright archive member exceeds its size limit")
        total_size += member.file_size
        if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise GuardError("pinned Playwright archive exceeds its extraction limit")
        if canonical in seen:
            raise GuardError("pinned Playwright archive contains a duplicate path")
        parents = ["/".join(raw_parts[:index]) for index in range(1, len(raw_parts))]
        if any(seen.get(parent) not in {None, "directory"} for parent in parents):
            raise GuardError("pinned Playwright archive path crosses a file")
        if kind != "directory" and any(
            existing.startswith(f"{canonical}/") for existing in seen
        ):
            raise GuardError("pinned Playwright archive path replaces a directory")
        seen[canonical] = kind
        result.append((member, tuple(raw_parts), kind))
    return result


def _download_pinned_zip(
    url: str,
    expected_sha256: str,
    expected_size: int,
    target: Path,
) -> None:
    archive = target.with_suffix(".zip")
    _download_exact(
        url=url,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        archive=archive,
        label="pinned Playwright archive",
    )
    target.mkdir()
    try:
        with zipfile.ZipFile(archive) as source:
            members = _validated_zip_members(source)
            resolved_target = target.resolve(strict=True)
            for member, member_parts, kind in members:
                destination = target.joinpath(*member_parts)
                mode = member.external_attr >> 16
                if kind == "directory":
                    destination.mkdir(parents=True, exist_ok=True)
                    os.chmod(destination, 0o755)  # nosec B103
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    try:
                        link = source.read(member).decode("utf-8")
                    except (UnicodeError, zipfile.BadZipFile) as exc:
                        raise GuardError(
                            "pinned Playwright archive symlink is invalid"
                        ) from exc
                    if not link or "\x00" in link or Path(link).is_absolute():
                        raise GuardError("pinned Playwright archive symlink is unsafe")
                    try:
                        (destination.parent / link).resolve(strict=False).relative_to(
                            resolved_target
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise GuardError(
                            "pinned Playwright archive symlink escapes"
                        ) from exc
                    destination.symlink_to(link)
                else:
                    with (
                        source.open(member) as input_file,
                        destination.open("xb") as output,
                    ):
                        remaining = member.file_size
                        while remaining:
                            chunk = input_file.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise GuardError(
                                    "pinned Playwright archive member was truncated"
                                )
                            output.write(chunk)
                            remaining -= len(chunk)
                        if input_file.read(1):
                            raise GuardError(
                                "pinned Playwright archive member exceeded its bound"
                            )
                    os.chmod(
                        destination,
                        0o755 if mode & 0o111 else 0o644,
                    )
        (target / "INSTALLATION_COMPLETE").touch(mode=0o644, exist_ok=False)
    except (OSError, zipfile.BadZipFile) as exc:
        raise GuardError("pinned Playwright archive extraction failed") from exc
    finally:
        archive.unlink(missing_ok=True)


def _assert_playwright_revision(web: Path) -> None:
    try:
        payload = json.loads(
            (web / "node_modules/playwright-core/browsers.json").read_text(
                encoding="utf-8"
            )
        )
        browsers = {
            item["name"]: (item["revision"], item.get("browserVersion"))
            for item in payload["browsers"]
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GuardError(
            "lock-installed Playwright browser manifest is invalid"
        ) from exc
    expected = {
        "chromium": ("1228", "149.0.7827.55"),
        "chromium-headless-shell": ("1228", "149.0.7827.55"),
        "webkit": ("2311", "26.5"),
        "ffmpeg": ("1011", None),
    }
    if any(browsers.get(name) != revision for name, revision in expected.items()):
        raise GuardError(
            "Playwright browser revisions do not match the pinned archives"
        )


def _install_locked_dependencies(target: Path) -> Path:
    web = target / "web"
    node = target / "node/bin/node"
    npm_cli = target / "node/lib/node_modules/npm/bin/npm-cli.js"
    if not node.is_file() or not npm_cli.is_file():
        raise GuardError("pinned Node npm runtime is incomplete")
    npm_cache = target / ".npm-cache"
    npm_cache.mkdir(mode=0o700)
    clean_env = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PATH": f"{target / 'node/bin'}:/usr/bin:/bin",
        "npm_config_cache": str(npm_cache),
        "npm_config_fetch_retries": "0",
    }
    _run_clean(
        [
            "/usr/bin/prlimit",
            f"--fsize={MAX_NPM_FILE_BYTES}",
            "--",
            str(node),
            str(npm_cli),
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--fetch-retries=0",
        ],
        cwd=web,
        env=clean_env,
    )
    shutil.rmtree(npm_cache)
    _assert_playwright_revision(web)
    browsers = target / "browsers"
    browsers.mkdir()
    for directory_name, url, checksum, byte_size in PLAYWRIGHT_ARCHIVES:
        _download_pinned_zip(url, checksum, byte_size, browsers / directory_name)
    sandbox = target / CHROMIUM_SANDBOX_RELATIVE
    try:
        metadata = sandbox.lstat()
    except OSError as exc:
        raise GuardError("runtime cache sandbox helper is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != CHROMIUM_SANDBOX_SIZE
        or _sha256_regular(sandbox, expected_uid=0) != CHROMIUM_SANDBOX_SHA256
    ):
        raise GuardError("canonical Chromium sandbox helper checksum is invalid")
    helpers = list(target.rglob("chrome_sandbox"))
    if helpers != [sandbox]:
        raise GuardError("runtime input contains an unexpected sandbox helper")
    return sandbox


def _sandbox_path(runtime_cache: Path) -> Path:
    if runtime_cache.parent != RUNNER_CACHE_ROOT or not RUNTIME_NAME_PATTERN.fullmatch(
        runtime_cache.name
    ):
        raise GuardError("runtime cache path is unsafe")
    sandbox = runtime_cache / CHROMIUM_SANDBOX_RELATIVE
    try:
        sandboxes = list(runtime_cache.rglob("chrome_sandbox"))
    except OSError as exc:
        raise GuardError("runtime cache sandbox helper is unavailable") from exc
    if sandboxes != [sandbox]:
        raise GuardError("runtime cache sandbox helper is ambiguous")
    try:
        metadata = sandbox.lstat()
    except OSError as exc:
        raise GuardError("runtime cache sandbox helper is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o4755
        or metadata.st_size != CHROMIUM_SANDBOX_SIZE
        or _sha256_regular(sandbox, expected_uid=0, expected_mode=0o4755)
        != CHROMIUM_SANDBOX_SHA256
    ):
        raise GuardError("runtime cache sandbox helper metadata is unsafe")
    return sandbox


def prepare_runtime_cache(platform_root: Path, commit: str) -> Path:
    if not COMMIT_PATTERN.fullmatch(commit):
        raise GuardError("runtime cache commit is invalid")
    if platform_root != TRUSTED_PLATFORM_ROOT:
        raise GuardError("runtime cache requires the fixed root-controlled checkout")
    liveqa_identity()
    assert_liveqa_idle()
    _ensure_root_directory(RUNNER_CACHE_ROOT, 0o755)
    target = RUNNER_CACHE_ROOT / f"runtime-{commit}"
    manifest_path = target / ".manifest.json"
    if target.exists() or target.is_symlink():
        metadata = target.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise GuardError("existing live QA runtime cache is unsafe")
        manifest = _read_private_json(manifest_path, expected_uid=0, mode=0o444)
        if (
            set(manifest)
            != {
                "version",
                "source_commit",
                "tree_sha256",
                "node_archive_sha256",
                "package_lock_sha256",
                "playwright_browsers_sha256",
            }
            or manifest.get("version") != 1
            or manifest.get("source_commit") != commit
        ):
            raise GuardError("runtime cache manifest is invalid")
        expected = manifest.get("tree_sha256")
        if not isinstance(expected, str) or expected != _tree_digest(
            target,
            ignored_relatives=frozenset({Path(".manifest.json")}),
        ):
            raise GuardError("runtime cache content drifted from its manifest")
        package_lock_digest = hashlib.sha256(
            (platform_root / "apps/platform_web/package-lock.json").read_bytes()
        ).hexdigest()
        browsers_manifest_digest = hashlib.sha256(
            (target / "web/node_modules/playwright-core/browsers.json").read_bytes()
        ).hexdigest()
        if (
            manifest.get("node_archive_sha256") != NODE_ARCHIVE_SHA256
            or manifest.get("package_lock_sha256") != package_lock_digest
            or manifest.get("playwright_browsers_sha256") != browsers_manifest_digest
        ):
            raise GuardError("runtime cache dependency provenance is invalid")
        _validate_cache_tree_permissions(
            target,
            sandbox_relative=CHROMIUM_SANDBOX_RELATIVE,
        )
        _sandbox_path(target)
        return target
    stage = RUNNER_CACHE_ROOT / f".runtime-{commit}.building-{uuid4().hex}"
    try:
        stage.mkdir(mode=0o700)
        _copy_tracked_web(platform_root, stage)
        _download_node_runtime(stage)
        sandbox = _install_locked_dependencies(stage)
        # No reusable QA-UID process may race the exact helper promotion.
        assert_liveqa_idle()
        _normalize_cache_tree(stage, sandbox=sandbox)
        tree_digest = _tree_digest(stage)
        package_lock_digest = hashlib.sha256(
            (stage / "web/package-lock.json").read_bytes()
        ).hexdigest()
        browsers_manifest_digest = hashlib.sha256(
            (stage / "web/node_modules/playwright-core/browsers.json").read_bytes()
        ).hexdigest()
        # Temporarily writable only by root while the manifest is published.
        os.chmod(stage, 0o755)  # nosec B103
        _write_immutable_manifest(
            stage / ".manifest.json",
            {
                "version": 1,
                "source_commit": commit,
                "tree_sha256": tree_digest,
                "node_archive_sha256": NODE_ARCHIVE_SHA256,
                "package_lock_sha256": package_lock_digest,
                "playwright_browsers_sha256": browsers_manifest_digest,
            },
            failure="runtime cache manifest could not be written",
        )
        # Published runtime directories are immutable but readable/executable.
        os.chmod(stage, 0o555)  # nosec B103
        os.rename(stage, target)
        parent_fd = os.open(
            RUNNER_CACHE_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        _validate_cache_tree_permissions(
            target,
            sandbox_relative=CHROMIUM_SANDBOX_RELATIVE,
        )
        _sandbox_path(target)
    except BaseException:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            os.chmod(stage, 0o700)
            for item in _walk_nofollow(stage):
                try:
                    if item.is_dir() and not item.is_symlink():
                        os.chmod(item, 0o700)
                    elif not item.is_symlink():
                        os.chmod(item, 0o600)
                except OSError:
                    pass
            shutil.rmtree(stage)
        raise
    return target


def _validate_runtime_cache_root(root: Path) -> None:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise GuardError("live QA runtime cache root is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or resolved != root
    ):
        raise GuardError("live QA runtime cache root metadata is unsafe")


def _validate_runtime_retention_entry(
    path: Path, *, root: Path
) -> RuntimeCacheEntry:
    match = RUNTIME_NAME_PATTERN.fullmatch(path.name)
    if path.parent != root or match is None:
        raise GuardError("runtime cache retention target name is unsafe")
    commit = match.group(1)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GuardError("runtime cache retention target is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o555
        or metadata.st_dev != root.lstat().st_dev
        or resolved != path
    ):
        raise GuardError("runtime cache retention target metadata is unsafe")
    _validate_cache_tree_permissions(
        path,
        sandbox_relative=CHROMIUM_SANDBOX_RELATIVE,
    )
    manifest_path = path / ".manifest.json"
    try:
        manifest_metadata = manifest_path.lstat()
    except OSError as exc:
        raise GuardError("runtime cache retention manifest is unavailable") from exc
    if (
        stat.S_ISLNK(manifest_metadata.st_mode)
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
        or manifest_metadata.st_uid != 0
        or manifest_metadata.st_gid != 0
        or stat.S_IMODE(manifest_metadata.st_mode) != 0o444
    ):
        raise GuardError("runtime cache retention manifest metadata is unsafe")
    manifest = _read_private_json(manifest_path, expected_uid=0, mode=0o444)
    digest_fields = {
        "tree_sha256",
        "node_archive_sha256",
        "package_lock_sha256",
        "playwright_browsers_sha256",
    }
    if (
        set(manifest) != {"version", "source_commit", *digest_fields}
        or manifest.get("version") != 1
        or manifest.get("source_commit") != commit
        or any(
            not isinstance(manifest.get(name), str)
            or DIGEST_PATTERN.fullmatch(str(manifest[name])) is None
            for name in digest_fields
        )
    ):
        raise GuardError("runtime cache retention manifest is invalid")
    return RuntimeCacheEntry(
        path=path,
        commit=commit,
        modified_at_ns=metadata.st_mtime_ns,
        fingerprint=_fingerprint(metadata),
    )


def _relative_link_stays_within(root: Path, target: Path, link: str) -> bool:
    if Path(link).is_absolute():
        return False
    depth = 0
    relative_parent = target.parent.relative_to(root)
    for part in (*relative_parent.parts, *PurePosixPath(link).parts):
        if part in {"", "."}:
            continue
        if part == "..":
            if depth == 0:
                return False
            depth -= 1
        else:
            depth += 1
    return True


def _validate_runtime_tombstone(
    path: Path, *, root: Path
) -> RuntimeCacheTombstone:
    match = RUNTIME_TOMBSTONE_NAME_PATTERN.fullmatch(path.name)
    if path.parent != root or match is None:
        raise GuardError("runtime cache tombstone name is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GuardError("runtime cache tombstone is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) not in {0o555, 0o700}
        or metadata.st_dev != root.lstat().st_dev
        or resolved != path
    ):
        raise GuardError("runtime cache tombstone metadata is unsafe")
    for target in _walk_nofollow(path):
        target_metadata = target.lstat()
        if (
            target_metadata.st_uid != 0
            or target_metadata.st_gid != 0
            or target_metadata.st_dev != metadata.st_dev
        ):
            raise GuardError("runtime cache tombstone has unsafe ownership")
        if stat.S_ISLNK(target_metadata.st_mode):
            try:
                link = os.readlink(target)
            except OSError as exc:
                raise GuardError("runtime cache tombstone symlink is unsafe") from exc
            if not _relative_link_stays_within(path, target, link):
                raise GuardError("runtime cache tombstone symlink is unsafe")
        elif stat.S_ISDIR(target_metadata.st_mode):
            if stat.S_IMODE(target_metadata.st_mode) not in {0o555, 0o700}:
                raise GuardError("runtime cache tombstone directory mode is unsafe")
        elif stat.S_ISREG(target_metadata.st_mode):
            relative = target.relative_to(path)
            allowed_modes = {0o444, 0o555}
            if relative == CHROMIUM_SANDBOX_RELATIVE:
                allowed_modes.add(0o4755)
            elif target_metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
                raise GuardError("runtime cache tombstone has an unexpected set-id file")
            if target.name == "chrome_sandbox" and relative != CHROMIUM_SANDBOX_RELATIVE:
                raise GuardError("runtime cache tombstone has an unexpected sandbox")
            if (
                target_metadata.st_nlink != 1
                or stat.S_IMODE(target_metadata.st_mode) not in allowed_modes
            ):
                raise GuardError("runtime cache tombstone file metadata is unsafe")
        else:
            raise GuardError("runtime cache tombstone contains a special file")
    return RuntimeCacheTombstone(
        path=path,
        commit=match.group(1),
        fingerprint=_fingerprint(metadata),
    )


def build_runtime_cache_retention_plan(
    root: Path,
    *,
    protected_commits: frozenset[str],
    keep: int,
) -> RuntimeCacheRetentionPlan:
    if keep < 1 or keep > 100:
        raise GuardError("runtime cache keep must be between 1 and 100")
    if any(COMMIT_PATTERN.fullmatch(commit) is None for commit in protected_commits):
        raise GuardError("protected runtime cache commit is invalid")
    if not os.path.lexists(root):
        return RuntimeCacheRetentionPlan((), (), (), ())
    _validate_runtime_cache_root(root)
    tombstones = tuple(
        _validate_runtime_tombstone(path, root=root)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if RUNTIME_TOMBSTONE_NAME_PATTERN.fullmatch(path.name)
    )
    entries = tuple(
        _validate_runtime_retention_entry(path, root=root)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if RUNTIME_NAME_PATTERN.fullmatch(path.name)
    )
    protected = tuple(entry for entry in entries if entry.commit in protected_commits)
    unprotected = sorted(
        (entry for entry in entries if entry.commit not in protected_commits),
        key=lambda entry: (-entry.modified_at_ns, entry.path.name),
    )
    retained = tuple(unprotected[:keep])
    candidates = tuple(unprotected[keep:])
    return RuntimeCacheRetentionPlan(protected, retained, candidates, tombstones)


def _make_runtime_tree_removable(path: Path) -> None:
    for target in reversed([path, *_walk_nofollow(path)]):
        metadata = target.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            os.chmod(target, 0o700, follow_symlinks=False)


def _remove_runtime_tombstone(
    tombstone: RuntimeCacheTombstone, *, root: Path
) -> None:
    current = _validate_runtime_tombstone(tombstone.path, root=root)
    if current.fingerprint != tombstone.fingerprint or current.commit != tombstone.commit:
        raise GuardError("runtime cache tombstone changed after planning")
    _make_runtime_tree_removable(tombstone.path)
    shutil.rmtree(tombstone.path)
    _sync_directory(root)


def apply_runtime_cache_retention_plan(
    plan: RuntimeCacheRetentionPlan, *, root: Path
) -> None:
    _validate_runtime_cache_root(root)
    protected_paths = {entry.path for entry in (*plan.protected, *plan.retained)}
    for tombstone in plan.tombstones:
        _remove_runtime_tombstone(tombstone, root=root)
    for entry in plan.candidates:
        if entry.path in protected_paths:
            raise GuardError("runtime cache retention plan overlaps protected entries")
        current = _validate_runtime_retention_entry(entry.path, root=root)
        if current.fingerprint != entry.fingerprint or current.commit != entry.commit:
            raise GuardError("runtime cache retention target changed after planning")
        tombstone_path = root / f".{entry.path.name}.pruning-{uuid4().hex}"
        if os.path.lexists(tombstone_path):
            raise GuardError("runtime cache tombstone already exists")
        os.rename(entry.path, tombstone_path)
        _sync_directory(root)
        tombstone = _validate_runtime_tombstone(tombstone_path, root=root)
        _remove_runtime_tombstone(tombstone, root=root)


def prune_runtime_cache_release_lock_held(
    *,
    apply: bool,
    keep: int,
    root: Path = RUNNER_CACHE_ROOT,
    app_dir: Path = APP_DIR,
) -> RuntimeCacheRetentionPlan:
    """Prune while the caller holds the platform release-operation lock."""

    if keep < 1 or keep > 100:
        raise GuardError("runtime cache keep must be between 1 and 100")
    if not os.path.lexists(root):
        return RuntimeCacheRetentionPlan((), (), (), ())
    descriptor = _open_machine_lock()
    try:
        assert_liveqa_idle()
        protected_commits = _protected_release_commits(app_dir)
        plan = build_runtime_cache_retention_plan(
            root,
            protected_commits=protected_commits,
            keep=keep,
        )
        if apply and (plan.candidates or plan.tombstones):
            assert_liveqa_idle()
            if _protected_release_commits(app_dir) != protected_commits:
                raise GuardError("release pointers changed during runtime cache retention")
            apply_runtime_cache_retention_plan(plan, root=root)
        return plan
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def prune_runtime_cache(
    *,
    apply: bool,
    keep: int,
    root: Path = RUNNER_CACHE_ROOT,
    app_dir: Path = APP_DIR,
) -> RuntimeCacheRetentionPlan:
    # Global lock order: release operation, then live-QA machine lock.
    with _release_operation_lock(app_dir):
        return prune_runtime_cache_release_lock_held(
            apply=apply,
            keep=keep,
            root=root,
            app_dir=app_dir,
        )


def _print_runtime_cache_retention_plan(
    plan: RuntimeCacheRetentionPlan, *, apply: bool
) -> None:
    mode = "apply" if apply else "dry-run"
    print(
        f"Live QA runtime cache retention ({mode}): "
        f"protected={len(plan.protected)}, retained={len(plan.retained)}, "
        f"delete={len(plan.candidates)}, "
        f"reclaim_tombstones={len(plan.tombstones)}"
    )
    for tombstone in plan.tombstones:
        print(f"reclaim tombstone: {tombstone.path}")
    for entry in plan.candidates:
        print(f"delete: {entry.path}")


def prepare_build_node() -> Path:
    _ensure_root_directory(BUILD_NODE_ROOT, 0o755)
    target = BUILD_NODE_ROOT / f"node-v{NODE_VERSION}"
    manifest_path = target / ".manifest.json"
    if target.exists() or target.is_symlink():
        metadata = target.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise GuardError("existing pinned build Node runtime is unsafe")
        manifest = _read_private_json(manifest_path, expected_uid=0, mode=0o444)
        if (
            set(manifest) != {"version", "node_archive_sha256", "tree_sha256"}
            or manifest.get("version") != 1
            or manifest.get("node_archive_sha256") != NODE_ARCHIVE_SHA256
        ):
            raise GuardError("pinned build Node manifest is invalid")
        expected = manifest.get("tree_sha256")
        if not isinstance(expected, str) or expected != _tree_digest(
            target,
            ignored_relatives=frozenset({Path(".manifest.json")}),
        ):
            raise GuardError("pinned build Node content drifted")
        _validate_cache_tree_permissions(target)
        return target
    stage = BUILD_NODE_ROOT / f".node-v{NODE_VERSION}.building-{uuid4().hex}"
    try:
        stage.mkdir(mode=0o700)
        _download_node_runtime(stage)
        node_home = stage / "node"
        _normalize_cache_tree(node_home)
        tree_digest = _tree_digest(node_home)
        # Temporarily writable only by root while the manifest is published.
        os.chmod(node_home, 0o755)  # nosec B103
        _write_immutable_manifest(
            node_home / ".manifest.json",
            {
                "version": 1,
                "node_archive_sha256": NODE_ARCHIVE_SHA256,
                "tree_sha256": tree_digest,
            },
            failure="pinned build Node manifest could not be written",
        )
        # Published runtime directories are immutable but readable/executable.
        os.chmod(node_home, 0o555)  # nosec B103
        os.rename(node_home, target)
        stage.rmdir()
        parent_fd = os.open(
            BUILD_NODE_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        _validate_cache_tree_permissions(target)
    except BaseException:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            for item in _walk_nofollow(stage):
                try:
                    if item.is_dir() and not item.is_symlink():
                        os.chmod(item, 0o700)
                    elif not item.is_symlink():
                        os.chmod(item, 0o600)
                except OSError:
                    pass
            shutil.rmtree(stage)
        raise
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guard production live QA execution.")
    commands = parser.add_subparsers(dest="command", required=True)
    locked = commands.add_parser("locked-exec")
    locked.add_argument("--bundle-path", type=Path, required=True)
    locked.add_argument("argv", nargs=argparse.REMAINDER)
    recovery_locked = commands.add_parser("recovery-locked-exec")
    recovery_locked.add_argument("--bundle-path", type=Path, required=True)
    recovery_locked.add_argument("argv", nargs=argparse.REMAINDER)
    asserted = commands.add_parser("assert-lock")
    asserted.add_argument("--bundle-path", type=Path, required=True)
    asserted.add_argument("--fd", type=int, required=True)
    provenance = commands.add_parser("verify-provenance")
    provenance.add_argument("--platform-root", type=Path, required=True)
    provenance.add_argument("--bundle-path", type=Path)
    provenance.add_argument("--helper-path", type=Path)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--bundle-path", type=Path, required=True)
    preflight.add_argument(
        "--mode", choices=("automated", "manual-prepare", "provision"), required=True
    )
    validate = commands.add_parser("validate-recovery")
    validate.add_argument("--bundle-path", type=Path, required=True)
    validate.add_argument("--state-dir", type=Path, required=True)
    prepare_state = commands.add_parser("prepare-root-state")
    prepare_state.add_argument("--bundle-path", type=Path, required=True)
    remove_setup = commands.add_parser("remove-setup-state")
    remove_setup.add_argument("--bundle-path", type=Path, required=True)
    remove_setup.add_argument("--setup-dir", type=Path, required=True)
    runtime = commands.add_parser("prepare-runtime-cache")
    runtime.add_argument("--platform-root", type=Path, required=True)
    runtime.add_argument("--commit", required=True)
    prune_runtime = commands.add_parser(
        "prune-runtime-cache",
        description=(
            "Bound live QA runtime caches. Dry-run is the default; deletion "
            "requires --apply."
        ),
    )
    prune_runtime.add_argument("--keep", type=int, default=1)
    prune_runtime.add_argument("--apply", action="store_true")
    commands.add_parser("prepare-build-node")
    sandbox = commands.add_parser("sandbox-path")
    sandbox.add_argument("--runtime-cache", type=Path, required=True)
    gate = commands.add_parser("prepare-browser-gate")
    gate.add_argument("--bundle-path", type=Path, required=True)
    gate.add_argument("--state-dir", type=Path, required=True)
    commands.add_parser("prepare-public-browser-gate")
    merge = commands.add_parser("merge-browser-inventory")
    merge.add_argument("--bundle-path", type=Path, required=True)
    merge.add_argument("--state-dir", type=Path, required=True)
    remove_gate = commands.add_parser("remove-browser-gate")
    remove_gate.add_argument("--state-dir", type=Path, required=True)
    remove_public = commands.add_parser("remove-public-browser-gate")
    remove_public.add_argument("--gate", type=Path, required=True)
    remove_state = commands.add_parser("remove-root-state")
    remove_state.add_argument("--bundle-path", type=Path, required=True)
    remove_state.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise GuardError("live QA guard requires root")
        if args.command == "locked-exec":
            command = list(args.argv)
            if command and command[0] == "--":
                command = command[1:]
            locked_exec(args.bundle_path, command)
        if args.command == "recovery-locked-exec":
            command = list(args.argv)
            if command and command[0] == "--":
                command = command[1:]
            recovery_locked_exec(args.bundle_path, command)
        if args.command == "assert-lock":
            assert_bundle_lock(args.bundle_path, args.fd)
        elif args.command == "verify-provenance":
            commit = verify_checkout_provenance(args.platform_root)
            if args.bundle_path is not None:
                _marker, helper = _bundle_marker_and_helper(args.bundle_path)
                verify_helper_binding(
                    platform_root=args.platform_root, installed_helper=helper
                )
            elif args.helper_path is not None:
                verify_helper_binding(
                    platform_root=args.platform_root, installed_helper=args.helper_path
                )
            else:
                raise GuardError("helper binding input is required")
            print(commit)
        elif args.command == "preflight":
            if args.mode == "automated":
                validate_chromium_apparmor_contract()
            assert_no_recovery_state(
                args.bundle_path,
                include_manual=args.mode in {"automated", "provision"},
            )
        elif args.command == "validate-recovery":
            marker, _inventory, _sessions = validate_root_state(
                args.bundle_path, args.state_dir
            )
            print(marker)
        elif args.command == "prepare-root-state":
            print(prepare_root_state(args.bundle_path))
        elif args.command == "remove-setup-state":
            remove_setup_state(args.bundle_path, args.setup_dir)
        elif args.command == "prepare-runtime-cache":
            print(prepare_runtime_cache(args.platform_root, args.commit))
        elif args.command == "prune-runtime-cache":
            plan = prune_runtime_cache(
                apply=args.apply,
                keep=args.keep,
            )
            _print_runtime_cache_retention_plan(plan, apply=args.apply)
        elif args.command == "prepare-build-node":
            print(prepare_build_node())
        elif args.command == "sandbox-path":
            print(_sandbox_path(args.runtime_cache))
        elif args.command == "prepare-browser-gate":
            print(prepare_browser_gate(args.bundle_path, args.state_dir))
        elif args.command == "prepare-public-browser-gate":
            print(prepare_public_browser_gate())
        elif args.command == "merge-browser-inventory":
            merge_browser_inventory(args.bundle_path, args.state_dir)
        elif args.command == "remove-browser-gate":
            reclaim_browser_gate(args.state_dir)
            remove_browser_gate(args.state_dir)
        elif args.command == "remove-public-browser-gate":
            remove_public_browser_gate(args.gate)
        elif args.command == "remove-root-state":
            remove_root_state(args.bundle_path, args.state_dir)
        return 0
    except GuardError as exc:
        print(f"Live QA guard refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Live QA guard failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
