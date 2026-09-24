from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_safe_env_exec.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_safe_env_exec_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
safe_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_env)


class SafeEnvExecTests(unittest.TestCase):
    LIVE_QA_PAYLOAD = Path(
        "/root/.oldsparky/liveqa/releases/"
        "0123456789abcdef0123456789abcdef01234567"
    )

    @contextmanager
    def active_payload_fixture(self, *, file_count: int = 0):
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            production = root / "production"
            production_releases = production / "releases"
            production_release = production_releases / "release-under-test"
            production_release.mkdir(mode=0o755, parents=True)
            production_releases.mkdir(mode=0o755, exist_ok=True)
            os.chmod(production_releases, 0o755)
            (production_release / "RELEASE.json").write_text(
                json.dumps({"source_git_commit": "a" * 40}) + "\n",
                encoding="ascii",
            )
            os.chmod(production_release / "RELEASE.json", 0o444)
            (production / "current").symlink_to(production_release)

            trusted = root / "liveqa"
            payload_root = trusted / "releases"
            payload = payload_root / ("a" * 40)
            payload.mkdir(mode=0o555, parents=True)
            files: dict[str, str] = {}
            for index in range(file_count):
                relative = f"manifest-files/entry-{index:04d}-{'x' * 70}"
                path = payload / relative
                path.parent.mkdir(mode=0o555, exist_ok=True)
                path.write_bytes(b"x")
                os.chmod(path.parent, 0o555)
                os.chmod(path, 0o444)
                files[relative] = hashlib.sha256(b"x").hexdigest()
            os.chmod(payload, 0o555)
            payload_tree_digest = hashlib.sha256()
            for path in sorted(payload.rglob("*")):
                relative = path.relative_to(payload).as_posix()
                metadata = path.lstat()
                payload_tree_digest.update(relative.encode("utf-8") + b"\0")
                if stat.S_ISDIR(metadata.st_mode):
                    payload_tree_digest.update(b"d\0")
                else:
                    payload_tree_digest.update(
                        b"f\0" + bytes.fromhex(files[relative])
                    )

            trusted.mkdir(mode=0o700, exist_ok=True)
            os.chmod(trusted, 0o700)
            os.chmod(payload_root, 0o755)
            (trusted / "active").symlink_to(f"releases/{'a' * 40}")
            manifest = trusted / "active-manifest.json"
            manifest.write_bytes(
                (
                    json.dumps(
                        {
                            "version": 1,
                            "source_sha": "a" * 40,
                            "release_slug": "release-under-test",
                            "payload": str(payload),
                            "payload_tree_sha256": payload_tree_digest.hexdigest(),
                            "files": files,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("ascii")
            )
            os.chown(manifest, 0, 0)
            os.chmod(manifest, 0o444)
            with mock.patch.multiple(
                safe_env,
                PRODUCTION_RUNTIME_ROOT=production,
                ACTIVE_PLATFORM_ROOT=production / "current",
                LIVE_QA_ROOT=trusted,
                LIVE_QA_RELEASE_ROOT=payload_root,
                LIVE_QA_ACTIVE_MANIFEST=manifest,
                LIVE_QA_ACTIVE_POINTER=trusted / "active",
            ):
                yield manifest, payload

    def test_production_path_requires_root_owned_fixed_components(self) -> None:
        self.assertEqual(safe_env._production_component_owners(), (0, 0, 0, 0, 0))

    def test_dotenv_is_parsed_as_data_without_shell_expansion(self) -> None:
        values = safe_env.parse_dotenv(
            b"PLATFORM_ENVIRONMENT=production\n"
            b"PLATFORM_WEB_ORIGIN=https://old-sparky.com\n"
            b"PLATFORM_SECRET_KEY='$(touch /tmp/must-not-run)'\n"
            b"PLATFORM_EMAIL_SENDER_EMAIL='Old Sparky <noreply@example.invalid>'\n"
        )
        self.assertEqual(values["PLATFORM_SECRET_KEY"], "$(touch /tmp/must-not-run)")
        self.assertEqual(
            values["PLATFORM_EMAIL_SENDER_EMAIL"],
            "Old Sparky <noreply@example.invalid>",
        )

    def test_dotenv_rejects_export_duplicate_and_foreign_keys(self) -> None:
        invalid_payloads = (
            b"export PLATFORM_ENVIRONMENT=production\n",
            b"PLATFORM_ENVIRONMENT=production\nPLATFORM_ENVIRONMENT=test\n",
            b"LD_PRELOAD=/tmp/attack.so\n",
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(safe_env.SafeEnvError):
                    safe_env.parse_dotenv(payload)

    def test_dotenv_rejects_noncanonical_line_separators(self) -> None:
        for payload in (
            b"PLATFORM_ENVIRONMENT=production\rPLATFORM_WEB_ORIGIN=x\n",
            "PLATFORM_ENVIRONMENT=production\u2028PLATFORM_WEB_ORIGIN=x".encode(),
            b"PLATFORM_ENVIRONMENT=production\x00ignored\n",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(safe_env.SafeEnvError, "unsafe|carriage"):
                    safe_env.parse_dotenv(payload)

    def test_clean_environment_drops_every_inherited_variable(self) -> None:
        values = {
            "PLATFORM_ENVIRONMENT": "production",
            "PLATFORM_DATABASE_URL": "postgresql://required-secret",
        }
        with (
            mock.patch.dict(
                os.environ,
                {"LD_PRELOAD": "/tmp/attack.so", "PYTHONPATH": "/tmp/attack"},
                clear=True,
            ),
            mock.patch.object(
                safe_env,
                "_read_liveqa_manifest",
                return_value={"payload": str(self.LIVE_QA_PAYLOAD)},
            ),
        ):
            child = safe_env.clean_child_environment(
                values,
                pythonpath=self.LIVE_QA_PAYLOAD,
            )
        self.assertNotIn("LD_PRELOAD", child)
        self.assertEqual(child["PYTHONPATH"], str(self.LIVE_QA_PAYLOAD))
        self.assertEqual(child["HOME"], "/nonexistent")
        self.assertEqual(
            child["PLATFORM_DATABASE_URL"], values["PLATFORM_DATABASE_URL"]
        )

    def test_command_validation_requires_matching_runtime_contour(self) -> None:
        with (
            mock.patch.object(
                safe_env,
                "_read_liveqa_manifest",
                return_value={"payload": str(self.LIVE_QA_PAYLOAD)},
            ),
            self.assertRaisesRegex(safe_env.SafeEnvError, "approved live QA DB tool"),
        ):
            safe_env.validate_trusted_command(
                [
                    str(safe_env.ACTIVE_PYTHON),
                    str(
                        safe_env.ACTIVE_PLATFORM_ROOT
                        / "tools/platform_cleanup_live_user_qa.py"
                    ),
                ],
                pythonpath=self.LIVE_QA_PAYLOAD,
            )

    def test_retained_report_recovery_is_an_approved_db_tool(self) -> None:
        self.assertIn(
            "platform_recover_retained_report.py",
            safe_env.TRUSTED_DB_TOOLS,
        )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_reads_an_exact_root_owned_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            env_file = private / "environment"
            payload = b"PLATFORM_ENVIRONMENT=production\n"
            env_file.write_bytes(payload)
            os.chmod(env_file, 0o600)
            owners = tuple(
                path.stat().st_uid for path in (Path("/"), Path("/root"), root, private)
            )
            self.assertEqual(
                safe_env._read_env_bytes_at(env_file, owners=owners),
                payload,
            )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_rejects_a_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir(mode=0o700)
            env_file = actual / "environment"
            env_file.write_text("PLATFORM_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(env_file, 0o600)
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            unsafe_path = linked / "environment"
            owners = (0, 0, 0, 0)
            with self.assertRaisesRegex(safe_env.SafeEnvError, "unavailable or unsafe"):
                safe_env._read_env_bytes_at(unsafe_path, owners=owners)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_rejects_a_hardlinked_env_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            env_file = private / "environment"
            env_file.write_text("PLATFORM_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(env_file, 0o600)
            os.link(env_file, private / "second-link")
            owners = tuple(
                path.stat().st_uid for path in (Path("/"), Path("/root"), root, private)
            )
            with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
                safe_env._read_env_bytes_at(env_file, owners=owners)

    def test_safe_env_main_refuses_nonroot_before_reading(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch.object(safe_env, "read_production_env_bytes") as read_env,
            mock.patch("sys.stderr", stderr),
        ):
            result = safe_env.main(["print-public-value", "PLATFORM_WEB_ORIGIN"])
        self.assertEqual(result, 2)
        read_env.assert_not_called()
        self.assertIn("requires root", stderr.getvalue())

    def test_private_file_contract_is_exact_0600_root_owned(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFREG | 0o640,
            st_nlink=1,
            st_uid=0,
            st_gid=0,
            st_size=10,
        )
        with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
            safe_env._validate_env_file(metadata)

    def test_active_manifest_between_release_and_active_bounds_is_accepted(self) -> None:
        with self.active_payload_fixture(file_count=900) as (manifest, _payload):
            self.assertGreater(manifest.stat().st_size, safe_env.MAX_RELEASE_JSON_BYTES)
            self.assertLessEqual(manifest.stat().st_size, safe_env.MAX_ACTIVE_MANIFEST_BYTES)
            payload = safe_env._read_liveqa_manifest()
            self.assertEqual(payload["source_sha"], "a" * 40)

    def test_active_manifest_over_active_bound_is_rejected(self) -> None:
        with self.active_payload_fixture(file_count=3200) as (manifest, _payload):
            self.assertGreater(manifest.stat().st_size, safe_env.MAX_ACTIVE_MANIFEST_BYTES)
            with self.assertRaisesRegex(safe_env.SafeEnvError, "size limit|metadata is unsafe"):
                safe_env._read_liveqa_manifest()

    def test_active_manifest_rejects_symlink_hardlink_and_wrong_mode(self) -> None:
        with self.active_payload_fixture(file_count=1) as (manifest, _payload):
            replacement = manifest.with_name("replacement.json")
            replacement.write_bytes(manifest.read_bytes())
            os.chmod(replacement, 0o444)
            manifest.unlink()
            manifest.symlink_to(replacement)
            with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
                safe_env._read_liveqa_manifest()

        with self.active_payload_fixture(file_count=1) as (manifest, _payload):
            os.link(manifest, manifest.with_name("second-link"))
            with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
                safe_env._read_liveqa_manifest()

        with self.active_payload_fixture(file_count=1) as (manifest, _payload):
            os.chmod(manifest, 0o644)
            with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
                safe_env._read_liveqa_manifest()

    def test_active_manifest_rejects_deterministic_replacement_before_open(self) -> None:
        with self.active_payload_fixture(file_count=1) as (manifest, _payload):
            replacement = manifest.with_name("replacement.json")
            replacement.write_bytes(manifest.read_bytes())
            os.chmod(replacement, 0o444)
            real_open = os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if Path(path) == manifest:
                    os.replace(replacement, manifest)
                return real_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(safe_env.os, "open", side_effect=replace_before_open),
                self.assertRaisesRegex(safe_env.SafeEnvError, "changed while opening"),
            ):
                safe_env._read_liveqa_manifest()

    def test_active_manifest_source_contract_uses_bounded_descriptor_reader(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("LIVE_QA_ACTIVE_MANIFEST.read_text", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("os.fstat(descriptor)", source)


if __name__ == "__main__":
    unittest.main()
