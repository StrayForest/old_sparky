#!/usr/bin/env python3
"""Build the small immutable runtime used by trusted live-user QA.

The normal web release needs the complete application dependency tree while it
is being built.  The credential-bearing browser contour does not: it needs a
pinned Node executable, Playwright itself, the reviewed live journey and the
browser archives referenced by the checked-in Playwright revision.  This tool
copies only that closed set into ``liveqa-runtime`` and publishes a content
manifest before the release archive is created.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import shutil
import stat
import sys


def _validate_import_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("live-QA runtime tool directory is unavailable") from exc
    if (
        canonical != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o7000
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (
            os.geteuid() == 0
            and (metadata.st_uid != 0 or metadata.st_gid != 0)
        )
    ):
        raise RuntimeError("live-QA runtime tool directory metadata is unsafe")


def _validate_import_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if (
        canonical != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (
            os.geteuid() == 0
            and (metadata.st_uid != 0 or metadata.st_gid != 0)
        )
    ):
        raise RuntimeError(f"{label} metadata is unsafe")


def _load_staged_guard():
    """Load the guard beside this staged builder without ambient imports."""

    builder_path = Path(__file__).absolute()
    _validate_import_file(builder_path, label="staged live-QA runtime builder")
    tools_directory = builder_path.parent
    _validate_import_directory(tools_directory)

    guard_path = tools_directory / "platform_live_qa_guard.py"
    _validate_import_file(guard_path, label="staged live-QA guard")
    spec = importlib.util.spec_from_file_location("platform_live_qa_guard", guard_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("staged live-QA guard cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    guard = _load_staged_guard()
except Exception as exc:  # Keep import-boundary failures machine-readable.
    guard = None
    _guard_load_error = exc
else:
    _guard_load_error = None


RUNTIME_SOURCE_FILES = (
    "playwright.live.config.ts",
    "tests/smoke/live-user-journey.spec.ts",
    "tests/support/live-qa-origin.ts",
    "tests/support/live-qa-sandbox.ts",
    "package-lock.json",
)
PLAYWRIGHT_PACKAGES = (
    "@playwright/test",
    "playwright",
    "playwright-core",
)
SANDBOX_RELATIVE = (
    guard.CHROMIUM_SANDBOX_RELATIVE
    if guard is not None
    else Path("browsers/chromium-1228/chrome-linux64/chrome_sandbox")
)
SANDBOX_SHA256 = (
    guard.CHROMIUM_SANDBOX_SHA256
    if guard is not None
    else "0" * 64
)
SANDBOX_SIZE = guard.CHROMIUM_SANDBOX_SIZE if guard is not None else 0
RUNTIME_FILE_LIMIT = 768 * 1024 * 1024
RUNTIME_TOTAL_LIMIT = 2 * 1024 * 1024 * 1024
MAX_LIVE_QA_RUNTIME_MANIFEST_BYTES = (
    guard.MAX_LIVE_QA_RUNTIME_MANIFEST_BYTES if guard is not None else 256 * 1024
)


class RuntimeBuildError(RuntimeError):
    """The reviewed live-QA runtime could not be built safely."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "build-failed",
        phase: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.phase = phase
        self.cleanup = "not-needed"


DIAGNOSTIC_PHASES = frozenset(
    {
        "validate-input",
        "copy-source",
        "copy-packages",
        "download-browser",
        "materialize-browser-links",
        "normalize",
        "manifest",
        "complete",
        "build",
    }
)
DIAGNOSTIC_REASONS = frozenset(
    {
        "ok",
        "build-failed",
        "invalid-input",
        "unsafe-source",
        "unsafe-link",
        "link-escape",
        "link-dangling",
        "link-cycle",
        "link-nonregular",
        "link-special",
        "link-hardlink",
        "link-ownership",
        "link-mode",
        "size-limit",
        "count-limit",
        "checksum",
        "io",
        "manifest",
        "cleanup",
    }
)
DIAGNOSTIC_CLEANUP = frozenset({"not-needed", "passed", "failed"})


def _print_diagnostic(
    *,
    phase: str,
    status: str,
    reason: str,
    cleanup: str,
    tree_sha256: str | None = None,
    stream: object | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema": 1,
        "phase": phase,
        "status": status,
        "reason": reason,
        "cleanup": cleanup,
    }
    if tree_sha256 is not None:
        payload["tree_sha256"] = tree_sha256
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        file=sys.stdout if stream is None else stream,
    )


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


def _metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeBuildError("live-QA runtime source is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeBuildError("live-QA runtime source contains a symlink")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0:
        raise RuntimeBuildError("live-QA runtime source metadata is unsafe")
    if (
        metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeBuildError("live-QA runtime source metadata is unsafe")
    if metadata.st_size > RUNTIME_FILE_LIMIT:
        raise RuntimeBuildError("live-QA runtime source file is too large")
    return metadata


def _copy_file(source: Path, destination: Path, *, executable: bool = False) -> None:
    metadata = _metadata(source)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chown(destination, 0, 0)
    os.chmod(destination, 0o555 if executable else 0o444)
    descriptor = os.open(destination, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if destination.stat().st_size != metadata.st_size:
        raise RuntimeBuildError("live-QA runtime source changed while copying")


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeBuildError("live-QA runtime package is unavailable")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise RuntimeBuildError("live-QA runtime package contains a symlink")
        if path.is_dir():
            directory_metadata = path.lstat()
            if (
                directory_metadata.st_uid != 0
                or directory_metadata.st_gid != 0
                or directory_metadata.st_mode & 0o7000
                or stat.S_IMODE(directory_metadata.st_mode) & 0o022
            ):
                raise RuntimeBuildError("live-QA runtime package directory metadata is unsafe")
            target.mkdir(mode=0o700)
            continue
        metadata = _metadata(path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        os.chown(target, 0, 0)
        os.chmod(target, 0o555 if metadata.st_mode & 0o111 else 0o444)
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _validate_browser_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeBuildError(
            "browser directory is unavailable",
            reason="unsafe-source",
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o7000
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeBuildError(
            "browser directory metadata is unsafe",
            reason="unsafe-source",
        )
    return metadata


def _browser_path_lstat(root: Path, path: Path) -> os.stat_result:
    """Lstat each component without following directory symlinks."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeBuildError(
            "browser path escaped its root",
            reason="link-escape",
        ) from exc
    current = root
    _validate_browser_directory(root)
    parts = relative.parts
    if not parts:
        return root.lstat()
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise RuntimeBuildError(
                "browser link target is dangling",
                reason="link-dangling",
            ) from exc
        if index < len(parts) - 1 and stat.S_ISLNK(metadata.st_mode):
            raise RuntimeBuildError(
                "browser link target crosses a directory symlink",
                reason="link-nonregular",
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeBuildError(
                "browser link target crosses a non-directory",
                reason="link-nonregular",
            )
    return metadata


def _browser_link_target(root: Path, link_path: Path) -> Path:
    """Resolve one symlink lexically, retaining the exact browser-root bound."""

    try:
        link = os.readlink(link_path)
    except OSError as exc:
        raise RuntimeBuildError(
            "browser link target is unavailable",
            reason="unsafe-link",
        ) from exc
    if (
        not isinstance(link, str)
        or not link
        or "\x00" in link
        or "\\" in link
        or link.startswith("/")
        or PurePosixPath(link).is_absolute()
    ):
        raise RuntimeBuildError(
            "browser link target is unsafe",
            reason="unsafe-link",
        )

    relative_parent = list(link_path.relative_to(root).parent.parts)
    target_parts = link.split("/")
    if any(part in {"", "."} for part in target_parts):
        raise RuntimeBuildError(
            "browser link target is not canonical",
            reason="unsafe-link",
        )
    for part in target_parts:
        if part == "..":
            if not relative_parent:
                raise RuntimeBuildError(
                    "browser link target escapes its root",
                    reason="link-escape",
                )
            relative_parent.pop()
        else:
            relative_parent.append(part)
    target = root.joinpath(*relative_parent)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeBuildError(
            "browser link target escapes its root",
            reason="link-escape",
        ) from exc
    return target


def _resolve_browser_terminal(
    root: Path,
    path: Path,
    active: tuple[Path, ...] = (),
) -> Path:
    metadata = _browser_path_lstat(root, path)
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise RuntimeBuildError(
                "browser link target ownership is unsafe",
                reason="link-ownership",
            )
        if metadata.st_nlink != 1:
            raise RuntimeBuildError(
                "browser link target is hard-linked",
                reason="link-hardlink",
            )
        if (
            metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RuntimeBuildError(
                "browser link target mode is unsafe",
                reason="link-mode",
            )
        if metadata.st_size > RUNTIME_FILE_LIMIT:
            raise RuntimeBuildError(
                "browser link target is too large",
                reason="size-limit",
            )
        return path
    if not stat.S_ISLNK(metadata.st_mode):
        reason = "link-nonregular" if stat.S_ISDIR(metadata.st_mode) else "link-special"
        raise RuntimeBuildError(
            "browser link target is not a regular file",
            reason=reason,
        )
    if (
        metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    ):
        reason = "link-hardlink" if metadata.st_nlink != 1 else "link-ownership"
        raise RuntimeBuildError(
            "browser link metadata is unsafe",
            reason=reason,
        )
    if path in active:
        raise RuntimeBuildError("browser link cycle detected", reason="link-cycle")
    target = _browser_link_target(root, path)
    return _resolve_browser_terminal(root, target, (*active, path))


def _open_materialization_temp(destination: Path) -> tuple[Path, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(128):
        temporary = destination.with_name(
            f".{destination.name}.materialize-{secrets.token_hex(16)}"
        )
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError:
            continue
        return temporary, descriptor
    raise RuntimeBuildError(
        "unable to allocate browser materialization temporary file",
        reason="io",
    )


def _copy_materialized_file(
    destination: Path,
    terminal: Path,
    link_identity: tuple[int, int],
) -> None:
    source_metadata = _metadata(terminal)
    temporary, output_descriptor = _open_materialization_temp(destination)
    source_descriptor: int | None = None
    try:
        try:
            source_descriptor = os.open(
                terminal,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            source_stat = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_dev != source_metadata.st_dev
                or source_stat.st_ino != source_metadata.st_ino
                or source_stat.st_nlink != 1
                or source_stat.st_uid != 0
                or source_stat.st_gid != 0
                or source_stat.st_size != source_metadata.st_size
            ):
                raise RuntimeBuildError(
                    "browser link source changed while copying",
                    reason="unsafe-source",
                )
            remaining = source_stat.st_size
            while remaining:
                chunk = os.read(source_descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeBuildError(
                        "browser link source was truncated",
                        reason="unsafe-source",
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(output_descriptor, view)
                    if written <= 0:
                        raise RuntimeBuildError(
                            "browser link materialization write failed",
                            reason="io",
                        )
                    view = view[written:]
                remaining -= len(chunk)
            if os.read(source_descriptor, 1):
                raise RuntimeBuildError(
                    "browser link source exceeded its bound",
                    reason="size-limit",
                )
            os.fchown(output_descriptor, 0, 0)
            os.fchmod(
                output_descriptor,
                0o555 if source_stat.st_mode & 0o111 else 0o444,
            )
            os.fsync(output_descriptor)
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            os.close(output_descriptor)

        current = destination.lstat()
        if (
            not stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != link_identity
        ):
            raise RuntimeBuildError(
                "browser link changed while materializing",
                reason="unsafe-link",
            )
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _materialize_browser_tree(root: Path, *, total_before: int) -> None:
    """Turn only validated browser symlink files into immutable regular files."""

    _validate_browser_directory(root)
    links: list[tuple[Path, Path, tuple[int, int]]] = []
    regular_total = total_before
    regular_count = 0
    link_count = 0
    for path in sorted(root.rglob("*")):
        metadata = _browser_path_lstat(root, path)
        if stat.S_ISLNK(metadata.st_mode):
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_nlink != 1
                or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            ):
                reason = "link-hardlink" if metadata.st_nlink != 1 else "link-ownership"
                raise RuntimeBuildError("browser link metadata is unsafe", reason=reason)
            terminal = _resolve_browser_terminal(root, path)
            links.append((path, terminal, (metadata.st_dev, metadata.st_ino)))
            regular_total += terminal.stat().st_size
            link_count += 1
        elif stat.S_ISREG(metadata.st_mode):
            _metadata(path)
            regular_total += metadata.st_size
            regular_count += 1
        elif stat.S_ISDIR(metadata.st_mode):
            _validate_browser_directory(path)
        else:
            raise RuntimeBuildError(
                "browser tree contains a special file",
                reason="link-special",
            )
    if regular_count + link_count > 200_000:
        raise RuntimeBuildError(
            "live-QA runtime exceeds its file-count bound",
            reason="count-limit",
        )
    if regular_total > RUNTIME_TOTAL_LIMIT:
        raise RuntimeBuildError(
            "live-QA runtime exceeds its duplicated-size bound",
            reason="size-limit",
        )
    for path, terminal, identity in links:
        _copy_materialized_file(path, terminal, identity)


def _regular_usage(root: Path, *, exclude: Path | None = None) -> int:
    total = 0
    for path in sorted(root.rglob("*")):
        if exclude is not None:
            try:
                path.relative_to(exclude)
            except ValueError:
                pass
            else:
                continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if stat.S_ISREG(metadata.st_mode):
            _metadata(path)
            total += metadata.st_size
        elif not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeBuildError(
                "runtime tree contains a special file",
                reason="link-special",
            )
    return total


def _tree_digest(root: Path) -> tuple[str, dict[str, str]]:
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    count = 0
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeBuildError("live-QA runtime contains a symlink")
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_mode & 0o7000
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise RuntimeBuildError("live-QA runtime directory metadata is unsafe")
            digest.update(b"d\0")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeBuildError("live-QA runtime contains a special file")
        if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
            raise RuntimeBuildError("live-QA runtime file metadata is unsafe")
        if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            expected = SANDBOX_RELATIVE.as_posix()
            if relative != expected or stat.S_IMODE(metadata.st_mode) != 0o4755:
                raise RuntimeBuildError("live-QA runtime contains an unexpected set-id file")
        elif stat.S_IMODE(metadata.st_mode) not in {0o444, 0o555}:
            raise RuntimeBuildError("live-QA runtime file permissions are unsafe")
        if path.name == "chrome_sandbox" and relative != SANDBOX_RELATIVE.as_posix():
            raise RuntimeBuildError("live-QA runtime contains an unexpected sandbox helper")
        if metadata.st_size > RUNTIME_FILE_LIMIT:
            raise RuntimeBuildError("live-QA runtime file is too large")
        count += 1
        total += metadata.st_size
        if count > 200_000 or total > RUNTIME_TOTAL_LIMIT:
            raise RuntimeBuildError("live-QA runtime exceeds its bound")
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative] = file_digest
        digest.update(b"f\0" + bytes.fromhex(file_digest))
    return digest.hexdigest(), files


def build(platform_root: Path, node_home: Path, output: Path) -> dict[str, object]:
    if _guard_load_error is not None or guard is None:
        raise RuntimeBuildError(
            "staged live-QA guard is unavailable",
            reason="invalid-input",
            phase="validate-input",
        )
    if os.geteuid() != 0:
        raise RuntimeBuildError("live-QA runtime build requires root")
    phase = "validate-input"
    output_created = False
    platform_root = platform_root.resolve(strict=True)
    node_home = node_home.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise RuntimeBuildError("live-QA runtime output already exists")
    web = platform_root / "apps/platform_web"
    node = node_home / "bin/node"
    try:
        _metadata(node)
        output.mkdir(mode=0o700)
        output_created = True
        phase = "copy-source"
        _copy_file(node, output / "node/bin/node", executable=True)
        for relative in RUNTIME_SOURCE_FILES:
            _copy_file(
                web / relative,
                output / "web" / relative,
                executable=relative.endswith(".sh"),
            )
        phase = "copy-packages"
        for package in PLAYWRIGHT_PACKAGES:
            _copy_tree(web / "node_modules" / package, output / "web/node_modules" / package)

        browsers = output / "browsers"
        browsers.mkdir(mode=0o700)
        phase = "download-browser"
        for directory_name, url, checksum, byte_size in guard.PLAYWRIGHT_ARCHIVES:
            browser_root = browsers / directory_name
            guard._download_pinned_zip(
                url,
                checksum,
                byte_size,
                browser_root,
            )
            for marker in browser_root.rglob("INSTALLATION_COMPLETE"):
                marker.unlink()
            phase = "materialize-browser-links"
            _materialize_browser_tree(
                browser_root,
                total_before=_regular_usage(output, exclude=browser_root),
            )
        sandbox = output / SANDBOX_RELATIVE
        sandbox_metadata = sandbox.lstat()
        if (
            not stat.S_ISREG(sandbox_metadata.st_mode)
            or sandbox_metadata.st_size != SANDBOX_SIZE
            or hashlib.sha256(sandbox.read_bytes()).hexdigest() != SANDBOX_SHA256
        ):
            raise RuntimeBuildError(
                "canonical Chromium sandbox helper checksum is invalid",
                reason="checksum",
            )
        os.chown(sandbox, 0, 0)
        os.chmod(sandbox, 0o4755)
        # Normalize all directories/files before the manifest is computed.
        phase = "normalize"
        for path in sorted(output.rglob("*"), reverse=True):
            if path.is_symlink():
                raise RuntimeBuildError(
                    "live-QA runtime contains a symlink",
                    reason="unsafe-link",
                )
            if path.is_dir():
                os.chown(path, 0, 0)
                os.chmod(path, 0o555)
            else:
                os.chown(path, 0, 0)
                if path == sandbox:
                    os.chmod(path, 0o4755)
                else:
                    os.chmod(path, 0o555 if path.stat().st_mode & 0o111 else 0o444)
        os.chown(output, 0, 0)
        os.chmod(output, 0o555)
        for directory in [
            output,
            *sorted((path for path in output.rglob("*") if path.is_dir()), reverse=True),
        ]:
            _fsync_directory(directory)
        phase = "manifest"
        tree_sha256, files = _tree_digest(output)
        lock_sha256 = hashlib.sha256((web / "package-lock.json").read_bytes()).hexdigest()
        manifest = {
            "version": 1,
            "node_version": guard.NODE_VERSION,
            "package_lock_sha256": lock_sha256,
            "tree_sha256": tree_sha256,
            "files": files,
        }
        manifest_raw = (
            json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        if len(manifest_raw) > MAX_LIVE_QA_RUNTIME_MANIFEST_BYTES:
            raise RuntimeBuildError(
                "live-QA runtime manifest exceeds its size bound",
                reason="size-limit",
                phase="manifest",
            )
        manifest_path = output / "runtime-manifest.json"
        manifest_path.parent.mkdir(mode=0o555, exist_ok=True)
        # The output root is immutable, so briefly permit root to publish the
        # manifest and then restore the final read-only contract.
        os.chmod(output, 0o755)
        manifest_path.write_bytes(manifest_raw)
        os.chown(manifest_path, 0, 0)
        os.chmod(manifest_path, 0o444)
        descriptor = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(output, 0o555)
        _fsync_directory(output)
        return manifest
    except BaseException as exc:
        cleanup = "not-needed"
        if output_created and output.exists() and not output.is_symlink():
            try:
                for path in sorted(output.rglob("*"), reverse=True):
                    try:
                        if path.is_dir() and not path.is_symlink():
                            os.chmod(path, 0o700)
                        elif not path.is_symlink():
                            os.chmod(path, 0o600)
                    except OSError:
                        pass
                shutil.rmtree(output)
                cleanup = "passed"
            except OSError:
                cleanup = "failed"
        if isinstance(exc, RuntimeBuildError):
            if exc.phase is None:
                exc.phase = phase if phase in DIAGNOSTIC_PHASES else "build"
            exc.cleanup = cleanup
        else:
            # GuardError and other bounded runtime failures must reach the same
            # machine-readable boundary without exposing their message.
            try:
                setattr(exc, "phase", phase if phase in DIAGNOSTIC_PHASES else "build")
                setattr(exc, "cleanup", cleanup)
            except (AttributeError, TypeError):
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    if _guard_load_error is not None:
        _print_diagnostic(
            phase="validate-input",
            status="failed",
            reason="invalid-input",
            cleanup="not-needed",
            stream=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--node-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build(args.platform_root, args.node_home, args.output)
        _print_diagnostic(
            phase="complete",
            status="passed",
            reason="ok",
            cleanup="not-needed",
            tree_sha256=str(manifest["tree_sha256"]),
        )
        return 0
    except Exception as exc:
        phase = getattr(exc, "phase", None)
        reason = getattr(exc, "reason", None)
        cleanup = getattr(exc, "cleanup", "not-needed")
        if phase not in DIAGNOSTIC_PHASES:
            phase = "build"
        if reason not in DIAGNOSTIC_REASONS:
            reason = "io" if isinstance(exc, OSError) else "build-failed"
        if cleanup not in DIAGNOSTIC_CLEANUP:
            cleanup = "failed"
        _print_diagnostic(
            phase=phase,
            status="failed",
            reason=reason,
            cleanup=cleanup,
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
