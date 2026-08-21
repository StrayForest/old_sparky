from __future__ import annotations

from contextlib import contextmanager
import fcntl
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_guard.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_live_qa_guard_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


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

    def extract_test_zip(self, source: Path, target: Path) -> None:
        def copy_archive(**kwargs: object) -> None:
            shutil.copyfile(source, Path(kwargs["archive"]))

        with mock.patch.object(guard, "_download_exact", side_effect=copy_archive):
            guard._download_pinned_zip(
                "https://downloads.example.invalid/browser.zip",
                "0" * 64,
                source.stat().st_size,
                target,
            )

    def test_lock_path_is_machine_wide_for_different_bundles(self) -> None:
        with mock.patch.object(guard, "_validate_root_secret_parent"):
            first = guard._lock_path(Path("/root/one/bundle.json"))
            second = guard._lock_path(Path("/root/two/bundle.json"))
        self.assertEqual(first, guard.MACHINE_LOCK_PATH)
        self.assertEqual(second, guard.MACHINE_LOCK_PATH)

    def test_machine_lock_refuses_a_second_concurrent_open(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            lock_path = Path(temporary) / "liveqa.lock"
            with (
                mock.patch.object(guard, "MACHINE_LOCK_PATH", lock_path),
                mock.patch.object(guard, "_validate_root_secret_parent"),
            ):
                first = guard._open_bundle_lock(Path("/root/one/bundle.json"))
                try:
                    with self.assertRaisesRegex(guard.GuardError, "another live QA"):
                        guard._open_bundle_lock(Path("/root/two/bundle.json"))
                finally:
                    os.close(first)

    def test_locked_exec_rejects_non_trusted_wrapper(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "not a reviewed"):
            guard._validate_trusted_wrapper(
                ["/opt/oldsparky/platform/current/tools/platform_live_user_qa.sh"],
                recovery=False,
            )

    def test_recovery_exec_requires_an_exact_recovery_command(self) -> None:
        wrapper = guard.TRUSTED_TOOLS_ROOT / "platform_live_user_qa.sh"
        with self.assertRaisesRegex(guard.GuardError, "arguments are invalid"):
            guard._validate_trusted_wrapper([str(wrapper)], recovery=True)

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
            source.write_bytes(b"reviewed")
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
            with (
                mock.patch.object(guard, "MACHINE_LOCK_PATH", lock_path),
                mock.patch.object(guard, "_liveqa_cgroup_process_ids", return_value=()),
                mock.patch.object(guard, "_liveqa_process_ids", return_value=()),
                mock.patch.object(
                    guard, "_open_machine_lock", wraps=guard._open_machine_lock
                ) as opened_lock,
            ):
                plan = guard.prune_runtime_cache(
                    apply=False,
                    keep=1,
                    root=root,
                    app_dir=app_dir,
                )
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
            descriptor = os.open(base / "lock", os.O_RDWR | os.O_CREAT, 0o600)
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

    def test_runtime_retention_cli_is_dry_run_by_default(self) -> None:
        args = guard._parser().parse_args(["prune-runtime-cache"])
        self.assertFalse(args.apply)
        self.assertEqual(args.keep, 1)
        self.assertFalse(hasattr(args, "max_age_days"))

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
                    root=Path("/var/lib/oldsparky-liveqa"),
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
            descriptor = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned cache contract")
    def test_interrupted_deletion_is_reclaimed_from_a_validated_tombstone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "cache"
            root.mkdir(mode=0o755)
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
