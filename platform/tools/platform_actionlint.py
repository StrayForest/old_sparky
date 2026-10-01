#!/usr/bin/env python3
"""Download a pinned actionlint release and lint tracked workflow YAML.

The CI workflow owns the runner and invokes this small wrapper from the
existing verification-contract job.  The wrapper intentionally has no mutable
download reuse or fallback: it selects one of four explicitly reviewed release
assets, downloads into a private temporary directory, verifies the committed
digest, validates every archive member, and only then executes actionlint.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIONLINT_VERSION = "1.7.12"
ACTIONLINT_RELEASE_TAG = f"v{ACTIONLINT_VERSION}"
ACTIONLINT_RELEASE_BASE_URL = (
    "https://github.com/rhysd/actionlint/releases/download/"
    f"{ACTIONLINT_RELEASE_TAG}"
)
ACTIONLINT_CHECKSUMS_FILENAME = f"actionlint_{ACTIONLINT_VERSION}_checksums.txt"
# Independently verified against the official release asset before this map
# was committed.  It is retained as a source-of-truth check for the fixture
# contract; runtime trust comes from the per-platform binary digest below.
ACTIONLINT_CHECKSUMS_SHA256 = (
    "433028cf0ba3c42163ea1a668dedce30fcdbe84fe912b1a5e288c006eab8a4f5"
)
ACTIONLINT_BINARY_MEMBER = "actionlint"
# Exact modes observed in the official v1.7.12 tarballs.  A mode allowlist is
# deliberately stricter than an executable-bit check: every release member
# must have the reviewed type, path and mode before the binary is materialized.
ACTIONLINT_ARCHIVE_MODES: Mapping[str, int] = {
    "LICENSE.txt": 0o644,
    "README.md": 0o644,
    "docs/README.md": 0o644,
    "docs/api.md": 0o644,
    "docs/checks.md": 0o644,
    "docs/config.md": 0o644,
    "docs/install.md": 0o644,
    "docs/reference.md": 0o644,
    "docs/usage.md": 0o644,
    "man/actionlint.1": 0o644,
    ACTIONLINT_BINARY_MEMBER: 0o755,
}
ACTIONLINT_ARCHIVE_MEMBERS = frozenset(ACTIONLINT_ARCHIVE_MODES)
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 32
SHA256_LENGTH = 64


class ActionlintError(RuntimeError):
    """Fail-closed actionlint installation or execution error."""


@dataclass(frozen=True, slots=True)
class ActionlintAsset:
    """One immutable official release asset."""

    platform: str
    architecture: str
    filename: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{ACTIONLINT_RELEASE_BASE_URL}/{self.filename}"


ACTIONLINT_ASSETS: Mapping[tuple[str, str], ActionlintAsset] = {
    ("linux", "amd64"): ActionlintAsset(
        "linux",
        "amd64",
        "actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("linux", "arm64"): ActionlintAsset(
        "linux",
        "arm64",
        "actionlint_1.7.12_linux_arm64.tar.gz",
        "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ),
    ("darwin", "amd64"): ActionlintAsset(
        "darwin",
        "amd64",
        "actionlint_1.7.12_darwin_amd64.tar.gz",
        "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
    ),
    ("darwin", "arm64"): ActionlintAsset(
        "darwin",
        "arm64",
        "actionlint_1.7.12_darwin_arm64.tar.gz",
        "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ),
}

_PLATFORM_ALIASES = {
    "linux": "linux",
    "Linux": "linux",
    "darwin": "darwin",
    "Darwin": "darwin",
}
_ARCHITECTURE_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def select_asset(
    system: str | None = None,
    machine: str | None = None,
) -> ActionlintAsset:
    """Select only an explicitly supported Linux/macOS release asset."""

    raw_system = host_platform.system() if system is None else system
    raw_machine = host_platform.machine() if machine is None else machine
    normalized_system = _PLATFORM_ALIASES.get(raw_system)
    normalized_machine = _ARCHITECTURE_ALIASES.get(raw_machine)
    if normalized_system is None or normalized_machine is None:
        raise ActionlintError(
            "unsupported actionlint platform: "
            f"os={raw_system!r} architecture={raw_machine!r}"
        )
    try:
        return ACTIONLINT_ASSETS[(normalized_system, normalized_machine)]
    except KeyError as exc:
        raise ActionlintError(
            "actionlint platform mapping is not explicitly supported: "
            f"{normalized_system}/{normalized_machine}"
        ) from exc


def parse_official_checksums(checksum_text: str) -> dict[str, str]:
    """Parse the official checksum format without accepting extra syntax."""

    parsed: dict[str, str] = {}
    for line in checksum_text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or len(fields[0]) != SHA256_LENGTH:
            raise ActionlintError("official actionlint checksum line is malformed")
        digest, filename = fields
        if any(character not in "0123456789abcdef" for character in digest):
            raise ActionlintError("official actionlint checksum is not lowercase SHA-256")
        if filename in parsed:
            raise ActionlintError("official actionlint checksums contain a duplicate asset")
        parsed[filename] = digest
    return parsed


def _read_regular_file(path: Path, maximum: int) -> bytes:
    """Read a bounded regular file without following a symlink."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ActionlintError("actionlint archive is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
    ):
        raise ActionlintError("actionlint archive is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ActionlintError("actionlint archive cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > maximum
        ):
            raise ActionlintError("actionlint archive changed while opening")
        data = bytearray()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while len(data) <= maximum:
                chunk = stream.read(min(1024 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        if len(data) > maximum:
            raise ActionlintError("actionlint archive is oversized")
        return bytes(data)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _validate_member_path(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name:
        raise ActionlintError("actionlint archive contains an unsafe member path")
    path = PurePosixPath(name)
    if path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts):
        raise ActionlintError("actionlint archive contains a traversal member path")
    if name not in ACTIONLINT_ARCHIVE_MEMBERS:
        raise ActionlintError("actionlint archive contains an unexpected member")


def _validated_binary(data: bytes) -> bytes:
    """Validate the complete tar member set and return only the binary bytes."""

    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ActionlintError("actionlint archive is not a valid gzip tar archive") from exc
    with archive:
        try:
            members = archive.getmembers()
        except (OSError, tarfile.TarError) as exc:
            raise ActionlintError("actionlint archive members cannot be read") from exc
        member_names = [member.name for member in members]
        if (
            len(members) > MAX_ARCHIVE_MEMBERS
            or len(member_names) != len(set(member_names))
            or set(member_names) != set(ACTIONLINT_ARCHIVE_MEMBERS)
        ):
            raise ActionlintError("actionlint archive member set is not canonical")
        total_size = 0
        binary_member: tarfile.TarInfo | None = None
        for member in members:
            _validate_member_path(member.name)
            if not member.isreg() or member.issym() or member.islnk():
                raise ActionlintError("actionlint archive contains a non-regular member")
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ActionlintError("actionlint archive member is oversized")
            total_size += member.size
            if total_size > MAX_UNPACKED_BYTES:
                raise ActionlintError("actionlint archive expands beyond its bound")
            if member.mode != ACTIONLINT_ARCHIVE_MODES[member.name]:
                raise ActionlintError("actionlint archive member mode is not canonical")
            if member.name == ACTIONLINT_BINARY_MEMBER:
                binary_member = member
        if binary_member is None:
            raise ActionlintError("actionlint archive binary is missing")
        try:
            binary_stream = archive.extractfile(binary_member)
            if binary_stream is None:
                raise ActionlintError("actionlint archive binary cannot be read")
            binary = binary_stream.read(MAX_MEMBER_BYTES + 1)
        except (OSError, tarfile.TarError) as exc:
            raise ActionlintError("actionlint archive binary cannot be extracted safely") from exc
        if len(binary) != binary_member.size or len(binary) > MAX_MEMBER_BYTES:
            raise ActionlintError("actionlint archive binary size changed")
        return binary


def verify_archive_digest(archive_path: Path, asset: ActionlintAsset) -> bytes:
    """Verify the pinned binary archive digest before any tar parsing."""

    data = _read_regular_file(archive_path, MAX_ARCHIVE_BYTES)
    actual = hashlib.sha256(data).hexdigest()
    if actual != asset.sha256:
        raise ActionlintError(
            f"actionlint archive digest mismatch for {asset.filename}"
        )
    return data


def extract_verified_binary(
    archive_path: Path,
    asset: ActionlintAsset,
    destination: Path,
) -> Path:
    """Verify, validate and safely materialize only the trusted binary."""

    binary = _validated_binary(verify_archive_digest(archive_path, asset))
    try:
        parent = destination.parent.lstat()
    except OSError as exc:
        raise ActionlintError("actionlint extraction directory is unavailable") from exc
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ActionlintError("actionlint extraction directory is unsafe")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o700)
    except OSError as exc:
        raise ActionlintError("actionlint binary destination is unsafe") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(binary)
            stream.flush()
            os.fchmod(stream.fileno(), 0o700)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    metadata = destination.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != len(binary)
    ):
        raise ActionlintError("materialized actionlint binary is unsafe")
    return destination


def _download_archive(asset: ActionlintAsset, destination: Path) -> Path:
    curl = shutil.which("curl")
    if not curl:
        raise ActionlintError("curl is required to install pinned actionlint")
    command = [
        curl,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "5",
        "--max-time",
        "60",
        "--max-filesize",
        str(MAX_ARCHIVE_BYTES),
        "--retry",
        "0",
        "--output",
        str(destination),
        asset.url,
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ActionlintError("pinned actionlint download failed") from exc
    return destination


def tracked_workflow_paths(repository_root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Return every tracked workflow YAML, rejecting unsafe checkout paths."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "ls-files", "-z", "--", ".github/workflows"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ActionlintError("cannot enumerate tracked workflow YAML") from exc
    paths: list[Path] = []
    seen: set[Path] = set()
    root = repository_root.resolve(strict=True)
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = Path(raw_path.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ActionlintError("tracked workflow path is not valid UTF-8") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise ActionlintError("tracked workflow path escapes the repository")
        if relative.suffix.lower() not in {".yml", ".yaml"}:
            continue
        if len(relative.parts) < 3 or relative.parts[:2] != (".github", "workflows"):
            raise ActionlintError("git returned a workflow path outside the workflow root")
        if relative in seen:
            raise ActionlintError("git returned a duplicate tracked workflow path")
        seen.add(relative)
        path = repository_root / relative
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ActionlintError("tracked workflow YAML is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (root not in resolved.parents and resolved != root)
        ):
            raise ActionlintError("tracked workflow YAML is not a safe regular file")
        paths.append(path)
    if not paths:
        raise ActionlintError("no tracked workflow YAML files were found")
    return tuple(sorted(paths))


def run_actionlint(binary: Path, repository_root: Path = REPO_ROOT) -> int:
    """Run actionlint with inherited output and its exact failure status."""

    workflow_paths = tracked_workflow_paths(repository_root)
    command = [str(binary), "-shellcheck", "", *(str(path) for path in workflow_paths)]
    try:
        completed = subprocess.run(command, cwd=repository_root, check=False)
    except OSError as exc:
        raise ActionlintError("actionlint execution failed") from exc
    return completed.returncode


def install_and_run(repository_root: Path = REPO_ROOT) -> int:
    """Install the selected release in a private temp directory and lint."""

    asset = select_asset()
    with tempfile.TemporaryDirectory(prefix="platform-actionlint-") as temporary:
        temporary_root = Path(temporary)
        archive = _download_archive(asset, temporary_root / asset.filename)
        binary = extract_verified_binary(archive, asset, temporary_root / ACTIONLINT_BINARY_MEMBER)
        return run_actionlint(binary, repository_root)


def main(argv: Iterable[str] | None = None) -> int:
    del argv  # The CI contract intentionally has no mutable or bypass flags.
    try:
        return install_and_run()
    except ActionlintError as exc:
        print(f"platform actionlint: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
