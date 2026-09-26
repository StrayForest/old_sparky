from __future__ import annotations

import hashlib
import json
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import re
import resource
import shlex
import shutil
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle
from tools import platform_host_tools_pin as pin
from tools import platform_workflow_remote_dispatch as dispatcher
from tools.platform_verify_contract import _workflow_step_blocks


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "platform" / "tools"
SOURCE_SHA = "d974c8b0536683d0ca8d6f1aca8331a215023fd4"
PIN_SHA = "4233e3ce3395da6948192f14e50af2033774f4f0"
ARTIFACT_DIGEST = "sha256:" + "e" * 64


class HostToolsBundleTests(unittest.TestCase):
    def _current_target_sha(self) -> str:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_repository_pin_declares_installed_generation_and_closure_baseline(self) -> None:
        contract = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(contract["schema"], 1)
        self.assertEqual(contract["repository"], pin.EXPECTED_REPOSITORY)
        self.assertEqual(contract["host_tools_sha"], PIN_SHA)
        self.assertEqual(
            tuple(record["path"] for record in contract["closure"]),
            tuple(f"platform/tools/{name}" for name in bundle.HOST_TOOL_FILES),
        )

    def test_repository_pin_rejects_circular_generation_and_repository_tampering(self) -> None:
        with self.assertRaises(pin.HostToolsPinError):
            pin.resolve_pin(
                REPO_ROOT,
                target_sha="4b795dec048a9abda4577f32f36586bedfc39045",
                expected_repository=pin.EXPECTED_REPOSITORY,
            )
        with self.assertRaises(pin.HostToolsPinError):
            pin.resolve_pin(
                REPO_ROOT,
                target_sha=PIN_SHA,
                expected_repository="attacker/old_sparky",
            )

    def test_repository_pin_rejects_closure_type_path_and_digest_tampering(self) -> None:
        payload = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        for mutation in (
            lambda value: {**value, "schema": "1"},
            lambda value: {**value, "repository": "StrayForest/old_sparky/escape"},
            lambda value: {**value, "host_tools_sha": 4233},
            lambda value: {**value, "closure": "not-a-list"},
            lambda value: {
                **value,
                "closure": [
                    {**value["closure"][0], "path": "../outside.py"},
                    *value["closure"][1:],
                ],
            },
            lambda value: {
                **value,
                "closure": [
                    {**value["closure"][0], "sha256": "0" * 64},
                    *value["closure"][1:],
                ],
            },
        ):
            with self.subTest(mutation=mutation):
                candidate = mutation(payload)
                with patch.object(pin, "_read_pin", return_value=candidate):
                    with self.assertRaises(pin.HostToolsPinError):
                        pin.resolve_pin(
                            REPO_ROOT,
                            target_sha=self._current_target_sha(),
                            expected_repository=pin.EXPECTED_REPOSITORY,
                        )

    def test_repository_pin_path_and_duplicate_key_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "platform" / "contracts").mkdir(parents=True)
            pin_path = root / pin.PIN_RELATIVE_PATH
            pin_path.write_text('{"schema":1,"schema":1}\n', encoding="utf-8")
            with self.assertRaises(pin.HostToolsPinError):
                pin._read_pin(root)
            pin_path.unlink()
            pin_path.symlink_to(REPO_ROOT / pin.PIN_RELATIVE_PATH)
            with self.assertRaises(pin.HostToolsPinError):
                pin._read_pin(root)

    def test_pin_bump_uses_prior_generation_and_rejects_unpinned_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "repo"
            cloned = subprocess.run(
                ["git", "clone", "--no-local", str(REPO_ROOT), str(fixture)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            commands = (
                ["remote", "set-url", "origin", "https://github.com/StrayForest/old_sparky.git"],
                ["config", "user.email", "host-tools-pin-test@example.invalid"],
                ["config", "user.name", "Host tools pin test"],
            )
            for command in commands:
                configured = subprocess.run(
                    ["git", "-C", str(fixture), *command],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(configured.returncode, 0, configured.stderr)

            def git(*arguments: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(fixture), *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            base_available = subprocess.run(
                ["git", "-C", str(fixture), "cat-file", "-e", f"{PIN_SHA}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode == 0
            if base_available:
                subprocess.run(
                    ["git", "-C", str(fixture), "checkout", "--detach", PIN_SHA],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                generation_base = PIN_SHA
            else:
                # The CI checkout is intentionally shallow at the PR merge
                # commit. Build a local base generation when that exact
                # installed commit object is unavailable; the lifecycle
                # assertions below remain identical and network-free.
                for path in (
                    fixture / pin.PIN_RELATIVE_PATH,
                    fixture / "platform/tools/platform_host_tools_pin.py",
                ):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                git("add", "-u")
                git("commit", "-m", "fixture host-tools base generation")
                generation_base = git("rev-parse", "HEAD")
            contract_path = fixture / pin.PIN_RELATIVE_PATH
            contract_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(
                (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
            )
            payload["host_tools_sha"] = generation_base
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            shutil.copyfile(
                REPO_ROOT / "platform/tools/platform_host_tools_pin.py",
                fixture / "platform/tools/platform_host_tools_pin.py",
            )

            git(
                "add",
                "platform/contracts/host_tools_pin.json",
                "platform/tools/platform_host_tools_pin.py",
            )
            git("commit", "-m", "add host-tools pin contract")
            baseline_target = git("rev-parse", "HEAD")
            self.assertEqual(
                pin.resolve_pin(fixture, target_sha=baseline_target),
                generation_base,
            )

            changed_file = fixture / "platform/tools/platform_storage_evidence_summary.py"
            changed_file.write_bytes(
                changed_file.read_bytes() + b"\n# intentional host-tools bump fixture\n"
            )
            git("add", str(changed_file.relative_to(fixture)))
            git("commit", "-m", "change host-tools closure")
            generation_a = git("rev-parse", "HEAD")
            with self.assertRaises(pin.HostToolsPinError):
                pin.resolve_pin(fixture, target_sha=generation_a)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            payload["host_tools_sha"] = generation_a
            changed_record = next(
                record
                for record in payload["closure"]
                if record["path"] == "platform/tools/platform_storage_evidence_summary.py"
            )
            changed_record["sha256"] = hashlib.sha256(
                changed_file.read_bytes()
            ).hexdigest()
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            git("add", "platform/contracts/host_tools_pin.json")
            git("commit", "-m", "pin provisioned host-tools generation")
            pin_commit_b = git("rev-parse", "HEAD")
            self.assertEqual(
                git("diff-tree", "--no-commit-id", "--name-only", "-r", pin_commit_b),
                "platform/contracts/host_tools_pin.json",
            )
            self.assertEqual(pin.resolve_pin(fixture, target_sha=pin_commit_b), generation_a)

            changed_again = fixture / "platform/tools/platform_validate_edge_policy.py"
            changed_again.write_bytes(
                changed_again.read_bytes() + b"\n# unpinned closure drift fixture\n"
            )
            git("add", str(changed_again.relative_to(fixture)))
            git("commit", "-m", "change host-tools closure without pin")
            with self.assertRaises(pin.HostToolsPinError):
                pin.resolve_pin(fixture, target_sha=git("rev-parse", "HEAD"))

    def test_workflow_keeps_application_and_host_generation_sha_separate(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        host_build = workflow.split("  build-host-tools:", 1)[1].split(
            "  host-capability-preflight:", 1
        )[0]
        host_build_step = host_build.split(
            "      - name: Build and verify deterministic host-tools bundle", 1
        )[1].split("      - name: Publish exact host-tools bundle", 1)[0]
        host_preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        preflight = workflow.split("  preflight:", 1)[1].split("  production:", 1)[0]
        production = workflow.split("  production:", 1)[1]
        self.assertIn("platform_host_tools_pin.py", host_build)
        self.assertIn("ref: ${{ steps.resolve_host_tools_pin.outputs.host_tools_sha }}", host_build)
        self.assertIn('--source-sha "$HOST_TOOLS_SHA"', host_build_step)
        self.assertNotIn('--source-sha "$TARGET_SHA"', host_build_step)
        self.assertNotIn("/opt/oldsparky/platform/shared/host-tools/${{ github.sha }}", workflow)
        self.assertIn("source_sha=$HOST_TOOLS_SHA", host_preflight)
        self.assertIn("generation=$HOST_TOOLS_SHA", host_preflight)
        self.assertIn("needs.host-capability-preflight.outputs.host_tools_sha", preflight)
        self.assertIn("needs.host-capability-preflight.outputs.host_tools_sha", production)

        security = (REPO_ROOT / ".github/workflows/platform-security.yml").read_text(
            encoding="utf-8"
        )
        verification = security.split("  verification-contract:", 1)[1].split(
            "  release-runtime:", 1
        )[0]
        self.assertIn("fetch-depth: 0", verification)
        self.assertIn("ref: ${{ github.sha }}", verification)
        self.assertIn(
            "name: Resolve and verify canonical host-tools pin against full target history",
            verification,
        )
        self.assertIn("platform/tools/platform_host_tools_pin.py resolve", verification)
        self.assertIn('--target-sha "$TARGET_SHA"', verification)
        self.assertIn('--expected-repository "$EXPECTED_REPOSITORY"', verification)
        self.assertIn("test ! -e \"$pin_output\"", verification)

    def _source_fixture(self, root: Path) -> Path:
        source_root = root / "source"
        tools = source_root / "platform" / "tools"
        tools.mkdir(parents=True)
        for name in bundle.HOST_TOOL_FILES:
            destination = tools / name
            shutil.copyfile(TOOLS_ROOT / name, destination)
            os.chmod(destination, 0o755)
        return source_root

    def test_bundle_is_deterministic_and_separates_closures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            first = root / "first.zip"
            second = root / "second.zip"
            first_summary = bundle.build_bundle(source, SOURCE_SHA, first)
            bundle.build_bundle(source, SOURCE_SHA, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            manifest = first_summary["manifest"]
            self.assertIsInstance(manifest, dict)
            self.assertEqual(manifest["source_sha"], SOURCE_SHA)
            self.assertEqual(
                manifest["components"],
                {key: list(value) for key, value in bundle.COMPONENT_FILES.items()},
            )
            self.assertNotIn("platform_release_deploy.sh", bundle.HOST_TOOL_FILES)
            self.assertNotIn("platform_run_api.sh", bundle.HOST_TOOL_FILES)
            contract = root / "contract"
            bundle.write_contract_files(first_summary, contract)
            self.assertIn(
                "platform_configure_shared_env.py", (contract / "files.sha256").read_text()
            )
            self.assertEqual((contract / "source_sha").read_text(), f"{SOURCE_SHA}\n")
            self.assertEqual(
                len(first_summary["manifest"]["files"]), len(bundle.HOST_TOOL_FILES) + 1
            )
            self.assertEqual(
                len((contract / "files.sha256").read_text().splitlines()),
                len(bundle.HOST_TOOL_FILES) + 1,
            )
            for contract_file in (
                "manifest.sha256",
                "capabilities.sha256",
                "files.sha256",
                "files.modes",
                "source_sha",
                "toolset_version",
            ):
                metadata = (contract / contract_file).lstat()
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertFalse(any(contract.glob(".*.tmp")))

    def test_generated_mode_contract_passes_workflow_consumer(self) -> None:
        """Keep files.modes aligned with the production shell consumer.

        The manifest intentionally uses JSON numeric Unix modes (292/365),
        while the workflow compares the text sidecar with `stat -c %a`
        (444/555).  Generate the real bundle/manifest/contract and execute
        the exact mode-validation loop extracted from the workflow so either
        representation cannot drift silently.
        """

        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        start_marker = '          while read -r expected_mode expected_path; do'
        end_marker = '          done < "$contract_dir/files.modes"'
        start = preflight.index(start_marker)
        end = preflight.index(end_marker, start) + len(end_marker)
        mode_consumer = textwrap.dedent(preflight[start:end])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            summary = bundle.build_bundle(source, SOURCE_SHA, archive)
            verified = bundle.verify_bundle(archive, expected_source_sha=SOURCE_SHA)
            contract = root / "contract"
            bundle.write_contract_files(verified, contract)

            manifest = summary["manifest"]
            self.assertIsInstance(manifest, dict)
            modes = (contract / "files.modes").read_text(encoding="ascii").splitlines()
            self.assertIn("444  capabilities.txt", modes)
            self.assertTrue(
                all(
                    line.startswith("555  ")
                    for line in modes
                    if not line.endswith("  capabilities.txt")
                )
            )
            self.assertEqual(
                next(
                    record["mode"]
                    for record in manifest["files"]
                    if record["path"] == "capabilities.txt"
                ),
                bundle.DATA_MODE,
            )
            self.assertEqual(
                next(
                    record["mode"]
                    for record in manifest["files"]
                    if record["path"] == bundle.HOST_TOOL_FILES[0]
                ),
                bundle.EXECUTABLE_MODE,
            )

            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'set -euo pipefail\ncontract_dir="$1"\n{mode_consumer}',
                    "mode-check",
                    str(contract),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manifest_and_archive_have_exact_closed_member_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            summary = bundle.build_bundle(source, SOURCE_SHA, archive)
            with zipfile.ZipFile(archive) as opened:
                self.assertEqual(len(opened.infolist()), len(bundle.HOST_TOOL_FILES) + 2)
                self.assertEqual(
                    {info.filename for info in opened.infolist()},
                    {
                        f"{bundle.MEMBER_ROOT}/{name}"
                        for name in (*bundle.HOST_TOOL_FILES, "capabilities.txt", "manifest.json")
                    },
                )
            self.assertEqual(
                [record["path"] for record in summary["manifest"]["files"]],
                sorted(record["path"] for record in summary["manifest"]["files"]),
            )

    def test_archive_member_set_accepts_shuffled_order_but_keeps_canonical_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            original = root / "original.zip"
            bundle.build_bundle(source, SOURCE_SHA, original)
            shuffled = root / "shuffled.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                shuffled, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                infos = list(reversed(source_zip.infolist()))
                for info in infos:
                    target_zip.writestr(info, source_zip.read(info))
            verified = bundle.verify_bundle(shuffled, expected_source_sha=SOURCE_SHA)
            paths = {record["path"] for record in verified["manifest"]["files"]}
            self.assertIn("platform_configure_shared_env.py", paths)
            self.assertIn("platform_update_cloudflare_ips.py", paths)

    def test_tampered_archive_and_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            original = root / "original.zip"
            bundle.build_bundle(source, SOURCE_SHA, original)
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                for info in source_zip.infolist():
                    payload = source_zip.read(info)
                    if info.filename.endswith("/manifest.json"):
                        manifest = json.loads(payload)
                        manifest["source_sha"] = "a" * 40
                        payload = (
                            json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        ).encode()
                    target_zip.writestr(info, payload)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(tampered, expected_source_sha=SOURCE_SHA)

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                duplicate, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                for info in source_zip.infolist():
                    target_zip.writestr(info, source_zip.read(info))
                info = source_zip.infolist()[0]
                target_zip.writestr(info, source_zip.read(info))
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(duplicate, expected_source_sha=SOURCE_SHA)

    def test_manifest_numeric_fields_reject_bool_float_and_string_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            bundle.build_bundle(source, SOURCE_SHA, archive)
            with zipfile.ZipFile(archive) as source_zip:
                infos = list(source_zip.infolist())
                members = {info.filename: source_zip.read(info) for info in source_zip.infolist()}

            for field, values in (
                ("schema", (True, 1.0, "1")),
                ("limits", ({"max_bundle_bytes": True, "max_file_bytes": 512 * 1024, "max_file_count": 13},
                             {"max_bundle_bytes": 4 * 1024 * 1024, "max_file_bytes": 512 * 1024.0, "max_file_count": 13},
                             {"max_bundle_bytes": 4 * 1024 * 1024, "max_file_bytes": 512 * 1024, "max_file_count": "13"})),
            ):
                for value in values:
                    with self.subTest(field=field, value=value):
                        manifest = json.loads(members[f"{bundle.MEMBER_ROOT}/manifest.json"])
                        manifest[field] = value
                        tampered = root / f"tampered-{field}-{len(str(value))}.zip"
                        with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as target:
                            for info in infos:
                                payload = members[info.filename]
                                if info.filename.endswith("/manifest.json"):
                                    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
                                target.writestr(info, payload)
                        with self.assertRaises(bundle.HostToolsBundleError):
                            bundle.verify_bundle(tampered, expected_source_sha=SOURCE_SHA)

    def test_source_links_and_special_files_fail_closed(self) -> None:
        for mutation in ("symlink", "hardlink", "fifo"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._source_fixture(root)
                target = source / "platform" / "tools" / bundle.HOST_TOOL_FILES[0]
                if mutation == "symlink":
                    target.unlink()
                    target.symlink_to("platform_workflow_input_guard.py")
                elif mutation == "hardlink":
                    peer = root / "peer"
                    peer.write_bytes(target.read_bytes())
                    target.unlink()
                    os.link(peer, target)
                else:
                    target.unlink()
                    os.mkfifo(target)
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle.build_bundle(source, SOURCE_SHA, root / "bundle.zip")

    def test_staged_isolated_dispatcher_import_has_no_ambient_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "host-tools" / SOURCE_SHA
            staged.mkdir(parents=True)
            shutil.copyfile(
                TOOLS_ROOT / "platform_workflow_remote_dispatch.py",
                staged / "platform_workflow_remote_dispatch.py",
            )
            shutil.copyfile(
                TOOLS_ROOT / "platform_workflow_input_guard.py",
                staged / "platform_workflow_input_guard.py",
            )
            decoy = root / "decoy"
            decoy.mkdir()
            (decoy / "platform_workflow_input_guard.py").write_text(
                "raise RuntimeError('ambient guard loaded')\n", encoding="utf-8"
            )
            environment = {
                "HOME": str(root / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(decoy),
            }
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    str(staged / "platform_workflow_remote_dispatch.py"),
                    "host-capabilities",
                ],
                cwd=decoy,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertNotIn("ModuleNotFoundError", completed.stderr)
            self.assertNotIn("ambient guard loaded", completed.stderr)

            # Exercise the real installed bundle contour in a writable
            # staging tree. The dispatcher is still invoked with the exact
            # production flags, while metadata checks are patched only in
            # this child so the test does not touch /opt or require root.
            source = self._source_fixture(root)
            archive = root / "platform-host-tools-bundle.zip"
            bundle.build_bundle(source, SOURCE_SHA, archive)
            installed = root / "shared" / "host-tools" / SOURCE_SHA
            installed.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive) as source_zip:
                source_zip.extractall(installed.parent.parent.parent)
            extracted = installed.parent.parent.parent / bundle.MEMBER_ROOT
            extracted.rename(installed)
            for path in installed.iterdir():
                os.chmod(path, 0o755 if path.name in bundle.HOST_TOOL_FILES else 0o644)
            os.chmod(installed, 0o755)

            def inventory() -> dict[str, bytes]:
                return {
                    str(path.relative_to(installed)): path.read_bytes()
                    for path in installed.rglob("*")
                    if path.is_file()
                }

            before = inventory()
            child = """
from pathlib import Path
import sys
import types

dispatcher_path = Path(sys.argv[1])
host_tools_root = Path(sys.argv[2])
sys.path.insert(0, str(dispatcher_path.parent))
module = types.ModuleType("staged_dispatcher")
module.__file__ = str(dispatcher_path)
exec(compile(dispatcher_path.read_text(encoding="utf-8"), str(dispatcher_path), "exec"), module.__dict__)
module.HOST_TOOLS_ROOT = host_tools_root
module.ACTIVE_TOOLS_DIR = dispatcher_path.parent
module._trusted_generation = lambda: True
module._trusted_host_helper = lambda _path: True
module._trusted_data = lambda _path: True
raise SystemExit(module.main(["host-capabilities"]))
"""

            def limited_run(*flags: str) -> subprocess.CompletedProcess[str]:
                def limit_file_size() -> None:
                    resource.setrlimit(resource.RLIMIT_FSIZE, (512, 512))

                environment = os.environ.copy()
                # The defense environment is supplemental; the immutable
                # dispatcher must still see the literal interpreter flag.
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                return subprocess.run(
                    [
                        "/usr/bin/python3.12",
                        *flags,
                        "-c",
                        child,
                        str(installed / "platform_workflow_remote_dispatch.py"),
                        str(installed.parent),
                    ],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    preexec_fn=limit_file_size,
                )

            expected = (
                f"HOST_TOOLS schema=1 source_sha={SOURCE_SHA} generation={SOURCE_SHA} "
                "dispatcher=2 artifact_prepare=2 supervisor=2 input_guard=1 "
                "python_isolated=1 python_bytecode_disabled=1\n"
            )
            bounded = limited_run("-I", "-B")
            self.assertEqual(bounded.returncode, 0, bounded.stderr)
            self.assertEqual(bounded.stdout, expected)
            self.assertEqual(inventory(), before)
            self.assertFalse(any(path.name == "__pycache__" for path in installed.rglob("*")))
            self.assertFalse(any(path.suffix == ".pyc" for path in installed.rglob("*")))

            # Omitting -B must be rejected before the sibling import can
            # create a truncated pyc under the same tight file-size limit.
            without_bytecode_flag = limited_run("-I")
            self.assertEqual(without_bytecode_flag.returncode, 2)
            self.assertEqual(inventory(), before)
            self.assertFalse(any(path.name == "__pycache__" for path in installed.rglob("*")))
            self.assertFalse(any(path.suffix == ".pyc" for path in installed.rglob("*")))

            extra_directory = installed / "__pycache__"
            extra_directory.mkdir()
            self.assertEqual(limited_run("-I", "-B").returncode, 2)
            extra_directory.rmdir()
            self.assertEqual(inventory(), before)

    def test_artifact_metadata_binding_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.json"
            archive = Path(temporary) / "artifact.zip"
            archive.write_bytes(b"zip")
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "run_attempt": 1,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            bundle.verify_artifact_metadata(
                metadata,
                artifact_id="123",
                artifact_name="platform-host-tools-bundle-99-1",
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                expected_branch="dev",
                artifact_digest=ARTIFACT_DIGEST,
                archive_path=archive,
            )
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            # GitHub's artifact API omits workflow_run.run_attempt.  The
            # artifact envelope remains valid; the dedicated attempt payload
            # below is the authoritative source for that field.
            bundle.verify_artifact_metadata(
                metadata,
                artifact_id="123",
                artifact_name="platform-host-tools-bundle-99-1",
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                expected_branch="dev",
                artifact_digest=ARTIFACT_DIGEST,
                archive_path=archive,
            )
            for invalid_attempt in (False, "1", 1.0, 2):
                invalid_payload = json.loads(metadata.read_text(encoding="utf-8"))
                invalid_payload["workflow_run"]["run_attempt"] = invalid_attempt
                metadata.write_text(json.dumps(invalid_payload), encoding="utf-8")
                with self.subTest(invalid_attempt=invalid_attempt):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "run_attempt": 1,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )
            attempt = Path(temporary) / "attempt.json"
            attempt.write_text(
                json.dumps(
                    {
                        "id": 99,
                        "run_attempt": 1,
                        "head_sha": SOURCE_SHA,
                        "head_branch": "dev",
                        "event": "workflow_dispatch",
                        "ref": None,
                        "repository": {
                            "full_name": "StrayForest/old_sparky",
                            "name": "old_sparky",
                            "owner": {"login": "StrayForest"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            bundle.verify_workflow_attempt(
                attempt,
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                repository="StrayForest/old_sparky",
                expected_branch="dev",
                expected_event="workflow_dispatch",
            )
            for missing_field in ("id", "run_attempt", "head_sha", "repository", "head_branch", "event"):
                missing_payload = json.loads(attempt.read_text(encoding="utf-8"))
                missing_payload.pop(missing_field)
                attempt.write_text(json.dumps(missing_payload), encoding="utf-8")
                with self.subTest(missing_attempt_field=missing_field):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_workflow_attempt(
                            attempt,
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            repository="StrayForest/old_sparky",
                            expected_branch="dev",
                            expected_event="workflow_dispatch",
                        )
                attempt.write_text(
                    json.dumps(
                        {
                            "id": 99,
                            "run_attempt": 1,
                            "head_sha": SOURCE_SHA,
                            "head_branch": "dev",
                            "event": "workflow_dispatch",
                            "ref": None,
                            "repository": {
                                "full_name": "StrayForest/old_sparky",
                                "name": "old_sparky",
                                "owner": {"login": "StrayForest"},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            for field, invalid in (
                ("id", 100),
                ("run_attempt", False),
                ("run_attempt", "1"),
                ("run_attempt", 1.0),
                ("head_sha", "b" * 40),
                ("head_branch", "feature"),
                ("event", "push"),
                (
                    "repository",
                    {
                        "full_name": "attacker/old_sparky",
                        "name": "old_sparky",
                        "owner": {"login": "attacker"},
                    },
                ),
            ):
                invalid_payload = json.loads(attempt.read_text(encoding="utf-8"))
                invalid_payload[field] = invalid
                attempt.write_text(json.dumps(invalid_payload), encoding="utf-8")
                with self.subTest(attempt_field=field, invalid=invalid):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_workflow_attempt(
                            attempt,
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            repository="StrayForest/old_sparky",
                            expected_branch="dev",
                            expected_event="workflow_dispatch",
                        )
                attempt.write_text(
                    json.dumps(
                        {
                            "id": 99,
                            "run_attempt": 1,
                            "head_sha": SOURCE_SHA,
                            "head_branch": "dev",
                            "event": "workflow_dispatch",
                            "ref": None,
                            "repository": {
                                "full_name": "StrayForest/old_sparky",
                                "name": "old_sparky",
                                "owner": {"login": "StrayForest"},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            attempt.write_text("{" + "x" * (bundle.MAX_FILE_BYTES + 1), encoding="utf-8")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_workflow_attempt(
                    attempt,
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    repository="StrayForest/old_sparky",
                    expected_branch="dev",
                    expected_event="workflow_dispatch",
                )
            attempt.unlink()
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_workflow_attempt(
                    attempt,
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    repository="StrayForest/old_sparky",
                    expected_branch="dev",
                    expected_event="workflow_dispatch",
                )
            metadata.write_text(
                '{"id":123,"id":124,"name":"platform-host-tools-bundle-99-1",'
                f'"expired":false,"digest":"{ARTIFACT_DIGEST}",'
                '"size_in_bytes":3,'
                '"workflow_run":{"id":99,"run_attempt":1,"head_branch":"dev",'
                f'"head_sha":"{SOURCE_SHA}"}}',
                encoding="utf-8",
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )

            valid_metadata = {
                "id": 123,
                "name": "platform-host-tools-bundle-99-1",
                "expired": False,
                "digest": ARTIFACT_DIGEST,
                "size_in_bytes": 3,
                "workflow_run": {
                    "id": 99,
                    "head_branch": "dev",
                    "head_sha": SOURCE_SHA,
                },
            }
            for missing_field in (
                "id",
                "name",
                "expired",
                "digest",
                "size_in_bytes",
                "workflow_run",
            ):
                missing_metadata = json.loads(json.dumps(valid_metadata))
                missing_metadata.pop(missing_field)
                metadata.write_text(json.dumps(missing_metadata), encoding="utf-8")
                with self.subTest(missing_artifact_field=missing_field):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )
            for field, invalid in (
                ("id", False),
                ("id", "123"),
                ("name", 123),
                ("expired", 0),
                ("expired", True),
                ("digest", "sha256:" + "f" * 64),
                ("digest", False),
                ("size_in_bytes", False),
                ("size_in_bytes", "3"),
                ("size_in_bytes", 3.0),
                ("size_in_bytes", 0),
                ("size_in_bytes", bundle.MAX_ARTIFACT_ARCHIVE_BYTES + 1),
                ("size_in_bytes", 4),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": "feature", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 100, "head_branch": "dev", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": "99", "head_branch": "dev", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": True, "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": "dev", "head_sha": False},
                ),
            ):
                invalid_metadata = json.loads(json.dumps(valid_metadata))
                invalid_metadata[field] = invalid
                metadata.write_text(json.dumps(invalid_metadata), encoding="utf-8")
                with self.subTest(artifact_field=field, invalid=invalid):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )

            metadata.write_text("{", encoding="utf-8")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )
            metadata.write_bytes(b"{" + b"x" * bundle.MAX_FILE_BYTES)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )

    def test_host_path_references_stay_inside_declared_components(self) -> None:
        supervisor = (TOOLS_ROOT / "platform_production_deploy_supervisor.sh").read_text()
        dispatcher = (TOOLS_ROOT / "platform_workflow_remote_dispatch.py").read_text()
        host_path_references = set(
            re.findall(r"\$host_tools_dir/(platform_[A-Za-z0-9_.-]+\.(?:py|sh))", supervisor)
        )
        self.assertTrue(host_path_references)
        self.assertTrue(host_path_references <= set(bundle.HOST_TOOL_FILES))
        self.assertTrue(
            all(name in supervisor for name in bundle.PRODUCTION_DEPLOY_CONTROL_FILES)
        )
        self.assertTrue(set(bundle.PREPARE_ARTIFACT_FILES) <= set(bundle.HOST_TOOL_FILES))
        self.assertNotIn("platform_release_deploy.sh", bundle.HOST_TOOL_FILES)
        self.assertNotIn("platform_run_api.sh", bundle.HOST_TOOL_FILES)
        self.assertIn("platform_prepare_artifact_dir.py", dispatcher)

    def test_declared_host_tools_are_the_recursive_static_runtime_closure(self) -> None:
        names = set(bundle.HOST_TOOL_FILES)
        tools = {name: (TOOLS_ROOT / name).read_text(encoding="utf-8") for name in names}
        # Ignore the supervisor's metadata-only helper inventory.  Dependencies
        # must instead be discovered from fixed host-tools path expressions and
        # imports; candidate/runtime paths are intentionally outside this set.
        supervisor = tools["platform_production_deploy_supervisor.sh"]
        supervisor = re.sub(r"for host_helper in \\\n.*?done\n", "", supervisor, flags=re.DOTALL)
        tools["platform_production_deploy_supervisor.sh"] = supervisor
        reference = re.compile(
            r"(?:\$host_tools_dir/|\$SCRIPT_DIR/|ACTIVE_TOOLS_DIR\s*/\s*['\"]|"
            r"with_name\(['\"]|from\s+(?:\.\s*)?)(?P<name>"
            r"platform_[A-Za-z0-9_.-]+\.(?:py|sh))"
        )
        python_import = re.compile(
            r"from\s+(?:\.\s*)?(?P<module>platform_[A-Za-z0-9_]+)\s+import"
        )
        boundary_allowlist = {
            # Candidate/runtime or separately provisioned operator helpers.
            "platform_release_deploy.sh",
            "platform_run_api.sh",
            "platform_run_worker.sh",
            "platform_run_web.sh",
            "platform_run_alembic.sh",
            "platform_deploy_smoke.py",
            "platform_backup_restore_drill.py",
            # Dispatcher-owned external workflows intentionally outside this bundle.
            "platform_production_external_fixture_qa.sh",
            "platform_production_retained_load_cleanup_qa.sh",
            "platform_live_launch_supervisor.sh",
            "platform_live_user_qa_dispatch.py",
            "platform_live_launch_trusted.sh",
        }

        def build_graph(contents: dict[str, str]) -> dict[str, set[str]]:
            return {
                name: (
                    {match.group("name") for match in reference.finditer(text)}
                    | {f"{match.group('module')}.py" for match in python_import.finditer(text)}
                )
                for name, text in contents.items()
            }

        graph = build_graph(tools)
        discovered = set().union(*graph.values())
        local_tools = {path.name for path in TOOLS_ROOT.glob("platform_*")}
        self.assertTrue(discovered <= local_tools)
        self.assertEqual(discovered - names - boundary_allowlist, set())
        reachable = set()
        frontier = {"platform_workflow_remote_dispatch.py", "platform_production_deploy_supervisor.sh"}
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            frontier.update((graph[name] & names) - reachable)
        self.assertEqual(reachable, names)
        self.assertNotIn("platform_release_restore_runtime.sh", names)

        mutated = dict(tools)
        mutated["platform_release_preflight.sh"] += (
            '\n"$SCRIPT_DIR/platform_unlisted_local.py"\n'
        )
        mutated_discovered = set().union(*build_graph(mutated).values())
        self.assertIn("platform_unlisted_local.py", mutated_discovered - names)
        self.assertNotEqual(mutated_discovered - names - boundary_allowlist, set())

    def test_remote_integrity_block_processes_every_digest_and_mode_sidecar_row(self) -> None:
        """Execute the active YAML block against an SSH that consumes stdin.

        The sidecars contain one row for each of the 13 host helpers plus
        ``capabilities.txt``.  A remote SSH command without ``-n`` inherits
        the sidecar as stdin and steals rows from the enclosing ``while``;
        this fixture therefore fails on the vulnerable block and passes only
        when the read-only SSH array is detached from runner stdin.
        """

        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        integrity_steps = [
            step
            for step in _workflow_step_blocks(preflight)
            if "- name: Validate root SSH identity and installed generation" in step
        ]
        self.assertEqual(len(integrity_steps), 1)
        run_marker = "        run: |\n"
        self.assertIn(run_marker, integrity_steps[0])
        shell_block = integrity_steps[0].split(run_marker, 1)[1]
        shell_block = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in shell_block.splitlines()
        )
        self.assertIn("remote=(ssh -n ", shell_block)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "host-tools.zip"
            summary = bundle.build_bundle(source, SOURCE_SHA, archive)
            verified = bundle.verify_bundle(archive, expected_source_sha=SOURCE_SHA)
            self.assertIsInstance(summary["manifest"], dict)

            runner_temp = root / "runner-temp"
            contract = runner_temp / "host-tools-download" / "contract"
            contract.mkdir(parents=True)
            bundle.write_contract_files(verified, contract)

            unpacked = root / "remote-unpacked"
            unpacked.mkdir()
            with zipfile.ZipFile(archive) as bundle_zip:
                bundle_zip.extractall(unpacked)
            generation_root = unpacked / "platform-host-tools"
            self.assertTrue(generation_root.is_dir())
            os.chmod(generation_root, 0o555)
            for member in generation_root.iterdir():
                os.chmod(
                    member,
                    0o444
                    if member.name in {"capabilities.txt", "manifest.json"}
                    else 0o555,
                )

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            ssh_log = root / "ssh.log"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                """#!/usr/bin/env python3
from pathlib import Path
import hashlib
import os
import sys

if "-n" not in sys.argv[1:]:
    sys.stdin.readline()
arguments = sys.argv[1:]
destination = next(
    (index for index, argument in enumerate(arguments) if "@" in argument),
    None,
)
if destination is None:
    raise SystemExit("fake SSH destination is missing")
command = arguments[destination + 1 :]
log_path = Path(os.environ["FAKE_SSH_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    log.write(" ".join(command) + "\\n")
remote_path = command[-1] if command else ""
if command[0] == "/usr/bin/id":
    print("0")
elif command[0] == "/usr/bin/test":
    pass
elif command[0] == "/usr/bin/stat":
    format_value = command[2]
    if format_value == "%a":
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"mode:{Path(remote_path).name}\\n")
        print(
            "444"
            if remote_path.endswith("/capabilities.txt")
            or remote_path.endswith("/manifest.json")
            else "555"
        )
    elif remote_path.endswith("/" + os.environ["FAKE_SOURCE_SHA"]):
        print("directory:0:0:2:555")
    else:
        mode = (
            "444"
            if remote_path.endswith("/capabilities.txt")
            or remote_path.endswith("/manifest.json")
            else "555"
        )
        print(f"regular file:0:0:1:{mode}")
elif command[0] == "/usr/bin/sha256sum":
    local_path = Path(os.environ["FAKE_GENERATION"]) / Path(remote_path).name
    digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"digest:{local_path.name}\\n")
    print(f"{digest}  {remote_path}")
elif command[0] == "/usr/bin/find":
    remote_generation = command[2]
    for local_path in sorted(Path(os.environ["FAKE_GENERATION"]).glob("*")):
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"inventory:{local_path.name}\\n")
        sys.stdout.buffer.write(
            f"{remote_generation}/{local_path.name}\\0".encode("utf-8")
        )
else:
    raise SystemExit(f"unexpected fake SSH command: {command!r}")
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            fake_keyscan = fake_bin / "ssh-keyscan"
            fake_keyscan.write_text(
                """#!/usr/bin/env python3
import os
print(f"{os.environ['FAKE_SSH_HOST']} ssh-ed25519 AAAA")
""",
                encoding="utf-8",
            )
            fake_keyscan.chmod(0o755)
            fake_keygen = fake_bin / "ssh-keygen"
            fake_keygen.write_text(
                """#!/usr/bin/env python3
print("256 SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y (ED25519)")
""",
                encoding="utf-8",
            )
            fake_keygen.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_ENV": str(root / "github-env"),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": "fixture-key",
                    "HOST_TOOLS_SHA": SOURCE_SHA,
                    "FAKE_SSH_HOST": "production.example.test",
                    "FAKE_SSH_LOG": str(ssh_log),
                    "FAKE_GENERATION": str(generation_root),
                    "FAKE_SOURCE_SHA": SOURCE_SHA,
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )

            def run_block(block: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["bash", "-c", block],
                    cwd=REPO_ROOT,
                    env=env,
                    input="",
                    capture_output=True,
                    text=True,
                    check=False,
                )

            completed = run_block(shell_block)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            expected_names = set(bundle.HOST_TOOL_FILES) | {"capabilities.txt"}
            log_lines = ssh_log.read_text(encoding="utf-8").splitlines()
            digest_records = [
                line.removeprefix("digest:")
                for line in log_lines
                if line.startswith("digest:")
            ]
            mode_records = [
                line.removeprefix("mode:")
                for line in log_lines
                if line.startswith("mode:")
            ]
            self.assertEqual(len(digest_records), len(expected_names) + 2)
            self.assertEqual(
                digest_records[: len(expected_names)], sorted(expected_names)
            )
            self.assertEqual(len(mode_records), len(expected_names))
            self.assertEqual(set(mode_records), expected_names)
            inventory_records = [
                line.removeprefix("inventory:")
                for line in log_lines
                if line.startswith("inventory:")
            ]
            expected_inventory = expected_names | {"manifest.json"}
            self.assertEqual(len(inventory_records), len(expected_inventory))
            self.assertEqual(set(inventory_records), expected_inventory)

            # Preserve the intentional stdin handoff used by the production
            # dispatcher while proving the read-only verifier is detached.
            self.assertIn('production-prepare-artifact < "$input_path"', workflow)
            self.assertIn('production-deploy < "$input_path"', workflow)

            # Re-run the active block with only the protective ``-n`` removed.
            # The adversarial fake consumes one sidecar row per SSH call, so
            # the digest count guard must fail closed before inventory.
            ssh_log.write_text("", encoding="utf-8")
            vulnerable = shell_block.replace("remote=(ssh -n ", "remote=(ssh ", 1)
            self.assertNotEqual(run_block(vulnerable).returncode, 0)
            vulnerable_digests = [
                line
                for line in ssh_log.read_text(encoding="utf-8").splitlines()
                if line.startswith("digest:")
            ]
            self.assertLess(len(vulnerable_digests), len(expected_names))

            digest_sidecar = contract / "files.sha256"
            mode_sidecar = contract / "files.modes"
            original_digest = digest_sidecar.read_text(encoding="ascii")
            original_modes = mode_sidecar.read_text(encoding="ascii")
            digest_lines = original_digest.splitlines(keepends=True)
            mode_lines = original_modes.splitlines(keepends=True)

            def assert_block_fails(
                *, digest_text: str = original_digest, mode_text: str = original_modes
            ) -> None:
                digest_sidecar.write_text(digest_text, encoding="ascii")
                mode_sidecar.write_text(mode_text, encoding="ascii")
                ssh_log.write_text("", encoding="utf-8")
                failed = run_block(shell_block)
                self.assertNotEqual(failed.returncode, 0, failed.stderr)

            malformed_digest = digest_lines[0].replace(
                digest_lines[0].split("  ", 1)[0], "not-a-digest", 1
            )
            assert_block_fails(digest_text=malformed_digest + "".join(digest_lines[1:]))
            assert_block_fails(digest_text="".join(digest_lines[:-1]))

            malformed_mode = "444  ../unexpected\n" + "".join(mode_lines[1:])
            assert_block_fails(mode_text=malformed_mode)
            assert_block_fails(mode_text="".join(mode_lines[:-1]))

    def test_remote_capability_inventory_is_nul_safe_and_exact(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        self.assertIn(
            "find -- \"$generation\" -mindepth 1 -maxdepth 1 -print0",
            preflight,
        )
        self.assertIn("base64 --decode", preflight)
        self.assertIn("mapfile -d '' -t remote_names", preflight)
        self.assertIn('test "${#remote_names[@]}" -eq "$expected_entry_count"', preflight)
        self.assertIn("seen_entries", preflight)
        self.assertNotIn("expected_members=(", preflight)
        self.assertNotIn('"$generation"/*', preflight)

        steps = _workflow_step_blocks(preflight)
        integrity_steps = [
            step
            for step in steps
            if "- name: Validate root SSH identity and installed generation" in step
        ]
        probe_steps = [
            step
            for step in steps
            if "- name: Probe immutable host dispatcher capabilities" in step
        ]
        self.assertEqual(len(integrity_steps), 1)
        self.assertEqual(len(probe_steps), 1)
        self.assertLess(steps.index(integrity_steps[0]), steps.index(probe_steps[0]))
        probe = probe_steps[0]
        self.assertEqual(
            len(
                re.findall(
                    r'^\s+"\$\{remote\[@\]\}" /usr/bin/python3\.12 -I -B '
                    r'"\$HOST_TOOLS_DISPATCHER" host-capabilities$',
                    probe,
                    re.MULTILINE,
                )
            ),
            1,
        )
        self.assertIn("/usr/bin/timeout --signal=TERM --kill-after=2s 15s", probe)
        self.assertIn("ulimit -f 1", probe)
        self.assertIn("< /dev/null", probe)
        self.assertIn(
            'expected_output="HOST_TOOLS schema=1 source_sha=$HOST_TOOLS_SHA '
            'generation=$HOST_TOOLS_SHA dispatcher=2 artifact_prepare=2 supervisor=2 '
            'input_guard=1 python_isolated=1 python_bytecode_disabled=1"',
            probe,
        )
        self.assertIn('printf \'%s\\n\' "$expected_output" | cmp -s - "$probe_output"', probe)
        self.assertNotIn("platform/tools/platform_workflow_remote_dispatch.py", probe)
        self.assertNotIn("platform_host_tools_bundle.py", probe)

        # All trusted dispatcher call sites must carry both isolation flags;
        # an isolated interpreter without -B can write a truncated pyc into
        # an immutable/root-owned generation under a tight file-size limit.
        invocation_sources = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml",
            REPO_ROOT / ".github/workflows/platform-production-external-load.yml",
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml",
            REPO_ROOT / ".github/workflows/platform-live-launch.yml",
            TOOLS_ROOT / "platform_live_user_qa_trusted.sh",
            TOOLS_ROOT / "platform_live_launch_trusted.sh",
        )
        for source_path in invocation_sources:
            lines = source_path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if "platform_workflow_remote_dispatch.py" not in line:
                    continue
                window = "\n".join(lines[max(0, index - 1) : index + 1])
                if "python3.12" not in window:
                    continue
                self.assertIn("-I -B", window, source_path.name)
            for line in lines:
                if "python3.12" in line and "HOST_TOOLS_DISPATCHER" in line:
                    self.assertIn("-I -B", line, source_path.name)
            if source_path.name in {
                "platform_live_user_qa_trusted.sh",
                "platform_live_launch_trusted.sh",
            }:
                for line in lines:
                    if '"$DISPATCHER"' in line and "python3.12" in line:
                        self.assertIn("-I -B", line, source_path.name)

        expected = set(bundle.HOST_TOOL_FILES) | {"capabilities.txt", "manifest.json"}
        safe_name = re.compile(r"^[A-Za-z0-9_.-]+$")

        def accepted(names: list[str]) -> bool:
            return (
                len(names) == len(expected)
                and all(safe_name.fullmatch(name) and name in expected for name in names)
                and len(set(names)) == len(names)
                and set(names) == expected
            )

        self.assertFalse(accepted(sorted(expected | {".unexpected"})))
        self.assertFalse(accepted(sorted(expected | {"nested/child"})))
        self.assertFalse(accepted(sorted(expected | {"symlink"})))
        self.assertFalse(accepted(sorted(expected | {"bad\nname"})))
        self.assertTrue(accepted(sorted(expected)))

    def test_remote_inventory_shell_fixture_carries_dotfiles_and_extras_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "generation"
            generation.mkdir()
            (generation / "manifest.json").write_text("{}\n", encoding="ascii")
            (generation / "capabilities.txt").write_text("\n", encoding="ascii")
            (generation / ".unexpected").write_text("extra\n", encoding="ascii")
            remote_command = (
                "/usr/bin/find -- "
                + shlex.quote(str(generation))
                + " -mindepth 1 -maxdepth 1 -print0"
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", remote_command],
                check=True,
                capture_output=True,
            )
            remote_paths = completed.stdout.rstrip(b"\0").split(b"\0")
            names = [
                path.decode("utf-8").removeprefix(f"{generation}/")
                for path in remote_paths
            ]
            self.assertIn(".unexpected", names)
            expected = {"manifest.json", "capabilities.txt"}
            self.assertNotEqual(set(names), expected)
            self.assertEqual(len(names), 3)

    def test_host_capability_probe_checks_closed_generation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_root = root / "shared" / "host-tools"
            generation = host_root / SOURCE_SHA
            generation.mkdir(parents=True)
            os.chmod(host_root, 0o755)
            for name in bundle.HOST_TOOL_FILES:
                member = generation / name
                member.write_text("#!/usr/bin/python3\n", encoding="ascii")
                os.chmod(member, 0o555)
            for name in ("manifest.json", "capabilities.txt"):
                member = generation / name
                member.write_text("placeholder\n", encoding="ascii")
                os.chmod(member, 0o444)
            os.chmod(generation, 0o555)

            real_lstat = Path.lstat

            def root_owned_lstat(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path == generation or path.parent == generation:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=metadata.st_nlink,
                    )
                return metadata

            output = StringIO()
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])), \
                redirect_stdout(output):
                self.assertEqual(dispatcher._host_capabilities(), 0)
            self.assertRegex(output.getvalue(), r"^HOST_TOOLS schema=1 source_sha=[0-9a-f]{40} ")
            self.assertNotIn(str(generation), output.getvalue())
            os.chmod(generation / bundle.HOST_TOOL_FILES[-1], 0o554)
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])):
                self.assertEqual(dispatcher._host_capabilities(), 2)


if __name__ == "__main__":
    unittest.main()
