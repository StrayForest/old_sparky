#!/usr/bin/env python3
"""Resolve and validate the repository-owned production host-tools pin.

The pin is application source metadata, not a deployment secret.  It is
validated on a secret-free runner before the immutable host-tools helper is
checked out.  The closure baseline in the same bounded JSON contract makes a
host-control edit fail until the pin is intentionally updated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Mapping


SCHEMA = 1
EXPECTED_REPOSITORY = "StrayForest/old_sparky"
PIN_RELATIVE_PATH = Path("platform/contracts/host_tools_pin.json")
MAX_PIN_BYTES = 16 * 1024
MAX_CLOSURE_FILES = 32
MAX_FILE_BYTES = 512 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PATH_RE = re.compile(r"^platform/tools/platform_[A-Za-z0-9_.-]+\.(?:py|sh)$")


class HostToolsPinError(ValueError):
    """Bounded validation failure for the host-tools pin contract."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostToolsPinError("host-tools pin contains duplicate keys")
        result[key] = value
    return result


def _read_pin(source_root: Path) -> dict[str, object]:
    if not isinstance(source_root, Path) or not source_root.is_absolute():
        raise HostToolsPinError("host-tools pin source root is invalid")
    if source_root.is_symlink() or not source_root.is_dir():
        raise HostToolsPinError("host-tools pin source root is unsafe")
    for relative in (Path("platform"), Path("platform/contracts")):
        directory = source_root / relative
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise HostToolsPinError("host-tools pin parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HostToolsPinError("host-tools pin parent is unsafe")
    path = source_root / PIN_RELATIVE_PATH
    if path.parent != source_root / PIN_RELATIVE_PATH.parent:
        raise HostToolsPinError("host-tools pin path escaped its source root")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostToolsPinError("host-tools pin is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_PIN_BYTES
    ):
        raise HostToolsPinError("host-tools pin metadata is unsafe")
    try:
        raw = path.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HostToolsPinError("host-tools pin is invalid") from exc
    if not isinstance(payload, dict):
        raise HostToolsPinError("host-tools pin is not an object")
    return payload


def _git(source_root: Path, *arguments: str, check: bool = True) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=check,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostToolsPinError("host-tools source git metadata is unavailable") from exc
    if not check:
        return str(completed.returncode)
    return completed.stdout.strip()


def _repository_from_remote(remote: str) -> str:
    value = remote.strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("https://github.com/"):
        return value.removeprefix("https://github.com/")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    raise HostToolsPinError("host-tools source repository is not GitHub")


def _validate_commit(source_root: Path, host_tools_sha: str, target_sha: str) -> None:
    if SHA_RE.fullmatch(target_sha) is None:
        raise HostToolsPinError("target SHA is not a full lowercase commit")
    if SHA_RE.fullmatch(host_tools_sha) is None:
        raise HostToolsPinError("host-tools SHA is not a full lowercase commit")
    if _git(source_root, "rev-parse", "--verify", "HEAD^{commit}") != target_sha:
        raise HostToolsPinError("target source checkout is not the requested commit")
    if _git(source_root, "rev-parse", "--verify", f"{host_tools_sha}^{{commit}}") != host_tools_sha:
        raise HostToolsPinError("host-tools SHA is not a reachable commit")
    if _git(source_root, "cat-file", "-t", host_tools_sha) != "commit":
        raise HostToolsPinError("host-tools SHA is not a commit object")
    if (
        _git(
            source_root,
            "merge-base",
            "--is-ancestor",
            host_tools_sha,
            target_sha,
            check=False,
        )
        != "0"
    ):
        raise HostToolsPinError("host-tools SHA is not an ancestor of the target")


def _bundle_file_names(source_root: Path) -> tuple[str, ...]:
    platform_dir = source_root / "platform"
    if platform_dir.is_symlink() or not platform_dir.is_dir():
        raise HostToolsPinError("host-tools platform directory is unsafe")
    tools_dir = source_root / "platform" / "tools"
    if tools_dir.is_symlink() or not tools_dir.is_dir():
        raise HostToolsPinError("host-tools tools directory is unsafe")
    try:
        source = (tools_dir / "platform_host_tools_bundle.py").read_text(
            encoding="utf-8"
        )
    except OSError as exc:
        raise HostToolsPinError("host-tools bundle helper is unavailable") from exc
    # The closure declaration is deliberately simple and bounded.  Importing
    # target source here would make a source-only pin gate depend on arbitrary
    # module imports, so parse only its literal HOST_TOOL_FILES tuple.
    match = re.search(
        r"HOST_TOOL_FILES\s*=\s*PREPARE_ARTIFACT_FILES\s*\+\s*PRODUCTION_DEPLOY_CONTROL_FILES",
        source,
    )
    if match is None:
        raise HostToolsPinError("host-tools closure declaration is missing")
    groups = re.findall(
        r"(?:PREPARE_ARTIFACT_FILES|PRODUCTION_DEPLOY_CONTROL_FILES)\s*=\s*\((.*?)\)",
        source,
        flags=re.DOTALL,
    )
    names: list[str] = []
    for group in groups:
        names.extend(re.findall(r"\"([^\"]+)\"", group))
    if not names or len(names) > MAX_CLOSURE_FILES or len(set(names)) != len(names):
        raise HostToolsPinError("host-tools closure declaration is invalid")
    return tuple(names)


def _validate_closure(source_root: Path, closure: object) -> None:
    if not isinstance(closure, list) or not 1 <= len(closure) <= MAX_CLOSURE_FILES:
        raise HostToolsPinError("host-tools closure baseline is invalid")
    records: list[Mapping[str, object]] = []
    for record in closure:
        if not isinstance(record, Mapping) or set(record) != {"mode", "path", "sha256"}:
            raise HostToolsPinError("host-tools closure record is invalid")
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or PATH_RE.fullmatch(path) is None
            or path in {str(item["path"]) for item in records}
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode not in {0o644, 0o755}
        ):
            raise HostToolsPinError("host-tools closure record is invalid")
        records.append(record)
    expected_paths = tuple(str(record["path"]) for record in records)
    names = _bundle_file_names(source_root)
    if expected_paths != tuple(f"platform/tools/{name}" for name in names):
        raise HostToolsPinError("host-tools closure baseline does not match helper")
    for record in records:
        path = source_root / str(record["path"])
        if (
            path.parent != source_root / "platform" / "tools"
            or (source_root / "platform").is_symlink()
            or (source_root / "platform" / "tools").is_symlink()
        ):
            raise HostToolsPinError("host-tools closure path escaped its source root")
        try:
            metadata = path.lstat()
            data = path.read_bytes()
        except OSError as exc:
            raise HostToolsPinError("host-tools closure member is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_FILE_BYTES
            or stat.S_IMODE(metadata.st_mode) != int(record["mode"])
            or hashlib.sha256(data).hexdigest() != record["sha256"]
        ):
            raise HostToolsPinError("host-tools closure changed without a pin bump")


def resolve_pin(
    source_root: Path,
    *,
    target_sha: str,
    expected_repository: str = EXPECTED_REPOSITORY,
) -> str:
    """Validate the pin and return the immutable host-tools commit."""

    if expected_repository != EXPECTED_REPOSITORY:
        raise HostToolsPinError("host-tools repository policy is invalid")
    payload = _read_pin(source_root)
    if set(payload) != {"schema", "repository", "host_tools_sha", "closure"}:
        raise HostToolsPinError("host-tools pin schema is not closed")
    if type(payload.get("schema")) is not int or payload.get("schema") != SCHEMA:
        raise HostToolsPinError("host-tools pin schema version is invalid")
    if payload.get("repository") != expected_repository:
        raise HostToolsPinError("host-tools pin repository is invalid")
    host_tools_sha = payload.get("host_tools_sha")
    if not isinstance(host_tools_sha, str):
        raise HostToolsPinError("host-tools pin SHA is invalid")
    _validate_commit(source_root, host_tools_sha, target_sha)
    _validate_closure(source_root, payload.get("closure"))
    remote_repository = _repository_from_remote(
        _git(source_root, "remote", "get-url", "origin")
    )
    if remote_repository != expected_repository:
        raise HostToolsPinError("host-tools source repository does not match policy")
    return host_tools_sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--source-root", required=True)
    resolve.add_argument("--target-sha", required=True)
    resolve.add_argument("--expected-repository", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "resolve":
            host_tools_sha = resolve_pin(
                Path(arguments.source_root),
                target_sha=arguments.target_sha,
                expected_repository=arguments.expected_repository,
            )
            print(host_tools_sha)
            return 0
    except (HostToolsPinError, OSError, ValueError):
        print("host-tools pin is invalid", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
