from __future__ import annotations

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4
import zipfile

from tests import platform_test_lock_support as lock_support
from tests import platform_chromium_sandbox_fixture


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_guard.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_live_qa_guard_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@contextmanager
def synthetic_trusted_tools():
    """Materialize a root-owned minimal trusted-wrapper fixture under /root."""

    parent = Path("/root")
    parent_metadata = parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent.resolve(strict=True) != parent
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise AssertionError("unsafe synthetic tools parent")
    tools_root = Path(tempfile.mkdtemp(prefix="oldsparky-liveqa-tools-", dir=parent))
    initial: tuple[int, int] | None = None
    wrapper = tools_root / "platform_live_user_qa.sh"
    wrapper_initial: tuple[int, int] | None = None
    try:
        tools_metadata = tools_root.lstat()
        initial = (tools_metadata.st_dev, tools_metadata.st_ino)
        if (
            tools_root.parent != parent
            or not tools_root.name.startswith("oldsparky-liveqa-tools-")
            or not stat.S_ISDIR(tools_metadata.st_mode)
            or tools_metadata.st_uid != 0
            or tools_metadata.st_gid != 0
            or stat.S_IMODE(tools_metadata.st_mode) != 0o700
        ):
            raise AssertionError("unsafe synthetic tools directory")
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        wrapper.chmod(0o700)
        wrapper_metadata = wrapper.lstat()
        wrapper_initial = (wrapper_metadata.st_dev, wrapper_metadata.st_ino)
        if (
            not stat.S_ISREG(wrapper_metadata.st_mode)
            or wrapper_metadata.st_uid != 0
            or wrapper_metadata.st_gid != 0
            or wrapper_metadata.st_nlink != 1
            or stat.S_IMODE(wrapper_metadata.st_mode) != 0o700
        ):
            raise AssertionError("unsafe synthetic trusted wrapper")
        yield tools_root
    finally:
        try:
            current = tools_root.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            common_metadata_ok = (
                tools_root.parent == parent
                and tools_root.name.startswith("oldsparky-liveqa-tools-")
                and stat.S_ISDIR(current.st_mode)
                and current.st_uid == 0
                and current.st_gid == 0
                and (initial is None or (current.st_dev, current.st_ino) == initial)
            )
            current_wrapper = None
            if wrapper_initial is not None:
                try:
                    current_wrapper = wrapper.lstat()
                except FileNotFoundError:
                    current_wrapper = None
            wrapper_metadata_ok = wrapper_initial is None or (
                current_wrapper is not None
                and (current_wrapper.st_dev, current_wrapper.st_ino) == wrapper_initial
                and stat.S_ISREG(current_wrapper.st_mode)
                and current_wrapper.st_uid == 0
                and current_wrapper.st_gid == 0
                and current_wrapper.st_nlink == 1
            )
            if not common_metadata_ok or not wrapper_metadata_ok:
                raise AssertionError("refusing to clean replaced synthetic tools")
            shutil.rmtree(tools_root)


def passwd_entry(
    name: str,
    uid: int,
    gid: int,
    *,
    home: str = "/nonexistent",
    shell: str = "/usr/sbin/nologin",
) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", uid, gid, "", home, shell))


def group_entry(
    name: str, gid: int, members: list[str] | None = None
) -> grp.struct_group:
    return grp.struct_group((name, "x", gid, members or []))


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, url: str, content_length: str | None = None):
        super().__init__(payload)
        self._url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class LiveQaGuardTests(unittest.TestCase):
    def make_bundle(self, root: Path) -> Path:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        marker = "liveqa-guard-unit"
        payload = {
            "version": 1,
            "marker": marker,
            "created_at": "2026-08-11T00:00:00Z",
            "email": "liveqa@example.invalid",
            "password": "not-used-by-guard-tests",
            "mailbox_helper": str(root / "mailbox-helper"),
            "roster_accounts": [
                {
                    "id": str(uuid4()),
                    "email": f"player-{index}@example.invalid",
                    "password": "not-used-by-guard-tests",
                }
                for index in range(13)
            ],
        }
        bundle = root / "bundle.json"
        bundle.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(bundle, 0o600)
        return bundle

    def make_stale_cleanup_bundle_fixture(
        self,
        root: Path,
        *,
        created_at: str = "2020-01-01T00:00:00Z",
        source_sha: str = "a" * 40,
    ) -> tuple[Path, Path, Path, tuple[str, ...], Path, Path, Path]:
        private = root / "private"
        private.mkdir(mode=0o700)
        install_root = root / "payload" / "releases" / source_sha
        payload_platform = install_root / "platform"
        payload_tools = payload_platform / "tools"
        payload_tools.mkdir(parents=True)
        helper_bytes = b"synthetic active mailbox helper\n"
        (payload_tools / "platform_live_qa_mailbox_helper.py").write_bytes(
            helper_bytes
        )
        os.chmod(payload_tools / "platform_live_qa_mailbox_helper.py", 0o444)
        helper = private / "platform_live_qa_mailbox_helper.py"
        helper.write_bytes(helper_bytes)
        os.chmod(helper, 0o500)
        os.chmod(payload_tools, 0o555)
        os.chmod(payload_platform, 0o555)
        os.chmod(install_root, 0o555)
        releases = install_root.parent
        os.chmod(releases, 0o755)
        payload_digest = hashlib.sha256()
        payload_files: dict[str, str] = {}
        for path in sorted(install_root.rglob("*")):
            relative = path.relative_to(install_root).as_posix()
            payload_digest.update(relative.encode("utf-8") + b"\0")
            if path.is_dir():
                payload_digest.update(b"d\0")
                continue
            file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            payload_files[relative] = file_digest
            payload_digest.update(b"f\0" + bytes.fromhex(file_digest))
        active_pointer = private / "active"
        active_pointer.symlink_to(install_root)
        manifest_path = private / "active-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_sha": source_sha,
                    "release_slug": "liveqa-stale-mode-fixture",
                    "payload": str(install_root),
                    "payload_tree_sha256": payload_digest.hexdigest(),
                    "files": payload_files,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
        os.chown(manifest_path, 0, 0)
        os.chmod(manifest_path, 0o444)
        app_dir = root / "app"
        app_releases = app_dir / "releases"
        app_release = app_releases / f"fixture-{source_sha[:8]}"
        app_release.mkdir(parents=True)
        (app_release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha}), encoding="ascii"
        )
        os.chown(app_release / "RELEASE.json", 0, 0)
        os.chmod(app_release / "RELEASE.json", 0o444)
        os.chown(app_release, 0, 0)
        os.chmod(app_release, 0o555)
        os.chown(app_releases, 0, 0)
        os.chmod(app_releases, 0o555)
        (app_dir / "current").symlink_to(app_release)
        marker = "liveqa-stale-guard-fixture"
        roster = [
            {
                "id": str(uuid4()),
                "email": f"player-{index:02d}-{marker}@auth.old-sparky.com",
                "password": "synthetic-roster-password",
            }
            for index in range(13)
        ]
        payload = {
            "version": 1,
            "marker": marker,
            "created_at": created_at,
            "email": "liveqa-operator@auth.old-sparky.com",
            "password": "synthetic-top-password",
            "mailbox_helper": str(helper),
            "roster_accounts": roster,
        }
        bundle = private / "csp-live-qa.json"
        bundle.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(bundle, 0o600)
        return (
            bundle,
            install_root,
            payload_platform,
            tuple(item["id"] for item in roster),
            private,
            releases,
            app_dir,
        )

    def make_zip(self, root: Path, entries: list[tuple[str, bytes]]) -> Path:
        source = root / "source.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
        return source

    def make_runtime_cache(
        self, root: Path, commit: str, *, modified_at_ns: int
    ) -> Path:
        cache = root / f"runtime-{commit}"
        cache.mkdir(mode=0o755)
        payload = cache / "payload"
        payload.write_bytes(b"reviewed runtime")
        manifest = cache / ".manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_commit": commit,
                    "tree_sha256": "1" * 64,
                    "node_archive_sha256": "2" * 64,
                    "package_lock_sha256": "3" * 64,
                    "playwright_browsers_sha256": "4" * 64,
                }
            ),
            encoding="ascii",
        )
        os.chmod(payload, 0o444)
        os.chmod(manifest, 0o444)
        os.chmod(cache, 0o555)
        os.utime(cache, ns=(modified_at_ns, modified_at_ns))
        return cache

    @contextmanager
    def legacy_runtime_cache_fixture(self):
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            cache_root = base / "cache"
            cache_root.mkdir(mode=0o755)
            os.chmod(cache_root, 0o755)
            target = cache_root / f"runtime-{guard.LEGACY_FALLBACK_RUNTIME_COMMIT}"
            target.mkdir(mode=0o755)
            platform_root = base / "platform"
            package_lock = platform_root / "apps/platform_web/package-lock.json"
            package_lock.parent.mkdir(parents=True)
            legacy_package_lock = b'{"name":"synthetic-4a-lock"}\n'
            package_lock.write_bytes(b'{"name":"synthetic-current-lock"}\n')
            browsers_payload = {
                "browsers": [
                    {"name": "chromium", "revision": "1228", "browserVersion": "149.0.7827.55"},
                    {"name": "chromium-headless-shell", "revision": "1228", "browserVersion": "149.0.7827.55"},
                    {"name": "webkit", "revision": "2311", "browserVersion": "26.5"},
                    {"name": "ffmpeg", "revision": "1011"},
                ]
            }
            browsers_raw = json.dumps(browsers_payload, separators=(",", ":")).encode()
            files = {
                "node/bin/node": b"synthetic pinned node\n",
                "web/node_modules/playwright-core/browsers.json": browsers_raw,
                "web/package-lock.json": legacy_package_lock,
                "browsers/chromium-1228/chrome-linux64/chrome": b"full chromium payload\n",
                "browsers/chromium-1228/chrome-linux64/resources.pak": b"unused full payload\n",
                "browsers/chromium_headless_shell-1228/headless_shell": b"headless shell\n",
                "browsers/webkit-2311/pw_run.sh": b"webkit\n",
                "browsers/ffmpeg-1011/ffmpeg-linux": b"ffmpeg\n",
            }
            sandbox = target / guard.CHROMIUM_SANDBOX_RELATIVE
            for relative, payload in files.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                if relative in {
                    "node/bin/node",
                    "browsers/chromium-1228/chrome-linux64/chrome",
                    "browsers/chromium_headless_shell-1228/headless_shell",
                    "browsers/webkit-2311/pw_run.sh",
                    "browsers/ffmpeg-1011/ffmpeg-linux",
                }:
                    os.chmod(path, 0o755)
            sandbox.parent.mkdir(parents=True, exist_ok=True)
            sandbox.write_bytes(platform_chromium_sandbox_fixture.read_bytes())
            for directory_name in (
                "chromium-1228",
                "chromium_headless_shell-1228",
                "webkit-2311",
                "ffmpeg-1011",
            ):
                complete = target / "browsers" / directory_name / "INSTALLATION_COMPLETE"
                complete.write_bytes(b"")
            guard._normalize_cache_tree(target, sandbox=sandbox)
            tree_sha256 = guard._tree_digest(target)
            manifest = {
                "version": 1,
                "source_commit": guard.LEGACY_FALLBACK_RUNTIME_COMMIT,
                "tree_sha256": tree_sha256,
                "node_archive_sha256": guard.NODE_ARCHIVE_SHA256,
                "package_lock_sha256": hashlib.sha256(legacy_package_lock).hexdigest(),
                "playwright_browsers_sha256": hashlib.sha256(browsers_raw).hexdigest(),
            }
            os.chmod(target, 0o755)
            guard._write_immutable_manifest(
                target / ".manifest.json", manifest, failure="fixture write failed"
            )
            os.chmod(target, 0o555)
            try:
                with (
                    mock.patch.object(
                        guard,
                        "LEGACY_FALLBACK_PACKAGE_LOCK_SHA256",
                        hashlib.sha256(legacy_package_lock).hexdigest(),
                    ),
                    mock.patch.object(
                        guard,
                        "LEGACY_FALLBACK_PLAYWRIGHT_BROWSERS_SHA256",
                        hashlib.sha256(browsers_raw).hexdigest(),
                    ),
                ):
                    yield platform_root, cache_root, target, manifest
            finally:
                for path in sorted(
                    (cache_root, *cache_root.rglob("*")),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    try:
                        metadata = path.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                        os.chmod(path, 0o755)
                    elif not stat.S_ISLNK(metadata.st_mode):
                        os.chmod(path, 0o644)
                if cache_root.exists():
                    os.chmod(cache_root, 0o755)

    def make_release_pointers(
        self, app_dir: Path, *, current: str, previous: str
    ) -> None:
        releases = app_dir / "releases"
        releases.mkdir(parents=True)
        (app_dir / "shared").mkdir()
        for pointer_name, commit in (("current", current), ("previous", previous)):
            release = releases / pointer_name
            release.mkdir()
            (release / "RELEASE.json").write_text(
                json.dumps({"source_git_commit": commit}), encoding="ascii"
            )
            (app_dir / pointer_name).symlink_to(release)

    def make_installed_payload_fixture(
        self, root: Path, *, file_count: int, typed_provider_links: bool = False
    ) -> tuple[Path, Path, Path, str]:
        """Create a valid active payload with a deterministically sized manifest."""

        trusted = root / "liveqa"
        releases = trusted / "releases"
        source_sha = "a" * 40
        payload = releases / source_sha
        trusted.mkdir(mode=0o700)
        releases.mkdir(mode=0o755)
        payload.mkdir(mode=0o555)
        files_root = payload / "manifest-files"
        files_root.mkdir(mode=0o555)
        os.chmod(trusted, 0o700)
        os.chmod(releases, 0o755)
        os.chmod(payload, 0o555)
        os.chmod(files_root, 0o555)

        for index in range(file_count):
            path = files_root / f"manifest-padding-{index:04d}-{'x' * 100}"
            path.write_bytes(b"x")
            os.chown(path, 0, 0)
            os.chmod(path, 0o444)
        if typed_provider_links:
            provider_root = payload / "provider-runtime"
            engine_root = provider_root / "browsers/webkit-2311/lib"
            engine_root.mkdir(mode=0o755, parents=True)
            terminal = engine_root / "libengine.so.1"
            terminal.write_bytes(b"engine")
            os.chown(terminal, 0, 0)
            os.chmod(terminal, 0o444)
            alias = engine_root / "libengine.so"
            alias.symlink_to("libengine.so.1")
            os.chown(alias, 0, 0, follow_symlinks=False)
            package_lock = provider_root / "web/package-lock.json"
            package_lock.parent.mkdir(mode=0o755, parents=True)
            package_lock.write_bytes(b"{}\n")
            os.chown(package_lock, 0, 0)
            os.chmod(package_lock, 0o444)
            provider = {
                "version": 2,
                "source_sha": source_sha,
                "provider_sha": source_sha,
                "engine_tree_sha256": "b" * 64,
                "node_version": guard.NODE_VERSION,
                "package_lock_sha256": hashlib.sha256(b"{}\n").hexdigest(),
                "engine_files": {
                    "browsers/webkit-2311/lib/libengine.so.1": hashlib.sha256(
                        b"engine"
                    ).hexdigest()
                },
                "engine_links": {
                    "browsers/webkit-2311/lib/libengine.so": "libengine.so.1"
                },
            }
            provider_path = payload / "runtime-provider.json"
            provider_path.write_text(
                json.dumps(provider, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            os.chown(provider_path, 0, 0)
            os.chmod(provider_path, 0o444)
            for directory in sorted(
                (path for path in payload.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                os.chown(directory, 0, 0)
                os.chmod(directory, 0o555)
        active = trusted / "active"
        active.symlink_to(payload)

        digest = hashlib.sha256()
        files: dict[str, str] = {}
        for path in sorted(payload.rglob("*")):
            relative = path.relative_to(payload).as_posix()
            metadata = path.lstat()
            digest.update(relative.encode("utf-8") + b"\0")
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(b"l\0" + os.readlink(path).encode("utf-8") + b"\0")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"d\0")
                continue
            file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files[relative] = file_digest
            digest.update(b"f\0" + bytes.fromhex(file_digest))
        manifest = {
            "version": 1,
            "source_sha": source_sha,
            "release_slug": "manifest-size-test",
            "payload": str(payload),
            "payload_tree_sha256": digest.hexdigest(),
            "files": files,
        }
        manifest_path = trusted / "active-manifest.json"
        manifest_path.write_bytes(
            (
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii")
        )
        os.chown(manifest_path, 0, 0)
        os.chmod(manifest_path, 0o444)
        return trusted, releases, payload, source_sha

    def extract_test_zip(
        self,
        source: Path,
        target: Path,
        *,
        retained_paths: frozenset[PurePosixPath] | None = None,
    ) -> None:
        def copy_archive(**kwargs: object) -> None:
            shutil.copyfile(source, Path(kwargs["archive"]))

        with mock.patch.object(guard, "_download_exact", side_effect=copy_archive):
            guard._download_pinned_zip(
                "https://downloads.example.invalid/browser.zip",
                hashlib.sha256(source.read_bytes()).hexdigest(),
                source.stat().st_size,
                target,
                retained_paths=retained_paths,
            )

    def test_lock_path_is_machine_wide_for_different_bundles(self) -> None:
        with mock.patch.object(guard, "_validate_root_secret_parent"):
            first = guard._lock_path(Path("/root/one/bundle.json"))
            second = guard._lock_path(Path("/root/two/bundle.json"))
        self.assertEqual(first, guard.MACHINE_LOCK_PATH)
        self.assertEqual(second, guard.MACHINE_LOCK_PATH)

    def test_machine_lock_refuses_a_second_concurrent_open(self) -> None:
        from tools import platform_storage_maintenance as maintenance

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            lock_path = Path(temporary) / "liveqa.lock"
            with (
                mock.patch.object(guard, "MACHINE_LOCK_PATH", lock_path),
                mock.patch.object(
                    maintenance, "LIVE_QA_MACHINE_LOCK_PATH", lock_path
                ),
                mock.patch.object(guard, "_validate_root_secret_parent"),
            ):
                first = guard._open_bundle_lock(Path("/root/one/bundle.json"))
                try:
                    opened = os.fstat(first)
                    path_metadata = lock_path.stat()
                    self.assertEqual(
                        (opened.st_dev, opened.st_ino),
                        (path_metadata.st_dev, path_metadata.st_ino),
                    )
                    with self.assertRaisesRegex(guard.GuardError, "another live QA"):
                        guard._open_bundle_lock(Path("/root/two/bundle.json"))
                    with self.assertRaisesRegex(guard.GuardError, "another live QA"):
                        with maintenance.live_qa_machine_lock():
                            self.fail("contended storage lock unexpectedly acquired")
                finally:
                    os.close(first)
                with maintenance.live_qa_machine_lock():
                    self.assertEqual(
                        (lock_path.stat().st_dev, lock_path.stat().st_ino),
                        (path_metadata.st_dev, path_metadata.st_ino),
                    )

    def test_locked_exec_rejects_non_trusted_wrapper(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "not a reviewed"):
            guard._validate_trusted_wrapper(
                ["/opt/oldsparky/platform/current/tools/platform_live_user_qa.sh"],
                recovery=False,
            )

    def test_engine_preflight_locked_exec_requires_exact_installed_wrapper(self) -> None:
        source_sha = "a" * 40
        install_root = f"/root/.oldsparky/liveqa/releases/{source_sha}"
        wrapper = f"{install_root}/platform/tools/platform_live_browser_qa.sh"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PLATFORM_LIVE_QA_INSTALL_ROOT": install_root,
                    "PLATFORM_LIVE_QA_TARGET_SHA": source_sha,
                },
                clear=True,
            ),
            mock.patch.object(
                guard,
                "_validate_installed_payload_root",
                return_value=source_sha,
            ),
            mock.patch.object(guard, "_assert_root_controlled_path"),
        ):
            guard._validate_trusted_wrapper(
                [wrapper, "engine-preflight"], recovery=False
            )
            with self.assertRaisesRegex(guard.GuardError, "one installed wrapper argument"):
                guard._validate_trusted_wrapper(
                    [wrapper, "engine-preflight", "extra"], recovery=False
                )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(guard, "_assert_root_controlled_path"),
        ):
            with self.assertRaisesRegex(guard.GuardError, "one installed wrapper argument"):
                guard._validate_trusted_wrapper(
                    ["/root/old_sparky/platform/tools/platform_live_browser_qa.sh", "engine-preflight"],
                    recovery=False,
                )

    def test_engine_preflight_child_command_is_exact_and_runtime_bound(self) -> None:
        source_sha = "a" * 40
        suffix = "a1b2c3d4"
        suite = (
            f"/var/lib/oldsparky-liveqa/runtime-suite-{source_sha}-{suffix}"
        )
        engine = "/var/lib/oldsparky-liveqa/runtime-engine-" + "b" * 40 + "-" + suffix
        command = [
            "/usr/bin/systemd-run",
            "--no-ask-password",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            "--expand-environment=no",
            "--unit=oldsparky-liveqa-browser.service",
            "--uid=1001",
            "--gid=1001",
            f"--working-directory={suite}/web",
            "--property=KillMode=control-group",
            "--property=Restart=no",
            "--property=RuntimeMaxSec=30min",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=5s",
            "--property=UMask=0077",
            f"--property=BindReadOnlyPaths={suite}:{suite}",
            f"--property=BindReadOnlyPaths={engine}:{engine}",
            "--",
            "/usr/bin/env",
            "-i",
            "HOME=/run/oldsparky-liveqa/public-live-qa.a1b2c3d4/home",
            "LANG=C.UTF-8",
            f"NODE_PATH={engine}/web/node_modules",
            f"PATH={engine}/node/bin:/usr/bin:/bin",
            "PLATFORM_LIVE_EXPECTED_ORIGIN=https://old-sparky.com",
            "PLATFORM_LIVE_USER_QA_UID=1001",
            "PLATFORM_QA_BROWSER_GATE_DIR=/run/oldsparky-liveqa/public-live-qa.a1b2c3d4",
            f"PLATFORM_LIVE_QA_RUNNER_SHA={source_sha}",
            f"PLATFORM_LIVE_QA_TARGET_SHA={source_sha}",
            f"PLATFORM_LIVE_QA_MARKER_SHA256={'c' * 64}",
            f"PLAYWRIGHT_BROWSERS_PATH={engine}/browsers",
            "PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com",
            "TMPDIR=/run/oldsparky-liveqa/public-live-qa.a1b2c3d4/tmp",
            "XDG_CACHE_HOME=/run/oldsparky-liveqa/public-live-qa.a1b2c3d4/home/.cache",
            f"{engine}/node/bin/node",
            f"{suite}/web/tests/support/live-engine-preflight.cjs",
        ]
        self.assertTrue(guard._is_fixed_live_browser_command(command))
        for invalid in (
            command + ["extra"],
            [
                (
                    f"{suite}/web/tests/support/../support/live-engine-preflight.cjs"
                    if item == f"{suite}/web/tests/support/live-engine-preflight.cjs"
                    else item
                )
                for item in command
            ],
            ["NODE_OPTIONS=--require=/tmp/untrusted" if item.startswith("NODE_PATH=") else item for item in command],
        ):
            self.assertFalse(guard._is_fixed_live_browser_command(invalid))

    def test_locked_exec_preserves_only_validated_payload_identity(self) -> None:
        source_sha = "a" * 40
        installed_root = f"/root/.oldsparky/liveqa/releases/{source_sha}"
        wrapper = (
            f"{installed_root}/platform/tools/platform_live_user_qa.sh"
        )
        descriptor = os.open(os.devnull, os.O_RDONLY)
        captured: dict[str, object] = {}

        def validate_payload(root: Path, *, target_sha: str | None = None) -> str:
            self.assertEqual(str(root), installed_root)
            self.assertEqual(target_sha, source_sha)
            return source_sha

        def capture_exec(
            _path: str, _argv: list[str], child_env: dict[str, str]
        ) -> None:
            captured.update(child_env)
            raise OSError("stop after inspecting locked child environment")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "PLATFORM_LIVE_QA_INSTALL_ROOT": installed_root,
                    "PLATFORM_LIVE_QA_TARGET_SHA": source_sha,
                    "UNTRUSTED_TEST_VALUE": "must-not-cross-lock-exec",
                },
                clear=True,
            ),
            mock.patch.object(
                guard,
                "_validate_installed_payload_root",
                side_effect=validate_payload,
            ),
            mock.patch.object(guard, "_assert_root_controlled_path"),
            mock.patch.object(guard, "_open_bundle_lock", return_value=descriptor),
            mock.patch.object(guard.os, "execve", side_effect=capture_exec),
            self.assertRaisesRegex(OSError, "stop after inspecting"),
        ):
            guard.locked_exec(Path("/root/csp-live-qa.json"), [wrapper])

        self.assertEqual(
            set(captured),
            {
                "LANG",
                "PATH",
                guard.LOCK_ENV_NAME,
                "PLATFORM_LIVE_QA_INSTALL_ROOT",
                "PLATFORM_LIVE_QA_TARGET_SHA",
            },
        )
        self.assertEqual(captured["PLATFORM_LIVE_QA_INSTALL_ROOT"], installed_root)
        self.assertEqual(captured["PLATFORM_LIVE_QA_TARGET_SHA"], source_sha)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_locked_exec_rejects_mismatched_target_before_lock_or_exec(self) -> None:
        source_sha = "a" * 40
        installed_root = f"/root/.oldsparky/liveqa/releases/{source_sha}"
        wrapper = f"{installed_root}/platform/tools/platform_live_user_qa.sh"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PLATFORM_LIVE_QA_INSTALL_ROOT": installed_root,
                    "PLATFORM_LIVE_QA_TARGET_SHA": "b" * 40,
                },
                clear=True,
            ),
            mock.patch.object(guard, "_open_bundle_lock") as open_lock,
            mock.patch.object(guard.os, "execve") as execve,
            self.assertRaisesRegex(guard.GuardError, "wrapper root is invalid"),
        ):
            guard.locked_exec(Path("/root/csp-live-qa.json"), [wrapper])
        open_lock.assert_not_called()
        execve.assert_not_called()

    def test_recovery_exec_requires_an_exact_recovery_command(self) -> None:
        with synthetic_trusted_tools() as tools_root:
            wrapper = tools_root / "platform_live_user_qa.sh"
            with mock.patch.object(guard, "TRUSTED_TOOLS_ROOT", tools_root):
                with self.assertRaisesRegex(
                    guard.GuardError, "arguments are invalid"
                ):
                    guard._validate_trusted_wrapper([str(wrapper)], recovery=True)
                guard._validate_trusted_wrapper(
                    [str(wrapper), "recover-bundle"], recovery=True
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "arguments are invalid"
                ):
                    guard._validate_trusted_wrapper(
                        [str(wrapper), "recover-bundle", "/tmp/roster.json"],
                        recovery=True,
                    )

    def test_checkout_provenance_rejects_any_other_checkout(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            platform_root = Path(temporary) / "platform"
            platform_root.mkdir()
            with self.assertRaisesRegex(guard.GuardError, "fixed root-controlled"):
                guard.verify_checkout_provenance(platform_root)

    def test_checkout_provenance_rejects_dirty_platform(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            repo = Path(temporary)
            platform_root = repo / "platform"
            tools_root = platform_root / "tools"
            tools_root.mkdir(parents=True)

            def fake_git(_repo: Path, *args: str) -> bytes:
                if args == ("rev-parse", "--show-toplevel"):
                    return f"{repo}\n".encode()
                if args == ("rev-parse", "--verify", "HEAD"):
                    return f"{commit}\n".encode()
                return b" M platform/tools/platform_live_qa_guard.py\n"

            with (
                mock.patch.object(guard, "TRUSTED_REPO_ROOT", repo),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "TRUSTED_TOOLS_ROOT", tools_root),
                mock.patch.object(guard, "_git", side_effect=fake_git),
            ):
                with self.assertRaisesRegex(guard.GuardError, "tracked or untracked"):
                    guard.verify_checkout_provenance(platform_root)

    def test_helper_binding_rejects_a_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            platform_root = root / "checkout" / "platform"
            tools = platform_root / "tools"
            tools.mkdir(parents=True)
            source = tools / "platform_live_qa_mailbox_helper.py"
            source_bytes = b"reviewed"
            source.write_bytes(source_bytes)
            os.chmod(source, 0o755)
            installed_parent = root / "private"
            installed_parent.mkdir(mode=0o700)
            installed = installed_parent / "helper"
            installed.write_bytes(b"different")
            os.chmod(installed, 0o500)
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", installed_parent),
                self.assertRaisesRegex(guard.GuardError, "does not match"),
            ):
                guard.verify_helper_binding(
                    platform_root=platform_root,
                    installed_helper=installed,
                )

            installed.write_bytes(source_bytes)
            with mock.patch.object(guard, "TRUSTED_SECRET_ROOT", installed_parent):
                guard.verify_helper_binding(
                    platform_root=platform_root,
                    installed_helper=installed,
                )
            os.chmod(source, 0o444)
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", installed_parent),
                self.assertRaisesRegex(guard.GuardError, "helper file metadata"),
            ):
                guard.verify_helper_binding(
                    platform_root=platform_root,
                    installed_helper=installed,
                )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned secret path contract")
    def test_secret_path_rejects_a_writable_ancestor(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            ancestor = Path(temporary)
            secret_root = ancestor / "liveqa"
            secret_root.mkdir(mode=0o700)
            os.chmod(ancestor, 0o777)
            try:
                with (
                    mock.patch.object(guard, "TRUSTED_SECRET_ROOT", secret_root),
                    self.assertRaisesRegex(guard.GuardError, "chain is unsafe"),
                ):
                    guard._validate_root_secret_parent(secret_root / "bundle.json")
            finally:
                os.chmod(ancestor, 0o700)

    def test_liveqa_identity_rejects_a_service_uid_collision(self) -> None:
        entries = {
            guard.LIVE_QA_USER: passwd_entry(guard.LIVE_QA_USER, 995, 990),
            "oldsparky": passwd_entry("oldsparky", 999, 1000),
            "oldsparky-platform": passwd_entry("oldsparky-platform", 996, 988),
            "oldsparky-api": passwd_entry("oldsparky-api", 995, 987),
        }
        groups = {
            guard.LIVE_QA_USER: group_entry(guard.LIVE_QA_USER, 990),
            "oldsparky": group_entry("oldsparky", 1000),
            "oldsparky-platform": group_entry("oldsparky-platform", 988),
            "oldsparky-api": group_entry("oldsparky-api", 987),
        }

        def get_passwd(name: str) -> pwd.struct_passwd:
            if name not in entries:
                raise KeyError(name)
            return entries[name]

        def get_group(name: str) -> grp.struct_group:
            if name not in groups:
                raise KeyError(name)
            return groups[name]

        with (
            mock.patch.object(pwd, "getpwnam", side_effect=get_passwd),
            mock.patch.object(pwd, "getpwall", return_value=list(entries.values())),
            mock.patch.object(grp, "getgrnam", side_effect=get_group),
            mock.patch.object(grp, "getgrall", return_value=list(groups.values())),
        ):
            with self.assertRaisesRegex(guard.GuardError, "unsafe identity"):
                guard.liveqa_identity()

    def test_liveqa_identity_accepts_absent_legacy_service_identity(self) -> None:
        entries = {
            guard.LIVE_QA_USER: passwd_entry(guard.LIVE_QA_USER, 995, 990),
            "oldsparky-platform": passwd_entry("oldsparky-platform", 996, 988),
        }
        groups = {
            guard.LIVE_QA_USER: group_entry(guard.LIVE_QA_USER, 990),
            "oldsparky-platform": group_entry("oldsparky-platform", 988),
        }

        def get_passwd(name: str) -> pwd.struct_passwd:
            if name not in entries:
                raise KeyError(name)
            return entries[name]

        def get_group(name: str) -> grp.struct_group:
            if name not in groups:
                raise KeyError(name)
            return groups[name]

        with (
            mock.patch.object(pwd, "getpwnam", side_effect=get_passwd),
            mock.patch.object(pwd, "getpwall", return_value=list(entries.values())),
            mock.patch.object(grp, "getgrnam", side_effect=get_group),
            mock.patch.object(grp, "getgrall", return_value=list(groups.values())),
        ):
            self.assertEqual(guard.liveqa_identity(), (995, 990))

    def test_liveqa_identity_requires_platform_service_identity(self) -> None:
        entries = {
            guard.LIVE_QA_USER: passwd_entry(guard.LIVE_QA_USER, 995, 990),
            "oldsparky": passwd_entry("oldsparky", 999, 1000),
        }
        groups = {
            guard.LIVE_QA_USER: group_entry(guard.LIVE_QA_USER, 990),
            "oldsparky": group_entry("oldsparky", 1000),
        }

        def get_passwd(name: str) -> pwd.struct_passwd:
            if name not in entries:
                raise KeyError(name)
            return entries[name]

        def get_group(name: str) -> grp.struct_group:
            if name not in groups:
                raise KeyError(name)
            return groups[name]

        with (
            mock.patch.object(pwd, "getpwnam", side_effect=get_passwd),
            mock.patch.object(pwd, "getpwall", return_value=list(entries.values())),
            mock.patch.object(grp, "getgrnam", side_effect=get_group),
            mock.patch.object(grp, "getgrall", return_value=list(groups.values())),
            self.assertRaisesRegex(
                guard.GuardError, "production identity boundary is unavailable"
            ),
        ):
            guard.liveqa_identity()

    def test_cgroup_membership_blocks_path_reclaim(self) -> None:
        with (
            mock.patch.object(guard, "_liveqa_cgroup_process_ids", return_value=(123,)),
            mock.patch.object(guard, "_liveqa_process_ids", return_value=()),
        ):
            with self.assertRaisesRegex(guard.GuardError, "cgroup or identity"):
                guard.assert_liveqa_idle()

    def test_recovery_kills_cgroup_then_uid_before_reset(self) -> None:
        calls: list[str] = []
        with (
            mock.patch.object(
                guard, "_kill_liveqa_cgroup", side_effect=lambda: calls.append("cgroup")
            ),
            mock.patch.object(
                guard,
                "_terminate_liveqa_processes",
                side_effect=lambda: calls.append("uid"),
            ),
            mock.patch.object(guard, "_liveqa_cgroup_process_ids", return_value=()),
            mock.patch.object(
                guard,
                "_reset_liveqa_systemd_unit",
                side_effect=lambda: calls.append("reset"),
            ),
        ):
            guard._recover_stale_liveqa()
        self.assertEqual(calls, ["cgroup", "uid", "reset"])

    def test_recovery_lock_contention_never_terminates_an_active_runner(self) -> None:
        with (
            mock.patch.object(guard, "_validate_trusted_wrapper"),
            mock.patch.object(guard, "_validate_root_secret_parent"),
            mock.patch.object(
                guard,
                "_open_bundle_lock",
                side_effect=guard.GuardError(
                    "another live QA operation holds the lock"
                ),
            ),
            mock.patch.object(guard, "_recover_stale_liveqa") as recover,
        ):
            with self.assertRaisesRegex(guard.GuardError, "holds the lock"):
                guard.recovery_locked_exec(
                    Path("/root/.oldsparky/liveqa/bundle.json"), ["wrapper"]
                )
        recover.assert_not_called()

        with (
            mock.patch.object(guard, "_validate_trusted_wrapper"),
            mock.patch.object(
                guard,
                "_assert_release_lock_supervisor",
                side_effect=guard.GuardError("canonical release lock is missing"),
            ) as release_lock,
            mock.patch.object(guard, "_open_bundle_lock") as open_machine_lock,
            mock.patch.object(guard, "_recover_stale_liveqa") as recover_stale,
            self.assertRaisesRegex(guard.GuardError, "canonical release lock"),
        ):
            guard.recovery_locked_exec(
                Path("/root/.oldsparky/liveqa/csp-live-qa.json"),
                [
                    "/root/.oldsparky/liveqa/releases/"
                    + "a" * 40
                    + "/platform/tools/platform_live_user_qa.sh",
                    "recover-bundle",
                ],
            )
        release_lock.assert_called_once_with()
        open_machine_lock.assert_not_called()
        recover_stale.assert_not_called()

    def test_uid_recovery_escalates_to_sigkill(self) -> None:
        with (
            mock.patch.object(
                guard, "_liveqa_process_ids", side_effect=[(321,), (321,), ()]
            ),
            mock.patch.object(guard, "_liveqa_cgroup_process_ids", return_value=()),
            mock.patch.object(os, "kill") as kill,
        ):
            guard._terminate_liveqa_processes(timeout=0)
        self.assertEqual(
            kill.call_args_list,
            [mock.call(321, signal.SIGTERM), mock.call(321, signal.SIGKILL)],
        )

    def test_zip_extraction_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_zip(root, [("../escape", b"owned")])
            with self.assertRaisesRegex(guard.GuardError, "layout is unsafe"):
                self.extract_test_zip(source, root / "browser")
            self.assertFalse((root / "escape").exists())

            helper = platform_chromium_sandbox_fixture.read_bytes()
            source = self.make_zip(
                root,
                [
                    ("chrome-linux64/", b""),
                    ("chrome-linux64/chrome", b"full chromium executable"),
                    ("chrome-linux64/resources.pak", b"unused full browser data"),
                    ("chrome-linux64/chrome_sandbox", helper),
                ],
            )
            target = root / "chromium-1228"
            self.extract_test_zip(
                source,
                target,
                retained_paths=guard.CHROMIUM_ARCHIVE_RETAINED_PATHS,
            )
            sandbox = target / "chrome-linux64/chrome_sandbox"
            self.assertEqual(sandbox.read_bytes(), helper)
            self.assertEqual(sandbox.stat().st_size, guard.CHROMIUM_SANDBOX_SIZE)
            self.assertEqual(
                hashlib.sha256(sandbox.read_bytes()).hexdigest(),
                guard.CHROMIUM_SANDBOX_SHA256,
            )
            self.assertFalse((target / "chrome-linux64/chrome").exists())
            self.assertFalse((target / "chrome-linux64/resources.pak").exists())
            self.assertTrue((target / "INSTALLATION_COMPLETE").is_file())
            self.assertEqual(
                {path.relative_to(target).as_posix() for path in target.rglob("*")},
                {
                    "chrome-linux64",
                    "chrome-linux64/chrome_sandbox",
                    "INSTALLATION_COMPLETE",
                },
            )

            source = self.make_zip(root, [("chrome-linux64/chrome", b"binary")])
            with self.assertRaisesRegex(guard.GuardError, "retained member"):
                self.extract_test_zip(
                    source,
                    root / "missing",
                    retained_paths=guard.CHROMIUM_ARCHIVE_RETAINED_PATHS,
                )

            linked = root / "linked-source.zip"
            info = zipfile.ZipInfo("chrome-linux64/chrome_sandbox")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(linked, "w") as archive:
                archive.writestr(info, "../../escape")
            with self.assertRaisesRegex(guard.GuardError, "escapes"):
                self.extract_test_zip(
                    linked,
                    root / "linked",
                    retained_paths=guard.CHROMIUM_ARCHIVE_RETAINED_PATHS,
                )

    def test_zip_extraction_rejects_uncompressed_size_over_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_zip(root, [("browser/file", b"1234")])
            with (
                mock.patch.object(guard, "MAX_ZIP_UNCOMPRESSED_BYTES", 3),
                self.assertRaisesRegex(guard.GuardError, "extraction limit"),
            ):
                self.extract_test_zip(source, root / "browser")

    def test_download_rejects_checksum_mismatch_and_removes_archive(self) -> None:
        url = "https://downloads.example.invalid/archive.zip"
        payload = b"pinned bytes"
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive.zip"
            response = FakeResponse(payload, url=url, content_length=str(len(payload)))
            with (
                mock.patch.object(
                    guard.urllib.request, "urlopen", return_value=response
                ),
                self.assertRaisesRegex(guard.GuardError, "checksum mismatch"),
            ):
                guard._download_exact(
                    url=url,
                    expected_sha256="0" * 64,
                    expected_size=len(payload),
                    archive=archive,
                    label="test archive",
                )
            self.assertFalse(archive.exists())

    def test_download_rejects_bytes_beyond_exact_bound(self) -> None:
        url = "https://downloads.example.invalid/archive.zip"
        payload = b"12345"
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive.zip"
            response = FakeResponse(payload, url=url)
            with (
                mock.patch.object(
                    guard.urllib.request, "urlopen", return_value=response
                ),
                self.assertRaisesRegex(guard.GuardError, "exceeds its pinned size"),
            ):
                guard._download_exact(
                    url=url,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size=4,
                    archive=archive,
                    label="test archive",
                )
            self.assertFalse(archive.exists())

    def test_immutable_manifest_is_readable_under_root_private_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / ".manifest.json"
            previous_umask = os.umask(0o077)
            try:
                guard._write_immutable_manifest(
                    manifest,
                    {"version": 1, "tree_sha256": "a" * 64},
                    failure="test manifest write failed",
                )
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o444)
            self.assertEqual(
                json.loads(manifest.read_text(encoding="ascii")),
                {"version": 1, "tree_sha256": "a" * 64},
            )

    def test_active_manifest_between_legacy_and_active_bounds_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, releases, payload, source_sha = self.make_installed_payload_fixture(
                Path(temporary), file_count=430
            )
            manifest = trusted / "active-manifest.json"
            self.assertGreater(manifest.stat().st_size, guard.MAX_JSON_BYTES)
            self.assertLess(manifest.stat().st_size, 100 * 1024)
            self.assertLessEqual(
                manifest.stat().st_size, guard.MAX_ACTIVE_MANIFEST_BYTES
            )
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", trusted),
                mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", releases),
                mock.patch.object(guard, "TRUSTED_ACTIVE_MANIFEST", manifest),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", trusted / "active"),
                mock.patch.object(
                    guard, "_active_release_commit", return_value=source_sha
                ),
            ):
                self.assertEqual(
                    guard._validate_installed_payload_root(
                        payload, target_sha=source_sha
                    ),
                    source_sha,
                )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned payload contract")
    def test_active_payload_accepts_only_provider_declared_browser_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, releases, payload, source_sha = self.make_installed_payload_fixture(
                Path(temporary), file_count=1, typed_provider_links=True
            )
            manifest = trusted / "active-manifest.json"
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", trusted),
                mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", releases),
                mock.patch.object(guard, "TRUSTED_ACTIVE_MANIFEST", manifest),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", trusted / "active"),
                mock.patch.object(
                    guard, "_active_release_commit", return_value=source_sha
                ),
            ):
                self.assertEqual(
                    guard._validate_installed_payload_root(payload, target_sha=source_sha),
                    source_sha,
                )
                alias = payload / "provider-runtime/browsers/webkit-2311/lib/libengine.so"
                alias.unlink()
                alias.symlink_to("../../../../outside")
                with self.assertRaisesRegex(
                    guard.GuardError, "installed live-QA payload contains an undeclared symlink"
                ):
                    guard._validate_installed_payload_root(payload, target_sha=source_sha)

    def test_active_manifest_over_active_bound_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, releases, payload, source_sha = self.make_installed_payload_fixture(
                Path(temporary), file_count=1800
            )
            manifest = trusted / "active-manifest.json"
            self.assertGreater(manifest.stat().st_size, guard.MAX_ACTIVE_MANIFEST_BYTES)
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", trusted),
                mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", releases),
                mock.patch.object(guard, "TRUSTED_ACTIVE_MANIFEST", manifest),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", trusted / "active"),
                mock.patch.object(
                    guard, "_active_release_commit", return_value=source_sha
                ),
                self.assertRaisesRegex(
                    guard.GuardError, "installed live-QA payload manifest is unavailable"
                ),
            ):
                guard._validate_installed_payload_root(payload, target_sha=source_sha)

    def test_chromium_apparmor_contract_requires_global_restriction_and_profiles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            restrict = root / "restrict"
            clone = root / "clone"
            profiles = root / "profiles"
            restrict.write_text("1\n", encoding="ascii")
            clone.write_text("1\n", encoding="ascii")
            profiles.write_text(
                "unrelated (enforce)\n"
                + "\n".join(guard.LIVE_QA_APPARMOR_PROFILES)
                + "\n",
                encoding="ascii",
            )
            with (
                mock.patch.object(guard, "APPARMOR_RESTRICT_USERNS", restrict),
                mock.patch.object(guard, "UNPRIVILEGED_USERNS_CLONE", clone),
                mock.patch.object(guard, "APPARMOR_PROFILES", profiles),
            ):
                guard.validate_chromium_apparmor_contract()

    def test_chromium_apparmor_contract_refuses_disabled_global_restriction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            restrict = root / "restrict"
            clone = root / "clone"
            profiles = root / "profiles"
            restrict.write_text("0\n", encoding="ascii")
            clone.write_text("1\n", encoding="ascii")
            profiles.write_text(
                "\n".join(guard.LIVE_QA_APPARMOR_PROFILES) + "\n",
                encoding="ascii",
            )
            with (
                mock.patch.object(guard, "APPARMOR_RESTRICT_USERNS", restrict),
                mock.patch.object(guard, "UNPRIVILEGED_USERNS_CLONE", clone),
                mock.patch.object(guard, "APPARMOR_PROFILES", profiles),
                self.assertRaisesRegex(guard.GuardError, "must remain enabled"),
            ):
                guard.validate_chromium_apparmor_contract()

    def test_chromium_apparmor_contract_refuses_a_missing_narrow_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            restrict = root / "restrict"
            clone = root / "clone"
            profiles = root / "profiles"
            restrict.write_text("1\n", encoding="ascii")
            clone.write_text("1\n", encoding="ascii")
            profiles.write_text(
                f"{guard.LIVE_QA_APPARMOR_PROFILES[0]}\n", encoding="ascii"
            )
            with (
                mock.patch.object(guard, "APPARMOR_RESTRICT_USERNS", restrict),
                mock.patch.object(guard, "UNPRIVILEGED_USERNS_CLONE", clone),
                mock.patch.object(guard, "APPARMOR_PROFILES", profiles),
                self.assertRaisesRegex(guard.GuardError, "profiles are not active"),
            ):
                guard.validate_chromium_apparmor_contract()

    @unittest.skipUnless(os.geteuid() == 0, "root-owned state contract")
    def test_root_state_has_durable_phase_and_exact_modes(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            bundle = self.make_bundle(Path(temporary) / "private")
            with mock.patch.object(guard, "TRUSTED_SECRET_ROOT", bundle.parent):
                state = guard.prepare_root_state(bundle)
                try:
                    marker, inventory, sessions = guard.validate_root_state(
                        bundle, state
                    )
                    self.assertEqual(marker, "liveqa-guard-unit")
                    self.assertIsNone(sessions)
                    phase = json.loads((state / guard.STATE_PHASE_FILE).read_text())
                    self.assertEqual(phase["phase"], guard.STATE_PHASE_ROOT)
                    self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE(inventory.stat().st_mode), 0o600)
                    self.assertEqual(
                        stat.S_IMODE((state / guard.STATE_PHASE_FILE).stat().st_mode),
                        0o600,
                    )
                finally:
                    guard.remove_root_state(bundle, state)

        with self.subTest("post-rename validation failure reports retained state"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                bundle = self.make_bundle(Path(temporary) / "private")
                original_failure = guard.GuardError("injected state validation failure")
                error_output = io.StringIO()
                with (
                    mock.patch.object(
                        guard,
                        "validate_root_state",
                        side_effect=original_failure,
                    ),
                    mock.patch.object(guard, "TRUSTED_SECRET_ROOT", bundle.parent),
                    mock.patch.object(guard.sys, "stderr", error_output),
                    self.assertRaises(guard.GuardError) as raised,
                ):
                    guard.prepare_root_state(bundle)
                self.assertIs(raised.exception, original_failure)
                self.assertEqual(len(list(bundle.parent.glob("live-user-qa.*"))), 1)
                reported = error_output.getvalue().strip().split(" at: ", 1)
                self.assertEqual(len(reported), 2)
                retained_state = Path(reported[1])
                self.assertEqual(retained_state.parent, bundle.parent)
                self.assertTrue(retained_state.is_dir())
                self.assertFalse(retained_state.is_symlink())
                self.assertTrue((retained_state / "inventory.json").is_file())

        source_sha = "a" * 40
        for scenario in (
            "success",
            "wrong_source",
            "fresh_bundle",
            "future_bundle",
            "malformed_timestamp",
            "wrong_helper",
            "unsafe_source_mode",
            "wrong_external_digest",
            "unsafe_external_mode",
            "symlink_bundle",
            "other_state",
            "setup_state",
            "run_gate",
            "lock",
        ):
            with self.subTest(stale_bundle_scenario=scenario):
                with tempfile.TemporaryDirectory(dir="/root") as temporary:
                    root = Path(temporary)
                    created_at = (
                        datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                        if scenario == "fresh_bundle"
                        else (
                            (datetime.now(timezone.utc) + timedelta(minutes=2))
                            .replace(microsecond=0)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                            if scenario == "future_bundle"
                            else (
                                "2020-01-01T00:00:00+00:00"
                                if scenario == "malformed_timestamp"
                                else "2020-01-01T00:00:00Z"
                            )
                        )
                    )
                    target_sha = "b" * 40 if scenario == "wrong_source" else source_sha
                    (
                        bundle,
                        install_root,
                        payload_platform,
                        expected_ids,
                        secret_root,
                        payload_releases,
                        app_dir,
                    ) = (
                        self.make_stale_cleanup_bundle_fixture(
                            root, created_at=created_at, source_sha=target_sha
                        )
                    )
                    gate_root = root / "run-gates"
                    env = {
                        "PLATFORM_LIVE_QA_INSTALL_ROOT": str(install_root),
                        "PLATFORM_LIVE_QA_TARGET_SHA": target_sha,
                        "PLATFORM_APP_DIR": str(app_dir),
                        "PLATFORM_LIVE_CSP_QA_BUNDLE": str(bundle),
                    }
                    if scenario == "wrong_helper":
                        payload = json.loads(bundle.read_text(encoding="utf-8"))
                        payload["mailbox_helper"] = str(secret_root / "other-helper")
                        bundle.write_text(json.dumps(payload), encoding="utf-8")
                        os.chmod(bundle, 0o600)
                    elif scenario == "unsafe_source_mode":
                        os.chmod(
                            payload_platform
                            / "tools/platform_live_qa_mailbox_helper.py",
                            0o644,
                        )
                    elif scenario == "wrong_external_digest":
                        external_helper = secret_root / "platform_live_qa_mailbox_helper.py"
                        external_helper.chmod(0o600)
                        external_helper.write_text("different helper\n", encoding="ascii")
                        external_helper.chmod(0o500)
                    elif scenario == "unsafe_external_mode":
                        os.chmod(
                            secret_root / "platform_live_qa_mailbox_helper.py",
                            0o444,
                        )
                    if scenario == "symlink_bundle":
                        real_bundle = secret_root / "bundle-data.json"
                        bundle.rename(real_bundle)
                        bundle.symlink_to(real_bundle)
                    if scenario == "other_state":
                        (secret_root / "live-user-qa.unexpected").mkdir(mode=0o700)
                    if scenario == "setup_state":
                        (secret_root / f".live-user-qa.setup-{'c' * 32}").mkdir(
                            mode=0o700
                        )
                    if scenario == "run_gate":
                        gate_root.mkdir(mode=0o711)
                        os.chmod(gate_root, 0o711)
                        (gate_root / "live-user-qa.unexpected").mkdir(mode=0o700)
                    if scenario == "lock":
                        lock_context = self.assertRaisesRegex(
                            guard.GuardError, "canonical release lock"
                        )
                    elif scenario in {
                        "wrong_source",
                        "wrong_helper",
                        "unsafe_source_mode",
                        "wrong_external_digest",
                        "unsafe_external_mode",
                        "symlink_bundle",
                        "other_state",
                        "setup_state",
                        "run_gate",
                        "fresh_bundle",
                        "future_bundle",
                        "malformed_timestamp",
                    }:
                        lock_context = self.assertRaises(guard.GuardError)
                    else:
                        lock_context = self.subTest("prepared exact stale inventory")
                    installed_payload_validation = (
                        mock.patch.object(
                            guard,
                            "_validate_installed_payload_root",
                            wraps=guard._validate_installed_payload_root,
                        )
                        if scenario == "success"
                        else mock.patch.object(
                            guard,
                            "_validate_installed_payload_root",
                            side_effect=(
                                guard.GuardError("wrong active source")
                                if scenario == "wrong_source"
                                else lambda *_args, **_kwargs: target_sha
                            ),
                        )
                    )
                    source_validation = (
                        installed_payload_validation,
                        mock.patch.object(
                            guard,
                            "_active_release_commit",
                            side_effect=lambda: guard._release_pointer_commit(
                                "current", app_dir
                            ),
                        ),
                        mock.patch.object(guard, "APP_DIR", app_dir),
                        mock.patch.object(guard, "_assert_release_lock_supervisor"),
                        mock.patch.object(guard, "assert_bundle_lock"),
                        mock.patch.object(guard, "assert_liveqa_idle"),
                        mock.patch.object(guard, "TRUSTED_SECRET_ROOT", secret_root),
                        mock.patch.object(
                            guard, "TRUSTED_PAYLOAD_ROOT", payload_releases
                        ),
                        mock.patch.object(
                            guard,
                            "TRUSTED_ACTIVE_MANIFEST",
                            secret_root / "active-manifest.json",
                        ),
                        mock.patch.object(
                            guard,
                            "TRUSTED_ACTIVE_POINTER",
                            secret_root / "active",
                        ),
                        mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                        mock.patch.dict(os.environ, env, clear=True),
                    )
                    with ExitStack() as stack:
                        for manager in source_validation:
                            stack.enter_context(manager)
                        stale_cleanup = mock.Mock(
                            side_effect=RuntimeError("stop before wrapper exec")
                        )
                        machine_lock_fd = os.open(os.devnull, os.O_RDONLY)
                        with (
                            mock.patch.object(guard, "_validate_trusted_wrapper"),
                            mock.patch.object(guard, "_open_bundle_lock", return_value=machine_lock_fd),
                            mock.patch.object(guard, "_recover_stale_liveqa", stale_cleanup),
                        ):
                            argv = [
                                str(install_root / "platform/tools/platform_live_user_qa.sh"),
                                "recover-bundle",
                            ]
                            if scenario == "success":
                                with self.assertRaisesRegex(
                                    RuntimeError, "stop before wrapper exec"
                                ):
                                    guard.recovery_locked_exec(bundle, argv)
                                stale_cleanup.assert_called_once_with()
                            elif scenario == "lock":
                                # The first release-lock proof fails before opening
                                # the machine lock or reaching stale-runner cleanup.
                                with mock.patch.object(
                                    guard,
                                    "_assert_release_lock_supervisor",
                                    side_effect=guard.GuardError(
                                        "canonical release lock missing"
                                    ),
                                ):
                                    with self.assertRaisesRegex(
                                        guard.GuardError, "canonical release lock"
                                    ):
                                        guard.recovery_locked_exec(bundle, argv)
                                stale_cleanup.assert_not_called()
                            else:
                                with self.assertRaises(guard.GuardError):
                                    guard.recovery_locked_exec(bundle, argv)
                                stale_cleanup.assert_not_called()
                        if scenario == "success":
                            state = guard.prepare_stale_bundle_cleanup_state(
                                bundle,
                                platform_root=payload_platform,
                                target_sha=source_sha,
                                lock_fd=42,
                            )
                            try:
                                _marker, inventory_path, sessions_path = guard.validate_root_state(
                                    bundle, state
                                )
                                self.assertIsNone(sessions_path)
                                inventory = guard._read_private_json(inventory_path)
                                self.assertEqual(inventory["user_ids"], list(expected_ids))
                                self.assertEqual(inventory["tournament_ids"], [])
                                self.assertEqual(inventory["media_ids"], [])
                            finally:
                                guard.remove_root_state(bundle, state)
                        else:
                            if scenario == "lock":
                                stack.enter_context(
                                    mock.patch.object(
                                        guard,
                                        "_assert_release_lock_supervisor",
                                        side_effect=guard.GuardError(
                                            "canonical release lock missing"
                                        ),
                                    )
                                )
                            with lock_context:
                                guard.prepare_stale_bundle_cleanup_state(
                                    bundle,
                                    platform_root=payload_platform,
                                    target_sha=target_sha,
                                    lock_fd=42,
                                )
                            generated = [
                                path
                                for path in secret_root.iterdir()
                                if path.name.startswith("live-user-qa.")
                                or path.name.startswith(".live-user-qa.setup-")
                            ]
                            if scenario == "other_state":
                                self.assertEqual(
                                    [path.name for path in generated],
                                    ["live-user-qa.unexpected"],
                                )
                            elif scenario == "setup_state":
                                self.assertEqual(
                                    [path.name for path in generated],
                                    [f".live-user-qa.setup-{'c' * 32}"],
                                )
                            else:
                                self.assertEqual(generated, [])

    @unittest.skipUnless(os.geteuid() == 0, "root-owned state contract")
    def test_setup_recovery_accepts_only_an_exact_empty_stage(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            parent = Path(temporary) / "private"
            bundle = self.make_bundle(parent)
            setup = parent / f".live-user-qa.setup-{'a' * 32}"
            setup.mkdir(mode=0o700)
            with mock.patch.object(guard, "TRUSTED_SECRET_ROOT", parent):
                guard.remove_setup_state(bundle, setup)
            self.assertFalse(setup.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-owned state contract")
    def test_published_browser_phase_survives_a_prior_root_reclaim(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            private = root / "private"
            bundle = self.make_bundle(private)
            gate_root = root / "run-gates"
            gate_root.mkdir(mode=0o711)
            with (
                mock.patch.object(guard, "TRUSTED_SECRET_ROOT", private),
                mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
                mock.patch.object(
                    guard, "liveqa_identity", return_value=(12345, 12345)
                ),
            ):
                state = guard.prepare_root_state(bundle)
                marker, inventory, _sessions = guard.validate_root_state(bundle, state)
                root_payload = guard._read_private_json(inventory)
                tournament_id = str(uuid4())
                candidate = dict(root_payload)
                candidate["tournament_ids"] = [tournament_id]
                gate = gate_root / state.name
                gate.mkdir(mode=0o700)
                guard._write_new_private_json(gate / "inventory.json", candidate)
                guard._publish_private_json(
                    state / guard.STATE_PHASE_FILE,
                    guard._phase_payload(
                        marker=marker,
                        phase=guard.STATE_PHASE_BROWSER_PUBLISHED,
                    ),
                )

                # A first recovery may reclaim the gate and then be interrupted.
                guard.reclaim_browser_gate(state)
                guard.merge_browser_inventory(bundle, state)
                guard.merge_browser_inventory(bundle, state)

                merged = guard._read_private_json(inventory)
                self.assertEqual(merged["tournament_ids"], [tournament_id])
                guard.remove_browser_gate(state)
                guard.remove_root_state(bundle, state)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_cache_rejects_any_noncanonical_setid_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            cache = Path(temporary) / "runtime"
            cache.mkdir(mode=0o555)
            payload = cache / "unexpected"
            payload.write_bytes(b"setid")
            os.chmod(payload, 0o4755)
            os.chmod(cache, 0o555)
            with self.assertRaisesRegex(guard.GuardError, "unexpected set-id"):
                guard._validate_cache_tree_permissions(cache)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_cache_rejects_a_cross_device_child_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            cache = Path(temporary) / "runtime"
            cache.mkdir(mode=0o755)
            payload = cache / "payload"
            payload.write_bytes(b"different device")
            os.chmod(payload, 0o444)
            os.chmod(cache, 0o555)
            original_lstat = Path.lstat

            def cross_device_lstat(path: Path) -> os.stat_result:
                metadata = original_lstat(path)
                if path == payload:
                    fields = list(metadata)
                    fields[2] = metadata.st_dev + 1
                    return os.stat_result(fields)
                return metadata

            with (
                mock.patch.object(
                    Path,
                    "lstat",
                    autospec=True,
                    side_effect=cross_device_lstat,
                ),
                self.assertRaisesRegex(guard.GuardError, "ownership or device"),
            ):
                guard._validate_cache_tree_permissions(cache)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache transaction contract")
    def test_legacy_fallback_compaction_is_reversible_and_resumable(self) -> None:
        self.assertEqual(
            guard.LEGACY_FALLBACK_PACKAGE_LOCK_SHA256,
            "bbfe1a66cc39665cffac0b59672877716f53785b92dbb09ff921a2627300f92f",
        )
        self.assertEqual(
            guard.LEGACY_FALLBACK_NODE_ARCHIVE_SHA256,
            "55647180e4ae58ffeaa3294e89aa4abda7c371dfbd64b44cbdb022980177aae0",
        )
        self.assertEqual(
            guard.LEGACY_FALLBACK_PLAYWRIGHT_BROWSERS_SHA256,
            "ee39bc924bc3d1bd895626c2910f1292d109bbfeeb5abd113acb45e1951cc942",
        )
        self.assertEqual(guard.LEGACY_FALLBACK_CHROMIUM_SANDBOX_SIZE, 15232)
        self.assertEqual(
            guard.LEGACY_FALLBACK_CHROMIUM_SANDBOX_SHA256,
            "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3",
        )
        source_sha = "a" * 40
        with self.legacy_runtime_cache_fixture() as (platform_root, cache_root, target, _):
            original_tree = guard._tree_digest(
                target, ignored_relatives=frozenset({Path(".manifest.json")})
            )
            original_manifest = (target / ".manifest.json").read_bytes()
            events: list[tuple[str, dict[str, object]]] = []
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
            ):
                current_lock = hashlib.sha256(
                    (platform_root / "apps/platform_web/package-lock.json").read_bytes()
                ).hexdigest()
                cached_lock = hashlib.sha256(
                    (target / "web/package-lock.json").read_bytes()
                ).hexdigest()
                self.assertNotEqual(current_lock, cached_lock)
                with self.assertRaisesRegex(
                    guard.GuardError, "dependency provenance"
                ):
                    guard._validate_existing_runtime_cache(
                        target, platform_root, guard.LEGACY_FALLBACK_RUNTIME_COMMIT
                    )
                self.assertEqual(
                    guard._validate_legacy_fallback_runtime_cache()["package_lock_sha256"],
                    cached_lock,
                )
                for pin_name in (
                    "LEGACY_FALLBACK_PACKAGE_LOCK_SHA256",
                    "LEGACY_FALLBACK_PLAYWRIGHT_BROWSERS_SHA256",
                    "LEGACY_FALLBACK_NODE_ARCHIVE_SHA256",
                    "LEGACY_FALLBACK_CHROMIUM_SANDBOX_SHA256",
                ):
                    with (
                        self.subTest(pin=pin_name),
                        mock.patch.object(guard, pin_name, "0" * 64),
                        self.assertRaises(guard.GuardError),
                    ):
                        guard._validate_legacy_fallback_runtime_cache()
                with self.assertRaisesRegex(guard.GuardError, "manifest is invalid"):
                    guard._validate_existing_runtime_cache(
                        target, platform_root, "b" * 40
                    )
                result = guard.compact_legacy_runtime_cache(
                    run_id=12345,
                    attempt=1,
                    operation_source_sha=source_sha,
                    write_intent=lambda record: events.append(("intent", dict(record))),
                    write_validated=lambda record: events.append(("validated", dict(record))),
                    write_completion=lambda record: events.append(("complete", dict(record))),
                    probe_runtime=lambda _path: None,
                )
                self.assertEqual([phase for phase, _ in events], ["intent", "validated", "complete"])
                self.assertEqual(result["result"], "compacted")
                self.assertEqual(events[0][1]["source_commit"], guard.LEGACY_FALLBACK_RUNTIME_COMMIT)
                self.assertIn("old_chromium_inventory", events[0][1])
                self.assertNotIn("old_chromium_inventory", events[1][1])
                self.assertEqual(events[1][1]["intent_sha256"], hashlib.sha256(guard._encode_compaction_record(events[0][1])).hexdigest())
                self.assertEqual(events[2][1]["result"], "compacted")
                self.assertFalse(cache_root.joinpath(str(events[0][1]["rollback_name"])).exists())
                self.assertTrue(guard._validate_legacy_fallback_runtime_cache())
                chromium = target / "browsers/chromium-1228"
                self.assertEqual(
                    {path.relative_to(chromium).as_posix() for path in chromium.rglob("*")},
                    {"INSTALLATION_COMPLETE", "chrome-linux64", "chrome-linux64/chrome_sandbox"},
                )

        with self.legacy_runtime_cache_fixture() as (platform_root, cache_root, target, _):
            original_tree = guard._tree_digest(
                target, ignored_relatives=frozenset({Path(".manifest.json")})
            )
            original_manifest = (target / ".manifest.json").read_bytes()
            records: dict[str, dict[str, object]] = {}
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
            ):
                with self.assertRaisesRegex(guard.GuardError, "probe failure"):
                    guard.compact_legacy_runtime_cache(
                        run_id=12346,
                        attempt=1,
                        operation_source_sha=source_sha,
                        write_intent=lambda record: records.setdefault("intent", dict(record)),
                        write_validated=lambda record: records.setdefault("validated", dict(record)),
                        write_completion=lambda record: records.setdefault("completion", dict(record)),
                        probe_runtime=lambda _path: (_ for _ in ()).throw(guard.GuardError("probe failure")),
                    )
                self.assertEqual(records["completion"]["result"], "restored")
                self.assertEqual(records["completion"]["reclaimed_bytes"], 0)
                self.assertEqual((target / ".manifest.json").read_bytes(), original_manifest)
                self.assertEqual(
                    guard._tree_digest(target, ignored_relatives=frozenset({Path(".manifest.json")})),
                    original_tree,
                )
                self.assertFalse(cache_root.joinpath(str(records["intent"]["rollback_name"])).exists())

        with self.legacy_runtime_cache_fixture() as (platform_root, cache_root, target, manifest):
            source = "b" * 40
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
            ):
                (
                    old_manifest,
                    manifest_raw,
                    cache_metadata,
                    chromium_metadata,
                    old_tree,
                    old_non_chromium,
                    old_chromium_tree,
                    allocated,
                    inventory,
                    inventory_sha,
                ) = guard._compaction_snapshot(target)
            rollback_name = guard._compaction_rollback_name(guard.LEGACY_FALLBACK_RUNTIME_COMMIT)
            intent = guard._compaction_record_base(
                run_id=12347,
                attempt=1,
                operation_source_sha=source,
                cache_metadata=cache_metadata,
                manifest=old_manifest,
                manifest_raw=manifest_raw,
                manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                old_tree_sha256=old_tree,
                old_non_chromium_tree_sha256=old_non_chromium,
                chromium_metadata=chromium_metadata,
                chromium_tree_sha256=old_chromium_tree,
                chromium_allocated_bytes=allocated,
                chromium_inventory=inventory,
                chromium_inventory_sha256=inventory_sha,
                rollback_name=rollback_name,
            )
            events: list[dict[str, object]] = []
            rollback = cache_root / rollback_name
            os.rename(target / "browsers/chromium-1228", rollback)
            (target / "browsers/chromium-1228").mkdir(mode=0o755)
            serialized_intent = json.loads(json.dumps(intent, sort_keys=True))
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
            ):
                restored = guard.recover_legacy_runtime_cache(
                    serialized_intent,
                    None,
                    run_id=12347,
                    attempt=1,
                    operation_source_sha=source,
                    write_completion=lambda record: events.append(dict(record)),
                    probe_runtime=lambda _path: None,
                )
                self.assertEqual(restored["result"], "restored")
                self.assertEqual(events[0]["phase"], "complete")
                self.assertEqual(events[0]["new_manifest_sha256"], hashlib.sha256(manifest_raw).hexdigest())
                self.assertFalse(rollback.exists())
                self.assertEqual(
                    guard._tree_digest(target, ignored_relatives=frozenset({Path(".manifest.json")})),
                    old_tree,
                )

        with self.legacy_runtime_cache_fixture() as (platform_root, cache_root, target, _):
            source = "c" * 40
            intent_records: list[dict[str, object]] = []
            validated_records: list[dict[str, object]] = []
            completion_records: list[dict[str, object]] = []
            real_unlink = os.unlink
            unlink_count = 0

            def fail_after_one_unlink(path, *args, **kwargs):
                nonlocal unlink_count
                unlink_count += 1
                if unlink_count == 2:
                    raise OSError("injected partial-delete interruption")
                return real_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
                mock.patch.object(os, "unlink", side_effect=fail_after_one_unlink),
            ):
                with self.assertRaises(OSError):
                    guard.compact_legacy_runtime_cache(
                        run_id=12348,
                        attempt=1,
                        operation_source_sha=source,
                        write_intent=lambda record: intent_records.append(dict(record)),
                        write_validated=lambda record: validated_records.append(dict(record)),
                        write_completion=lambda record: completion_records.append(dict(record)),
                        probe_runtime=lambda _path: None,
                    )
            self.assertEqual(len(intent_records), 1)
            self.assertEqual(len(validated_records), 1)
            self.assertFalse(completion_records)
            rollback = cache_root / str(intent_records[0]["rollback_name"])
            unknown = rollback / "unexpected-after-validation"
            unknown.write_bytes(b"must remain fail-closed")
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "assert_liveqa_idle"),
            ):
                with self.assertRaisesRegex(guard.GuardError, "inventory does not close"):
                    guard.recover_legacy_runtime_cache(
                        intent_records[0],
                        validated_records[0],
                        run_id=12348,
                        attempt=1,
                        operation_source_sha=source,
                        write_completion=lambda record: completion_records.append(dict(record)),
                        probe_runtime=lambda _path: None,
                    )
                self.assertTrue(unknown.exists())
                unknown.unlink()
                recovered = guard.recover_legacy_runtime_cache(
                    intent_records[0],
                    validated_records[0],
                    run_id=12348,
                    attempt=1,
                    operation_source_sha=source,
                    write_completion=lambda record: completion_records.append(dict(record)),
                    probe_runtime=lambda _path: None,
                )
                self.assertEqual(recovered["result"], "compacted")
                self.assertEqual(completion_records[-1]["result"], "compacted")
                self.assertFalse(rollback.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache probe contract")
    def test_legacy_fallback_probe_uses_collected_sandboxed_children(self) -> None:
        with self.legacy_runtime_cache_fixture() as (platform_root, cache_root, target, _):
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "liveqa_identity", return_value=(12345, 12345)),
                mock.patch.object(guard, "assert_liveqa_idle") as idle,
                mock.patch.object(guard, "_assert_runtime_unit_collected") as collected,
                mock.patch.object(
                    guard,
                    "_capture_qa_child",
                    return_value=(
                        0,
                        "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=0\n",
                    ),
                ) as capture,
            ):
                guard.probe_compacted_legacy_runtime_cache(target)
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(collected.call_count, 2)
            idle.assert_has_calls([mock.call(), mock.call(), mock.call()])
            chromium_command = capture.call_args_list[0].args[0]
            webkit_command = capture.call_args_list[1].args[0]
            self.assertEqual(chromium_command[0], "/usr/bin/systemd-run")
            self.assertIn("--property=PrivateNetwork=yes", chromium_command)
            self.assertIn("--property=PrivateTmp=yes", chromium_command)
            self.assertIn("CHROME_DEVEL_SANDBOX=" + str(target / guard.CHROMIUM_SANDBOX_RELATIVE), chromium_command)
            self.assertTrue(
                any("chromiumSandbox:true" in argument for argument in chromium_command)
            )
            self.assertTrue(any("about:blank" in argument for argument in chromium_command))
            self.assertNotIn("--no-sandbox", chromium_command)
            self.assertIn("--property=PrivateNetwork=yes", webkit_command)
            self.assertTrue(
                any('else if(k==="webkit")' in argument for argument in webkit_command)
            )

            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root),
                mock.patch.object(guard, "TRUSTED_PLATFORM_ROOT", platform_root),
                mock.patch.object(guard, "liveqa_identity", return_value=(12345, 12345)),
                mock.patch.object(guard, "assert_liveqa_idle"),
                mock.patch.object(guard, "_assert_runtime_unit_collected") as collected,
                mock.patch.object(
                    guard,
                    "_capture_qa_child",
                    side_effect=[
                        (0, "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=0\n"),
                        (1, "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=browser_launch_error stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1\n"),
                    ],
                ),
            ):
                with self.assertRaisesRegex(guard.GuardError, "probe failed"):
                    guard.probe_compacted_legacy_runtime_cache(target)
            self.assertEqual(collected.call_count, 2)

    def test_sandbox_path_rejects_an_additional_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / f"runtime-{'a' * 40}"
            canonical = cache / guard.CHROMIUM_SANDBOX_RELATIVE
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"canonical")
            extra = cache / "other" / "chrome_sandbox"
            extra.parent.mkdir()
            extra.write_bytes(b"extra")
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", root),
                self.assertRaisesRegex(guard.GuardError, "ambiguous"),
            ):
                guard._sandbox_path(cache)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned runtime provider contract")
    def test_sandbox_path_resolves_only_the_installed_suite_provider(self) -> None:
        source_sha = "a" * 40
        external_provider_sha = "b" * 40

        def make_provider(base: Path, *, version: int, self_owned: bool = False) -> tuple[Path, Path, Path]:
            payloads = base / "trusted" / "releases"
            active_payload = payloads / source_sha
            provider_sha = source_sha if self_owned else external_provider_sha
            provider_payload = payloads / provider_sha
            runtime = active_payload / "runtime"
            runtime.mkdir(mode=0o755, parents=True)
            sandbox_root = provider_payload / ("provider-runtime" if version == 2 else "runtime")
            sandbox = sandbox_root / guard.CHROMIUM_SANDBOX_RELATIVE
            sandbox.parent.mkdir(mode=0o755, parents=True)
            sandbox.write_bytes(platform_chromium_sandbox_fixture.read_bytes())
            os.chown(sandbox, 0, 0)
            os.chmod(sandbox, 0o4755)
            for directory in sorted(
                (path for path in payloads.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                os.chown(directory, 0, 0)
                os.chmod(directory, 0o555)
            active_pointer = base / "trusted" / "active"
            active_pointer.symlink_to(Path("releases") / source_sha)
            return active_pointer, payloads, runtime

        for version, self_owned in ((2, False), (2, True), (1, False)):
            with self.subTest(version=version, self_owned=self_owned), tempfile.TemporaryDirectory(
                dir="/root"
            ) as temporary:
                base = Path(temporary)
                pointer, payloads, suite_root = make_provider(
                    base, version=version, self_owned=self_owned
                )
                active_payload = pointer.resolve(strict=True)
                engine_root = payloads / (
                    source_sha if self_owned else external_provider_sha
                ) / ("provider-runtime" if version == 2 else "runtime")
                with (
                    mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", pointer),
                    mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", payloads),
                    mock.patch.object(
                        guard, "_validate_installed_payload_root", return_value=source_sha
                    ),
                    mock.patch.object(guard, "_trusted_runtime_root", return_value=engine_root),
                ):
                    engine_output = io.StringIO()
                    with redirect_stdout(engine_output):
                        self.assertEqual(guard.main(["runtime-root"]), 0)
                    self.assertEqual(engine_output.getvalue(), f"{engine_root}\n")
                    sandbox_output = io.StringIO()
                    with redirect_stdout(sandbox_output):
                        self.assertEqual(
                            guard.main(["sandbox-path", "--runtime-cache", str(suite_root)]),
                            0,
                        )
                    self.assertEqual(
                        sandbox_output.getvalue(),
                        f"{engine_root / guard.CHROMIUM_SANDBOX_RELATIVE}\n",
                    )
                    error_output = io.StringIO()
                    with redirect_stderr(error_output):
                        self.assertEqual(
                            guard.main(
                                [
                                    "sandbox-path",
                                    "--runtime-cache",
                                    str(payloads / ("c" * 40) / "provider-runtime"),
                                ]
                            ),
                            2,
                        )
                    self.assertIn("refused", error_output.getvalue())
                    self.assertEqual(active_payload, payloads / source_sha)

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            cache_root = root / "cache"
            cache_root.mkdir(mode=0o755)
            cache = cache_root / f"runtime-{source_sha}"
            sandbox = cache / guard.CHROMIUM_SANDBOX_RELATIVE
            sandbox.parent.mkdir(mode=0o755, parents=True)
            sandbox.write_bytes(platform_chromium_sandbox_fixture.read_bytes())
            os.chown(sandbox, 0, 0)
            os.chmod(sandbox, 0o4755)
            with mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache_root):
                self.assertEqual(guard._sandbox_path(cache), sandbox)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_retention_protects_releases_and_hard_caps_fallbacks(
        self,
    ) -> None:
        day_ns = 24 * 60 * 60 * 1_000_000_000
        now_ns = 2_000_000_000_000_000_000
        commits = {name: character * 40 for name, character in zip("abcde", "abcde")}
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            caches = {
                "a": self.make_runtime_cache(
                    root, commits["a"], modified_at_ns=now_ns - 90 * day_ns
                ),
                "b": self.make_runtime_cache(
                    root, commits["b"], modified_at_ns=now_ns - 80 * day_ns
                ),
                "c": self.make_runtime_cache(
                    root, commits["c"], modified_at_ns=now_ns - day_ns
                ),
                "d": self.make_runtime_cache(
                    root, commits["d"], modified_at_ns=now_ns - 2 * day_ns
                ),
                "e": self.make_runtime_cache(
                    root, commits["e"], modified_at_ns=now_ns - 30 * day_ns
                ),
            }
            malformed = root / f"runtime-{'F' * 40}"
            malformed.mkdir()

            plan = guard.build_runtime_cache_retention_plan(
                root,
                protected_commits=frozenset({commits["a"], commits["b"]}),
                keep=1,
            )

            self.assertEqual(
                {entry.path for entry in plan.protected},
                {caches["a"], caches["b"]},
            )
            self.assertEqual([entry.path for entry in plan.retained], [caches["c"]])
            self.assertEqual(
                [entry.path for entry in plan.candidates],
                [caches["d"], caches["e"]],
            )
            self.assertNotIn(
                malformed,
                {
                    entry.path
                    for entry in (
                        *plan.protected,
                        *plan.retained,
                        *plan.candidates,
                    )
                },
            )

            guard.apply_runtime_cache_retention_plan(plan, root=root)
            self.assertFalse(caches["e"].exists())
            self.assertTrue(caches["a"].exists())
            self.assertTrue(caches["b"].exists())
            self.assertTrue(caches["c"].exists())
            self.assertFalse(caches["d"].exists())
            self.assertTrue(malformed.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_retention_refuses_an_exact_name_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            outside = base / "outside"
            outside.mkdir()
            candidate = root / f"runtime-{'a' * 40}"
            candidate.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(guard.GuardError, "target metadata is unsafe"):
                guard.build_runtime_cache_retention_plan(
                    root,
                    protected_commits=frozenset(),
                    keep=1,
                )
            self.assertTrue(candidate.is_symlink())
            self.assertTrue(outside.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_retention_refuses_an_unsafe_manifest_before_deletion(
        self,
    ) -> None:
        day_ns = 24 * 60 * 60 * 1_000_000_000
        now_ns = 2_000_000_000_000_000_000
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            valid = self.make_runtime_cache(
                root, "a" * 40, modified_at_ns=now_ns - 30 * day_ns
            )
            unsafe = self.make_runtime_cache(
                root, "b" * 40, modified_at_ns=now_ns - 20 * day_ns
            )
            os.chmod(unsafe / ".manifest.json", 0o400)

            with self.assertRaisesRegex(
                guard.GuardError, "file permissions are unsafe"
            ):
                guard.build_runtime_cache_retention_plan(
                    root,
                    protected_commits=frozenset(),
                    keep=1,
                )

            self.assertTrue(valid.exists())
            self.assertTrue(unsafe.exists())

    def test_runtime_retention_requires_one_fallback(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "keep must be between 1"):
            guard.build_runtime_cache_retention_plan(
                Path("/does/not/matter"),
                protected_commits=frozenset(),
                keep=0,
            )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_runtime_retention_dry_run_takes_lock_and_does_not_delete(self) -> None:
        day_ns = 24 * 60 * 60 * 1_000_000_000
        now_ns = 2_000_000_000_000_000_000
        current = "a" * 40
        previous = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            newest = self.make_runtime_cache(
                root, "c" * 40, modified_at_ns=now_ns - 20 * day_ns
            )
            old = self.make_runtime_cache(
                root, "d" * 40, modified_at_ns=now_ns - 30 * day_ns
            )
            app_dir = base / "app"
            self.make_release_pointers(
                app_dir, current=current, previous=previous
            )
            lock_path = base / "liveqa.lock"
            release_lock = lock_support.create_test_lock(
                "liveqa-retention-release", root=base
            )
            with (
                mock.patch.object(guard, "MACHINE_LOCK_PATH", lock_path),
                mock.patch.object(guard, "RELEASE_LOCK_PATH", release_lock.path),
                mock.patch.object(guard, "_liveqa_cgroup_process_ids", return_value=()),
                mock.patch.object(guard, "_liveqa_process_ids", return_value=()),
                mock.patch.object(
                    guard, "_open_machine_lock", wraps=guard._open_machine_lock
                ) as opened_lock,
            ):
                try:
                    plan = guard.prune_runtime_cache(
                        apply=False,
                        keep=1,
                        root=root,
                        app_dir=app_dir,
                    )
                finally:
                    release_lock.cleanup()
            opened_lock.assert_called_once_with()
            self.assertEqual([entry.path for entry in plan.retained], [newest])
            self.assertEqual([entry.path for entry in plan.candidates], [old])
            self.assertTrue(newest.exists())
            self.assertTrue(old.exists())

    def test_missing_runtime_root_does_not_require_live_qa_identity(self) -> None:
        missing = Path("/definitely-missing-liveqa-runtime-root")
        with (
            mock.patch.object(guard, "_open_machine_lock") as machine_lock,
            mock.patch.object(guard, "assert_liveqa_idle") as assert_idle,
        ):
            plan = guard.prune_runtime_cache_release_lock_held(
                apply=True,
                keep=1,
                root=missing,
                app_dir=Path("/opt/oldsparky/platform"),
            )
        self.assertEqual(plan, guard.RuntimeCacheRetentionPlan((), (), (), ()))
        machine_lock.assert_not_called()
        assert_idle.assert_not_called()

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_existing_runtime_root_keeps_identity_check_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            machine_lock = lock_support.create_test_lock(
                "liveqa-machine", root=base
            )
            try:
                descriptor = os.dup(machine_lock.fd)
                with (
                    mock.patch.object(
                        guard, "_open_machine_lock", return_value=descriptor
                    ),
                    mock.patch.object(
                        guard,
                        "assert_liveqa_idle",
                        side_effect=guard.GuardError(
                            "dedicated oldsparky-liveqa system account is unavailable"
                        ),
                    ),
                    self.assertRaisesRegex(guard.GuardError, "account is unavailable"),
                ):
                    guard.prune_runtime_cache_release_lock_held(
                        apply=False,
                        keep=1,
                        root=root,
                        app_dir=Path("/opt/oldsparky/platform"),
                    )
            finally:
                machine_lock.cleanup()

    def test_runtime_retention_cli_is_dry_run_by_default(self) -> None:
        args = guard._parser().parse_args(["prune-runtime-cache"])
        self.assertFalse(args.apply)
        self.assertEqual(args.keep, 1)
        self.assertFalse(hasattr(args, "max_age_days"))

    def test_trusted_runtime_root_takes_no_caller_selected_path(self) -> None:
        self.assertEqual(guard._parser().parse_args(["runtime-root"]).command, "runtime-root")
        with self.assertRaises(SystemExit):
            guard._parser().parse_args(["runtime-root", "/tmp/untrusted-runtime"])
        files = {
            "browsers/webkit-2311/lib/libengine.so.1": "a" * 64,
        }
        self.assertEqual(
            guard._provider_link_map(
                {
                    "engine_files": files,
                    "engine_links": {
                        "browsers/webkit-2311/lib/libengine.so": "libengine.so.1"
                    },
                }
            ),
            {"browsers/webkit-2311/lib/libengine.so": "libengine.so.1"},
        )
        for bad_links in (
            {"browsers/webkit-2311/lib/escape": "../../../../outside"},
            {"browsers/webkit-2311/lib/cycle-a": "cycle-b", "browsers/webkit-2311/lib/cycle-b": "cycle-a"},
            {"browsers/chromium-1228/lib/alias": "../webkit-2311/lib/libengine.so.1"},
        ):
            with self.subTest(bad_links=bad_links), self.assertRaises(guard.GuardError):
                guard._provider_link_map(
                    {"engine_files": files, "engine_links": bad_links}
                )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned runtime provider contract")
    def test_trusted_runtime_root_hashes_bounded_large_provider_member(self) -> None:
        source_sha = "a" * 40
        provider_sha = "b" * 40
        node_bytes = b"trusted-node-binary" * 60_000
        self.assertGreater(len(node_bytes), 1024 * 1024)

        def make_provider(
            base: Path,
            *,
            node_size: int = len(node_bytes),
            expected_node_digest: str | None = None,
            node_mode: int = 0o444,
            node_symlink: bool = False,
            recovery_provider: bool = False,
            self_owned_provider: bool = False,
            omit_source_marker: bool = False,
            source_marker_value: str | None = None,
            extra_provider_entry: bool = False,
            provider_version: int = 1,
            link_alias: bool = False,
            extra_provider_suite_file: bool = False,
            nested_package_files: bool = False,
            nested_node_modules: bool = False,
        ) -> tuple[Path, Path]:
            payloads = base / "trusted" / "releases"
            active_payload = payloads / source_sha
            actual_provider_sha = (
                guard.PROVIDER_RECOVERY_SOURCE_SHA
                if recovery_provider
                else source_sha if self_owned_provider else provider_sha
            )
            provider_payload = payloads / actual_provider_sha
            active_payload.mkdir(mode=0o755, parents=True)
            (active_payload / "runtime").mkdir(mode=0o555)
            (active_payload / "runtime" / "runtime-manifest.json").write_text(
                "{}\n", encoding="ascii"
            )
            os.chmod(active_payload / "runtime" / "runtime-manifest.json", 0o444)
            runtime_root = provider_payload / (
                "provider-runtime" if provider_version == 2 else "runtime"
            )
            node_path = runtime_root / "node" / "bin" / "node"
            node_path.parent.mkdir(mode=0o555, parents=True)
            with node_path.open("wb") as stream:
                if node_size == len(node_bytes):
                    stream.write(node_bytes)
                else:
                    stream.truncate(node_size)
            os.chmod(node_path, node_mode)
            if node_symlink:
                target = base / "node-target"
                node_path.replace(target)
                node_path.symlink_to(target)

            engine_files: dict[str, str] = {
                "node/bin/node": (
                    expected_node_digest
                    if expected_node_digest is not None
                    else hashlib.sha256(node_bytes).hexdigest()
                )
            }
            for browser in sorted(guard.LIVE_QA_BROWSER_ROOTS):
                browser_root = runtime_root / "browsers" / browser
                browser_root.mkdir(mode=0o555, parents=True)
                relative = f"browsers/{browser}/engine.bin"
                content = f"{browser}-fixture".encode("ascii")
                browser_file = runtime_root / relative
                browser_file.write_bytes(content)
                os.chmod(browser_file, 0o444)
                engine_files[relative] = hashlib.sha256(content).hexdigest()

            sandbox_relative = guard.CHROMIUM_SANDBOX_RELATIVE.as_posix()
            sandbox_path = runtime_root / sandbox_relative
            sandbox_path.parent.mkdir(mode=0o555, parents=True, exist_ok=True)
            sandbox_path.write_bytes(platform_chromium_sandbox_fixture.read_bytes())
            os.chmod(sandbox_path, 0o4755)
            engine_files[sandbox_relative] = guard.CHROMIUM_SANDBOX_SHA256

            web_root = runtime_root / "web"
            web_root.mkdir(mode=0o555)
            package_lock = b"{}\n"
            (web_root / "package-lock.json").write_bytes(package_lock)
            os.chmod(web_root / "package-lock.json", 0o444)
            package_lock_sha = hashlib.sha256(package_lock).hexdigest()
            if nested_package_files:
                package_files = {
                    "web/node_modules/@playwright/test/lib/index.js": b"test package\n",
                    "web/node_modules/playwright/lib/runner/index.js": b"playwright package\n",
                    "web/node_modules/playwright-core/lib/server/index.js": b"core package\n",
                }
                if nested_node_modules:
                    package_files[
                        "web/node_modules/playwright-core/lib/server/node_modules/escape/index.js"
                    ] = b"nested dependency\n"
                for relative, content in package_files.items():
                    path = runtime_root / relative
                    path.parent.mkdir(mode=0o555, parents=True, exist_ok=True)
                    path.write_bytes(content)
                    os.chmod(path, 0o444)
                    engine_files[relative] = hashlib.sha256(content).hexdigest()
            engine_links: dict[str, str] = {}
            if link_alias:
                alias = "browsers/webkit-2311/engine.alias"
                engine_links[alias] = "engine.bin"
                (runtime_root / alias).symlink_to(engine_links[alias])
            engine_digest = hashlib.sha256(
                (
                    b"oldsparky-liveqa-engine-v2\0"
                    if provider_version == 2
                    else b"oldsparky-liveqa-engine-v1\0"
                )
                + guard.NODE_VERSION.encode("ascii")
                + b"\0"
                + package_lock_sha.encode("ascii")
                + b"\0"
            )
            for relative in sorted(
                engine_files.keys() | engine_links.keys(),
                key=lambda value: PurePosixPath(value).parts,
            ):
                engine_digest.update(relative.encode("utf-8") + b"\0")
                if relative in engine_links:
                    engine_digest.update(
                        b"l\0" + engine_links[relative].encode("utf-8") + b"\0"
                    )
                else:
                    engine_digest.update(b"f\0" + bytes.fromhex(engine_files[relative]))
            suite_manifest = {
                "version": provider_version + 1,
                "node_version": guard.NODE_VERSION,
                "package_lock_sha256": package_lock_sha,
                "engine_tree_sha256": engine_digest.hexdigest(),
                "engine_files": engine_files,
            }
            if provider_version == 2:
                suite_manifest["engine_links"] = engine_links
            (active_payload / "runtime" / "runtime-manifest.json").write_text(
                json.dumps(suite_manifest, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            os.chmod(active_payload / "runtime" / "runtime-manifest.json", 0o444)
            provider = {
                "version": provider_version,
                "source_sha": source_sha,
                "provider_sha": actual_provider_sha,
                "engine_tree_sha256": engine_digest.hexdigest(),
                "node_version": guard.NODE_VERSION,
                "package_lock_sha256": package_lock_sha,
                "engine_files": engine_files,
            }
            if provider_version == 2:
                provider["engine_links"] = engine_links
            provider_path = active_payload / "runtime-provider.json"
            provider_path.write_text(
                json.dumps(provider, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            os.chmod(provider_path, 0o444)
            if not omit_source_marker:
                source_file = provider_payload / "source-sha"
                source_file.write_text(
                    source_marker_value
                    if source_marker_value is not None
                    else actual_provider_sha + "\n",
                    encoding="ascii",
                )
                os.chmod(source_file, 0o444)
            if extra_provider_entry:
                unexpected = provider_payload / "unexpected"
                unexpected.write_text("x\n", encoding="ascii")
                os.chmod(unexpected, 0o444)
            if extra_provider_suite_file:
                forbidden = runtime_root / "web/tests/support/extra.ts"
                forbidden.parent.mkdir(mode=0o555, parents=True, exist_ok=True)
                forbidden.write_text("export {};\n", encoding="ascii")
                os.chmod(forbidden, 0o444)

            for directory in sorted(
                (path for path in payloads.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                os.chmod(directory, 0o555)
            pointer = base / "trusted" / "active"
            pointer.symlink_to(Path("releases") / source_sha)
            return pointer, node_path

        def resolve(pointer: Path, payloads: Path) -> Path:
            with (
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", pointer),
                mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", payloads),
                mock.patch.object(
                    guard, "_validate_installed_payload_root", return_value=source_sha
                ),
            ):
                return guard._trusted_runtime_root()

        def sandbox(pointer: Path, payloads: Path) -> Path:
            active_payload = pointer.resolve(strict=True)
            with (
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", pointer),
                mock.patch.object(guard, "TRUSTED_PAYLOAD_ROOT", payloads),
                mock.patch.object(
                    guard, "_validate_installed_payload_root", return_value=source_sha
                ),
            ):
                return guard._sandbox_path(active_payload / "runtime")

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            pointer, node = make_provider(base)
            self.assertEqual(
                resolve(pointer, base / "trusted" / "releases"),
                base / "trusted" / "releases" / provider_sha / "runtime",
            )
            self.assertEqual(
                sandbox(pointer, base / "trusted" / "releases"),
                base
                / "trusted"
                / "releases"
                / provider_sha
                / "runtime"
                / guard.CHROMIUM_SANDBOX_RELATIVE,
            )
            with self.assertRaisesRegex(
                guard.GuardError, "helper file metadata is unsafe"
            ):
                guard._sha256_regular(node, expected_uid=0)

        with self.subTest("provider v2 preserves declared aliases but rejects suite files"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base,
                    provider_version=2,
                    link_alias=True,
                    nested_package_files=True,
                )
                self.assertEqual(
                    resolve(pointer, base / "trusted" / "releases"),
                    base
                    / "trusted"
                    / "releases"
                    / provider_sha
                    / "provider-runtime",
                )
                self.assertEqual(
                    sandbox(pointer, base / "trusted" / "releases"),
                    base
                    / "trusted"
                    / "releases"
                    / provider_sha
                    / "provider-runtime"
                    / guard.CHROMIUM_SANDBOX_RELATIVE,
                )
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base,
                    provider_version=2,
                    link_alias=True,
                    nested_package_files=True,
                    nested_node_modules=True,
                )
                with self.assertRaisesRegex(guard.GuardError, "file map is invalid"):
                    resolve(pointer, base / "trusted" / "releases")
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base,
                    provider_version=2,
                    link_alias=True,
                    extra_provider_suite_file=True,
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "unreviewed directory|unreviewed file"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("provider v2 self-owned"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base,
                    provider_version=2,
                    self_owned_provider=True,
                    link_alias=True,
                )
                expected_root = base / "trusted" / "releases" / source_sha / "provider-runtime"
                self.assertEqual(resolve(pointer, base / "trusted" / "releases"), expected_root)
                self.assertEqual(
                    sandbox(pointer, base / "trusted" / "releases"),
                    expected_root / guard.CHROMIUM_SANDBOX_RELATIVE,
                )

        self.assertEqual(
            guard.PROVIDER_RECOVERY_SOURCE_SHA,
            "19603dfff122386726313c86b0350812ebbe8cca",
        )
        installer_source = SCRIPT_PATH.with_name(
            "platform_live_qa_runtime_install.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            f'PROVIDER_RECOVERY_SOURCE_SHA = "{guard.PROVIDER_RECOVERY_SOURCE_SHA}"',
            installer_source,
        )
        with self.subTest("recovered provider exact runtime-only shape"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base, recovery_provider=True, omit_source_marker=True
                )
                self.assertEqual(
                    resolve(pointer, base / "trusted" / "releases"),
                    base / "trusted" / "releases" / guard.PROVIDER_RECOVERY_SOURCE_SHA / "runtime",
                )

        with self.subTest("ordinary provider still requires source marker"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(base, omit_source_marker=True)
                with self.assertRaisesRegex(
                    guard.GuardError, "referenced live-QA engine provider is unavailable"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("recovered provider rejects malformed present marker"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base, recovery_provider=True, source_marker_value="other\n"
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "referenced live-QA engine provider metadata is unsafe"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("recovered provider rejects extra outer entry"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base, recovery_provider=True, omit_source_marker=True,
                    extra_provider_entry=True,
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "referenced live-QA engine provider is unavailable"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("hash mismatch"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base, expected_node_digest="0" * 64
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "provider digest does not match"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("oversized member"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(
                    base, node_size=guard.MAX_ZIP_MEMBER_BYTES + 1
                )
                with self.assertRaisesRegex(
                    guard.GuardError, "helper file metadata is unsafe"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("unsafe member mode"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(base, node_mode=0o644)
                with self.assertRaisesRegex(
                    guard.GuardError, "provider file is unsafe"
                ):
                    resolve(pointer, base / "trusted" / "releases")

        with self.subTest("symlink member"):
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                base = Path(temporary)
                pointer, _node = make_provider(base, node_symlink=True)
                with self.assertRaisesRegex(
                    guard.GuardError, "provider contains unsafe metadata"
                ):
                    resolve(pointer, base / "trusted" / "releases")

    @unittest.skipUnless(os.geteuid() == 0, "root-owned runtime mapping contract")
    def test_runtime_mount_aliases_are_fixed_empty_root_owned_and_removed_by_identity(
        self,
    ) -> None:
        source_sha = "a" * 40
        provider_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            cache = base / "cache"
            cache.mkdir(mode=0o755)
            gate_root = base / "run"
            gate_root.mkdir(mode=0o711)
            gate = gate_root / "public-live-qa.abcdefgh"
            gate.mkdir(mode=0o700)
            os.chown(gate, 12345, 12345)
            engine_root = cache / provider_sha / "runtime"
            engine_root.parent.mkdir(mode=0o755)
            engine_root.mkdir(mode=0o555)
            pointer_target = base / "active-payload"
            pointer_target.mkdir()
            active_pointer = base / "active"
            active_pointer.symlink_to(pointer_target)
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache),
                mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", active_pointer),
                mock.patch.object(guard, "liveqa_identity", return_value=(12345, 12345)),
                mock.patch.object(
                    guard, "_validate_installed_payload_root", return_value=source_sha
                ),
                mock.patch.object(guard, "_trusted_runtime_root", return_value=engine_root),
            ):
                values = guard._runtime_mount_aliases(gate)
                self.assertEqual(values[:3], (source_sha, provider_sha, "abcdefgh"))
                suite = cache / f"runtime-suite-{source_sha}-abcdefgh"
                engine = cache / f"runtime-engine-{provider_sha}-abcdefgh"
                for target, identity in zip((suite, engine), values[3:], strict=True):
                    metadata = target.lstat()
                    self.assertTrue(stat.S_ISDIR(metadata.st_mode))
                    self.assertEqual((metadata.st_uid, metadata.st_gid), (0, 0))
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o555)
                    self.assertEqual(metadata.st_nlink, 2)
                    self.assertFalse(any(target.iterdir()))
                    self.assertEqual(identity, f"{metadata.st_dev}:{metadata.st_ino}")
                guard._cleanup_runtime_mount_aliases(*values)
                self.assertFalse(suite.exists())
                self.assertFalse(engine.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-owned runtime mapping contract")
    def test_runtime_mount_aliases_accept_the_exact_private_user_qa_gate(self) -> None:
        source_sha = "a" * 40
        provider_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            cache = base / "cache"
            cache.mkdir(mode=0o755)
            gate_root = base / "run"
            gate_root.mkdir(mode=0o711)
            gate = gate_root / "live-user-qa.a1B2c3"
            gate.mkdir(mode=0o700)
            os.chown(gate, 12345, 12345)
            engine_root = cache / provider_sha / "provider-runtime"
            engine_root.parent.mkdir(mode=0o755)
            engine_root.mkdir(mode=0o555)
            pointer_target = base / "active-payload"
            pointer_target.mkdir()
            active_pointer = base / "active"
            active_pointer.symlink_to(pointer_target)
            expected_suffix = hashlib.sha256(gate.name.encode("ascii")).hexdigest()[:8]
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache),
                mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", active_pointer),
                mock.patch.object(guard, "liveqa_identity", return_value=(12345, 12345)),
                mock.patch.object(
                    guard, "_validate_installed_payload_root", return_value=source_sha
                ),
                mock.patch.object(guard, "_trusted_runtime_root", return_value=engine_root),
            ):
                values = guard._runtime_mount_aliases(gate)
                self.assertEqual(values[:3], (source_sha, provider_sha, expected_suffix))
                suite = cache / f"runtime-suite-{source_sha}-{expected_suffix}"
                engine = cache / f"runtime-engine-{provider_sha}-{expected_suffix}"
                self.assertEqual(values[3:], tuple(
                    f"{path.lstat().st_dev}:{path.lstat().st_ino}"
                    for path in (suite, engine)
                ))
                guard._cleanup_runtime_mount_aliases(*values)
                self.assertFalse(suite.exists())
                self.assertFalse(engine.exists())

    def test_live_user_browser_command_binds_suite_and_engine_roots_exactly(self) -> None:
        source_sha = "a" * 40
        provider_sha = "b" * 40
        suffix = "a1b2c3d4"
        suite = f"/var/lib/oldsparky-liveqa/runtime-suite-{source_sha}-{suffix}"
        engine = f"/var/lib/oldsparky-liveqa/runtime-engine-{provider_sha}-{suffix}"
        command = [
            "/usr/bin/systemd-run",
            "--no-ask-password",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            "--expand-environment=no",
            "--unit=oldsparky-liveqa-browser.service",
            "--uid=1001",
            "--gid=1001",
            f"--working-directory={suite}/web",
            "--property=KillMode=control-group",
            "--property=Restart=no",
            "--property=RuntimeMaxSec=30min",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=5s",
            "--property=UMask=0077",
            f"--property=BindReadOnlyPaths={suite}:{suite}",
            f"--property=BindReadOnlyPaths={engine}:{engine}",
            "--",
            "/usr/bin/env",
            "-i",
            f"CHROME_DEVEL_SANDBOX={engine}/{guard.CHROMIUM_SANDBOX_RELATIVE.as_posix()}",
            "HOME=/run/oldsparky-liveqa/live-user-qa.abc123/home",
            "LANG=C.UTF-8",
            f"NODE_PATH={engine}/web/node_modules",
            f"PATH={engine}/node/bin:/usr/bin:/bin",
            "PLATFORM_LIVE_EXPECTED_ORIGIN=https://old-sparky.com",
            "PLATFORM_LIVE_USER_QA_UID=1001",
            "PLATFORM_QA_BROWSER_GATE_DIR=/run/oldsparky-liveqa/live-user-qa.abc123",
            f"PLATFORM_LIVE_QA_RUNNER_SHA={source_sha}",
            f"PLATFORM_LIVE_QA_TARGET_SHA={source_sha}",
            f"PLATFORM_LIVE_QA_MARKER_SHA256={'c' * 64}",
            "PLATFORM_LIVE_USER_QA=1",
            "PLATFORM_LIVE_USER_QA_INVENTORY=/run/oldsparky-liveqa/live-user-qa.abc123/inventory.json",
            "PLATFORM_LIVE_USER_QA_MARKER=marker",
            "PLATFORM_LIVE_USER_QA_SESSIONS=/run/oldsparky-liveqa/live-user-qa.abc123/browser-sessions.json",
            f"PLAYWRIGHT_BROWSERS_PATH={engine}/browsers",
            "PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com",
            "TMPDIR=/run/oldsparky-liveqa/live-user-qa.abc123/tmp",
            "XDG_CACHE_HOME=/run/oldsparky-liveqa/live-user-qa.abc123/home/.cache",
            f"{engine}/node/bin/node",
            f"{engine}/web/node_modules/@playwright/test/cli.js",
            "test",
            f"--config={suite}/web/playwright.live.config.ts",
            f"{suite}/web/tests/smoke/live-user-journey.spec.ts",
            "--project=live-desktop",
        ]
        self.assertTrue(guard._is_fixed_live_user_browser_command(command))
        for invalid in (
            [
                f"NODE_PATH={suite}/web/node_modules" if item.startswith("NODE_PATH=") else item
                for item in command
            ],
            [
                item.replace(f"{engine}/web/node_modules/@playwright/test/cli.js", f"{suite}/web/node_modules/@playwright/test/cli.js")
                if item.endswith("/web/node_modules/@playwright/test/cli.js")
                else item
                for item in command
            ],
            [
                f"--property=BindReadOnlyPaths=/root/.oldsparky/liveqa/releases/{provider_sha}/provider-runtime:/root/.oldsparky/liveqa/releases/{provider_sha}/provider-runtime"
                if item == f"--property=BindReadOnlyPaths={engine}:{engine}"
                else item
                for item in command
            ],
        ):
            self.assertFalse(guard._is_fixed_live_user_browser_command(invalid))

    @unittest.skipUnless(shutil.which("node"), "Node is required for module-resolution contract")
    def test_split_runtime_resolves_suite_config_dependencies_from_engine_node_modules(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            suite_web = root / "runtime-suite" / "web"
            package = root / "runtime-engine" / "web/node_modules/@playwright/test"
            suite_web.mkdir(mode=0o700, parents=True)
            package.mkdir(mode=0o755, parents=True)
            (package / "package.json").write_text(
                '{"name":"@playwright/test","main":"index.js"}\n',
                encoding="ascii",
            )
            (package / "index.js").write_text("module.exports = {};\n", encoding="ascii")
            result = subprocess.run(
                [
                    shutil.which("node") or "node",
                    "-e",
                    "process.stdout.write(require.resolve('@playwright/test'))",
                ],
                cwd=suite_web,
                env={
                    "LANG": "C.UTF-8",
                    "NODE_PATH": str(root / "runtime-engine/web/node_modules"),
                    "PATH": "/usr/bin:/bin",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(package / "index.js"))

    def test_live_user_wrapper_keeps_suite_and_engine_paths_separate(self) -> None:
        wrapper = SCRIPT_PATH.with_name("platform_live_user_qa.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('RUNTIME_SUITE_SOURCE="$TRUSTED_INSTALL_ROOT/runtime"', wrapper)
        self.assertIn('RUNTIME_ENGINE_SOURCE="$("${GUARD[@]}" runtime-root)"', wrapper)
        self.assertIn('prepare-runtime-mounts --gate-path "$BROWSER_GATE"', wrapper)
        self.assertIn('"--property=BindReadOnlyPaths=$RUNTIME_SUITE_SOURCE:$RUNTIME_SUITE"', wrapper)
        self.assertIn('"--property=BindReadOnlyPaths=$RUNTIME_ENGINE_SOURCE:$RUNTIME_ENGINE"', wrapper)
        self.assertIn('--working-directory="$RUNTIME_SUITE/web"', wrapper)
        self.assertIn('NODE_PATH="$RUNTIME_ENGINE/web/node_modules"', wrapper)
        self.assertIn('"$RUNTIME_ENGINE/web/node_modules/@playwright/test/cli.js"', wrapper)
        self.assertIn('--config="$RUNTIME_SUITE/web/playwright.live.config.ts"', wrapper)
        self.assertIn('"$RUNTIME_SUITE/web/tests/smoke/live-user-journey.spec.ts"', wrapper)
        self.assertIn('cleanup-runtime-mounts', wrapper)

    @unittest.skipUnless(os.geteuid() == 0, "trusted wrapper execution contract")
    def test_live_user_wrapper_reports_runtime_setup_before_state_creation(self) -> None:
        releases = Path("/root/.oldsparky/liveqa/releases")
        releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        source_sha = "f" * 40
        install_root = releases / source_sha
        self.assertFalse(install_root.exists())
        trace_fd, trace_name = tempfile.mkstemp(prefix="liveqa-wrapper-trace-", dir="/root")
        os.close(trace_fd)
        trace_path = Path(trace_name)
        trace_path.unlink()

        try:
            install_root.mkdir(mode=0o700)
            tools = install_root / "platform" / "tools"
            tools.mkdir(mode=0o755, parents=True)
            wrapper_path = tools / "platform_live_user_qa.sh"
            wrapper_bytes = SCRIPT_PATH.with_name("platform_live_user_qa.sh").read_bytes()
            fixture_runtime = b'QA_PYTHON="/opt/oldsparky/platform/shared/venv/bin/python"'
            self.assertEqual(wrapper_bytes.count(fixture_runtime), 1)
            wrapper_path.write_bytes(
                wrapper_bytes.replace(fixture_runtime, b'QA_PYTHON="/usr/bin/true"')
            )
            os.chmod(wrapper_path, 0o500)

            guard_stub = tools / "platform_live_qa_guard.py"
            guard_stub.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "with Path(os.environ['TRACE_PATH']).open('a', encoding='utf-8') as f:\n"
                "    f.write((args[0] if args else '<empty>') + '\\n')\n"
                "if args and args[0] == 'runtime-root':\n"
                "    print(os.environ['ENGINE_ROOT'])\n"
                "elif args and args[0] == 'sandbox-path':\n"
                "    print('stub diagnostic must stay suppressed', file=sys.stderr)\n"
                "    raise SystemExit(2)\n"
                "elif args and args[0] in {'assert-lock', 'preflight'}:\n"
                "    raise SystemExit(0)\n"
                "else:\n"
                "    raise SystemExit(91)\n",
                encoding="utf-8",
            )
            safe_env_stub = tools / "platform_safe_env_exec.py"
            safe_env_stub.write_text(
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['validate-runtime']:\n"
                "    raise SystemExit(0)\n"
                "if args == ['print-public-value', 'PLATFORM_WEB_ORIGIN']:\n"
                "    print('https://old-sparky.com')\n"
                "    raise SystemExit(0)\n"
                "if args == ['print-public-value', 'PLATFORM_ENVIRONMENT']:\n"
                "    print('production')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(92)\n",
                encoding="utf-8",
            )
            runtime_root = install_root / "runtime"
            runtime_root.mkdir(mode=0o755)
            engine_root = Path("/root/.oldsparky/liveqa/releases") / ("e" * 40) / "provider-runtime"

            result = subprocess.run(
                ["/usr/bin/bash", str(wrapper_path)],
                cwd="/",
                env={
                    "PATH": "/usr/bin:/bin",
                    "TRACE_PATH": str(trace_path),
                    "ENGINE_ROOT": str(engine_root),
                    "PLATFORM_LIVE_QA_INSTALL_ROOT": str(install_root),
                    "PLATFORM_LIVE_QA_TARGET_SHA": source_sha,
                    "PLATFORM_APP_DIR": "/opt/oldsparky/platform",
                    "PLATFORM_LIVE_CSP_QA_BUNDLE": "/root/private-test-bundle.json",
                    "PLATFORM_LIVE_QA_LOCK_FD": "9",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(
                result.stdout,
                "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=dispatch_failure "
                "stage=dispatch check=runtime_setup stdout_bytes=0 stderr_bytes=0 "
                "truncated=false child_exit=2\n",
            )
            self.assertNotIn("stub diagnostic", result.stderr)
            self.assertEqual(
                trace_path.read_text(encoding="utf-8").splitlines(),
                ["assert-lock", "preflight", "runtime-root", "sandbox-path"],
            )
            self.assertFalse((install_root / "qa-state").exists())
        finally:
            trace_path.unlink(missing_ok=True)
            if install_root.exists():
                shutil.rmtree(install_root)

    @unittest.skipUnless(os.geteuid() == 0, "trusted wrapper execution contract")
    def test_live_user_wrapper_captures_split_runtime_before_browser_launch(self) -> None:
        releases = Path("/root/.oldsparky/liveqa/releases")
        releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        source_sha = "d" * 40
        runner_sha = "c" * 40
        provider_sha = "e" * 40
        install_root = releases / source_sha
        self.assertFalse(install_root.exists())
        temp_root = Path(tempfile.mkdtemp(prefix="liveqa-wrapper-split-", dir="/root"))
        trace_path = temp_root / "trace.jsonl"
        state_path = temp_root / "state"
        gate_path = temp_root / "gate"
        engine_root = releases / provider_sha / "provider-runtime"
        suite_alias = f"/var/lib/oldsparky-liveqa/runtime-suite-{source_sha}-deadbeef"
        engine_alias = f"/var/lib/oldsparky-liveqa/runtime-engine-{provider_sha}-deadbeef"
        sandbox_path = f"{engine_root}/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
        marker = "fixture-marker"
        marker_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()

        try:
            install_root.mkdir(mode=0o700)
            tools = install_root / "platform" / "tools"
            tools.mkdir(mode=0o755, parents=True)
            wrapper_path = tools / "platform_live_user_qa.sh"
            wrapper_bytes = SCRIPT_PATH.with_name("platform_live_user_qa.sh").read_bytes()
            fixture_runtime = b'QA_PYTHON="/opt/oldsparky/platform/shared/venv/bin/python"'
            self.assertEqual(wrapper_bytes.count(fixture_runtime), 1)
            fixture_uid = "1001"
            fixture_gid = "1002"
            fixture_id_commands = (
                (
                    b'LIVE_QA_UID="$(/usr/bin/id -u oldsparky-liveqa)"',
                    f'LIVE_QA_UID="{fixture_uid}"'.encode("ascii"),
                ),
                (
                    b'LIVE_QA_GID="$(/usr/bin/id -g oldsparky-liveqa)"',
                    f'LIVE_QA_GID="{fixture_gid}"'.encode("ascii"),
                ),
            )
            for source, replacement in fixture_id_commands:
                self.assertEqual(wrapper_bytes.count(source), 1)
                wrapper_bytes = wrapper_bytes.replace(source, replacement)
            wrapper_path.write_bytes(
                wrapper_bytes.replace(fixture_runtime, b'QA_PYTHON="/usr/bin/true"')
            )
            os.chmod(wrapper_path, 0o500)
            (install_root / "runtime" / "web").mkdir(mode=0o755, parents=True)

            (tools / "platform_live_qa_guard.py").write_text(
                "import json, os, re, shutil, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "with Path(os.environ['TRACE_PATH']).open('a', encoding='utf-8') as f:\n"
                "    f.write(json.dumps(args) + '\\n')\n"
                "command = args[0] if args else ''\n"
                "if command == 'runtime-root': print(os.environ['ENGINE_ROOT'])\n"
                "elif command == 'sandbox-path': print(os.environ['SANDBOX_PATH'])\n"
                "elif command == 'prepare-root-state':\n"
                "    Path(os.environ['STATE_PATH']).mkdir(mode=0o700, exist_ok=True); print(os.environ['STATE_PATH'])\n"
                "elif command == 'validate-recovery': print(os.environ['MARKER'])\n"
                "elif command == 'prepare-browser-gate':\n"
                "    gate=Path(os.environ['GATE_PATH']); (gate/'home/.cache').mkdir(mode=0o700, parents=True, exist_ok=True); (gate/'tmp').mkdir(mode=0o700, exist_ok=True); print(gate)\n"
                "elif command == 'prepare-runtime-mounts': print(os.environ['MOUNT_RECORD'])\n"
                "elif command == 'capture-qa-browser-child':\n"
                "    print('LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=browser_launch_error stdout_bytes=0 stderr_bytes=0 truncated=false child_exit=1')\n"
                "elif command == 'emit-public-browser-counts':\n"
                "    values={args[i]:args[i+1] for i in range(len(args)-1) if args[i].startswith('--')}\n"
                "    print('LIVE_BROWSER_COUNTS schema=1 run_status=failed logical_total=0 logical_pass=0 logical_fail=0 logical_expected_fail=0 logical_flaky=0 logical_skip=0 logical_interrupted=0 attempt_total=0 attempt_pass=0 attempt_fail=0 attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 source_sha=%s app_sha=%s marker_sha256=%s' % (values['--source-sha'], values['--app-sha'], values['--marker-sha256']))\n"
                "elif command == 'remove-root-state': shutil.rmtree(os.environ['STATE_PATH'])\n"
                "elif command in {'assert-lock','preflight','merge-browser-inventory','remove-browser-gate','cleanup-runtime-mounts'}: pass\n"
                "else: raise SystemExit(93)\n",
                encoding="utf-8",
            )
            (tools / "platform_safe_env_exec.py").write_text(
                "import sys\n"
                "args=sys.argv[1:]\n"
                "if args == ['validate-runtime']: raise SystemExit(0)\n"
                "if args == ['print-public-value','PLATFORM_WEB_ORIGIN']: print('https://old-sparky.com'); raise SystemExit(0)\n"
                "if args == ['print-public-value','PLATFORM_ENVIRONMENT']: print('production'); raise SystemExit(0)\n"
                "if 'prepare-browser-sessions' in args: print('fixture-marker')\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/usr/bin/bash", str(wrapper_path)],
                cwd="/",
                env={
                    "PATH": "/usr/bin:/bin",
                    "TRACE_PATH": str(trace_path),
                    "ENGINE_ROOT": str(engine_root),
                    "SANDBOX_PATH": sandbox_path,
                    "STATE_PATH": str(state_path),
                    "GATE_PATH": str(gate_path),
                    "MARKER": marker,
                    "MOUNT_RECORD": f"{source_sha}\t{provider_sha}\tdeadbeef\t123:456\t123:456",
                    "PLATFORM_LIVE_QA_INSTALL_ROOT": str(install_root),
                    "PLATFORM_LIVE_QA_TARGET_SHA": source_sha,
                    "PLATFORM_LIVE_QA_RUNNER_SHA": runner_sha,
                    "PLATFORM_APP_DIR": "/opt/oldsparky/platform",
                    "PLATFORM_LIVE_CSP_QA_BUNDLE": "/root/private-test-bundle.json",
                    "PLATFORM_LIVE_QA_LOCK_FD": "9",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=browser_launch_error",
                result.stdout,
            )
            self.assertIn("LIVE_BROWSER_COUNTS schema=1 run_status=failed", result.stdout)
            trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            capture = next(args for args in trace if args[0] == "capture-qa-browser-child")
            self.assertIn(f"--property=BindReadOnlyPaths={install_root}/runtime:{suite_alias}", capture)
            self.assertIn(f"--property=BindReadOnlyPaths={engine_root}:{engine_alias}", capture)
            self.assertIn(f"--uid={fixture_uid}", capture)
            self.assertIn(f"--gid={fixture_gid}", capture)
            self.assertIn(f"--working-directory={suite_alias}/web", capture)
            self.assertIn(f"NODE_PATH={engine_alias}/web/node_modules", capture)
            self.assertIn(f"PATH={engine_alias}/node/bin:/usr/bin:/bin", capture)
            self.assertIn(f"PLATFORM_LIVE_USER_QA_UID={fixture_uid}", capture)
            self.assertIn(
                f"{engine_alias}/web/node_modules/@playwright/test/cli.js", capture
            )
            self.assertIn(f"--config={suite_alias}/web/playwright.live.config.ts", capture)
            self.assertIn(
                f"{suite_alias}/web/tests/smoke/live-user-journey.spec.ts", capture
            )
            self.assertIn(f"CHROME_DEVEL_SANDBOX={engine_alias}/browsers/chromium-1228/chrome-linux64/chrome_sandbox", capture)
            counts = next(args for args in trace if args[0] == "emit-public-browser-counts")
            self.assertIn(runner_sha, counts)
            self.assertIn(source_sha, counts)
            self.assertIn(marker_digest, counts)
            self.assertIn("remove-root-state", [args[0] for args in trace])
            self.assertFalse(state_path.exists())
        finally:
            if install_root.exists():
                shutil.rmtree(install_root)
            shutil.rmtree(temp_root)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned runtime mapping contract")
    def test_runtime_mount_alias_collision_and_changed_identity_fail_closed(self) -> None:
        source_sha = "a" * 40
        provider_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            base = Path(temporary)
            cache = base / "cache"
            cache.mkdir(mode=0o755)
            gate_root = base / "run"
            gate_root.mkdir(mode=0o711)
            gate = gate_root / "public-live-qa.abcdefgh"
            gate.mkdir(mode=0o700)
            os.chown(gate, 12345, 12345)
            engine_root = cache / provider_sha / "runtime"
            engine_root.parent.mkdir(mode=0o755)
            engine_root.mkdir(mode=0o555)
            pointer_target = base / "active-payload"
            pointer_target.mkdir()
            active_pointer = base / "active"
            active_pointer.symlink_to(pointer_target)
            collision = cache / f"runtime-engine-{provider_sha}-abcdefgh"
            collision.mkdir(mode=0o555)
            with (
                mock.patch.object(guard, "RUNNER_CACHE_ROOT", cache),
                mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                mock.patch.object(guard, "TRUSTED_ACTIVE_POINTER", active_pointer),
                mock.patch.object(guard, "liveqa_identity", return_value=(12345, 12345)),
                mock.patch.object(
                    guard, "_validate_installed_payload_root", return_value=source_sha
                ),
                mock.patch.object(guard, "_trusted_runtime_root", return_value=engine_root),
            ):
                with self.assertRaisesRegex(guard.GuardError, "already exists"):
                    guard._runtime_mount_aliases(gate)
                self.assertFalse((cache / f"runtime-suite-{source_sha}-abcdefgh").exists())
                self.assertTrue(collision.is_dir())
                collision.rmdir()
                values = guard._runtime_mount_aliases(gate)
                suite, engine = (
                    cache / f"runtime-suite-{source_sha}-abcdefgh",
                    cache / f"runtime-engine-{provider_sha}-abcdefgh",
                )
                with self.assertRaisesRegex(guard.GuardError, "unsafe"):
                    guard._cleanup_runtime_mount_aliases(
                        *values[:3], "0:1", values[4]
                    )
                self.assertTrue(suite.is_dir())
                self.assertTrue(engine.is_dir())

    def test_runtime_mount_cleanup_requires_collected_unit_and_reaped_processes(self) -> None:
        with (
            mock.patch.object(guard, "assert_liveqa_idle") as assert_idle,
            mock.patch.object(
                guard.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["systemctl"], returncode=0, stdout="loaded\n", stderr=""
                ),
            ),
            self.assertRaisesRegex(guard.GuardError, "not been collected"),
        ):
            guard._assert_runtime_unit_collected()
        assert_idle.assert_called_once_with()

    def test_live_browser_runner_maps_all_read_only_runtime_paths_in_unit_namespace(self) -> None:
        runner = SCRIPT_PATH.with_name("platform_live_browser_qa.sh").read_text()
        child_command_start = runner.index("CHILD_COMMAND=(\n")
        child_command_end = runner.index("\n)\n", child_command_start)
        child_command = runner[child_command_start:child_command_end]
        self.assertIn("--property=BindReadOnlyPaths=$RUNTIME_SUITE_SOURCE:$RUNTIME_MOUNT_SUITE_TARGET", runner)
        self.assertIn("--property=BindReadOnlyPaths=$RUNTIME_ENGINE_SOURCE:$RUNTIME_MOUNT_ENGINE_TARGET", runner)
        self.assertIn('--working-directory="$RUNTIME_SUITE/web"', runner)
        self.assertIn("/usr/bin/env -i", child_command)
        self.assertIn('"PATH=$RUNTIME_CACHE/node/bin:/usr/bin:/bin"', child_command)
        self.assertIn('"NODE_PATH=$RUNTIME_CACHE/web/node_modules"', child_command)
        self.assertIn('"PLAYWRIGHT_BROWSERS_PATH=$RUNTIME_CACHE/browsers"', child_command)
        self.assertIn('"CHROME_DEVEL_SANDBOX=$CHROMIUM_SANDBOX"', child_command)
        self.assertIn('"$RUNTIME_CACHE/web/node_modules/@playwright/test/cli.js"', runner)
        self.assertIn('"--config=$RUNTIME_SUITE/web/playwright.live.config.ts"', runner)
        self.assertIn('"$RUNTIME_SUITE/web/tests/smoke/live-launch.spec.ts"', runner)
        self.assertIn('"HOME=$BROWSER_GATE/home"', child_command)
        self.assertIn('"TMPDIR=$BROWSER_GATE/tmp"', child_command)
        self.assertIn('"PLATFORM_QA_BROWSER_GATE_DIR=$BROWSER_GATE"', child_command)

    def test_runtime_contract_rejects_64_character_commits_end_to_end(self) -> None:
        self.assertIsNotNone(guard.COMMIT_PATTERN.fullmatch("a" * 40))
        self.assertIsNone(guard.COMMIT_PATTERN.fullmatch("a" * 64))
        self.assertIsNotNone(guard.RUNTIME_NAME_PATTERN.fullmatch(f"runtime-{'a' * 40}"))
        self.assertIsNone(guard.RUNTIME_NAME_PATTERN.fullmatch(f"runtime-{'a' * 64}"))
        with self.assertRaisesRegex(guard.GuardError, "commit is invalid"):
            guard.prepare_runtime_cache(guard.TRUSTED_PLATFORM_ROOT, "a" * 64)

    def test_standalone_retention_acquires_release_lock_before_guard_helper(
        self,
    ) -> None:
        events: list[str] = []

        @contextmanager
        def release_lock(_app_dir: Path):
            events.append("release-enter")
            yield
            events.append("release-exit")

        empty = guard.RuntimeCacheRetentionPlan((), (), (), ())

        def locked_helper(**_kwargs: object) -> guard.RuntimeCacheRetentionPlan:
            events.append("liveqa")
            return empty

        with (
            mock.patch.object(guard, "_release_operation_lock", side_effect=release_lock),
            mock.patch.object(
                guard,
                "prune_runtime_cache_release_lock_held",
                side_effect=locked_helper,
            ),
        ):
            self.assertEqual(
                guard.prune_runtime_cache(
                    apply=False,
                    keep=1,
                    root=guard.RUNNER_CACHE_ROOT,
                    app_dir=Path("/opt/oldsparky/platform"),
                ),
                empty,
            )
        self.assertEqual(events, ["release-enter", "liveqa", "release-exit"])

    @unittest.skipUnless(os.geteuid() == 0, "root-owned release lock contract")
    def test_standalone_retention_refuses_release_lock_contention(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            app_dir = Path(temporary) / "platform"
            shared = app_dir / "shared"
            shared.mkdir(parents=True)
            release_lock = lock_support.create_test_lock("liveqa-contention")
            try:
                release_lock.acquire(nonblocking=True)
                with mock.patch.object(guard, "RELEASE_LOCK_PATH", release_lock.path):
                    with self.assertRaisesRegex(
                        guard.GuardError, "another platform release operation"
                    ):
                        guard.prune_runtime_cache(
                            apply=False,
                            keep=1,
                            root=Path(temporary) / "missing-cache",
                            app_dir=app_dir,
                        )
            finally:
                release_lock.cleanup()

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_interrupted_deletion_is_reclaimed_from_a_validated_tombstone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            newest = self.make_runtime_cache(root, "a" * 40, modified_at_ns=20)
            candidate = self.make_runtime_cache(root, "b" * 40, modified_at_ns=10)
            plan = guard.build_runtime_cache_retention_plan(
                root,
                protected_commits=frozenset(),
                keep=1,
            )

            with (
                mock.patch.object(
                    guard.shutil, "rmtree", side_effect=OSError("interrupted")
                ),
                self.assertRaisesRegex(OSError, "interrupted"),
            ):
                guard.apply_runtime_cache_retention_plan(plan, root=root)

            tombstones = tuple(root.glob(".runtime-*.pruning-*"))
            self.assertEqual(len(tombstones), 1)
            self.assertFalse(candidate.exists())
            self.assertTrue(newest.exists())

            recovery_plan = guard.build_runtime_cache_retention_plan(
                root,
                protected_commits=frozenset(),
                keep=1,
            )
            self.assertEqual(
                [entry.path for entry in recovery_plan.tombstones],
                list(tombstones),
            )
            guard.apply_runtime_cache_retention_plan(recovery_plan, root=root)
            self.assertFalse(tombstones[0].exists())
            self.assertTrue(newest.exists())

    def test_isolated_python_can_load_retention_parser_without_sibling_imports(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(SCRIPT_PATH),
                "prune-runtime-cache",
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--keep", completed.stdout)
        self.assertNotIn("max-age", completed.stdout)

    def test_guard_main_refuses_nonroot_before_action(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch("sys.stderr", stderr),
        ):
            result = guard.main(["prepare-build-node"])
        self.assertEqual(result, 2)
        self.assertIn("requires root", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
