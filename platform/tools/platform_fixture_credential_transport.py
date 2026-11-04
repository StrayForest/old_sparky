"""Encrypt credential-bearing external-fixture handoffs for public Actions artifacts.

The public recipient and age release are intentionally pinned here. The private
identity is supplied by a fixture-only GitHub environment secret and is never
written to this repository or an Actions artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import NoReturn
import urllib.request


AGE_VERSION = "v1.3.2"
AGE_ARCHIVE_URL = (
    "https://github.com/FiloSottile/age/releases/download/v1.3.2/"
    "age-v1.3.2-linux-amd64.tar.gz"
)
AGE_ARCHIVE_SHA256 = "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"
AGE_RECIPIENT = "age1egptr8zhhvfe0ggmyu4cr9azhfsjpvvn3p5eyppznnzx8c5yedjsyeaasa"
MAX_PRIVATE_INPUT_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 24 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = MAX_MANIFEST_BYTES + 1024 * 1024

INPUT_KEYS = {
    "schema", "confirmation", "target_sha", "control_email", "setup_concurrency",
    "run_id", "profile", "tournament_count", "users_per_tournament",
    "timeout_diagnostics",
}
OPTIONAL_INPUT_KEYS = {
    "fixture_kind", "fixture_schedule", "ssr_capacity_concurrency",
    "ssr_capacity_workload", "source_binding",
}
MANIFEST_KEYS = {
    "schema", "purpose", "origin", "session_cookie_name", "csrf_cookie_name",
    "marker", "created_at", "tournaments", "users", "runner_sha",
    "app_target_sha", "source_binding", "source_binding_sha256",
}
PUBLIC_INPUT_KEYS = {
    "schema", "target_sha", "setup_concurrency", "run_id", "profile",
    "tournament_count", "users_per_tournament", "timeout_diagnostics",
    "fixture_kind", "fixture_schedule", "ssr_capacity_concurrency",
    "ssr_capacity_workload", "source_binding",
}
READ_MIX_SCHEDULE_PROFILE = "read-mix-concurrency-ramp-v1"


def _safe_error(message: str) -> "NoReturn":
    raise SystemExit(message)


def _private_read(path: Path, cap: int) -> bytes:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > cap
        ):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ):
                raise ValueError
            chunks = bytearray()
            while len(chunks) <= cap:
                block = os.read(fd, min(65536, cap + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
            if len(chunks) != before.st_size:
                raise ValueError
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
            ):
                raise ValueError
            return bytes(chunks)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        _safe_error("private fixture transport file is unavailable or unsafe")


def _atomic_private_write(path: Path, data: bytes) -> None:
    if not data or len(data) > MAX_CIPHERTEXT_BYTES:
        _safe_error("fixture transport output size is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        _safe_error("fixture transport directory permissions are unsafe")
    fd, temporary = tempfile.mkstemp(prefix=".fixture-transport-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if path.exists() or path.is_symlink():
            raise FileExistsError
        os.replace(temporary, path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError("unsafe output")
    except OSError:
        _safe_error("fixture transport output could not be written safely")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_age_binary(destination: Path) -> Path:
    """Fetch only the pinned official age executable and verify its release digest."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = destination.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        _safe_error("age tool directory permissions are unsafe")
    binary = destination / "age"
    if binary.exists():
        if not stat.S_ISREG(binary.lstat().st_mode) or stat.S_ISLNK(binary.lstat().st_mode):
            _safe_error("pinned age executable is not a regular file")
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip() == AGE_VERSION:
            return binary
        _safe_error("pinned age executable version does not match")
    with urllib.request.urlopen(AGE_ARCHIVE_URL, timeout=30) as response:
        archive_bytes = response.read(25 * 1024 * 1024 + 1)
    if len(archive_bytes) > 25 * 1024 * 1024 or hashlib.sha256(archive_bytes).hexdigest() != AGE_ARCHIVE_SHA256:
        _safe_error("pinned age release archive digest does not match")
    with tempfile.NamedTemporaryFile(dir=destination, prefix=".age-release-", delete=False) as handle:
        archive_path = Path(handle.name)
        os.fchmod(handle.fileno(), 0o600)
        handle.write(archive_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = [member for member in archive.getmembers() if member.name in {"age/age", "age/age-keygen"}]
            if len(members) != 2 or {member.name for member in members} != {"age/age", "age/age-keygen"} or any(not member.isfile() or member.size <= 0 or member.size > 64 * 1024 * 1024 for member in members):
                _safe_error("pinned age archive executable member is invalid")
            for member in members:
                source = archive.extractfile(member)
                if source is None:
                    _safe_error("pinned age archive executable is unavailable")
                executable = source.read(64 * 1024 * 1024 + 1)
                if len(executable) != member.size or len(executable) > 64 * 1024 * 1024:
                    _safe_error("pinned age archive executable size is invalid")
                output = destination / Path(member.name).name
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o700)
                try:
                    view = memoryview(executable)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short write")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False)
        if result.returncode != 0 or result.stdout.strip() != AGE_VERSION:
            _safe_error("pinned age executable version does not match")
        return binary
    except (OSError, tarfile.TarError):
        binary.unlink(missing_ok=True)
        (destination / "age-keygen").unlink(missing_ok=True)
        _safe_error("pinned age executable could not be extracted safely")
    finally:
        archive_path.unlink(missing_ok=True)


def identity_from_environment(destination: Path, variable: str) -> None:
    value = os.environ.get(variable)
    if not isinstance(value, str) or not value.startswith("AGE-SECRET-KEY-1") or "\n" in value or "\r" in value or len(value) != 74:
        _safe_error("fixture decryption identity is unavailable")
    _atomic_private_write(destination, value.encode("ascii") + b"\n")


def encrypt_file(source: Path, destination: Path, age_binary: Path) -> None:
    raw = _private_read(source, max(MAX_PRIVATE_INPUT_BYTES, MAX_MANIFEST_BYTES))
    if destination.exists() or destination.is_symlink():
        _safe_error("fixture ciphertext destination already exists")
    try:
        result = subprocess.run(
            [str(age_binary), "--encrypt", "--recipient", AGE_RECIPIENT],
            input=raw,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            _safe_error("fixture encryption failed")
        _atomic_private_write(destination, result.stdout)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        _safe_error("fixture encryption failed")


def decrypt_file(source: Path, destination: Path, identity: Path, age_binary: Path) -> None:
    _private_read(source, MAX_CIPHERTEXT_BYTES)
    _private_read(identity, 4096)
    if destination.exists() or destination.is_symlink():
        _safe_error("fixture plaintext destination already exists")
    try:
        result = subprocess.run(
            [str(age_binary), "--decrypt", "--identity", str(identity)],
            input=_private_read(source, MAX_CIPHERTEXT_BYTES),
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            _safe_error("fixture decryption failed")
        if not result.stdout or len(result.stdout) > MAX_MANIFEST_BYTES:
            _safe_error("decrypted fixture size or permissions are invalid")
    except (OSError, subprocess.TimeoutExpired):
        destination.unlink(missing_ok=True)
        _safe_error("fixture decryption failed")
    _atomic_private_write(destination, result.stdout)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def build_descriptor(
    input_path: Path, manifest_path: Path, output_path: Path,
    run_id: str, attempt: str, runner_sha: str, app_sha: str,
) -> None:
    input_raw = _private_read(input_path, MAX_PRIVATE_INPUT_BYTES)
    manifest_raw = _private_read(manifest_path, MAX_MANIFEST_BYTES)
    try:
        payload = json.loads(input_raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
        manifest = json.loads(manifest_raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _safe_error("fixture descriptor source is invalid")
    if not isinstance(payload, dict) or not isinstance(manifest, dict):
        _safe_error("fixture descriptor source is invalid")
    platform_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(platform_root))
    try:
        from tools.platform_workflow_input_guard import validate_external_payload
        payload = validate_external_payload(payload)
    except (ImportError, TypeError, ValueError):
        _safe_error("fixture descriptor external-input validation failed")
    optional = OPTIONAL_INPUT_KEYS & payload.keys()
    if set(payload) != (INPUT_KEYS | optional) or payload.get("control_email") is None:
        _safe_error("fixture descriptor input schema is invalid")
    if payload.get("target_sha") != runner_sha or payload.get("run_id") != run_id:
        _safe_error("fixture descriptor input identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", runner_sha) or not re.fullmatch(r"[0-9a-f]{40}", app_sha):
        _safe_error("fixture descriptor source SHA is invalid")
    fixture_schedule = payload.get("fixture_schedule")
    expected_manifest_keys = MANIFEST_KEYS
    if fixture_schedule is not None:
        expected_manifest_keys = MANIFEST_KEYS | {"fixture_schedule"}
    if (
        set(manifest) != expected_manifest_keys
        or manifest.get("schema") != 2
        or manifest.get("runner_sha") != runner_sha
        or manifest.get("app_target_sha") != app_sha
        or manifest.get("fixture_schedule") != fixture_schedule
    ):
        _safe_error("fixture descriptor manifest identity is invalid")
    if manifest.get("source_binding") != payload.get("source_binding"):
        _safe_error("fixture descriptor source binding differs")
    if (
        manifest.get("purpose") != "external_ready_vote"
        or manifest.get("origin") != "https://old-sparky.com"
        or not isinstance(manifest.get("session_cookie_name"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", manifest["session_cookie_name"]) is None
        or not isinstance(manifest.get("csrf_cookie_name"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", manifest["csrf_cookie_name"]) is None
        or not isinstance(manifest.get("created_at"), str)
        or re.fullmatch(r"preprod[0-9]{12}[0-9a-f]{4}", str(manifest.get("marker"))) is None
    ):
        _safe_error("fixture descriptor manifest values are invalid")
    binding = manifest.get("source_binding")
    if binding is None:
        if app_sha != runner_sha or manifest.get("source_binding_sha256") is not None:
            _safe_error("fixture descriptor same-source binding is invalid")
    else:
        if app_sha == runner_sha or not isinstance(binding, dict) or binding.get("runner_sha") != runner_sha or binding.get("app_target_sha") != app_sha:
            _safe_error("fixture descriptor no-op binding is invalid")
        canonical = json.dumps(binding, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
        if hashlib.sha256(canonical).hexdigest() != manifest.get("source_binding_sha256"):
            _safe_error("fixture descriptor binding digest is invalid")
    tournaments, users = manifest.get("tournaments"), manifest.get("users")
    if not isinstance(tournaments, list) or not 1 <= len(tournaments) <= 40 or not isinstance(users, list) or not 14 <= len(users) <= 20_000:
        _safe_error("fixture descriptor counts are invalid")
    if fixture_schedule is not None and (
        payload.get("fixture_kind") != "external_load"
        or payload.get("timeout_diagnostics") != "false"
        or payload.get("tournament_count") != "40"
        or payload.get("users_per_tournament") != "500"
        or len(tournaments) != 40
        or len(users) != 20_000
    ):
        _safe_error("fixture descriptor scheduled dimensions are invalid")
    tournament_row_keys = {"id", "slug", "user_count"}
    if fixture_schedule is not None:
        tournament_row_keys |= {"ready_check_starts_at", "ready_check_ends_at"}
    for row in tournaments:
        if (not isinstance(row, dict) or set(row) != tournament_row_keys
                or not isinstance(row.get("id"), str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"]) is None
                or not isinstance(row.get("slug"), str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["slug"]) is None
                or type(row.get("user_count")) is not int or row["user_count"] < 14):
            _safe_error("fixture descriptor tournament row is invalid")
        if fixture_schedule is not None and any(
            not isinstance(row.get(field), str) or not row[field]
            for field in ("ready_check_starts_at", "ready_check_ends_at")
        ):
            _safe_error("fixture descriptor schedule deadlines are invalid")
    for row in users:
        if not isinstance(row, dict) or set(row) != {"user_id", "tournament_slug", "session_token", "csrf_token"}:
            _safe_error("fixture descriptor user row is invalid")
        for key in ("user_id", "tournament_slug", "session_token", "csrf_token"):
            if not isinstance(row.get(key), str) or re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", row[key]) is None:
                _safe_error("fixture descriptor user field is invalid")
    public_input = {key: value for key, value in payload.items() if key in PUBLIC_INPUT_KEYS}
    descriptor = {
        "schema": 1,
        "run_id": run_id,
        "run_attempt": attempt,
        "runner_sha": runner_sha,
        "app_target_sha": app_sha,
        "input": public_input,
        "manifest": {
            "sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "size_bytes": len(manifest_raw),
            "schema": 2,
            "purpose": "external_ready_vote",
            "origin": "https://old-sparky.com",
            "runner_sha": runner_sha,
            "app_target_sha": app_sha,
            "source_binding": binding,
            "source_binding_sha256": manifest.get("source_binding_sha256"),
            "tournament_count": len(tournaments),
            "user_count": len(users),
            "users_per_tournament": sorted(
                row.get("user_count") for row in tournaments
                if isinstance(row, dict) and type(row.get("user_count")) is int
            ),
        },
    }
    if fixture_schedule is not None:
        descriptor["manifest"]["fixture_schedule"] = fixture_schedule
    if len(descriptor["manifest"]["users_per_tournament"]) != len(tournaments):
        _safe_error("fixture descriptor tournament counts are invalid")
    output = (json.dumps(descriptor, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    _atomic_private_write(output_path, output)


def validate_descriptor(
    descriptor_path: Path, run_id: str, attempt: str, runner_sha: str, app_sha: str,
) -> dict[str, object]:
    raw = _private_read(descriptor_path, 64 * 1024)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _safe_error("fixture descriptor is invalid")
    if not isinstance(value, dict) or set(value) != {"schema", "run_id", "run_attempt", "runner_sha", "app_target_sha", "input", "manifest"}:
        _safe_error("fixture descriptor schema is invalid")
    if (type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("run_id") != run_id or value.get("run_attempt") != attempt
            or value.get("runner_sha") != runner_sha or value.get("app_target_sha") != app_sha
            or re.fullmatch(r"[1-9][0-9]{0,31}", run_id) is None
            or re.fullmatch(r"[1-9][0-9]{0,8}", attempt) is None
            or re.fullmatch(r"[0-9a-f]{40}", runner_sha) is None
            or re.fullmatch(r"[0-9a-f]{40}", app_sha) is None):
        _safe_error("fixture descriptor run/source binding is invalid")
    input_value = value.get("input")
    manifest = value.get("manifest")
    if not isinstance(input_value, dict) or not isinstance(manifest, dict):
        _safe_error("fixture descriptor payload is invalid")
    if "control_email" in input_value or "confirmation" in input_value:
        _safe_error("fixture descriptor contains a private input field")
    fixture_kind = input_value.get("fixture_kind")
    fixture_schedule = input_value.get("fixture_schedule")
    expected_input_keys = {
        "schema", "target_sha", "setup_concurrency", "run_id", "profile",
        "tournament_count", "users_per_tournament", "timeout_diagnostics", "fixture_kind",
    }
    if "fixture_schedule" in input_value:
        expected_input_keys.add("fixture_schedule")
        if (
            fixture_kind != "external_load"
            or fixture_schedule != READ_MIX_SCHEDULE_PROFILE
        ):
            _safe_error("fixture descriptor schedule is invalid")
    if input_value.get("schema") in (2, "2"):
        expected_input_keys.add("source_binding")
    if fixture_kind in {"ssr_capacity_diagnostic_v1", "ssr_html_capacity_diagnostic_v1", "ssr_html_capacity_composite_diagnostic_v1"}:
        expected_input_keys.add("ssr_capacity_concurrency")
    if fixture_kind in {"ssr_html_capacity_diagnostic_v1", "ssr_html_capacity_composite_diagnostic_v1"}:
        expected_input_keys.add("ssr_capacity_workload")
    schema = input_value.get("schema")
    if type(schema) not in (int, str) or isinstance(schema, bool) or schema not in (1, "1", 2, "2"):
        _safe_error("fixture descriptor input schema is invalid")
    if set(input_value) != expected_input_keys:
        _safe_error("fixture descriptor input schema is invalid")
    if (input_value.get("target_sha") != runner_sha or input_value.get("run_id") != run_id
            or input_value.get("profile") != "external-vote"
            or fixture_kind not in {"external_load", "cpu_diagnostic_only_v1", "ssr_capacity_diagnostic_v1", "ssr_html_capacity_diagnostic_v1", "ssr_html_capacity_composite_diagnostic_v1"}
            or not isinstance(input_value.get("setup_concurrency"), str) or re.fullmatch(r"[1-9][0-9]{0,2}", input_value["setup_concurrency"]) is None
            or not isinstance(input_value.get("tournament_count"), str) or re.fullmatch(r"[1-9][0-9]{0,2}", input_value["tournament_count"]) is None
            or not isinstance(input_value.get("users_per_tournament"), str) or re.fullmatch(r"[1-9][0-9]{0,3}", input_value["users_per_tournament"]) is None
            or input_value.get("timeout_diagnostics") not in {"true", "false"}):
        _safe_error("fixture descriptor input identity is invalid")
    if fixture_schedule is not None and (
        input_value.get("timeout_diagnostics") != "false"
        or input_value.get("tournament_count") != "40"
        or input_value.get("users_per_tournament") != "500"
    ):
        _safe_error("fixture descriptor scheduled dimensions are invalid")
    if fixture_kind in {"cpu_diagnostic_only_v1", "ssr_capacity_diagnostic_v1", "ssr_html_capacity_diagnostic_v1", "ssr_html_capacity_composite_diagnostic_v1"}:
        if input_value.get("timeout_diagnostics") != "false" or input_value.get("tournament_count") != "1" or input_value.get("users_per_tournament") != "14":
            _safe_error("fixture descriptor diagnostic dimensions are invalid")
    binding = input_value.get("source_binding")
    if input_value.get("schema") in (1, "1"):
        if "source_binding" in input_value or runner_sha != app_sha:
            _safe_error("fixture descriptor same-source identity is invalid")
    else:
        if not isinstance(binding, dict) or runner_sha == app_sha or binding.get("runner_sha") != runner_sha or binding.get("app_target_sha") != app_sha:
            _safe_error("fixture descriptor no-op identity is invalid")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from tools.platform_workflow_input_guard import _validate_source_binding_payload
            _validate_source_binding_payload(binding)
        except (ImportError, TypeError, ValueError):
            _safe_error("fixture descriptor no-op identity is invalid")
    if fixture_kind == "ssr_capacity_diagnostic_v1" and (input_value.get("ssr_capacity_concurrency") not in {"8", "16"} or "ssr_capacity_workload" in input_value):
        _safe_error("fixture descriptor capacity input is invalid")
    if fixture_kind in {"ssr_html_capacity_diagnostic_v1", "ssr_html_capacity_composite_diagnostic_v1"}:
        allowed = {"32"} if fixture_kind == "ssr_html_capacity_composite_diagnostic_v1" else {"8", "16", "32", "64"}
        expected_workload = "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1" if fixture_kind == "ssr_html_capacity_composite_diagnostic_v1" else "authenticated_html_ssr_capacity_v1"
        if input_value.get("ssr_capacity_concurrency") not in allowed or input_value.get("ssr_capacity_workload") != expected_workload:
            _safe_error("fixture descriptor HTML capacity input is invalid")
    expected_manifest = {
        "sha256", "size_bytes", "schema", "purpose", "origin", "runner_sha",
        "app_target_sha", "source_binding", "source_binding_sha256",
        "tournament_count", "user_count", "users_per_tournament",
    }
    if fixture_schedule is not None:
        expected_manifest.add("fixture_schedule")
    if set(manifest) != expected_manifest:
        _safe_error("fixture descriptor manifest schema is invalid")
    if (not isinstance(manifest.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is None
            or type(manifest.get("size_bytes")) is not int or not 1 <= manifest["size_bytes"] <= MAX_MANIFEST_BYTES
            or manifest.get("schema") != 2 or manifest.get("purpose") != "external_ready_vote"
            or manifest.get("origin") != "https://old-sparky.com"
            or manifest.get("runner_sha") != runner_sha or manifest.get("app_target_sha") != app_sha
            or manifest.get("fixture_schedule") != fixture_schedule
            or type(manifest.get("tournament_count")) is not int or not 1 <= manifest["tournament_count"] <= 40
            or type(manifest.get("user_count")) is not int or not 14 <= manifest["user_count"] <= 20_000
            or not isinstance(manifest.get("users_per_tournament"), list)
            or len(manifest["users_per_tournament"]) != manifest["tournament_count"]
            or any(type(count) is not int or count < 14 for count in manifest["users_per_tournament"])):
        _safe_error("fixture descriptor manifest identity or counts are invalid")
    if fixture_schedule is not None and (
        manifest.get("tournament_count") != 40
        or manifest.get("user_count") != 20_000
        or manifest.get("users_per_tournament") != [500] * 40
    ):
        _safe_error("fixture descriptor scheduled manifest dimensions are invalid")
    binding = manifest.get("source_binding")
    if (binding != input_value.get("source_binding")
            or (binding is None and (runner_sha != app_sha or manifest.get("source_binding_sha256") is not None))):
        _safe_error("fixture descriptor no-op source binding is invalid")
    if binding is not None:
        if runner_sha == app_sha or not isinstance(binding, dict) or binding.get("runner_sha") != runner_sha or binding.get("app_target_sha") != app_sha:
            _safe_error("fixture descriptor no-op source binding is invalid")
        canonical = json.dumps(binding, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
        if hashlib.sha256(canonical).hexdigest() != manifest.get("source_binding_sha256"):
            _safe_error("fixture descriptor source-binding digest is invalid")
    return value


def _command() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install-age")
    install.add_argument("--directory", required=True, type=Path)
    identity_parser = subparsers.add_parser("identity-from-env")
    identity_parser.add_argument("--output", required=True, type=Path)
    identity_parser.add_argument("--variable", default="FIXTURE_MANIFEST_AGE_IDENTITY")
    encrypt = subparsers.add_parser("encrypt")
    encrypt.add_argument("--input", required=True, type=Path)
    encrypt.add_argument("--output", required=True, type=Path)
    encrypt.add_argument("--age", required=True, type=Path)
    decrypt = subparsers.add_parser("decrypt")
    decrypt.add_argument("--input", required=True, type=Path)
    decrypt.add_argument("--output", required=True, type=Path)
    decrypt.add_argument("--identity", required=True, type=Path)
    decrypt.add_argument("--age", required=True, type=Path)
    descriptor = subparsers.add_parser("descriptor")
    descriptor.add_argument("--input", required=True, type=Path)
    descriptor.add_argument("--manifest", required=True, type=Path)
    descriptor.add_argument("--output", required=True, type=Path)
    descriptor.add_argument("--run-id", required=True)
    descriptor.add_argument("--attempt", required=True)
    descriptor.add_argument("--runner-sha", required=True)
    descriptor.add_argument("--app-sha", required=True)
    check_descriptor = subparsers.add_parser("validate-descriptor")
    check_descriptor.add_argument("--descriptor", required=True, type=Path)
    check_descriptor.add_argument("--run-id", required=True)
    check_descriptor.add_argument("--attempt", required=True)
    check_descriptor.add_argument("--runner-sha", required=True)
    check_descriptor.add_argument("--app-sha", required=True)
    args = parser.parse_args()
    if args.command == "install-age":
        print(ensure_age_binary(args.directory))
    elif args.command == "identity-from-env":
        identity_from_environment(args.output, args.variable)
    elif args.command == "encrypt":
        encrypt_file(args.input, args.output, args.age)
    elif args.command == "decrypt":
        decrypt_file(args.input, args.output, args.identity, args.age)
    elif args.command == "descriptor":
        build_descriptor(args.input, args.manifest, args.output, args.run_id, args.attempt, args.runner_sha, args.app_sha)
    elif args.command == "validate-descriptor":
        validate_descriptor(args.descriptor, args.run_id, args.attempt, args.runner_sha, args.app_sha)
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_command())
