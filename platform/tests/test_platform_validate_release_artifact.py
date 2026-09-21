from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from tests import platform_chromium_sandbox_fixture as chromium_sandbox_fixture
from tools import platform_validate_release_artifact as validator
from tools import platform_live_qa_guard
from tools import platform_live_qa_runtime_install
from tools import platform_live_user_qa_dispatch
from tools import platform_safe_env_exec


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_SCRIPT = REPO_ROOT / "platform/tools/platform_validate_release_artifact.py"
RELEASE_REF = "artifact-test"
BUILT_AT = "20260811T120000Z"
RELEASE_SLUG = f"{RELEASE_REF}-{BUILT_AT}"


def release_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_format_version": 1,
        "release_slug": RELEASE_SLUG,
        "built_at_utc": BUILT_AT,
        "release_ref": RELEASE_REF,
        "source_git_commit": "a" * 40,
        "python_requirements_file": "requirements-platform.txt",
        "python_lock_file": "requirements-platform.lock.txt",
        "python_freeze_file": "requirements-platform.freeze.txt",
        "python_wheelhouse_dir": "wheelhouse",
        "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
        "web_package_lock_file": "apps/platform_web/package-lock.json",
        "web_build_id": "safe-build-id_123",
        "node_version": validator.PINNED_NODE_VERSION,
        "npm_version": validator.PINNED_NPM_VERSION,
    }
    payload.update(overrides)
    return payload


class ArchiveBuilder:
    def __init__(
        self, artifact: Path, payload: dict[str, object] | None = None
    ) -> None:
        self.artifact = artifact
        self.entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
        self.add_directory(RELEASE_SLUG)
        self.add_file(
            f"{RELEASE_SLUG}/RELEASE.json",
            json.dumps(payload or release_payload(), sort_keys=True).encode() + b"\n",
            mode=0o444,
        )
        self.add_file(f"{RELEASE_SLUG}/.env.platform.example", b"EXAMPLE=value\n")
        self.add_file(f"{RELEASE_SLUG}/requirements-platform.txt", b"pip==26.1.2\n")
        self.add_file(
            f"{RELEASE_SLUG}/requirements-platform.lock.txt",
            b"pip==26.1.2\n",
        )
        self.add_file(
            f"{RELEASE_SLUG}/requirements-platform.freeze.txt",
            b"pip==26.1.2\n",
            mode=0o444,
        )
        self.add_directory(f"{RELEASE_SLUG}/wheelhouse", mode=0o555)
        self.add_file(
            f"{RELEASE_SLUG}/wheelhouse/WHEELHOUSE.sha256",
            b"0" * 64 + b"  pip-26.1.2-py3-none-any.whl\n",
            mode=0o444,
        )
        self.add_file(
            f"{RELEASE_SLUG}/wheelhouse/pip-26.1.2-py3-none-any.whl",
            b"wheel",
            mode=0o444,
        )
        for directory in (
            "apps",
            "apps/platform_web",
            "apps/platform_web/.next",
            "apps/platform_web/.next/standalone",
            "apps/platform_web/.next/standalone/.next",
            "apps/platform_web/.next/standalone/.next/static",
        ):
            self.add_directory(f"{RELEASE_SLUG}/{directory}")
        self.add_file(f"{RELEASE_SLUG}/apps/platform_web/package-lock.json", b"{}\n")
        self.add_file(
            f"{RELEASE_SLUG}/apps/platform_web/.next/standalone/server.js",
            b"console.log('ok');\n",
        )
        self.add_directory(f"{RELEASE_SLUG}/tools")
        self.add_file(
            f"{RELEASE_SLUG}/tools/platform_nginx_error_summary.py",
            b"#!/usr/bin/env python3\nprint('nginx summary')\n",
        )
        self.add_file(
            f"{RELEASE_SLUG}/tools/platform_web_runtime_diagnostics_summary.py",
            b"#!/usr/bin/env python3\nprint('web summary')\n",
        )
        self.add_file(
            f"{RELEASE_SLUG}/tools/platform_storage_evidence_summary.py",
            b"#!/usr/bin/env python3\nprint('storage summary')\n",
        )
        self.add_file(
            f"{RELEASE_SLUG}/tools/platform_media_migration_diagnostics_summary.py",
            b"#!/usr/bin/env python3\nprint('media migration summary')\n",
        )
        self._add_liveqa_runtime()

    def _add_liveqa_runtime(self) -> None:
        """Add the minimum digest-bound runtime required by current artifacts."""

        runtime = f"{RELEASE_SLUG}/liveqa-runtime"
        directories = (
            runtime,
            f"{runtime}/node",
            f"{runtime}/node/bin",
            f"{runtime}/web",
            f"{runtime}/web/tests",
            f"{runtime}/web/tests/smoke",
            f"{runtime}/web/tests/support",
            f"{runtime}/web/node_modules",
            f"{runtime}/web/node_modules/@playwright",
            f"{runtime}/web/node_modules/@playwright/test",
            f"{runtime}/web/node_modules/playwright",
            f"{runtime}/web/node_modules/playwright-core",
            f"{runtime}/browsers",
            f"{runtime}/browsers/chromium-1228",
            f"{runtime}/browsers/chromium-1228/chrome-linux64",
            f"{runtime}/browsers/chromium_headless_shell-1228",
            f"{runtime}/browsers/webkit-2311",
            f"{runtime}/browsers/ffmpeg-1011",
        )
        for directory in directories:
            self.add_directory(directory, mode=0o555)
        files: dict[str, bytes] = {
            "node/bin/node": b"node\n",
            "web/package-lock.json": b"{}\n",
            "web/playwright.live.config.ts": b"export default {};\n",
            "web/tests/smoke/live-user-journey.spec.ts": b"test('live', () => {});\n",
            "web/tests/support/live-qa-origin.ts": b"export {};\n",
            "web/tests/support/live-qa-sandbox.ts": b"export {};\n",
            "web/node_modules/@playwright/test/package.json": b'{"name":"@playwright/test"}\n',
            "web/node_modules/playwright/package.json": b'{"name":"playwright"}\n',
            "web/node_modules/playwright-core/package.json": b'{"name":"playwright-core"}\n',
        }
        for relative, content in files.items():
            self.add_file(f"{runtime}/{relative}", content, mode=0o555 if relative == "node/bin/node" else 0o444)
        self.add_file(
            f"{runtime}/browsers/chromium-1228/chrome-linux64/chrome_sandbox",
            chromium_sandbox_fixture.read_bytes(),
            mode=0o4755,
        )

    def add_directory(self, name: str, *, mode: int = 0o755) -> tarfile.TarInfo:
        member = tarfile.TarInfo(name)
        member.type = tarfile.DIRTYPE
        member.mode = mode
        member.uid = member.gid = 0
        self.entries.append((member, None))
        return member

    def add_file(
        self, name: str, content: bytes, *, mode: int = 0o644
    ) -> tarfile.TarInfo:
        member = tarfile.TarInfo(name)
        member.type = tarfile.REGTYPE
        member.mode = mode
        member.uid = member.gid = 0
        member.size = len(content)
        self.entries.append((member, content))
        return member

    def add_symlink(self, name: str, linkname: str) -> tarfile.TarInfo:
        member = tarfile.TarInfo(name)
        member.type = tarfile.SYMTYPE
        member.mode = 0o777
        member.uid = member.gid = 0
        member.linkname = linkname
        self.entries.append((member, None))
        return member

    def write(self) -> Path:
        runtime_prefix = f"{RELEASE_SLUG}/liveqa-runtime/"
        digest = hashlib.sha256()
        files: dict[str, str] = {}
        for member, content in sorted(self.entries, key=lambda item: item[0].name):
            if not member.name.startswith(runtime_prefix) or member.name.endswith(
                "/runtime-manifest.json"
            ):
                continue
            relative = member.name.removeprefix(runtime_prefix)
            digest.update(relative.encode() + b"\0")
            if member.isdir():
                digest.update(b"d\0")
            else:
                assert content is not None
                file_digest = hashlib.sha256(content).hexdigest()
                files[relative] = file_digest
                digest.update(b"f\0" + bytes.fromhex(file_digest))
        manifest = {
            "version": 1,
            "node_version": validator.PINNED_NODE_VERSION,
            "package_lock_sha256": hashlib.sha256(b"{}\n").hexdigest(),
            "tree_sha256": digest.hexdigest(),
            "files": files,
        }
        self.add_file(
            f"{RELEASE_SLUG}/liveqa-runtime/runtime-manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            mode=0o444,
        )
        with tarfile.open(self.artifact, "w:gz") as archive:
            for member, content in self.entries:
                archive.addfile(
                    member, None if content is None else io.BytesIO(content)
                )
        return self.artifact


class PlatformReleaseArtifactValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_checksum(self, artifact: Path, *, digest: str | None = None) -> Path:
        checksum = Path(f"{artifact}.sha256")
        checksum.write_text(
            f"{digest or hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n",
            encoding="ascii",
        )
        return checksum

    def test_valid_archive_checksum_and_safe_extraction(self) -> None:
        sandbox = chromium_sandbox_fixture.read_bytes()
        self.assertEqual(len(sandbox), chromium_sandbox_fixture.EXPECTED_SIZE)
        self.assertEqual(
            hashlib.sha256(sandbox).hexdigest(),
            chromium_sandbox_fixture.EXPECTED_SHA256,
        )
        for size, digest in (
            (validator.LIVE_QA_SANDBOX_SIZE, validator.LIVE_QA_SANDBOX_SHA256),
            (
                platform_live_qa_guard.CHROMIUM_SANDBOX_SIZE,
                platform_live_qa_guard.CHROMIUM_SANDBOX_SHA256,
            ),
            (
                platform_live_qa_runtime_install.CHROMIUM_SANDBOX_SIZE,
                platform_live_qa_runtime_install.CHROMIUM_SANDBOX_SHA256,
            ),
            (
                platform_live_user_qa_dispatch.CHROMIUM_SANDBOX_SIZE,
                platform_live_user_qa_dispatch.CHROMIUM_SANDBOX_SHA256,
            ),
            (
                platform_safe_env_exec.LIVE_QA_SANDBOX_SIZE,
                platform_safe_env_exec.LIVE_QA_SANDBOX_SHA256,
            ),
        ):
            self.assertEqual(size, chromium_sandbox_fixture.EXPECTED_SIZE)
            self.assertEqual(digest, chromium_sandbox_fixture.EXPECTED_SHA256)
        artifact = self.root / f"{RELEASE_SLUG}.tar.gz"
        builder = ArchiveBuilder(artifact)
        builder.add_symlink(
            f"{RELEASE_SLUG}/server-link",
            "apps/platform_web/.next/standalone/server.js",
        )
        builder.write()
        checksum = self.write_checksum(artifact)
        extraction_root = self.root / "releases"
        extraction_root.mkdir()

        validator._checksum_contract(artifact, checksum)
        with tarfile.open(artifact, "r:gz") as archive:
            sandbox_member = archive.getmember(
                f"{RELEASE_SLUG}/{validator.LIVE_QA_SANDBOX_RELATIVE}"
            )
            self.assertEqual((sandbox_member.uid, sandbox_member.gid), (0, 0))
            self.assertEqual(sandbox_member.mode, 0o4755)
        payload = validator.validate_archive(
            artifact,
            release_slug=RELEASE_SLUG,
            extract_to=extraction_root,
        )

        release = extraction_root / RELEASE_SLUG
        self.assertEqual(payload["source_git_commit"], "a" * 40)
        self.assertEqual(
            (release / "server-link").resolve(),
            release / "apps/platform_web/.next/standalone/server.js",
        )
        self.assertEqual((release / "server-link").read_text(), "console.log('ok');\n")

    def test_cli_checks_checksum_before_archive_parsing(self) -> None:
        artifact = self.root / f"{RELEASE_SLUG}.tar.gz"
        artifact.write_bytes(b"not a tar")
        checksum = self.write_checksum(artifact, digest="0" * 64)

        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(VALIDATOR_SCRIPT),
                "--artifact",
                str(artifact),
                "--checksum",
                str(checksum),
                "--release-slug",
                RELEASE_SLUG,
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertNotIn("archive is invalid", result.stderr)

    def test_checksum_must_be_exactly_adjacent_and_name_the_basename(self) -> None:
        artifact = ArchiveBuilder(self.root / f"{RELEASE_SLUG}.tar.gz").write()
        self.write_checksum(artifact)
        other = self.root / "other.sha256"
        other.write_text(f"{'0' * 64}  {artifact.name}\n")

        with self.assertRaisesRegex(validator.ArtifactError, "not adjacent"):
            validator._checksum_contract(artifact, other)

        Path(f"{artifact}.sha256").write_text(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  subdir/{artifact.name}\n"
        )
        with self.assertRaisesRegex(validator.ArtifactError, "adjacent sha256sum"):
            validator._checksum_contract(artifact, Path(f"{artifact}.sha256"))

    def test_non_canonical_archive_paths_are_rejected(self) -> None:
        unsafe_names = (
            "",
            f"{RELEASE_SLUG}//collision",
            f"{RELEASE_SLUG}/./collision",
            f"{RELEASE_SLUG}/../collision",
            f"/{RELEASE_SLUG}/collision",
            f"{RELEASE_SLUG}/bad\\name",
        )
        for index, unsafe_name in enumerate(unsafe_names):
            with self.subTest(name=unsafe_name):
                artifact = self.root / f"unsafe-{index}.tar.gz"
                builder = ArchiveBuilder(artifact)
                builder.add_file(unsafe_name, b"x")
                builder.write()
                with self.assertRaisesRegex(
                    validator.ArtifactError, "non-canonical path"
                ):
                    validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

    def test_duplicate_and_missing_parent_paths_are_rejected(self) -> None:
        duplicate = self.root / "duplicate.tar.gz"
        builder = ArchiveBuilder(duplicate)
        builder.add_file(f"{RELEASE_SLUG}/requirements-platform.txt", b"pip==26.1.2\n")
        builder.write()
        with self.assertRaisesRegex(validator.ArtifactError, "duplicate paths"):
            validator.validate_archive(duplicate, release_slug=RELEASE_SLUG)

        missing_parent = self.root / "missing-parent.tar.gz"
        builder = ArchiveBuilder(missing_parent)
        builder.add_file(f"{RELEASE_SLUG}/not-listed/child", b"x")
        builder.write()
        with self.assertRaisesRegex(validator.ArtifactError, "omits a parent"):
            validator.validate_archive(missing_parent, release_slug=RELEASE_SLUG)

    def test_only_safe_member_types_modes_and_ownership_are_allowed(self) -> None:
        cases = []

        def set_setuid(builder: ArchiveBuilder) -> None:
            builder.entries[-1][0].mode = 0o4755

        def set_world_writable(builder: ArchiveBuilder) -> None:
            builder.entries[-1][0].mode = 0o666

        def set_non_root(builder: ArchiveBuilder) -> None:
            builder.entries[-1][0].uid = 1000

        def set_type(builder: ArchiveBuilder, member_type: bytes) -> None:
            builder.entries[-1][0].type = member_type
            builder.entries[-1][0].size = 0
            builder.entries[-1] = (builder.entries[-1][0], None)

        cases.extend(
            (
                ("setuid", set_setuid),
                ("world-writable", set_world_writable),
                ("non-root", set_non_root),
                ("fifo", lambda builder: set_type(builder, tarfile.FIFOTYPE)),
                ("device", lambda builder: set_type(builder, tarfile.CHRTYPE)),
                ("hardlink", lambda builder: set_type(builder, tarfile.LNKTYPE)),
            )
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                artifact = self.root / f"{label}.tar.gz"
                builder = ArchiveBuilder(artifact)
                builder.add_file(f"{RELEASE_SLUG}/{label}", b"x")
                mutate(builder)
                builder.write()
                with self.assertRaises(validator.ArtifactError):
                    validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

    def test_symlinks_must_be_canonical_contained_and_non_dangling(self) -> None:
        cases = (
            ("escape", "../../etc/passwd"),
            ("absolute", "/etc/passwd"),
            ("repeated", "apps//platform_web"),
            ("dangling", "missing"),
        )
        for label, target in cases:
            with self.subTest(label=label):
                artifact = self.root / f"link-{label}.tar.gz"
                builder = ArchiveBuilder(artifact)
                builder.add_symlink(f"{RELEASE_SLUG}/bad-link", target)
                builder.write()
                with self.assertRaises(validator.ArtifactError):
                    validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

        cycle = self.root / "link-cycle.tar.gz"
        builder = ArchiveBuilder(cycle)
        builder.add_symlink(f"{RELEASE_SLUG}/one", "two")
        builder.add_symlink(f"{RELEASE_SLUG}/two", "one")
        builder.write()
        with self.assertRaisesRegex(validator.ArtifactError, "cycle"):
            validator.validate_archive(cycle, release_slug=RELEASE_SLUG)

    def test_member_count_and_expanded_size_are_bounded(self) -> None:
        artifact = self.root / "bounded.tar.gz"
        ArchiveBuilder(artifact).write()
        with mock.patch.object(validator, "MAX_MEMBERS", 2):
            with self.assertRaisesRegex(validator.ArtifactError, "member-count"):
                validator.validate_archive(artifact, release_slug=RELEASE_SLUG)
        with mock.patch.object(validator, "MAX_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(validator.ArtifactError, "expanded size"):
                validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

    def test_release_json_has_an_exact_structural_contract(self) -> None:
        invalid_payloads = (
            {**release_payload(), "unexpected": True},
            release_payload(release_ref='bad"ref'),
            release_payload(built_at_utc="20261399T996099Z"),
            release_payload(release_slug="another-release"),
            release_payload(python_freeze_file="../freeze"),
            release_payload(runtime_layout={"app_dir": "/tmp"}),
            release_payload(node_version="26.3.0"),
            release_payload(npm_version="11.15.0"),
            release_payload(artifact_format_version=True),
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                artifact = self.root / f"metadata-{index}.tar.gz"
                ArchiveBuilder(artifact, payload).write()
                with self.assertRaises(validator.ArtifactError):
                    validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

        duplicate_keys = self.root / "metadata-duplicate-key.tar.gz"
        builder = ArchiveBuilder(duplicate_keys)
        release_name = f"{RELEASE_SLUG}/RELEASE.json"
        for index, (member, content) in enumerate(builder.entries):
            if member.name == release_name:
                assert content is not None
                duplicate = content.rstrip()[:-1] + b',"release_slug":"duplicate"}\n'
                member.size = len(duplicate)
                builder.entries[index] = (member, duplicate)
                break
        builder.write()
        with self.assertRaisesRegex(validator.ArtifactError, "duplicate keys"):
            validator.validate_archive(duplicate_keys, release_slug=RELEASE_SLUG)

    def test_standalone_runtime_cache_is_not_allowed_in_immutable_artifact(self) -> None:
        artifact = self.root / "runtime-cache.tar.gz"
        builder = ArchiveBuilder(artifact)
        cache = f"{RELEASE_SLUG}/apps/platform_web/.next/standalone/.next/cache"
        builder.add_directory(cache)
        builder.add_file(f"{cache}/index", b"stale runtime state")
        builder.write()

        with self.assertRaisesRegex(validator.ArtifactError, "runtime cache"):
            validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

    def test_runtime_diagnostic_helpers_require_nonempty_tracked_content(self) -> None:
        for index, relative in enumerate(validator.REQUIRED_RUNTIME_DIAGNOSTIC_HELPERS):
            artifact = self.root / f"empty-runtime-helper-{index}.tar.gz"
            builder = ArchiveBuilder(artifact)
            helper_name = f"{RELEASE_SLUG}/{relative}"
            for entry_index, (member, content) in enumerate(builder.entries):
                if member.name == helper_name:
                    member.size = 0
                    builder.entries[entry_index] = (member, b"")
                    break
            else:  # pragma: no cover - ArchiveBuilder includes every helper.
                self.fail(f"fixture omitted {relative}")
            builder.write()
            with self.assertRaisesRegex(validator.ArtifactError, "helper content"):
                validator.validate_archive(artifact, release_slug=RELEASE_SLUG)

    def test_rollback_state_is_reserved_for_the_installer(self) -> None:
        artifact = self.root / "rollback-state.tar.gz"
        builder = ArchiveBuilder(artifact)
        builder.add_directory(f"{RELEASE_SLUG}/.rollback", mode=0o700)
        builder.write()

        with self.assertRaisesRegex(validator.ArtifactError, "reserved rollback"):
            validator.validate_archive(artifact, release_slug=RELEASE_SLUG)


if __name__ == "__main__":
    unittest.main()
