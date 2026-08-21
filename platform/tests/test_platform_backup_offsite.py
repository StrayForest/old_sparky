from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_offsite.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_offsite", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
offsite = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = offsite
SPEC.loader.exec_module(offsite)


FINGERPRINT = "A" * 40
R2_ENDPOINT = f"https://{'a' * 32}.r2.cloudflarestorage.com"


def _write_private(path: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _backup_env(public_key: Path, **overrides: str) -> str:
    values = {
        "PLATFORM_BACKUP_R2_ENDPOINT_URL": R2_ENDPOINT,
        "PLATFORM_BACKUP_R2_ACCESS_KEY_ID": "backup-access-key",
        "PLATFORM_BACKUP_R2_SECRET_ACCESS_KEY": "backup-secret-key",
        "PLATFORM_BACKUP_R2_BUCKET_NAME": "oldsparky-backups",
        "PLATFORM_BACKUP_R2_BUCKET_VISIBILITY": "private",
        "PLATFORM_BACKUP_R2_PRIVATE_BUCKET_CONFIRMED": "true",
        "PLATFORM_BACKUP_R2_KEY_PREFIX": "database",
        "PLATFORM_BACKUP_GPG_PUBLIC_KEY_FILE": str(public_key),
        "PLATFORM_BACKUP_GPG_RECIPIENT_FINGERPRINT": FINGERPRINT,
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def _platform_env(**overrides: str) -> str:
    values = {
        "PLATFORM_R2_ACCESS_KEY_ID": "media-access-key",
        "PLATFORM_R2_SECRET_ACCESS_KEY": "media-secret-key",
        "PLATFORM_R2_BUCKET_NAME": "oldsparky-media",
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def _create_verified_backup(directory: Path) -> tuple[Path, Path]:
    now = dt.datetime.now(dt.UTC)
    dump = directory / f"platformdb-{now:%Y%m%dT%H%M%SZ}.dump"
    _write_private(dump, b"PGDMP restore-verified payload")
    manifest = dump.with_suffix(".json")
    metadata = {
        "format_version": 2,
        "database": "platformdb",
        "schema": "platform",
        "dump_file": dump.name,
        "size_bytes": dump.stat().st_size,
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "completed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "restore_verified": True,
        "alembic_revision_verified": True,
    }
    _write_private(manifest, json.dumps(metadata))
    return dump, manifest


def _config(public_key: Path) -> offsite.OffsiteConfig:
    return offsite.OffsiteConfig(
        endpoint_url=R2_ENDPOINT,
        access_key_id="backup-access-key",
        secret_access_key="backup-secret-key",
        bucket_name="oldsparky-backups",
        region="auto",
        key_prefix="database",
        public_key_file=public_key,
        recipient_fingerprint=FINGERPRINT,
    )


class StorageError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("redacted storage error")
        self.response = {"Error": {"Code": code}}


class RecordingStorageClient:
    def __init__(
        self,
        *,
        config: offsite.OffsiteConfig,
        backup: offsite.VerifiedBackup,
        encrypted: offsite.EncryptedBackup,
    ) -> None:
        self.config = config
        self.backup = backup
        self.encrypted = encrypted
        self.put_calls: list[dict[str, object]] = []
        self.stored_head: dict[str, object] | None = None

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        if self.stored_head is None:
            raise StorageError("404")
        return self.stored_head

    def put_object(self, **kwargs: object) -> dict[str, str]:
        body = kwargs["Body"]
        assert hasattr(body, "read")
        payload = body.read()
        self.put_calls.append({**kwargs, "Body": payload})
        self.stored_head = {
            "Metadata": kwargs["Metadata"],
            "ContentLength": len(payload),
            "ContentType": kwargs["ContentType"],
            "ETag": f'"{self.encrypted.md5_hex}"',
        }
        return {"ETag": f'"{self.encrypted.md5_hex}"'}


class PlatformBackupOffsiteTests(unittest.TestCase):
    def test_load_config_requires_private_separate_r2_contour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            public_key = root / "recovery.asc"
            public_key.write_text("public key", encoding="utf-8")
            env_path = root / ".env.backup"
            platform_env_path = root / ".env.platform"
            _write_private(env_path, _backup_env(public_key))
            _write_private(platform_env_path, _platform_env())
            platform_env_path.chmod(0o640)

            config = offsite.load_config(env_path, platform_env_path, apply=False)

            self.assertEqual(config.bucket_name, "oldsparky-backups")
            self.assertEqual(config.recipient_fingerprint, FINGERPRINT)

    def test_load_config_rejects_public_domain_and_media_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            public_key = root / "recovery.asc"
            public_key.write_text("public key", encoding="utf-8")
            env_path = root / ".env.backup"
            platform_env_path = root / ".env.platform"
            _write_private(
                env_path,
                _backup_env(
                    public_key,
                    PLATFORM_BACKUP_R2_CUSTOM_DOMAIN="backups.example.test",
                    PLATFORM_BACKUP_R2_ACCESS_KEY_ID="shared-access-key",  # secret-scan: allow-test-fixture
                ),
            )
            _write_private(
                platform_env_path,
                _platform_env(PLATFORM_R2_ACCESS_KEY_ID="shared-access-key"),
            )

            with self.assertRaisesRegex(offsite.OffsiteBackupError, "forbidden"):
                offsite.load_config(env_path, platform_env_path, apply=False)

            _write_private(
                env_path,
                _backup_env(
                    public_key,
                    PLATFORM_BACKUP_R2_ACCESS_KEY_ID="shared-access-key",  # secret-scan: allow-test-fixture
                ),
            )
            with self.assertRaisesRegex(offsite.OffsiteBackupError, "credentials"):
                offsite.load_config(env_path, platform_env_path, apply=False)

    def test_private_environment_must_be_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            env_path = Path(temporary_dir) / ".env.backup"
            env_path.write_text("KEY=value\n", encoding="utf-8")
            env_path.chmod(0o640)

            with self.assertRaisesRegex(offsite.OffsiteBackupError, "0600"):
                offsite._read_env(env_path, apply=False)

    def test_select_verified_backup_validates_manifest_checksum_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dump, _manifest = _create_verified_backup(root)

            selected = offsite.select_verified_backup(
                root, None, max_age_hours=24, apply=False
            )

            self.assertEqual(selected.dump_path, dump)
            self.assertEqual(selected.plaintext_sha256, hashlib.sha256(dump.read_bytes()).hexdigest())

            dump.write_bytes(b"tampered")
            dump.chmod(0o600)
            with self.assertRaisesRegex(offsite.OffsiteBackupError, "checksum"):
                offsite.select_verified_backup(
                    root, dump, max_age_hours=24, apply=False
                )

    def test_public_key_validation_rejects_private_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("key bundle", encoding="utf-8")
            config = _config(key_path)
            responses = [
                subprocess.CompletedProcess([], 0, f"sec:::::::::{FINGERPRINT}:\n", ""),
            ]
            with mock.patch.object(offsite, "_run_gpg", side_effect=responses):
                with self.assertRaisesRegex(offsite.OffsiteBackupError, "private-key"):
                    offsite.validate_public_key(config, root / "gnupg", apply=False)

    def test_encrypt_uses_ephemeral_keyring_and_produces_mode_0600_ciphertext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )

            def fake_gpg(
                command: list[str], *, check: bool = True
            ) -> subprocess.CompletedProcess[str]:
                del check
                if "show-only" in command or "--list-keys" in command:
                    output = f"pub:::::::::\nfpr:::::::::{FINGERPRINT}:\n"
                    return subprocess.CompletedProcess(command, 0, output, "")
                if "--list-packets" in command:
                    output = ":pubkey enc packet:\n:encrypted data packet:\n"
                    return subprocess.CompletedProcess(command, 2, output, "No secret key")
                if "--encrypt" in command:
                    output_path = Path(command[command.index("--output") + 1])
                    output_path.write_bytes(b"OPENPGP-CIPHERTEXT")
                return subprocess.CompletedProcess(command, 0, "", "")

            work_dir = root / "work"
            work_dir.mkdir(mode=0o700)
            with mock.patch.object(offsite, "_run_gpg", side_effect=fake_gpg):
                encrypted = offsite.encrypt_backup(
                    _config(key_path), backup, work_dir, apply=False
                )

            self.assertEqual(encrypted.path.read_bytes(), b"OPENPGP-CIPHERTEXT")
            self.assertEqual(encrypted.path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(encrypted.path.read_bytes(), dump.read_bytes())

    def test_encrypt_refuses_gpg_output_that_is_still_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )

            def fake_gpg(
                command: list[str], *, check: bool = True
            ) -> subprocess.CompletedProcess[str]:
                del check
                if "show-only" in command or "--list-keys" in command:
                    output = f"pub:::::::::\nfpr:::::::::{FINGERPRINT}:\n"
                    return subprocess.CompletedProcess(command, 0, output, "")
                if "--encrypt" in command:
                    output_path = Path(command[command.index("--output") + 1])
                    output_path.write_bytes(dump.read_bytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            work_dir = root / "work"
            work_dir.mkdir(mode=0o700)
            with mock.patch.object(offsite, "_run_gpg", side_effect=fake_gpg):
                with self.assertRaisesRegex(
                    offsite.OffsiteBackupError, "plaintext backup"
                ):
                    offsite.encrypt_backup(
                        _config(key_path), backup, work_dir, apply=False
                    )

    def test_upload_body_is_ciphertext_and_head_verifies_both_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )
            encrypted_path = root / "backup.dump.gpg"
            encrypted_path.write_bytes(b"ENCRYPTED-ONLY")
            encrypted_path.chmod(0o600)
            md5_hex, md5_base64 = offsite.md5_file(encrypted_path)
            encrypted = offsite.EncryptedBackup(
                path=encrypted_path,
                sha256=offsite.sha256_file(encrypted_path),
                md5_hex=md5_hex,
                md5_base64=md5_base64,
                size_bytes=encrypted_path.stat().st_size,
            )
            config = _config(key_path)
            client = RecordingStorageClient(
                config=config, backup=backup, encrypted=encrypted
            )

            uploaded, remote = offsite.upload_and_verify(
                client,
                config=config,
                backup=backup,
                encrypted=encrypted,
                key=offsite.object_key(config, backup),
            )

            self.assertTrue(uploaded)
            self.assertEqual(remote["cipher_sha256"], encrypted.sha256)
            self.assertEqual(client.put_calls[0]["Body"], b"ENCRYPTED-ONLY")
            self.assertNotEqual(client.put_calls[0]["Body"], dump.read_bytes())
            self.assertEqual(client.put_calls[0]["IfNoneMatch"], "*")
            self.assertEqual(client.put_calls[0]["CacheControl"], "no-store")

    def test_existing_matching_object_is_idempotent_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )
            encrypted_path = root / "backup.dump.gpg"
            encrypted_path.write_bytes(b"ENCRYPTED-ONLY")
            md5_hex, md5_base64 = offsite.md5_file(encrypted_path)
            encrypted = offsite.EncryptedBackup(
                path=encrypted_path,
                sha256=offsite.sha256_file(encrypted_path),
                md5_hex=md5_hex,
                md5_base64=md5_base64,
                size_bytes=encrypted_path.stat().st_size,
            )
            config = _config(key_path)
            client = RecordingStorageClient(
                config=config, backup=backup, encrypted=encrypted
            )
            client.stored_head = {
                "Metadata": offsite._expected_metadata(config, backup, encrypted),
                "ContentLength": encrypted.size_bytes,
                "ContentType": "application/pgp-encrypted",
                "ETag": f'"{encrypted.md5_hex}"',
            }

            uploaded, _remote = offsite.upload_and_verify(
                client,
                config=config,
                backup=backup,
                encrypted=encrypted,
                key=offsite.object_key(config, backup),
            )

            self.assertFalse(uploaded)
            self.assertEqual(client.put_calls, [])

    def test_execute_dry_run_never_builds_or_contacts_r2_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, manifest = _create_verified_backup(root)
            backup = offsite.VerifiedBackup(
                dump_path=dump,
                metadata_path=manifest,
                timestamp=dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC),
                plaintext_sha256=offsite.sha256_file(dump),
                metadata_sha256=offsite.sha256_file(manifest),
                size_bytes=dump.stat().st_size,
            )
            work_paths: list[Path] = []

            def fake_encrypt(
                _config_value: offsite.OffsiteConfig,
                _backup_value: offsite.VerifiedBackup,
                work_dir: Path,
                *,
                apply: bool,
            ) -> offsite.EncryptedBackup:
                self.assertFalse(apply)
                work_paths.append(work_dir)
                path = work_dir / "cipher.gpg"
                path.write_bytes(b"cipher")
                md5_hex, md5_base64 = offsite.md5_file(path)
                return offsite.EncryptedBackup(
                    path=path,
                    sha256=offsite.sha256_file(path),
                    md5_hex=md5_hex,
                    md5_base64=md5_base64,
                    size_bytes=path.stat().st_size,
                )

            args = argparse.Namespace(
                apply=False,
                env_file=root / "unused-env",
                platform_env_file=root / "unused-platform-env",
                backup_dir=root,
                dump=dump,
                max_age_hours=24.0,
                timeout=20.0,
                as_json=False,
            )
            with (
                mock.patch.object(offsite, "load_config", return_value=_config(key_path)),
                mock.patch.object(offsite, "select_verified_backup", return_value=backup),
                mock.patch.object(offsite, "encrypt_backup", side_effect=fake_encrypt),
                mock.patch.object(offsite, "build_storage_client") as build_client,
            ):
                result = offsite.execute(args)

            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(result["remote_operations"], 0)
            self.assertEqual(result["retention_actions"], 0)
            self.assertEqual(len(work_paths), 1)
            self.assertFalse(work_paths[0].exists())
            build_client.assert_not_called()

    def test_configuration_failure_has_deterministic_exit_code_and_redacted_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            missing_env = Path(temporary_dir) / ".env.backup"
            with mock.patch("sys.stdout") as stdout:
                exit_code = offsite.main(["--env-file", str(missing_env), "--json"])

            self.assertEqual(exit_code, int(offsite.ExitCode.CONFIGURATION))
            output = "".join(str(call.args[0]) for call in stdout.write.call_args_list if call.args)
            self.assertNotIn("secret", output.lower())


if __name__ == "__main__":
    unittest.main()
