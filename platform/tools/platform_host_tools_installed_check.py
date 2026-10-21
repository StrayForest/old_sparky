#!/usr/bin/env python3
"""Verify one exact, already-provisioned host-tools generation over pinned SSH.

This is a read-only runner-side verifier shared by production preflight and the
bootstrap coordinator. Its remote command set and paths are fixed here; callers
provide only the verified generation SHA and local bundle contract directory.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time


HOST_KEY_FINGERPRINT = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
GENERATION_ROOT = "/opt/oldsparky/platform/shared/host-tools"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
PATH_RE = re.compile(r"(?:platform_[A-Za-z0-9_.-]+\.(?:py|sh)|capabilities\.txt|manifest\.json)\Z")
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
MAX_CONTRACT_ROWS = 32
MAX_REMOTE_OUTPUT = 16384
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
