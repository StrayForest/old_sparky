#!/usr/bin/env python3
"""Verify one exact, already-provisioned host-tools generation over pinned SSH.

This is a read-only runner-side verifier shared by production preflight and the
bootstrap coordinator. Its remote command set and paths are fixed here; callers
provide only the verified generation SHA and local bundle contract directory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import time


HOST_KEY_FINGERPRINT = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
GENERATION_ROOT = "/opt/oldsparky/platform/shared/host-tools"
RELEASE_LOCK_PATH = "/run/lock/oldsparky-platform-release.lock"
LIVE_QA_LOCK_PATH = "/run/lock/oldsparky-liveqa.lock"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
PATH_RE = re.compile(r"(?:platform_[A-Za-z0-9_.-]+\.(?:py|sh)|capabilities\.txt|manifest\.json)\Z")
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
MAX_CONTRACT_ROWS = 32
MAX_REMOTE_OUTPUT = 16384
# The last readiness decision runs as one remote process while it owns both
# canonical locks. This closes the window in which AUTO could observe the new
# generation and dispatch its child before the root-console self-test finishes.
# It opens existing lock files only: there is no O_CREAT, chmod, or state write.
LOCKED_FINAL_CHECK = r'''import base64, fcntl, hashlib, json, os, pathlib, stat, subprocess, sys
class Busy(Exception): pass
def acquire(path):
    path = pathlib.Path(path)
    parent = path.parent
    pm = parent.lstat()
    if not stat.S_ISDIR(pm.st_mode) or pm.st_uid != 0 or parent.is_symlink() or ((stat.S_IMODE(pm.st_mode) & 0o022) and not (stat.S_IMODE(pm.st_mode) & stat.S_ISVTX)):
        raise RuntimeError("unsafe lock directory")
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        po = os.fstat(pfd)
        if (po.st_dev, po.st_ino) != (pm.st_dev, pm.st_ino): raise RuntimeError("lock directory changed")
        before = os.stat(path.name, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0 or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600: raise RuntimeError("unsafe lock file")
        fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    finally:
        os.close(pfd)
    opened = os.fstat(fd)
    current = path.lstat()
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or opened.st_uid != 0 or opened.st_gid != 0 or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600:
        os.close(fd); raise RuntimeError("lock identity changed")
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd); raise Busy()
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_nlink) != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_gid, opened.st_nlink):
        os.close(fd); raise RuntimeError("lock identity changed after acquisition")
    parent_after = parent.lstat()
    if (parent_after.st_dev, parent_after.st_ino) != (po.st_dev, po.st_ino):
        os.close(fd); raise RuntimeError("lock directory changed after acquisition")
    return fd
def digest_file(path, mode, expected, maximum=524288):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_uid != 0 or before.st_gid != 0 or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != mode or before.st_size > maximum: raise RuntimeError("generation member metadata mismatch")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino): raise RuntimeError("generation member changed")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) != opened.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, 1): raise RuntimeError("generation member changed while reading")
    finally: os.close(fd)
    raw = bytes(data)
    if hashlib.sha256(raw).hexdigest() != expected: raise RuntimeError("generation member digest mismatch")
    return raw
def main():
    payload = json.loads(base64.b64decode(sys.argv[1], validate=True))
    required = {"generation", "source_sha", "toolset_version", "manifest_sha256", "capabilities_sha256", "expected_member_count", "members"}
    if not isinstance(payload, dict) or set(payload) != required: raise RuntimeError("input contract schema mismatch")
    expected_member_count = {"production-host-tools-v3": 16, "production-host-tools-v4": 17}.get(payload["toolset_version"])
    if type(payload["expected_member_count"]) is not int or payload["expected_member_count"] != expected_member_count: raise RuntimeError("inventory size contract mismatch")
    held = []
    try:
        for path in ("/run/lock/oldsparky-platform-release.lock", "/run/lock/oldsparky-liveqa.lock"): held.append(acquire(path))
        operation = pathlib.Path("/opt/oldsparky/platform/shared/.release-operation.json")
        if operation.exists() or operation.is_symlink(): raise RuntimeError("release transaction is pending")
        generation = pathlib.Path(payload["generation"])
        directory = generation.lstat()
        if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode) or directory.st_uid != 0 or directory.st_gid != 0 or directory.st_nlink != 2 or stat.S_IMODE(directory.st_mode) != 0o555: raise RuntimeError("generation directory metadata mismatch")
        members = payload["members"]
        names = {item["name"] for item in members}
        if not isinstance(members, list) or len(members) != expected_member_count or len(names) != expected_member_count or not {"manifest.json", "capabilities.txt"}.issubset(names): raise RuntimeError("generation inventory contract mismatch")
        if {entry.name for entry in generation.iterdir()} != names: raise RuntimeError("generation has unexpected entries")
        digest_file(generation / "manifest.json", 0o444, payload["manifest_sha256"])
        digest_file(generation / "capabilities.txt", 0o444, payload["capabilities_sha256"])
        for item in members:
            if not isinstance(item, dict) or set(item) != {"name", "sha256", "mode"}: raise RuntimeError("member contract malformed")
            if item["name"] in {"manifest.json", "capabilities.txt"}: continue
            if item["mode"] != 0o555: raise RuntimeError("tool mode contract mismatch")
            digest_file(generation / item["name"], 0o555, item["sha256"])
        sha = payload["source_sha"]
        if payload["toolset_version"] == "production-host-tools-v3":
            capability = f"HOST_TOOLS schema=1 source_sha={sha} generation={sha} dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 release_baseline=1 retained_load_export_cleanup=1 retained_load_source_binding=1 python_isolated=1 python_bytecode_disabled=1\n"
        elif payload["toolset_version"] == "production-host-tools-v4":
            capability = f"HOST_TOOLS schema=1 source_sha={sha} generation={sha} dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 release_baseline=1 retained_load_export_cleanup=1 retained_load_source_binding=1 cpu_diagnostic_plan_control=1 python_isolated=1 python_bytecode_disabled=1\n"
        else: raise RuntimeError("toolset version invalid")
        cap = (generation / "capabilities.txt").read_bytes()
        if cap != capability.encode("ascii"): raise RuntimeError("capability text mismatch")
        result = subprocess.run(["/usr/bin/python3.12", "-I", "-B", str(generation / "platform_workflow_remote_dispatch.py"), "host-capabilities"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False, env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONDONTWRITEBYTECODE":"1", "LC_ALL":"C"})
        if result.returncode != 0 or len(result.stdout) > 512 or result.stdout != capability.encode("ascii"): raise RuntimeError("dispatcher self-test failed")
        sys.stdout.write(f"HOST_TOOLS_READY schema=1 source_sha={sha} manifest_sha256={payload['manifest_sha256']}\n")
        return 0
    finally:
        for fd in reversed(held):
            try: fcntl.flock(fd, fcntl.LOCK_UN)
            finally: os.close(fd)
try: raise SystemExit(main())
except Busy: raise SystemExit(75)
except BaseException: raise SystemExit(1)
'''
# OpenSSH private-key files are multiline text. GitHub Actions secrets are
# limited to 48 KiB; reserve another 16 KiB for envelope/formatting overhead
# while preventing an unbounded secret from reaching the runner's SSH directory.
MAX_SSH_PRIVATE_KEY_BYTES = 65536
SSH_PRIVATE_KEY_PARSE_TIMEOUT_SECONDS = 5
SSH_DIAGNOSTIC_CODES = frozenset(
    {
        "ssh_host_format",
        "ssh_user_format",
        "ssh_key_shape",
        "ssh_key_parser_invalid",
        "ssh_key_parser_unavailable",
        "ssh_key_parser_timeout",
    }
)


class VerificationError(RuntimeError):
    """Closed, safe verification failure."""

    def __init__(self, message: str, *, diagnostic_code: str | None = None) -> None:
        if diagnostic_code is not None and diagnostic_code not in SSH_DIAGNOSTIC_CODES:
            raise ValueError("unsupported host-tools verification diagnostic")
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


class RemoteCommandFailure(VerificationError):
    def __init__(self, returncode: int) -> None:
        super().__init__("bounded host-tools verification command failed")
        self.returncode = returncode


class GenerationAbsent(VerificationError):
    """Only the exact missing generation may be retried by a coordinator."""


def _regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_nlink != 1:
            raise VerificationError("host-tools local contract metadata is unsafe")
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise VerificationError("host-tools local contract size is invalid")
        value = path.read_bytes()
    except OSError as exc:
        raise VerificationError("host-tools local contract is unavailable") from exc
    if len(value) != metadata.st_size:
        raise VerificationError("host-tools local contract changed while reading")
    return value


def _contract_rows(contract_dir: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str, str]:
    digest_rows: list[tuple[str, str]] = []
    for raw in _regular_file(contract_dir / "files.sha256", max_bytes=8192).decode("ascii").splitlines():
        parts = raw.split()
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None or PATH_RE.fullmatch(parts[1]) is None:
            raise VerificationError("host-tools file digest contract is malformed")
        digest_rows.append((parts[0], parts[1]))
    mode_rows: list[tuple[str, str]] = []
    for raw in _regular_file(contract_dir / "files.modes", max_bytes=8192).decode("ascii").splitlines():
        parts = raw.split()
        if len(parts) != 2 or parts[0] not in {"444", "555"} or PATH_RE.fullmatch(parts[1]) is None:
            raise VerificationError("host-tools file mode contract is malformed")
        mode_rows.append((parts[0], parts[1]))
    paths = [path for _, path in digest_rows]
    if not digest_rows or len(digest_rows) > MAX_CONTRACT_ROWS or len(paths) != len(set(paths)):
        raise VerificationError("host-tools file contract cardinality is invalid")
    if [path for _, path in mode_rows] != paths:
        raise VerificationError("host-tools file digest and mode contracts differ")
    manifest_hash = _regular_file(contract_dir / "manifest.sha256", max_bytes=256).decode("ascii").strip()
    capabilities_hash = _regular_file(contract_dir / "capabilities.sha256", max_bytes=256).decode("ascii").strip()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (manifest_hash, capabilities_hash)):
        raise VerificationError("host-tools root digest contract is malformed")
    return digest_rows, mode_rows, manifest_hash, capabilities_hash


def _parse_scanned_host_key(output: bytes, host: str) -> str:
    try:
        text = output.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise VerificationError("production SSH host-key response is malformed") from exc
    records = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(records) != 1:
        raise VerificationError("production SSH host-key response is malformed")
    fields = records[0].split()
    if (
        len(fields) != 3
        or fields[0] != host
        or fields[1] != "ssh-ed25519"
        or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[2]) is None
    ):
        raise VerificationError("production SSH host-key response is malformed")
    return " ".join(fields)


def _run(argv: list[str], *, timeout: int, max_output: int = MAX_REMOTE_OUTPUT) -> bytes:
    if type(timeout) is not int or timeout <= 0 or type(max_output) is not int or max_output <= 0:
        raise VerificationError("bounded host-tools verification command failed")
    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VerificationError("bounded host-tools verification command failed")
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    chunk = os.read(key.fd, min(4096, max_output + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise VerificationError("bounded host-tools verification command failed")
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise VerificationError("bounded host-tools verification command failed") from exc
    except VerificationError:
        raise
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process is not None and process.stdout is not None:
            process.stdout.close()
    if returncode != 0:
        raise RemoteCommandFailure(returncode)
    return bytes(output)


def _safe_host_ssh(host: str, user: str, key: str, temp_root: Path) -> tuple[Path, list[str], bool]:
    if not isinstance(host, str) or HOST_RE.fullmatch(host) is None:
        raise VerificationError("production SSH configuration is malformed", diagnostic_code="ssh_host_format")
    if not isinstance(user, str) or USER_RE.fullmatch(user) is None:
        raise VerificationError("production SSH configuration is malformed", diagnostic_code="ssh_user_format")
    if not isinstance(key, str):
        raise VerificationError("production SSH configuration is malformed", diagnostic_code="ssh_key_shape")
    try:
        key_bytes = key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise VerificationError("production SSH configuration is malformed", diagnostic_code="ssh_key_shape") from exc
    if (
        not key_bytes
        or len(key_bytes) > MAX_SSH_PRIVATE_KEY_BYTES
        or b"\r" in key_bytes
        or b"\x00" in key_bytes
        or any(byte != 0x0A and not 0x20 <= byte <= 0x7E for byte in key_bytes)
    ):
        raise VerificationError("production SSH configuration is malformed", diagnostic_code="ssh_key_shape")
    ssh_dir = temp_root / "platform-host-capability-ssh"
    created = not ssh_dir.exists() and not ssh_dir.is_symlink()
    if created:
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    if ssh_dir.is_symlink() or ssh_dir.stat().st_mode & 0o777 != 0o700:
        raise VerificationError("production SSH directory metadata is unsafe")
    key_path = ssh_dir / "id_ed25519"
    known_hosts_path = ssh_dir / "known_hosts"
    scan_path = ssh_dir / "known_hosts.scan"
    config_path = ssh_dir / "config"
    try:
        if created:
            key_fd = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(key_fd, "wb") as key_file:
                os.fchmod(key_file.fileno(), 0o600)
                key_file.write(key_bytes.rstrip(b"\n") + b"\n")
                key_file.flush()
                os.fsync(key_file.fileno())
        else:
            for path in (key_path, known_hosts_path, config_path):
                metadata = path.lstat()
                if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1 or metadata.st_mode & 0o777 != 0o600:
                    raise VerificationError("existing production SSH files are unsafe")
            if key_path.read_bytes() != key_bytes.rstrip(b"\n") + b"\n":
                raise VerificationError("existing production SSH key differs from the supplied identity")

        try:
            parsed_key = subprocess.run(
                ["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", str(key_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SSH_PRIVATE_KEY_PARSE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError(
                "production SSH private key validation failed",
                diagnostic_code="ssh_key_parser_timeout",
            ) from exc
        except OSError as exc:
            raise VerificationError(
                "production SSH private key validation failed",
                diagnostic_code="ssh_key_parser_unavailable",
            ) from exc
        if parsed_key.returncode != 0:
            raise VerificationError(
                "production SSH private key is invalid",
                diagnostic_code="ssh_key_parser_invalid",
            )

        if created:
            scanned = _run(["/usr/bin/timeout", "--foreground", "4s", "/usr/bin/ssh-keyscan", "-T", "3", "-t", "ed25519", host], timeout=6)
            scan_path.write_text(_parse_scanned_host_key(scanned, host) + "\n", encoding="ascii")
            scan_path.chmod(0o600)
            fingerprint_path = scan_path
        else:
            _parse_scanned_host_key(known_hosts_path.read_bytes(), host)
            config_text = config_path.read_text(encoding="ascii")
            expected_config = (
                "Host *\n  StrictHostKeyChecking yes\n"
                f"  UserKnownHostsFile {known_hosts_path}\n"
            )
            if config_text != expected_config:
                raise VerificationError("existing production SSH config is not bound to known hosts")
            fingerprint_path = known_hosts_path
        fingerprint = _run(["/usr/bin/ssh-keygen", "-lf", str(fingerprint_path), "-E", "sha256"], timeout=5, max_output=1024).decode("ascii", "strict").split()
        if len(fingerprint) < 2 or fingerprint[1] != HOST_KEY_FINGERPRINT:
            raise VerificationError("production SSH host key does not match the pinned fingerprint")
        if created:
            known_hosts_path.write_bytes(scan_path.read_bytes())
            known_hosts_path.chmod(0o600)
            config_path.write_text(
                "Host *\n  StrictHostKeyChecking yes\n"
                f"  UserKnownHostsFile {known_hosts_path}\n",
                encoding="ascii",
            )
            config_path.chmod(0o600)
        remote = [
            "/usr/bin/ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-F", str(config_path), "-i", str(key_path), f"{user}@{host}",
        ]
        return ssh_dir, remote, created
    except BaseException:
        if created:
            _remove_owned_ssh_dir(ssh_dir, temp_root)
        raise


def _remove_owned_ssh_dir(ssh_dir: Path, temp_root: Path) -> None:
    if ssh_dir != temp_root / "platform-host-capability-ssh":
        raise VerificationError("production SSH cleanup path is invalid")
    try:
        metadata = ssh_dir.lstat()
    except FileNotFoundError:
        return
    if ssh_dir.is_symlink() or not ssh_dir.is_dir() or metadata.st_uid != os.getuid():
        raise VerificationError("production SSH cleanup target is unsafe")
    shutil.rmtree(ssh_dir)


def _remote(remote: list[str], *fixed_args: str, timeout: int = 60, max_output: int = MAX_REMOTE_OUTPUT) -> bytes:
    return _run([*remote, *fixed_args], timeout=timeout, max_output=max_output)


def _locked_final_generation_check(
    remote: list[str],
    *,
    sha: str,
    toolset_version: str,
    generation: str,
    digests: list[tuple[str, str]],
    manifest_hash: str,
    capabilities_hash: str,
) -> bool:
    """Return false only when a canonical lock is busy; validate under both."""

    members = _locked_generation_members(digests, toolset_version, manifest_hash, capabilities_hash)
    expected_member_count = 16 if toolset_version == "production-host-tools-v3" else 17
    payload = {
        "generation": generation,
        "source_sha": sha,
        "toolset_version": toolset_version,
        "manifest_sha256": manifest_hash,
        "capabilities_sha256": capabilities_hash,
        "expected_member_count": expected_member_count,
        "members": members,
    }
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).decode("ascii")
    code = base64.b64encode(LOCKED_FINAL_CHECK.encode("ascii")).decode("ascii")
    remote_command = " ".join(shlex.quote(value) for value in (
        "/usr/bin/python3.12", "-I", "-B", "-c",
        f"import base64; exec(base64.b64decode('{code}'))", encoded,
    ))
    try:
        output = _run([*remote, remote_command], timeout=35, max_output=1024)
    except RemoteCommandFailure as exc:
        if exc.returncode == 75:
            return False
        raise
    expected = f"HOST_TOOLS_READY schema=1 source_sha={sha} manifest_sha256={manifest_hash}\n".encode("ascii")
    if output != expected:
        raise VerificationError("locked host-tools generation verification failed")
    return True


def _locked_generation_members(
    digests: list[tuple[str, str]], toolset_version: str,
    manifest_hash: str, capabilities_hash: str,
) -> list[dict[str, object]]:
    """Build the exact v3/v4 installed inventory, with root files only once."""

    members = [
        {"name": name, "sha256": digest, "mode": 0o555}
        for digest, name in digests
        if name not in {"manifest.json", "capabilities.txt"}
    ]
    members.extend((
        {"name": "manifest.json", "sha256": manifest_hash, "mode": 0o444},
        {"name": "capabilities.txt", "sha256": capabilities_hash, "mode": 0o444},
    ))
    names = [item["name"] for item in members]
    expected_count = {"production-host-tools-v3": 16, "production-host-tools-v4": 17}.get(toolset_version)
    if expected_count is None or len(names) != expected_count or len(set(names)) != expected_count:
        raise VerificationError("host-tools locked inventory cardinality is invalid")
    if sum(item["name"] == "capabilities.txt" and item["mode"] == 0o444 for item in members) != 1:
        raise VerificationError("host-tools capabilities inventory is invalid")
    return members


def verify_installed_generation(
    sha: str, toolset_version: str, contract_dir: Path, *, wait_seconds: int = 0,
) -> tuple[Path, Path, bool]:
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 1800:
        raise VerificationError("host-tools generation wait is invalid")
    if SHA_RE.fullmatch(sha) is None or toolset_version not in {"production-host-tools-v3", "production-host-tools-v4"}:
        raise VerificationError("host-tools generation pin is invalid")
    host, user, key = (os.environ.get(name, "") for name in ("PROD_SSH_HOST", "PROD_SSH_USER", "PROD_SSH_KEY"))
    runner_temp = Path(os.environ.get("RUNNER_TEMP", ""))
    if not runner_temp.is_absolute() or not runner_temp.is_dir() or runner_temp.is_symlink():
        raise VerificationError("runner temporary directory is invalid")
    digests, modes, manifest_hash, capabilities_hash = _contract_rows(contract_dir)
    sha_path = _regular_file(contract_dir / "source_sha", max_bytes=64).decode("ascii").strip()
    version_path = _regular_file(contract_dir / "toolset_version", max_bytes=128).decode("ascii").strip()
    if sha_path != sha or version_path != toolset_version:
        raise VerificationError("host-tools local contract does not match the selected pin")

    ssh_dir, remote, created = _safe_host_ssh(host, user, key, runner_temp)
    generation = f"{GENERATION_ROOT}/{sha}"
    try:
        if _remote(remote, "/usr/bin/id", "-u").strip() != b"0":
            raise VerificationError("production SSH identity is not root")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _remote(remote, "/usr/bin/test", "-d", generation)
                break
            except RemoteCommandFailure as exc:
                if exc.returncode != 1:
                    raise
                if time.monotonic() >= deadline:
                    raise GenerationAbsent("exact host-tools generation is not installed") from exc
                time.sleep(min(10, max(0.0, deadline - time.monotonic())))
        if _remote(remote, "/usr/bin/test", "!", "-L", generation) != b"":
            raise VerificationError("host-tools generation is missing or unsafe")
        if _remote(remote, "/usr/bin/stat", "-c", "%F:%u:%g:%h:%a", "--", generation).strip() != b"directory:0:0:2:555":
            raise VerificationError("host-tools generation metadata is unsafe")

        expected: set[str] = {"manifest.json", "capabilities.txt"}
        for index, ((expected_hash, relative), (expected_mode, mode_path)) in enumerate(zip(digests, modes, strict=True), start=1):
            if relative != mode_path:
                raise VerificationError("host-tools contract row binding changed")
            expected.add(relative)
            path = f"{generation}/{relative}"
            expected_mode = "444" if relative in {"manifest.json", "capabilities.txt"} else expected_mode
            metadata = _remote(remote, "/usr/bin/stat", "-c", "%F:%u:%g:%h:%a", "--", path).strip()
            if metadata != f"regular file:0:0:1:{expected_mode}".encode():
                raise VerificationError(f"host-tools payload metadata failed at row {index}")
            output = _remote(remote, "/usr/bin/sha256sum", "--", path, max_output=1024).decode("ascii", "strict").split()
            if len(output) != 2 or output[0] != expected_hash or output[1] != path:
                raise VerificationError(f"host-tools payload digest failed at row {index}")
        for relative, expected_hash in (("manifest.json", manifest_hash), ("capabilities.txt", capabilities_hash)):
            path = f"{generation}/{relative}"
            output = _remote(remote, "/usr/bin/sha256sum", "--", path, max_output=1024).decode("ascii", "strict").split()
            if len(output) != 2 or output[0] != expected_hash or output[1] != path:
                raise VerificationError("host-tools root payload digest failed")
            if _remote(remote, "/usr/bin/stat", "-c", "%F:%u:%g:%h:%a", "--", path).strip() != b"regular file:0:0:1:444":
                raise VerificationError("host-tools root payload metadata is unsafe")

        # OpenSSH preserves NUL bytes in stdout; compare exact leaf names rather
        # than line-oriented output, so names containing controls cannot hide.
        inventory = _remote(remote, "/usr/bin/find", "--", generation, "-mindepth", "1", "-maxdepth", "1", "-print0", max_output=MAX_REMOTE_OUTPUT)
        names = inventory.split(b"\x00")
        if names and names[-1] == b"":
            names.pop()
        prefix = (generation + "/").encode()
        if len(names) != len(expected) or any(not name.startswith(prefix) for name in names):
            raise VerificationError("host-tools generation inventory is incomplete")
        relatives = [name[len(prefix):].decode("ascii", "strict") for name in names]
        if len(relatives) != len(set(relatives)) or set(relatives) != expected:
            raise VerificationError("host-tools generation inventory differs from contract")

        if toolset_version == "production-host-tools-v3":
            expected_capability = (
                f"HOST_TOOLS schema=1 source_sha={sha} generation={sha} dispatcher=4 artifact_prepare=2 "
                "supervisor=3 input_guard=2 release_baseline=1 retained_load_export_cleanup=1 "
                "retained_load_source_binding=1 python_isolated=1 python_bytecode_disabled=1\n"
            )
        else:
            expected_capability = (
                f"HOST_TOOLS schema=1 source_sha={sha} generation={sha} dispatcher=4 artifact_prepare=2 "
                "supervisor=3 input_guard=2 release_baseline=1 retained_load_export_cleanup=1 "
                "retained_load_source_binding=1 cpu_diagnostic_plan_control=1 python_isolated=1 python_bytecode_disabled=1\n"
            )
        dispatcher = f"{generation}/platform_workflow_remote_dispatch.py"
        capability_output = _remote(remote, "/usr/bin/python3.12", "-I", "-B", dispatcher, "host-capabilities", timeout=20, max_output=512)
        if capability_output != expected_capability.encode("ascii"):
            raise VerificationError("installed host-tools capability self-test failed")
        # A provisioner may publish the exact generation while holding both
        # canonical locks and run the same self-test. Retry only true lock
        # contention; malformed or missing lock state remains fail-closed.
        while True:
            if _locked_final_generation_check(
                remote,
                sha=sha,
                toolset_version=toolset_version,
                generation=generation,
                digests=digests,
                manifest_hash=manifest_hash,
                capabilities_hash=capabilities_hash,
            ):
                break
            if time.monotonic() >= deadline:
                raise VerificationError("canonical production locks remained busy")
            time.sleep(min(5, max(0.0, deadline - time.monotonic())))
        return ssh_dir, Path(generation), created
    finally:
        if created:
            _remove_owned_ssh_dir(ssh_dir, runner_temp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--toolset-version", required=True)
    parser.add_argument("--contract-dir", required=True)
    parser.add_argument("--wait-for-generation-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_for_generation_seconds <= 1800:
        parser.error("generation wait must be between 0 and 1800 seconds")
    try:
        ssh_dir, generation, created = verify_installed_generation(
            args.source_sha, args.toolset_version, Path(args.contract_dir),
            wait_seconds=args.wait_for_generation_seconds,
        )
        output = Path(os.environ["GITHUB_ENV"])
        with output.open("a", encoding="utf-8") as stream:
            if not created:
                stream.write(f"HOST_TOOLS_SSH_DIR={ssh_dir}\n")
            stream.write(f"HOST_TOOLS_GENERATION={generation}\n")
        print("Verified installed host-tools generation and dispatcher capabilities.")
        return 0
    except GenerationAbsent:
        print("Exact host-tools generation is not installed yet.", file=sys.stderr)
        return 10
    except (KeyError, OSError, UnicodeError, VerificationError) as exc:
        if isinstance(exc, VerificationError) and exc.diagnostic_code is not None:
            print(f"{exc} [reason={exc.diagnostic_code}]", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
