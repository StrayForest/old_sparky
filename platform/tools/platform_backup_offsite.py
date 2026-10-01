#!/usr/bin/env python3
"""Encrypt a restore-verified platform backup and copy it to private R2.

The default mode is a local-only dry run.  Remote writes require ``--apply``.
The tool deliberately has no object deletion or retention implementation.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import datetime as dt
from enum import IntEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
# The only child process is the required local gpg binary, always with shell=False.
import subprocess  # nosec B404
import sys
import tempfile
from typing import Any, Callable
import urllib.parse

try:
    from .platform_backup_manifest import (
        BACKUP_NAME_RE,
        BackupManifestError,
        read_private_prefix,
        read_manifest_file,
        sha256_private_file,
    )
except ImportError:  # Direct execution from the tools directory.
    try:
        from tools.platform_backup_manifest import (
            BACKUP_NAME_RE,
            BackupManifestError,
            read_private_prefix,
            read_manifest_file,
            sha256_private_file,
        )
    except ImportError:
        from platform_backup_manifest import (  # type: ignore[no-redef]
            BACKUP_NAME_RE,
            BackupManifestError,
            read_private_prefix,
            read_manifest_file,
            sha256_private_file,
        )


DEFAULT_BACKUP_ENV = Path("/opt/oldsparky/platform/shared/.env.backup")
DEFAULT_PLATFORM_ENV = Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_BACKUP_DIR = Path("/opt/oldsparky/platform/shared/backups")
GPG_BINARY = "/usr/bin/gpg"
FINGERPRINT_RE = re.compile(r"^[A-F0-9]{40,64}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
R2_ENDPOINT_HOST_RE = re.compile(r"^[a-f0-9]{32}\.r2\.cloudflarestorage\.com$")
PUBLIC_CONFIGURATION_KEYS = (
    "PLATFORM_BACKUP_R2_PUBLIC_BASE_URL",
    "PLATFORM_BACKUP_R2_PUBLIC_URL",
    "PLATFORM_BACKUP_R2_CUSTOM_DOMAIN",
    "PLATFORM_BACKUP_R2_DEV_URL",
)
MAX_SINGLE_PUT_BYTES = 5 * 1024 * 1024 * 1024
MIN_TEMP_HEADROOM_BYTES = 64 * 1024 * 1024


class ExitCode(IntEnum):
    OK = 0
    UNEXPECTED = 1
    CONFIGURATION = 2
    SOURCE_BACKUP = 3
    ENCRYPTION = 4
    STORAGE = 5
    VERIFICATION = 6


class OffsiteBackupError(RuntimeError):
    def __init__(self, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class OffsiteConfig:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    region: str
    key_prefix: str
    public_key_file: Path
    recipient_fingerprint: str


@dataclass(frozen=True)
class VerifiedBackup:
    dump_path: Path
    metadata_path: Path
    timestamp: dt.datetime
    plaintext_sha256: str
    metadata_sha256: str
    size_bytes: int
    dump_identity: tuple[int, int, int, int, int] | None = None
    metadata_identity: tuple[int, int, int, int, int] | None = None


@dataclass(frozen=True)
class EncryptedBackup:
    path: Path
    sha256: str
    md5_hex: str
    md5_base64: str
    size_bytes: int
    # Supervisor-owned off-site transactions keep this descriptor open from
    # encryption through HeadObject/PutObject.  Legacy dry-run callers may
    # leave it unset and use the path-based primitive below.
    fd: int | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Encrypt the newest restore-verified platformdb dump with an offline-custodied "
            "OpenPGP public key and copy only ciphertext to a separate private R2 bucket. "
            "The default is a local-only dry run; --apply is required to contact or write R2."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Upload ciphertext and verify it.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_BACKUP_ENV)
    parser.add_argument("--platform-env-file", type=Path, default=DEFAULT_PLATFORM_ENV)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="Specific platformdb-*.dump in --backup-dir; defaults to the newest manifest.",
    )
    parser.add_argument("--max-age-hours", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> tuple[str, str]:
    # S3 Content-MD5 is required for transport integrity. This is not used as a
    # cryptographic authenticity primitive; OpenPGP and SHA-256 own that role.
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), base64.b64encode(digest.digest()).decode("ascii")


def _read_env(
    path: Path,
    *,
    apply: bool,
) -> dict[str, str]:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise OffsiteBackupError(
            f"Required private environment file is missing: {path}.",
            ExitCode.CONFIGURATION,
        ) from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise OffsiteBackupError(
            f"Private environment path must be a regular non-symlink file: {path}.",
            ExitCode.CONFIGURATION,
        )
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise OffsiteBackupError(
            f"Private environment file must have mode 0600: {path}.",
            ExitCode.CONFIGURATION,
        )
    required_owner = 0 if apply else os.geteuid()
    required_group = 0 if apply else os.getegid()
    if file_stat.st_uid != required_owner or file_stat.st_gid != required_group:
        expected = "root" if apply else "the invoking user"
        expected += ":root" if apply else " and group"
        raise OffsiteBackupError(
            f"Private environment file must be owned by {expected}: {path}.",
            ExitCode.CONFIGURATION,
        )

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OffsiteBackupError(
            f"Could not read private environment file: {path}.",
            ExitCode.CONFIGURATION,
        ) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key or normalized_key in values:
            raise OffsiteBackupError(
                f"Private environment file contains an empty or duplicate key: {path}.",
                ExitCode.CONFIGURATION,
            )
        values[normalized_key] = value.strip().strip("'\"")
    return values


def _required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise OffsiteBackupError(
            f"Required setting {name} is missing from the private backup environment.",
            ExitCode.CONFIGURATION,
        )
    return value


def _validate_endpoint(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_ENDPOINT_URL is not a valid URL.",
            ExitCode.CONFIGURATION,
        ) from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not R2_ENDPOINT_HOST_RE.fullmatch(hostname)
        or parsed.username
        or parsed.password
        or parsed_port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_ENDPOINT_URL must be an account-scoped HTTPS R2 S3 endpoint.",
            ExitCode.CONFIGURATION,
        )
    return value.rstrip("/")


def load_config(
    env_path: Path,
    platform_env_path: Path,
    *,
    apply: bool,
) -> OffsiteConfig:
    if apply and os.geteuid() != 0:
        raise OffsiteBackupError(
            "--apply must run as root so the root-only backup credentials remain protected.",
            ExitCode.CONFIGURATION,
        )
    values = _read_env(env_path, apply=apply)
    platform_values = _read_env(platform_env_path, apply=apply)

    visibility = _required(values, "PLATFORM_BACKUP_R2_BUCKET_VISIBILITY").lower()
    confirmed_private = _required(
        values, "PLATFORM_BACKUP_R2_PRIVATE_BUCKET_CONFIRMED"
    ).lower()
    if visibility != "private" or confirmed_private != "true":
        raise OffsiteBackupError(
            "The backup R2 bucket must be explicitly confirmed private-only.",
            ExitCode.CONFIGURATION,
        )
    configured_public_keys = [
        name for name in PUBLIC_CONFIGURATION_KEYS if values.get(name, "").strip()
    ]
    if configured_public_keys:
        raise OffsiteBackupError(
            "Public or custom-domain settings are forbidden for the backup bucket: "
            + ", ".join(configured_public_keys)
            + ".",
            ExitCode.CONFIGURATION,
        )

    bucket_name = _required(values, "PLATFORM_BACKUP_R2_BUCKET_NAME")
    if not BUCKET_RE.fullmatch(bucket_name):
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_BUCKET_NAME is not a valid private bucket name.",
            ExitCode.CONFIGURATION,
        )
    access_key_id = _required(values, "PLATFORM_BACKUP_R2_ACCESS_KEY_ID")
    secret_access_key = _required(values, "PLATFORM_BACKUP_R2_SECRET_ACCESS_KEY")
    media_bucket = _required(platform_values, "PLATFORM_R2_BUCKET_NAME")
    media_access_key = _required(platform_values, "PLATFORM_R2_ACCESS_KEY_ID")
    media_secret_key = _required(platform_values, "PLATFORM_R2_SECRET_ACCESS_KEY")
    if bucket_name == media_bucket:
        raise OffsiteBackupError(
            "The off-site backup bucket must be separate from the public media bucket.",
            ExitCode.CONFIGURATION,
        )
    if access_key_id == media_access_key or secret_access_key == media_secret_key:
        raise OffsiteBackupError(
            "The off-site backup credentials must be separate from media R2 credentials.",
            ExitCode.CONFIGURATION,
        )

    fingerprint = re.sub(
        r"\s+",
        "",
        _required(values, "PLATFORM_BACKUP_GPG_RECIPIENT_FINGERPRINT"),
    ).upper()
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_GPG_RECIPIENT_FINGERPRINT must be a full OpenPGP fingerprint.",
            ExitCode.CONFIGURATION,
        )
    key_prefix = values.get("PLATFORM_BACKUP_R2_KEY_PREFIX", "database").strip().strip("/")
    if not key_prefix or any(part in {"", ".", ".."} for part in key_prefix.split("/")):
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_KEY_PREFIX must be a non-empty relative object prefix.",
            ExitCode.CONFIGURATION,
        )
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", key_prefix):
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_KEY_PREFIX contains unsupported characters.",
            ExitCode.CONFIGURATION,
        )

    region = values.get("PLATFORM_BACKUP_R2_REGION", "auto").strip() or "auto"
    if region != "auto":
        raise OffsiteBackupError(
            "PLATFORM_BACKUP_R2_REGION must be auto for Cloudflare R2.",
            ExitCode.CONFIGURATION,
        )
    return OffsiteConfig(
        endpoint_url=_validate_endpoint(
            _required(values, "PLATFORM_BACKUP_R2_ENDPOINT_URL")
        ),
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        bucket_name=bucket_name,
        region=region,
        key_prefix=key_prefix,
        public_key_file=Path(
            _required(values, "PLATFORM_BACKUP_GPG_PUBLIC_KEY_FILE")
        ),
        recipient_fingerprint=fingerprint,
    )


def select_verified_backup(
    backup_dir: Path,
    dump_argument: Path | None,
    *,
    max_age_hours: float,
    apply: bool,
) -> VerifiedBackup:
    if max_age_hours <= 0 or max_age_hours > 168:
        raise OffsiteBackupError(
            "--max-age-hours must be greater than zero and at most 168.",
            ExitCode.CONFIGURATION,
        )
    try:
        backup_dir_stat = backup_dir.lstat()
    except OSError as exc:
        raise OffsiteBackupError(
            f"Backup directory is missing: {backup_dir}.", ExitCode.SOURCE_BACKUP
        ) from exc
    if stat.S_ISLNK(backup_dir_stat.st_mode) or not stat.S_ISDIR(backup_dir_stat.st_mode):
        raise OffsiteBackupError(
            f"Backup path must be a regular non-symlink directory: {backup_dir}.",
            ExitCode.SOURCE_BACKUP,
        )
    try:
        resolved_dir = backup_dir.resolve(strict=True)
    except OSError as exc:
        raise OffsiteBackupError(
            f"Backup directory is unavailable: {backup_dir}.", ExitCode.SOURCE_BACKUP
        ) from exc

    if dump_argument is None:
        try:
            manifests = sorted(
                resolved_dir.glob("platformdb-*.json"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
        except OSError as exc:
            raise OffsiteBackupError(
                f"Could not inspect platform backup manifests in {resolved_dir}.",
                ExitCode.SOURCE_BACKUP,
            ) from exc
        if not manifests:
            raise OffsiteBackupError(
                f"No platform backup manifests exist in {resolved_dir}.",
                ExitCode.SOURCE_BACKUP,
            )
        metadata_path = manifests[-1]
        dump_path = metadata_path.with_suffix(".dump")
    else:
        requested_path = dump_argument
        if not requested_path.is_absolute():
            requested_path = backup_dir / requested_path
        try:
            # Resolve only the parent for the containment check.  Keep the
            # original final component so lstat/O_NOFOLLOW can reject a
            # symlink instead of silently accepting its target.
            if requested_path.parent.resolve(strict=True) != resolved_dir:
                raise OffsiteBackupError(
                    "--dump must reference a direct child of --backup-dir.",
                    ExitCode.SOURCE_BACKUP,
                )
            dump_path = requested_path
            resolved_dump_path = requested_path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise OffsiteBackupError(
                f"Requested backup dump is missing: {dump_argument}.",
                ExitCode.SOURCE_BACKUP,
            ) from exc
        if resolved_dump_path.parent != resolved_dir or resolved_dump_path.name != requested_path.name:
            raise OffsiteBackupError(
                "--dump must reference a direct child of --backup-dir.",
                ExitCode.SOURCE_BACKUP,
            )
        metadata_path = dump_path.with_suffix(".json")

    match = BACKUP_NAME_RE.fullmatch(dump_path.name)
    if match is None:
        raise OffsiteBackupError(
            "Backup dump name must match platformdb-YYYYMMDDTHHMMSSZ-<run_id>.dump.",
            ExitCode.SOURCE_BACKUP,
        )
    expected_owner = 0 if apply else os.geteuid()
    expected_group = 0 if apply else os.getegid()
    try:
        manifest_file = read_manifest_file(
            metadata_path,
            expected_owner=expected_owner,
            expected_group=expected_group,
            expected_dump_file=dump_path.name,
        )
    except BackupManifestError as exc:
        raise OffsiteBackupError(
            f"Platform backup manifest is invalid: {metadata_path}: {exc}",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    manifest = manifest_file.manifest
    if (
        manifest.database != "platformdb"
        or manifest.schemas != ("platform", "public")
        or not manifest.restore_verified
        or not manifest.alembic_revision_verified
    ):
        raise OffsiteBackupError(
            "Only a format-v2, Alembic-checked, restore-verified platformdb backup may be uploaded.",
            ExitCode.SOURCE_BACKUP,
        )
    try:
        actual_sha256, dump_stat = sha256_private_file(
            dump_path,
            label="Platform backup dump",
            expected_owner=expected_owner,
            expected_group=expected_group,
        )
    except BackupManifestError as exc:
        raise OffsiteBackupError(
            f"Could not read the platform backup dump for checksum validation: {exc}",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    if actual_sha256 != manifest.sha256:
        raise OffsiteBackupError(
            "Platform backup checksum does not match its restore-verified manifest.",
            ExitCode.SOURCE_BACKUP,
        )
    if dump_stat.st_size <= 0 or manifest.size_bytes != dump_stat.st_size:
        raise OffsiteBackupError(
            "Platform backup size does not match its restore-verified manifest.",
            ExitCode.SOURCE_BACKUP,
        )
    try:
        dump_prefix, _ = read_private_prefix(
            dump_path,
            label="Platform backup dump",
            expected_owner=expected_owner,
            expected_group=expected_group,
            prefix_bytes=5,
        )
    except BackupManifestError as exc:
        raise OffsiteBackupError(
            f"Could not validate the platform backup archive format: {exc}",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    if dump_prefix != b"PGDMP":
        raise OffsiteBackupError(
            "Platform backup is not a PostgreSQL custom-format archive.",
            ExitCode.SOURCE_BACKUP,
        )
    completed_at = manifest.completed_at_utc
    try:
        backup_timestamp = dt.datetime.strptime(
            match.group("timestamp"), "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=dt.UTC)
    except ValueError as exc:
        raise OffsiteBackupError(
            "Platform backup filename timestamp is invalid.",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    if abs((completed_at - backup_timestamp).total_seconds()) > 3600:
        raise OffsiteBackupError(
            "Platform backup filename and manifest timestamps are inconsistent.",
            ExitCode.SOURCE_BACKUP,
        )
    age_hours = (dt.datetime.now(dt.UTC) - completed_at).total_seconds() / 3600
    if age_hours < -(5 / 60) or age_hours > max_age_hours:
        raise OffsiteBackupError(
            f"Restore-verified backup age is outside the allowed {max_age_hours:g}-hour window.",
            ExitCode.SOURCE_BACKUP,
        )

    metadata_sha256 = hashlib.sha256(manifest_file.raw_bytes).hexdigest()
    try:
        metadata_stat = metadata_path.lstat()
    except OSError as exc:
        raise OffsiteBackupError(
            "Backup manifest identity was lost during selection.", ExitCode.SOURCE_BACKUP
        ) from exc
    if (
        stat.S_ISLNK(metadata_stat.st_mode)
        or not stat.S_ISREG(metadata_stat.st_mode)
        or metadata_stat.st_nlink != 1
        or metadata_stat.st_uid != expected_owner
        or metadata_stat.st_gid != expected_group
        or stat.S_IMODE(metadata_stat.st_mode) != 0o600
    ):
        raise OffsiteBackupError(
            "Backup manifest metadata changed during selection.", ExitCode.SOURCE_BACKUP
        )
    return VerifiedBackup(
        dump_path=dump_path,
        metadata_path=metadata_path,
        timestamp=backup_timestamp,
        plaintext_sha256=actual_sha256,
        metadata_sha256=metadata_sha256,
        size_bytes=dump_stat.st_size,
        dump_identity=(
            dump_stat.st_dev,
            dump_stat.st_ino,
            dump_stat.st_nlink,
            dump_stat.st_size,
            dump_stat.st_mtime_ns,
        ),
        metadata_identity=(
            metadata_stat.st_dev,
            metadata_stat.st_ino,
            metadata_stat.st_nlink,
            metadata_stat.st_size,
            metadata_stat.st_mtime_ns,
        ),
    )


def _run_gpg(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        # Every argv is built by this module; no shell parsing is involved.
        return subprocess.run(  # nosec B603
            command,
            check=check,
            capture_output=True,
            text=True,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OffsiteBackupError(
            "OpenPGP public-key validation or encryption failed.",
            ExitCode.ENCRYPTION,
        ) from exc


def _primary_fingerprints(colon_output: str) -> list[str]:
    fingerprints: list[str] = []
    awaiting_primary = False
    for line in colon_output.splitlines():
        fields = line.split(":")
        record_type = fields[0] if fields else ""
        if record_type == "pub":
            awaiting_primary = True
        elif record_type == "sub":
            awaiting_primary = False
        elif record_type == "fpr" and awaiting_primary and len(fields) > 9:
            fingerprints.append(fields[9].upper())
            awaiting_primary = False
    return fingerprints


def validate_public_key(config: OffsiteConfig, gpg_home: Path, *, apply: bool) -> None:
    key_path = config.public_key_file
    try:
        key_stat = key_path.lstat()
    except OSError as exc:
        raise OffsiteBackupError(
            f"OpenPGP public key file is missing: {key_path}.",
            ExitCode.ENCRYPTION,
        ) from exc
    if stat.S_ISLNK(key_stat.st_mode) or not stat.S_ISREG(key_stat.st_mode):
        raise OffsiteBackupError(
            "OpenPGP public key must be a regular non-symlink file.",
            ExitCode.ENCRYPTION,
        )
    if key_stat.st_mode & 0o022:
        raise OffsiteBackupError(
            "OpenPGP public key file must not be group- or world-writable.",
            ExitCode.ENCRYPTION,
        )
    expected_owner = 0 if apply else os.geteuid()
    if key_stat.st_uid != expected_owner:
        raise OffsiteBackupError(
            "OpenPGP public key file has an unexpected owner.",
            ExitCode.ENCRYPTION,
        )

    try:
        gpg_home.mkdir(mode=0o700)
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not create the ephemeral OpenPGP keyring.",
            ExitCode.ENCRYPTION,
        ) from exc
    common = [
        GPG_BINARY,
        "--no-options",
        "--batch",
        "--no-tty",
        "--homedir",
        str(gpg_home),
    ]
    inspection = _run_gpg(
        [
            *common,
            "--with-colons",
            "--fingerprint",
            "--import-options",
            "show-only",
            "--dry-run",
            "--import",
            str(key_path),
        ]
    )
    if any(line.startswith(("sec:", "ssb:")) for line in inspection.stdout.splitlines()):
        raise OffsiteBackupError(
            "Recovery private-key material is forbidden on the production host.",
            ExitCode.ENCRYPTION,
        )
    if _primary_fingerprints(inspection.stdout) != [config.recipient_fingerprint]:
        raise OffsiteBackupError(
            "OpenPGP public-key file must contain exactly the configured primary fingerprint.",
            ExitCode.ENCRYPTION,
        )
    _run_gpg([*common, "--import-options", "import-clean", "--import", str(key_path)])
    public_result = _run_gpg([*common, "--with-colons", "--fingerprint", "--list-keys"])
    fingerprints = _primary_fingerprints(public_result.stdout)
    if fingerprints != [config.recipient_fingerprint]:
        raise OffsiteBackupError(
            "OpenPGP public-key file must contain exactly the configured primary fingerprint.",
            ExitCode.ENCRYPTION,
        )


def encrypt_backup(
    config: OffsiteConfig,
    backup: VerifiedBackup,
    work_dir: Path,
    *,
    apply: bool,
) -> EncryptedBackup:
    gpg_home = work_dir / "gnupg"
    validate_public_key(config, gpg_home, apply=apply)
    encrypted_path = work_dir / f"{backup.dump_path.name}.gpg"
    try:
        free_bytes = shutil.disk_usage(work_dir).free
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not validate temporary disk capacity for encryption.",
            ExitCode.ENCRYPTION,
        ) from exc
    if free_bytes < backup.size_bytes + MIN_TEMP_HEADROOM_BYTES:
        raise OffsiteBackupError(
            "Insufficient temporary disk capacity for encrypted backup output.",
            ExitCode.ENCRYPTION,
        )
    try:
        before = backup.dump_path.stat()
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not inspect the source backup before encryption.",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    _run_gpg(
        [
            GPG_BINARY,
            "--no-options",
            "--batch",
            "--yes",
            "--no-tty",
            "--homedir",
            str(gpg_home),
            "--trust-model",
            "always",
            "--cipher-algo",
            "AES256",
            "--compress-algo",
            "none",
            "--recipient",
            config.recipient_fingerprint,
            "--output",
            str(encrypted_path),
            "--encrypt",
            str(backup.dump_path),
        ]
    )
    try:
        encrypted_path.chmod(0o600)
        encrypted_stat = encrypted_path.stat()
        after = backup.dump_path.stat()
    except OSError as exc:
        raise OffsiteBackupError(
            "OpenPGP encryption did not produce a protected ciphertext file.",
            ExitCode.ENCRYPTION,
        ) from exc
    if encrypted_stat.st_size <= 0 or stat.S_IMODE(encrypted_stat.st_mode) != 0o600:
        raise OffsiteBackupError(
            "OpenPGP encryption did not produce a protected ciphertext file.",
            ExitCode.ENCRYPTION,
        )
    if encrypted_stat.st_size > MAX_SINGLE_PUT_BYTES:
        raise OffsiteBackupError(
            "Encrypted backup exceeds the R2 5 GiB single-PUT limit; an approved multipart "
            "backup change is required.",
            ExitCode.STORAGE,
        )
    try:
        with encrypted_path.open("rb") as encrypted_handle:
            encrypted_magic = encrypted_handle.read(5)
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not validate the generated OpenPGP ciphertext.",
            ExitCode.ENCRYPTION,
        ) from exc
    if encrypted_magic == b"PGDMP":
        raise OffsiteBackupError(
            "Encryption output unexpectedly contains the plaintext backup archive.",
            ExitCode.ENCRYPTION,
        )
    packet_result = _run_gpg(
        [
            GPG_BINARY,
            "--no-options",
            "--batch",
            "--no-tty",
            "--homedir",
            str(gpg_home),
            "--list-packets",
            str(encrypted_path),
        ],
        check=False,
    )
    has_public_key_packet = ":pubkey enc packet:" in packet_result.stdout
    has_encrypted_data_packet = any(
        marker in packet_result.stdout
        for marker in (":encrypted data packet:", ":aead encrypted packet:")
    )
    if (
        packet_result.returncode not in (0, 2)
        or not has_public_key_packet
        or not has_encrypted_data_packet
    ):
        raise OffsiteBackupError(
            "Encryption output is not a public-key OpenPGP ciphertext.",
            ExitCode.ENCRYPTION,
        )
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OffsiteBackupError(
            "Source backup changed while it was being encrypted.", ExitCode.SOURCE_BACKUP
        )
    try:
        source_sha256_after = sha256_file(backup.dump_path)
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not revalidate the source backup after encryption.",
            ExitCode.SOURCE_BACKUP,
        ) from exc
    if source_sha256_after != backup.plaintext_sha256:
        raise OffsiteBackupError(
            "Source backup checksum changed while it was being encrypted.",
            ExitCode.SOURCE_BACKUP,
        )
    cipher_sha256 = sha256_file(encrypted_path)
    cipher_md5, cipher_md5_base64 = md5_file(encrypted_path)
    return EncryptedBackup(
        path=encrypted_path,
        sha256=cipher_sha256,
        md5_hex=cipher_md5,
        md5_base64=cipher_md5_base64,
        size_bytes=encrypted_stat.st_size,
    )


def encrypt_backup_from_fd(
    config: OffsiteConfig,
    backup: VerifiedBackup,
    work_dir: Path,
    *,
    source_fd: int,
    validate_source: Callable[[], None],
    apply: bool,
) -> EncryptedBackup:
    """Encrypt bytes from a held dump descriptor and return a held ciphertext fd.

    The supervisor owns this path.  Neither GPG nor the upload consumer is
    allowed to reopen the selected dump or ciphertext by pathname.
    """

    gpg_home = work_dir / "gnupg"
    validate_public_key(config, gpg_home, apply=apply)
    try:
        free_bytes = shutil.disk_usage(work_dir).free
    except OSError as exc:
        raise OffsiteBackupError(
            "Could not validate temporary disk capacity for encryption.",
            ExitCode.ENCRYPTION,
        ) from exc
    if free_bytes < backup.size_bytes + MIN_TEMP_HEADROOM_BYTES:
        raise OffsiteBackupError(
            "Insufficient temporary disk capacity for encrypted backup output.",
            ExitCode.ENCRYPTION,
        )
    encrypted_path = work_dir / f"{backup.dump_path.name}.gpg"
    output_fd: int | None = None
    source_dup: int | None = None
    try:
        validate_source()
        output_fd = os.open(
            encrypted_path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        source_dup = os.dup(source_fd)
        os.set_inheritable(source_dup, False)
        command = [
            GPG_BINARY,
            "--no-options",
            "--batch",
            "--yes",
            "--no-tty",
            "--homedir",
            str(gpg_home),
            "--trust-model",
            "always",
            "--cipher-algo",
            "AES256",
            "--compress-algo",
            "none",
            "--recipient",
            config.recipient_fingerprint,
            "--encrypt",
            "-",
        ]
        try:
            os.lseek(source_dup, 0, os.SEEK_SET)
            subprocess.run(  # nosec B603 - fixed argv, shell=False
                command,
                check=True,
                stdin=source_dup,
                stdout=output_fd,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
                pass_fds=(source_dup,),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise OffsiteBackupError(
                "OpenPGP public-key encryption failed.", ExitCode.ENCRYPTION
            ) from exc
        finally:
            os.close(source_dup)
            source_dup = None
        validate_source()
        os.fsync(output_fd)
        output_stat = os.fstat(output_fd)
        if (
            not stat.S_ISREG(output_stat.st_mode)
            or output_stat.st_nlink != 1
            or output_stat.st_uid != os.geteuid()
            or output_stat.st_gid != os.getegid()
            or stat.S_IMODE(output_stat.st_mode) != 0o600
            or output_stat.st_size <= 0
            or output_stat.st_size > MAX_SINGLE_PUT_BYTES
        ):
            raise OffsiteBackupError(
                "OpenPGP encryption did not produce a protected ciphertext file.",
                ExitCode.ENCRYPTION,
            )
        output_path_stat = encrypted_path.lstat()
        if (
            not stat.S_ISREG(output_path_stat.st_mode)
            or output_path_stat.st_nlink != 1
            or (output_path_stat.st_dev, output_path_stat.st_ino)
            != (output_stat.st_dev, output_stat.st_ino)
        ):
            raise OffsiteBackupError(
                "OpenPGP ciphertext pathname was replaced.", ExitCode.ENCRYPTION
            )
        try:
            os.lseek(output_fd, 0, os.SEEK_SET)
            cipher_sha = hashlib.sha256()
            cipher_md5 = hashlib.md5(usedforsecurity=False)
            while True:
                chunk = os.read(output_fd, 1024 * 1024)
                if not chunk:
                    break
                cipher_sha.update(chunk)
                cipher_md5.update(chunk)
        except OSError as exc:
            raise OffsiteBackupError(
                "Could not hash the generated OpenPGP ciphertext.", ExitCode.ENCRYPTION
            ) from exc
        after_stat = os.fstat(output_fd)
        if (
            after_stat.st_size != output_stat.st_size
            or after_stat.st_mtime_ns != output_stat.st_mtime_ns
            or (after_stat.st_dev, after_stat.st_ino) != (output_stat.st_dev, output_stat.st_ino)
        ):
            raise OffsiteBackupError(
                "OpenPGP ciphertext changed while it was hashed.", ExitCode.ENCRYPTION
            )
        if os.pread(output_fd, 5, 0) == b"PGDMP":
            raise OffsiteBackupError(
                "Encryption output unexpectedly contains the plaintext backup archive.",
                ExitCode.ENCRYPTION,
            )
        packet_result = _run_gpg(
            [
                GPG_BINARY,
                "--no-options",
                "--batch",
                "--no-tty",
                "--homedir",
                str(gpg_home),
                "--list-packets",
                f"/proc/self/fd/{output_fd}",
            ],
            check=False,
        )
        has_public_key_packet = ":pubkey enc packet:" in packet_result.stdout
        has_encrypted_data_packet = any(
            marker in packet_result.stdout
            for marker in (":encrypted data packet:", ":aead encrypted packet:")
        )
        if (
            packet_result.returncode not in (0, 2)
            or not has_public_key_packet
            or not has_encrypted_data_packet
        ):
            raise OffsiteBackupError(
                "Encryption output is not a public-key OpenPGP ciphertext.",
                ExitCode.ENCRYPTION,
            )
        return EncryptedBackup(
            path=encrypted_path,
            sha256=cipher_sha.hexdigest(),
            md5_hex=cipher_md5.hexdigest(),
            md5_base64=base64.b64encode(cipher_md5.digest()).decode("ascii"),
            size_bytes=after_stat.st_size,
            fd=output_fd,
        )
    except Exception:
        if output_fd is not None:
            try:
                os.close(output_fd)
            except OSError:
                pass
            output_fd = None
        try:
            encrypted_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    finally:
        if source_dup is not None:
            try:
                os.close(source_dup)
            except OSError:
                pass


def object_key(config: OffsiteConfig, backup: VerifiedBackup) -> str:
    return (
        f"{config.key_prefix}/{backup.timestamp:%Y/%m}/"
        f"{backup.dump_path.name}.gpg"
    )


def validate_timeout(timeout: float) -> None:
    if timeout <= 0 or timeout > 60:
        raise OffsiteBackupError(
            "--timeout must be greater than zero and at most 60 seconds.",
            ExitCode.CONFIGURATION,
        )


def build_storage_client(config: OffsiteConfig, *, timeout: float) -> Any:
    validate_timeout(timeout)
    try:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
            config=Config(
                connect_timeout=min(timeout, 10),
                read_timeout=timeout,
                retries={"mode": "standard", "total_max_attempts": 3},
                signature_version="s3v4",
            ),
        )
    except Exception as exc:
        raise OffsiteBackupError(
            "Could not initialize the private R2 client.", ExitCode.STORAGE
        ) from exc


def _storage_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "unknown"
    error = response.get("Error")
    if not isinstance(error, dict):
        return "unknown"
    return str(error.get("Code") or "unknown")


def _head_object(client: Any, *, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _storage_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise OffsiteBackupError(
            f"R2 HeadObject failed (code={_storage_error_code(exc)}).",
            ExitCode.STORAGE,
        ) from exc


def _expected_metadata(
    config: OffsiteConfig,
    backup: VerifiedBackup,
    encrypted: EncryptedBackup,
) -> dict[str, str]:
    return {
        "cipher-sha256": encrypted.sha256,
        "cipher-md5": encrypted.md5_hex,
        "plaintext-sha256": backup.plaintext_sha256,
        "manifest-sha256": backup.metadata_sha256,
        "source-size": str(backup.size_bytes),
        "encryption": "openpgp-aes256",
        "recipient-fingerprint": config.recipient_fingerprint,
    }


def verify_remote_head(
    head: dict[str, Any],
    *,
    config: OffsiteConfig,
    backup: VerifiedBackup,
    encrypted: EncryptedBackup | None,
) -> dict[str, str]:
    metadata = {
        str(key).lower(): str(value)
        for key, value in (head.get("Metadata") or {}).items()
    }
    expected_common = {
        "plaintext-sha256": backup.plaintext_sha256,
        "manifest-sha256": backup.metadata_sha256,
        "source-size": str(backup.size_bytes),
        "encryption": "openpgp-aes256",
        "recipient-fingerprint": config.recipient_fingerprint,
    }
    if any(metadata.get(name) != value for name, value in expected_common.items()):
        raise OffsiteBackupError(
            "Remote ciphertext metadata does not match the restore-verified source backup.",
            ExitCode.VERIFICATION,
        )
    cipher_sha256 = metadata.get("cipher-sha256", "")
    if not SHA256_RE.fullmatch(cipher_sha256):
        raise OffsiteBackupError(
            "Remote ciphertext metadata has no valid SHA-256 checksum.",
            ExitCode.VERIFICATION,
        )
    cipher_md5 = metadata.get("cipher-md5", "")
    if not re.fullmatch(r"[a-f0-9]{32}", cipher_md5):
        raise OffsiteBackupError(
            "Remote ciphertext metadata has no valid transport checksum.",
            ExitCode.VERIFICATION,
        )
    try:
        content_length = int(head.get("ContentLength") or 0)
    except (TypeError, ValueError) as exc:
        raise OffsiteBackupError(
            "Remote ciphertext has no valid size.", ExitCode.VERIFICATION
        ) from exc
    if content_length <= 0 or head.get("ContentType") != "application/pgp-encrypted":
        raise OffsiteBackupError(
            "Remote object is not a non-empty OpenPGP ciphertext.",
            ExitCode.VERIFICATION,
        )
    etag = str(head.get("ETag") or "").strip('"').lower()
    if not etag or etag != cipher_md5:
        raise OffsiteBackupError(
            "Remote ciphertext transport checksum does not match its metadata.",
            ExitCode.VERIFICATION,
        )
    if encrypted is not None:
        if (
            content_length != encrypted.size_bytes
            or cipher_sha256 != encrypted.sha256
            or etag != encrypted.md5_hex
        ):
            raise OffsiteBackupError(
                "Remote ciphertext size or checksum differs from the local encrypted file.",
                ExitCode.VERIFICATION,
            )
    return {"cipher_sha256": cipher_sha256, "etag": etag}


def upload_and_verify(
    client: Any,
    *,
    config: OffsiteConfig,
    backup: VerifiedBackup,
    encrypted: EncryptedBackup,
    key: str,
    encrypted_fd: int | None = None,
) -> tuple[bool, dict[str, str]]:
    existing = _head_object(client, bucket=config.bucket_name, key=key)
    if existing is not None:
        return False, verify_remote_head(
            existing, config=config, backup=backup, encrypted=None
        )
    metadata = _expected_metadata(config, backup, encrypted)
    try:
        body_handle = None
        if encrypted_fd is not None:
            try:
                duplicate = os.dup(encrypted_fd)
                os.lseek(duplicate, 0, os.SEEK_SET)
                body_handle = os.fdopen(duplicate, "rb", closefd=True)
            except OSError as exc:
                raise OffsiteBackupError(
                    "Could not open the held ciphertext descriptor.", ExitCode.STORAGE
                ) from exc
        else:
            body_handle = encrypted.path.open("rb")
        with body_handle as ciphertext:
            response = client.put_object(
                Bucket=config.bucket_name,
                Key=key,
                Body=ciphertext,
                ContentLength=encrypted.size_bytes,
                ContentType="application/pgp-encrypted",
                ContentMD5=encrypted.md5_base64,
                CacheControl="no-store",
                Metadata=metadata,
                IfNoneMatch="*",
            )
    except Exception as exc:
        if _storage_error_code(exc) in {
            "409",
            "412",
            "ConditionalRequestConflict",
            "PreconditionFailed",
        }:
            raced_head = _head_object(client, bucket=config.bucket_name, key=key)
            if raced_head is not None:
                return False, verify_remote_head(
                    raced_head, config=config, backup=backup, encrypted=None
                )
        raise OffsiteBackupError(
            f"R2 PutObject failed (code={_storage_error_code(exc)}).",
            ExitCode.STORAGE,
        ) from exc
    response_etag = str(response.get("ETag") or "").strip('"').lower()
    if response_etag and response_etag != encrypted.md5_hex:
        raise OffsiteBackupError(
            "R2 PutObject returned an unexpected transport checksum.",
            ExitCode.VERIFICATION,
        )
    uploaded_head = _head_object(client, bucket=config.bucket_name, key=key)
    if uploaded_head is None:
        raise OffsiteBackupError(
            "Uploaded ciphertext was not visible to HeadObject.",
            ExitCode.VERIFICATION,
        )
    return True, verify_remote_head(
        uploaded_head, config=config, backup=backup, encrypted=encrypted
    )


def execute(
    args: argparse.Namespace, *, capability: object | None = None
) -> dict[str, Any]:
    if args.apply:
        if capability is None:
            raise OffsiteBackupError(
                "off-site mutation requires the in-process backup supervisor",
                ExitCode.CONFIGURATION,
            )
        try:
            from . import platform_backup_supervisor as supervisor
        except ImportError:
            import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
        supervisor.require_mutation_capability(capability, "offsite")
    validate_timeout(args.timeout)
    config = load_config(args.env_file, args.platform_env_file, apply=args.apply)
    backup = select_verified_backup(
        args.backup_dir,
        args.dump,
        max_age_hours=args.max_age_hours,
        apply=args.apply,
    )
    key = object_key(config, backup)
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="oldsparky-offsite-") as temporary_dir:
            work_dir = Path(temporary_dir)
            work_dir.chmod(0o700)
            encrypted = encrypt_backup(
                config, backup, work_dir, apply=args.apply
            )
            result: dict[str, Any] = {
                "ok": True,
                "mode": "apply" if args.apply else "dry-run",
                "source_dump": backup.dump_path.name,
                "source_sha256": backup.plaintext_sha256,
                "cipher_sha256": encrypted.sha256,
                "cipher_size_bytes": encrypted.size_bytes,
                "bucket": config.bucket_name,
                "object_key": key,
                "uploaded": False,
                "verified": False,
                "remote_operations": 0,
                "retention_actions": 0,
            }
            if not args.apply:
                return result
            client = build_storage_client(config, timeout=args.timeout)
            try:
                client.head_bucket(Bucket=config.bucket_name)
            except Exception as exc:
                raise OffsiteBackupError(
                    f"Private R2 bucket access check failed (code={_storage_error_code(exc)}).",
                    ExitCode.STORAGE,
                ) from exc
            uploaded, remote = upload_and_verify(
                client,
                config=config,
                backup=backup,
                encrypted=encrypted,
                key=key,
            )
            result.update(
                {
                    "cipher_sha256": remote["cipher_sha256"],
                    "uploaded": uploaded,
                    "verified": True,
                    "remote_operations": 4 if uploaded else 2,
                }
            )
            return result
    finally:
        os.umask(previous_umask)


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return
    if result["mode"] == "dry-run":
        print("[OK] Off-site backup dry run passed; R2 was not contacted.")
    else:
        action = "uploaded" if result["uploaded"] else "already present"
        print(f"[OK] Encrypted off-site backup verified ({action}).")
    print(f"[OK] Source: {result['source_dump']}")
    print(f"[OK] Object: {result['bucket']}/{result['object_key']}")
    print("[OK] Retention actions: 0")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.apply:
            # Production off-site mutation belongs to the lock/evidence
            # supervisor.  ``execute`` remains a library primitive for
            # hermetic tests and local dry-run callers; no ambient environment
            # variable or inherited FD can grant this path authority.
            try:
                from . import platform_backup_supervisor as supervisor
            except ImportError:
                import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
            result = supervisor.run_offsite_entrypoint(
                args,
                app_dir=Path("/opt/oldsparky/platform"),
            )
        else:
            result = execute(args)
        _print_result(result, as_json=args.as_json)
        return int(ExitCode.OK)
    except OffsiteBackupError as exc:
        payload = {"ok": False, "exit_code": int(exc.exit_code), "error": str(exc)}
        if args.as_json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"[FAIL] {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except Exception:
        payload = {
            "ok": False,
            "exit_code": int(ExitCode.UNEXPECTED),
            "error": "Unexpected off-site backup failure; inspect protected service logs.",
        }
        if args.as_json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"[FAIL] {payload['error']}", file=sys.stderr)
        return int(ExitCode.UNEXPECTED)


if __name__ == "__main__":
    raise SystemExit(main())
