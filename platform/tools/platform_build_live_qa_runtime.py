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
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import platform_live_qa_guard as guard


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
SANDBOX_RELATIVE = guard.CHROMIUM_SANDBOX_RELATIVE
SANDBOX_SHA256 = guard.CHROMIUM_SANDBOX_SHA256
SANDBOX_SIZE = guard.CHROMIUM_SANDBOX_SIZE
RUNTIME_FILE_LIMIT = 768 * 1024 * 1024
RUNTIME_TOTAL_LIMIT = 2 * 1024 * 1024 * 1024


class RuntimeBuildError(RuntimeError):
    """The reviewed live-QA runtime could not be built safely."""


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
    if os.geteuid() != 0:
        raise RuntimeBuildError("live-QA runtime build requires root")
    platform_root = platform_root.resolve(strict=True)
    node_home = node_home.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise RuntimeBuildError("live-QA runtime output already exists")
    web = platform_root / "apps/platform_web"
    node = node_home / "bin/node"
    _metadata(node)
    output.mkdir(mode=0o700)
    try:
        _copy_file(node, output / "node/bin/node", executable=True)
        for relative in RUNTIME_SOURCE_FILES:
            _copy_file(
                web / relative,
                output / "web" / relative,
                executable=relative.endswith(".sh"),
            )
        for package in PLAYWRIGHT_PACKAGES:
            _copy_tree(web / "node_modules" / package, output / "web/node_modules" / package)

        browsers = output / "browsers"
        browsers.mkdir(mode=0o700)
        for directory_name, url, checksum, byte_size in guard.PLAYWRIGHT_ARCHIVES:
            guard._download_pinned_zip(
                url,
                checksum,
                byte_size,
                browsers / directory_name,
            )
            for marker in (browsers / directory_name).rglob("INSTALLATION_COMPLETE"):
                marker.unlink()
        sandbox = output / SANDBOX_RELATIVE
        sandbox_metadata = sandbox.lstat()
        if (
            not stat.S_ISREG(sandbox_metadata.st_mode)
            or sandbox_metadata.st_size != SANDBOX_SIZE
            or hashlib.sha256(sandbox.read_bytes()).hexdigest() != SANDBOX_SHA256
        ):
            raise RuntimeBuildError("canonical Chromium sandbox helper checksum is invalid")
        os.chown(sandbox, 0, 0)
        os.chmod(sandbox, 0o4755)
        # Normalize all directories/files before the manifest is computed.
        for path in sorted(output.rglob("*"), reverse=True):
            if path.is_symlink():
                raise RuntimeBuildError("live-QA runtime contains a symlink")
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
        tree_sha256, files = _tree_digest(output)
        lock_sha256 = hashlib.sha256((web / "package-lock.json").read_bytes()).hexdigest()
        manifest = {
            "version": 1,
            "node_version": guard.NODE_VERSION,
            "package_lock_sha256": lock_sha256,
            "tree_sha256": tree_sha256,
            "files": files,
        }
        manifest_path = output / "runtime-manifest.json"
        manifest_path.parent.mkdir(mode=0o555, exist_ok=True)
        # The output root is immutable, so briefly permit root to publish the
        # manifest and then restore the final read-only contract.
        os.chmod(output, 0o755)
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
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
    except BaseException:
        if output.exists() and not output.is_symlink():
            for path in sorted(output.rglob("*"), reverse=True):
                try:
                    if path.is_dir() and not path.is_symlink():
                        os.chmod(path, 0o700)
                    elif not path.is_symlink():
                        os.chmod(path, 0o600)
                except OSError:
                    pass
            shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--node-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build(args.platform_root, args.node_home, args.output)
        print(f"LIVE_QA_RUNTIME_BUILD status=passed tree_sha256={manifest['tree_sha256']}")
        return 0
    except (OSError, RuntimeBuildError, ValueError) as exc:
        print(f"LIVE_QA_RUNTIME_BUILD status=failed reason={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
