from __future__ import annotations

import json
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle
from tools import platform_workflow_remote_dispatch as dispatcher


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "platform" / "tools"
SOURCE_SHA = "d974c8b0536683d0ca8d6f1aca8331a215023fd4"


class HostToolsBundleTests(unittest.TestCase):
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

    def test_artifact_metadata_binding_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "workflow_run": {
                            "id": 99,
                            "run_attempt": 1,
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
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                )
            metadata.write_text(
                '{"id":123,"id":124,"name":"platform-host-tools-bundle-99-1",'
                '"expired":false,"workflow_run":{"id":99,"run_attempt":1,'
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
