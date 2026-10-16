#!/usr/bin/env python3
"""Decrypt one source-bound CMS maintenance-evidence artifact locally."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any
import zipfile


RECIPIENT_CERTIFICATE_SHA256 = "28d857f5fc6ff987444ca920a2bb5072081dbf2e41ef158d362b60e45e3f67a3"
ENCRYPTION_PROFILE = "CMS-EnvelopedData/AES-256-GCM/RSA-OAEP-SHA-256"
MAX_PLAINTEXT_BYTES = 196608
MAX_CIPHERTEXT_BYTES = 384 * 1024
MAX_RUNTIME_REMOTE_STDERR_BYTES = 128 * 1024
MAX_RUNTIME_MARKER_OUTPUT_BYTES = 16 * 1024
MANIFEST_KEYS = {
    "schema", "kind", "repository", "workflow_path", "capture_run_id",
    "capture_run_attempt", "source_sha", "bundle_sha256", "security_run_id",
    "security_run_attempt", "recovery_run_id", "recovery_run_attempt",
    "publisher_run_id", "publisher_run_attempt", "recipient_certificate_sha256",
    "encryption", "ciphertext_sha256", "ciphertext_size",
}
LOCAL_BINDING_KEYS = {
    "schema", "kind", "repository", "workflow_path", "artifact_id",
    "artifact_name", "artifact_digest", "capture_run_id", "capture_run_attempt",
    "source_sha", "bundle_sha256", "security_run_id", "security_run_attempt",
    "recovery_run_id", "recovery_run_attempt", "publisher_run_id",
    "publisher_run_attempt",
}
EVIDENCE_KEYS = {
    "schema", "kind", "source_sha", "bundle_sha256", "capture_run_id",
    "capture_run_attempt", "security_run_id", "security_run_attempt",
    "host_id_sha256", "captured_at_utc", "app_dir", "pointers", "receipts",
    "unit", "timer", "queued_jobs", "cgroup_pids", "journal",
}
DIAGNOSTIC_KEYS = {
    "schema", "kind", "source_sha", "bundle_sha256", "capture_run_id",
    "capture_run_attempt", "security_run_id", "security_run_attempt",
    "captured_at_utc", "stage", "error_code",
}
DIAGNOSTIC_PRIVATE_CAUSE_KEYS = {
    "command", "getter", "unit", "reason", "error_message", "exception_class",
    "cause_class", "cause_message", "returncode", "missing_fields", "extra_fields",
    "stdout_b64", "stdout_truncated", "stderr_b64", "stderr_truncated",
}
SERVICE_PROPERTIES = {
    "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
    "MainPID", "ExecMainPID", "ControlGroup", "ExecMainStartTimestamp",
    "ExecMainExitTimestamp", "StateChangeTimestamp", "UnitFileState",
}
TIMER_PROPERTIES = {
    "ActiveState", "SubState", "Result", "StateChangeTimestamp",
    "UnitFileState",
}
DIAGNOSTIC_STAGES = {
    "transaction_receipt", "transaction_context", "host_identity",
    "systemd_properties", "systemd_jobs", "cgroup", "journal",
    "evidence_write", "capture_guard",
}
DIAGNOSTIC_CODES = {
    "receipt_invalid", "receipt_binding_mismatch", "host_identity_unavailable",
    "unit_properties_unavailable", "jobs_unavailable_or_malformed",
    "cgroup_unavailable_or_malformed", "journal_unavailable_or_malformed",
    "evidence_destination_invalid", "capture_guard_failed",
}
RUN_ID = re.compile(r"^[1-9][0-9]{0,31}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40,64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_BINDING_KEYS = {
    "schema", "kind", "repository", "workflow_path", "artifact_id",
    "artifact_name", "artifact_digest", "route", "workflow_run_id",
    "workflow_run_attempt", "workflow_sha", "source_sha", "bundle_sha256",
    "security_run_id", "security_run_attempt", "recovery_run_id",
    "recovery_run_attempt", "publisher_run_id", "publisher_run_attempt",
}
RUNTIME_PROVENANCE_KEYS = {
    "route", "workflow_run_id", "workflow_run_attempt", "workflow_sha",
    "source_sha", "bundle_sha256", "security_run_id", "security_run_attempt",
    "recovery_run_id", "recovery_run_attempt", "publisher_run_id",
    "publisher_run_attempt",
}
RUNTIME_MANIFEST_KEYS = {
    "schema", "kind", "repository", "workflow_path", *RUNTIME_PROVENANCE_KEYS,
    "recipient_certificate_sha256", "encryption", "ciphertext_sha256",
    "ciphertext_size",
}
RUNTIME_PLAINTEXT_KEYS = {
    "schema", "kind", "repository", "workflow_path", *RUNTIME_PROVENANCE_KEYS,
    "remote_stderr_b64", "remote_stderr_size",
}


class DecryptError(ValueError):
    """A closed failure that never includes evidence content or key data."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DecryptError("evidence JSON has duplicate keys")
        value[key] = item
    return value


def _parse_json(raw: bytes, *, maximum: int) -> dict[str, Any]:
    if not 1 <= len(raw) <= maximum:
        raise DecryptError("capture JSON exceeded its bound")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, DecryptError) as exc:
        raise DecryptError("capture JSON is invalid") from exc
    if not isinstance(value, dict):
        raise DecryptError("capture JSON is invalid")
    return value


def _valid_private_diagnostic_cause(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != DIAGNOSTIC_PRIVATE_CAUSE_KEYS:
        return False
    reasons = {
        "none": {"exception"},
        "systemctl_show": {
            "command_error", "output_too_large", "nonzero_exit", "invalid_utf8",
            "malformed_output", "missing_properties",
        },
        "systemctl_list_jobs": {
            "command_error", "output_too_large", "nonzero_exit", "invalid_utf8",
            "malformed_header", "duplicate_summary", "malformed_row",
            "missing_summary", "count_mismatch", "unexpected_zero_table",
        },
        "journalctl_unit": {
            "command_error", "output_too_large", "nonzero_exit",
            "malformed_entry", "too_many_entries",
        },
        "cgroup_read": {
            "invalid_identity", "unavailable", "output_too_large", "invalid_ascii",
            "malformed_pid", "too_many_members",
        },
    }
    command = value.get("command")
    getter_by_command = {
        "none": set(DIAGNOSTIC_STAGES),
        "systemctl_show": {"systemd_properties"},
        "systemctl_list_jobs": {"systemd_jobs"},
        "journalctl_unit": {"journal"},
        "cgroup_read": {"cgroup"},
    }
    text_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,79}$")
    unit_allowed = {
        "none": {"none"},
        "systemctl_show": {"service", "timer"},
        "systemctl_list_jobs": {"none"},
        "journalctl_unit": {"service"},
        "cgroup_read": {"service"},
    }
    if (
        command not in reasons
        or value.get("getter") not in getter_by_command.get(command, set())
        or value.get("unit") not in unit_allowed.get(command, set())
        or value.get("reason") not in reasons.get(command, set())
        or not isinstance(value.get("error_message"), str)
        or not 1 <= len(value["error_message"]) <= 1024
        or not isinstance(value.get("exception_class"), str)
        or text_pattern.fullmatch(value["exception_class"]) is None
        or (
            value.get("cause_class") is not None
            and (not isinstance(value["cause_class"], str) or text_pattern.fullmatch(value["cause_class"]) is None)
        )
        or (
            value.get("cause_message") is not None
            and (not isinstance(value["cause_message"], str) or len(value["cause_message"]) > 1024)
        )
        or ((value.get("cause_class") is None) != (value.get("cause_message") is None))
        or (value.get("returncode") is not None and (type(value["returncode"]) is not int or not -255 <= value["returncode"] <= 255))
        or type(value.get("stdout_truncated")) is not bool
        or type(value.get("stderr_truncated")) is not bool
    ):
        return False
    missing = value.get("missing_fields")
    extra = value.get("extra_fields")
    if (
        not isinstance(missing, list) or len(missing) > 32
        or any(not isinstance(item, str) or len(item) > 128 for item in missing)
        or missing != sorted(set(missing))
        or not isinstance(extra, list) or len(extra) > 32
        or any(not isinstance(item, str) or len(item) > 128 for item in extra)
        or extra != sorted(set(extra))
    ):
        return False
    for name in ("stdout_b64", "stderr_b64"):
        encoded = value.get(name)
        if not isinstance(encoded, str) or len(encoded) > 5464:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            return False
        if len(decoded) > 4096 or base64.b64encode(decoded).decode("ascii") != encoded:
            return False
    return True


def _run_openssl(arguments: list[str], *, maximum: int = 16 * 1024) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/openssl", *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            timeout=30, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecryptError("OpenSSL CMS operation failed") from exc
    if result.returncode != 0 or len(result.stdout) > maximum:
        raise DecryptError("OpenSSL CMS operation failed")
    return result.stdout


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def decrypt_capture(
    *, artifact_archive: Path, binding_json: Path, private_key: Path,
    certificate: Path, destination: Path, git_dir: Path,
) -> tuple[str, int]:
    git_dir = git_dir.resolve(strict=True)
    binding_info = binding_json.lstat()
    if (
        not stat.S_ISREG(binding_info.st_mode)
        or stat.S_ISLNK(binding_info.st_mode)
        or binding_info.st_uid != os.geteuid()
        or binding_info.st_nlink != 1
        or stat.S_IMODE(binding_info.st_mode) != 0o600
        or not _inside(binding_json.resolve(strict=True), git_dir)
    ):
        raise DecryptError("local API binding must be a protected git-private file")
    binding_raw = binding_json.read_bytes()
    if len(binding_raw) != binding_info.st_size:
        raise DecryptError("local API binding changed while reading")
    binding = _parse_json(binding_raw, maximum=8192)
    archive_info = artifact_archive.lstat()
    if (
        not stat.S_ISREG(archive_info.st_mode)
        or stat.S_ISLNK(archive_info.st_mode)
        or archive_info.st_uid != os.geteuid()
        or archive_info.st_nlink != 1
        or stat.S_IMODE(archive_info.st_mode) != 0o600
        or archive_info.st_size < 1
        or archive_info.st_size > 1024 * 1024
        or not _inside(artifact_archive.resolve(strict=True), git_dir)
    ):
        raise DecryptError("local capture archive must be a protected git-private file")
    archive_bytes = artifact_archive.read_bytes()
    if len(archive_bytes) != archive_info.st_size:
        raise DecryptError("local capture archive changed while reading")
    artifact_id = binding.get("artifact_id")
    artifact_name = binding.get("artifact_name")
    capture_run_id = binding.get("capture_run_id")
    capture_run_attempt = binding.get("capture_run_attempt")
    expected_archive_digest = binding.get("artifact_digest")
    if (
        set(binding) != LOCAL_BINDING_KEYS
        or binding.get("schema") != 1
        or binding.get("kind") != "github_capture_artifact_binding"
        or binding.get("repository") != "StrayForest/old_sparky"
        or binding.get("workflow_path") != ".github/workflows/platform-production-recovery-bootstrap-abort.yml"
        or not isinstance(artifact_id, str)
        or not RUN_ID.fullmatch(artifact_id)
        or not isinstance(capture_run_id, str)
        or not RUN_ID.fullmatch(capture_run_id)
        or not isinstance(capture_run_attempt, str)
        or not RUN_ID.fullmatch(capture_run_attempt)
        or artifact_name != f"platform-maintenance-failure-{capture_run_id}-{capture_run_attempt}"
        or not isinstance(expected_archive_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_archive_digest) is None
        or hashlib.sha256(archive_bytes).hexdigest() != expected_archive_digest.removeprefix("sha256:")
    ):
        raise DecryptError("local API artifact binding is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 2 or {entry.filename for entry in entries} != {"manifest.json", "evidence.cms"}:
                raise DecryptError("capture artifact archive is not closed")
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0o177777
                if (
                    entry.is_dir()
                    or entry.flag_bits & 0x1
                    or entry.file_size > 512 * 1024
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                ):
                    raise DecryptError("capture artifact member is unsafe")
            manifest_raw = bundle.read("manifest.json")
            ciphertext = bundle.read("evidence.cms")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise DecryptError("capture artifact archive is invalid") from exc
    manifest = _parse_json(manifest_raw, maximum=8192)
    if not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise DecryptError("capture ciphertext exceeded its bound")
    if (
        set(manifest) != MANIFEST_KEYS
        or manifest.get("schema") != 1
        or manifest.get("kind") != "encrypted_maintenance_failure_report"
        or manifest.get("repository") != "StrayForest/old_sparky"
        or manifest.get("workflow_path") != ".github/workflows/platform-production-recovery-bootstrap-abort.yml"
        or manifest.get("capture_run_id") != capture_run_id
        or manifest.get("capture_run_attempt") != capture_run_attempt
        or manifest.get("source_sha") != binding.get("source_sha")
        or manifest.get("bundle_sha256") != binding.get("bundle_sha256")
        or manifest.get("security_run_id") != binding.get("security_run_id")
        or manifest.get("security_run_attempt") != binding.get("security_run_attempt")
        or manifest.get("recovery_run_id") != binding.get("recovery_run_id")
        or manifest.get("recovery_run_attempt") != binding.get("recovery_run_attempt")
        or manifest.get("publisher_run_id") != binding.get("publisher_run_id")
        or manifest.get("publisher_run_attempt") != binding.get("publisher_run_attempt")
        or not isinstance(binding.get("source_sha"), str)
        or not SOURCE_SHA.fullmatch(binding["source_sha"])
        or not isinstance(binding.get("bundle_sha256"), str)
        or not HEX64.fullmatch(binding["bundle_sha256"])
        or any(not isinstance(binding.get(key), str) or not RUN_ID.fullmatch(binding[key]) for key in (
            "security_run_id", "security_run_attempt", "recovery_run_id",
            "recovery_run_attempt", "publisher_run_id", "publisher_run_attempt",
        ))
        or manifest.get("recipient_certificate_sha256") != RECIPIENT_CERTIFICATE_SHA256
        or manifest.get("encryption") != ENCRYPTION_PROFILE
        or not isinstance(manifest.get("ciphertext_sha256"), str)
        or not HEX64.fullmatch(manifest["ciphertext_sha256"])
        or manifest.get("ciphertext_sha256") != hashlib.sha256(ciphertext).hexdigest()
        or type(manifest.get("ciphertext_size")) is not int
        or manifest.get("ciphertext_size") != len(ciphertext)
    ):
        raise DecryptError("capture manifest binding is invalid")
    key_info = private_key.lstat()
    if (
        not stat.S_ISREG(key_info.st_mode)
        or stat.S_ISLNK(key_info.st_mode)
        or key_info.st_uid != os.geteuid()
        or key_info.st_nlink != 1
        or stat.S_IMODE(key_info.st_mode) != 0o600
        or not _inside(private_key.resolve(strict=True), git_dir)
    ):
        raise DecryptError("local recipient key must be a protected git-private file")
    cert_info = certificate.lstat()
    if (
        not stat.S_ISREG(cert_info.st_mode)
        or stat.S_ISLNK(cert_info.st_mode)
        or cert_info.st_uid != os.geteuid()
        or cert_info.st_nlink != 1
        or not _inside(certificate.resolve(strict=True), git_dir)
    ):
        raise DecryptError("local recipient certificate path is unsafe")
    cert_der = _run_openssl(["x509", "-in", str(certificate), "-outform", "DER"], maximum=8192)
    if hashlib.sha256(cert_der).hexdigest() != RECIPIENT_CERTIFICATE_SHA256:
        raise DecryptError("local recipient certificate does not match source pin")
    parent = destination.parent
    parent_meta = parent.lstat()
    if (
        not stat.S_ISDIR(parent_meta.st_mode)
        or stat.S_ISLNK(parent_meta.st_mode)
        or parent_meta.st_uid != os.geteuid()
        or stat.S_IMODE(parent_meta.st_mode) != 0o700
        or not _inside(parent.resolve(strict=True), git_dir)
        or destination.exists()
        or destination.is_symlink()
    ):
        raise DecryptError("local evidence destination must be new in a protected git-private directory")
    ciphertext_descriptor, ciphertext_name = tempfile.mkstemp(prefix=".maintenance-evidence-cms-", dir=parent)
    ciphertext_path = Path(ciphertext_name)
    try:
        os.fchmod(ciphertext_descriptor, 0o600)
        with os.fdopen(ciphertext_descriptor, "wb") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        ciphertext_path.unlink(missing_ok=True)
        raise
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".maintenance-evidence-decrypt-", dir=parent)
    except BaseException:
        ciphertext_path.unlink(missing_ok=True)
        raise
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        _run_openssl([
            "cms", "-decrypt", "-binary", "-inform", "DER", "-in", str(ciphertext_path),
            "-out", str(temporary), "-recip", str(certificate), "-inkey", str(private_key),
            "-keyopt", "rsa_padding_mode:oaep", "-keyopt", "rsa_oaep_md:sha256",
        ], maximum=0)
        plaintext_info = temporary.lstat()
        if (
            not stat.S_ISREG(plaintext_info.st_mode)
            or stat.S_ISLNK(plaintext_info.st_mode)
            or plaintext_info.st_nlink != 1
            or stat.S_IMODE(plaintext_info.st_mode) != 0o600
            or not 1 <= plaintext_info.st_size <= MAX_PLAINTEXT_BYTES
        ):
            raise DecryptError("decrypted evidence metadata is unsafe")
        raw = temporary.read_bytes()
        if len(raw) != plaintext_info.st_size:
            raise DecryptError("decrypted evidence size is invalid")
        try:
            evidence = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, DecryptError) as exc:
            raise DecryptError("decrypted evidence JSON is invalid") from exc
        common_binding = (
            isinstance(evidence, dict)
            and evidence.get("schema") == 1
            and evidence.get("source_sha") == binding["source_sha"]
            and evidence.get("bundle_sha256") == binding["bundle_sha256"]
            and evidence.get("capture_run_id") == binding["capture_run_id"]
            and evidence.get("capture_run_attempt") == binding["capture_run_attempt"]
            and evidence.get("security_run_id") == binding["security_run_id"]
            and evidence.get("security_run_attempt") == binding["security_run_attempt"]
        )
        evidence_kind = isinstance(evidence, dict) and evidence.get("kind") == "maintenance_failure_evidence"
        diagnostic_kind = isinstance(evidence, dict) and evidence.get("kind") == "maintenance_failure_diagnostic"
        diagnostic_valid = (
            diagnostic_kind
            and set(evidence) in (DIAGNOSTIC_KEYS, DIAGNOSTIC_KEYS | {"private_cause"})
            and isinstance(evidence.get("captured_at_utc"), str)
            and isinstance(evidence.get("stage"), str)
            and evidence["stage"] in DIAGNOSTIC_STAGES
            and isinstance(evidence.get("error_code"), str)
            and evidence["error_code"] in DIAGNOSTIC_CODES
            and (
                "private_cause" not in evidence
                or (
                    _valid_private_diagnostic_cause(evidence["private_cause"])
                )
            )
        )
        if not common_binding or not (
            (evidence_kind and set(evidence) == EVIDENCE_KEYS)
            or diagnostic_valid
        ):
            raise DecryptError("decrypted evidence source/run binding is invalid")
        output_descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(output_descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return hashlib.sha256(raw).hexdigest(), len(raw)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            ciphertext_path.unlink()
        except FileNotFoundError:
            pass


def _validate_runtime_binding(binding: dict[str, Any]) -> None:
    if (
        set(binding) != RUNTIME_BINDING_KEYS
        or binding.get("schema") != 1
        or binding.get("kind") != "github_release_runtime_failure_binding"
        or binding.get("repository") != "StrayForest/old_sparky"
    ):
        raise DecryptError("runtime failure API binding is invalid")
    route = binding.get("route")
    if not isinstance(route, str):
        raise DecryptError("runtime failure route is invalid")
    expected_path = {
        "capture_probe": ".github/workflows/platform-production-diagnostics.yml",
        "recover_pending": ".github/workflows/platform-production-release-recover.yml",
    }.get(route)
    if binding.get("workflow_path") != expected_path:
        raise DecryptError("runtime failure workflow binding is invalid")
    for key in ("workflow_run_id", "workflow_run_attempt"):
        if not isinstance(binding.get(key), str) or not RUN_ID.fullmatch(binding[key]):
            raise DecryptError("runtime failure run binding is invalid")
    if not isinstance(binding.get("workflow_sha"), str) or not SOURCE_SHA.fullmatch(binding["workflow_sha"]):
        raise DecryptError("runtime failure workflow SHA is invalid")
    if not isinstance(binding.get("source_sha"), str) or not SOURCE_SHA.fullmatch(binding["source_sha"]):
        raise DecryptError("runtime failure source SHA is invalid")
    if route == "capture_probe":
        if (
            binding.get("source_sha") != binding.get("workflow_sha")
            or binding.get("bundle_sha256") is not None
            or any(binding.get(key) is not None for key in (
                "security_run_id", "security_run_attempt", "recovery_run_id",
                "recovery_run_attempt", "publisher_run_id", "publisher_run_attempt",
            ))
        ):
            raise DecryptError("capture-probe binding contains unexpected recovery provenance")
    else:
        if not isinstance(binding.get("bundle_sha256"), str) or not HEX64.fullmatch(binding["bundle_sha256"]):
            raise DecryptError("recovery bundle binding is invalid")
        for key in (
            "security_run_id", "security_run_attempt", "recovery_run_id",
            "recovery_run_attempt", "publisher_run_id", "publisher_run_attempt",
        ):
            if not isinstance(binding.get(key), str) or not RUN_ID.fullmatch(binding[key]):
                raise DecryptError("recovery chain binding is invalid")
    artifact_id = binding.get("artifact_id")
    artifact_name = binding.get("artifact_name")
    if (
        not isinstance(artifact_id, str) or not RUN_ID.fullmatch(artifact_id)
        or artifact_name != f"platform-release-runtime-failure-{binding['workflow_run_id']}-{binding['workflow_run_attempt']}"
        or not isinstance(binding.get("artifact_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", binding["artifact_digest"]) is None
    ):
        raise DecryptError("runtime failure artifact binding is invalid")


def _validate_runtime_marker(remote_stderr: bytes, route: str) -> None:
    if not 1 <= len(remote_stderr) <= MAX_RUNTIME_REMOTE_STDERR_BYTES:
        raise DecryptError("runtime failure stderr exceeded its bound")
    try:
        text = remote_stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DecryptError("runtime failure stderr encoding is invalid") from exc
    marker_lines = [line.removeprefix("RUNTIME_PRIVATE_CONTEXT ") for line in text.splitlines() if line.startswith("RUNTIME_PRIVATE_CONTEXT ")]
    if not 1 <= len(marker_lines) <= 5 or (route == "capture_probe" and len(marker_lines) != 1):
        raise DecryptError("runtime failure marker count is invalid")
    marker_keys = {
        "schema", "route", "component", "guard_id", "returncode", "stream", "output_b64",
        "total_bytes", "truncated", "retained",
    }
    expected_components = {
        "capture_probe": {
            "nginx_preflight": "stderr",
            "runtime_payload_verify": "stderr",
            "runtime_expected_tree": "stderr",
            "systemd_unit_snapshot": "stderr",
        },
        "recover_pending": {
            "systemd_state_validate": "combined", "runtime_reconcile": "combined",
            "systemd_units": "combined", "nginx_apply": "combined",
            "nginx_config_test": "combined", "nginx_reload": "combined",
            "systemd_restore_enabled": "combined", "runtime_guard": "guard",
        },
    }
    allowed_guards = {
        "argument", "privilege", "lock", "layout", "tooling", "systemd_state",
        "transaction_state", "legacy_liveqa_authority", "liveqa_runtime", "nginx", "runtime",
    }
    previous_order = -1
    seen: set[str] = set()
    order = [
        "systemd_state_validate", "runtime_reconcile", "systemd_units", "nginx_apply",
        "nginx_config_test", "nginx_reload", "systemd_restore_enabled",
    ]
    for marker_line in marker_lines:
        try:
            marker = json.loads(marker_line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, DecryptError) as exc:
            raise DecryptError("runtime failure marker is invalid") from exc
        if (
            not isinstance(marker, dict) or set(marker) != marker_keys
            or marker.get("schema") != 1 or marker.get("route") != route
            or not isinstance(marker.get("component"), str)
            or marker.get("component") not in expected_components.get(route, {})
            or marker.get("stream") != expected_components[route][marker["component"]]
            or marker.get("retained") != "tail"
            or type(marker.get("truncated")) is not bool
            or (marker.get("returncode") != "unavailable" and (
                type(marker["returncode"]) is not int or not 1 <= marker["returncode"] <= 255
            ))
        ):
            raise DecryptError("runtime failure marker fields are invalid")
        component = marker["component"]
        if component == "runtime_guard":
            if (
                route != "recover_pending" or not isinstance(marker.get("guard_id"), str)
                or marker.get("guard_id") not in allowed_guards
                or marker.get("returncode") == "unavailable" or marker.get("stream") != "guard"
                or marker.get("output_b64") != "" or marker.get("total_bytes") != 0
                or marker.get("truncated") is not False
            ):
                raise DecryptError("runtime guard marker is invalid")
            continue
        if marker.get("guard_id") is not None:
            raise DecryptError("runtime command marker contains a guard id")
        if component in seen:
            raise DecryptError("runtime failure marker component is duplicated")
        seen.add(component)
        if route == "recover_pending":
            component_order = order.index(component)
            if component_order <= previous_order:
                raise DecryptError("runtime failure marker order is invalid")
            previous_order = component_order
        encoded = marker.get("output_b64")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_RUNTIME_MARKER_OUTPUT_BYTES + 2) // 3):
            raise DecryptError("runtime failure marker output exceeded its bound")
        try:
            retained = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise DecryptError("runtime failure marker output is invalid") from exc
        if len(retained) > MAX_RUNTIME_MARKER_OUTPUT_BYTES or base64.b64encode(retained).decode("ascii") != encoded:
            raise DecryptError("runtime failure marker output is invalid")
        total = marker.get("total_bytes")
        if total == "unavailable":
            if marker["truncated"] is not True:
                raise DecryptError("runtime failure marker truncation is invalid")
        elif type(total) is not int or not len(retained) <= total <= 9223372036854775807 or marker["truncated"] != (total > len(retained)):
            raise DecryptError("runtime failure marker byte count is invalid")
    if len(marker_lines) > 1 and any(
        json.loads(line.removeprefix("RUNTIME_PRIVATE_CONTEXT ")).get("component") == "runtime_guard"
        for line in marker_lines
    ):
        raise DecryptError("runtime guard marker cannot accompany command context")


def decrypt_runtime_failure_context(
    *, artifact_archive: Path, binding_json: Path, private_key: Path,
    certificate: Path, destination: Path, git_dir: Path,
) -> tuple[str, int]:
    """Decrypt a source/API-bound recovery-runtime diagnostic, never reset evidence."""
    git_dir = git_dir.resolve(strict=True)
    binding_info = binding_json.lstat()
    if (
        not stat.S_ISREG(binding_info.st_mode) or stat.S_ISLNK(binding_info.st_mode)
        or binding_info.st_uid != os.geteuid() or binding_info.st_nlink != 1
        or stat.S_IMODE(binding_info.st_mode) != 0o600
        or not _inside(binding_json.resolve(strict=True), git_dir)
    ):
        raise DecryptError("runtime failure API binding must be a protected git-private file")
    binding = _parse_json(binding_json.read_bytes(), maximum=8192)
    _validate_runtime_binding(binding)
    archive_info = artifact_archive.lstat()
    if (
        not stat.S_ISREG(archive_info.st_mode) or stat.S_ISLNK(archive_info.st_mode)
        or archive_info.st_uid != os.geteuid() or archive_info.st_nlink != 1
        or stat.S_IMODE(archive_info.st_mode) != 0o600
        or not 1 <= archive_info.st_size <= 1024 * 1024
        or not _inside(artifact_archive.resolve(strict=True), git_dir)
    ):
        raise DecryptError("runtime failure archive must be a protected git-private file")
    archive_bytes = artifact_archive.read_bytes()
    if len(archive_bytes) != archive_info.st_size or hashlib.sha256(archive_bytes).hexdigest() != binding["artifact_digest"].removeprefix("sha256:"):
        raise DecryptError("runtime failure archive does not match API digest")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 2 or {entry.filename for entry in entries} != {"manifest.json", "evidence.cms"}:
                raise DecryptError("runtime failure archive is not closed")
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0o177777
                if entry.is_dir() or entry.flag_bits & 0x1 or entry.file_size > MAX_CIPHERTEXT_BYTES or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise DecryptError("runtime failure archive member is unsafe")
            manifest_raw = bundle.read("manifest.json")
            ciphertext = bundle.read("evidence.cms")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise DecryptError("runtime failure archive is invalid") from exc
    manifest = _parse_json(manifest_raw, maximum=8192)
    if not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise DecryptError("runtime failure ciphertext exceeded its bound")
    if (
        set(manifest) != RUNTIME_MANIFEST_KEYS
        or manifest.get("schema") != 1
        or manifest.get("kind") != "encrypted_release_runtime_failure_context"
        or any(manifest.get(key) != binding.get(key) for key in (
            "repository", "workflow_path", *RUNTIME_PROVENANCE_KEYS,
        ))
        or manifest.get("recipient_certificate_sha256") != RECIPIENT_CERTIFICATE_SHA256
        or manifest.get("encryption") != ENCRYPTION_PROFILE
        or manifest.get("ciphertext_sha256") != hashlib.sha256(ciphertext).hexdigest()
        or type(manifest.get("ciphertext_size")) is not int
        or manifest.get("ciphertext_size") != len(ciphertext)
    ):
        raise DecryptError("runtime failure manifest binding is invalid")
    key_info = private_key.lstat()
    cert_info = certificate.lstat()
    if (
        not stat.S_ISREG(key_info.st_mode) or stat.S_ISLNK(key_info.st_mode)
        or key_info.st_uid != os.geteuid() or key_info.st_nlink != 1
        or stat.S_IMODE(key_info.st_mode) != 0o600
        or not _inside(private_key.resolve(strict=True), git_dir)
        or not stat.S_ISREG(cert_info.st_mode) or stat.S_ISLNK(cert_info.st_mode)
        or cert_info.st_uid != os.geteuid() or cert_info.st_nlink != 1
        or not _inside(certificate.resolve(strict=True), git_dir)
    ):
        raise DecryptError("local runtime recipient paths are unsafe")
    cert_der = _run_openssl(["x509", "-in", str(certificate), "-outform", "DER"], maximum=8192)
    if hashlib.sha256(cert_der).hexdigest() != RECIPIENT_CERTIFICATE_SHA256:
        raise DecryptError("local runtime recipient certificate does not match source pin")
    parent = destination.parent
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700
        or not _inside(parent.resolve(strict=True), git_dir) or destination.exists() or destination.is_symlink()
    ):
        raise DecryptError("runtime failure destination must be new in a protected git-private directory")
    with tempfile.TemporaryDirectory(prefix=".runtime-failure-", dir=parent) as temporary:
        temp_dir = Path(temporary)
        os.chmod(temp_dir, 0o700)
        encrypted = temp_dir / "evidence.cms"
        plaintext = temp_dir / "evidence.json"
        encrypted.write_bytes(ciphertext)
        os.chmod(encrypted, 0o600)
        descriptor = os.open(plaintext, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        _run_openssl([
            "cms", "-decrypt", "-binary", "-inform", "DER", "-in", str(encrypted),
            "-out", str(plaintext), "-recip", str(certificate), "-inkey", str(private_key),
            "-keyopt", "rsa_padding_mode:oaep", "-keyopt", "rsa_oaep_md:sha256",
        ], maximum=0)
        plain_info = plaintext.lstat()
        if not stat.S_ISREG(plain_info.st_mode) or stat.S_ISLNK(plain_info.st_mode) or stat.S_IMODE(plain_info.st_mode) != 0o600 or not 1 <= plain_info.st_size <= MAX_PLAINTEXT_BYTES:
            raise DecryptError("decrypted runtime failure metadata is unsafe")
        raw = plaintext.read_bytes()
    try:
        private = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, DecryptError) as exc:
        raise DecryptError("decrypted runtime failure JSON is invalid") from exc
    if (
        not isinstance(private, dict) or set(private) != RUNTIME_PLAINTEXT_KEYS
        or private.get("schema") != 1 or private.get("kind") != "release_runtime_failure_private_context"
        or any(private.get(key) != manifest.get(key) for key in ("repository", "workflow_path", *RUNTIME_PROVENANCE_KEYS))
        or type(private.get("remote_stderr_size")) is not int
        or not 1 <= private["remote_stderr_size"] <= MAX_RUNTIME_REMOTE_STDERR_BYTES
        or not isinstance(private.get("remote_stderr_b64"), str)
        or len(private["remote_stderr_b64"]) > 4 * ((MAX_RUNTIME_REMOTE_STDERR_BYTES + 2) // 3)
    ):
        raise DecryptError("decrypted runtime failure binding is invalid")
    try:
        remote_stderr = base64.b64decode(private["remote_stderr_b64"], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise DecryptError("decrypted runtime failure stderr is invalid") from exc
    if len(remote_stderr) != private["remote_stderr_size"] or base64.b64encode(remote_stderr).decode("ascii") != private["remote_stderr_b64"]:
        raise DecryptError("decrypted runtime failure stderr size is invalid")
    _validate_runtime_marker(remote_stderr, binding["route"])
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-failure-context", action="store_true",
        help="decrypt a source/API-bound encrypted recovery runtime failure context",
    )
    parser.add_argument("--artifact-archive", type=Path, required=True)
    parser.add_argument("--binding-json", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--git-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        decrypt = decrypt_runtime_failure_context if args.runtime_failure_context else decrypt_capture
        digest, size = decrypt(
            artifact_archive=args.artifact_archive, binding_json=args.binding_json,
            private_key=args.private_key,
            certificate=args.certificate, destination=args.destination,
            git_dir=args.git_dir,
        )
    except (DecryptError, OSError, UnicodeError) as exc:
        print(f"MAINTENANCE_EVIDENCE status=decrypt_failed error_class={type(exc).__name__}")
        return 1
    print(f"MAINTENANCE_EVIDENCE status=decrypted sha256={digest} size={size} private=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
