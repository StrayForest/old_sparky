from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
from io import BytesIO
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import subprocess
import stat
import py_compile
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import platform_release_active_artifact_proof as proof
from tools import platform_release_capture_probe as capture_probe
from tools import platform_live_qa_runtime_install as liveqa_runtime


class _TinyInstaller:
    TOOL_FILES = ("proof_tool.py",)
    SOURCE_TREES = ("apps/platform_api", "python_packages")
    SOURCE_FILES = ("deploy/apparmor/oldsparky-liveqa-chromium",)
    MAX_FILES = 100
    MAX_RUNTIME_BYTES = 1024 * 1024

    @staticmethod
    def _runtime_path_order_key(relative: str) -> tuple[str, ...]:
        return PurePosixPath(relative).parts


class _TinyInstallerWithSourceCachePolicy(_TinyInstaller):
    SOURCE_TREE_CACHE_POLICY = "paired-python-bytecode-v1"
    _source_tree_cache_relative = staticmethod(liveqa_runtime._source_tree_cache_relative)


def _stat_view(metadata: os.stat_result, *, uid: int | None = None, gid: int | None = None):
    return SimpleNamespace(
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_uid=metadata.st_uid if uid is None else uid,
        st_gid=metadata.st_gid if gid is None else gid,
        st_nlink=metadata.st_nlink,
        st_size=metadata.st_size,
        st_mode=metadata.st_mode,
        st_mtime_ns=metadata.st_mtime_ns,
        st_ctime_ns=metadata.st_ctime_ns,
    )


@contextmanager
def _trusted_nonroot_metadata():
    """Waive only root ownership for private test fixtures; retain real I/O checks."""

    def open_regular(path: Path, *, maximum: int, mode: int | None = None):
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            or (mode is None and stat.S_IMODE(before.st_mode) & 0o022)
        ):
            proof._fail("test_fixture_metadata")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if proof._fingerprint(opened) != proof._fingerprint(before):
            os.close(descriptor)
            proof._fail("test_fixture_changed")
        return descriptor, opened

    def safe_input(path: Path, *, maximum: int, mode: int = 0o600):
        return open_regular(path, maximum=maximum, mode=mode)

    def safe_release(path: Path, *, maximum: int):
        return open_regular(path, maximum=maximum)

    def protected_directory(path: Path):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_nlink < 2
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            proof._fail("test_fixture_directory")
        return metadata

    def source_tree_directory(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_nlink < 2
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            proof._fail("test_fixture_directory")

    with (
        patch.object(proof, "_safe_file", side_effect=safe_input),
        patch.object(proof, "_safe_release_file", side_effect=safe_release),
        patch.object(proof, "_check_protected_directory", side_effect=protected_directory),
        patch.object(proof, "_check_dir_metadata", side_effect=source_tree_directory),
    ):
        yield


@contextmanager
def _trusted_pointer_owner(current: Path):
    """Keep the real symlink identity/target while stubbing its root owner fields."""
    original_lstat = Path.lstat

    def lstat(path: Path):
        metadata = original_lstat(path)
        if path == current:
            return _stat_view(metadata, uid=0, gid=0)
        return metadata

    with patch.object(Path, "lstat", new=lstat):
        yield


def _trusted_capture_module_bytes(path: Path) -> tuple[bytes, SimpleNamespace]:
    """Stub only root ownership for the pinned capture-module fixture."""
    before = path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size > capture_probe.MAX_MODULE_BYTES
    ):
        proof._fail("test_fixture_module_metadata")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        def identity(item):
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_uid,
                item.st_gid,
                item.st_nlink,
                item.st_mode,
            )
        if identity(opened) != identity(before):
            proof._fail("test_fixture_module_changed")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(
            descriptor, min(65536, capture_probe.MAX_MODULE_BYTES + 1 - size)
        ):
            chunks.append(chunk)
            size += len(chunk)
            if size > capture_probe.MAX_MODULE_BYTES:
                proof._fail("test_fixture_module_size")
        after = os.fstat(descriptor)
        if (
            identity(after) != identity(opened)
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            proof._fail("test_fixture_module_changed")
        return b"".join(chunks), _stat_view(opened, uid=0, gid=0)
    finally:
        os.close(descriptor)


def _receipt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 1,
        "repository": proof.REPOSITORY,
        "run_id": proof.DEPLOY_RUN_ID,
        "run_attempt": proof.DEPLOY_RUN_ATTEMPT,
        "artifact_id": proof.ARTIFACT_ID,
        "artifact_name": proof.ARTIFACT_NAME,
        "artifact_zip_sha256": proof.ARTIFACT_ZIP_SHA256,
        "tar_sha256": proof.RELEASE_TAR_SHA256,
        "source_sha": proof.SOURCE_SHA,
        "release_slug": proof.RELEASE_SLUG,
        "source_ref": proof.SOURCE_REF,
        "policies": [dict(policy) for policy in proof.ATTESTATION_POLICIES],
    }
    value.update(overrides)
    return value


def _write_receipt(path: Path, **overrides: object) -> bytes:
    raw = (json.dumps(_receipt(**overrides), sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def _tiny_release_tree(root: Path, *, source_value: bytes = b"VALUE = 1\n") -> dict[str, bytes]:
    files = {
        "tools/proof_tool.py": b"TOOL = True\n",
        "apps/platform_api/module.py": source_value,
        "python_packages/package.py": b"PACKAGE = True\n",
        "deploy/apparmor/oldsparky-liveqa-chromium": b"profile\n",
        "liveqa-runtime/runtime-manifest.json": b'{"version":1}\n',
    }
    for relative in (
        "tools",
        "apps",
        "apps/platform_api",
        "python_packages",
        "deploy",
        "deploy/apparmor",
        "liveqa-runtime",
    ):
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o444)
    return files


def _write_tiny_archive(
    path: Path, files: dict[str, bytes], *, release_slug: str | None = None
) -> None:
    root = release_slug or proof.RELEASE_SLUG
    directories = (
        root,
        f"{root}/tools",
        f"{root}/apps",
        f"{root}/apps/platform_api",
        f"{root}/python_packages",
        f"{root}/deploy",
        f"{root}/deploy/apparmor",
        f"{root}/liveqa-runtime",
    )
    with tarfile.open(path, mode="w:gz") as archive:
        for name in directories:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            member.uid = member.gid = 0
            archive.addfile(member)
        for relative, content in files.items():
            member = tarfile.TarInfo(f"{root}/{relative}")
            member.size = len(content)
            member.mode = 0o444
            member.uid = member.gid = 0
            archive.addfile(member, BytesIO(content))
    path.chmod(0o600)


class PlatformReleaseActiveArtifactProofTests(unittest.TestCase):
    def test_full_api_accepts_exact_active_fixture_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app_dir = base / "platform"
            release = app_dir / "releases" / proof.RELEASE_SLUG
            release.mkdir(parents=True, mode=0o755)
            release.chmod(0o755)
            app_dir.chmod(0o755)
            (app_dir / "releases").chmod(0o755)
            current = app_dir / "current"
            current.symlink_to(f"releases/{proof.RELEASE_SLUG}")

            source_sha = proof.SOURCE_SHA
            files = _tiny_release_tree(release)
            release_payload = json.dumps(
                {"source_git_commit": source_sha, "release_slug": proof.RELEASE_SLUG},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii") + b"\n"
            validator_source = b"""from pathlib import Path
import hashlib
import json
import tarfile
MAX_RELEASE_JSON_BYTES = 4096
def _checksum_contract(artifact, checksum):
    data = Path(artifact).read_bytes()
    expected = hashlib.sha256(data).hexdigest()
    if Path(checksum).read_text(encoding='ascii') != f'{expected}  {Path(artifact).name}\\n':
        raise ValueError('checksum')
    return expected
def validate_archive(artifact, *, release_slug, expected_source_commit):
    with tarfile.open(artifact, 'r:gz') as archive:
        payload = json.load(archive.extractfile(f'{release_slug}/RELEASE.json'))
    if payload.get('source_git_commit') != expected_source_commit or payload.get('release_slug') != release_slug:
        raise ValueError('release identity')
    return payload
"""
            added_files = {
                "RELEASE.json": release_payload,
                "tools/platform_validate_release_artifact.py": validator_source,
                "tools/platform_live_qa_runtime_install.py": b"# fixture runtime boundary\n",
            }
            files.update(added_files)
            for relative, content in added_files.items():
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o444)

            artifact = base / f"{proof.RELEASE_SLUG}.tar.gz"
            _write_tiny_archive(artifact, files)
            with _trusted_nonroot_metadata():
                active_files, active_dirs, _, _, _ = proof._read_mapped_inputs(
                    _TinyInstaller, artifact, release, deadline=time.monotonic() + 10
                )
            proof._add_generated_payload(
                _TinyInstaller, active_files, active_dirs,
                source_sha=source_sha, runtime_provider=None,
            )
            active_digest = proof._expected_tree_digest(
                _TinyInstaller, active_files, active_dirs
            )
            installer_source = (
                "import json\n"
                "from pathlib import Path\n"
                f"TOOL_FILES = {_TinyInstaller.TOOL_FILES!r}\n"
                f"SOURCE_TREES = {_TinyInstaller.SOURCE_TREES!r}\n"
                f"SOURCE_FILES = {_TinyInstaller.SOURCE_FILES!r}\n"
                f"MAX_FILES = {_TinyInstaller.MAX_FILES}\n"
                f"MAX_RUNTIME_BYTES = {_TinyInstaller.MAX_RUNTIME_BYTES}\n"
                "def _runtime_path_order_key(relative): return tuple(relative.split('/'))\n"
                "def verify(app_dir, target_sha):\n"
                f"    return {{'files': {active_files!r}, 'payload_tree_sha256': {active_digest!r}}}\n"
                "def _validate_runtime_source(path, *, source_sha):\n"
                "    return json.loads((Path(path) / 'runtime-manifest.json').read_text(encoding='ascii'))\n"
            ).encode("ascii")
            files["tools/platform_live_qa_runtime_install.py"] = installer_source
            installer_path = release / "tools/platform_live_qa_runtime_install.py"
            installer_path.chmod(0o644)
            installer_path.write_bytes(installer_source)
            installer_path.chmod(0o444)
            _write_tiny_archive(artifact, files)
            tar_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            checksum = artifact.with_name(artifact.name + ".sha256")
            checksum.write_text(f"{tar_digest}  {artifact.name}\n", encoding="ascii")
            checksum.chmod(0o600)
            receipt = base / "attestation-receipt.json"
            receipt.write_bytes((json.dumps({
                "schema": 1,
                "repository": proof.REPOSITORY,
                "run_id": proof.DEPLOY_RUN_ID,
                "run_attempt": proof.DEPLOY_RUN_ATTEMPT,
                "artifact_id": proof.ARTIFACT_ID,
                "artifact_name": proof.ARTIFACT_NAME,
                "artifact_zip_sha256": proof.ARTIFACT_ZIP_SHA256,
                "tar_sha256": tar_digest,
                "source_sha": source_sha,
                "release_slug": proof.RELEASE_SLUG,
                "source_ref": "refs/heads/dev",
                "policies": [dict(item) for item in proof.ATTESTATION_POLICIES],
            }, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
            receipt.chmod(0o600)
            def trusted_dir(path: Path):
                metadata = path.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
                    proof._fail("test_fixture_directory")
                return metadata

            def tree_fingerprint() -> dict[str, tuple[int, int, int, int]]:
                result: dict[str, tuple[int, int, int, int]] = {}
                for path in sorted(release.rglob("*")):
                    metadata = path.lstat()
                    result[str(path.relative_to(release))] = (
                        metadata.st_mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                    )
                return result

            load_capture_probe = proof._load_capture_probe

            def load_trusted_capture_probe(*, deadline: float):
                module = load_capture_probe(deadline=deadline)
                module._safe_module_bytes = _trusted_capture_module_bytes
                return module

            before_tree = tree_fingerprint()
            with (
                patch.multiple(
                    proof,
                    APP_DIR=app_dir,
                    SOURCE_SHA=source_sha,
                    RELEASE_TAR_SHA256=tar_digest,
                    ARTIFACT_ZIP_SHA256=proof.ARTIFACT_ZIP_SHA256,
                    DEPLOY_RUN_ID=proof.DEPLOY_RUN_ID,
                    DEPLOY_RUN_ATTEMPT=proof.DEPLOY_RUN_ATTEMPT,
                    ARTIFACT_ID=proof.ARTIFACT_ID,
                    ARTIFACT_NAME=proof.ARTIFACT_NAME,
                    _check_protected_directory=trusted_dir,
                ),
                _trusted_nonroot_metadata(),
                _trusted_pointer_owner(current),
                patch.object(proof, "_load_capture_probe", side_effect=load_trusted_capture_probe),
            ):
                result = proof.verify_active_release_artifact(
                    app_dir, artifact, checksum, receipt, deadline_seconds=30
                )

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["proof"], "active_signed_artifact_reuse")
            self.assertEqual(result["source_sha"], source_sha)
            self.assertEqual(result["payload_tree_sha256"], active_digest)
            self.assertEqual(tree_fingerprint(), before_tree)
            self.assertEqual(os.readlink(current), f"releases/{proof.RELEASE_SLUG}")

    def test_checked_and_timestamp_cache_bodies_match_only_exact_source_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app_dir = base / "platform"
            source_path = app_dir / "releases" / proof.RELEASE_SLUG / "apps/platform_api/module.py"
            source_path.parent.mkdir(parents=True)
            source_bytes = b"VALUE = 7\n"
            source_path.write_bytes(source_bytes)
            source_stat = source_path.stat()
            source_relative = source_path.relative_to(app_dir / "releases" / proof.RELEASE_SLUG)
            filenames = (
                str(source_path),
                str(app_dir / "current" / source_relative),
            )
            relative_pyc = "apps/platform_api/__pycache__/module.cpython-312.pyc"

            with patch.object(proof, "APP_DIR", app_dir):
                for index, filename in enumerate(filenames):
                    checked_path = base / f"checked-{index}.pyc"
                    py_compile.compile(
                        str(source_path),
                        cfile=str(checked_path),
                        dfile=filename,
                        doraise=True,
                        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
                    )
                    checked = checked_path.read_bytes()
                    proof._validate_derived_pyc_bytes(
                        checked,
                        source_bytes,
                        source_stat,
                        source_path,
                        relative_pyc,
                        deadline=time.monotonic() + 10,
                    )
                    timestamp_pyc = (
                        checked[:4]
                        + (0).to_bytes(4, "little")
                        + ((source_stat.st_mtime_ns // 1_000_000_000) & 0xFFFFFFFF).to_bytes(4, "little")
                        + (len(source_bytes) & 0xFFFFFFFF).to_bytes(4, "little")
                        + checked[16:]
                    )
                    proof._validate_derived_pyc_bytes(
                        timestamp_pyc,
                        source_bytes,
                        source_stat,
                        source_path,
                        relative_pyc,
                        deadline=time.monotonic() + 10,
                    )

                trailing_pyc = (
                    checked[:16]
                    + checked[16:]
                    + b"unparsed-trailing-bytes"
                )
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof._validate_derived_pyc_bytes(
                        trailing_pyc,
                        source_bytes,
                        source_stat,
                        source_path,
                        relative_pyc,
                        deadline=time.monotonic() + 10,
                    )
                self.assertEqual(caught.exception.code, "active_cache_code_mismatch")

                changed_code = marshal.dumps(
                    compile(b"VALUE = 8\n", filenames[0], "exec", dont_inherit=True)
                )
                changed_pyc = (
                    importlib.util.MAGIC_NUMBER
                    + (3).to_bytes(4, "little")
                    + importlib.util.source_hash(source_bytes)
                    + changed_code
                )
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof._validate_derived_pyc_bytes(
                        changed_pyc,
                        source_bytes,
                        source_stat,
                        source_path,
                        relative_pyc,
                        deadline=time.monotonic() + 10,
                    )
                self.assertEqual(caught.exception.code, "active_cache_code_mismatch")

                cached_code = marshal.loads(checked[16:])
                mutations = (
                    cached_code.replace(co_stacksize=cached_code.co_stacksize + 1),
                    cached_code.replace(co_qualname="untrusted_qualname"),
                )
                nested_source = b"def nested():\n    return 7\n"
                nested_code = compile(nested_source, filenames[0], "exec", dont_inherit=True)
                nested_function = next(item for item in nested_code.co_consts if hasattr(item, "co_code"))
                changed_nested = nested_function.replace(
                    co_consts=tuple(8 if item == 7 and type(item) is int else item for item in nested_function.co_consts)
                )
                changed_nested_code = nested_code.replace(
                    co_consts=tuple(changed_nested if item is nested_function else item for item in nested_code.co_consts)
                )
                mutations += (changed_nested_code,)
                for altered_code in mutations:
                    with self.subTest(altered_code=altered_code.co_qualname):
                        altered_pyc = (
                            importlib.util.MAGIC_NUMBER
                            + (3).to_bytes(4, "little")
                            + importlib.util.source_hash(source_bytes)
                            + marshal.dumps(altered_code)
                        )
                        with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                            proof._validate_derived_pyc_bytes(
                                altered_pyc,
                                source_bytes,
                                source_stat,
                                source_path,
                                relative_pyc,
                                deadline=time.monotonic() + 10,
                            )
                        self.assertEqual(caught.exception.code, "active_cache_code_mismatch")

                with patch.object(
                    proof.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("cache-compare", 1),
                ):
                    with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                        proof._cache_body_matches_source(
                            checked[16:],
                            source_bytes,
                            filenames,
                            optimization=0,
                            deadline=time.monotonic() + 10,
                        )
                self.assertEqual(caught.exception.code, "active_cache_compare_timeout")

                arbitrary_code = marshal.dumps(
                    compile(source_bytes, "/tmp/untrusted/module.py", "exec", dont_inherit=True)
                )
                arbitrary_pyc = (
                    importlib.util.MAGIC_NUMBER
                    + (3).to_bytes(4, "little")
                    + importlib.util.source_hash(source_bytes)
                    + arbitrary_code
                )
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof._validate_derived_pyc_bytes(
                        arbitrary_pyc,
                        source_bytes,
                        source_stat,
                        source_path,
                        relative_pyc,
                        deadline=time.monotonic() + 10,
                    )
                self.assertEqual(caught.exception.code, "active_cache_code_mismatch")

    def test_source_inventory_traversal_error_is_not_silently_ignored(self) -> None:
        failure = PermissionError("fixture traversal error")

        def failing_walk(_base, **kwargs):
            kwargs["onerror"](failure)
            return iter(())

        with patch.object(proof.os, "walk", side_effect=failing_walk):
            with self.assertRaises(PermissionError) as caught:
                list(proof._walk_release_tree(Path("/fixture/source")))
        self.assertIs(caught.exception, failure)

    def test_tiny_tar_map_matches_capture_comparator_and_rejects_source_drift_or_extra(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            release = base / "release"
            release.mkdir(mode=0o755)
            files = _tiny_release_tree(release)
            artifact = base / "tiny.tar.gz"
            _write_tiny_archive(artifact, files)

            with _trusted_nonroot_metadata():
                mapped, directories, input_count, input_bytes, cache_count = proof._read_mapped_inputs(
                    _TinyInstaller,
                    artifact,
                    release,
                    deadline=time.monotonic() + 20,
                )
            expected = {
                "platform/tools/proof_tool.py": hashlib.sha256(files["tools/proof_tool.py"]).hexdigest(),
                "platform/apps/platform_api/module.py": hashlib.sha256(files["apps/platform_api/module.py"]).hexdigest(),
                "platform/python_packages/package.py": hashlib.sha256(files["python_packages/package.py"]).hexdigest(),
                "platform/deploy/apparmor/oldsparky-liveqa-chromium": hashlib.sha256(files["deploy/apparmor/oldsparky-liveqa-chromium"]).hexdigest(),
                "runtime/runtime-manifest.json": hashlib.sha256(files["liveqa-runtime/runtime-manifest.json"]).hexdigest(),
            }
            self.assertEqual(mapped, expected)
            self.assertEqual(input_count, len(files))
            self.assertEqual(input_bytes, sum(map(len, files.values())))
            self.assertEqual(cache_count, 0)
            tree_digest = proof._expected_tree_digest(_TinyInstaller, mapped, directories)
            matched, _ = capture_probe._compare_expected_payload(
                tree_digest,
                mapped,
                {"files": mapped, "payload_tree_sha256": tree_digest},
            )
            self.assertTrue(matched)

            mapped_source = release / "apps/platform_api/module.py"
            mapped_source.chmod(0o644)
            mapped_source.write_bytes(b"VALUE = 2\n")
            mapped_source.chmod(0o444)
            with _trusted_nonroot_metadata(), self.assertRaises(proof.ActiveArtifactProofError) as changed:
                proof._read_mapped_inputs(
                    _TinyInstaller,
                    artifact,
                    release,
                    deadline=time.monotonic() + 20,
                )
            self.assertEqual(changed.exception.code, "active_source_mismatch")

            mapped_source.chmod(0o644)
            mapped_source.write_bytes(files["apps/platform_api/module.py"])
            mapped_source.chmod(0o444)
            extra = release / "python_packages/unmapped.py"
            extra.write_bytes(b"EXTRA = True\n")
            extra.chmod(0o444)
            with _trusted_nonroot_metadata(), self.assertRaises(proof.ActiveArtifactProofError) as unexpected:
                proof._read_mapped_inputs(
                    _TinyInstaller,
                    artifact,
                    release,
                    deadline=time.monotonic() + 20,
                )
            self.assertEqual(unexpected.exception.code, "active_source_extra")
            extra.unlink()

            # The old source-9ce installer has no cache policy, so every archive
            # member remains part of its expected source-tree payload.
            legacy_cache_files = dict(files)
            legacy_cache = "apps/platform_api/__pycache__/module.cpython-312.pyc"
            legacy_cache_files[legacy_cache] = b"legacy cache bytes\n"
            cache_path = release / legacy_cache
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(legacy_cache_files[legacy_cache])
            cache_path.chmod(0o444)
            legacy_artifact = base / "legacy-cache.tar.gz"
            _write_tiny_archive(legacy_artifact, legacy_cache_files)
            with _trusted_nonroot_metadata():
                legacy_map, _legacy_dirs, legacy_count, legacy_bytes, _ = proof._read_mapped_inputs(
                    _TinyInstaller,
                    legacy_artifact,
                    release,
                    deadline=time.monotonic() + 20,
                )
            self.assertIn(f"platform/{legacy_cache}", legacy_map)
            self.assertEqual(legacy_count, len(legacy_cache_files))
            self.assertEqual(legacy_bytes, sum(map(len, legacy_cache_files.values())))
            cache_path.unlink()
            cache_path.parent.rmdir()

            # A future signed installer declares the one closed cache policy:
            # source-tree caches disappear, while runtime-artifact caches remain.
            forward_files = dict(files)
            forward_files[legacy_cache] = b"not copied by source policy\n"
            runtime_cache = "liveqa-runtime/__pycache__/artifact.cpython-312.pyc"
            forward_files[runtime_cache] = b"runtime artifact cache bytes\n"
            (release / runtime_cache).parent.mkdir(parents=True, exist_ok=True)
            (release / runtime_cache).write_bytes(forward_files[runtime_cache])
            (release / runtime_cache).chmod(0o444)
            forward_artifact = base / "forward-cache-policy.tar.gz"
            _write_tiny_archive(forward_artifact, forward_files)
            with _trusted_nonroot_metadata():
                forward_map, forward_dirs, forward_count, forward_bytes, _ = proof._read_mapped_inputs(
                    _TinyInstallerWithSourceCachePolicy,
                    forward_artifact,
                    release,
                    deadline=time.monotonic() + 20,
                )
            self.assertNotIn(f"platform/{legacy_cache}", forward_map)
            self.assertNotIn("platform/apps/platform_api/__pycache__", forward_dirs)
            self.assertIn("runtime/__pycache__/artifact.cpython-312.pyc", forward_map)
            self.assertEqual(forward_count, len(forward_files) - 1)
            self.assertEqual(
                forward_bytes,
                sum(len(value) for key, value in forward_files.items() if key != legacy_cache),
            )

    def test_receipt_binding_refuses_before_host_state_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app_dir = base / "app"
            receipt_path = base / "receipt.json"
            tar_path = base / f"{proof.RELEASE_SLUG}.tar.gz"
            tar_path.write_bytes(b"not the pinned tar")
            tar_path.chmod(0o600)
            before_tar = tar_path.read_bytes()
            before_receipt = _write_receipt(receipt_path, run_id=proof.DEPLOY_RUN_ID + 1)

            with _trusted_nonroot_metadata(), patch.object(proof, "APP_DIR", app_dir):
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof.verify_active_release_artifact(
                        app_dir,
                        tar_path,
                        tar_path.with_name(tar_path.name + ".sha256"),
                        receipt_path,
                    )
            self.assertEqual(caught.exception.code, "receipt_binding")
            self.assertFalse((app_dir / "current").exists())
            self.assertEqual(tar_path.read_bytes(), before_tar)
            self.assertEqual(receipt_path.read_bytes(), before_receipt)

    def test_current_target_and_archive_or_checksum_binding_refuse_without_pointer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app_dir = base / "app"
            releases = app_dir / "releases"
            releases.mkdir(parents=True)
            target = releases / proof.RELEASE_SLUG
            target.mkdir()
            target.chmod(0o755)
            other = releases / "other"
            other.mkdir()
            other.chmod(0o755)
            current = app_dir / "current"
            current.symlink_to("releases/other")
            original_link = os.readlink(current)
            original_pointer = current.lstat()

            with _trusted_nonroot_metadata(), _trusted_pointer_owner(current), patch.object(proof, "APP_DIR", app_dir):
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof._check_current(app_dir)
            self.assertEqual(caught.exception.code, "current_target")
            self.assertEqual(os.readlink(current), original_link)
            self.assertEqual(current.lstat().st_ino, original_pointer.st_ino)

            current.unlink()
            current.symlink_to(f"releases/{proof.RELEASE_SLUG}")
            tar_path = base / f"{proof.RELEASE_SLUG}.tar.gz"
            tar_path.write_bytes(b"wrong tar bytes")
            tar_path.chmod(0o600)
            receipt_path = base / "receipt.json"
            _write_receipt(receipt_path)
            before_tar = tar_path.read_bytes()
            before_receipt = receipt_path.read_bytes()
            link_before = os.readlink(current)

            with _trusted_nonroot_metadata(), _trusted_pointer_owner(current), patch.object(proof, "APP_DIR", app_dir):
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof.verify_active_release_artifact(
                        app_dir,
                        tar_path,
                        tar_path.with_name(tar_path.name + ".sha256"),
                        receipt_path,
                    )
            self.assertEqual(caught.exception.code, "archive_digest")
            self.assertEqual(os.readlink(current), link_before)
            self.assertEqual(tar_path.read_bytes(), before_tar)
            self.assertEqual(receipt_path.read_bytes(), before_receipt)

            valid_tar_sha = hashlib.sha256(before_tar).hexdigest()
            checksum_path = tar_path.with_name(tar_path.name + ".sha256")
            checksum_path.write_text("0" * 64 + "  " + tar_path.name + "\n", encoding="ascii")
            checksum_path.chmod(0o600)
            before_checksum = checksum_path.read_bytes()
            _write_receipt(receipt_path, tar_sha256=valid_tar_sha)
            before_receipt = receipt_path.read_bytes()
            with (
                _trusted_nonroot_metadata(),
                _trusted_pointer_owner(current),
                patch.object(proof, "APP_DIR", app_dir),
                patch.object(proof, "RELEASE_TAR_SHA256", valid_tar_sha),
            ):
                with self.assertRaises(proof.ActiveArtifactProofError) as caught:
                    proof.verify_active_release_artifact(
                        app_dir,
                        tar_path,
                        checksum_path,
                        receipt_path,
                    )
            self.assertEqual(caught.exception.code, "checksum_binding")
            self.assertEqual(os.readlink(current), link_before)
            self.assertEqual(tar_path.read_bytes(), before_tar)
            self.assertEqual(receipt_path.read_bytes(), before_receipt)
            self.assertEqual(checksum_path.read_bytes(), before_checksum)
