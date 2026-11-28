from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import hashlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock
from uuid import uuid4

from tests import platform_chromium_sandbox_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_runtime_install.py"
SPEC = importlib.util.spec_from_file_location("platform_live_qa_runtime_install_tested", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
ORIGINAL_VALIDATE_RUNTIME_SOURCE = runtime._validate_runtime_source
ORIGINAL_VALIDATE_PAYLOAD = runtime._validate_payload
SAFE_ENV_SCRIPT = SCRIPT.with_name("platform_safe_env_exec.py")
SAFE_ENV_SPEC = importlib.util.spec_from_file_location(
    "platform_safe_env_exec_runtime_install_tested", SAFE_ENV_SCRIPT
)
assert SAFE_ENV_SPEC is not None and SAFE_ENV_SPEC.loader is not None
safe_env = importlib.util.module_from_spec(SAFE_ENV_SPEC)
SAFE_ENV_SPEC.loader.exec_module(safe_env)
DISPATCH_SPEC = importlib.util.spec_from_file_location(
    "platform_live_user_qa_dispatch_runtime_install_tested",
    SCRIPT.with_name("platform_live_user_qa_dispatch.py"),
)
assert DISPATCH_SPEC is not None and DISPATCH_SPEC.loader is not None
dispatch = importlib.util.module_from_spec(DISPATCH_SPEC)
DISPATCH_SPEC.loader.exec_module(dispatch)
GUARD_SPEC = importlib.util.spec_from_file_location(
    "platform_live_qa_guard_runtime_install_tested",
    SCRIPT.with_name("platform_live_qa_guard.py"),
)
assert GUARD_SPEC is not None and GUARD_SPEC.loader is not None
guard = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard)


@unittest.skipUnless(os.geteuid() == 0, "installer contract requires root-owned test paths")
class LiveQaRuntimeInstallTests(unittest.TestCase):
    def test_bootstrap_preflight_binds_private_release_and_stage_reserve(self) -> None:
        bootstrap = Path("/tmp") / ("old-sparky-release-bootstrap." + uuid4().hex[:6])
        bootstrap.mkdir(mode=0o700)
        try:
            os.chmod(bootstrap, 0o700)
            source_sha = "c" * 40
            slug = "gha-1-1-" + source_sha[:12]
            release = bootstrap / slug
            release.mkdir(mode=0o555)
            payload = {
                "release_slug": slug,
                "source_git_commit": source_sha,
                "python_venv_install_evidence": {
                    "schema": 1,
                    "builder_allocated_bytes": 4096,
                    "wheelhouse_expanded_bytes": 8192,
                    "host_overhead_bytes": 16384,
                },
            }
            metadata = release / "RELEASE.json"
            metadata.write_text(json.dumps(payload) + "\n", encoding="ascii")
            os.chmod(metadata, 0o444)
            self.assertEqual(
                runtime._safe_bootstrap_release(release, slug),
                (release, source_sha, slug),
            )
            artifact = bootstrap / (slug + ".tar.gz")
            with tarfile.open(artifact, "w:gz") as archive:
                root_member = tarfile.TarInfo(slug)
                root_member.type = tarfile.DIRTYPE
                root_member.mode = 0o555
                archive.addfile(root_member)
                member = tarfile.TarInfo(slug + "/RELEASE.json")
                content = metadata.read_bytes()
                member.size = len(content)
                member.mode = 0o444
                archive.addfile(member, io.BytesIO(content))
            os.chmod(artifact, 0o444)
            self.assertGreater(
                runtime._artifact_stage_reserve(artifact, release),
                sum(payload["python_venv_install_evidence"][key] for key in (
                    "builder_allocated_bytes", "wheelhouse_expanded_bytes", "host_overhead_bytes",
                )),
            )

            def write_archive(
                entries: list[tuple[str, bytes, bytes | str | None]],
            ) -> None:
                artifact.unlink(missing_ok=True)
                with tarfile.open(artifact, "w:gz") as archive:
                    for name, member_type, content in entries:
                        entry = tarfile.TarInfo(name)
                        entry.type = member_type
                        entry.mode = 0o555 if member_type == tarfile.DIRTYPE else 0o444
                        file_content = content if isinstance(content, bytes) else b""
                        if member_type == tarfile.REGTYPE:
                            entry.size = len(file_content)
                        if member_type == tarfile.SYMTYPE:
                            entry.linkname = content if isinstance(content, str) else "target"
                        archive.addfile(
                            entry,
                            io.BytesIO(file_content) if member_type == tarfile.REGTYPE else None,
                        )
                os.chmod(artifact, 0o444)

            invalid_archives = {
                "root regular file": [(slug, tarfile.REGTYPE, b"root")],
                "root symlink": [(slug, tarfile.SYMTYPE, "RELEASE.json")],
                "sibling path": [
                    (slug, tarfile.DIRTYPE, None),
                    (slug + "-sibling/RELEASE.json", tarfile.REGTYPE, b"{}"),
                ],
                "traversal path": [
                    (slug, tarfile.DIRTYPE, None),
                    (slug + "/../outside", tarfile.REGTYPE, b"{}"),
                ],
                "unsupported member type": [
                    (slug, tarfile.DIRTYPE, None),
                    (slug + "/device", tarfile.CHRTYPE, None),
                ],
                "missing root": [(slug + "/RELEASE.json", tarfile.REGTYPE, b"{}")],
            }
            for case, entries in invalid_archives.items():
                with self.subTest(archive_case=case):
                    write_archive(entries)
                    with self.assertRaises(runtime.InstallerError):
                        runtime._artifact_stage_reserve(artifact, release)

            with self.assertRaises(runtime.InstallerError):
                runtime._safe_bootstrap_release(release, "wrong-slug")
        finally:
            shutil.rmtree(bootstrap)

    def test_deploy_preflights_live_qa_before_migration_and_pointer_switch(self) -> None:
        deploy = (SCRIPT.parent / "platform_release_deploy.sh").read_text(encoding="utf-8")
        bootstrap_preflight = deploy.index('--bootstrap-slug "$bootstrap_slug" --artifact "$ARTIFACT"')
        preflight_diag = deploy.index(
            "RELEASE_INSTALL_DIAG schema=1 status=failed class=liveqa_runtime "
            "stage=runtime_prepare exit_status=1 release_slug=%s"
        )
        normal_quiesce = deploy.index("  quiesce_runtime_writers\n", bootstrap_preflight)
        resume_preflight = deploy.index('preflight --app-dir "$APP_DIR" --release "$CANDIDATE"')
        resume_quiesce = deploy.index("  quiesce_runtime_writers\n", resume_preflight)
        migration = deploy.index('run_candidate tools/platform_run_alembic.sh upgrade head')
        pointer = deploy.index('--name current --target "$CANDIDATE"')
        self.assertLess(bootstrap_preflight, normal_quiesce)
        self.assertLess(bootstrap_preflight, preflight_diag)
        self.assertLess(preflight_diag, normal_quiesce)
        self.assertLess(resume_preflight, resume_quiesce)
        self.assertLess(resume_quiesce, migration)
        self.assertLess(migration, pointer)

    def test_release_rollback_selects_current_installer_only_after_receipt_proof(self) -> None:
        rollback = (SCRIPT.parent / "platform_release_rollback.sh").read_text(encoding="utf-8")
        deploy = (SCRIPT.parent / "platform_release_deploy.sh").read_text(encoding="utf-8")
        self.assertIn("rollback-check", rollback)
        self.assertIn("--target-release \"$release\"", rollback)
        self.assertIn("--rollback-transaction \"$TRANSACTION_STATE\"", rollback)
        self.assertIn('"$check_status" -ne 2', rollback)
        self.assertIn('"$previous_before" != "$release"', rollback)
        self.assertIn('"$candidate_release" != "$CURRENT_TARGET"', rollback)
        self.assertNotIn("e2e94c2a728c6648ab9c617118db806cc4c0d314", rollback)
        self.assertIn("recovery-check", deploy)
        self.assertIn("--target-release \"$release\"", deploy)
        self.assertIn("--transaction \"$TRANSACTION_STATE\"", deploy)
        self.assertNotIn("e2e94c2a728c6648ab9c617118db806cc4c0d314", deploy)

    @staticmethod
    def _source_tree_fixture(root: Path, *, cache_relative: str | None = None) -> Path:
        source = root / "checkout" / "apps" / "platform_api"
        package = source / "pkg"
        package.mkdir(mode=0o755, parents=True)
        (package / "module.py").write_bytes(b"VALUE = 1\n")
        (package / "module.py").chmod(0o444)
        if cache_relative is not None:
            cache_path = source / cache_relative
            cache_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            cache_path.write_bytes(b"generated cache fixture\n")
            cache_path.chmod(0o444)
            for directory in sorted(source.rglob("*")):
                if directory.is_dir():
                    directory.chmod(0o755)
        return source

    @contextmanager
    def _payload_bytecode_repair_fixture(self, root: Path):
        """Build a valid active payload plus exactly the two observed bytecode extras."""

        source_sha = "a" * 40
        runner_sha = "b" * 40
        with self.canonical_runtime_tree(root, source_sha) as (
            app_dir,
            release,
            trusted,
            payload_root,
        ):
            (app_dir / "shared").mkdir(mode=0o700)
            previous_sha = "c" * 40
            previous_release = app_dir / "releases" / f"release-{previous_sha[:8]}"
            previous_release.mkdir(mode=0o755)
            (previous_release / "RELEASE.json").write_text(
                json.dumps(
                    {
                        "source_git_commit": previous_sha,
                        "release_slug": previous_release.name,
                    }
                )
                + "\n",
                encoding="ascii",
            )
            os.chmod(previous_release / "RELEASE.json", 0o444)
            (app_dir / "previous").symlink_to(previous_release)

            release_lock_path = root / "release.lock"
            lock_fd = os.open(
                release_lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.fchown(lock_fd, 0, 0)
            os.fchmod(lock_fd, 0o600)
            os.close(lock_fd)
            installer_source = release / "tools/platform_live_qa_runtime_install.py"
            installer_bytes = installer_source.read_bytes()
            replacements = (
                (
                    b'TRUSTED_ROOT = Path("/root/.oldsparky/liveqa")',
                    f"TRUSTED_ROOT = Path({str(trusted)!r})".encode("ascii"),
                ),
                (
                    b'RELEASE_LOCK_PATH = Path("/run/lock/oldsparky-platform-release.lock")',
                    f"RELEASE_LOCK_PATH = Path({str(release_lock_path)!r})".encode("ascii"),
                ),
            )
            for old, new in replacements:
                self.assertEqual(installer_bytes.count(old), 1)
                installer_bytes = installer_bytes.replace(old, new)
            os.chmod(installer_source, 0o644)
            installer_source.write_bytes(installer_bytes)
            os.chmod(installer_source, 0o444)

            manifest = runtime.install(app_dir, release)
            payload = payload_root / source_sha
            self.assertEqual(manifest["source_sha"], source_sha)
            self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
            cache_dir = payload / "platform/tools/__pycache__"
            cache_dir.mkdir(mode=0o755)
            os.chmod(cache_dir, 0o755)
            for module in (
                "platform_live_qa_guard",
                "platform_live_qa_machine_lock",
            ):
                cache = cache_dir / f"{module}.cpython-312.pyc"
                source = payload / f"platform/tools/{module}.py"
                compile_pyc = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        "import importlib._bootstrap_external, pathlib, sys; "
                        "source=pathlib.Path(sys.argv[1]); output=pathlib.Path(sys.argv[2]); "
                        "data=source.read_bytes(); metadata=source.stat(); "
                        "code=compile(data, str(source), 'exec', dont_inherit=True, optimize=sys.flags.optimize); "
                        "output.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc("
                        "code, int(metadata.st_mtime), metadata.st_size))",
                        str(source),
                        str(cache),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(compile_pyc.returncode, 0, compile_pyc.stderr)
                os.chmod(cache, 0o644)

            plan_root = root / "plans"
            quarantine_root = root / "quarantine"
            tool_root = root / "repair-tools"
            machine_lock_path = root / "machine.lock"
            plan_root.mkdir(mode=0o700)
            quarantine_root.mkdir(mode=0o700)
            tool_root.mkdir(mode=0o700)
            repair_tool = SCRIPT.with_name("platform_live_qa_payload_bytecode_repair.py")
            machine_lock = SCRIPT.with_name("platform_live_qa_machine_lock.py")
            guard_script = SCRIPT.with_name("platform_live_qa_guard.py")
            repair_bytes = repair_tool.read_bytes()
            machine_lock_bytes = machine_lock.read_bytes().replace(
                b'Path("/run/lock/oldsparky-liveqa.lock")',
                f"Path({str(machine_lock_path)!r})".encode("ascii"),
            )
            self.assertNotEqual(machine_lock_bytes, machine_lock.read_bytes())
            guard_bytes = guard_script.read_bytes()
            (tool_root / repair_tool.name).write_bytes(repair_bytes)
            (tool_root / machine_lock.name).write_bytes(machine_lock_bytes)
            (tool_root / guard_script.name).write_bytes(guard_bytes)
            os.chmod(tool_root / repair_tool.name, 0o444)
            os.chmod(tool_root / machine_lock.name, 0o444)
            os.chmod(tool_root / guard_script.name, 0o444)
            bundle_manifest = {
                "schema": 1,
                "runner_source_sha": runner_sha,
                "repair_tool_sha256": hashlib.sha256(repair_bytes).hexdigest(),
                "machine_lock_sha256": hashlib.sha256(machine_lock_bytes).hexdigest(),
                "guard_sha256": hashlib.sha256(guard_bytes).hexdigest(),
            }
            (tool_root / "repair-bundle.json").write_text(
                json.dumps(bundle_manifest, sort_keys=True) + "\n",
                encoding="ascii",
            )
            os.chmod(tool_root / "repair-bundle.json", 0o444)
            yield {
                "app_dir": app_dir,
                "active_source_sha": source_sha,
                "runner_source_sha": runner_sha,
                "cache_dir": cache_dir,
                "manifest": manifest,
                "payload": payload,
                "payload_root": payload_root,
                "plan_root": plan_root,
                "quarantine_root": quarantine_root,
                "machine_lock_path": machine_lock_path,
                "release_lock_path": release_lock_path,
                "tool_root": tool_root,
                "trusted": trusted,
            }

    @staticmethod
    def _payload_bytecode_repair_module():
        helper_path = SCRIPT.with_name("platform_live_qa_payload_bytecode_repair.py")
        spec = importlib.util.spec_from_file_location(
            "platform_live_qa_payload_bytecode_repair_tested", helper_path
        )
        if spec is None or spec.loader is None:
            raise AssertionError("payload bytecode repair helper is unavailable")
        existing = sys.modules.get(spec.name)
        if existing is not None:
            return existing
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        finally:
            sys.dont_write_bytecode = previous

    @staticmethod
    def _payload_bytecode_repair_paths(helper, fixture):
        return helper.RepairPaths(
            app_dir=fixture["app_dir"],
            liveqa_root=fixture["trusted"],
            plan_root=fixture["plan_root"],
            quarantine_root=fixture["quarantine_root"],
            tool_root=fixture["tool_root"],
        )

    def _assert_payload_repair_refusal(self, helper, reason: str, operation) -> None:
        with self.assertRaises(helper.RepairError) as raised:
            operation()
        self.assertEqual(raised.exception.reason, reason)

    def test_payload_bytecode_repair_stdin_uses_fixed_source_bundle_root(self) -> None:
        helper = self._payload_bytecode_repair_module()
        runner_sha = "b" * 40
        args = helper._parser().parse_args(
            [
                "prepare",
                "--expected-source-sha",
                "a" * 40,
                "--runner-source-sha",
                runner_sha,
            ]
        )
        self.assertEqual(
            set(vars(args)),
            {"operation", "expected_source_sha", "runner_source_sha"},
        )
        self.assertEqual(
            helper._bundle_root(helper.DEFAULT_PATHS, runner_sha),
            helper.TOOL_ROOT / runner_sha,
        )

        source = SCRIPT.with_name("platform_live_qa_payload_bytecode_repair.py").read_text(
            encoding="utf-8"
        )
        previous_cwd = Path.cwd()
        previous_bytecode = sys.dont_write_bytecode
        try:
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                os.chdir(temporary)
                sys.dont_write_bytecode = True
                module_name = "platform_live_qa_payload_bytecode_repair_stdin_test"
                module = type(sys)(module_name)
                module.__file__ = "<stdin>"
                sys.modules[module_name] = module
                try:
                    exec(compile(source, "<stdin>", "exec"), module.__dict__)
                    self.assertEqual(
                        module._bundle_root(module.DEFAULT_PATHS, runner_sha),
                        Path("/root/.oldsparky/liveqa/payload-repair-tools") / runner_sha,
                    )
                finally:
                    sys.modules.pop(module_name, None)
        finally:
            os.chdir(previous_cwd)
            sys.dont_write_bytecode = previous_bytecode

    def test_payload_bytecode_repair_workflow_is_narrow_and_source_bound(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-liveqa-payload-repair.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        for fragment in (
            "name: Platform LiveQA payload bytecode repair",
            "  workflow_dispatch:",
            "          - prepare",
            "          - apply",
            "group: platform-liveqa-payload-repair-${{ inputs.security_run_id }}-${{ inputs.security_run_attempt }}",
            '"platform_live_qa_payload_bytecode_repair.py"',
            '"platform_live_qa_machine_lock.py"',
            '"platform_live_qa_guard.py"',
            '"repair-bundle.json"',
            'Path("/root/.oldsparky/liveqa/payload-repair-tools")',
            '"$lock" --run /bin/bash -s --',
            '/usr/bin/python3.12 -I -B - "$stage/bundle.tar" "$runner_sha"',
            '/usr/bin/python3.12 -I -B - "${args[@]}" < "/root/.oldsparky/liveqa/payload-repair-tools/$runner_sha/platform_live_qa_payload_bytecode_repair.py"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

        self.assertEqual(workflow.count("workflow_dispatch:"), 1)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("platform-deploy.yml", workflow)

        heredoc_start = (
            'runner_sha="$(/usr/bin/python3 -I -B - "$run_json" '
            '"$workflow_json" "$branch_json" "$SECURITY_RUN_ID" <<\'PY\'\n'
        )
        self.assertEqual(workflow.count(heredoc_start), 1)
        verifier = workflow.split(heredoc_start, 1)[1].split("\n          PY\n          )\"", 1)[0]
        verifier = textwrap.dedent(verifier)

        valid_run = {
            "id": 12345,
            "head_sha": "a" * 40,
            "run_attempt": 3,
            "workflow_id": 77,
            "path": ".github/workflows/platform-security.yml",
            "name": "Platform security and build",
            "event": "push",
            "head_branch": "dev",
            "status": "completed",
            "conclusion": "success",
            "head_repository": {"full_name": "StrayForest/old_sparky"},
            "repository": {"full_name": "StrayForest/old_sparky"},
        }
        valid_workflow = {
            "id": 77,
            "path": ".github/workflows/platform-security.yml",
            "state": "active",
        }
        valid_branch = {"commit": {"sha": "a" * 40}}

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture_root = Path(temporary)

            def execute(run, workflow_record, branch, *, attempt="3"):
                records = (run, workflow_record, branch)
                record_paths = []
                for name, record in zip(("run", "workflow", "branch"), records):
                    path = fixture_root / f"{name}.json"
                    path.write_text(json.dumps(record), encoding="ascii")
                    record_paths.append(str(path))
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        verifier,
                        *record_paths,
                        "12345",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env={"SECURITY_RUN_ATTEMPT": attempt},
                    timeout=5,
                )

            valid = execute(valid_run, valid_workflow, valid_branch)
            self.assertEqual(valid.returncode, 0)
            self.assertEqual(valid.stdout, ("a" * 40 + "\n").encode("ascii"))
            self.assertEqual(valid.stderr, b"")

            rejected_fixtures = (
                (
                    "wrong_repository",
                    {**valid_run, "repository": {"full_name": "untrusted/repo"}},
                    valid_workflow,
                    valid_branch,
                    "3",
                ),
                (
                    "wrong_workflow",
                    valid_run,
                    {**valid_workflow, "path": ".github/workflows/other.yml"},
                    valid_branch,
                    "3",
                ),
                ("wrong_attempt", valid_run, valid_workflow, valid_branch, "4"),
                (
                    "stale_dev_head",
                    valid_run,
                    valid_workflow,
                    {"commit": {"sha": "b" * 40}},
                    "3",
                ),
            )
            for name, run, workflow_record, branch, attempt in rejected_fixtures:
                with self.subTest(case=name):
                    rejected = execute(
                        run,
                        workflow_record,
                        branch,
                        attempt=attempt,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(rejected.stdout, b"")

        transport_line = re.search(
            r'(?m)^\s*timeout --foreground 60s scp (?P<preserve>-p) "\$\{ssh_args\[@\]\}" '
            r'"\$BUNDLE" "\$PROD_SSH_USER@\$PROD_SSH_HOST:\$remote_stage/bundle\.tar"',
            workflow,
        )
        self.assertIsNotNone(transport_line)
        scp_binary = shutil.which("scp")
        self.assertIsNotNone(scp_binary)

        outcome_start = workflow.index("          write_outcome() {")
        outcome_end = workflow.index("          ssh_args=", outcome_start)
        outcome_function = textwrap.dedent(workflow[outcome_start:outcome_end])
        cleanup_start = workflow.index("          cleanup_remote_stage() {")
        cleanup_end = workflow.index(
            "          trap cleanup_remote_stage EXIT", cleanup_start
        )
        cleanup_function = textwrap.dedent(workflow[cleanup_start:cleanup_end])
        self.assertIn("stat.S_IMODE(file_meta.st_mode) != 0o600", workflow)
        self.assertIn("file_meta.st_nlink != 1", workflow)
        cleanup_python_match = re.search(
            r"/usr/bin/python3\.12 -I -B - .*?<<'PY'\n(?P<script>.*?)\nPY\nCLEANUP",
            cleanup_function,
            re.DOTALL,
        )
        self.assertIsNotNone(cleanup_python_match)
        cleanup_python = cleanup_python_match.group("script")

        def assert_outcome_mirrored(result, summary_path):
            summary_lines = summary_path.read_bytes().splitlines()
            self.assertEqual(len(summary_lines), 1)
            self.assertIn(summary_lines[0], result.stdout.splitlines())

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture_root = Path(temporary)
            source_bundle = fixture_root / "bundle.tar"
            destination_bundle = fixture_root / "remote-bundle.tar"
            source_bundle.write_bytes(b"bounded transport mode fixture")
            source_bundle.chmod(0o600)
            scp_run = subprocess.run(
                [scp_binary, transport_line.group("preserve"), str(source_bundle), str(destination_bundle)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(scp_run.returncode, 0, scp_run.stderr.decode("utf-8", "replace"))
            self.assertEqual(stat.S_IMODE(destination_bundle.stat().st_mode), 0o600)

            def run_remote_cleanup(*, bundle_mode=0o600, expected_inode_delta=0):
                for _ in range(8):
                    stage = Path("/tmp") / f"oldsparky-liveqa-payload-repair.{uuid4().hex[:6]}"
                    try:
                        os.mkdir(stage, 0o700)
                    except FileExistsError:
                        continue
                    break
                else:
                    self.fail("could not allocate unique private cleanup fixture")
                os.chmod(stage, 0o700)
                stage_stat = os.lstat(stage)
                bundle = stage / "bundle.tar"
                fd = os.open(
                    bundle,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    os.fchmod(fd, bundle_mode)
                    os.write(fd, b"owned cleanup fixture")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                bundle_stat = os.lstat(bundle)
                expected = (
                    stage_stat.st_dev,
                    stage_stat.st_ino + expected_inode_delta,
                    stage_stat.st_uid,
                    stage_stat.st_gid,
                    stage_stat.st_nlink,
                    f"{stat.S_IMODE(stage_stat.st_mode):o}",
                )
                try:
                    result = subprocess.run(
                        [
                            "/usr/bin/python3.12",
                            "-I",
                            "-B",
                            "-",
                            str(stage),
                            str(expected[0]),
                            str(expected[1]),
                            str(expected[2]),
                            str(expected[3]),
                            str(expected[4]),
                            expected[5],
                        ],
                        input=cleanup_python.encode("utf-8"),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=10,
                    )
                    return result, stage, stage_stat, bundle, bundle_stat
                except BaseException:
                    current = os.lstat(stage)
                    if (current.st_dev, current.st_ino) == (stage_stat.st_dev, stage_stat.st_ino):
                        if os.path.lexists(bundle):
                            os.chmod(bundle, 0o600)
                            os.unlink(bundle)
                        os.rmdir(stage)
                    raise

            def remove_owned_cleanup_fixture(stage, stage_stat, bundle, bundle_stat):
                if not os.path.lexists(stage):
                    return
                current = os.lstat(stage)
                self.assertEqual((current.st_dev, current.st_ino), (stage_stat.st_dev, stage_stat.st_ino))
                self.assertTrue(stat.S_ISDIR(current.st_mode))
                self.assertEqual((current.st_uid, stat.S_IMODE(current.st_mode)), (stage_stat.st_uid, 0o700))
                current_bundle = os.lstat(bundle)
                self.assertEqual(
                    (current_bundle.st_dev, current_bundle.st_ino, current_bundle.st_uid, current_bundle.st_nlink, current_bundle.st_size),
                    (bundle_stat.st_dev, bundle_stat.st_ino, bundle_stat.st_uid, bundle_stat.st_nlink, bundle_stat.st_size),
                )
                self.assertTrue(stat.S_ISREG(current_bundle.st_mode))
                os.chmod(bundle, 0o600)
                os.unlink(bundle)
                os.rmdir(stage)

            valid_cleanup, valid_stage, valid_stage_stat, valid_bundle, valid_bundle_stat = run_remote_cleanup()
            try:
                self.assertEqual(valid_cleanup.returncode, 0, valid_cleanup.stderr.decode("utf-8", "replace"))
                self.assertFalse(os.path.lexists(valid_stage))
            finally:
                remove_owned_cleanup_fixture(valid_stage, valid_stage_stat, valid_bundle, valid_bundle_stat)

            for name, kwargs in (
                ("bad_bundle_mode", {"bundle_mode": 0o644}),
                ("wrong_stage_identity", {"expected_inode_delta": 1}),
            ):
                with self.subTest(cleanup_case=name):
                    refused, stage, stage_stat, bundle, bundle_stat = run_remote_cleanup(**kwargs)
                    try:
                        self.assertNotEqual(refused.returncode, 0)
                        current = os.lstat(stage)
                        self.assertEqual((current.st_dev, current.st_ino), (stage_stat.st_dev, stage_stat.st_ino))
                        self.assertTrue(bundle.exists())
                        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), kwargs.get("bundle_mode", 0o600))
                    finally:
                        remove_owned_cleanup_fixture(stage, stage_stat, bundle, bundle_stat)

            mock_bin = fixture_root / "bin"
            mock_bin.mkdir(mode=0o700)
            ssh_stub = mock_bin / "ssh"
            ssh_stub.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${MOCK_SSH_OUTPUT:-}\" ]; then printf '%s' \"$MOCK_SSH_OUTPUT\"; fi\n"
                "exit \"${MOCK_SSH_EXIT:-0}\"\n",
                encoding="ascii",
            )
            ssh_stub.chmod(0o700)

            def run_cleanup(exit_code: int):
                output_path = fixture_root / f"github-output-{exit_code}"
                summary_path = fixture_root / f"github-summary-{exit_code}"
                output_path.write_bytes(b"")
                summary_path.write_bytes(b"")
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        "OPERATION=prepare",
                        "step_phase=complete",
                        "step_rc=0",
                        "step_cleanup_rc=none",
                        "helper_reason=none",
                        "plan_sha=none",
                        "tool_bundle_residual=none",
                        "operation_result_line=",
                        outcome_function,
                        f"plan_sha={'a' * 64}",
                        f"operation_result_line='LIVE_QA_PAYLOAD_BYTECODE_REPAIR status=prepared plan_sha256={'a' * 64} extra_file_count=2'",
                        "ssh_args=()",
                        "remote_stage=/tmp/oldsparky-liveqa-payload-repair.ABC123",
                        "remote_stage_dev=1",
                        "remote_stage_ino=2",
                        "remote_stage_uid=0",
                        "remote_stage_gid=0",
                        "remote_stage_nlink=2",
                        "remote_stage_mode=700",
                        cleanup_function,
                        "cleanup_remote_stage",
                    )
                )
                env = {
                    "PATH": f"{mock_bin}:/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "MOCK_SSH_EXIT": str(exit_code),
                    "PROD_SSH_USER": "fixture",
                    "PROD_SSH_HOST": "fixture.invalid",
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_STEP_SUMMARY": str(summary_path),
                }
                result = subprocess.run(
                    ["/bin/bash", "-c", script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env=env,
                    timeout=10,
                )
                return result, output_path, summary_path

            cleanup_ok, success_output, success_summary = run_cleanup(0)
            self.assertEqual(cleanup_ok.returncode, 0, cleanup_ok.stderr.decode("utf-8", "replace"))
            self.assertIn(b"plan_sha256=" + b"a" * 64, success_output.read_bytes())
            self.assertIn(
                b"status=completed phase=complete exit_code=0 cleanup_rc=0 helper_reason=none operation=prepare plan_sha256="
                + b"a" * 64
                + b" stage_residual=none tool_bundle_residual=none",
                success_summary.read_bytes(),
            )
            assert_outcome_mirrored(cleanup_ok, success_summary)

            cleanup_failed, failed_output, failed_summary = run_cleanup(1)
            self.assertNotEqual(cleanup_failed.returncode, 0)
            self.assertEqual(failed_output.read_bytes(), b"")
            self.assertIn(
                b"status=failed phase=cleanup exit_code=1 cleanup_rc=1",
                failed_summary.read_bytes(),
                failed_summary.read_bytes().decode("utf-8", "replace")
                + cleanup_failed.stderr.decode("utf-8", "replace"),
            )
            self.assertIn(
                b"helper_reason=none operation=prepare plan_sha256="
                + b"a" * 64
                + b" stage_residual=unverified tool_bundle_residual=none",
                failed_summary.read_bytes(),
            )
            self.assertEqual(
                cleanup_failed.stderr,
                b"Remote repair transport cleanup failed identity checks\n",
            )
            assert_outcome_mirrored(cleanup_failed, failed_summary)

            outer_remote_rc = workflow.index("\n          remote_rc=$?\n          set -e\n")
            remote_handler_start = workflow.index(
                "          if (( remote_rc != 0 )); then", outer_remote_rc
            )
            remote_handler_end = workflow.index(
                "\n          step_phase=result_validation", remote_handler_start
            )
            remote_handler = textwrap.dedent(
                workflow[remote_handler_start:remote_handler_end]
            )

            def run_remote_failure(error_text: bytes, *, error_present: bool = True):
                output_path = fixture_root / "github-output-remote-failure"
                summary_path = fixture_root / "github-summary-remote-failure"
                result_path = fixture_root / "remote-result"
                error_path = fixture_root / "remote-error"
                output_path.write_bytes(b"")
                summary_path.write_bytes(b"")
                result_path.write_bytes(b"")
                if error_present:
                    error_path.write_bytes(error_text)
                elif error_path.exists():
                    error_path.unlink()
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        "OPERATION=prepare",
                        outcome_function,
                        "step_phase=locked_install",
                        "step_rc=0",
                        "helper_reason=none",
                        "plan_sha=none",
                        "tool_bundle_residual=none",
                        "remote_rc=9",
                        f"RUNNER_SOURCE_SHA={'b' * 40}",
                        f"RUNNER_TEMP={shlex.quote(str(fixture_root))}",
                        f"result={shlex.quote(str(result_path))}",
                        f"error={shlex.quote(str(error_path))}",
                        "ssh_args=()",
                        "remote_stage=/tmp/oldsparky-liveqa-payload-repair.ABC123",
                        "remote_stage_dev=1",
                        "remote_stage_ino=2",
                        "remote_stage_uid=0",
                        "remote_stage_gid=0",
                        "remote_stage_nlink=2",
                        "remote_stage_mode=700",
                        cleanup_function,
                        "trap cleanup_remote_stage EXIT",
                        remote_handler,
                    )
                )
                env = {
                    "PATH": f"{mock_bin}:/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "MOCK_SSH_EXIT": "0",
                    "PROD_SSH_USER": "fixture",
                    "PROD_SSH_HOST": "fixture.invalid",
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_STEP_SUMMARY": str(summary_path),
                }
                completed = subprocess.run(
                    ["/bin/bash", "-c", script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env=env,
                    timeout=10,
                )
                return completed, output_path, summary_path, error_path

            closed_marker = (
                b"LIVE_QA_PAYLOAD_BYTECODE_REPAIR_REMOTE "
                b"phase=helper exit_code=9 helper_reason=pointer_mismatch\n"
            )
            remote_known, known_output, known_summary, known_error = run_remote_failure(
                closed_marker
                + b"LIVE_QA_PAYLOAD_REPAIR_BUNDLE status=partial_cleanup_failed runner_source_sha="
                + b"b" * 40
                + b"\nprivate-diagnostic-sentinel\n"
            )
            self.assertEqual(remote_known.returncode, 9, remote_known.stderr.decode("utf-8", "replace"))
            self.assertEqual(known_output.read_bytes(), b"")
            self.assertEqual(
                known_summary.read_bytes(),
                b"LIVE_QA_PAYLOAD_BYTECODE_REPAIR_OUTCOME status=failed phase=helper exit_code=9 cleanup_rc=0 helper_reason=pointer_mismatch operation=prepare plan_sha256=none stage_residual=none tool_bundle_residual=unverified\n",
            )
            self.assertNotIn(b"private-diagnostic-sentinel", remote_known.stdout + remote_known.stderr + known_summary.read_bytes())
            self.assertNotIn(b"b" * 40, known_summary.read_bytes())
            self.assertFalse(known_error.exists())
            assert_outcome_mirrored(remote_known, known_summary)

            unknown_marker = (
                b"LIVE_QA_PAYLOAD_BYTECODE_REPAIR_REMOTE "
                b"phase=helper exit_code=9 helper_reason=unknown_private_value\n"
            )
            remote_unknown, unknown_output, unknown_summary, unknown_error = run_remote_failure(
                unknown_marker
                + b"LIVE_QA_PAYLOAD_REPAIR_BUNDLE status=partial_cleanup_failed runner_source_sha="
                + b"c" * 40
                + b"\nprivate-diagnostic-sentinel\n"
            )
            self.assertEqual(remote_unknown.returncode, 9)
            self.assertEqual(unknown_output.read_bytes(), b"")
            self.assertIn(
                b"status=failed phase=remote_command exit_code=9 cleanup_rc=0 helper_reason=none operation=prepare plan_sha256=none stage_residual=none tool_bundle_residual=none",
                unknown_summary.read_bytes(),
            )
            self.assertNotIn(b"private-diagnostic-sentinel", remote_unknown.stdout + remote_unknown.stderr + unknown_summary.read_bytes())
            self.assertFalse(unknown_error.exists())
            assert_outcome_mirrored(remote_unknown, unknown_summary)

            for name, error_text, error_present in (
                ("oversized", b"x" * 65537, True),
                ("unreadable", b"", False),
            ):
                with self.subTest(remote_diagnostic=name):
                    unobservable, unobservable_output, unobservable_summary, unobservable_error = run_remote_failure(
                        error_text, error_present=error_present
                    )
                    self.assertEqual(unobservable.returncode, 9)
                    self.assertEqual(unobservable_output.read_bytes(), b"")
                    self.assertIn(
                        b"status=failed phase=remote_command exit_code=9 cleanup_rc=0 helper_reason=none operation=prepare plan_sha256=none stage_residual=none tool_bundle_residual=unverified",
                        unobservable_summary.read_bytes(),
                    )
                    self.assertNotIn(b"x" * 256, unobservable.stdout + unobservable.stderr + unobservable_summary.read_bytes())
                    self.assertFalse(unobservable_error.exists())

            stage_failure_block_start = workflow.index(
                "          if (( stage_create_rc != 0 )); then"
            )
            stage_failure_block_end = workflow.index(
                "\n          IFS='|' read -r remote_stage", stage_failure_block_start
            )
            stage_failure_block = textwrap.dedent(
                workflow[stage_failure_block_start:stage_failure_block_end]
            )
            stage_summary = fixture_root / "github-summary-stage-failure"
            stage_output = fixture_root / "github-output-stage-failure"
            stage_summary.write_bytes(b"")
            stage_output.write_bytes(b"")
            stage_script = "\n".join(
                (
                    "set -euo pipefail",
                    "OPERATION=prepare",
                    outcome_function,
                    "stage_create_rc=7",
                    stage_failure_block,
                )
            )
            stage_result = subprocess.run(
                ["/bin/bash", "-c", stage_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "GITHUB_OUTPUT": str(stage_output),
                    "GITHUB_STEP_SUMMARY": str(stage_summary),
                },
                timeout=10,
            )
            self.assertEqual(stage_result.returncode, 7)
            self.assertIn(
                b"status=failed phase=stage_create exit_code=7 cleanup_rc=none helper_reason=none operation=prepare plan_sha256=none stage_residual=unverified tool_bundle_residual=none",
                stage_summary.read_bytes(),
            )
            self.assertEqual(stage_output.read_bytes(), b"")
            assert_outcome_mirrored(stage_result, stage_summary)

            stage_command_start = workflow.index(
                "          set +e\n          # shellcheck disable=SC2016\n          remote_stage_info="
            )
            stage_command_end = workflow.index(
                "\n          cleanup_remote_stage() {", stage_command_start
            )
            stage_command_block = textwrap.dedent(
                workflow[stage_command_start:stage_command_end]
            )

            def run_stage_failure(ssh_exit: int, ssh_output: str):
                summary = fixture_root / f"stage-summary-{ssh_exit}-{len(ssh_output)}"
                output = fixture_root / f"stage-output-{ssh_exit}-{len(ssh_output)}"
                summary.write_bytes(b"")
                output.write_bytes(b"")
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        "OPERATION=prepare",
                        outcome_function,
                        "ssh_args=()",
                        stage_command_block,
                        "printf 'STAGE_META_ACCEPTED:%s|%s|%s|%s|%s|%s|%s\\n' \"$remote_stage\" \"$remote_stage_dev\" \"$remote_stage_ino\" \"$remote_stage_uid\" \"$remote_stage_gid\" \"$remote_stage_nlink\" \"$remote_stage_mode\"",
                    )
                )
                completed = subprocess.run(
                    ["/bin/bash", "-c", script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env={
                        "PATH": f"{mock_bin}:/usr/bin:/bin",
                        "LANG": "C",
                        "LC_ALL": "C",
                        "MOCK_SSH_EXIT": str(ssh_exit),
                        "MOCK_SSH_OUTPUT": ssh_output,
                        "PROD_SSH_USER": "fixture",
                        "PROD_SSH_HOST": "fixture.invalid",
                        "GITHUB_OUTPUT": str(output),
                        "GITHUB_STEP_SUMMARY": str(summary),
                    },
                    timeout=10,
                )
                return completed, summary, output

            for case, ssh_exit, ssh_output, expected_exit in (
                ("ssh_failed", 7, "", 7),
                ("invalid_tuple", 0, "malformed-private-response", 1),
            ):
                with self.subTest(stage_create_case=case):
                    failed, summary, output = run_stage_failure(ssh_exit, ssh_output)
                    self.assertEqual(failed.returncode, expected_exit, failed.stderr.decode("utf-8", "replace"))
                    self.assertIn(
                        b"status=failed phase=stage_create exit_code="
                        + str(expected_exit).encode("ascii")
                        + b" cleanup_rc=none helper_reason=none operation=prepare plan_sha256=none stage_residual=unverified tool_bundle_residual=none",
                        summary.read_bytes(),
                    )
                    self.assertEqual(output.read_bytes(), b"")
                    assert_outcome_mirrored(failed, summary)
                    self.assertNotIn(b"malformed-private-response", failed.stdout + failed.stderr + summary.read_bytes())

            accepted_stage = None
            for _ in range(8):
                candidate = Path("/tmp") / f"oldsparky-liveqa-payload-repair.{uuid4().hex[:6]}"
                try:
                    os.mkdir(candidate, 0o700)
                except FileExistsError:
                    continue
                accepted_stage = candidate
                break
            self.assertIsNotNone(accepted_stage)
            accepted_meta = os.lstat(accepted_stage)
            try:
                accepted_tuple = (
                    f"{accepted_stage}|{accepted_meta.st_dev}|{accepted_meta.st_ino}|"
                    f"{accepted_meta.st_uid}|{accepted_meta.st_gid}|{accepted_meta.st_nlink}|"
                    f"{stat.S_IMODE(accepted_meta.st_mode):o}"
                )
                accepted, accepted_summary, accepted_output = run_stage_failure(0, accepted_tuple)
                self.assertEqual(accepted.returncode, 0, accepted.stderr.decode("utf-8", "replace"))
                self.assertEqual(
                    accepted.stdout,
                    f"STAGE_META_ACCEPTED:{accepted_tuple}\n".encode("ascii"),
                )
                self.assertEqual(accepted_summary.read_bytes(), b"")
                self.assertEqual(accepted_output.read_bytes(), b"")
            finally:
                if os.path.lexists(accepted_stage):
                    current = os.lstat(accepted_stage)
                    self.assertEqual((current.st_dev, current.st_ino), (accepted_meta.st_dev, accepted_meta.st_ino))
                    self.assertEqual(os.listdir(accepted_stage), [])
                    os.rmdir(accepted_stage)

            remote_start = workflow.index("          set -Eeuo pipefail\n          umask 077", workflow.index("<<'REMOTE'\n"))
            remote_end = workflow.index("\n          REMOTE\n          remote_rc=$?", remote_start)
            remote_source = textwrap.dedent(workflow[remote_start:remote_end])
            self.assertIn('Path("/root/.oldsparky/liveqa/payload-repair-tools")', remote_source)
            self.assertIn('current=/opt/oldsparky/platform/current', remote_source)
            active_sha = "e" * 40
            runner_sha = "d" * 40
            prepared_sha = "f" * 64
            expected_helper_output = (
                "LIVE_QA_PAYLOAD_BYTECODE_REPAIR status=prepared "
                f"plan_sha256={prepared_sha} extra_file_count=2\n"
            )

            def run_locked_entry(
                case_name,
                *,
                stage_token_ok=True,
                stage_mode=0o700,
                stage_uid=0,
                bundle_mode=0o600,
                bundle_uid=0,
                inject_target_lstat_failure=False,
            ):
                case_root = fixture_root / f"remote-entry-{case_name}"
                case_root.mkdir(mode=0o700)
                app_root = case_root / "app"
                release = app_root / "releases" / "active"
                release_tools = release / "tools"
                release_tools.mkdir(parents=True, mode=0o700)
                (app_root / "shared").mkdir(mode=0o700)
                (app_root / "current").symlink_to("releases/active")
                tool_parent = case_root / "liveqa" / "payload-repair-tools"
                tool_parent.mkdir(parents=True, mode=0o700)
                lock_capture = case_root / "lock-argv.bin"
                lock_script = release_tools / "platform_release_lock.sh"
                lock_script.write_text(
                    "#!/bin/bash\n"
                    "if [[ \"${BASH_SOURCE[0]}\" == \"$0\" ]]; then\n"
                    "  [[ \"${1:-}\" == --run ]] || exit 91\n"
                    "  { printf '%s\\0' \"$0\"; printf '%s\\0' \"$@\"; } > \"$LOCK_ARGV_CAPTURE\"\n"
                    "  shift\n"
                    "  exec \"$@\"\n"
                    "fi\n"
                    "platform_release_lock_open() { :; }\n"
                    "platform_release_lock_supervisor_holds() { :; }\n",
                    encoding="ascii",
                )
                lock_script.chmod(0o700)

                helper = textwrap.dedent(
                    f"""\
                    import sys
                    expected = ["prepare", "--expected-source-sha", {active_sha!r}, "--runner-source-sha", {runner_sha!r}]
                    if sys.argv[1:] != expected:
                        raise SystemExit(42)
                    print({expected_helper_output.strip()!r})
                    """
                ).encode("ascii")
                bundle_parts = {
                    "platform_live_qa_payload_bytecode_repair.py": helper,
                    "platform_live_qa_machine_lock.py": b"synthetic lock member\\n",
                    "platform_live_qa_guard.py": b"synthetic guard member\\n",
                }
                bundle_manifest = {
                    "schema": 1,
                    "runner_source_sha": runner_sha,
                    "repair_tool_sha256": hashlib.sha256(bundle_parts["platform_live_qa_payload_bytecode_repair.py"]).hexdigest(),
                    "machine_lock_sha256": hashlib.sha256(bundle_parts["platform_live_qa_machine_lock.py"]).hexdigest(),
                    "guard_sha256": hashlib.sha256(bundle_parts["platform_live_qa_guard.py"]).hexdigest(),
                }
                bundle_parts["repair-bundle.json"] = (json.dumps(bundle_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
                archive_bytes = io.BytesIO()
                with tarfile.open(fileobj=archive_bytes, mode="w:") as archive:
                    for name, content in bundle_parts.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        member.mode = 0o400
                        archive.addfile(member, io.BytesIO(content))

                stage = None
                stage_stat = None
                staged_bundle = None
                bundle_stat = None
                for _ in range(8):
                    candidate = Path("/tmp") / f"oldsparky-liveqa-payload-repair.{uuid4().hex[:6]}"
                    try:
                        os.mkdir(candidate, 0o700)
                    except FileExistsError:
                        continue
                    stage = candidate
                    break
                if stage is None:
                    self.fail("could not allocate exact-format local transport stage")
                stage_stat = os.lstat(stage)
                try:
                    os.chmod(stage, stage_mode)
                    if stage_uid != 0:
                        os.chown(stage, stage_uid, 0)
                    staged_bundle = stage / "bundle.tar"
                    source_bundle = case_root / "bundle.tar"
                    source_fd = os.open(
                        source_bundle,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    try:
                        os.write(source_fd, archive_bytes.getvalue())
                        os.fchmod(source_fd, 0o600)
                        os.fsync(source_fd)
                    finally:
                        os.close(source_fd)
                    scp_run = subprocess.run(
                        [scp_binary, transport_line.group("preserve"), str(source_bundle), str(staged_bundle)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(scp_run.returncode, 0, scp_run.stderr.decode("utf-8", "replace"))
                    bundle_stat = os.lstat(staged_bundle)
                    if bundle_uid != 0:
                        os.chown(staged_bundle, bundle_uid, 0)
                    if bundle_mode != 0o600:
                        os.chmod(staged_bundle, bundle_mode)
                    stage_arg = str(stage) if stage_token_ok else str(stage) + "/../invalid"
                    private_tool_root = tool_parent / runner_sha
                    script = remote_source.replace(
                        "/opt/oldsparky/platform", str(app_root)
                    ).replace(
                        "/root/.oldsparky/liveqa/payload-repair-tools", str(tool_parent)
                    )
                    if inject_target_lstat_failure:
                        script, replacement_count = script.replace(
                            "target_meta = target.lstat()",
                            "raise OSError('synthetic target identity read failure')",
                            1,
                        ), script.count("target_meta = target.lstat()")
                        self.assertEqual(replacement_count, 1)
                    command = subprocess.run(
                        ["/bin/bash", "-s", "--", stage_arg, "prepare", active_sha, runner_sha, ""],
                        input=script.encode("utf-8"),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        env={
                            "PATH": "/usr/bin:/bin",
                            "LANG": "C",
                            "LC_ALL": "C",
                            "LOCK_ARGV_CAPTURE": str(lock_capture),
                        },
                        timeout=20,
                    )
                    return command, app_root, tool_parent, private_tool_root, lock_capture, stage, stage_stat, staged_bundle, bundle_stat
                finally:
                    if stage is not None and stage_stat is not None and os.path.lexists(stage):
                        current_stage = os.lstat(stage)
                        self.assertEqual(
                            (current_stage.st_dev, current_stage.st_ino),
                            (stage_stat.st_dev, stage_stat.st_ino),
                        )
                        self.assertTrue(stat.S_ISDIR(current_stage.st_mode))
                        os.chown(stage, 0, 0)
                        os.chmod(stage, 0o700)
                        if staged_bundle is not None and os.path.lexists(staged_bundle):
                            current_bundle = os.lstat(staged_bundle)
                            self.assertTrue(stat.S_ISREG(current_bundle.st_mode))
                            if bundle_stat is not None:
                                self.assertEqual(
                                    (current_bundle.st_dev, current_bundle.st_ino, current_bundle.st_size, current_bundle.st_nlink),
                                    (bundle_stat.st_dev, bundle_stat.st_ino, bundle_stat.st_size, bundle_stat.st_nlink),
                                )
                            os.chown(staged_bundle, 0, 0)
                            os.chmod(staged_bundle, 0o600)
                            os.unlink(staged_bundle)
                        self.assertEqual(list(os.scandir(stage)), [])
                        os.rmdir(stage)

            entry_ok = run_locked_entry("valid")
            command, app_root, tool_parent, installed_bundle, lock_capture, stage, _, _, _ = entry_ok
            self.assertEqual(command.returncode, 0, command.stderr.decode("utf-8", "replace"))
            self.assertEqual(command.stdout, expected_helper_output.encode("ascii"))
            self.assertEqual(command.stderr, b"")
            self.assertEqual(
                lock_capture.read_bytes().split(b"\0")[:-1],
                [
                    str(app_root / "current/tools/platform_release_lock.sh").encode(),
                    b"--run",
                    b"/bin/bash",
                    b"-s",
                    b"--",
                    str(stage).encode(),
                    b"prepare",
                    active_sha.encode(),
                    runner_sha.encode(),
                    b"",
                ],
            )
            self.assertEqual(
                sorted(path.name for path in installed_bundle.iterdir()),
                sorted(
                    (
                        "platform_live_qa_payload_bytecode_repair.py",
                        "platform_live_qa_machine_lock.py",
                        "platform_live_qa_guard.py",
                        "repair-bundle.json",
                    )
                ),
            )
            self.assertEqual(stat.S_IMODE(installed_bundle.stat().st_mode), 0o700)
            for installed_file in installed_bundle.iterdir():
                metadata = installed_file.lstat()
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertEqual((metadata.st_uid, metadata.st_nlink, stat.S_IMODE(metadata.st_mode)), (0, 1, 0o400))

            for case_name, options, expect_lock in (
                ("bad_stage_token", {"stage_token_ok": False}, False),
                ("bad_stage_mode", {"stage_mode": 0o755}, True),
                ("bad_stage_owner", {"stage_uid": 65534}, True),
                ("bad_bundle_mode", {"bundle_mode": 0o644}, True),
                ("bad_bundle_owner", {"bundle_uid": 65534}, True),
            ):
                with self.subTest(remote_entry_case=case_name):
                    rejected, rejected_app, _, rejected_bundle, rejected_lock, _, _, _, _ = run_locked_entry(case_name, **options)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(rejected.stdout, b"")
                    self.assertNotIn(expected_helper_output.encode("ascii"), rejected.stderr)
                    self.assertFalse(rejected_bundle.exists())
                    self.assertEqual(rejected_lock.exists(), expect_lock)
                    if rejected_lock.exists():
                        captured = rejected_lock.read_bytes().split(b"\0")[:-1]
                        self.assertEqual(captured[0], str(rejected_app / "current/tools/platform_release_lock.sh").encode())

            partial, _, _, partial_bundle, _, _, _, _, _ = run_locked_entry(
                "target_lstat_failure", inject_target_lstat_failure=True
            )
            self.assertNotEqual(partial.returncode, 0)
            self.assertEqual(partial.stdout, b"")
            self.assertNotIn(expected_helper_output.encode("ascii"), partial.stdout)
            expected_partial_marker = (
                f"LIVE_QA_PAYLOAD_REPAIR_BUNDLE status=partial_cleanup_failed runner_source_sha={runner_sha}"
            ).encode("ascii")
            self.assertIn(expected_partial_marker, partial.stderr)
            self.assertIn(b"LIVE_QA_PAYLOAD_BYTECODE_REPAIR_REMOTE phase=locked_install exit_code=1 helper_reason=none", partial.stderr)
            partial_meta = os.lstat(partial_bundle)
            self.assertTrue(stat.S_ISDIR(partial_meta.st_mode))
            self.assertEqual((partial_meta.st_uid, stat.S_IMODE(partial_meta.st_mode)), (0, 0o700))
            self.assertEqual(os.listdir(partial_bundle), [])
            partial_bundle.rmdir()

            upload_start = workflow.index("          step_phase=upload\n")
            upload_end = workflow.index(
                "\n          result=\"$RUNNER_TEMP/liveqa-payload-repair.result\"",
                upload_start,
            )
            upload_block = textwrap.dedent(workflow[upload_start:upload_end])
            upload_bundle = fixture_root / "upload-failure-bundle.tar"
            upload_bundle.write_bytes(b"synthetic scp failure fixture")
            upload_bundle.chmod(0o600)
            upload_summary = fixture_root / "github-summary-upload-failure"
            upload_output = fixture_root / "github-output-upload-failure"
            scp_capture = fixture_root / "scp-argv.bin"
            upload_summary.write_bytes(b"")
            upload_output.write_bytes(b"")
            failing_scp = mock_bin / "scp"
            failing_scp.write_text(
                "#!/bin/sh\n"
                "printf '%s\\0' \"$@\" > \"$SCP_ARGV_CAPTURE\"\n"
                "echo private-upload-sentinel >&2\n"
                "exit 7\n",
                encoding="ascii",
            )
            failing_scp.chmod(0o700)
            upload_script = "\n".join(
                (
                    "set -euo pipefail",
                    "OPERATION=prepare",
                    "step_phase=stage_create",
                    "step_rc=0",
                    "helper_reason=none",
                    "plan_sha=none",
                    "tool_bundle_residual=none",
                    "operation_result_line=",
                    outcome_function,
                    "ssh_args=()",
                    "remote_stage=/tmp/oldsparky-liveqa-payload-repair.ABC123",
                    "remote_stage_dev=1",
                    "remote_stage_ino=2",
                    "remote_stage_uid=0",
                    "remote_stage_gid=0",
                    "remote_stage_nlink=2",
                    "remote_stage_mode=700",
                    cleanup_function,
                    "trap cleanup_remote_stage EXIT",
                    upload_block,
                )
            )
            upload_result = subprocess.run(
                ["/bin/bash", "-c", upload_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={
                    "PATH": f"{mock_bin}:/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "MOCK_SSH_EXIT": "0",
                    "SCP_ARGV_CAPTURE": str(scp_capture),
                    "PROD_SSH_USER": "fixture",
                    "PROD_SSH_HOST": "fixture.invalid",
                    "RUNNER_TEMP": str(fixture_root),
                    "BUNDLE": str(upload_bundle),
                    "GITHUB_OUTPUT": str(upload_output),
                    "GITHUB_STEP_SUMMARY": str(upload_summary),
                },
                timeout=10,
            )
            self.assertEqual(upload_result.returncode, 7)
            self.assertEqual(upload_output.read_bytes(), b"")
            self.assertIn(
                b"status=failed phase=upload exit_code=7 cleanup_rc=0 helper_reason=none operation=prepare plan_sha256=none stage_residual=none tool_bundle_residual=none",
                upload_summary.read_bytes(),
            )
            assert_outcome_mirrored(upload_result, upload_summary)
            self.assertNotIn(b"private-upload-sentinel", upload_result.stdout + upload_result.stderr + upload_summary.read_bytes())
            self.assertEqual(upload_result.stderr, b"")
            self.assertEqual(scp_capture.read_bytes().split(b"\0")[0], b"-p")

    def test_payload_bytecode_repair_uses_inherited_release_flock_under_stdin(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                unrelated_cwd = Path(temporary) / "unrelated"
                unrelated_cwd.mkdir(mode=0o700)
                paths = self._payload_bytecode_repair_paths(
                    self._payload_bytecode_repair_module(), fixture
                )
                values = {
                    "helper_path": str(
                        SCRIPT.with_name("platform_live_qa_payload_bytecode_repair.py")
                    ),
                    "app_dir": str(paths.app_dir),
                    "liveqa_root": str(paths.liveqa_root),
                    "plan_root": str(paths.plan_root),
                    "quarantine_root": str(paths.quarantine_root),
                    "tool_root": str(paths.tool_root),
                    "release_lock": str(fixture["release_lock_path"]),
                    "active_sha": fixture["active_source_sha"],
                    "runner_sha": fixture["runner_source_sha"],
                }
                script = f"""
import importlib.util, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    'bytecode_repair_stdin_child', {json.dumps(values['helper_path'])}
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.RELEASE_LOCK_PATH = Path({json.dumps(values['release_lock'])})
module._assert_no_active_qa = lambda *_args: None
print(f'CHILD_ID euid={__import__("os").geteuid()} egid={__import__("os").getegid()}', file=sys.stderr)
paths = module.RepairPaths(
    app_dir=Path({json.dumps(values['app_dir'])}),
    liveqa_root=Path({json.dumps(values['liveqa_root'])}),
    plan_root=Path({json.dumps(values['plan_root'])}),
    quarantine_root=Path({json.dumps(values['quarantine_root'])}),
    tool_root=Path({json.dumps(values['tool_root'])}),
)
result = module.prepare_repair_plan(
    {json.dumps(values['active_sha'])}, {json.dumps(values['runner_sha'])}, paths
)
print(result['status'], result['extra_file_count'])
"""
                result = subprocess.run(
                    [
                        "/usr/bin/flock",
                        str(fixture["release_lock_path"]),
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                    ],
                    input=script,
                    cwd=unrelated_cwd,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"parent euid={os.geteuid()} egid={os.getegid()}; stderr={result.stderr}",
                )
                self.assertEqual(result.stdout.strip(), "prepared 2")
                self.assertFalse(
                    any(fixture["tool_root"].rglob("__pycache__"))
                )

    @contextmanager
    def _isolated_payload_repair_locks(self, helper, fixture):
        order: list[str] = []

        def release_lock() -> None:
            order.append("release")

        def machine_lock(_paths, _runner_source_sha) -> int:
            order.append("qa")
            return os.open(
                fixture["machine_lock_path"],
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )

        with mock.patch.object(
            helper,
            "_require_release_lock",
            side_effect=release_lock,
        ), mock.patch.object(
            helper,
            "_open_machine_lock",
            side_effect=machine_lock,
        ), mock.patch.object(
            helper,
            "_assert_no_active_qa",
            return_value=None,
        ):
            yield order

    @contextmanager
    def _test_flocked_payload_validator(self, helper, fixture):
        real_run = subprocess.run

        def run_under_private_release_lock(command, *args, **kwargs):
            if (
                isinstance(command, (list, tuple))
                and len(command) >= 3
                and tuple(command[:3]) == ("/usr/bin/python3.12", "-I", "-B")
            ):
                command = [
                    "/usr/bin/flock",
                    str(fixture["release_lock_path"]),
                    *command,
                ]
            return real_run(command, *args, **kwargs)

        with mock.patch.object(
            helper.subprocess,
            "run",
            side_effect=run_under_private_release_lock,
        ):
            yield

    def _interrupt_payload_repair_subprocess(self, fixture, plan, mode: str) -> int:
        values = {
            "helper_path": str(SCRIPT.with_name("platform_live_qa_payload_bytecode_repair.py")),
            "app_dir": str(fixture["app_dir"]),
            "liveqa_root": str(fixture["trusted"]),
            "plan_root": str(fixture["plan_root"]),
            "quarantine_root": str(fixture["quarantine_root"]),
            "tool_root": str(fixture["tool_root"]),
            "release_lock": str(fixture["release_lock_path"]),
            "active_sha": fixture["active_source_sha"],
            "runner_sha": fixture["runner_source_sha"],
            "plan_sha": plan["plan_sha256"],
            "mode": mode,
        }
        script = f"""
import importlib.util, json, os, sys
from pathlib import Path
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    'bytecode_repair_interrupt_child', {json.dumps(values['helper_path'])}
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.RELEASE_LOCK_PATH = Path({json.dumps(values['release_lock'])})
module._assert_no_pending_operations = lambda *_args: None
module._assert_no_active_qa = lambda *_args: None
module._validate_active_payload = lambda *_args: None
interrupt_mode = {json.dumps(mode)}
real_rename = module._rename_noreplace
real_write_receipt = module._write_receipt
real_write_exclusive = module._write_exclusive
real_publish_initial = module._publish_initial_receipt
real_replace = os.replace
rename_count = 0
def interrupt_rename(*args, **kwargs):
    if interrupt_mode == 'prepared':
        os._exit(81)
    global rename_count
    result = real_rename(*args, **kwargs)
    rename_count += 1
    if interrupt_mode == 'rollback_gap' and rename_count == 2:
        os._exit(87)
    return result
def interrupt_receipt(path, value, expected_parent_identity):
    if interrupt_mode == 'moved' and value.get('phase') == 'quarantined':
        os._exit(82)
    result = real_write_receipt(path, value, expected_parent_identity)
    if interrupt_mode == 'quarantined' and value.get('phase') == 'quarantined':
        os._exit(83)
    return result
def interrupt_write_exclusive(path, raw, **kwargs):
    mode_bits = kwargs.get('mode', 0o600)
    if interrupt_mode == 'initial_temp' and Path(path).name.startswith('.intent.json.'):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode_bits)
        try:
            os.write(fd, raw[:max(1, len(raw) // 3)])
            os.fchown(fd, 0, 0)
            os.fchmod(fd, mode_bits)
            os.fsync(fd)
        finally:
            os.close(fd)
        os._exit(86)
    if (
        interrupt_mode in {'transition_temp_partial', 'transition_temp_zero'}
        and Path(path).name.startswith('.intent.json.')
        and json.loads(raw.decode('ascii')).get('phase') == 'quarantined'
    ):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode_bits)
        try:
            if interrupt_mode == 'transition_temp_partial':
                os.write(fd, raw[:max(1, len(raw) // 3)])
            os.fchown(fd, 0, 0)
            os.fchmod(fd, mode_bits)
            os.fsync(fd)
        finally:
            os.close(fd)
        os._exit(88 if interrupt_mode == 'transition_temp_partial' else 89)
    return real_write_exclusive(path, raw, **kwargs)
def interrupt_initial_publish(path, value, expected_parent_identity):
    if interrupt_mode == 'empty_qdir':
        os._exit(85)
    return real_publish_initial(path, value, expected_parent_identity)
def fail_validation(*_args):
    raise module.RepairError('validation_failed')
def interrupt_replace(source, destination, *args, **kwargs):
    if interrupt_mode == 'receipt_temp' and Path(source).name.startswith('.intent.json.') and Path(source).name.endswith('.tmp'):
        os._exit(84)
    return real_replace(source, destination, *args, **kwargs)
module._rename_noreplace = interrupt_rename
module._write_receipt = interrupt_receipt
module._write_exclusive = interrupt_write_exclusive
module._publish_initial_receipt = interrupt_initial_publish
if interrupt_mode == 'rollback_gap':
    module._validate_active_payload = fail_validation
os.replace = interrupt_replace
paths = module.RepairPaths(
    app_dir=Path({json.dumps(values['app_dir'])}),
    liveqa_root=Path({json.dumps(values['liveqa_root'])}),
    plan_root=Path({json.dumps(values['plan_root'])}),
    quarantine_root=Path({json.dumps(values['quarantine_root'])}),
    tool_root=Path({json.dumps(values['tool_root'])}),
)
os.write(2, f'CHILD_ID euid={{os.geteuid()}} egid={{os.getegid()}}\\n'.encode('ascii'))
module.apply_repair_plan(
    {json.dumps(values['plan_sha'])},
    {json.dumps(values['active_sha'])},
    {json.dumps(values['runner_sha'])},
    paths,
)
os._exit(0)
"""
        completed = subprocess.run(
            [
                "/usr/bin/flock",
                str(fixture["release_lock_path"]),
                sys.executable,
                "-I",
                "-B",
                "-",
            ],
            input=script,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        expected_exit = {
            "prepared": 81,
            "moved": 82,
            "quarantined": 83,
            "receipt_temp": 84,
            "empty_qdir": 85,
            "initial_temp": 86,
            "rollback_gap": 87,
            "transition_temp_partial": 88,
            "transition_temp_zero": 89,
        }[mode]
        self.assertEqual(
            completed.returncode,
            expected_exit,
            f"mode={mode} parent euid={os.geteuid()} egid={os.getegid()}; stderr={completed.stderr}",
        )
        self.assertEqual(completed.stdout, "")
        return completed.returncode

    def test_payload_bytecode_repair_roundtrip_preserves_payload(self) -> None:
        helper = self._payload_bytecode_repair_module()

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                manifest_path = fixture["trusted"] / "active-manifest.json"
                active_manifest_before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                pointers_before = tuple(
                    (path.lstat().st_ino, os.readlink(path))
                    for path in (
                        fixture["app_dir"] / "current",
                        fixture["app_dir"] / "previous",
                        fixture["trusted"] / "active",
                    )
                )
                original_bytes = {
                    path.name: path.read_bytes()
                    for path in fixture["cache_dir"].iterdir()
                }
                with (
                    self._isolated_payload_repair_locks(helper, fixture) as lock_order,
                    self._test_flocked_payload_validator(helper, fixture),
                ):
                    plan = helper.prepare_repair_plan(expected, runner, paths)
                    plan_file = fixture["plan_root"] / f"{plan['plan_sha256']}.json"
                    plan_stat = plan_file.lstat()
                    self.assertEqual(stat.S_IMODE(plan_stat.st_mode), 0o600)
                    self.assertEqual(
                        (plan_stat.st_uid, plan_stat.st_gid, plan_stat.st_nlink),
                        (0, 0, 1),
                    )
                    self.assertLessEqual(plan_stat.st_size, helper.MAX_PLAN_BYTES)
                    plan_raw = plan_file.read_bytes()
                    self.assertNotIn(str(Path(temporary)).encode(), plan_raw)
                    plan_envelope = json.loads(plan_raw.decode("ascii"))
                    self.assertEqual(set(plan_envelope), {"plan", "plan_sha256"})
                    self.assertEqual(
                        set(plan_envelope["plan"]),
                        {
                            "schema",
                            "runner_source_sha",
                            "active_source_sha",
                            "current_release",
                            "previous_release",
                            "active_manifest_sha256",
                            "payload_tree_sha256",
                            "cache_dir",
                            "extra_files",
                        },
                    )
                    self.assertEqual(
                        [
                            entry["relative_path"]
                            for entry in plan_envelope["plan"]["extra_files"]
                        ],
                        sorted(helper.EXTRA_FILE_RELATIVES),
                    )
                    self.assertEqual(
                        plan,
                        {
                            "status": "prepared",
                            "plan_sha256": plan["plan_sha256"],
                            "extra_file_count": 2,
                        },
                    )
                    result = helper.apply_repair_plan(
                        plan["plan_sha256"], expected, runner, paths
                    )
                self.assertEqual(lock_order, ["release", "qa", "release", "qa"])

                self.assertEqual(
                    result,
                    {
                        "status": "validated",
                        "plan_sha256": plan["plan_sha256"],
                        "quarantined_file_count": 2,
                    },
                )
                self.assertFalse(fixture["cache_dir"].exists())
                quarantined = list(fixture["quarantine_root"].rglob("*.pyc"))
                self.assertEqual(
                    {path.name: path.read_bytes() for path in quarantined},
                    original_bytes,
                )
                self.assertEqual(
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    active_manifest_before,
                )
                self.assertEqual(
                    tuple(
                        (path.lstat().st_ino, os.readlink(path))
                        for path in (
                            fixture["app_dir"] / "current",
                            fixture["app_dir"] / "previous",
                            fixture["trusted"] / "active",
                        )
                    ),
                    pointers_before,
                )
                runtime._validate_payload(
                    fixture["manifest"],
                    app_dir=fixture["app_dir"],
                    validate_provider=False,
                )
                quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                before_retry = {
                    path.relative_to(quarantine).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in quarantine.rglob("*")
                    if path.is_file()
                }
                quarantine_identity_before_retry = (
                    quarantine.lstat().st_dev,
                    quarantine.lstat().st_ino,
                    quarantine.lstat().st_mtime_ns,
                    quarantine.lstat().st_ctime_ns,
                )
                with (
                    self._isolated_payload_repair_locks(helper, fixture),
                    self._test_flocked_payload_validator(helper, fixture),
                ):
                    retry = helper.apply_repair_plan(
                        plan["plan_sha256"], expected, runner, paths
                    )
                self.assertEqual(retry, result)
                self.assertEqual(
                    {
                        path.relative_to(quarantine).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in quarantine.rglob("*")
                        if path.is_file()
                    },
                    before_retry,
                )
                quarantine_after_retry = quarantine.lstat()
                self.assertEqual(
                    (
                        quarantine_after_retry.st_dev,
                        quarantine_after_retry.st_ino,
                        quarantine_after_retry.st_mtime_ns,
                        quarantine_after_retry.st_ctime_ns,
                    ),
                    quarantine_identity_before_retry,
                )
                self.assertFalse(
                    any((fixture["payload"] / "platform/tools").rglob("__pycache__"))
                )

    def test_payload_bytecode_repair_resumes_persisted_crash_boundaries(self) -> None:
        helper = self._payload_bytecode_repair_module()

        for mode, exit_code, phase, cache_in_quarantine in (
            ("prepared", 81, "prepared", False),
            ("moved", 82, "prepared", True),
            ("quarantined", 83, "quarantined", True),
            ("receipt_temp", 84, "prepared", True),
            ("transition_temp_partial", 88, "prepared", True),
            ("transition_temp_zero", 89, "prepared", True),
            ("empty_qdir", 85, None, False),
            ("initial_temp", 86, None, False),
            ("rollback_gap", 87, "quarantined", False),
        ):
            with self.subTest(interruption=mode), tempfile.TemporaryDirectory(dir="/root") as temporary:
                with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                    paths = self._payload_bytecode_repair_paths(helper, fixture)
                    expected = fixture["active_source_sha"]
                    runner = fixture["runner_source_sha"]
                    with self._isolated_payload_repair_locks(helper, fixture):
                        plan = helper.prepare_repair_plan(expected, runner, paths)

                    self.assertEqual(
                        self._interrupt_payload_repair_subprocess(fixture, plan, mode),
                        exit_code,
                    )
                    quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                    receipt_path = quarantine / "intent.json"
                    if phase is None:
                        self.assertFalse(receipt_path.exists())
                        if mode in {"initial_temp", "transition_temp_partial", "transition_temp_zero"}:
                            temps = list(quarantine.glob(".intent.json.*.tmp"))
                            self.assertEqual(len(temps), 1)
                            if mode == "transition_temp_zero":
                                self.assertEqual(temps[0].stat().st_size, 0)
                            else:
                                self.assertGreater(temps[0].stat().st_size, 0)
                                self.assertLess(temps[0].stat().st_size, 200)
                        else:
                            self.assertEqual(list(quarantine.iterdir()), [])
                        with (
                            self._isolated_payload_repair_locks(helper, fixture),
                            self._test_flocked_payload_validator(helper, fixture),
                        ):
                            result = helper.apply_repair_plan(
                                plan["plan_sha256"], expected, runner, paths
                            )
                        self.assertEqual(result["status"], "validated")
                        self.assertFalse(fixture["cache_dir"].exists())
                        self.assertEqual(
                            {entry.name for entry in quarantine.iterdir()},
                            {"intent.json", "bytecode-cache"},
                        )
                        self.assertEqual(
                            json.loads(receipt_path.read_text(encoding="ascii"))["phase"],
                            "validated",
                        )
                        self.assertTrue((quarantine / "bytecode-cache").is_dir())
                        continue

                    if mode == "rollback_gap":
                        self.assertFalse((quarantine / "bytecode-cache").exists())
                        self.assertTrue(fixture["cache_dir"].is_dir())
                        with self._isolated_payload_repair_locks(helper, fixture):
                            self._assert_payload_repair_refusal(
                                helper,
                                "plan_mismatch",
                                lambda: helper.apply_repair_plan(
                                    plan["plan_sha256"], expected, runner, paths
                                ),
                            )
                        repaired_receipt = json.loads(
                            receipt_path.read_text(encoding="ascii")
                        )
                        self.assertEqual(repaired_receipt["phase"], "rolled_back")
                        with (
                            self._isolated_payload_repair_locks(helper, fixture),
                            self._test_flocked_payload_validator(helper, fixture),
                        ):
                            fresh_plan = helper.prepare_repair_plan(expected, runner, paths)
                            result = helper.apply_repair_plan(
                                fresh_plan["plan_sha256"], expected, runner, paths
                            )
                        self.assertNotEqual(fresh_plan["plan_sha256"], plan["plan_sha256"])
                        self.assertEqual(result["status"], "validated")
                        continue

                    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
                    self.assertEqual(
                        set(receipt),
                        {"schema", "plan_sha256", "phase", "quarantine_identity"},
                    )
                    self.assertEqual(receipt["phase"], phase)
                    self.assertEqual(receipt["plan_sha256"], plan["plan_sha256"])
                    if mode == "receipt_temp":
                        temporary_receipts = list(quarantine.glob(".intent.json.*.tmp"))
                        self.assertEqual(len(temporary_receipts), 1)
                    self.assertEqual(
                        (fixture["cache_dir"].exists(), (quarantine / "bytecode-cache").exists()),
                        (not cache_in_quarantine, cache_in_quarantine),
                    )

                    with (
                        self._isolated_payload_repair_locks(helper, fixture),
                        self._test_flocked_payload_validator(helper, fixture),
                    ):
                        result = helper.apply_repair_plan(
                            plan["plan_sha256"], expected, runner, paths
                        )
                    self.assertEqual(result["status"], "validated")
                    self.assertFalse(fixture["cache_dir"].exists())
                    self.assertTrue((quarantine / "bytecode-cache").is_dir())
                    final_receipt = json.loads(receipt_path.read_text(encoding="ascii"))
                    self.assertEqual(final_receipt["phase"], "validated")
                    self.assertEqual(list(quarantine.glob(".intent.json.*.tmp")), [])
                    runtime._validate_payload(
                        fixture["manifest"],
                        app_dir=fixture["app_dir"],
                        validate_provider=False,
                    )

    def test_payload_bytecode_repair_refuses_ambiguous_and_spent_resume_states(self) -> None:
        helper = self._payload_bytecode_repair_module()

        def prepare(fixture, paths):
            with self._isolated_payload_repair_locks(helper, fixture):
                return helper.prepare_repair_plan(
                    fixture["active_source_sha"], fixture["runner_source_sha"], paths
                )

        def apply(fixture, paths, plan):
            return helper.apply_repair_plan(
                plan["plan_sha256"],
                fixture["active_source_sha"],
                fixture["runner_source_sha"],
                paths,
            )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(fixture, plan, "moved"),
                    82,
                )
                fixture["cache_dir"].mkdir(mode=0o755)
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "quarantine_exists",
                        lambda: apply(fixture, paths, plan),
                    )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(fixture, plan, "initial_temp"),
                    86,
                )
                quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                partial_temp = next(quarantine.glob(".intent.json.*.tmp"))
                external_link = fixture["quarantine_root"] / "external-temp-link"
                os.link(partial_temp, external_link)
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "manifest_mismatch",
                        lambda: apply(fixture, paths, plan),
                    )
                self.assertTrue(partial_temp.exists())
                self.assertTrue(external_link.exists())

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(
                        fixture, plan, "transition_temp_partial"
                    ),
                    88,
                )
                quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                partial_temp = next(quarantine.glob(".intent.json.*.tmp"))
                partial_temp.write_bytes(b"not a canonical transition prefix")
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "quarantine_exists",
                        lambda: apply(fixture, paths, plan),
                    )
                self.assertTrue(partial_temp.exists())

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(fixture, plan, "prepared"),
                    81,
                )
                receipt_path = (
                    fixture["quarantine_root"] / plan["plan_sha256"] / "intent.json"
                )
                receipt = json.loads(receipt_path.read_text(encoding="ascii"))
                receipt["quarantine_identity"]["inode"] += 1
                receipt_path.write_text(
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chmod(receipt_path, 0o600)
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "quarantine_exists",
                        lambda: apply(fixture, paths, plan),
                    )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(fixture, plan, "moved"),
                    82,
                )
                quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                (quarantine / "unexpected-state").write_text("x", encoding="ascii")
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "quarantine_exists",
                        lambda: apply(fixture, paths, plan),
                    )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                plan = prepare(fixture, paths)
                self.assertEqual(
                    self._interrupt_payload_repair_subprocess(fixture, plan, "prepared"),
                    81,
                )
                shutil.rmtree(fixture["cache_dir"])
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "quarantine_exists",
                        lambda: apply(fixture, paths, plan),
                    )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                plan = prepare(fixture, paths)
                with (
                    self._isolated_payload_repair_locks(helper, fixture),
                    mock.patch.object(
                        helper,
                        "_validate_active_payload",
                        side_effect=helper.RepairError("validation_failed"),
                    ),
                ):
                    self._assert_payload_repair_refusal(
                        helper,
                        "validation_failed",
                        lambda: apply(fixture, paths, plan),
                    )
                quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
                receipt = json.loads((quarantine / "intent.json").read_text(encoding="ascii"))
                self.assertEqual(receipt["phase"], "rolled_back")
                self.assertTrue(fixture["cache_dir"].is_dir())
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "plan_mismatch",
                        lambda: apply(fixture, paths, plan),
                    )
                    fresh_plan = helper.prepare_repair_plan(expected, runner, paths)
                self.assertNotEqual(fresh_plan["plan_sha256"], plan["plan_sha256"])
                with (
                    self._isolated_payload_repair_locks(helper, fixture),
                    self._test_flocked_payload_validator(helper, fixture),
                ):
                    fresh_result = helper.apply_repair_plan(
                        fresh_plan["plan_sha256"], expected, runner, paths
                    )
                self.assertEqual(fresh_result["status"], "validated")

    def test_payload_bytecode_repair_rechecks_plan_payload_and_pointer_identity(self) -> None:
        helper = self._payload_bytecode_repair_module()

        def exercise(
            case: str,
            expected_reason: str,
            mutate_before,
            mutate_after=None,
            apply_operation=None,
        ) -> None:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir="/root") as temporary:
                with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                    paths = self._payload_bytecode_repair_paths(helper, fixture)
                    expected = fixture["active_source_sha"]
                    runner = fixture["runner_source_sha"]
                    with self._isolated_payload_repair_locks(helper, fixture):
                        plan = None
                        if mutate_after is not None or apply_operation is not None:
                            plan = helper.prepare_repair_plan(expected, runner, paths)
                            if mutate_after is not None:
                                mutate_after(fixture, plan)
                            operation = (
                                apply_operation(fixture, plan, paths)
                                if apply_operation is not None
                                else lambda: helper.apply_repair_plan(
                                    plan["plan_sha256"], expected, runner, paths
                                )
                            )
                        else:
                            mutate_before(fixture)

                            def operation():
                                return helper.prepare_repair_plan(expected, runner, paths)
                        self._assert_payload_repair_refusal(
                            helper,
                            expected_reason,
                            operation,
                        )

        def remove_expected(fixture) -> None:
            next(fixture["cache_dir"].glob("platform_live_qa_guard.*.pyc")).unlink()

        def add_file(fixture) -> None:
            (fixture["cache_dir"] / "unexpected.pyc").write_bytes(b"extra\n")

        def add_directory(fixture) -> None:
            (fixture["cache_dir"] / "nested").mkdir()

        def add_top_level_empty_directory(fixture) -> None:
            (fixture["payload"] / "unexpected-empty").mkdir(mode=0o755)

        def add_symlink(fixture) -> None:
            (fixture["cache_dir"] / "unexpected.pyc").symlink_to(
                "platform_live_qa_guard.cpython-312.pyc"
            )

        def add_hardlink(fixture) -> None:
            cache = next(fixture["cache_dir"].glob("platform_live_qa_guard.*.pyc"))
            outside = fixture["cache_dir"].parent / "linked-bytecode"
            outside.write_bytes(cache.read_bytes())
            cache.unlink()
            os.link(outside, cache)

        def change_mode(fixture) -> None:
            cache = next(fixture["cache_dir"].glob("platform_live_qa_guard.*.pyc"))
            os.chmod(cache, 0o600)

        def change_owner(fixture) -> None:
            cache = next(fixture["cache_dir"].glob("platform_live_qa_guard.*.pyc"))
            os.chown(cache, 65534, 65534)

        def change_expected_file(fixture, _plan) -> None:
            cache = next(fixture["cache_dir"].glob("platform_live_qa_guard.*.pyc"))
            cache.write_bytes(b"replaced after planning\n")

        def change_pointer(fixture, _plan) -> None:
            pointer = fixture["app_dir"] / "current"
            pointer.unlink()
            pointer.symlink_to(fixture["app_dir"] / "releases" / "release-missing")

        def change_manifest(fixture, _plan) -> None:
            manifest = fixture["trusted"] / "active-manifest.json"
            os.chmod(manifest, 0o644)
            manifest.write_bytes(manifest.read_bytes() + b" ")
            os.chmod(manifest, 0o444)

        def change_previous_pointer(fixture, _plan) -> None:
            previous_release = fixture["app_dir"] / "releases" / "release-dddddddd"
            previous_release.mkdir(mode=0o755)
            (previous_release / "RELEASE.json").write_text(
                json.dumps(
                    {
                        "source_git_commit": "d" * 40,
                        "release_slug": previous_release.name,
                    }
                )
                + "\n",
                encoding="ascii",
            )
            os.chmod(previous_release / "RELEASE.json", 0o444)
            pointer = fixture["app_dir"] / "previous"
            pointer.unlink()
            pointer.symlink_to(previous_release)

        def change_plan(fixture, _plan) -> None:
            plan_files = list(fixture["plan_root"].iterdir())
            self.assertEqual(len(plan_files), 1)
            envelope = json.loads(plan_files[0].read_text(encoding="ascii"))
            envelope["plan"]["runner_source_sha"] = "e" * 40
            plan_files[0].write_text(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )

        def replace_cache_directory(fixture, _plan) -> None:
            original = fixture["cache_dir"]
            backup = fixture["payload"].parent / "__cache_backup"
            original.rename(backup)
            original.mkdir(mode=0o755)
            for entry in backup.iterdir():
                destination = original / entry.name
                destination.write_bytes(entry.read_bytes())
                os.chmod(destination, 0o644)

        def occupy_quarantine(fixture, plan) -> None:
            quarantine = fixture["quarantine_root"] / plan["plan_sha256"]
            quarantine.mkdir(mode=0o700)
            (quarantine / "unrecognized").write_text("x", encoding="ascii")

        def apply_wrong_source(fixture, plan, paths):
            return lambda: helper.apply_repair_plan(
                plan["plan_sha256"],
                "d" * 40,
                fixture["runner_source_sha"],
                paths,
            )

        def apply_wrong_runner(fixture, plan, paths):
            return lambda: helper.apply_repair_plan(
                plan["plan_sha256"],
                fixture["active_source_sha"],
                "e" * 40,
                paths,
            )

        cases = (
            ("missing_expected", "unexpected_tree", remove_expected, None),
            ("third_file", "unexpected_tree", add_file, None),
            ("third_directory", "manifest_mismatch", add_directory, None),
            (
                "top_level_empty_directory",
                "manifest_mismatch",
                add_top_level_empty_directory,
                None,
            ),
            ("symlink", "unexpected_tree", add_symlink, None),
            ("hardlink", "manifest_mismatch", add_hardlink, None),
            ("mode_drift", "extra_identity_mismatch", change_mode, None),
            ("owner_drift", "manifest_mismatch", change_owner, None),
            ("post_plan_file_change", "extra_identity_mismatch", None, change_expected_file),
            ("pointer_change", "pointer_mismatch", None, change_pointer),
            ("manifest_change", "plan_mismatch", None, change_manifest),
            ("previous_pointer_change", "plan_mismatch", None, change_previous_pointer),
            ("plan_change", "plan_mismatch", None, change_plan),
            (
                "cache_directory_replacement",
                "extra_identity_mismatch",
                None,
                replace_cache_directory,
            ),
            ("quarantine_collision", "quarantine_exists", None, occupy_quarantine),
            ("wrong_active_sha_on_apply", "plan_mismatch", None, None, apply_wrong_source),
            (
                "wrong_runner_sha_on_apply",
                "source_bundle_mismatch",
                None,
                None,
                apply_wrong_runner,
            ),
        )
        for case_entry in cases:
            case, reason, before, after = case_entry[:4]
            apply_operation = case_entry[4] if len(case_entry) == 5 else None
            exercise(case, reason, before, after, apply_operation)

    def test_payload_bytecode_repair_binds_requested_sources_and_refuses_busy_state(self) -> None:
        helper = self._payload_bytecode_repair_module()

        for guard_name, reason in (
            ("_assert_no_pending_operations", "pending_operation"),
            ("_assert_no_active_qa", "qa_busy"),
        ):
            with (
                self.subTest(guard=guard_name),
                tempfile.TemporaryDirectory(dir="/root") as temporary,
            ):
                with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                    paths = self._payload_bytecode_repair_paths(helper, fixture)
                    with self._isolated_payload_repair_locks(helper, fixture), mock.patch.object(
                        helper,
                        guard_name,
                        side_effect=helper.RepairError(reason),
                    ):
                        self._assert_payload_repair_refusal(
                            helper,
                            reason,
                            lambda: helper.prepare_repair_plan(
                                fixture["active_source_sha"],
                                fixture["runner_source_sha"],
                                paths,
                            ),
                        )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                with mock.patch.object(
                    helper,
                    "_require_release_lock",
                    side_effect=helper.RepairError("lock_unavailable"),
                ):
                    self._assert_payload_repair_refusal(
                        helper,
                        "lock_unavailable",
                        lambda: helper.prepare_repair_plan(
                            fixture["active_source_sha"],
                            fixture["runner_source_sha"],
                            paths,
                        ),
                    )

    def test_payload_bytecode_repair_uses_verified_guard_and_fails_closed_on_stale_qa(self) -> None:
        helper = self._payload_bytecode_repair_module()
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                namespace = helper._load_verified_guard(paths, runner)
                self.assertIn("assert_liveqa_idle", namespace)
                self.assertIn("_assert_runtime_unit_collected", namespace)
                self.assertFalse(
                    any(fixture["tool_root"].rglob("__pycache__"))
                )

                # Exercise the real guard's conjunction with injected closed
                # process inventories, without reading production cgroup or
                # UID state from this test process.
                namespace["_assert_runtime_unit_collected"] = lambda: None
                for probe_name in (
                    "_liveqa_cgroup_process_ids",
                    "_liveqa_process_ids",
                ):
                    with self.subTest(stale_boundary=probe_name):
                        namespace["_liveqa_cgroup_process_ids"] = lambda: ()
                        namespace["_liveqa_process_ids"] = lambda: ()
                        namespace[probe_name] = lambda: (4242,)
                        with mock.patch.object(
                            helper,
                            "_load_verified_guard",
                            return_value=namespace,
                        ):
                            self._assert_payload_repair_refusal(
                                helper,
                                "qa_busy",
                                lambda: helper._assert_no_active_qa(paths, runner),
                            )

                namespace["_liveqa_cgroup_process_ids"] = lambda: ()
                namespace["_liveqa_process_ids"] = lambda: ()

                def transient_unit_present() -> None:
                    raise RuntimeError("closed test refusal")

                namespace["_assert_runtime_unit_collected"] = transient_unit_present
                with mock.patch.object(
                    helper,
                    "_load_verified_guard",
                    return_value=namespace,
                ):
                    self._assert_payload_repair_refusal(
                        helper,
                        "qa_busy",
                        lambda: helper._assert_no_active_qa(paths, runner),
                    )

                with self._isolated_payload_repair_locks(helper, fixture), mock.patch.object(
                    helper,
                    "_assert_no_active_qa",
                    return_value=None,
                ):
                    self.assertEqual(
                        helper.prepare_repair_plan(expected, runner, paths)["status"],
                        "prepared",
                    )

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                with self._isolated_payload_repair_locks(helper, fixture):
                    self._assert_payload_repair_refusal(
                        helper,
                        "pointer_mismatch",
                        lambda: helper.prepare_repair_plan("d" * 40, runner, paths),
                    )
                    plan = helper.prepare_repair_plan(expected, runner, paths)
                    self._assert_payload_repair_refusal(
                        helper,
                        "plan_mismatch",
                        lambda: helper.apply_repair_plan(
                            plan["plan_sha256"], "d" * 40, runner, paths
                        ),
                    )
                    self._assert_payload_repair_refusal(
                        helper,
                        "source_bundle_mismatch",
                        lambda: helper.apply_repair_plan(
                            plan["plan_sha256"], expected, "e" * 40, paths
                        ),
                    )
                    self._assert_payload_repair_refusal(
                        helper,
                        "runner_mismatch",
                        lambda: helper.apply_repair_plan(
                            plan["plan_sha256"], expected, "invalid", paths
                        ),
                    )

    def test_payload_bytecode_repair_restores_exact_cache_on_postcheck_failure(self) -> None:
        helper = self._payload_bytecode_repair_module()

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                before_identity = fixture["cache_dir"].lstat().st_ino
                before_bytes = {
                    path.name: path.read_bytes()
                    for path in fixture["cache_dir"].iterdir()
                }
                with self._isolated_payload_repair_locks(helper, fixture):
                    plan = helper.prepare_repair_plan(expected, runner, paths)
                    with mock.patch.object(
                        helper,
                        "_validate_active_payload",
                        side_effect=helper.RepairError("validation_failed"),
                    ):
                        self._assert_payload_repair_refusal(
                            helper,
                            "validation_failed",
                            lambda: helper.apply_repair_plan(
                                plan["plan_sha256"], expected, runner, paths
                            ),
                        )

                self.assertEqual(fixture["cache_dir"].lstat().st_ino, before_identity)
                self.assertEqual(
                    {path.name: path.read_bytes() for path in fixture["cache_dir"].iterdir()},
                    before_bytes,
                )
                self.assertFalse(
                    any(fixture["quarantine_root"].rglob("*.pyc"))
                )

    def test_payload_bytecode_repair_receipt_failures_follow_commit_boundary(self) -> None:
        helper = self._payload_bytecode_repair_module()
        for failure_phase in ("intent", "quarantined", "validated"):
            with (
                self.subTest(receipt_phase=failure_phase),
                tempfile.TemporaryDirectory(dir="/root") as temporary,
            ):
                with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                    paths = self._payload_bytecode_repair_paths(helper, fixture)
                    expected = fixture["active_source_sha"]
                    runner = fixture["runner_source_sha"]
                    cache_dir = fixture["cache_dir"]
                    cache_inode = cache_dir.lstat().st_ino
                    original_bytes = {
                        path.name: path.read_bytes() for path in cache_dir.iterdir()
                    }
                    with self._isolated_payload_repair_locks(helper, fixture):
                        plan = helper.prepare_repair_plan(expected, runner, paths)
                        if failure_phase == "intent":
                            def fail_intent(_path, _value, _parent_identity):
                                raise helper.RepairError("plan_unavailable")

                            with mock.patch.object(
                                helper,
                                "_publish_initial_receipt",
                                side_effect=fail_intent,
                            ):
                                self._assert_payload_repair_refusal(
                                    helper,
                                    "plan_unavailable",
                                    lambda: helper.apply_repair_plan(
                                        plan["plan_sha256"], expected, runner, paths
                                    ),
                                )
                            self.assertEqual(cache_dir.lstat().st_ino, cache_inode)
                            self.assertEqual(
                                {path.name: path.read_bytes() for path in cache_dir.iterdir()},
                                original_bytes,
                            )
                            self.assertEqual(list(fixture["quarantine_root"].iterdir()), [])
                            with self._test_flocked_payload_validator(helper, fixture):
                                result = helper.apply_repair_plan(
                                    plan["plan_sha256"], expected, runner, paths
                                )
                            self.assertEqual(result["status"], "validated")
                            continue

                        write_receipt = helper._write_receipt

                        def fail_receipt(path, value, expected_parent_identity):
                            if value.get("phase") == failure_phase:
                                raise helper.RepairError("receipt_write_failed")
                            return write_receipt(path, value, expected_parent_identity)

                        validator = mock.patch.object(
                            helper,
                            "_validate_active_payload",
                            wraps=helper._validate_active_payload,
                        )
                        with (
                            mock.patch.object(
                                helper,
                                "_write_receipt",
                                side_effect=fail_receipt,
                            ),
                            validator as validate_payload,
                            self._test_flocked_payload_validator(helper, fixture),
                        ):
                            self._assert_payload_repair_refusal(
                                helper,
                                "receipt_write_failed",
                                lambda: helper.apply_repair_plan(
                                    plan["plan_sha256"], expected, runner, paths
                                ),
                            )

                        quarantine_dir = fixture["quarantine_root"] / plan["plan_sha256"]
                        if failure_phase == "quarantined":
                            self.assertEqual(validate_payload.call_count, 0)
                            self.assertEqual(cache_dir.lstat().st_ino, cache_inode)
                            self.assertEqual(
                                {path.name: path.read_bytes() for path in cache_dir.iterdir()},
                                original_bytes,
                            )
                            self.assertFalse(
                                any(quarantine_dir.rglob("*.pyc"))
                            )
                            receipt = json.loads(
                                (quarantine_dir / "intent.json").read_text(encoding="ascii")
                            )
                            self.assertEqual(receipt["phase"], "rolled_back")
                        else:
                            self.assertEqual(validate_payload.call_count, 1)
                            self.assertFalse(cache_dir.exists())
                            self.assertEqual(
                                {path.name: path.read_bytes() for path in quarantine_dir.rglob("*.pyc")},
                                original_bytes,
                            )
                            runtime._validate_payload(
                                fixture["manifest"],
                                app_dir=fixture["app_dir"],
                                validate_provider=False,
                            )
                            self.assertFalse(
                                any(
                                    (fixture["payload"] / "platform/tools").rglob("__pycache__")
                                )
                            )

    def test_payload_bytecode_repair_no_replace_and_rollback_collisions(self) -> None:
        helper = self._payload_bytecode_repair_module()
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            source_parent = root / "source-parent"
            destination_parent = root / "destination-parent"
            source_parent.mkdir(mode=0o700)
            destination_parent.mkdir(mode=0o700)
            source = source_parent / "cache"
            destination = destination_parent / "cache"
            source.mkdir(mode=0o700)
            destination.mkdir(mode=0o700)
            (source / "source-marker").write_bytes(b"source\n")
            (destination / "destination-marker").write_bytes(b"destination\n")
            source_identity = helper._identity(source.lstat())
            destination_inode = destination.lstat().st_ino
            with self.assertRaises(helper.RepairError) as raised:
                helper._rename_noreplace(
                    source_parent,
                    source.name,
                    destination_parent,
                    destination.name,
                    source_identity,
                    failure_reason="extra_identity_mismatch",
                )
            self.assertEqual(raised.exception.reason, "quarantine_exists")
            self.assertEqual(source.lstat().st_ino, source_identity["inode"])
            self.assertEqual(destination.lstat().st_ino, destination_inode)
            self.assertEqual((destination / "destination-marker").read_bytes(), b"destination\n")

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self._payload_bytecode_repair_fixture(Path(temporary)) as fixture:
                paths = self._payload_bytecode_repair_paths(helper, fixture)
                expected = fixture["active_source_sha"]
                runner = fixture["runner_source_sha"]
                cache_dir = fixture["cache_dir"]
                original_inode = cache_dir.lstat().st_ino
                original_bytes = {
                    path.name: path.read_bytes() for path in cache_dir.iterdir()
                }

                def inject_rollback_collision(_paths, _source_sha) -> None:
                    cache_dir.mkdir(mode=0o755)
                    (cache_dir / "racer-marker").write_bytes(b"racer\n")
                    raise helper.RepairError("validation_failed")

                with self._isolated_payload_repair_locks(helper, fixture):
                    plan = helper.prepare_repair_plan(expected, runner, paths)
                    with mock.patch.object(
                        helper,
                        "_validate_active_payload",
                        side_effect=inject_rollback_collision,
                    ):
                        self._assert_payload_repair_refusal(
                            helper,
                            "rollback_failed",
                            lambda: helper.apply_repair_plan(
                                plan["plan_sha256"], expected, runner, paths
                            ),
                        )

                self.assertNotEqual(cache_dir.lstat().st_ino, original_inode)
                self.assertEqual((cache_dir / "racer-marker").read_bytes(), b"racer\n")
                quarantined = (
                    fixture["quarantine_root"]
                    / plan["plan_sha256"]
                    / "bytecode-cache"
                )
                self.assertEqual(
                    {path.name: path.read_bytes() for path in quarantined.iterdir()},
                    original_bytes,
                )

    def test_source_tree_cache_projection_omits_paired_bytecode_and_cache_only_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            source = self._source_tree_fixture(
                root,
                cache_relative="pkg/__pycache__/module.cpython-312.pyc",
            )
            destination = root / "payload"
            copied = runtime._copy_tree(
                source,
                destination,
                exclude_source_tree_caches=True,
            )
            self.assertEqual(copied, {"pkg/module.py": hashlib.sha256(b"VALUE = 1\n").hexdigest()})
            self.assertTrue((destination / "pkg/module.py").is_file())
            self.assertFalse((destination / "pkg/__pycache__").exists())
            self.assertEqual(
                runtime._source_tree_cache_relative("__pycache__/module.cpython-312.pyc"),
                "module.py",
            )

    def test_source_tree_cache_projection_refuses_unpaired_and_nested_cache_members(self) -> None:
        cases = (
            "pkg/__pycache__/orphan.cpython-312.pyc",
            "pkg/__pycache__/nested/__pycache__/module.cpython-312.pyc",
        )
        for cache_relative in cases:
            with self.subTest(cache_relative=cache_relative), tempfile.TemporaryDirectory(dir="/root") as temporary:
                root = Path(temporary)
                source = self._source_tree_fixture(root, cache_relative=cache_relative)
                with self.assertRaises(runtime.InstallerError):
                    runtime._copy_tree(
                        source,
                        root / "payload",
                        exclude_source_tree_caches=True,
                    )

    def test_runtime_cache_copy_stays_exact_and_manifest_rejects_injected_payload_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            runtime_source = root / "runtime-source"
            package = runtime_source / "pkg"
            cache = package / "__pycache__" / "module.cpython-312.pyc"
            cache.parent.mkdir(mode=0o755, parents=True)
            (package / "module.py").write_bytes(b"VALUE = 2\n")
            (package / "module.py").chmod(0o444)
            cache.write_bytes(b"runtime artifact bytes\n")
            cache.chmod(0o444)
            copied_to = root / "runtime-copy"
            copied = runtime._copy_tree(runtime_source, copied_to)
            self.assertEqual(copied["pkg/__pycache__/module.cpython-312.pyc"], hashlib.sha256(cache.read_bytes()).hexdigest())
            self.assertEqual(
                (copied_to / "pkg/__pycache__/module.cpython-312.pyc").read_bytes(),
                cache.read_bytes(),
            )
            with self.assertRaises(runtime.InstallerError):
                runtime._copy_tree(
                    runtime_source,
                    root / "runtime-filtered",
                    exclude_source_tree_caches=True,
                )

            source_sha = "e" * 40
            with self.runtime_tree(root, source_sha) as (app_dir, release, _trusted, _payload_root):
                manifest = runtime.install(app_dir, release)
                injected = Path(str(manifest["payload"])) / "platform/apps/platform_api/__pycache__/injected.cpython-312.pyc"
                injected.parent.mkdir(mode=0o555, parents=True)
                injected.parent.chmod(0o555)
                injected.write_bytes(b"unexpected cache bytes\n")
                injected.chmod(0o444)
                with mock.patch.object(runtime, "_validate_payload", new=ORIGINAL_VALIDATE_PAYLOAD):
                    with self.assertRaisesRegex(runtime.InstallerError, "payload digest"):
                        runtime.verify(app_dir, source_sha)

    def _engine_provider_fixture(self, root: Path) -> tuple[Path, dict[str, str], str]:
        engine = root / "runtime"
        paths = [
            "node/bin",
            "browsers/chromium-1228/chrome-linux64",
            "browsers/chromium_headless_shell-1228",
            "browsers/webkit-2311",
            "browsers/ffmpeg-1011",
            "web/tests/smoke",
            "web/tests/support",
            "web/node_modules/@playwright/test",
            "web/node_modules/playwright",
            "web/node_modules/playwright-core",
        ]
        for relative in paths:
            (engine / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        files = {
            "node/bin/node": (b"node\n", 0o555),
            "browsers/chromium-1228/chrome-linux64/chrome_sandbox": (b"sandbox\n", 0o4755),
            "browsers/chromium_headless_shell-1228/shell": (b"headless\n", 0o555),
            "browsers/webkit-2311/browser": (b"webkit\n", 0o555),
            "browsers/ffmpeg-1011/ffmpeg": (b"ffmpeg\n", 0o555),
            "web/package-lock.json": (b"lock\n", 0o444),
            "web/playwright.live.config.ts": (b"config\n", 0o444),
            "web/tests/smoke/live-launch.spec.ts": (b"launch\n", 0o444),
            "web/tests/smoke/live-user-journey.spec.ts": (b"journey\n", 0o444),
            "web/tests/support/live-qa-origin.ts": (b"origin\n", 0o444),
            "web/tests/support/live-qa-sandbox.ts": (b"support\n", 0o444),
            "web/tests/support/live-count-reporter.cjs": (b"reporter\n", 0o444),
            "web/node_modules/@playwright/test/package.json": (b'{"name":"test"}\n', 0o444),
            "web/node_modules/@playwright/test/index.js": (b"test\n", 0o444),
            "web/node_modules/playwright/package.json": (b'{"name":"playwright"}\n', 0o444),
            "web/node_modules/playwright/index.js": (b"playwright\n", 0o444),
            "web/node_modules/playwright-core/package.json": (b'{"name":"core"}\n', 0o444),
            "web/node_modules/playwright-core/index.js": (b"core\n", 0o444),
        }
        for relative, (content, mode) in files.items():
            path = engine / relative
            path.write_bytes(content)
            os.chmod(path, mode)
        for path in sorted(engine.rglob("*"), reverse=True):
            if path.is_dir():
                os.chmod(path, 0o555)
        os.chmod(engine, 0o555)
        sandbox = engine / runtime.RUNTIME_SANDBOX_RELATIVE
        with mock.patch.multiple(
            runtime,
            CHROMIUM_SANDBOX_SIZE=sandbox.stat().st_size,
            CHROMIUM_SANDBOX_SHA256=hashlib.sha256(sandbox.read_bytes()).hexdigest(),
        ):
            file_map = runtime._runtime_file_map(
                engine,
                prefixes=("node/", "browsers/", "web/node_modules/"),
            )
        lock_sha = runtime._digest_regular(engine / "web/package-lock.json")
        return engine, file_map, lock_sha

    def test_changed_locked_dependency_bytes_fail_engine_provider_validation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            engine, file_map, lock_sha = self._engine_provider_fixture(root)
            sandbox_path = engine / runtime.RUNTIME_SANDBOX_RELATIVE
            sandbox_sha = runtime._digest_regular(sandbox_path, allow_sandbox=True)
            with mock.patch.multiple(
                runtime,
                CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ):
                engine_sha = runtime._engine_digest(
                    file_map, node_version="26.3.1", package_lock_sha256=lock_sha
                )
                runtime._validate_engine_root(
                    engine,
                    expected_files=file_map,
                    engine_sha256=engine_sha,
                    node_version="26.3.1",
                    package_lock_sha256=lock_sha,
                )
                dependency = engine / "web/node_modules/playwright-core/index.js"
                os.chmod(dependency, 0o644)
                dependency.write_bytes(b"changed dependency bytes\n")
                os.chmod(dependency, 0o444)
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_engine_root(
                        engine,
                        expected_files=file_map,
                        engine_sha256=engine_sha,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )

    def test_typed_provider_rebinds_identical_engine_to_changed_app_lock(self) -> None:
        self.assertEqual(
            runtime._provider_disk_floor(40 * 1024**3),
            5 * 1024**3 + 128 * 1024**2,
        )
        self.assertEqual(
            runtime._provider_disk_floor(100 * 1024**3),
            10 * 1024**3 + 128 * 1024**2,
        )
        self.assertEqual(
            runtime._provider_disk_floor(100 * 1024**3 + 1),
            10 * 1024**3 + 128 * 1024**2 + 1,
        )
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            old_sha, candidate_sha = "a" * 40, "b" * 40
            source, engine_files, old_lock_sha = self._engine_provider_fixture(root / "source")
            payload_root = root / "trusted" / "releases"
            old_payload = payload_root / old_sha
            old_payload.mkdir(mode=0o555, parents=True)
            provider_root = old_payload / "provider-runtime"
            new_suite = root / "new-suite"
            (new_suite / "web").mkdir(mode=0o700, parents=True)
            new_lock = new_suite / "web/package-lock.json"
            new_lock.write_bytes(b"lock with an application-only dependency update\n")
            os.chmod(new_lock, 0o444)
            new_lock_sha = runtime._digest_regular(new_lock)
            engine_links = {"browsers/webkit-2311/browser-alias": "browser"}
            old_engine_sha = runtime._engine_map_digest(
                engine_files, engine_links, node_version="26.3.1", package_lock_sha256=old_lock_sha
            )
            new_engine_sha = runtime._engine_map_digest(
                engine_files, engine_links, node_version="26.3.1", package_lock_sha256=new_lock_sha
            )
            sandbox = source / runtime.RUNTIME_SANDBOX_RELATIVE
            with mock.patch.multiple(
                runtime,
                CHROMIUM_SANDBOX_SIZE=sandbox.stat().st_size,
                CHROMIUM_SANDBOX_SHA256=hashlib.sha256(sandbox.read_bytes()).hexdigest(),
            ):
                runtime._copy_typed_engine_provider(
                    source, source, provider_root,
                    engine_files=engine_files, engine_links=engine_links, package_lock_sha256=old_lock_sha,
                )
                old_manifest = {
                    "version": 3, "node_version": "26.3.1",
                    "package_lock_sha256": old_lock_sha,
                    "engine_tree_sha256": old_engine_sha,
                    "engine_files": engine_files, "engine_links": engine_links,
                }
                provider = {
                    "version": 2, "source_sha": old_sha, "provider_sha": old_sha,
                    "engine_tree_sha256": old_engine_sha,
                    "node_version": "26.3.1", "package_lock_sha256": old_lock_sha,
                    "engine_files": engine_files, "engine_links": engine_links,
                }
                provider_file = old_payload / "runtime-provider.json"
                provider_file.write_text(json.dumps(provider, sort_keys=True) + "\n", encoding="ascii")
                os.chmod(provider_file, 0o444)
                candidate_manifest = {
                    **old_manifest,
                    "package_lock_sha256": new_lock_sha,
                    "engine_tree_sha256": new_engine_sha,
                }
                with (
                    mock.patch.multiple(runtime, PAYLOAD_ROOT=payload_root),
                    mock.patch.object(runtime, "_read_manifest", return_value={"source_sha": old_sha}),
                    mock.patch.object(runtime, "_validate_payload"),
                    mock.patch.object(runtime, "_validate_runtime_source", return_value=old_manifest),
                    mock.patch.object(runtime, "_protected_shas", return_value={old_sha}),
                ):
                    planned, source_root = runtime._runtime_provider_plan(
                        root, candidate_source_sha=candidate_sha,
                        expected_manifest=candidate_manifest,
                    )
                    self.assertEqual(source_root, provider_root)
                    self.assertEqual(planned["provider_sha"], candidate_sha)
                    self.assertEqual(planned["package_lock_sha256"], new_lock_sha)
                    runtime._require_provider_copy_capacity(
                        source_root, new_suite, candidate_manifest,
                    )
                    with mock.patch.object(runtime.os, "statvfs", return_value=mock.Mock(
                        f_frsize=4096, f_blocks=10_000_000, f_bavail=1,
                    )):
                        with self.assertRaisesRegex(runtime.InstallerError, "capacity is insufficient"):
                            runtime._require_provider_copy_capacity(
                                source_root, new_suite, candidate_manifest,
                            )
                    old_tree_before = (
                        runtime._runtime_file_map(
                            provider_root,
                            prefixes=("node/", "browsers/", "web/node_modules/"),
                            links=engine_links,
                        ),
                        runtime._digest_regular(provider_root / "web/package-lock.json"),
                        os.readlink(provider_root / "browsers/webkit-2311/browser-alias"),
                    )
                    new_provider = root / "new-provider"
                    runtime._copy_typed_engine_provider(
                        source_root, new_suite, new_provider,
                        engine_files=engine_files, engine_links=engine_links,
                        package_lock_sha256=new_lock_sha,
                    )
                    runtime._validate_engine_root(
                        new_provider, expected_files=engine_files, expected_links=engine_links,
                        engine_sha256=new_engine_sha, node_version="26.3.1",
                        package_lock_sha256=new_lock_sha,
                    )
                    with mock.patch.object(runtime.os, "statvfs", return_value=mock.Mock(
                        f_frsize=4096, f_blocks=10_000_000, f_bavail=1,
                    )):
                        with self.assertRaisesRegex(runtime.InstallerError, "breach host floor"):
                            runtime._require_provider_write_floor(new_provider, 4096)
                    self.assertEqual(
                        os.readlink(new_provider / "browsers/webkit-2311/browser-alias"), "browser"
                    )
                    self.assertEqual(
                        (
                            runtime._runtime_file_map(
                                provider_root,
                                prefixes=("node/", "browsers/", "web/node_modules/"),
                                links=engine_links,
                            ),
                            runtime._digest_regular(provider_root / "web/package-lock.json"),
                            os.readlink(provider_root / "browsers/webkit-2311/browser-alias"),
                        ),
                        old_tree_before,
                    )
                    self.assertEqual(runtime._digest_regular(provider_root / "web/package-lock.json"), old_lock_sha)
                    reused, reused_source = runtime._runtime_provider_plan(
                        root, candidate_source_sha=candidate_sha,
                        expected_manifest=old_manifest,
                    )
                    self.assertEqual(reused["provider_sha"], old_sha)
                    self.assertIsNone(reused_source)
                    mismatched = dict(candidate_manifest)
                    mismatched["engine_files"] = {**engine_files, "node/bin/node": "0" * 64}
                    with self.assertRaisesRegex(runtime.InstallerError, "differs from candidate engine"):
                        runtime._runtime_provider_plan(
                            root, candidate_source_sha=candidate_sha,
                            expected_manifest=mismatched,
                        )
                    with mock.patch.object(runtime, "_protected_shas", return_value=set()):
                        with self.assertRaisesRegex(runtime.InstallerError, "protected release closure"):
                            runtime._runtime_provider_plan(
                                root, candidate_source_sha=candidate_sha,
                                expected_manifest=candidate_manifest,
                            )
                    invalid_provider = dict(provider)
                    invalid_provider["package_lock_sha256"] = new_lock_sha
                    os.chmod(provider_file, 0o644)
                    provider_file.write_text(json.dumps(invalid_provider, sort_keys=True) + "\n", encoding="ascii")
                    os.chmod(provider_file, 0o444)
                    with self.assertRaisesRegex(runtime.InstallerError, "identity is invalid"):
                        runtime._runtime_provider_plan(
                            root, candidate_source_sha=candidate_sha,
                            expected_manifest=candidate_manifest,
                        )
                    os.chmod(provider_file, 0o644)
                    provider_file.write_text(json.dumps(provider, sort_keys=True) + "\n", encoding="ascii")
                    os.chmod(provider_file, 0o444)
                    old_node = provider_root / "node/bin/node"
                    os.chmod(old_node, 0o755)
                    old_node.write_bytes(b"changed engine bytes\n")
                    os.chmod(old_node, 0o555)
                    with self.assertRaisesRegex(runtime.InstallerError, "engine digest is invalid"):
                        runtime._runtime_provider_plan(
                            root, candidate_source_sha=candidate_sha,
                            expected_manifest=candidate_manifest,
                        )

    def test_typed_browser_alias_provider_rebuilds_and_validates_exact_links(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            old_engine, old_files, lock_sha = self._engine_provider_fixture(root / "old")
            webkit_root = old_engine / "browsers/webkit-2311"
            for path in old_engine.rglob("*"):
                if path.is_dir():
                    os.chmod(path, 0o755)
            os.chmod(old_engine, 0o755)
            alias_bytes = b"shared WPE provider bytes\n"
            (webkit_root / "lib").mkdir(mode=0o755)
            (webkit_root / "lib/real.so.1.9.5").write_bytes(alias_bytes)
            (webkit_root / "lib/alias.so.1").write_bytes(alias_bytes)
            (webkit_root / "lib/alias.so").write_bytes(alias_bytes)
            for path in webkit_root.rglob("*"):
                if path.is_file():
                    os.chown(path, 0, 0)
                    os.chmod(path, 0o555 if path.stat().st_mode & 0o111 else 0o444)
                elif path.is_dir():
                    os.chown(path, 0, 0)
                    os.chmod(path, 0o555)
            for path in sorted(old_engine.rglob("*"), reverse=True):
                if path.is_dir():
                    os.chmod(path, 0o555)
            os.chmod(old_engine, 0o555)

            engine_links = {
                "browsers/webkit-2311/lib/alias.so": "alias.so.1",
                "browsers/webkit-2311/lib/alias.so.1": "real.so.1.9.5",
            }
            with mock.patch.multiple(
                runtime,
                CHROMIUM_SANDBOX_SIZE=(old_engine / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=hashlib.sha256(
                    (old_engine / runtime.RUNTIME_SANDBOX_RELATIVE).read_bytes()
                ).hexdigest(),
            ):
                flattened_files = runtime._runtime_file_map(
                    old_engine,
                    prefixes=("node/", "browsers/", "web/node_modules/"),
                )
                engine_files = {
                    relative: digest
                    for relative, digest in flattened_files.items()
                    if relative not in engine_links
                }
                engine_sha = runtime._engine_map_digest(
                    engine_files,
                    engine_links,
                    node_version="26.3.1",
                    package_lock_sha256=lock_sha,
                )
                suite_root = root / "suite"
                (suite_root / "web").mkdir(mode=0o700, parents=True)
                lock = suite_root / "web/package-lock.json"
                lock.write_bytes(b"lock\n")
                os.chown(lock, 0, 0)
                os.chmod(lock, 0o444)
                repaired = root / "provider-runtime"
                provider_files = runtime._copy_typed_engine_provider(
                    old_engine,
                    suite_root,
                    repaired,
                    engine_files=engine_files,
                    engine_links=engine_links,
                    package_lock_sha256=lock_sha,
                )
                runtime._validate_engine_root(
                    repaired,
                    expected_files=engine_files,
                    expected_links=engine_links,
                    engine_sha256=engine_sha,
                    node_version="26.3.1",
                    package_lock_sha256=lock_sha,
                )
                first_alias = repaired / "browsers/webkit-2311/lib/alias.so"
                chained_alias = repaired / "browsers/webkit-2311/lib/alias.so.1"
                self.assertTrue(first_alias.is_symlink())
                self.assertTrue(chained_alias.is_symlink())
                self.assertEqual(os.readlink(first_alias), "alias.so.1")
                self.assertEqual(os.readlink(chained_alias), "real.so.1.9.5")
                self.assertEqual(provider_files["provider-runtime/web/package-lock.json"], lock_sha)
                self.assertTrue((old_engine / "browsers/webkit-2311/lib/alias.so").is_file())
                self.assertFalse((old_engine / "browsers/webkit-2311/lib/alias.so").is_symlink())

                # Typed provider roots are engine-only projections; accepting
                # a v1 runtime manifest or suite source here would let an
                # undeclared runtime tree hitchhike beside the engine map.
                extra_manifest = repaired / "runtime-manifest.json"
                os.chmod(repaired, 0o755)
                extra_manifest.write_text("{}\n", encoding="ascii")
                os.chown(extra_manifest, 0, 0)
                os.chmod(extra_manifest, 0o444)
                os.chmod(repaired, 0o555)
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_engine_root(
                        repaired,
                        expected_files=engine_files,
                        expected_links=engine_links,
                        engine_sha256=engine_sha,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )
                os.chmod(repaired, 0o755)
                extra_manifest.unlink()
                os.chmod(repaired, 0o555)

                web_root = repaired / "web"
                tests_root = web_root / "tests/support"
                os.chmod(web_root, 0o755)
                tests_root.mkdir(mode=0o700, parents=True)
                extra_suite = tests_root / "live-engine-preflight.cjs"
                extra_suite.write_text("// suite source is not provider state\n", encoding="ascii")
                os.chown(extra_suite, 0, 0)
                os.chmod(extra_suite, 0o444)
                for directory in (web_root / "tests", tests_root):
                    os.chown(directory, 0, 0)
                    os.chmod(directory, 0o555)
                os.chmod(web_root, 0o555)
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_engine_root(
                        repaired,
                        expected_files=engine_files,
                        expected_links=engine_links,
                        engine_sha256=engine_sha,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )
                os.chmod(web_root, 0o755)
                shutil.rmtree(web_root / "tests")
                os.chmod(web_root, 0o555)

                flattened = root / "flattened-provider"
                shutil.copytree(repaired, flattened, symlinks=True)
                flattened_alias = flattened / "browsers/webkit-2311/lib/alias.so"
                flattened_alias.unlink()
                flattened_alias.write_bytes(alias_bytes)
                os.chown(flattened_alias, 0, 0)
                os.chmod(flattened_alias, 0o444)
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_engine_root(
                        flattened,
                        expected_files=engine_files,
                        expected_links=engine_links,
                        engine_sha256=engine_sha,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )

                first_alias.unlink()
                first_alias.symlink_to("../../../../outside.so")
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_engine_root(
                        repaired,
                        expected_files=engine_files,
                        expected_links=engine_links,
                        engine_sha256=engine_sha,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )

    def test_engine_alias_manifest_rejects_escape_cycles_and_missing_targets(self) -> None:
        files = {"browsers/webkit-2311/lib/real.so": "a" * 64}
        rejected = (
            {"browsers/webkit-2311/lib/link.so": "../../../../etc/passwd"},
            {
                "browsers/webkit-2311/lib/first.so": "second.so",
                "browsers/webkit-2311/lib/second.so": "first.so",
            },
            {"browsers/webkit-2311/lib/link.so": "missing.so"},
            {"browsers/not-pinned/lib/link.so": "real.so"},
            {"browsers/webkit-2311/lib/link.so": "/absolute/path"},
            {
                "browsers/webkit-2311/lib/parent.so": "real.so",
                "browsers/webkit-2311/lib/parent.so/child": "real.so",
            },
        )
        for links in rejected:
            with self.subTest(links=links), self.assertRaises(runtime.InstallerError):
                runtime._validate_engine_links(files, links)
        with self.assertRaises(runtime.InstallerError):
            runtime._validate_engine_links(
                {
                    **files,
                    "browsers/webkit-2311/lib/file.so": "b" * 64,
                    "browsers/webkit-2311/lib/file.so/child": "c" * 64,
                },
                {},
            )

    def test_typed_provider_payload_digest_binds_aliases_and_cleanup_is_closed(self) -> None:
        source_sha = "d" * 40
        engine_files = {
            "node/bin/node": hashlib.sha256(b"node\n").hexdigest(),
            "browsers/webkit-2311/lib/real.so": hashlib.sha256(b"engine\n").hexdigest(),
        }
        engine_links = {
            "browsers/webkit-2311/lib/alias.so": "real.so",
        }
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload = root / source_sha
            provider_root = payload / "provider-runtime"
            node = provider_root / "node/bin/node"
            terminal = provider_root / "browsers/webkit-2311/lib/real.so"
            node.parent.mkdir(mode=0o700, parents=True)
            terminal.parent.mkdir(mode=0o700, parents=True)
            (provider_root / "web").mkdir(mode=0o700, parents=True)
            node.write_bytes(b"node\n")
            terminal.write_bytes(b"engine\n")
            lock = provider_root / "web/package-lock.json"
            lock.write_bytes(b"lock\n")
            for path in (node, terminal, lock):
                os.chown(path, 0, 0)
                os.chmod(path, 0o444)
            alias = provider_root / "browsers/webkit-2311/lib/alias.so"
            alias.symlink_to("real.so")
            os.chown(alias, 0, 0, follow_symlinks=False)
            record = {
                "version": 2,
                "source_sha": source_sha,
                "provider_sha": source_sha,
                "engine_tree_sha256": "e" * 64,
                "node_version": "26.3.1",
                "package_lock_sha256": hashlib.sha256(b"lock\n").hexdigest(),
                "engine_files": engine_files,
                "engine_links": engine_links,
            }
            provider_manifest = payload / "runtime-provider.json"
            provider_manifest.write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            (payload / "source-sha").write_text(source_sha + "\n", encoding="ascii")
            for path in (provider_manifest, payload / "source-sha"):
                os.chown(path, 0, 0)
                os.chmod(path, 0o444)

            payload_links = runtime._payload_link_map(payload)
            self.assertEqual(
                payload_links,
                {f"provider-runtime/{path}": target for path, target in engine_links.items()},
            )
            tree_digest, payload_files = runtime._tree_digest(
                payload,
                allowed_links=payload_links,
            )
            self.assertEqual(len(tree_digest), 64)
            self.assertNotIn("provider-runtime/browsers/webkit-2311/lib/alias.so", payload_files)
            alias.unlink()
            alias.write_bytes(b"engine\n")
            os.chown(alias, 0, 0)
            os.chmod(alias, 0o444)
            with self.assertRaises(runtime.InstallerError):
                runtime._tree_digest(payload, allowed_links=payload_links)

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "payloads"
            payload_root.mkdir(mode=0o755)
            source_sha = "c" * 40
            stage = payload_root / f".{source_sha}.install-{'f' * 32}"
            provider_root = stage / "provider-runtime/browsers/webkit-2311/lib"
            provider_root.mkdir(mode=0o700, parents=True)
            (provider_root / "real.so").write_bytes(b"engine\n")
            (provider_root / "real.so").chmod(0o444)
            alias = provider_root / "alias.so"
            alias.symlink_to("real.so")
            os.chown(alias, 0, 0, follow_symlinks=False)
            stage_record = {
                "version": 2,
                "source_sha": source_sha,
                "provider_sha": source_sha,
                "engine_files": {
                    "browsers/webkit-2311/lib/real.so": hashlib.sha256(
                        b"engine\n"
                    ).hexdigest()
                },
                "engine_links": {
                    "browsers/webkit-2311/lib/alias.so": "real.so",
                },
            }
            (stage / "runtime-provider.json").write_text(
                json.dumps(stage_record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            (stage / "runtime-provider.json").chmod(0o444)
            keep = root / "keep.txt"
            keep.write_text("outside owned payload namespace\n", encoding="ascii")
            with mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root):
                self.assertEqual(runtime._cleanup_staging(apply=True), 1)
                self.assertFalse(stage.exists())
                self.assertEqual(keep.read_text(encoding="ascii"), "outside owned payload namespace\n")

                unsafe = payload_root / f".{source_sha}.install-{'a' * 32}"
                unsafe_alias = unsafe / "provider-runtime/browsers/webkit-2311/lib/alias.so"
                unsafe_alias.parent.mkdir(mode=0o700, parents=True)
                unsafe_alias.symlink_to("../../../../outside.so")
                (unsafe / "runtime-provider.json").write_text(
                    json.dumps(
                        {
                            **stage_record,
                            "engine_links": {
                                "browsers/webkit-2311/lib/alias.so": "../../../../outside.so",
                            },
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="ascii",
                )
                (unsafe / "runtime-provider.json").chmod(0o444)
                with self.assertRaises(runtime.InstallerError):
                    runtime._cleanup_staging(apply=True)
                self.assertTrue(unsafe.exists())
                self.assertTrue(keep.exists())

        self._assert_installer_published_v2_payload_crosses_all_trusted_readers()

    def _assert_installer_published_v2_payload_crosses_all_trusted_readers(self) -> None:
        """Publish real installer generations and run every independent reader."""
        legacy_sha = "6aa4b0c7e00b687a12d83af611df8c9f711615b3"
        repaired_sha = "e2e94c2a728c6648ab9c617118db806cc4c0d314"
        reused_sha = "3" * 40
        link_map = {
            "browsers/webkit-2311/lib/libwpe.so": "libwpe.so.1",
            "browsers/webkit-2311/lib/libwpe.so.1": "libwpe.so.1.9.3",
        }
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            with self.canonical_runtime_tree(root, legacy_sha) as (
                app_dir, legacy_release, trusted, payload_root
            ):
                legacy_runtime, _files, _lock = self._engine_provider_fixture(root / "legacy-engine")
                for directory in (legacy_runtime, *[p for p in legacy_runtime.rglob("*") if p.is_dir()]):
                    os.chmod(directory, 0o755)
                sandbox = legacy_runtime / runtime.RUNTIME_SANDBOX_RELATIVE
                sandbox.write_bytes(platform_chromium_sandbox_fixture.read_bytes())
                os.chmod(sandbox, 0o4755)
                library = legacy_runtime / "browsers/webkit-2311/lib"
                library.mkdir(mode=0o755, parents=True, exist_ok=True)
                terminal_bytes = b"installer published WPE terminal\n"
                (library / "libwpe.so.1.9.3").write_bytes(terminal_bytes)
                os.chmod(library / "libwpe.so.1.9.3", 0o444)
                for alias in link_map:
                    (legacy_runtime / alias).write_bytes(terminal_bytes)
                    os.chmod(legacy_runtime / alias, 0o444)
                (legacy_runtime / "web/tests/support/live-engine-preflight.cjs").write_bytes(b"preflight fixture\n")
                os.chmod(legacy_runtime / "web/tests/support/live-engine-preflight.cjs", 0o444)
                legacy_files = runtime._runtime_file_map(
                    legacy_runtime,
                    prefixes=("node/", "browsers/", "web/node_modules/"),
                )
                lock_sha = runtime._digest_regular(legacy_runtime / "web/package-lock.json")
                tree_v1, files_v1 = runtime._tree_digest(
                    legacy_runtime,
                    ignored=frozenset({runtime.RUNTIME_MANIFEST_RELATIVE.as_posix()}),
                    sandbox_relative=runtime.RUNTIME_SANDBOX_RELATIVE,
                )
                runtime._write_manifest(
                    legacy_runtime / runtime.RUNTIME_MANIFEST_RELATIVE,
                    {
                        "version": 1,
                        "node_version": "26.3.1",
                        "package_lock_sha256": lock_sha,
                        "tree_sha256": tree_v1,
                        "files": files_v1,
                    },
                )
                runtime._normalize_tree(
                    legacy_runtime,
                    sandbox_relative=runtime.RUNTIME_SANDBOX_RELATIVE,
                )
                shutil.rmtree(legacy_release / "liveqa-runtime")
                shutil.copytree(legacy_runtime, legacy_release / "liveqa-runtime", symlinks=True)
                repaired_files = {path: digest for path, digest in legacy_files.items() if path not in link_map}

                # First publish the existing flattened provider, then let the
                # normal source-only installer perform its v1-to-v2 repair.
                with mock.patch.object(runtime, "_validate_runtime_source", new=ORIGINAL_VALIDATE_RUNTIME_SOURCE):
                    runtime.install(app_dir, legacy_release)
                    runtime.verify(app_dir, legacy_sha)

                def make_candidate(
                    candidate_sha: str,
                    *,
                    lock_bytes: bytes = b"lock\n",
                    browser_gate_import: bool = True,
                    include_browser_gate_helper: bool = True,
                    screenshot_diagnostic_import: bool = False,
                    include_screenshot_diagnostic_helper: bool = False,
                ) -> Path:
                    release = app_dir / "releases" / f"release-{candidate_sha[:8]}"
                    release.mkdir(mode=0o755)
                    tools = release / "tools"
                    tools.mkdir(mode=0o755)
                    for name in runtime.TOOL_FILES:
                        source = SCRIPT.parent / name
                        target = tools / name
                        target.write_bytes(source.read_bytes())
                        os.chmod(target, 0o555 if name.endswith(".sh") else 0o444)
                    for relative in runtime.SOURCE_FILES:
                        source = SCRIPT.parents[1] / relative
                        target = release / relative
                        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                        target.write_bytes(source.read_bytes())
                        os.chmod(target, 0o444)
                    release_json = {
                        "source_git_commit": candidate_sha,
                        "release_slug": release.name,
                    }
                    (release / "RELEASE.json").write_text(json.dumps(release_json) + "\n", encoding="ascii")
                    os.chmod(release / "RELEASE.json", 0o444)
                    source_runtime = release / "liveqa-runtime"
                    suite_content = {
                        "web/package-lock.json": lock_bytes,
                        "web/playwright.live.config.ts": b"config\n",
                        "web/tests/smoke/live-launch.spec.ts": (
                            b'import { takeObjectiveScreenshotWithDiagnostics } from '
                            b'"../support/objective-screenshot-diagnostic";\n'
                            b"void takeObjectiveScreenshotWithDiagnostics;\n"
                            if screenshot_diagnostic_import
                            else b"launch\n"
                        ),
                        "web/tests/smoke/live-user-journey.spec.ts": (
                            b'import { createLiveUserBrowserGateCapture } from '
                            b'"../support/live-user-browser-gate";\n'
                            b"void createLiveUserBrowserGateCapture;\n"
                            if browser_gate_import
                            else b"journey\n"
                        ),
                        "web/tests/support/live-qa-origin.ts": b"origin\n",
                        "web/tests/support/live-qa-sandbox.ts": b"sandbox\n",
                        "web/tests/support/live-count-reporter.cjs": b"reporter\n",
                        "web/tests/support/live-engine-preflight.cjs": b"preflight\n",
                    }
                    if include_browser_gate_helper:
                        suite_content[
                            "web/tests/support/live-user-browser-gate.ts"
                        ] = (
                            SCRIPT.parents[1]
                            / "apps/platform_web/tests/support/live-user-browser-gate.ts"
                        ).read_bytes()
                    if include_screenshot_diagnostic_helper:
                        suite_content[
                            "web/tests/support/objective-screenshot-diagnostic.ts"
                        ] = (
                            SCRIPT.parents[1]
                            / "apps/platform_web/tests/support/objective-screenshot-diagnostic.ts"
                        ).read_bytes()
                    for relative, content in suite_content.items():
                        target = source_runtime / relative
                        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                        target.write_bytes(content)
                        os.chmod(target, 0o444)
                    suite_files = runtime._runtime_file_map(
                        source_runtime,
                        prefixes=("web/",),
                        excluded_prefixes=("web/node_modules/",),
                    )
                    lock_digest = hashlib.sha256(suite_content["web/package-lock.json"]).hexdigest()
                    source_manifest = {
                        "version": 3,
                        "node_version": "26.3.1",
                        "package_lock_sha256": lock_digest,
                        "engine_tree_sha256": runtime._engine_map_digest(
                            repaired_files, link_map,
                            node_version="26.3.1", package_lock_sha256=lock_digest,
                        ),
                        "engine_files": repaired_files,
                        "engine_links": link_map,
                        "suite_tree_sha256": runtime._file_map_digest(
                            suite_files, domain=b"oldsparky-liveqa-suite-v1\0"
                        ),
                        "suite_files": suite_files,
                    }
                    runtime._write_manifest(source_runtime / runtime.RUNTIME_MANIFEST_RELATIVE, source_manifest)
                    runtime._normalize_tree(source_runtime)
                    return release

                incomplete_candidate = make_candidate(
                    "7" * 40,
                    browser_gate_import=True,
                    include_browser_gate_helper=False,
                )
                with mock.patch.object(
                    runtime,
                    "_validate_runtime_source",
                    new=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                ), self.assertRaises(runtime.InstallerError):
                    runtime._candidate_source_plan(
                        app_dir, incomplete_candidate, "7" * 40
                    )

                incomplete_screenshot_candidate = make_candidate(
                    "9" * 40,
                    screenshot_diagnostic_import=True,
                    include_screenshot_diagnostic_helper=False,
                )
                with mock.patch.object(
                    runtime,
                    "_validate_runtime_source",
                    new=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                ), self.assertRaises(runtime.InstallerError):
                    runtime._candidate_source_plan(
                        app_dir, incomplete_screenshot_candidate, "9" * 40
                    )

                unreferenced_helper_candidate = make_candidate(
                    "8" * 40,
                    browser_gate_import=False,
                    include_browser_gate_helper=True,
                )
                with (
                    mock.patch.object(
                        runtime,
                        "_validate_runtime_source",
                        new=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                    ),
                    self.assertRaisesRegex(
                        runtime.InstallerError,
                        "live-QA runtime contains an unreviewed web member",
                    ),
                ):
                    runtime._candidate_source_plan(
                        app_dir, unreferenced_helper_candidate, "8" * 40
                    )

                unreferenced_screenshot_candidate = make_candidate(
                    "a" * 40,
                    screenshot_diagnostic_import=False,
                    include_screenshot_diagnostic_helper=True,
                )
                with (
                    mock.patch.object(
                        runtime,
                        "_validate_runtime_source",
                        new=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                    ),
                    self.assertRaisesRegex(
                        runtime.InstallerError,
                        "live-QA runtime contains an unreviewed web member",
                    ),
                ):
                    runtime._candidate_source_plan(
                        app_dir, unreferenced_screenshot_candidate, "a" * 40
                    )

                importless_v3_candidate = make_candidate(
                    "6" * 40,
                    browser_gate_import=False,
                    include_browser_gate_helper=False,
                )
                with mock.patch.object(
                    runtime,
                    "_validate_runtime_source",
                    new=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                ):
                    importless_manifest, _provider, _provider_source = (
                        runtime._candidate_source_plan(
                            app_dir, importless_v3_candidate, "6" * 40
                        )
                    )
                self.assertEqual(importless_manifest["version"], 3)
                self.assertNotIn(
                    "web/tests/support/live-user-browser-gate.ts",
                    importless_manifest["suite_files"],
                )

                # Full legacy providers keep the old importless suite. An extra
                # helper is rejected until the suite imports it; once referenced,
                # the same immutable provider tree is accepted.
                full_provider, full_files, full_lock_sha = self._engine_provider_fixture(
                    root / "full-provider"
                )
                full_sandbox = full_provider / runtime.RUNTIME_SANDBOX_RELATIVE
                full_sandbox_sha = runtime._digest_regular(
                    full_sandbox, allow_sandbox=True
                )
                helper_relative = "web/tests/support/live-user-browser-gate.ts"
                full_journey = full_provider / "web/tests/smoke/live-user-journey.spec.ts"
                full_helper = full_provider / helper_relative

                def validate_full_provider() -> None:
                    runtime._validate_engine_root(
                        full_provider,
                        expected_files=full_files,
                        engine_sha256=runtime._engine_digest(
                            full_files,
                            node_version="26.3.1",
                            package_lock_sha256=full_lock_sha,
                        ),
                        node_version="26.3.1",
                        package_lock_sha256=full_lock_sha,
                    )

                with mock.patch.multiple(
                    runtime,
                    CHROMIUM_SANDBOX_SIZE=full_sandbox.stat().st_size,
                    CHROMIUM_SANDBOX_SHA256=full_sandbox_sha,
                ):
                    validate_full_provider()
                    os.chmod(full_provider, 0o755)
                    os.chmod(full_journey, 0o644)
                    os.chmod(full_helper.parent, 0o755)
                    full_helper.write_bytes(
                        (
                            SCRIPT.parents[1]
                            / "apps/platform_web/tests/support/live-user-browser-gate.ts"
                        ).read_bytes()
                    )
                    full_helper.chmod(0o444)
                    os.chmod(full_helper.parent, 0o555)
                    os.chmod(full_journey, 0o444)
                    os.chmod(full_provider, 0o555)
                    with self.assertRaisesRegex(
                        runtime.InstallerError,
                        "runtime provider dependency tree contains an unreviewed file",
                    ):
                        validate_full_provider()

                    os.chmod(full_provider, 0o755)
                    os.chmod(full_journey, 0o644)
                    full_journey.write_bytes(
                        b'import { createLiveUserBrowserGateCapture } from '
                        b'"../support/live-user-browser-gate";\n'
                        b"void createLiveUserBrowserGateCapture;\n"
                    )
                    os.chmod(full_journey, 0o444)
                    os.chmod(full_provider, 0o555)
                    validate_full_provider()

                repaired_release = make_candidate(repaired_sha)
                # X's deployed installer predates typed-provider rebinding.
                # Keep that immutable target binary in the fixture so the
                # rollback proof must use W's receipt-authorized installer.
                historical_installer = subprocess.run(
                    [
                        "git", "show",
                        "HEAD:platform/tools/platform_live_qa_runtime_install.py",
                    ],
                    cwd=SCRIPT.parents[2],
                    capture_output=True,
                    check=True,
                ).stdout
                old_installer_path = repaired_release / "tools/platform_live_qa_runtime_install.py"
                old_installer_path.write_bytes(historical_installer)
                os.chmod(old_installer_path, 0o444)
                with mock.patch.object(runtime, "_validate_runtime_source", new=ORIGINAL_VALIDATE_RUNTIME_SOURCE):
                    # Provider planning consumes the already-published legacy
                    # importless generation and installer publication creates
                    # the new candidate generation with its imported helper.
                    legacy_active_runtime = payload_root / legacy_sha / "runtime"
                    legacy_journey = (
                        legacy_active_runtime
                        / "web/tests/smoke/live-user-journey.spec.ts"
                    )
                    self.assertFalse(
                        (
                            legacy_active_runtime
                            / "web/tests/support/live-user-browser-gate.ts"
                        ).exists()
                    )
                    self.assertNotIn(
                        runtime.LIVE_USER_BROWSER_GATE_IMPORT,
                        legacy_journey.read_bytes(),
                    )
                    runtime.install(app_dir, repaired_release)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(repaired_release)
                    runtime.verify(app_dir, repaired_sha)

                def validate_all(source_sha: str) -> Path:
                    payload = payload_root / source_sha
                    manifest = runtime._read_manifest()
                    self.assertEqual(manifest["source_sha"], source_sha)
                    with mock.patch.object(runtime, "_validate_runtime_source", new=ORIGINAL_VALIDATE_RUNTIME_SOURCE):
                        runtime.verify(app_dir, source_sha)
                    with (
                        mock.patch.multiple(
                            dispatch,
                            RUNTIME=app_dir,
                            CURRENT=app_dir / "current",
                            TRUSTED_LIVE_QA_ROOT=trusted,
                            QA_HELPER=trusted / "platform_live_user_qa_trusted.sh",
                            LAUNCH_HELPER=trusted / "platform_live_launch_trusted.sh",
                            REMOTE_DISPATCHER=trusted / "platform_workflow_remote_dispatch.py",
                            REMOTE_INPUT_GUARD=trusted / "platform_workflow_input_guard.py",
                            RELEASE_LOCK_EXEC=trusted / "platform_release_lock_exec.sh",
                            RELEASE_LOCK=trusted / "platform_release_lock.sh",
                            MAILBOX_HELPER=trusted / "platform_live_qa_mailbox_helper.py",
                            ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            PAYLOAD_ROOT=payload_root,
                            ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.multiple(
                            safe_env,
                            PRODUCTION_RUNTIME_ROOT=app_dir,
                            ACTIVE_PLATFORM_ROOT=app_dir / "current",
                            LIVE_QA_ROOT=trusted,
                            LIVE_QA_RELEASE_ROOT=payload_root,
                            LIVE_QA_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            LIVE_QA_ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.multiple(
                            guard,
                            TRUSTED_SECRET_ROOT=trusted,
                            TRUSTED_PAYLOAD_ROOT=payload_root,
                            TRUSTED_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            TRUSTED_ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.object(guard, "_active_release_commit", return_value=source_sha),
                    ):
                        self.assertEqual(dispatch.main(["verify", source_sha]), 0)
                        self.assertEqual(safe_env.main(["validate-runtime"]), 0)
                        self.assertEqual(guard._validate_installed_payload_root(payload, target_sha=source_sha), source_sha)
                        engine_root = guard._trusted_runtime_root()
                        gate_root = root / f"gates-{source_sha[:4]}-{uuid4().hex[:6]}"
                        gate_root.mkdir(mode=0o755)
                        gate = gate_root / f"public-live-qa.{source_sha[:8]}"
                        gate.mkdir(mode=0o700)
                        with (
                            mock.patch.object(guard, "RUN_GATE_ROOT", gate_root),
                            mock.patch.object(guard, "RUNNER_CACHE_ROOT", root / "cache"),
                            mock.patch.object(guard, "liveqa_identity", return_value=(0, 0)),
                        ):
                            mount_values = guard._runtime_mount_aliases(gate)
                            guard._cleanup_runtime_mount_aliases(*mount_values)
                        return engine_root

                os.chmod(app_dir / "releases", 0o755)
                os.chmod(app_dir, 0o755)
                engine_root = validate_all(repaired_sha)
                self.assertEqual(engine_root, payload_root / repaired_sha / "provider-runtime")
                self.assertEqual(
                    os.readlink(engine_root / "browsers/webkit-2311/lib/libwpe.so"),
                    "libwpe.so.1",
                )
                self.assertFalse((payload_root / repaired_sha / "runtime-provider.json").is_symlink())

                def assert_published_payload_rejected() -> None:
                    with (
                        mock.patch.multiple(
                            dispatch,
                            RUNTIME=app_dir,
                            CURRENT=app_dir / "current",
                            TRUSTED_LIVE_QA_ROOT=trusted,
                            ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            PAYLOAD_ROOT=payload_root,
                            ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.multiple(
                            safe_env,
                            PRODUCTION_RUNTIME_ROOT=app_dir,
                            ACTIVE_PLATFORM_ROOT=app_dir / "current",
                            LIVE_QA_ROOT=trusted,
                            LIVE_QA_RELEASE_ROOT=payload_root,
                            LIVE_QA_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            LIVE_QA_ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.multiple(
                            guard,
                            TRUSTED_SECRET_ROOT=trusted,
                            TRUSTED_PAYLOAD_ROOT=payload_root,
                            TRUSTED_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                            TRUSTED_ACTIVE_POINTER=trusted / "active",
                        ),
                        mock.patch.object(guard, "_active_release_commit", return_value=repaired_sha),
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertNotEqual(dispatch.main(["verify", repaired_sha]), 0)
                        self.assertNotEqual(safe_env.main(["validate-runtime"]), 0)
                        with self.assertRaises(runtime.InstallerError):
                            runtime.verify(app_dir, repaired_sha)
                        with self.assertRaises(guard.GuardError):
                            guard._validate_installed_payload_root(
                                payload_root / repaired_sha, target_sha=repaired_sha
                            )
                        with self.assertRaises(guard.GuardError):
                            guard._trusted_runtime_root()

                # The actual installed generation rejects topology, content,
                # and sandbox metadata mutations at every trusted reader.
                alias_path = engine_root / "browsers/webkit-2311/lib/libwpe.so"
                alias_path.unlink()
                alias_path.symlink_to("../../../../outside")
                os.chown(alias_path, 0, 0, follow_symlinks=False)
                with self.subTest(installed_mutation="escaping-alias"):
                    assert_published_payload_rejected()
                alias_path.unlink()
                alias_path.symlink_to("libwpe.so.1")
                os.chown(alias_path, 0, 0, follow_symlinks=False)

                terminal_path = engine_root / "browsers/webkit-2311/lib/libwpe.so.1.9.3"
                terminal_bytes = terminal_path.read_bytes()
                terminal_path.chmod(0o644)
                terminal_path.write_bytes(b"mutated engine target\n")
                terminal_path.chmod(0o444)
                with self.subTest(installed_mutation="changed-link-terminal"):
                    assert_published_payload_rejected()
                terminal_path.chmod(0o644)
                terminal_path.write_bytes(terminal_bytes)
                terminal_path.chmod(0o444)

                sandbox_path = engine_root / runtime.RUNTIME_SANDBOX_RELATIVE
                sandbox_path.chmod(0o755)
                with self.subTest(installed_mutation="sandbox-mode"):
                    assert_published_payload_rejected()
                sandbox_path.chmod(0o4755)
                sandbox_bytes = sandbox_path.read_bytes()
                sandbox_path.chmod(0o755)
                sandbox_path.write_bytes(b"mutated sandbox\n")
                sandbox_path.chmod(0o4755)
                with self.subTest(installed_mutation="sandbox-bytes"):
                    assert_published_payload_rejected()
                sandbox_path.chmod(0o755)
                sandbox_path.write_bytes(sandbox_bytes)
                sandbox_path.chmod(0o4755)

                misplaced_sandbox = (
                    engine_root / "browsers/webkit-2311/unexpected/chrome_sandbox"
                )
                misplaced_sandbox.parent.mkdir(mode=0o555, parents=True)
                shutil.copyfile(sandbox_path, misplaced_sandbox)
                os.chown(misplaced_sandbox, 0, 0)
                os.chmod(misplaced_sandbox, 0o4755)
                with self.subTest(installed_mutation="sandbox-path"):
                    assert_published_payload_rejected()
                misplaced_sandbox.unlink()
                misplaced_sandbox.parent.rmdir()

                # Reproduce X's existing typed payload: its own package lock
                # and provider receipt point to the protected historical 990
                # provider. W must validate that full old identity and
                # reactivate this payload without copying or rewriting 990.
                historical_provider_sha = "990de9936fcd5a019a026708d805db1fd8b8c280"
                repaired_payload = payload_root / repaired_sha
                historical_payload = payload_root / historical_provider_sha
                historical_payload.mkdir(mode=0o700)
                shutil.copytree(
                    repaired_payload / "provider-runtime",
                    historical_payload / "provider-runtime",
                    symlinks=True,
                )
                historical_source_marker = historical_payload / "source-sha"
                historical_source_marker.write_text(
                    historical_provider_sha + "\n", encoding="ascii",
                )
                os.chown(historical_source_marker, 0, 0)
                os.chmod(historical_source_marker, 0o444)
                os.chown(historical_payload, 0, 0)
                for path in [historical_payload, *historical_payload.rglob("*")]:
                    if path.is_dir() and not path.is_symlink():
                        os.chown(path, 0, 0)
                        os.chmod(path, 0o555)
                    elif not path.is_symlink():
                        os.chown(path, 0, 0)
                        mode = (
                            0o4755
                            if path == historical_payload / "provider-runtime" / runtime.RUNTIME_SANDBOX_RELATIVE
                            else 0o555 if path.stat().st_mode & 0o111 else 0o444
                        )
                        os.chmod(path, mode)
                active_before_rebinding = runtime._read_manifest()
                repaired_provider_path = repaired_payload / "runtime-provider.json"
                repaired_provider = json.loads(repaired_provider_path.read_text(encoding="ascii"))
                self.assertEqual(repaired_provider["provider_sha"], repaired_sha)
                repaired_provider["provider_sha"] = historical_provider_sha
                repaired_provider_path.chmod(0o644)
                repaired_provider_path.write_text(
                    json.dumps(repaired_provider, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                repaired_provider_path.chmod(0o444)
                for path in [repaired_payload, *repaired_payload.rglob("*")]:
                    if path.is_dir() and not path.is_symlink():
                        os.chmod(path, 0o700)
                shutil.rmtree(repaired_payload / "provider-runtime")
                for path in sorted(
                    [repaired_payload, *repaired_payload.rglob("*")],
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    if path.is_dir() and not path.is_symlink():
                        os.chown(path, 0, 0)
                        os.chmod(path, 0o555)
                repaired_tree, repaired_payload_files = runtime._tree_digest(repaired_payload)
                runtime._write_manifest(
                    trusted / "active-manifest.json",
                    {
                        **active_before_rebinding,
                        "payload_tree_sha256": repaired_tree,
                        "files": repaired_payload_files,
                    },
                )
                external_x_root = validate_all(repaired_sha)
                self.assertEqual(
                    external_x_root,
                    historical_payload / "provider-runtime",
                )
                sandbox_path = external_x_root / runtime.RUNTIME_SANDBOX_RELATIVE
                sandbox_sha = runtime._digest_regular(sandbox_path, allow_sandbox=True)

                reused_release = make_candidate(reused_sha)
                with mock.patch.object(runtime, "_validate_runtime_source", new=ORIGINAL_VALIDATE_RUNTIME_SOURCE):
                    runtime.install(app_dir, reused_release)
                    (app_dir / "previous").symlink_to(repaired_release)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(reused_release)
                    runtime.verify(app_dir, reused_sha)
                reused_payload = payload_root / reused_sha
                self.assertFalse(os.path.lexists(reused_payload / "provider-runtime"))
                reused_root = validate_all(reused_sha)
                self.assertEqual(reused_root, historical_payload / "provider-runtime")

                # A later web lock changes while the engine map, Node and
                # browser aliases stay byte-identical. W gets a candidate-owned
                # provider while X's old payload continues to reference 990.
                changed_sha = "4" * 40
                changed_release = make_candidate(changed_sha, lock_bytes=b"updated app lock\n")
                transaction_source = SCRIPT.with_name("platform_release_transaction.py")
                systemd_state_source = SCRIPT.with_name("platform_release_systemd_state.py")
                transaction_tool = changed_release / "tools/platform_release_transaction.py"
                transaction_tool.write_bytes(transaction_source.read_bytes())
                os.chmod(transaction_tool, 0o755)
                (changed_release / "tools/platform_release_systemd_state.py").write_bytes(
                    systemd_state_source.read_bytes()
                )
                os.chmod(changed_release / "tools/platform_release_systemd_state.py", 0o555)
                rollback_metadata = changed_release / ".rollback"
                rollback_metadata.mkdir(mode=0o700)
                (rollback_metadata / "previous-release").write_text(
                    f"{repaired_release}\n", encoding="ascii",
                )
                (rollback_metadata / "previous-release").chmod(0o600)
                (rollback_metadata / "venv-transition").write_text(
                    "unchanged\n", encoding="ascii",
                )
                (rollback_metadata / "venv-transition").chmod(0o600)
                freeze = changed_release / "requirements-platform.freeze.txt"
                freeze.write_text("fixture-package==1\n", encoding="ascii")
                freeze.chmod(0o444)
                (rollback_metadata / "shared-freeze.sha256").write_text(
                    hashlib.sha256(freeze.read_bytes()).hexdigest() + "\n",
                    encoding="ascii",
                )
                (rollback_metadata / "shared-freeze.sha256").chmod(0o600)
                repaired_payload = payload_root / repaired_sha
                repaired_links = runtime._payload_link_map(repaired_payload)
                repaired_before = runtime._tree_digest(
                    repaired_payload, allowed_links=repaired_links,
                )
                with mock.patch.object(runtime, "_validate_runtime_source", new=ORIGINAL_VALIDATE_RUNTIME_SOURCE):
                    runtime.install(app_dir, changed_release)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(changed_release)
                    runtime.verify(app_dir, changed_sha)
                    changed_payload = payload_root / changed_sha
                    self.assertTrue((changed_payload / "provider-runtime").is_dir())
                    changed_provider_before = runtime._tree_digest(
                        changed_payload,
                        allowed_links=runtime._payload_link_map(changed_payload),
                    )

                    shared = app_dir / "shared"
                    shared.mkdir(mode=0o700)
                    transaction_state = shared / ".release-operation.json"
                    snapshot = changed_release / ".rollback/shared-venv-before-install"
                    create = subprocess.run(
                        [
                            sys.executable, "-I", str(transaction_tool), "create",
                            "--state", str(transaction_state),
                            "--operation", "rollback",
                            "--app-dir", str(app_dir),
                            "--current-before", str(changed_release),
                            "--previous-before", str(repaired_release),
                            "--candidate-release", str(changed_release),
                            "--shared-venv", str(shared / "venv"),
                            "--peer", str(snapshot),
                            "--snapshot", str(snapshot),
                            "--transition", "none",
                        ],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(create.returncode, 0, create.stderr)

                    def run_transaction_with(
                        tool: Path,
                        *arguments: str,
                    ) -> subprocess.CompletedProcess[str]:
                        return subprocess.run(
                            [sys.executable, "-I", str(tool), *arguments],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            check=False,
                            text=True,
                        )

                    def run_transaction(*arguments: str) -> subprocess.CompletedProcess[str]:
                        return run_transaction_with(transaction_tool, *arguments)

                    for expected, phase in (
                        ("prepared", "venv-transitioned"),
                    ):
                        result = run_transaction(
                            "phase", "--state", str(transaction_state),
                            "--expected", expected, "--phase", phase,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "switch-pointer", "--state", str(transaction_state),
                        "--name", "current", "--target", str(repaired_release),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "phase", "--state", str(transaction_state),
                        "--expected", "venv-transitioned", "--phase", "current-switched",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "switch-pointer", "--state", str(transaction_state),
                        "--name", "previous", "--target", str(changed_release),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for expected, phase in (
                        ("current-switched", "pointers-switched"),
                        ("pointers-switched", "rollback-runtime-pending"),
                    ):
                        result = run_transaction(
                            "phase", "--state", str(transaction_state),
                            "--expected", expected, "--phase", phase,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)

                    current_installer = changed_release / "tools/platform_live_qa_runtime_install.py"
                    with (
                        mock.patch.object(runtime, "__file__", str(current_installer)),
                        mock.patch.object(
                            runtime, "_validate_runtime_source",
                            wraps=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                        ),
                        mock.patch.multiple(
                            runtime,
                            CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                            CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                        ),
                    ):
                        self.assertTrue(
                            runtime.rollback_check(app_dir, transaction_state, repaired_release)
                        )
                        self.assertEqual(
                            runtime.main([
                                "rollback-check", "--app-dir", str(app_dir),
                                "--target-release", str(repaired_release),
                                "--rollback-transaction", str(transaction_state),
                            ]),
                            0,
                        )
                        restored = runtime.reconcile(
                            app_dir, rollback_transaction=transaction_state,
                        )
                        self.assertEqual(restored["source_sha"], repaired_sha)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(repaired_release)
                    runtime.verify(app_dir, repaired_sha)
                    self.assertEqual(
                        json.loads((repaired_payload / "runtime-provider.json").read_text())[
                            "provider_sha"
                        ],
                        historical_provider_sha,
                    )
                    self.assertEqual(
                        runtime._tree_digest(
                            changed_payload,
                            allowed_links=runtime._payload_link_map(changed_payload),
                        ),
                        changed_provider_before,
                    )
                    self.assertEqual(
                        runtime._tree_digest(repaired_payload, allowed_links=repaired_links),
                        repaired_before,
                    )
                    self.assertFalse(os.path.lexists(repaired_payload / ".install-rebase"))
                    transaction_state.unlink()
                    (app_dir / "previous").unlink()
                    runtime.install(app_dir, reused_release)
                    (app_dir / "previous").symlink_to(repaired_release)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(reused_release)
                    runtime.verify(app_dir, reused_sha)

                    # The same typed-provider path must restore X during an
                    # install-failure recovery receipt after W's active alias
                    # was published but before current moved from X.
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(repaired_release)
                    (app_dir / "previous").unlink()
                    initial_release = app_dir / "releases" / f"release-{legacy_sha[:8]}"
                    (app_dir / "previous").symlink_to(initial_release)
                    failed_sha = "5" * 40
                    failed_release = make_candidate(
                        failed_sha, lock_bytes=b"failed app lock\n",
                    )
                    recovery_tool = failed_release / "tools/platform_release_transaction.py"
                    recovery_tool.write_bytes(transaction_source.read_bytes())
                    os.chmod(recovery_tool, 0o755)
                    (failed_release / "tools/platform_release_systemd_state.py").write_bytes(
                        systemd_state_source.read_bytes()
                    )
                    os.chmod(failed_release / "tools/platform_release_systemd_state.py", 0o555)
                    failed_metadata = failed_release / ".rollback"
                    failed_metadata.mkdir(mode=0o700)
                    (failed_metadata / "previous-release").write_text(
                        f"{repaired_release}\n", encoding="ascii",
                    )
                    (failed_metadata / "previous-release").chmod(0o600)
                    (failed_metadata / "venv-transition").write_text(
                        "unchanged\n", encoding="ascii",
                    )
                    (failed_metadata / "venv-transition").chmod(0o600)
                    failed_freeze = failed_release / "requirements-platform.freeze.txt"
                    failed_freeze.write_text("fixture-package==1\n", encoding="ascii")
                    failed_freeze.chmod(0o444)
                    (failed_metadata / "shared-freeze.sha256").write_text(
                        hashlib.sha256(failed_freeze.read_bytes()).hexdigest() + "\n",
                        encoding="ascii",
                    )
                    (failed_metadata / "shared-freeze.sha256").chmod(0o600)
                    runtime.install(app_dir, repaired_release)
                    runtime.install(app_dir, failed_release)
                    self.assertEqual(runtime._read_manifest()["source_sha"], failed_sha)
                    recovery_state = shared / ".release-operation.json"
                    peer = shared / f".venv-install-{failed_release.name}.fixture"
                    create = subprocess.run(
                        [
                            sys.executable, "-I", str(recovery_tool), "create",
                            "--state", str(recovery_state),
                            "--operation", "install",
                            "--app-dir", str(app_dir),
                            "--current-before", str(repaired_release),
                            "--previous-before", str(initial_release),
                            "--candidate-release", str(failed_release),
                            "--shared-venv", str(shared / "venv"),
                            "--peer", str(peer),
                            "--snapshot", str(failed_release / ".rollback/shared-venv-before-install"),
                            "--transition", "none",
                        ],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(create.returncode, 0, create.stderr)
                    for expected, phase in (
                        ("prepared", "venv-transitioned"),
                        ("venv-transitioned", "snapshot-placed"),
                        ("snapshot-placed", "staged"),
                        ("staged", "migration-pending"),
                        ("migration-pending", "migration-failed"),
                        ("migration-failed", "recovery-authorized"),
                    ):
                        result = run_transaction_with(recovery_tool,
                            "phase", "--state", str(recovery_state),
                            "--expected", expected, "--phase", phase,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction_with(
                        recovery_tool, "recover", "--retain", "--state", str(recovery_state),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recovery_installer = failed_release / "tools/platform_live_qa_runtime_install.py"
                    with (
                        mock.patch.object(runtime, "__file__", str(recovery_installer)),
                        mock.patch.object(
                            runtime, "_validate_runtime_source",
                            wraps=ORIGINAL_VALIDATE_RUNTIME_SOURCE,
                        ),
                        mock.patch.multiple(
                            runtime,
                            CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                            CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                        ),
                    ):
                        self.assertTrue(
                            runtime.recovery_check(app_dir, recovery_state, repaired_release)
                        )
                        recovered = runtime.reconcile(
                            app_dir, rollback_transaction=recovery_state,
                        )
                        self.assertEqual(recovered["source_sha"], repaired_sha)
                    self.assertEqual(runtime._read_manifest()["source_sha"], repaired_sha)
                    recovery_state.unlink()
                    runtime.install(app_dir, reused_release)
                    (app_dir / "previous").unlink()
                    (app_dir / "previous").symlink_to(repaired_release)
                    (app_dir / "current").unlink()
                    (app_dir / "current").symlink_to(reused_release)
                    runtime.verify(app_dir, reused_sha)

                # External-provider metadata is still parsed strictly even
                # though its engine bytes live in another immutable payload.
                external_provider_path = reused_payload / "runtime-provider.json"
                external_provider_bytes = external_provider_path.read_bytes()
                active_manifest_path = trusted / "active-manifest.json"
                active_manifest_bytes = active_manifest_path.read_bytes()
                malformed_external = json.loads(external_provider_bytes)
                malformed_external["engine_links"] = {
                    "browsers/webkit-2311/lib/escape.so": "../../../../outside"
                }
                external_provider_path.chmod(0o644)
                external_provider_path.write_text(
                    json.dumps(malformed_external, sort_keys=True) + "\n",
                    encoding="ascii",
                )
                external_provider_path.chmod(0o444)
                changed_tree_digest, changed_files = runtime._tree_digest(reused_payload)
                changed_active_manifest = json.loads(active_manifest_bytes)
                changed_active_manifest["payload_tree_sha256"] = changed_tree_digest
                changed_active_manifest["files"] = changed_files
                active_manifest_path.chmod(0o644)
                active_manifest_path.write_text(
                    json.dumps(changed_active_manifest, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                active_manifest_path.chmod(0o444)
                with (
                    mock.patch.multiple(
                        dispatch,
                        RUNTIME=app_dir,
                        CURRENT=app_dir / "current",
                        TRUSTED_LIVE_QA_ROOT=trusted,
                        ACTIVE_MANIFEST=active_manifest_path,
                        PAYLOAD_ROOT=payload_root,
                        ACTIVE_POINTER=trusted / "active",
                    ),
                    mock.patch.multiple(
                        safe_env,
                        PRODUCTION_RUNTIME_ROOT=app_dir,
                        ACTIVE_PLATFORM_ROOT=app_dir / "current",
                        LIVE_QA_ROOT=trusted,
                        LIVE_QA_RELEASE_ROOT=payload_root,
                        LIVE_QA_ACTIVE_MANIFEST=active_manifest_path,
                        LIVE_QA_ACTIVE_POINTER=trusted / "active",
                    ),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertNotEqual(dispatch.main(["verify", reused_sha]), 0)
                    self.assertNotEqual(safe_env.main(["validate-runtime"]), 0)
                external_provider_path.chmod(0o644)
                external_provider_path.write_bytes(external_provider_bytes)
                external_provider_path.chmod(0o444)
                active_manifest_path.chmod(0o644)
                active_manifest_path.write_bytes(active_manifest_bytes)
                active_manifest_path.chmod(0o444)

                # A stray local provider tree is rejected at every whole-payload reader.
                stray = reused_payload / "provider-runtime"
                stray.mkdir(mode=0o555)
                with (
                    mock.patch.multiple(
                        dispatch,
                        RUNTIME=app_dir,
                        CURRENT=app_dir / "current",
                        TRUSTED_LIVE_QA_ROOT=trusted,
                        ACTIVE_MANIFEST=trusted / "active-manifest.json",
                        PAYLOAD_ROOT=payload_root,
                        ACTIVE_POINTER=trusted / "active",
                    ),
                    mock.patch.multiple(
                        safe_env,
                        PRODUCTION_RUNTIME_ROOT=app_dir,
                        ACTIVE_PLATFORM_ROOT=app_dir / "current",
                        LIVE_QA_ROOT=trusted,
                        LIVE_QA_RELEASE_ROOT=payload_root,
                        LIVE_QA_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                        LIVE_QA_ACTIVE_POINTER=trusted / "active",
                    ),
                    mock.patch.multiple(
                        guard,
                        TRUSTED_SECRET_ROOT=trusted,
                        TRUSTED_PAYLOAD_ROOT=payload_root,
                        TRUSTED_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                        TRUSTED_ACTIVE_POINTER=trusted / "active",
                    ),
                ):
                    with redirect_stderr(io.StringIO()):
                        self.assertNotEqual(dispatch.main(["verify", reused_sha]), 0)
                        self.assertNotEqual(safe_env.main(["validate-runtime"]), 0)
                    with self.assertRaises(RuntimeError):
                        dispatch._payload_tree_digest(reused_payload, reused_sha)
                    with self.assertRaises(safe_env.SafeEnvError):
                        safe_env._validate_liveqa_payload_tree(reused_payload, runtime._read_manifest())
                    with self.assertRaises(guard.GuardError):
                        guard._validate_installed_payload_root(reused_payload, target_sha=reused_sha)
                    with mock.patch.object(guard, "_active_release_commit", return_value=reused_sha):
                        with self.assertRaises(guard.GuardError):
                            guard._trusted_runtime_root()

                # The standalone parsers independently reject the same malformed
                # provider map, before a full-payload digest can obscure the cause.
                valid_provider = json.loads((payload_root / repaired_sha / "runtime-provider.json").read_text())
                malformed_cases = (
                    {**link_map, "browsers/webkit-2311/lib/libwpe.so": "../../escape"},
                    {
                        "browsers/webkit-2311/lib/libwpe.so": "libwpe.so.1",
                        "browsers/webkit-2311/lib/libwpe.so.1": "libwpe.so",
                    },
                    {"browsers/webkit-2311/lib/missing.so": "absent.so"},
                    {"browsers/webkit-2311/lib/bad\x7f.so": "libwpe.so.1.9.3"},
                    {"browsers/webkit-2311/lib/libwpe.so": "libwpe.so.1\n"},
                )
                parser_root = root / "provider-parser-cases"
                parser_root.mkdir(mode=0o700)
                for case_index, bad_links in enumerate(malformed_cases):
                    case_root = parser_root / str(case_index)
                    case_root.mkdir(mode=0o700)
                    provider_path = case_root / "runtime-provider.json"
                    provider_path.write_text(
                        json.dumps({**valid_provider, "engine_links": bad_links}, sort_keys=True) + "\n",
                        encoding="ascii",
                    )
                    os.chmod(provider_path, 0o444)
                    with self.subTest(provider_link_case=case_index):
                        with self.assertRaises(RuntimeError):
                            dispatch._payload_provider_links(case_root, repaired_sha)
                        with self.assertRaises(safe_env.SafeEnvError):
                            safe_env._liveqa_payload_links(case_root, repaired_sha)
                        with self.assertRaises(guard.GuardError):
                            guard._provider_link_map({**valid_provider, "engine_links": bad_links})

                boolean_version_root = parser_root / "boolean-version"
                boolean_version_root.mkdir(mode=0o700)
                (boolean_version_root / "runtime-provider.json").write_text(
                    json.dumps({**valid_provider, "version": True}) + "\n",
                    encoding="ascii",
                )
                with self.assertRaises(RuntimeError):
                    dispatch._payload_provider_links(boolean_version_root, repaired_sha)
                with self.assertRaises(safe_env.SafeEnvError):
                    safe_env._liveqa_payload_links(boolean_version_root, repaired_sha)
                with self.assertRaises(guard.GuardError):
                    guard._active_payload_provider_files(boolean_version_root, repaired_sha)
                nested_package_provider = {
                    **valid_provider,
                    "engine_files": {
                        **valid_provider["engine_files"],
                        "web/node_modules/playwright/node_modules/extra/index.js": "a" * 64,
                    },
                }
                nested_package_root = parser_root / "nested-package"
                nested_package_root.mkdir(mode=0o700)
                (nested_package_root / "runtime-provider.json").write_text(
                    json.dumps(nested_package_provider) + "\n", encoding="ascii"
                )
                with self.assertRaises(RuntimeError):
                    dispatch._payload_provider_links(nested_package_root, repaired_sha)
                with self.assertRaises(safe_env.SafeEnvError):
                    safe_env._liveqa_payload_links(nested_package_root, repaired_sha)
                with self.assertRaises(guard.GuardError):
                    guard._provider_link_map(nested_package_provider)

    def _provider_recovery_fixture(self, root: Path):
        source_sha = runtime.PROVIDER_RECOVERY_SOURCE_SHA
        app_dir = root / "platform"
        releases = app_dir / "releases"
        payload_root = root / "liveqa" / "releases"
        releases.mkdir(parents=True, mode=0o755)
        payload_root.mkdir(parents=True, mode=0o755)
        os.chmod(app_dir, 0o755)
        owners = ["a" * 40, "b" * 40, "c" * 40]
        for index, owner_sha in enumerate(owners[:2]):
            release = releases / f"release-{index}"
            release.mkdir(mode=0o755)
            (release / "RELEASE.json").write_text(
                json.dumps({"source_git_commit": owner_sha, "release_slug": release.name})
                + "\n",
                encoding="ascii",
            )
            os.chmod(release / "RELEASE.json", 0o444)
            (app_dir / ("current" if index == 0 else "previous")).symlink_to(release)

        source_parent = root / "attested-source"
        source_parent.mkdir(mode=0o700)
        fixture_runtime, engine_files, lock_sha = self._engine_provider_fixture(source_parent)
        runtime_source = source_parent / "liveqa-runtime"
        fixture_runtime.rename(runtime_source)
        # The source-bound 19603 v1 artifact predates these two newer suite
        # files. Recovery may accept this exact historical manifest only when
        # its source SHA and manifest digest are both fixed above.
        for relative in (
            "web/tests/smoke/live-launch.spec.ts",
            "web/tests/support/live-count-reporter.cjs",
        ):
            (runtime_source / relative).unlink()
        sandbox = runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE
        sandbox_sha = hashlib.sha256(sandbox.read_bytes()).hexdigest()
        with mock.patch.multiple(
            runtime,
            CHROMIUM_SANDBOX_SIZE=sandbox.stat().st_size,
            CHROMIUM_SANDBOX_SHA256=sandbox_sha,
        ):
            full_tree_sha, full_files = runtime._tree_digest(
                runtime_source,
                ignored=frozenset({runtime.RUNTIME_MANIFEST_RELATIVE.as_posix()}),
                sandbox_relative=runtime.RUNTIME_SANDBOX_RELATIVE,
            )
        source_manifest = {
            "version": 1,
            "node_version": "26.3.1",
            "package_lock_sha256": lock_sha,
            "tree_sha256": full_tree_sha,
            "files": full_files,
        }
        manifest_path = runtime_source / runtime.RUNTIME_MANIFEST_RELATIVE
        manifest_path.write_text(json.dumps(source_manifest, sort_keys=True) + "\n", encoding="ascii")
        os.chown(manifest_path, 0, 0)
        os.chmod(manifest_path, 0o444)
        source_manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        os.chmod(runtime_source, 0o555)
        engine_sha = runtime._engine_digest(
            engine_files,
            node_version="26.3.1",
            package_lock_sha256=lock_sha,
        )
        consumer = {
            "version": 1,
            "provider_sha": source_sha,
            "engine_tree_sha256": engine_sha,
            "node_version": "26.3.1",
            "package_lock_sha256": lock_sha,
            "engine_files": engine_files,
        }
        for owner_sha in owners:
            payload = payload_root / owner_sha
            payload.mkdir(mode=0o755)
            record = {**consumer, "source_sha": owner_sha}
            provider_path = payload / "runtime-provider.json"
            provider_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
            os.chown(provider_path, 0, 0)
            os.chmod(provider_path, 0o444)
            os.chown(payload, 0, 0)
            os.chmod(payload, 0o555)
        trusted = root / "liveqa"
        (trusted / "active").symlink_to(f"releases/{owners[2]}")
        active_manifest_path = trusted / "active-manifest.json"
        active_manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_sha": owners[2],
                    "release_slug": "active-generation",
                    "payload": str(payload_root / owners[2]),
                    "payload_tree_sha256": "d" * 64,
                    "files": {},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="ascii",
        )
        os.chown(active_manifest_path, 0, 0)
        os.chmod(active_manifest_path, 0o444)
        os.chown(trusted, 0, 0)
        os.chmod(trusted, 0o700)
        return (
            app_dir,
            runtime_source,
            payload_root,
            owners,
            engine_files,
            engine_sha,
            lock_sha,
            sandbox_sha,
            source_manifest_sha,
        )

    def test_provider_recovery_publishes_only_attested_engine_and_preserves_consumers(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                engine_files,
                engine_sha,
                lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            active_pointer = root / "liveqa" / "active"
            before_pointer_ids = tuple(
                (path.name, path.lstat().st_ino, os.readlink(path))
                for path in (app_dir / "current", app_dir / "previous", active_pointer)
            )
            before_consumer_hashes = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=root / "liveqa" / "active-manifest.json",
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"):
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_runtime_source(
                        runtime_source,
                        source_sha=runtime.PROVIDER_RECOVERY_SOURCE_SHA,
                    )
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_runtime_source(
                        runtime_source,
                        allow_attested_provider_recovery=True,
                        source_sha="f" * 40,
                    )
                result = runtime.recover_provider(
                    app_dir,
                    runtime_source,
                )
                self.assertEqual(result["status"], "published")
                provider_root = payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA
                self.assertEqual({path.name for path in provider_root.iterdir()}, {"runtime"})
                runtime._validate_engine_root(
                    provider_root / "runtime",
                    expected_files=engine_files,
                    engine_sha256=engine_sha,
                    node_version="26.3.1",
                    package_lock_sha256=lock_sha,
                )
                after_pointer_ids = tuple(
                    (path.name, path.lstat().st_ino, os.readlink(path))
                    for path in (app_dir / "current", app_dir / "previous", active_pointer)
                )
                self.assertEqual(after_pointer_ids, before_pointer_ids)
                after_consumer_hashes = tuple(
                    runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                    for owner in owners
                )
                self.assertEqual(after_consumer_hashes, before_consumer_hashes)
                repeated = runtime.recover_provider(app_dir, runtime_source)
                self.assertEqual(repeated["status"], "already-valid")

    def test_provider_recovery_accepts_three_logical_roles_with_current_active_alias(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                _lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            active_pointer = root / "liveqa" / "active"
            active_pointer.unlink()
            active_pointer.symlink_to(f"releases/{owners[0]}")
            active_manifest = root / "liveqa" / "active-manifest.json"
            active_manifest.chmod(0o644)
            active_manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source_sha": owners[0],
                        "release_slug": "active-generation",
                        "payload": str(payload_root / owners[0]),
                        "payload_tree_sha256": "d" * 64,
                        "files": {},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
            os.chown(active_manifest, 0, 0)
            active_manifest.chmod(0o444)
            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=active_manifest,
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"):
                result = runtime.recover_provider(app_dir, runtime_source)
            self.assertEqual(result["status"], "published")
            self.assertTrue((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA / "runtime").is_dir())

    def test_provider_recovery_rejects_missing_logical_reference(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                _runtime_source,
                payload_root,
                _owners,
                engine_files,
                engine_sha,
                lock_sha,
                _sandbox_sha,
                _manifest_sha,
            ) = self._provider_recovery_fixture(root)
            (app_dir / "current").unlink()
            with mock.patch.multiple(
                runtime,
                PAYLOAD_ROOT=payload_root,
                ACTIVE_POINTER=root / "liveqa" / "active",
                ACTIVE_MANIFEST=root / "liveqa" / "active-manifest.json",
            ), self.assertRaisesRegex(
                runtime.InstallerError, "all three logical references"
            ):
                runtime._provider_recovery_snapshot(
                    app_dir,
                    source_sha=runtime.PROVIDER_RECOVERY_SOURCE_SHA,
                    runtime_manifest={
                        "engine_tree_sha256": engine_sha,
                        "node_version": "26.3.1",
                        "package_lock_sha256": lock_sha,
                        "engine_files": engine_files,
                    },
                )

    def test_provider_recovery_cli_diagnostic_hides_exception_text(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(runtime, "_require_release_lock"), mock.patch.object(
            runtime,
            "recover_provider",
            side_effect=runtime.InstallerError("private path and provider details"),
        ), redirect_stderr(stderr):
            result = runtime.main(
                ["provider-recover", "--runtime-source", "/tmp/fixed-source"]
            )
        diagnostic = stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("phase=provider_recovery", diagnostic)
        self.assertIn("error=InstallerError", diagnostic)
        self.assertIn("function=main", diagnostic)
        self.assertNotIn("private path", diagnostic)
        self.assertNotIn("provider details", diagnostic)

    def test_provider_recovery_refuses_alternate_app_directory_before_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            app_dir, runtime_source, *_ = self._provider_recovery_fixture(root)
            alternate_app_dir = root / "other-platform"
            alternate_app_dir.mkdir(mode=0o755)
            with mock.patch.object(runtime, "APP_DIR", app_dir), mock.patch.object(
                runtime, "_require_release_lock"
            ) as require_lock:
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "restricted to the production application directory",
                ):
                    runtime.recover_provider(alternate_app_dir, runtime_source)
            require_lock.assert_not_called()

    def test_provider_recovery_refuses_wrong_reference_or_existing_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                engine_files,
                _engine_sha,
                lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            reference = payload_root / owners[0] / "runtime-provider.json"
            wrong = json.loads(reference.read_text(encoding="ascii"))
            wrong["engine_tree_sha256"] = "f" * 64
            reference.chmod(0o644)
            reference.write_text(json.dumps(wrong) + "\n", encoding="ascii")
            reference.chmod(0o444)
            active_pointer = root / "liveqa" / "active"
            patch_args = {
                "APP_DIR": app_dir,
                "PAYLOAD_ROOT": payload_root,
                "TRUSTED_ROOT": root / "liveqa",
                "ACTIVE_POINTER": active_pointer,
                "ACTIVE_MANIFEST": root / "liveqa" / "active-manifest.json",
                "PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256": source_manifest_sha,
                "CHROMIUM_SANDBOX_SIZE": (runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                "CHROMIUM_SANDBOX_SHA256": sandbox_sha,
            }
            with mock.patch.multiple(runtime, **patch_args), mock.patch.object(
                runtime, "_require_release_lock"
            ), self.assertRaises(runtime.InstallerError):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertFalse((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA).exists())

    def test_provider_recovery_refuses_invalid_existing_target_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                _owners,
                engine_files,
                _engine_sha,
                lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            target = payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA
            target.mkdir(mode=0o700)
            sentinel = target / "operator-data"
            sentinel.write_text("preserve\n", encoding="ascii")
            active_pointer = root / "liveqa" / "active"
            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=root / "liveqa" / "active-manifest.json",
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"), self.assertRaises(
                runtime.InstallerError
            ):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertEqual(sentinel.read_text(encoding="ascii"), "preserve\n")
            self.assertEqual({path.name for path in target.iterdir()}, {"operator-data"})

    def test_provider_recovery_rejects_unbound_manifest_and_preserves_existing_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                _lock_sha,
                sandbox_sha,
                _source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            active_pointer = root / "liveqa" / "active"
            references_before = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=root / "liveqa" / "active-manifest.json",
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"), self.assertRaises(
                runtime.InstallerError
            ):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertFalse((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA).exists())
            references_after = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            self.assertEqual(references_after, references_before)

    def test_provider_recovery_requires_matching_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                _lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            manifest_path = root / "liveqa" / "active-manifest.json"
            references_before = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            patch_args = {
                "APP_DIR": app_dir,
                "PAYLOAD_ROOT": payload_root,
                "TRUSTED_ROOT": root / "liveqa",
                "ACTIVE_POINTER": root / "liveqa" / "active",
                "ACTIVE_MANIFEST": manifest_path,
                "PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256": source_manifest_sha,
                "CHROMIUM_SANDBOX_SIZE": (runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                "CHROMIUM_SANDBOX_SHA256": sandbox_sha,
            }
            for state in ("missing", "malformed", "mismatched"):
                with self.subTest(state=state):
                    if manifest_path.exists():
                        manifest_path.chmod(0o644)
                    if state == "missing":
                        manifest_path.unlink(missing_ok=True)
                    elif state == "malformed":
                        manifest_path.write_text("{\"version\":\n", encoding="ascii")
                        os.chown(manifest_path, 0, 0)
                        os.chmod(manifest_path, 0o444)
                    else:
                        mismatched = {
                            "version": 1,
                            "source_sha": "f" * 40,
                            "release_slug": "active-generation",
                            "payload": str(payload_root / ("f" * 40)),
                            "payload_tree_sha256": "d" * 64,
                            "files": {},
                        }
                        manifest_path.write_text(
                            json.dumps(mismatched, sort_keys=True) + "\n",
                            encoding="ascii",
                        )
                        os.chown(manifest_path, 0, 0)
                        os.chmod(manifest_path, 0o444)
                    with mock.patch.multiple(runtime, **patch_args), mock.patch.object(
                        runtime, "_require_release_lock"
                    ), self.assertRaises(runtime.InstallerError):
                        runtime.recover_provider(app_dir, runtime_source)
                    self.assertFalse((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA).exists())
                    references_after = tuple(
                        runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                        for owner in owners
                    )
                    self.assertEqual(references_after, references_before)

    def test_provider_recovery_detects_active_provider_directory_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                _lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            active_owner_root = payload_root / owners[2]
            active_pointer = root / "liveqa" / "active"
            active_manifest = root / "liveqa" / "active-manifest.json"
            references_before = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            real_snapshot = runtime._provider_recovery_snapshot
            calls = 0

            def replace_active_owner_after_first_snapshot(
                *args: object,
                **kwargs: object,
            ) -> tuple[object, ...]:
                nonlocal calls
                result = real_snapshot(*args, **kwargs)
                calls += 1
                if calls == 1:
                    replacement = payload_root / ".active-owner-replacement"
                    os.chmod(active_owner_root, 0o755)
                    os.rename(active_owner_root, replacement)
                    active_owner_root.mkdir(mode=0o755)
                    os.rename(replacement / "runtime-provider.json", active_owner_root / "runtime-provider.json")
                    os.chown(active_owner_root / "runtime-provider.json", 0, 0)
                    os.chmod(active_owner_root / "runtime-provider.json", 0o444)
                    os.chown(active_owner_root, 0, 0)
                    os.chmod(active_owner_root, 0o555)
                    os.rmdir(replacement)
                return result

            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=active_manifest,
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"), mock.patch.object(
                runtime,
                "_provider_recovery_snapshot",
                side_effect=replace_active_owner_after_first_snapshot,
            ), self.assertRaisesRegex(
                runtime.InstallerError,
                "provider recovery references changed before publish",
            ):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertEqual(calls, 2)
            self.assertFalse((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA).exists())
            self.assertFalse((payload_root / ".active-owner-replacement").exists())
            references_after = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            self.assertEqual(references_after, references_before)

    def test_provider_recovery_reference_drift_cleans_only_private_stage(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            active_pointer = root / "liveqa" / "active"
            active_manifest = root / "liveqa" / "active-manifest.json"
            unrelated_before = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            real_snapshot = runtime._provider_recovery_snapshot
            calls = 0

            def mutate_after_first_snapshot(*args: object, **kwargs: object) -> tuple[object, ...]:
                nonlocal calls
                calls += 1
                result = real_snapshot(*args, **kwargs)
                if calls == 1:
                    active_manifest.chmod(0o644)
                    active_manifest.write_text("{\"changed\":true}\n", encoding="ascii")
                    active_manifest.chmod(0o444)
                return result

            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=active_manifest,
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"), mock.patch.object(
                runtime,
                "_provider_recovery_snapshot",
                side_effect=mutate_after_first_snapshot,
            ), self.assertRaisesRegex(
                runtime.InstallerError,
                "trusted live-QA manifest schema is invalid",
            ):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertEqual(calls, 2)
            self.assertFalse((payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA).exists())
            self.assertEqual(
                sorted(path.name for path in payload_root.iterdir()),
                sorted(owners),
            )
            unrelated_after = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            self.assertEqual(unrelated_after, unrelated_before)

    def test_provider_recovery_publish_failure_removes_only_its_private_stage(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            (
                app_dir,
                runtime_source,
                payload_root,
                owners,
                _engine_files,
                _engine_sha,
                _lock_sha,
                sandbox_sha,
                source_manifest_sha,
            ) = self._provider_recovery_fixture(root)
            target = payload_root / runtime.PROVIDER_RECOVERY_SOURCE_SHA
            active_pointer = root / "liveqa" / "active"
            unrelated_before = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            real_rename = os.rename

            def fail_provider_publish(source: object, destination: object) -> None:
                if Path(destination) == target:
                    raise OSError("injected publish failure")
                real_rename(source, destination)

            with mock.patch.multiple(
                runtime,
                APP_DIR=app_dir,
                PAYLOAD_ROOT=payload_root,
                TRUSTED_ROOT=root / "liveqa",
                ACTIVE_POINTER=active_pointer,
                ACTIVE_MANIFEST=root / "liveqa" / "active-manifest.json",
                PROVIDER_RECOVERY_RUNTIME_MANIFEST_SHA256=source_manifest_sha,
                CHROMIUM_SANDBOX_SIZE=(runtime_source / runtime.RUNTIME_SANDBOX_RELATIVE).stat().st_size,
                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
            ), mock.patch.object(runtime, "_require_release_lock"), mock.patch.object(
                runtime.os, "rename", side_effect=fail_provider_publish
            ), self.assertRaises(OSError):
                runtime.recover_provider(app_dir, runtime_source)
            self.assertFalse(target.exists())
            self.assertEqual(sorted(path.name for path in payload_root.iterdir()), sorted(owners))
            unrelated_after = tuple(
                runtime._digest_regular(payload_root / owner / "runtime-provider.json")
                for owner in owners
            )
            self.assertEqual(unrelated_after, unrelated_before)

    def test_retention_protects_transitively_referenced_provider(self) -> None:
        source_shas = ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40]
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            app_dir = root / "platform"
            releases = app_dir / "releases"
            releases.mkdir(mode=0o755, parents=True)
            payloads = root / "liveqa" / "releases"
            payloads.mkdir(mode=0o755, parents=True)
            for source_sha in source_shas[:2]:
                release = releases / f"release-{source_sha[:8]}"
                release.mkdir(mode=0o555)
                record = {"source_git_commit": source_sha, "release_slug": release.name}
                (release / "RELEASE.json").write_text(json.dumps(record), encoding="ascii")
                os.chmod(release / "RELEASE.json", 0o444)
            (app_dir / "current").symlink_to(releases / f"release-{source_shas[0][:8]}")
            (app_dir / "previous").symlink_to(releases / f"release-{source_shas[1][:8]}")
            for source_sha in (*source_shas[1:2], *source_shas[3:]):
                payload = payloads / source_sha
                payload.mkdir(mode=0o555)
            provider = {
                "version": 1,
                "source_sha": source_shas[1],
                "provider_sha": source_shas[2],
                "engine_tree_sha256": "f" * 64,
                "node_version": "26.3.1",
                "package_lock_sha256": "e" * 64,
                "engine_files": {"node/bin/node": "d" * 64},
            }
            provider_path = payloads / source_shas[1] / "runtime-provider.json"
            provider_path.write_text(json.dumps(provider), encoding="ascii")
            os.chmod(provider_path, 0o444)
            trusted = root / "liveqa"
            (trusted / "active").symlink_to(f"releases/{source_shas[1]}")
            with mock.patch.multiple(
                runtime,
                PAYLOAD_ROOT=payloads,
                ACTIVE_POINTER=trusted / "active",
            ):
                protected = runtime._protected_shas(app_dir)
                self.assertIn(source_shas[2], protected)
                self.assertEqual(protected, set(source_shas[:3]))
                # A protected manifest may name a provider that is not yet
                # present during repair. Keep its identity in the closure so
                # a later materialization cannot be treated as spare cache.
                (payloads / source_shas[2]).mkdir(mode=0o555)
                self.assertEqual(runtime._retention(app_dir, apply=False), 2)
                self.assertTrue((payloads / source_shas[3]).exists())
                self.assertTrue((payloads / source_shas[4]).exists())
                self.assertEqual(runtime._retention(app_dir, apply=True), 2)
                self.assertTrue((payloads / source_shas[2]).exists())
                self.assertFalse((payloads / source_shas[3]).exists())
                self.assertFalse((payloads / source_shas[4]).exists())
                os.chmod(provider_path, 0o644)
                provider_path.write_text("{}\n", encoding="ascii")
                os.chmod(provider_path, 0o444)
                with self.assertRaises(runtime.InstallerError):
                    runtime._protected_shas(app_dir)

    @contextmanager
    def runtime_tree(self, root: Path, source_sha: str):
        trusted = root / "liveqa"
        payload_root = trusted / "releases"
        app_dir = root / "platform"
        releases = app_dir / "releases"
        release = releases / f"release-{source_sha[:8]}"
        tools = release / "tools"
        tools.mkdir(parents=True, mode=0o755)
        releases.mkdir(parents=True, exist_ok=True)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha, "release_slug": release.name}) + "\n",
            encoding="ascii",
        )
        os.chmod(release / "RELEASE.json", 0o444)
        app_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(app_dir, 0o700)
        (app_dir / "shared").mkdir(mode=0o700)
        (app_dir / "current").symlink_to(release)

        entrypoints = (
            "platform_live_user_qa_trusted.sh",
            "platform_live_launch_trusted.sh",
            "platform_live_user_qa_dispatch.py",
            "platform_workflow_remote_dispatch.py",
            "platform_validate_release_artifact.py",
            "platform_workflow_input_guard.py",
            "platform_release_lock_exec.sh",
            "platform_release_lock.sh",
            "platform_live_qa_mailbox_helper.py",
        )
        for name in entrypoints:
            path = tools / name
            path.write_text(f"#!/bin/sh\n# {name}\n", encoding="ascii")
            os.chmod(path, 0o755 if name.endswith(".sh") else 0o555)
        runtime_source = release / "liveqa-runtime"
        runtime_source.mkdir(mode=0o555)

        constants = {
            "TRUSTED_ROOT": trusted,
            "PAYLOAD_ROOT": payload_root,
            "ACTIVE_MANIFEST": trusted / "active-manifest.json",
            "ACTIVE_POINTER": trusted / "active",
            "CSP_LIVE_QA_BUNDLE_PATH": trusted / "csp-live-qa.json",
            "HELPER_PATH": trusted / entrypoints[0],
            "LAUNCH_HELPER_PATH": trusted / entrypoints[1],
            "DISPATCHER_PATH": trusted / entrypoints[2],
            "REMOTE_DISPATCHER_PATH": trusted / entrypoints[3],
            "RELEASE_VALIDATOR_PATH": trusted / entrypoints[4],
            "REMOTE_INPUT_GUARD_PATH": trusted / entrypoints[5],
            "RELEASE_LOCK_EXEC_PATH": trusted / entrypoints[6],
            "RELEASE_LOCK_HELPER_PATH": trusted / entrypoints[7],
            "MAILBOX_HELPER_PATH": trusted / entrypoints[8],
            "TOOL_FILES": entrypoints,
            "SOURCE_TREES": (),
            "SOURCE_FILES": (),
        }
        with mock.patch.multiple(runtime, **constants), \
            mock.patch.object(runtime, "_require_release_lock"), \
            mock.patch.object(runtime, "_validate_runtime_source"), \
            mock.patch.object(runtime, "_copy_tree", side_effect=self._copy_empty_tree), \
            mock.patch.object(runtime, "_validate_payload"), \
            mock.patch.object(runtime, "_retention", return_value=0), \
            mock.patch.object(runtime, "_cleanup_staging", return_value=0):
            yield app_dir, release, trusted, payload_root

    @contextmanager
    def canonical_runtime_tree(self, root: Path, source_sha: str):
        """Build the installer fixture from the production TOOL_FILES set."""

        trusted = root / "liveqa"
        payload_root = trusted / "releases"
        app_dir = root / "platform"
        releases = app_dir / "releases"
        release = releases / f"release-{source_sha[:8]}"
        tools = release / "tools"
        tools.mkdir(mode=0o755, parents=True)
        os.chmod(releases, 0o755)
        os.chmod(release, 0o755)
        os.chmod(tools, 0o755)
        app_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(app_dir, 0o755)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha, "release_slug": release.name})
            + "\n",
            encoding="ascii",
        )
        os.chmod(release / "RELEASE.json", 0o444)
        (app_dir / "current").symlink_to(release)
        runtime_source = release / "liveqa-runtime"
        runtime_source.mkdir(mode=0o755)

        source_tools = SCRIPT.parent
        for name in runtime.TOOL_FILES:
            source = source_tools / name
            destination = tools / name
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o555 if name.endswith(".sh") else 0o444)

        source_files = runtime.SOURCE_FILES
        for relative in source_files:
            source = SCRIPT.parents[1] / relative
            destination = release / relative
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o644)

        constants = {
            "TRUSTED_ROOT": trusted,
            "PAYLOAD_ROOT": payload_root,
            "ACTIVE_MANIFEST": trusted / "active-manifest.json",
            "ACTIVE_POINTER": trusted / "active",
            "CSP_LIVE_QA_BUNDLE_PATH": trusted / "csp-live-qa.json",
            "HELPER_PATH": trusted / "platform_live_user_qa_trusted.sh",
            "LAUNCH_HELPER_PATH": trusted / "platform_live_launch_trusted.sh",
            "DISPATCHER_PATH": trusted / "platform_live_user_qa_dispatch.py",
            "REMOTE_DISPATCHER_PATH": trusted / "platform_workflow_remote_dispatch.py",
            "RELEASE_VALIDATOR_PATH": trusted / "platform_validate_release_artifact.py",
            "REMOTE_INPUT_GUARD_PATH": trusted / "platform_workflow_input_guard.py",
            "RELEASE_LOCK_EXEC_PATH": trusted / "platform_release_lock_exec.sh",
            "RELEASE_LOCK_HELPER_PATH": trusted / "platform_release_lock.sh",
            "MAILBOX_HELPER_PATH": trusted / "platform_live_qa_mailbox_helper.py",
            "SOURCE_TREES": (),
            "SOURCE_FILES": source_files,
        }
        with mock.patch.multiple(runtime, **constants), \
            mock.patch.object(runtime, "_require_release_lock"), \
            mock.patch.object(runtime, "_validate_runtime_source"), \
            mock.patch.object(runtime, "_retention", return_value=0), \
            mock.patch.object(runtime, "_cleanup_staging", return_value=0):
            yield app_dir, release, trusted, payload_root

    @staticmethod
    def _copy_empty_tree(source: Path, destination: Path, **_kwargs: object) -> dict[str, str]:
        del source
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        return {}

    @unittest.skipUnless(os.geteuid() == 0, "release lock probe requires root-owned /run/lock")
    def test_release_lock_identity_is_not_shadowed_by_payload_helper(self) -> None:
        """The copied helper is payload data, not the canonical mutex path."""

        source = SCRIPT.read_text(encoding="utf-8")
        canonical = 'Path("/run/lock/oldsparky-platform-release.lock")'
        self.assertIn(canonical, source)
        helper_source_path = SCRIPT.with_name("platform_release_lock.sh")
        helper_source = helper_source_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture = Path(temporary)
            trusted = fixture / "liveqa"
            trusted.mkdir(mode=0o700)
            lock_path = Path("/run/lock") / f"oldsparky-platform-release-test-{uuid4().hex}.lock"
            self.assertFalse(lock_path.exists())

            helper_source = helper_source.replace(
                "/run/lock/oldsparky-platform-release.lock", str(lock_path)
            ).replace(
                "/run/lock/oldsparky-retained-load-matrix.lock",
                "/run/lock/oldsparky-retained-load-test-unused.lock",
            )
            helper_path = fixture / "platform_release_lock.sh"
            helper_path.write_text(helper_source, encoding="utf-8")
            os.chmod(helper_path, 0o500)

            installer_source = source.replace(
                canonical, f'Path("{lock_path}")'
            ).replace(
                'Path("/root/.oldsparky/liveqa")', f'Path("{trusted}")'
            )
            installer_path = fixture / "platform_live_qa_runtime_install.py"
            installer_path.write_text(installer_source, encoding="utf-8")
            os.chmod(installer_path, 0o500)

            callback = fixture / "probe.py"
            callback.write_text(
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('lock_probe', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module._require_release_lock()\n"
                "print('LOCK_PROBE_OK')\n",
                encoding="ascii",
            )
            runner = fixture / "probe.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                f"source {shlex.quote(str(helper_path))}\n"
                "platform_release_lock_supervise \"$@\"\n"
                'if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then exit 0; fi\n'
                "platform_release_lock_open\n"
                "exec /usr/bin/python3 -I -B \"$2\" \"$1\"\n",
                encoding="ascii",
            )
            os.chmod(runner, 0o500)

            def run_probe() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [str(runner), str(installer_path), str(callback)],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )

            def assert_probe_passes() -> None:
                completed = run_probe()
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "LOCK_PROBE_OK\n")
                self.assertEqual(completed.stderr, "")

            try:
                # The runtime predicate must still reject a caller with no
                # live pathname-form lock owner.
                unlocked = subprocess.run(
                    ["/usr/bin/python3", "-I", "-B", str(callback), str(installer_path)],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertNotEqual(unlocked.returncode, 0)
                self.assertNotIn("LOCK_PROBE_OK", unlocked.stdout)

                # First creation has no copied payload helper yet.
                assert_probe_passes()

                # After publication, the copied helper is immutable mode 0444;
                # it must not be mistaken for the active lock file.
                payload_helper = trusted / "platform_release_lock.sh"
                payload_helper.write_text("# immutable payload lock helper\n", encoding="ascii")
                os.chmod(payload_helper, 0o444)
                assert_probe_passes()
            finally:
                if lock_path.exists() and not lock_path.is_symlink():
                    metadata = lock_path.lstat()
                    if metadata.st_uid == 0 and metadata.st_nlink == 1:
                        lock_path.unlink()

    def test_install_writes_the_exact_relative_generation_pointer(self) -> None:
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                previous_umask = os.umask(0o077)
                try:
                    manifest = runtime.install(app_dir, release)
                finally:
                    os.umask(previous_umask)
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
                self.assertEqual((trusted / "active").resolve(), payload_root / source_sha)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)
                runtime._validate_active_pointer(source_sha)

        # Exercise the real fixed TOOL_FILES list and installer copy/digest
        # path: retained cleanup must be present in the installed generation,
        # not just in a hand-built test manifest.
        source_sha = "f" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                manifest = runtime.install(app_dir, release)
                payload = payload_root / source_sha
                cleanup_tools = (
                    "platform_cleanup_retained_orphan.py",
                    "platform_workflow_input_guard.py",
                    "platform_recover_retained_report.py",
                    "platform_cleanup_retained_matrix.py",
                )
                for name in cleanup_tools:
                    relative = f"platform/tools/{name}"
                    installed = payload / relative
                    self.assertTrue(installed.is_file(), name)
                    self.assertEqual(
                        runtime._digest_regular(installed), manifest["files"][relative]
                    )

                # Every direct supervisor/provisioning dependency must be
                # copied into and recorded by the immutable runtime manifest.
                launch_members = (
                    "platform/tools/platform_live_launch_supervisor.sh",
                    "platform/tools/platform_live_qa_guard.py",
                    "platform/tools/platform_live_qa_machine_lock.py",
                    "platform/tools/platform_safe_env_exec.py",
                    "platform/tools/platform_live_browser_qa.sh",
                    "platform/tools/platform_provision_live_csp_qa.sh",
                    "platform/tools/platform_provision_live_csp_qa.py",
                    "platform/tools/platform_install_live_qa_user.sh",
                    "platform/tools/platform_release_lock_exec.sh",
                    "platform/tools/platform_release_lock.sh",
                    "platform/tools/platform_validate_release_artifact.py",
                    "platform/deploy/apparmor/oldsparky-liveqa-chromium",
                )
                for relative in launch_members:
                    installed = payload / relative
                    self.assertIn(relative, manifest["files"])
                    self.assertTrue(installed.is_file(), relative)
                    expected_mode = (
                        0o555
                        if relative.endswith(".sh")
                        or relative.endswith("/platform_validate_release_artifact.py")
                        else 0o444
                    )
                    self.assertEqual(
                        stat.S_IMODE(installed.stat().st_mode), expected_mode
                    )
                    self.assertEqual(
                        runtime._digest_regular(installed), manifest["files"][relative]
                    )

                trusted_validator = trusted / "platform_validate_release_artifact.py"
                validator_relative = "platform/tools/platform_validate_release_artifact.py"
                self.assertTrue(trusted_validator.is_file())
                self.assertEqual(stat.S_IMODE(trusted_validator.stat().st_mode), 0o555)
                self.assertEqual(trusted_validator.stat().st_nlink, 1)
                self.assertEqual(
                    runtime._digest_regular(trusted_validator),
                    manifest["files"][validator_relative],
                )

                # Exercise the installer-copied dispatcher and its trusted-root
                # validator sibling, matching the production __file__ layout.
                dispatcher_path = trusted / "platform_workflow_remote_dispatch.py"
                dispatcher_namespace: dict[str, object] = {
                    "__file__": str(dispatcher_path),
                    "__name__": "copied_liveqa_dispatcher_test",
                }
                dispatcher_source = dispatcher_path.read_bytes()
                exec(
                    compile(dispatcher_source, str(dispatcher_path), "exec"),
                    dispatcher_namespace,
                )
                dispatcher_namespace["TRUSTED_LIVE_ROOT"] = trusted
                parse_release = dispatcher_namespace["_parse_active_release_receipt"]
                release_slug = "qa-test-20261009T000000Z"
                release_receipt = {
                    "artifact_format_version": 1,
                    "release_slug": release_slug,
                    "built_at_utc": "20261009T000000Z",
                    "release_ref": "qa-test",
                    "source_git_commit": source_sha,
                    "python_requirements_file": "requirements-platform.txt",
                    "python_lock_file": "requirements-platform.lock.txt",
                    "python_freeze_file": "requirements-platform.freeze.txt",
                    "python_wheelhouse_dir": "wheelhouse",
                    "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
                    "web_package_lock_file": "apps/platform_web/package-lock.json",
                    "web_build_id": "test-build",
                    "node_version": "26.3.1",
                    "npm_version": "11.16.0",
                }
                parsed = parse_release(
                    json.dumps(release_receipt).encode("ascii"),
                    release_slug=release_slug,
                )
                self.assertEqual(parsed["source_git_commit"], source_sha)

                validator_copy = payload / validator_relative
                runtime._validate_payload(
                    manifest, app_dir=app_dir, validate_provider=False
                )
                self.assertEqual(
                    runtime._digest_regular(validator_copy),
                    manifest["files"][validator_relative],
                )
                missing_validator = validator_copy.with_name(
                    validator_copy.name + ".missing-test"
                )
                validator_copy.rename(missing_validator)
                try:
                    missing_tree_sha, missing_files = runtime._tree_digest(payload)
                    missing_manifest = {
                        **manifest,
                        "payload_tree_sha256": missing_tree_sha,
                        "files": missing_files,
                    }
                    with self.assertRaises(runtime.InstallerError):
                        runtime._validate_payload(
                            missing_manifest,
                            app_dir=app_dir,
                            validate_provider=False,
                        )
                finally:
                    missing_validator.rename(validator_copy)

                validator_copy.chmod(0o444)
                try:
                    wrong_mode_tree_sha, wrong_mode_files = runtime._tree_digest(payload)
                    wrong_mode_manifest = {
                        **manifest,
                        "payload_tree_sha256": wrong_mode_tree_sha,
                        "files": wrong_mode_files,
                    }
                    with self.assertRaises(runtime.InstallerError):
                        runtime._validate_payload(
                            wrong_mode_manifest,
                            app_dir=app_dir,
                            validate_provider=False,
                        )
                finally:
                    validator_copy.chmod(0o555)

                trusted_validator.chmod(0o444)
                try:
                    with self.assertRaises(runtime.InstallerError):
                        runtime._validate_payload(
                            manifest, app_dir=app_dir, validate_provider=False
                        )
                finally:
                    trusted_validator.chmod(0o555)

                with tempfile.TemporaryDirectory(dir="/root") as empty_tools:
                    dispatcher_namespace["ACTIVE_TOOLS_DIR"] = Path(empty_tools)
                    with self.assertRaises(OSError):
                        parse_release(
                            json.dumps(release_receipt).encode("ascii"),
                            release_slug=release_slug,
                        )

                with tempfile.TemporaryDirectory(dir="/root") as tampered_tools:
                    tampered_validator = (
                        Path(tampered_tools) / "platform_validate_release_artifact.py"
                    )
                    tampered_validator.write_text("# altered validator\n", encoding="ascii")
                    os.chmod(tampered_validator, 0o555)
                    os.chown(tampered_validator, 0, 0)
                    dispatcher_namespace["ACTIVE_TOOLS_DIR"] = Path(tampered_tools)
                    with self.assertRaises(OSError):
                        parse_release(
                            json.dumps(release_receipt).encode("ascii"),
                            release_slug=release_slug,
                        )

                fake_python_target = Path(temporary) / "trusted-python-target"
                fake_python_target.write_bytes(b"#!/bin/sh\nexit 0\n")
                os.chmod(fake_python_target, 0o755)
                os.chown(fake_python_target, 0, 0)
                fake_python = Path(temporary) / "venv/bin/python"
                fake_python.parent.mkdir(parents=True, mode=0o755)
                fake_python.symlink_to(fake_python_target)
                os.chown(fake_python, 0, 0, follow_symlinks=False)
                selected_tool = payload / "platform/tools/platform_cleanup_retained_matrix.py"

                def execute_selected() -> int:
                    return safe_env.main(
                        [
                            "exec",
                            "--pythonpath",
                            str(payload),
                            "--",
                            str(fake_python),
                            str(selected_tool),
                            "--help",
                        ]
                    )

                with (
                    mock.patch.multiple(
                        safe_env,
                        PRODUCTION_RUNTIME_ROOT=app_dir,
                        ACTIVE_PLATFORM_ROOT=app_dir / "current",
                        LIVE_QA_ROOT=trusted,
                        LIVE_QA_RELEASE_ROOT=payload_root,
                        LIVE_QA_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                        LIVE_QA_ACTIVE_POINTER=trusted / "active",
                        ACTIVE_PYTHON=fake_python,
                        TRUSTED_SYSTEM_PYTHON=fake_python_target,
                    ),
                    mock.patch.object(safe_env, "validate_active_runtime"),
                    mock.patch.object(
                        safe_env,
                        "read_production_env_bytes",
                        return_value=b"PLATFORM_ENVIRONMENT=production\n",
                    ),
                ):
                    # Exercise the unchanged safe-env exec boundary against
                    # the actual installed helper path.  Intercept execve so
                    # no database tool or production environment is run.
                    with (
                        mock.patch.object(
                            safe_env.os, "execve", side_effect=SystemExit(0)
                        ) as execve,
                        self.assertRaises(SystemExit),
                    ):
                        execute_selected()
                    execve.assert_called_once()
                    self.assertEqual(
                        execve.call_args.args[1][1], str(selected_tool)
                    )

                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(
                            safe_env.main(
                                [
                                    "exec",
                                    "--pythonpath",
                                    str(payload),
                                    "--",
                                    str(fake_python),
                                    str(release / "tools/platform_cleanup_retained_matrix.py"),
                                ]
                            ),
                            2,
                        )
                    execve.assert_not_called()

                    release_json = release / "RELEASE.json"
                    release_json.chmod(0o644)
                    release_json.write_text(
                        json.dumps(
                            {"source_git_commit": "e" * 40, "release_slug": release.name}
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    release_json.chmod(0o444)
                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(execute_selected(), 2)
                    execve.assert_not_called()

                    # A self-consistent tree missing the provision wrapper
                    # still fails the installer's required-entrypoint check;
                    # the active safe-env manifest also rejects the changed
                    # generation digest.
                    release_json.chmod(0o644)
                    release_json.write_text(
                        json.dumps(
                            {"source_git_commit": source_sha, "release_slug": release.name}
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    release_json.chmod(0o444)
                    (payload / "platform/tools/platform_provision_live_csp_qa.sh").unlink()
                    changed_digest, changed_files = runtime._tree_digest(payload)
                    changed_manifest = dict(manifest)
                    changed_manifest["payload_tree_sha256"] = changed_digest
                    changed_manifest["files"] = changed_files
                    with self.assertRaisesRegex(
                        runtime.InstallerError, "missing a required entrypoint"
                    ):
                        runtime._validate_payload(changed_manifest)
                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(execute_selected(), 2)
                    execve.assert_not_called()

    def test_preflight_accepts_only_completed_post_userqa_helper_cleanup(self) -> None:
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                manifest = runtime.install(app_dir, release)
                mailbox_helper = trusted / "platform_live_qa_mailbox_helper.py"
                mailbox_helper.unlink()
                active_pointer = os.readlink(trusted / "active")
                payload_digest = manifest["payload_tree_sha256"]

                runtime._validate_payload(
                    manifest,
                    app_dir=app_dir,
                    validate_provider=False,
                    allow_post_userqa_mailbox_helper_omission=True,
                )
                self.assertEqual(os.readlink(trusted / "active"), active_pointer)
                self.assertEqual(manifest["payload_tree_sha256"], payload_digest)
                self.assertFalse(mailbox_helper.exists())
                original_lstat = Path.lstat

                def deny_helper_stat(path: Path):
                    if path == mailbox_helper:
                        raise PermissionError(errno.EACCES, "test permission denial")
                    return original_lstat(path)

                with mock.patch.object(Path, "lstat", deny_helper_stat):
                    with self.assertRaises(PermissionError):
                        runtime._validate_payload(
                            manifest,
                            app_dir=app_dir,
                            validate_provider=False,
                            allow_post_userqa_mailbox_helper_omission=True,
                        )
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(
                        manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                    )
                bundle = trusted / "csp-live-qa.json"
                bundle.write_bytes(b"")
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(
                        manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                        allow_post_userqa_mailbox_helper_omission=True,
                    )
                bundle.unlink()
                bundle.symlink_to(trusted / "missing-csp-live-qa.json")
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(
                        manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                        allow_post_userqa_mailbox_helper_omission=True,
                    )
                bundle.unlink()

                mailbox_helper.write_bytes(b"altered helper\n")
                os.chmod(mailbox_helper, 0o500)
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(
                        manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                        allow_post_userqa_mailbox_helper_omission=True,
                    )

                mailbox_helper.unlink()
                mailbox_helper.symlink_to(trusted / "missing-mailbox-helper.py")
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(
                        manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                        allow_post_userqa_mailbox_helper_omission=True,
                    )
                mailbox_helper.unlink()

                restored_manifest = runtime.install(app_dir, release)
                self.assertTrue(mailbox_helper.is_file())
                runtime._validate_payload(
                    restored_manifest,
                    app_dir=app_dir,
                    validate_provider=False,
                )

                payload_helper = (
                    trusted
                    / "releases"
                    / source_sha
                    / "platform/tools/platform_live_qa_mailbox_helper.py"
                )
                missing_payload_helper = payload_helper.with_name(
                    payload_helper.name + ".missing-test"
                )
                payload_helper.rename(missing_payload_helper)
                try:
                    with self.assertRaises(runtime.InstallerError):
                        runtime._validate_payload(
                            manifest,
                            app_dir=app_dir,
                            validate_provider=False,
                            allow_post_userqa_mailbox_helper_omission=True,
                        )
                finally:
                    missing_payload_helper.rename(payload_helper)

    def test_install_rejects_missing_validator_before_active_generation(self) -> None:
        source_sha = "d" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                source_validator = (
                    release / "tools" / "platform_validate_release_artifact.py"
                )
                source_validator.unlink()
                with self.assertRaises(runtime.InstallerError):
                    runtime.install(app_dir, release)
                self.assertFalse((trusted / "active").exists())
                self.assertFalse((trusted / "active-manifest.json").exists())

    def test_prior_provider_reuse_accepts_m6_payload_without_new_validator(self) -> None:
        self._assert_protected_legacy_machine_lock_compatibility()
        source_sha = "9" * 40
        engine_manifest = {
            "engine_tree_sha256": "d" * 64,
            "node_version": "26.3.1",
            "package_lock_sha256": "e" * 64,
            "engine_files": {},
        }
        provider = {
            "version": 1,
            "source_sha": source_sha,
            "provider_sha": source_sha,
            **engine_manifest,
        }

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                active_manifest = runtime.install(app_dir, release)
                payload = payload_root / source_sha
                validator_relative = "platform/tools/platform_validate_release_artifact.py"
                payload_validator = payload / validator_relative
                trusted_validator = trusted / "platform_validate_release_artifact.py"

                # Model the pre-M7 generation: its provider and all existing
                # payload bindings are valid, but it predates this new
                # validator member and trusted-root sibling.
                os.chmod(payload, 0o700)
                os.chmod(payload / "platform/tools", 0o700)
                payload_validator.unlink()
                trusted_validator.unlink()
                provider_path = payload / "runtime-provider.json"
                provider_path.write_text(
                    json.dumps(provider, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chown(provider_path, 0, 0)
                os.chmod(provider_path, 0o444)
                for directory in sorted(
                    (entry for entry in payload.rglob("*") if entry.is_dir()),
                    key=lambda entry: len(entry.parts),
                    reverse=True,
                ):
                    os.chown(directory, 0, 0)
                    os.chmod(directory, 0o555)
                os.chown(payload, 0, 0)
                os.chmod(payload, 0o555)

                def write_active_manifest() -> dict[str, object]:
                    tree_sha, file_map = runtime._tree_digest(payload)
                    updated = {
                        **active_manifest,
                        "payload_tree_sha256": tree_sha,
                        "files": file_map,
                    }
                    runtime._write_manifest(trusted / "active-manifest.json", updated)
                    return updated

                legacy_manifest = write_active_manifest()
                with (
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=engine_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                    self.assertRaises(runtime.InstallerError),
                ):
                    # Candidate activation/verification stays strict; only
                    # prior-provider reuse opts into the M6-compatible read.
                    runtime._validate_payload(
                        legacy_manifest,
                        app_dir=app_dir,
                        validate_provider=False,
                    )

                with (
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=engine_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                ):
                    reused = runtime._runtime_provider_payload(
                        app_dir,
                        expected_manifest=engine_manifest,
                    )
                self.assertEqual(reused, provider)

                # A one-sided validator installation is not legacy state and
                # must fail rather than being silently accepted.
                trusted_validator.write_bytes(
                    (release / "tools/platform_validate_release_artifact.py").read_bytes()
                )
                os.chown(trusted_validator, 0, 0)
                os.chmod(trusted_validator, 0o555)
                with (
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=engine_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                    self.assertRaises(runtime.InstallerError),
                ):
                    runtime._runtime_provider_payload(
                        app_dir,
                        expected_manifest=engine_manifest,
                    )

                trusted_validator.unlink()

                # Changing the old provider identity remains rejected even
                # when its manifest tree digest is recomputed consistently.
                os.chmod(payload, 0o700)
                provider_path.chmod(0o644)
                provider_path.write_text(
                    json.dumps(
                        {**provider, "engine_tree_sha256": "a" * 64},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="ascii",
                )
                os.chmod(provider_path, 0o444)
                os.chmod(payload, 0o555)
                write_active_manifest()
                with (
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=engine_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                    self.assertRaises(runtime.InstallerError),
                ):
                    runtime._runtime_provider_payload(
                        app_dir,
                        expected_manifest=engine_manifest,
                    )

    def _assert_protected_legacy_machine_lock_compatibility(self) -> None:
        source_sha = runtime.LEGACY_MACHINE_LOCK_SOURCE_SHA
        expected_manifest = {
            "version": 2,
            "engine_tree_sha256": "d" * 64,
            "node_version": "26.3.1",
            "package_lock_sha256": "e" * 64,
            "engine_files": {},
        }
        provider = {
            "version": 1,
            "source_sha": source_sha,
            "provider_sha": source_sha,
            **{
                key: expected_manifest[key]
                for key in (
                    "engine_tree_sha256",
                    "node_version",
                    "package_lock_sha256",
                    "engine_files",
                )
            },
        }
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                with mock.patch.object(
                    runtime,
                    "_validate_runtime_source",
                    return_value={"version": 1},
                ):
                    active_manifest = runtime.install(app_dir, release)
                payload = payload_root / source_sha
                lock_relative = "platform/tools/platform_live_qa_machine_lock.py"
                lock_path = payload / lock_relative
                self.assertTrue(lock_path.is_file())

                os.chmod(payload, 0o700)
                os.chmod(payload / "platform/tools", 0o700)
                lock_path.unlink()
                provider_path = payload / "runtime-provider.json"
                provider_path.write_text(
                    json.dumps(provider, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chown(provider_path, 0, 0)
                os.chmod(provider_path, 0o444)
                for directory in sorted(
                    (entry for entry in payload.rglob("*") if entry.is_dir()),
                    key=lambda entry: len(entry.parts),
                    reverse=True,
                ):
                    os.chown(directory, 0, 0)
                    os.chmod(directory, 0o555)
                os.chown(payload, 0, 0)
                os.chmod(payload, 0o555)

                def refresh_manifest() -> dict[str, object]:
                    tree_sha, file_map = runtime._tree_digest(payload)
                    updated = {
                        **active_manifest,
                        "payload_tree_sha256": tree_sha,
                        "files": file_map,
                    }
                    runtime._write_manifest(trusted / "active-manifest.json", updated)
                    return updated

                old_manifest = refresh_manifest()
                old_tree = runtime._tree_digest(payload)
                with mock.patch.object(
                    runtime, "_validate_runtime_source", return_value=expected_manifest
                ), mock.patch.object(runtime, "_validate_engine_root"):
                    with self.assertRaises(runtime.InstallerError):
                        runtime.verify(app_dir, source_sha)
                    reused = runtime._runtime_provider_payload(
                        app_dir,
                        expected_manifest=expected_manifest,
                    )
                self.assertEqual(reused, provider)
                self.assertEqual(runtime._tree_digest(payload), old_tree)

                # Every other required entrypoint remains mandatory.
                other_relative = "platform/tools/platform_live_qa_guard.py"
                other_path = payload / other_relative
                other_bytes = other_path.read_bytes()
                os.chmod(payload, 0o700)
                os.chmod(payload / "platform/tools", 0o700)
                other_path.unlink()
                os.chmod(payload / "platform/tools", 0o555)
                os.chmod(payload, 0o555)
                refresh_manifest()
                with mock.patch.object(
                    runtime, "_validate_runtime_source", return_value=expected_manifest
                ), mock.patch.object(runtime, "_validate_engine_root"):
                    with self.assertRaises(runtime.InstallerError):
                        runtime._runtime_provider_payload(
                            app_dir,
                            expected_manifest=expected_manifest,
                        )

                os.chmod(payload, 0o700)
                os.chmod(payload / "platform/tools", 0o700)
                other_path.write_bytes(other_bytes)
                os.chown(other_path, 0, 0)
                os.chmod(other_path, 0o444)
                os.chmod(payload / "platform/tools", 0o555)
                os.chmod(payload, 0o555)
                old_manifest = refresh_manifest()

                with (
                    mock.patch.object(runtime, "_protected_shas", return_value=set()),
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=expected_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                ):
                    with self.assertRaises(runtime.InstallerError):
                        runtime._runtime_provider_payload(
                            app_dir,
                            expected_manifest=expected_manifest,
                        )

                wrong_source_manifest = {
                    **old_manifest,
                    "source_sha": "8" * 40,
                    "payload": str(payload_root / ("8" * 40)),
                }
                with self.assertRaises(runtime.InstallerError):
                    runtime._validate_payload(wrong_source_manifest, app_dir=app_dir)

                # Reconciliation cannot reuse the malformed old generation as
                # its final payload: the default validator remains strict.
                with (
                    mock.patch.object(
                        runtime, "_validate_runtime_source", return_value=expected_manifest
                    ),
                    mock.patch.object(runtime, "_validate_engine_root"),
                ):
                    with self.assertRaises(runtime.InstallerError):
                        runtime._validate_payload(old_manifest, app_dir=app_dir)

    def test_m8_source_only_provider_may_omit_only_the_new_reporter(self) -> None:
        source_sha = runtime.LEGACY_RUNTIME_REPORTER_SOURCE_SHA
        provider_sha = "358c71bd49bd80111ea4a7b67745f881730ec714"
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            with self.canonical_runtime_tree(root, source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                # The canonical installer fixture supplies the trusted M8
                # entrypoints and active manifest. Build the actual legacy
                # source-only runtime and prior engine provider beneath those
                # payload paths; validation below uses production validators.
                active_manifest = runtime.install(app_dir, release)
                active_payload = payload_root / source_sha
                legacy_runtime = active_payload / "runtime"
                os.chmod(active_payload, 0o700)
                os.chmod(legacy_runtime, 0o700)

                previous_release = app_dir / "releases" / f"release-{provider_sha[:8]}"
                previous_release.mkdir(mode=0o755)
                (previous_release / "RELEASE.json").write_text(
                    json.dumps(
                        {
                            "source_git_commit": provider_sha,
                            "release_slug": previous_release.name,
                        }
                    )
                    + "\n",
                    encoding="ascii",
                )
                os.chmod(previous_release / "RELEASE.json", 0o444)
                (app_dir / "previous").symlink_to(previous_release)

                engine_root, engine_files, lock_sha = self._engine_provider_fixture(
                    payload_root / provider_sha
                )
                sandbox_path = engine_root / runtime.RUNTIME_SANDBOX_RELATIVE
                sandbox_sha = runtime._digest_regular(sandbox_path, allow_sandbox=True)
                with mock.patch.multiple(
                    runtime,
                    CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                    CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                ):
                    engine_sha = runtime._engine_digest(
                        engine_files,
                        node_version="26.3.1",
                        package_lock_sha256=lock_sha,
                    )
                    prior_payload = payload_root / provider_sha
                    prior_provider = {
                        "version": 1,
                        "source_sha": provider_sha,
                        "provider_sha": provider_sha,
                        "engine_tree_sha256": engine_sha,
                        "node_version": "26.3.1",
                        "package_lock_sha256": lock_sha,
                        "engine_files": engine_files,
                    }
                    prior_provider_path = prior_payload / "runtime-provider.json"
                    prior_provider_path.write_text(
                        json.dumps(prior_provider, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(prior_provider_path, 0, 0)
                    os.chmod(prior_provider_path, 0o444)
                    os.chown(prior_payload, 0, 0)
                    os.chmod(prior_payload, 0o555)
                    suite_files: dict[str, str] = {}
                    suite_bytes = {
                        "web/package-lock.json": b"lock\n",
                        "web/playwright.live.config.ts": b"export default {};\n",
                        "web/tests/smoke/live-launch.spec.ts": b"export {};\n",
                        "web/tests/smoke/live-user-journey.spec.ts": b"export {};\n",
                        "web/tests/support/live-qa-origin.ts": b"export {};\n",
                        "web/tests/support/live-qa-sandbox.ts": b"export {};\n",
                    }
                    for relative, content in suite_bytes.items():
                        destination = legacy_runtime / relative
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        destination.write_bytes(content)
                        os.chown(destination, 0, 0)
                        os.chmod(destination, 0o444)
                    for directory in sorted(
                        (path for path in legacy_runtime.rglob("*") if path.is_dir()),
                        key=lambda path: len(path.parts),
                        reverse=True,
                    ):
                        os.chown(directory, 0, 0)
                        os.chmod(directory, 0o555)
                    os.chown(legacy_runtime, 0, 0)
                    os.chmod(legacy_runtime, 0o555)
                    suite_files = runtime._runtime_file_map(
                        legacy_runtime,
                        prefixes=("web/",),
                        excluded_prefixes=("web/node_modules/",),
                    )
                    legacy_runtime_manifest = {
                        "version": 2,
                        "node_version": "26.3.1",
                        "package_lock_sha256": runtime._digest_regular(
                            legacy_runtime / "web/package-lock.json"
                        ),
                        "engine_tree_sha256": engine_sha,
                        "engine_files": engine_files,
                        "suite_tree_sha256": runtime._file_map_digest(
                            suite_files, domain=b"oldsparky-liveqa-suite-v1\0"
                        ),
                        "suite_files": suite_files,
                    }
                    manifest_path = legacy_runtime / runtime.RUNTIME_MANIFEST_RELATIVE
                    manifest_path.write_text(
                        json.dumps(
                            legacy_runtime_manifest,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(manifest_path, 0, 0)
                    os.chmod(manifest_path, 0o444)

                    provider = {
                        "version": 1,
                        "source_sha": source_sha,
                        "provider_sha": provider_sha,
                        "engine_tree_sha256": engine_sha,
                        "node_version": "26.3.1",
                        "package_lock_sha256": legacy_runtime_manifest[
                            "package_lock_sha256"
                        ],
                        "engine_files": engine_files,
                    }
                    provider_path = active_payload / "runtime-provider.json"
                    provider_path.write_text(
                        json.dumps(provider, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(provider_path, 0, 0)
                    os.chmod(provider_path, 0o444)
                    for directory in sorted(
                        (path for path in active_payload.rglob("*") if path.is_dir()),
                        key=lambda path: len(path.parts),
                        reverse=True,
                    ):
                        os.chown(directory, 0, 0)
                        os.chmod(directory, 0o555)
                    os.chown(active_payload, 0, 0)
                    os.chmod(active_payload, 0o555)
                    tree_sha, files = runtime._tree_digest(active_payload)
                    active_manifest = {
                        **active_manifest,
                        "payload_tree_sha256": tree_sha,
                        "files": files,
                    }
                    runtime._write_manifest(trusted / "active-manifest.json", active_manifest)

                    self.assertEqual(provider["source_sha"], source_sha)
                    self.assertEqual(provider["engine_tree_sha256"], engine_sha)
                    self.assertEqual(provider["node_version"], legacy_runtime_manifest["node_version"])
                    self.assertEqual(
                        provider["package_lock_sha256"],
                        legacy_runtime_manifest["package_lock_sha256"],
                    )
                    self.assertEqual(provider["engine_files"], engine_files)
                    real_validate_runtime_source = ORIGINAL_VALIDATE_RUNTIME_SOURCE
                    with mock.patch.object(
                        runtime, "_validate_runtime_source", wraps=real_validate_runtime_source
                    ):
                        # This is the real prior-provider call path. It must
                        # validate the suite, provider, protected release,
                        # engine bytes and payload closure without mocking the
                        # runtime validator.
                        reused = runtime._runtime_provider_payload(
                            app_dir,
                            expected_manifest=legacy_runtime_manifest,
                        )
                    self.assertEqual(reused, provider)

                    release_runtime = release / "liveqa-runtime"
                    shutil.rmtree(release_runtime)
                    shutil.copytree(legacy_runtime, release_runtime, copy_function=shutil.copy2)

                    # Recreate the post-pointer-switch rollback topology and
                    # validate the actual transaction status through the
                    # immutable installer path recorded as current_before.
                    rollback_origin_sha = "f" * 40
                    rollback_origin = app_dir / "releases" / f"release-{rollback_origin_sha[:8]}"
                    rollback_tools = rollback_origin / "tools"
                    rollback_tools.mkdir(mode=0o755, parents=True)
                    (rollback_origin / "RELEASE.json").write_text(
                        json.dumps(
                            {
                                "source_git_commit": rollback_origin_sha,
                                "release_slug": rollback_origin.name,
                            }
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    os.chmod(rollback_origin / "RELEASE.json", 0o444)
                    rollback_installer = rollback_tools / "platform_live_qa_runtime_install.py"
                    rollback_installer.write_bytes(SCRIPT.read_bytes())
                    os.chmod(rollback_installer, 0o755)
                    transaction_tool = rollback_tools / "platform_release_transaction.py"
                    transaction_source = SCRIPT.with_name("platform_release_transaction.py")
                    transaction_tool.write_bytes(transaction_source.read_bytes())
                    os.chmod(transaction_tool, 0o755)
                    transaction_systemd_state = SCRIPT.with_name(
                        "platform_release_systemd_state.py"
                    )
                    transaction_systemd_tool = rollback_tools / "platform_release_systemd_state.py"
                    transaction_systemd_tool.write_bytes(transaction_systemd_state.read_bytes())
                    os.chmod(transaction_systemd_tool, 0o755)
                    current_pointer = app_dir / "current"
                    previous_pointer = app_dir / "previous"
                    current_pointer.unlink()
                    current_pointer.symlink_to(rollback_origin)
                    previous_pointer.unlink()
                    previous_pointer.symlink_to(release)
                    shared = app_dir / "shared"
                    shared.mkdir(mode=0o700)
                    state = shared / ".release-operation.json"
                    snapshot = rollback_origin / ".rollback/shared-venv-before-install"
                    create_args = [
                        sys.executable,
                        "-I",
                        str(transaction_tool),
                        "create",
                        "--state",
                        str(state),
                        "--operation",
                        "rollback",
                        "--app-dir",
                        str(app_dir),
                        "--current-before",
                        str(rollback_origin),
                        "--previous-before",
                        str(release),
                        "--candidate-release",
                        str(rollback_origin),
                        "--shared-venv",
                        str(shared / "venv"),
                        "--peer",
                        str(snapshot),
                        "--snapshot",
                        str(snapshot),
                        "--transition",
                        "none",
                    ]
                    created = subprocess.run(
                        create_args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(created.returncode, 0, created.stderr)

                    def run_transaction(*arguments: str) -> subprocess.CompletedProcess[str]:
                        return subprocess.run(
                            [sys.executable, "-I", str(transaction_tool), *arguments],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            check=False,
                            text=True,
                        )
                    result = run_transaction(
                        "phase", "--state", str(state), "--expected", "prepared",
                        "--phase", "venv-transitioned",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "switch-pointer", "--state", str(state), "--name", "current",
                        "--target", str(release),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "phase", "--state", str(state), "--expected", "venv-transitioned",
                        "--phase", "current-switched",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction(
                        "switch-pointer", "--state", str(state), "--name", "previous",
                        "--target", str(rollback_origin),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for expected, next_phase in (
                        ("current-switched", "pointers-switched"),
                        ("pointers-switched", "rollback-runtime-pending"),
                    ):
                        result = run_transaction(
                            "phase", "--state", str(state), "--expected", expected,
                            "--phase", next_phase,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                    with mock.patch.object(runtime, "__file__", str(rollback_installer)):
                        self.assertTrue(
                            runtime._rollback_reconcile_is_authorized(
                                app_dir, state, release
                            )
                        )
                        with (
                            mock.patch.object(
                                runtime,
                                "_validate_runtime_source",
                                wraps=real_validate_runtime_source,
                            ),
                            mock.patch.multiple(
                                runtime,
                                CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                            ),
                        ):
                            migration_reconcile = runtime.reconcile(
                                app_dir, rollback_transaction=state
                            )
                            self.assertEqual(
                                migration_reconcile["source_sha"], source_sha
                            )

                    # A byte-identical installer copied from a different
                    # release is not authorized by the transaction receipt.
                    decoy = app_dir / "releases" / f"release-{'e' * 8}"
                    decoy_tools = decoy / "tools"
                    decoy_tools.mkdir(mode=0o755, parents=True)
                    (decoy / "RELEASE.json").write_text(
                        json.dumps(
                            {
                                "source_git_commit": "e" * 40,
                                "release_slug": decoy.name,
                            }
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    os.chmod(decoy / "RELEASE.json", 0o444)
                    decoy_installer = decoy_tools / "platform_live_qa_runtime_install.py"
                    decoy_installer.write_bytes(SCRIPT.read_bytes())
                    os.chmod(decoy_installer, 0o755)
                    decoy_transaction_tool = decoy_tools / "platform_release_transaction.py"
                    decoy_transaction_tool.write_bytes(transaction_source.read_bytes())
                    os.chmod(decoy_transaction_tool, 0o755)
                    decoy_systemd_tool = decoy_tools / "platform_release_systemd_state.py"
                    decoy_systemd_tool.write_bytes(transaction_systemd_state.read_bytes())
                    os.chmod(decoy_systemd_tool, 0o755)
                    with (
                        mock.patch.object(runtime, "__file__", str(decoy_installer)),
                        self.assertRaisesRegex(
                            runtime.InstallerError,
                            "rollback runtime transaction release binding is invalid",
                        ),
                    ):
                        runtime._rollback_reconcile_is_authorized(app_dir, state, release)

                    # The same F installer may reconcile the exact old M8
                    # runtime before pointer switching only during migration
                    # uncertainty, or after transaction-bound recovery has
                    # restored the original pointer pair.
                    current_pointer.unlink()
                    current_pointer.symlink_to(release)
                    previous_pointer.unlink()
                    previous_pointer.symlink_to(previous_release)
                    state.unlink()
                    install_create_args = [
                        sys.executable,
                        "-I",
                        str(transaction_tool),
                        "create",
                        "--state",
                        str(state),
                        "--operation",
                        "install",
                        "--app-dir",
                        str(app_dir),
                        "--current-before",
                        str(release),
                        "--previous-before",
                        str(previous_release),
                        "--candidate-release",
                        str(rollback_origin),
                        "--shared-venv",
                        str(shared / "venv"),
                        "--peer",
                        str(shared / f".venv-install-{rollback_origin.name}.fixture"),
                        "--snapshot",
                        str(snapshot),
                        "--transition",
                        "none",
                    ]
                    rollback_metadata = rollback_origin / ".rollback"
                    rollback_metadata.mkdir(mode=0o700)
                    (rollback_metadata / "previous-release").write_text(
                        f"{release}\n", encoding="ascii"
                    )
                    (rollback_metadata / "previous-release").chmod(0o600)
                    (rollback_metadata / "venv-transition").write_text(
                        "unchanged\n", encoding="ascii"
                    )
                    (rollback_metadata / "venv-transition").chmod(0o600)
                    freeze = rollback_origin / "requirements-platform.freeze.txt"
                    freeze.write_text("fixture-package==1\n", encoding="ascii")
                    freeze.chmod(0o444)
                    (rollback_metadata / "shared-freeze.sha256").write_text(
                        hashlib.sha256(freeze.read_bytes()).hexdigest() + "\n",
                        encoding="ascii",
                    )
                    (rollback_metadata / "shared-freeze.sha256").chmod(0o600)
                    created = subprocess.run(
                        install_create_args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(created.returncode, 0, created.stderr)
                    for expected, next_phase in (
                        ("prepared", "venv-transitioned"),
                        ("venv-transitioned", "snapshot-placed"),
                        ("snapshot-placed", "staged"),
                        ("staged", "migration-pending"),
                    ):
                        result = run_transaction(
                            "phase",
                            "--state",
                            str(state),
                            "--expected",
                            expected,
                            "--phase",
                            next_phase,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                    # During migration the original M8/M6 pointers are still
                    # active. This exact retained install transaction is the
                    # deploy recovery path that restores M8 using the incoming
                    # release's immutable installer.
                    with mock.patch.object(runtime, "__file__", str(rollback_installer)):
                        self.assertTrue(
                            runtime._rollback_reconcile_is_authorized(
                                app_dir, state, release
                            )
                        )
                        with self.assertRaisesRegex(
                            runtime.InstallerError,
                            "rollback runtime transaction release binding is invalid",
                        ):
                            runtime._rollback_reconcile_is_authorized(
                                app_dir, state, previous_release
                            )
                        with (
                            mock.patch.object(
                                runtime,
                                "_validate_runtime_source",
                                wraps=real_validate_runtime_source,
                            ),
                            mock.patch.multiple(
                                runtime,
                                CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                                CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                            ),
                        ):
                            migration_reconcile = runtime.reconcile(
                                app_dir, rollback_transaction=state
                            )
                            self.assertEqual(
                                migration_reconcile["source_sha"], source_sha
                            )
                    result = run_transaction(
                        "phase",
                        "--state",
                        str(state),
                        "--expected",
                        "migration-pending",
                        "--phase",
                        "migration-failed",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    with mock.patch.object(runtime, "__file__", str(rollback_installer)):
                        self.assertTrue(
                            runtime._rollback_reconcile_is_authorized(
                                app_dir, state, release
                            )
                        )
                    result = run_transaction(
                        "phase",
                        "--state",
                        str(state),
                        "--expected",
                        "migration-failed",
                        "--phase",
                        "recovery-authorized",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = run_transaction("recover", "--retain", "--state", str(state))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recovered = run_transaction("status", "--state", str(state), "--json")
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertEqual(json.loads(recovered.stdout)["phase"], "recovery-restored")
                    with (
                        mock.patch.object(runtime, "__file__", str(rollback_installer)),
                        mock.patch.object(
                            runtime,
                            "_validate_runtime_source",
                            wraps=real_validate_runtime_source,
                        ) as validate_runtime_source,
                        mock.patch.multiple(
                            runtime,
                            CHROMIUM_SANDBOX_SIZE=sandbox_path.stat().st_size,
                            CHROMIUM_SANDBOX_SHA256=sandbox_sha,
                        ),
                    ):
                        # Exercise the entire authorized rollback path through
                        # source validation, old-provider validation, stage,
                        # publication/reuse and the final payload validator.
                        reconciled = runtime.reconcile(
                            app_dir, rollback_transaction=state
                        )
                        self.assertEqual(reconciled["source_sha"], source_sha)
                        self.assertTrue(validate_runtime_source.called)
                        with self.assertRaisesRegex(
                            runtime.InstallerError,
                            "live-QA install path is unavailable",
                        ):
                            runtime.reconcile(app_dir)

                    with self.assertRaisesRegex(
                        runtime.InstallerError, "live-QA install path is unavailable"
                    ):
                        real_validate_runtime_source(legacy_runtime)

                    # The source-hash exception is specifically for the M8
                    # source-only v2 contract, not an older v1 runtime format.
                    runtime_manifest_path = legacy_runtime / runtime.RUNTIME_MANIFEST_RELATIVE
                    runtime_manifest_path.chmod(0o644)
                    legacy_tree_sha, legacy_files = runtime._tree_digest(
                        legacy_runtime,
                        ignored=frozenset({runtime.RUNTIME_MANIFEST_RELATIVE.as_posix()}),
                        sandbox_relative=runtime.RUNTIME_SANDBOX_RELATIVE,
                    )
                    v1_manifest = {
                        "version": 1,
                        "node_version": "26.3.1",
                        "package_lock_sha256": runtime._digest_regular(
                            legacy_runtime / "web/package-lock.json"
                        ),
                        "tree_sha256": legacy_tree_sha,
                        "files": legacy_files,
                    }
                    runtime_manifest_path.write_text(
                        json.dumps(v1_manifest, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(runtime_manifest_path, 0, 0)
                    runtime_manifest_path.chmod(0o444)
                    with self.assertRaisesRegex(
                        runtime.InstallerError,
                        "legacy reporter omission requires a source-only runtime",
                    ):
                        real_validate_runtime_source(
                            legacy_runtime,
                            allow_legacy_reporter_omission=True,
                            source_sha=source_sha,
                        )
                    runtime_manifest_path.chmod(0o644)
                    runtime_manifest_path.write_text(
                        json.dumps(
                            legacy_runtime_manifest,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(runtime_manifest_path, 0, 0)
                    runtime_manifest_path.chmod(0o444)

                    # A second, unlisted suite member cannot be smuggled in
                    # by recomputing the payload's outer digest.
                    os.chmod(active_payload, 0o700)
                    os.chmod(legacy_runtime, 0o700)
                    reporter = legacy_runtime / "web/tests/support/live-count-reporter.cjs"
                    reporter.write_text("// one-sided addition\n", encoding="ascii")
                    os.chown(reporter, 0, 0)
                    os.chmod(reporter, 0o444)
                    os.chmod(legacy_runtime, 0o555)
                    os.chmod(active_payload, 0o555)
                    tree_sha, files = runtime._tree_digest(active_payload)
                    changed_manifest = {**active_manifest, "payload_tree_sha256": tree_sha, "files": files}
                    runtime._write_manifest(trusted / "active-manifest.json", changed_manifest)
                    with (
                        mock.patch.object(
                            runtime,
                            "_validate_runtime_source",
                            wraps=real_validate_runtime_source,
                        ),
                        self.assertRaisesRegex(
                            runtime.InstallerError,
                            "live-QA runtime contains an unreviewed web member",
                        ),
                    ):
                        runtime._runtime_provider_payload(app_dir, expected_manifest=legacy_runtime_manifest)

                    # Missing an established M8 suite file is rejected even
                    # though the reporter omission itself is permitted.
                    os.chmod(active_payload, 0o700)
                    os.chmod(legacy_runtime, 0o700)
                    runtime_manifest_path.chmod(0o644)
                    runtime_manifest = json.loads(
                        runtime_manifest_path.read_text(encoding="ascii")
                    )
                    runtime_manifest["suite_files"] = runtime._runtime_file_map(
                        legacy_runtime,
                        prefixes=("web/",),
                        excluded_prefixes=("web/node_modules/",),
                    )
                    runtime_manifest["suite_tree_sha256"] = runtime._file_map_digest(
                        runtime_manifest["suite_files"],
                        domain=b"oldsparky-liveqa-suite-v1\0",
                    )
                    missing_member = legacy_runtime / "web/tests/support/live-qa-origin.ts"
                    missing_member.unlink()
                    runtime_manifest["suite_files"] = runtime._runtime_file_map(
                        legacy_runtime,
                        prefixes=("web/",),
                        excluded_prefixes=("web/node_modules/",),
                    )
                    runtime_manifest["suite_tree_sha256"] = runtime._file_map_digest(
                        runtime_manifest["suite_files"],
                        domain=b"oldsparky-liveqa-suite-v1\0",
                    )
                    runtime_manifest_path.write_text(
                        json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(runtime_manifest_path, 0, 0)
                    runtime_manifest_path.chmod(0o444)
                    for directory in sorted(
                        (path for path in legacy_runtime.rglob("*") if path.is_dir()),
                        key=lambda path: len(path.parts),
                        reverse=True,
                    ):
                        os.chmod(directory, 0o555)
                    os.chmod(legacy_runtime, 0o555)
                    os.chmod(active_payload, 0o555)
                    tree_sha, files = runtime._tree_digest(active_payload)
                    changed_manifest = {**active_manifest, "payload_tree_sha256": tree_sha, "files": files}
                    runtime._write_manifest(trusted / "active-manifest.json", changed_manifest)
                    with (
                        mock.patch.object(
                            runtime,
                            "_validate_runtime_source",
                            wraps=real_validate_runtime_source,
                        ),
                        self.assertRaisesRegex(
                            runtime.InstallerError, "live-QA install path is unavailable"
                        ),
                    ):
                        runtime._runtime_provider_payload(app_dir, expected_manifest=legacy_runtime_manifest)

                    # Manifest-only presence is rejected by the same exact
                    # suite-file/tree equality check.
                    os.chmod(active_payload, 0o700)
                    os.chmod(legacy_runtime, 0o700)
                    reporter.unlink()
                    runtime_manifest_path = legacy_runtime / runtime.RUNTIME_MANIFEST_RELATIVE
                    runtime_manifest_path.chmod(0o644)
                    runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="ascii"))
                    runtime_manifest["suite_files"][
                        "web/tests/support/live-count-reporter.cjs"
                    ] = "0" * 64
                    runtime_manifest["suite_tree_sha256"] = runtime._file_map_digest(
                        runtime_manifest["suite_files"], domain=b"oldsparky-liveqa-suite-v1\0"
                    )
                    runtime_manifest_path.write_text(
                        json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="ascii",
                    )
                    os.chown(runtime_manifest_path, 0, 0)
                    runtime_manifest_path.chmod(0o444)
                    os.chmod(legacy_runtime, 0o555)
                    os.chmod(active_payload, 0o555)
                    tree_sha, files = runtime._tree_digest(active_payload)
                    changed_manifest = {**active_manifest, "payload_tree_sha256": tree_sha, "files": files}
                    runtime._write_manifest(trusted / "active-manifest.json", changed_manifest)
                    with (
                        mock.patch.object(
                            runtime,
                            "_validate_runtime_source",
                            wraps=real_validate_runtime_source,
                        ),
                        self.assertRaisesRegex(
                            runtime.InstallerError,
                            "live-QA install path is unavailable",
                        ),
                    ):
                        runtime._runtime_provider_payload(app_dir, expected_manifest=legacy_runtime_manifest)
    def test_install_rejects_tampered_staged_validator_before_activation(self) -> None:
        source_sha = "c" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                copy_regular = runtime._copy_regular

                def tamper_staged_validator(
                    source: Path, destination: Path, **kwargs: object
                ) -> str:
                    digest = copy_regular(source, destination, **kwargs)
                    if (
                        source.name == "platform_validate_release_artifact.py"
                        and destination.parent.name == "tools"
                    ):
                        destination.chmod(0o755)
                        destination.write_bytes(b"# tampered staged validator\n")
                        destination.chmod(0o555)
                    return digest

                with mock.patch.object(
                    runtime, "_copy_regular", side_effect=tamper_staged_validator
                ), self.assertRaises(runtime.InstallerError):
                    runtime.install(app_dir, release)
                self.assertFalse((trusted / "active").exists())
                self.assertFalse((trusted / "active-manifest.json").exists())
                self.assertFalse((payload_root / source_sha).exists())

    def test_postpromotion_retention_failure_reports_closed_stage_and_keeps_pointer(self) -> None:
        source_sha = "e" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                stderr = io.StringIO()
                with mock.patch.object(
                    runtime,
                    "_retention",
                    side_effect=runtime.InstallerError("private diagnostic detail"),
                ), redirect_stderr(stderr), self.assertRaises(runtime.InstallerError):
                    runtime.install(app_dir, release)

                diagnostic = stderr.getvalue()
                self.assertIn(
                    "LIVE_QA_INSTALL_STAGE stage=retention status=failed ",
                    diagnostic,
                )
                self.assertNotIn("private diagnostic detail", diagnostic)
                self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
                manifest = json.loads((trusted / "active-manifest.json").read_text(encoding="ascii"))
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertTrue((payload_root / source_sha).is_dir())

    def test_install_repairs_only_legacy_private_payload_root_mode(self) -> None:
        source_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o700)
                previous_umask = os.umask(0o077)
                try:
                    runtime.install(app_dir, release)
                finally:
                    os.umask(previous_umask)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)

    def test_install_preserves_canonical_payload_root_mode(self) -> None:
        source_sha = "d" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o755)
                payload_root.chmod(0o755)
                runtime.install(app_dir, release)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)

    def test_install_rejects_unsupported_payload_root_metadata_before_cleanup(self) -> None:
        source_sha = "c" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, _trusted, payload_root):
                _trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o750)
                payload_root.chmod(0o750)
                with mock.patch.object(runtime, "_cleanup_temporary_files") as cleanup, self.assertRaisesRegex(
                    runtime.InstallerError,
                    "payload root mode is unsupported",
                ):
                    runtime.install(app_dir, release)
                cleanup.assert_not_called()
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o750)

                payload_root.rmdir()
                target = Path(temporary) / "outside-releases"
                target.mkdir(mode=0o755)
                payload_root.symlink_to(target)
                with self.assertRaisesRegex(runtime.InstallerError, "payload root is unavailable"):
                    runtime.install(app_dir, release)
                payload_root.unlink()

                payload_root.mkdir(mode=0o755)
                payload_root.chmod(0o755)
                os.chown(payload_root, 65534, 65534)
                with self.assertRaisesRegex(runtime.InstallerError, "payload root metadata is unsafe"):
                    runtime.install(app_dir, release)

    def test_verify_rejects_a_crash_left_noncanonical_pointer(self) -> None:
        source_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, _payload_root):
                runtime.install(app_dir, release)
                active = trusted / "active"
                active.unlink()
                active.symlink_to(source_sha)
                with self.assertRaisesRegex(runtime.InstallerError, "active generation pointer"):
                    runtime.verify(app_dir, source_sha)

    def test_reconcile_repairs_a_crash_pointer_and_verify_accepts(self) -> None:
        source_sha = "c" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                runtime.install(app_dir, release)
                active = trusted / "active"
                active.unlink()
                active.symlink_to(source_sha)
                manifest = runtime.reconcile(app_dir)
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertEqual(os.readlink(active), f"releases/{source_sha}")
                self.assertEqual(active.resolve(), payload_root / source_sha)
                verified = runtime.verify(app_dir, source_sha)
                self.assertEqual(verified["source_sha"], source_sha)

    def test_active_manifest_between_legacy_and_active_bounds_is_accepted(self) -> None:
        source_sha = "d" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                runtime.install(app_dir, release)
                manifest_path = trusted / "active-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                manifest["files"].update(
                    {
                        f"manifest-padding-{index:04d}-{'x' * 70}": "0" * 64
                        for index in range(900)
                    }
                )
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chmod(manifest_path, 0o444)
                self.assertGreater(manifest_path.stat().st_size, 64 * 1024)
                self.assertLessEqual(
                    manifest_path.stat().st_size,
                    runtime.MAX_ACTIVE_MANIFEST_BYTES,
                )
                self.assertEqual(runtime._read_manifest()["source_sha"], source_sha)

    def test_active_manifest_over_active_bound_is_rejected(self) -> None:
        source_sha = "e" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                runtime.install(app_dir, release)
                manifest_path = trusted / "active-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                manifest["files"].update(
                    {
                        f"manifest-padding-{index:04d}-{'x' * 70}": "0" * 64
                        for index in range(3200)
                    }
                )
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chmod(manifest_path, 0o444)
                self.assertGreater(
                    manifest_path.stat().st_size,
                    runtime.MAX_ACTIVE_MANIFEST_BYTES,
                )
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "metadata is unsafe|size bound",
                ):
                    runtime._read_manifest()

    def test_release_json_reader_keeps_the_canonical_64k_bound(self) -> None:
        source_sha = "f" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                _trusted,
                _payload_root,
            ):
                release_json = release / "RELEASE.json"
                release_json.write_text(
                    json.dumps(
                        {
                            "source_git_commit": source_sha,
                            "release_slug": release.name,
                            "padding": "x" * (runtime.MAX_RELEASE_JSON_BYTES + 1),
                        }
                    ),
                    encoding="ascii",
                )
                os.chmod(release_json, 0o444)
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "metadata is unsafe|size bound",
                ):
                    runtime._safe_release(app_dir, release)

    def test_active_manifest_reader_rejects_symlink_hardlink_and_wrong_mode(self) -> None:
        cases = ("symlink", "hardlink", "mode")
        for case in cases:
            with self.subTest(case=case):
                source_sha = ("1" if case == "symlink" else "2" if case == "hardlink" else "3") * 40
                with tempfile.TemporaryDirectory(dir="/root") as temporary:
                    with self.runtime_tree(Path(temporary), source_sha) as (
                        app_dir,
                        release,
                        trusted,
                        _payload_root,
                    ):
                        runtime.install(app_dir, release)
                        manifest_path = trusted / "active-manifest.json"
                        if case == "symlink":
                            target = trusted / "manifest-target.json"
                            target.write_bytes(manifest_path.read_bytes())
                            os.chmod(target, 0o444)
                            manifest_path.unlink()
                            manifest_path.symlink_to(target)
                        elif case == "hardlink":
                            os.link(manifest_path, trusted / "manifest-second-link")
                        else:
                            os.chmod(manifest_path, 0o644)
                        with self.assertRaisesRegex(
                            runtime.InstallerError,
                            "metadata is unsafe",
                        ):
                            runtime._read_manifest()

    def test_cleanup_staging_accepts_real_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            stage = payload_root / f".{('a' * 40)}.install-{('b' * 32)}"
            nested = stage / "runtime" / "browsers" / "chromium"
            nested.mkdir(mode=0o700, parents=True)
            (nested / "node").write_bytes(b"nested-runtime")
            for directory in (stage, stage / "runtime", stage / "runtime" / "browsers", nested):
                os.chmod(directory, 0o700)
            os.chmod(nested / "node", 0o600)

            with mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root):
                self.assertEqual(runtime._cleanup_staging(apply=True), 1)
            self.assertFalse(stage.exists())

    def test_cleanup_staging_rejects_real_nested_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            stage = payload_root / f".{('c' * 40)}.install-{('d' * 32)}"
            nested = stage / "runtime" / "browsers"
            nested.mkdir(mode=0o700, parents=True)
            source = nested / "node"
            source.write_bytes(b"hard-linked")
            os.link(source, nested / "node-alias")
            for directory in (stage, stage / "runtime", nested):
                os.chmod(directory, 0o700)
            os.chmod(source, 0o600)
            os.chmod(nested / "node-alias", 0o600)

            with mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root):
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "link count is unsafe",
                ):
                    runtime._cleanup_staging(apply=True)
            self.assertTrue(stage.exists())

    @staticmethod
    def _make_retention_entry(root: Path, source_sha: str, mtime: int, *, hardlink: bool = False) -> Path:
        entry = root / source_sha
        nested = entry / "runtime" / "browsers"
        nested.mkdir(mode=0o555, parents=True)
        source = nested / "node"
        source.write_bytes(b"retained-runtime")
        os.chmod(source, 0o444)
        if hardlink:
            os.link(source, nested / "node-alias")
            os.chmod(nested / "node-alias", 0o444)
        for directory in (entry, entry / "runtime", nested):
            os.chmod(directory, 0o555)
        os.utime(entry, (mtime, mtime))
        return entry

    def test_retention_accepts_real_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            old = self._make_retention_entry(payload_root, "e" * 40, 1)
            newest = self._make_retention_entry(payload_root, "f" * 40, 2)

            with (
                mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root),
                mock.patch.object(runtime, "ACTIVE_POINTER", root / "active"),
            ):
                self.assertEqual(runtime._retention(root / "app", apply=False), 2)
                self.assertTrue(old.exists())
                self.assertTrue(newest.exists())
                self.assertEqual(runtime._retention(root / "app", apply=True), 2)
            self.assertFalse(old.exists())
            self.assertFalse(newest.exists())

    def test_retention_rejects_real_nested_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            old = self._make_retention_entry(payload_root, "1" * 40, 1, hardlink=True)
            self._make_retention_entry(payload_root, "2" * 40, 2)

            with (
                mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root),
                mock.patch.object(runtime, "ACTIVE_POINTER", root / "active"),
            ):
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "link count is unsafe",
                ):
                    runtime._retention(root / "app", apply=True)
            self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
