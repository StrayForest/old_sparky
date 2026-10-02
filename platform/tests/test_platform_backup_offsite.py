from __future__ import annotations

import argparse
import datetime as dt
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import platform_test_lock_support as lock_support
from tools import platform_backup_supervisor


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_offsite.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_offsite", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
offsite = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = offsite
SPEC.loader.exec_module(offsite)

MANIFEST_SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_manifest.py"
MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "platform_backup_manifest_for_tests", MANIFEST_SCRIPT_PATH
)
assert MANIFEST_SPEC is not None and MANIFEST_SPEC.loader is not None
manifest_contract = importlib.util.module_from_spec(MANIFEST_SPEC)
sys.modules[MANIFEST_SPEC.name] = manifest_contract
MANIFEST_SPEC.loader.exec_module(manifest_contract)

RESTORE_SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py"
RESTORE_SPEC = importlib.util.spec_from_file_location(
    "platform_backup_restore_drill_for_offsite_tests", RESTORE_SCRIPT_PATH
)
assert RESTORE_SPEC is not None and RESTORE_SPEC.loader is not None
backup_creator = importlib.util.module_from_spec(RESTORE_SPEC)
sys.modules[RESTORE_SPEC.name] = backup_creator
RESTORE_SPEC.loader.exec_module(backup_creator)

TEST_HELPERS = platform_backup_supervisor.TrustedPostgresHelpers(
    runuser="/usr/sbin/runuser",
    createdb="/usr/bin/createdb",
    dropdb="/usr/bin/dropdb",
    psql="/usr/bin/psql",
    pg_dump="/usr/bin/pg_dump",
    pg_restore="/usr/bin/pg_restore",
)


@contextmanager
def _held_test_lock():
    with tempfile.TemporaryDirectory() as temporary_dir:
        path = Path(temporary_dir) / platform_backup_supervisor.BACKUP_LOCK_PATH.name
        with lock_support.root_owned_backup_lock(platform_backup_supervisor, path) as lock:
            yield lock


FINGERPRINT = "A" * 40
R2_ENDPOINT = f"https://{'a' * 32}.r2.cloudflarestorage.com"


def _trusted_source(source_root: Path) -> Path:
    versions = source_root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001.py").write_text(
        "revision = '20260913_0053'\ndown_revision = None\n",
        encoding="utf-8",
    )
    return source_root


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
    run_id = "a" * 32
    dump = directory / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{run_id}.dump"
    _write_private(dump, b"PGDMP restore-verified payload")
    manifest = dump.with_suffix(".json")
    metadata = manifest_contract.build_manifest(
        run_id=run_id,
        dump_file=dump.name,
        size_bytes=dump.stat().st_size,
        sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
        started_at_utc=now,
        completed_at_utc=now,
        duration_seconds=0,
        restore_verified=True,
        alembic_revision_verified=True,
        restored_table_count=31,
        restore_error=None,
    )
    _write_private(manifest, json.dumps(metadata, indent=2) + "\n")
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
    def setUp(self) -> None:
        self._restore_lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore_lock_dir.cleanup)
        restore_lock_path = Path(self._restore_lock_dir.name) / "restore-lifecycle.lock"
        self._restore_lock_path_patch = mock.patch.object(
            backup_creator, "RESTORE_LIFECYCLE_LOCK_PATH", restore_lock_path
        )
        self._restore_lock_path_patch.start()
        self.addCleanup(self._restore_lock_path_patch.stop)
        for name, value in (
            ("RESTORE_LIFECYCLE_LOCK_OWNER", os.geteuid()),
            ("RESTORE_LIFECYCLE_LOCK_GROUP", os.getegid()),
        ):
            patcher = mock.patch.object(backup_creator, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create_backup(
        self,
        args: argparse.Namespace,
        *,
        app_dir: Path,
        source_root: Path,
    ) -> dict[str, object]:
        with _held_test_lock() as lock:
            return platform_backup_supervisor._run_local_backup_scope(
                args,
                app_dir=app_dir,
                source_root=source_root,
                _restore_module=backup_creator,
                lock=lock,
                callback=lambda capability, trusted_head, _restore: backup_creator.create_backup(
                    args,
                    capability=capability,
                    trusted_alembic_head=trusted_head,
                ),
            )

    def test_actual_creator_manifest_is_consumed_by_offsite_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            env_path = root / ".env.platform"
            output_dir = root / "backups"
            output_dir.mkdir()
            trusted_source = _trusted_source(root / "trusted-source")
            args = argparse.Namespace(
                env_file=str(env_path),
                output_dir=str(output_dir),
                keep=2,
                admin_database_url=None,
                dump_only=False,
            )

            def fake_run_command(
                command: list[str], *, stdout: int | None = None, **_: object
            ) -> subprocess.CompletedProcess[str]:
                if Path(command[0]).name == "pg_dump":
                    assert stdout is not None
                    self.assertNotIn("--file", command)
                    os.write(stdout, b"PGDMP creator output")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    backup_creator.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@127.0.0.1/platformdb"},
                    clear=False,
                ),
                mock.patch.object(backup_creator, "load_env", return_value={"PLATFORM_DATABASE_URL": "postgresql://platform_user@127.0.0.1/platformdb"}),
                mock.patch.object(
                    backup_creator, "require_commands", return_value=TEST_HELPERS
                ),
                mock.patch.object(backup_creator, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_creator, "perform_restore_drill", return_value=31),
            ):
                created = self._create_backup(
                    args,
                    app_dir=root,
                    source_root=trusted_source,
                )

            selected = offsite.select_verified_backup(
                output_dir, None, max_age_hours=24, apply=False
            )

            self.assertTrue(created["restore_verified"])
            self.assertEqual(created["schemas"], ["platform", "public"])
            self.assertEqual(selected.dump_path.name, created["dump_file"])
            self.assertEqual(selected.plaintext_sha256, created["sha256"])

    def test_same_second_creator_runs_have_distinct_run_ids_and_archive_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            output_dir = root / "backups"
            output_dir.mkdir()
            trusted_source = _trusted_source(root / "trusted-source")
            fixed_now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)
            args = argparse.Namespace(
                env_file=str(root / ".env.platform"),
                output_dir=str(output_dir),
                keep=2,
                admin_database_url=None,
                dump_only=False,
            )

            def fake_run_command(
                command: list[str], *, stdout: int | None = None, **_: object
            ) -> subprocess.CompletedProcess[str]:
                if Path(command[0]).name == "pg_dump":
                    assert stdout is not None
                    self.assertNotIn("--file", command)
                    os.write(stdout, b"PGDMP same second")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    backup_creator.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@127.0.0.1/platformdb"},
                    clear=False,
                ),
                mock.patch.object(backup_creator, "load_env", return_value={"PLATFORM_DATABASE_URL": "postgresql://platform_user@127.0.0.1/platformdb"}),
                mock.patch.object(
                    backup_creator, "require_commands", return_value=TEST_HELPERS
                ),
                mock.patch.object(backup_creator, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_creator, "perform_restore_drill", return_value=31),
                mock.patch.object(backup_creator, "utc_now", return_value=fixed_now),
            ):
                first = self._create_backup(
                    args,
                    app_dir=root,
                    source_root=trusted_source,
                )
                second = self._create_backup(
                    args,
                    app_dir=root,
                    source_root=trusted_source,
                )

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(tuple(output_dir.glob("*.dump"))), 2)
            self.assertEqual(len(tuple(output_dir.glob("*.json"))), 2)
            for result in (first, second):
                dump = output_dir / result["dump_file"]
                metadata = dump.with_suffix(".json")
                self.assertTrue(dump.exists())
                self.assertTrue(metadata.exists())
                self.assertEqual(json.loads(metadata.read_text())["run_id"], result["run_id"])

    def test_load_config_requires_private_separate_r2_contour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            public_key = root / "recovery.asc"
            public_key.write_text("public key", encoding="utf-8")
            env_path = root / ".env.backup"
            platform_env_path = root / ".env.platform"
            _write_private(env_path, _backup_env(public_key))
            _write_private(platform_env_path, _platform_env())

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

    def test_select_verified_backup_rejects_read_only_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dump, manifest_path = _create_verified_backup(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            legacy = {
                key: value
                for key, value in payload.items()
                if key in manifest_contract.LEGACY_MANIFEST_KEY_SET
            }
            legacy["format_version"] = manifest_contract.LEGACY_MANIFEST_FORMAT_VERSION
            _write_private(manifest_path, json.dumps(legacy) + "\n")

            with self.assertRaisesRegex(offsite.OffsiteBackupError, "current-format"):
                offsite.select_verified_backup(
                    root, dump, max_age_hours=24, apply=False
                )
            with self.assertRaisesRegex(offsite.OffsiteBackupError, "current-format"):
                offsite.select_verified_backup(
                    root, None, max_age_hours=24, apply=False
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

    def test_gpg_packet_verifier_receives_the_held_ciphertext_fd(self) -> None:
        with mock.patch.object(
            offsite.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            offsite._run_gpg(
                [offsite.GPG_BINARY, "--list-packets", "/proc/self/fd/37"],
                check=False,
                pass_fds=(37,),
            )
        self.assertEqual(run.call_args.kwargs["pass_fds"], (37,))

    def test_supervisor_apply_passes_ciphertext_fd_to_upload_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            key_path = root / "recovery.asc"
            key_path.write_text("public key", encoding="utf-8")
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )
            cipher_path = root / "cipher.gpg"
            cipher_path.write_bytes(b"ciphertext")
            cipher_path.chmod(0o600)
            cipher_fd = os.open(cipher_path, os.O_RDONLY)
            md5_hex, md5_base64 = offsite.md5_file(cipher_path)
            encrypted = offsite.EncryptedBackup(
                path=cipher_path,
                sha256=offsite.sha256_file(cipher_path),
                md5_hex=md5_hex,
                md5_base64=md5_base64,
                size_bytes=cipher_path.stat().st_size,
                fd=cipher_fd,
            )
            captured: dict[str, object] = {}

            def upload_and_verify(*_args: object, encrypted_fd: int | None = None, **_kwargs: object):
                captured["encrypted_fd"] = encrypted_fd
                return True, {"cipher_sha256": encrypted.sha256}

            fake_offsite = SimpleNamespace(
                select_verified_backup=lambda *_args, **_kwargs: backup,
                load_config=lambda *_args, **_kwargs: _config(key_path),
                encrypt_backup_from_fd=lambda *_args, **_kwargs: encrypted,
                object_key=offsite.object_key,
                upload_and_verify=upload_and_verify,
            )
            restore = SimpleNamespace(
                expected_alembic_head=lambda _source_root: "20260913_0053"
            )
            evidence = SimpleNamespace(
                payload={"alembic": {}},
                update_pair=lambda _pair: None,
                update_remote=lambda **_kwargs: None,
            )
            args = argparse.Namespace(
                backup_dir=str(root),
                dump=str(dump),
                max_age_hours=24.0,
                apply=True,
                env_file=str(root / ".env.backup"),
                platform_env_file=str(root / ".env.platform"),
                timeout=8.0,
            )
            client = SimpleNamespace(head_bucket=lambda **_kwargs: None)
            real_import_module = platform_backup_supervisor.importlib.import_module

            def import_module(name: str):
                if name in {"tools.platform_backup_offsite", "platform_backup_offsite"}:
                    return fake_offsite
                return real_import_module(name)

            with _held_test_lock() as lock:
                with mock.patch.object(
                    platform_backup_supervisor.importlib,
                    "import_module",
                    side_effect=import_module,
                ):
                    result = platform_backup_supervisor._run_offsite_scope(
                        args,
                        app_dir=root,
                        lock=lock,
                        _restore_module=restore,
                        callback=lambda capability, trusted_head, _restore: platform_backup_supervisor.run_offsite(
                            args,
                            app_dir=root,
                            trusted_alembic_head=trusted_head,
                            capability=capability,
                            lock=lock,
                            evidence=evidence,
                            client=client,
                        ),
                    )
            self.assertTrue(result["verified"])
            self.assertEqual(captured["encrypted_fd"], cipher_fd)
            with self.assertRaises(OSError):
                os.fstat(cipher_fd)

    def test_supervisor_offsite_cleanup_failure_is_attached_to_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dump, _manifest = _create_verified_backup(root)
            backup = offsite.select_verified_backup(
                root, dump, max_age_hours=24, apply=False
            )
            fake_offsite = SimpleNamespace(
                select_verified_backup=lambda *_args, **_kwargs: backup,
                load_config=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("primary offsite failure")
                ),
            )
            restore = SimpleNamespace(
                expected_alembic_head=lambda _source_root: "20260913_0053"
            )
            evidence = SimpleNamespace(
                payload={"alembic": {}},
                update_pair=lambda _pair: None,
                update_remote=lambda **_kwargs: None,
            )
            args = argparse.Namespace(
                backup_dir=str(root),
                dump=str(dump),
                max_age_hours=24.0,
                apply=False,
                env_file=str(root / ".env.backup"),
                platform_env_file=str(root / ".env.platform"),
                timeout=8.0,
            )
            real_rmtree = platform_backup_supervisor.shutil.rmtree

            def failing_rmtree(path: str | os.PathLike[str], *args: object, **kwargs: object):
                if str(path).startswith("/tmp/oldsparky-offsite-"):
                    raise OSError("ciphertext cleanup failed")
                return real_rmtree(path, *args, **kwargs)

            real_import_module = platform_backup_supervisor.importlib.import_module

            def import_module(name: str):
                if name in {"tools.platform_backup_offsite", "platform_backup_offsite"}:
                    return fake_offsite
                return real_import_module(name)

            with _held_test_lock() as lock:
                with (
                    mock.patch.object(
                        platform_backup_supervisor.importlib,
                        "import_module",
                        side_effect=import_module,
                    ),
                    mock.patch.object(
                        platform_backup_supervisor.shutil,
                        "rmtree",
                        side_effect=failing_rmtree,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "primary offsite failure") as raised:
                        platform_backup_supervisor._run_offsite_scope(
                            args,
                            app_dir=root,
                            lock=lock,
                            _restore_module=restore,
                            callback=lambda capability, trusted_head, _restore: platform_backup_supervisor.run_offsite(
                                args,
                                app_dir=root,
                                trusted_alembic_head=trusted_head,
                                capability=capability,
                                lock=lock,
                                evidence=evidence,
                            ),
                        )
            payload = platform_backup_supervisor.safe_error_payload(raised.exception)
            self.assertEqual(payload["cleanup_status"], "unproven")
            self.assertNotIn("database_id", payload)

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
