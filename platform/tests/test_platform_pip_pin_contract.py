from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import platform_verify as verifier
from tools import platform_validate_wheelhouse as validator
from tools.platform_verify_contract import _workflow_step_blocks


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = REPO_ROOT / "platform"
CI_LOCK = PLATFORM_ROOT / "requirements-ci.lock.txt"
CI_LOCKER = PLATFORM_ROOT / "requirements-ci-locker.lock.txt"
CI_METADATA = PLATFORM_ROOT / "requirements-ci.lock.meta.json"
CI_INPUT = PLATFORM_ROOT / "requirements-ci.in"
SECURITY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security.yml"
CI_INSTALLER = PLATFORM_ROOT / "tools/platform_install_ci_python.sh"
CI_LOCK_GENERATOR = PLATFORM_ROOT / "tools/platform_generate_ci_lock.sh"
CI_ENV_WRAPPER = PLATFORM_ROOT / "tools/platform_ci_pip_env.sh"
LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)"
    r" --hash=sha256:(?P<hash>[0-9a-f]{64})$"
)

EXPECTED_SECURITY_DEPENDENCY_LOCKS = (
    "requirements-platform.lock.txt",
    "requirements-ci.lock.txt",
    "requirements-ci-locker.lock.txt",
    "apps/platform_draft/requirements-assets.lock.txt",
)
EXPECTED_PIP_AUDIT_FLAGS = (
    "--disable-pip",
    "--require-hashes",
    "--strict",
    "--format",
    "columns",
    "--progress-spinner",
    "off",
    "--timeout",
    "10",
)


def _normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement, version = line.split("==", 1)
        name = _normalise_name(requirement.split("[", 1)[0])
        if name in pins:
            raise AssertionError(f"duplicate direct pin: {name}")
        pins[name] = version
    return pins


def _ci_lock_pins(path: Path = CI_LOCK) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines != sorted(lines):
        raise AssertionError("CI lock is not canonically sorted")
    pins: dict[str, str] = {}
    for line in lines:
        match = LOCK_LINE.fullmatch(line)
        if match is None:
            raise AssertionError(f"CI lock line is not exact and hashed: {line}")
        name = _normalise_name(match.group("name"))
        if name in pins:
            raise AssertionError(f"duplicate CI lock package: {name}")
        pins[name] = match.group("version")
    return pins


class PlatformPipPinContractTests(unittest.TestCase):
    def test_security_dependency_audit_manifest_and_flags_are_exact(self) -> None:
        self.assertEqual(verifier.SECURITY_DEPENDENCY_LOCKS, EXPECTED_SECURITY_DEPENDENCY_LOCKS)
        self.assertEqual(verifier.PIP_AUDIT_FLAGS, EXPECTED_PIP_AUDIT_FLAGS)
        self.assertEqual(verifier.SECURITY_DEPENDENCY_LOCK_MAX_BYTES, 1024 * 1024)

        command_calls: list[tuple[str, list[str], dict[str, object]]] = []
        snapshot_evidence: list[tuple[Path, bytes, int, int]] = []

        def fake_run(
            label: str,
            command: list[str],
            **kwargs: object,
        ) -> int:
            command_calls.append((label, command, kwargs))
            snapshot = Path(command[4])
            snapshot_evidence.append(
                (
                    snapshot,
                    snapshot.read_bytes(),
                    stat.S_IMODE(snapshot.stat().st_mode),
                    stat.S_IMODE(snapshot.parent.stat().st_mode),
                )
            )
            return 0

        with mock.patch.object(verifier, "_run", side_effect=fake_run):
            self.assertEqual(verifier._run_security_dependency_audits(), 0)

        self.assertEqual(len(command_calls), len(EXPECTED_SECURITY_DEPENDENCY_LOCKS))
        for index, lock_path in enumerate(EXPECTED_SECURITY_DEPENDENCY_LOCKS):
            label, command, kwargs = command_calls[index]
            self.assertEqual(label, f"security/dependency-audit/{lock_path}")
            self.assertEqual(command[1:4], ["-m", "pip_audit", "-r"])
            snapshot = Path(command[4])
            self.assertNotEqual(snapshot, PLATFORM_ROOT / lock_path)
            _, snapshot_bytes, snapshot_mode, directory_mode = snapshot_evidence[index]
            self.assertEqual(snapshot_bytes, (PLATFORM_ROOT / lock_path).read_bytes())
            self.assertEqual(snapshot_mode, 0o400)
            self.assertEqual(directory_mode, 0o700)
            self.assertEqual(command[5:], list(EXPECTED_PIP_AUDIT_FLAGS))
            self.assertEqual(kwargs, {"timeout_seconds": 120})
            self.assertNotIn("install", command)
            self.assertNotIn("--fix", command)
            self.assertFalse(snapshot.exists())

    def test_security_dependency_audits_all_locks_after_a_failure(self) -> None:
        for failing_path, expected_status in (
            ("requirements-platform.lock.txt", 17),
            ("apps/platform_draft/requirements-assets.lock.txt", 124),
        ):
            calls: list[str] = []

            def fake_run(label: str, command: list[str], **kwargs: object) -> int:
                calls.append(label)
                self.assertEqual(kwargs, {"timeout_seconds": 120})
                return expected_status if label.endswith(failing_path) else 0

            with mock.patch.object(verifier, "_run", side_effect=fake_run):
                self.assertEqual(verifier._run_security_dependency_audits(), expected_status)
            self.assertEqual(
                calls,
                [f"security/dependency-audit/{path}" for path in EXPECTED_SECURITY_DEPENDENCY_LOCKS],
            )

    def test_security_dependency_missing_lock_fails_closed_without_running_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(verifier, "PLATFORM_ROOT", Path(temporary)):
                with mock.patch.object(verifier, "_run") as run:
                    self.assertNotEqual(verifier._run_security_dependency_audits(), 0)
                    run.assert_not_called()

    def test_security_dependency_lock_preflight_rejects_mutations_and_reads_remaining(self) -> None:
        mutations = (
            ("empty", b""),
            ("comment-only", b"# comment\n"),
            ("missing-hash", b"demo==1.0\n"),
            ("unpinned", b"demo>=1.0 --hash=sha256:" + b"0" * 64 + b"\n"),
            ("malformed", b"demo==1.0 --hash=sha256:not-a-digest\n"),
            ("oversized", b"x" * (1024 * 1024 + 1)),
            ("symlink", None),
            ("read-failure", None),
        )
        remaining_labels = [
            f"security/dependency-audit/{path}" for path in EXPECTED_SECURITY_DEPENDENCY_LOCKS[1:]
        ]

        for name, payload in mutations:
            with self.subTest(mutation=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for relative_path in EXPECTED_SECURITY_DEPENDENCY_LOCKS:
                        target = root / relative_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(PLATFORM_ROOT / relative_path, target)
                    invalid_path = root / EXPECTED_SECURITY_DEPENDENCY_LOCKS[0]
                    if name == "symlink":
                        invalid_path.unlink()
                        invalid_path.symlink_to(root / EXPECTED_SECURITY_DEPENDENCY_LOCKS[1])
                    elif payload is not None:
                        invalid_path.write_bytes(payload)

                    calls: list[str] = []

                    def fake_run(label: str, command: list[str], **kwargs: object) -> int:
                        calls.append(label)
                        return 0

                    with mock.patch.object(verifier, "PLATFORM_ROOT", root):
                        if name == "read-failure":
                            original_open = os.open

                            def fail_one(path, flags, *args, **kwargs):
                                if (
                                    kwargs.get("dir_fd") is not None
                                    and path == invalid_path.name
                                ):
                                    raise OSError("blocked")
                                return original_open(path, flags, *args, **kwargs)

                            with mock.patch.object(
                                verifier.os, "open", side_effect=fail_one
                            ):
                                with mock.patch.object(verifier, "_run", side_effect=fake_run):
                                    status = verifier._run_security_dependency_audits()
                        else:
                            with mock.patch.object(verifier, "_run", side_effect=fake_run):
                                status = verifier._run_security_dependency_audits()

                    self.assertNotEqual(status, 0)
                    self.assertEqual(calls, remaining_labels)

    def test_security_dependency_audit_uses_exact_snapshot_after_source_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative_path in EXPECTED_SECURITY_DEPENDENCY_LOCKS:
                target = root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PLATFORM_ROOT / relative_path, target)
            source = root / EXPECTED_SECURITY_DEPENDENCY_LOCKS[0]
            expected = source.read_bytes()
            observed: list[tuple[Path, bytes]] = []

            def fake_run(label: str, command: list[str], **kwargs: object) -> int:
                snapshot = Path(command[4])
                source.unlink()
                source.write_bytes(b"")
                observed.append((snapshot, snapshot.read_bytes()))
                return 0

            with mock.patch.object(verifier, "PLATFORM_ROOT", root):
                with mock.patch.object(verifier, "_run", side_effect=fake_run):
                    self.assertEqual(verifier._run_security_dependency_audits(), 0)

            self.assertEqual(observed[0][1], expected)
            self.assertEqual(source.read_bytes(), b"")
            self.assertFalse(observed[0][0].exists())

    def test_security_dependency_reader_rejects_in_place_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / EXPECTED_SECURITY_DEPENDENCY_LOCKS[0]
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PLATFORM_ROOT / EXPECTED_SECURITY_DEPENDENCY_LOCKS[0], source)
            original_read = verifier.os.read
            mutated = False

            def mutate_after_read(descriptor: int, count: int) -> bytes:
                nonlocal mutated
                payload = original_read(descriptor, count)
                if not mutated:
                    mutated = True
                    source.write_bytes(b"")
                return payload

            with mock.patch.object(verifier.os, "read", side_effect=mutate_after_read):
                payload, failure = verifier._read_stable_security_dependency_lock(
                    root,
                    EXPECTED_SECURITY_DEPENDENCY_LOCKS[0],
                )
            self.assertIsNone(payload)
            self.assertEqual(failure, "changed-during-read")

    def test_security_dependency_reader_rejects_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "platform_draft").mkdir()
            source = outside / "platform_draft" / "requirements-assets.lock.txt"
            shutil.copyfile(
                PLATFORM_ROOT / "apps/platform_draft/requirements-assets.lock.txt",
                source,
            )
            apps = root / "apps"
            apps.symlink_to(outside, target_is_directory=True)

            payload, failure = verifier._read_stable_security_dependency_lock(
                root,
                "apps/platform_draft/requirements-assets.lock.txt",
            )

            self.assertIsNone(payload)
            self.assertIn(failure, {"unreadable", "unsafe-parent"})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = root / "apps"
            source = apps / "platform_draft" / "requirements-assets.lock.txt"
            source.parent.mkdir(parents=True)
            shutil.copyfile(
                PLATFORM_ROOT / "apps/platform_draft/requirements-assets.lock.txt",
                source,
            )
            expected = source.read_bytes()
            outside = root / "outside"
            outside.mkdir()
            (outside / "platform_draft").mkdir()
            (outside / "platform_draft" / "requirements-assets.lock.txt").write_bytes(b"")
            original_open = verifier.os.open
            swapped = False

            def swap_parent_after_open(path, flags, *args, **kwargs):
                nonlocal swapped
                descriptor = original_open(path, flags, *args, **kwargs)
                if path == "apps" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    apps.rename(root / "apps-original")
                    apps.symlink_to(outside, target_is_directory=True)
                return descriptor

            with mock.patch.object(verifier.os, "open", side_effect=swap_parent_after_open):
                payload, failure = verifier._read_stable_security_dependency_lock(
                    root,
                    "apps/platform_draft/requirements-assets.lock.txt",
                )

            self.assertTrue(swapped)
            self.assertEqual(payload, expected)
            self.assertIsNone(failure)

    def test_security_dependency_snapshot_cleanup_survives_audit_failure(self) -> None:
        for expected_status in (17, 124):
            with self.subTest(expected_status=expected_status):
                captured: list[Path] = []

                def failing_run(label: str, command: list[str], **kwargs: object) -> int:
                    captured.append(Path(command[4]))
                    return expected_status

                with mock.patch.object(verifier, "_run", side_effect=failing_run):
                    self.assertEqual(verifier._run_security_dependency_audits(), expected_status)
                self.assertEqual(len(captured), len(EXPECTED_SECURITY_DEPENDENCY_LOCKS))
                self.assertTrue(all(not path.exists() for path in captured))

    def test_security_dependency_snapshot_cleanup_survives_interrupt(self) -> None:
        captured: list[Path] = []

        def interrupting_run(label: str, command: list[str], **kwargs: object) -> int:
            captured.append(Path(command[4]))
            raise KeyboardInterrupt

        with mock.patch.object(verifier, "_run", side_effect=interrupting_run):
            with self.assertRaises(KeyboardInterrupt):
                verifier._run_security_dependency_audits()
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_ci_lock_covers_runtime_quality_and_bootstrap_inputs(self) -> None:
        runtime = _direct_pins(PLATFORM_ROOT / "requirements-platform.txt")
        quality = _direct_pins(PLATFORM_ROOT / "requirements-quality.txt")
        self.assertTrue(set(runtime).isdisjoint(quality))

        lock = _ci_lock_pins()
        expected = {**runtime, **quality, "setuptools": "84.0.0", "wheel": "0.48.0"}
        for name, version in expected.items():
            self.assertEqual(lock.get(name), version, name)
        self.assertIn("pip", lock)
        self.assertGreaterEqual(len(lock), len(expected))

        locker = _ci_lock_pins(CI_LOCKER)
        self.assertEqual(locker["pip-tools"], "7.6.1")
        self.assertEqual(locker["pip"], "26.2.1")
        self.assertEqual(locker["setuptools"], "84.0.0")
        self.assertEqual(locker["wheel"], "0.48.0")

    def test_ci_input_and_generator_keep_one_canonical_source(self) -> None:
        ci_input = CI_INPUT.read_text(encoding="utf-8")
        self.assertIn("-r requirements-platform.txt", ci_input)
        self.assertIn("-r requirements-quality.txt", ci_input)
        self.assertIn("setuptools==84.0.0", ci_input)
        self.assertIn("wheel==0.48.0", ci_input)

        generator = CI_LOCK_GENERATOR.read_text(encoding="utf-8")
        self.assertIn('EXPECTED_PIP_TOOLS_VERSION="7.6.1"', generator)
        self.assertIn("--generate-hashes", generator)
        self.assertIn("--only-binary=:all:", generator)
        self.assertIn("requirements-ci.lock.txt", generator)
        self.assertIn("requirements-ci-locker.lock.txt", generator)
        self.assertIn("requirements-ci.lock.meta.json", generator)
        self.assertIn("--update", generator)
        self.assertIn("--constraint", generator)
        self.assertIn("--isolated", generator)
        self.assertNotIn("PLATFORM_PIP_COMPILE", generator)
        metadata = json.loads(CI_METADATA.read_text(encoding="utf-8"))
        self.assertEqual(metadata["toolchain_lock_file"], "requirements-ci-locker.lock.txt")
        self.assertEqual(metadata["toolchain"]["pip-tools"], "7.6.1")
        self.assertEqual(metadata["freshness_policy"]["default"], "reuse-existing-locked-versions")
        self.assertEqual(
            [item["path"] for item in metadata["input_files"]],
            [
                "requirements-ci.in",
                "requirements-platform.txt",
                "requirements-quality.txt",
            ],
        )

    def test_ci_installer_is_lock_only_and_fail_closed(self) -> None:
        installer = CI_INSTALLER.read_text(encoding="utf-8")
        verifier = (PLATFORM_ROOT / "tools/platform_verify.py").read_text(encoding="utf-8")
        generator = CI_LOCK_GENERATOR.read_text(encoding="utf-8")
        pip_env = CI_ENV_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("requirements-ci.lock.txt", installer)
        self.assertIn("--require-hashes", installer)
        self.assertIn("--only-binary=:all:", installer)
        self.assertIn("--index-url https://pypi.org/simple", installer)
        self.assertIn('"$VENV_PYTHON" -m pip check', installer)
        self.assertIn("refusing to reuse an existing CI virtualenv", installer)
        self.assertNotIn("requirements-platform.txt", installer)
        self.assertNotIn("requirements-quality.txt", installer)
        self.assertIn('"requirements-ci.lock.txt"', verifier)
        self.assertIn('"requirements-platform.lock.txt"', verifier)
        self.assertIn('"requirements-ci-locker.lock.txt"', verifier)
        self.assertIn('"apps/platform_draft/requirements-assets.lock.txt"', verifier)
        self.assertIn('"--disable-pip"', verifier)
        self.assertIn('"--require-hashes"', verifier)
        self.assertIn('"--strict"', verifier)
        self.assertIn('"--timeout"', verifier)
        self.assertNotIn('"-r", "requirements-platform.txt"', verifier)
        self.assertIn("platform_ci_pip_env.sh", installer)
        self.assertIn("platform_ci_pip_env.sh", generator)
        self.assertIn("PIP_CONFIG_FILE=/dev/null", pip_env)

        secret = "SHOULD_NOT_APPEAR"
        injected = os.environ.copy()
        for name in (
            "PIP_EXTRA_INDEX_URL",
            "PIP_INDEX_URL",
            "PIP_TRUSTED_HOST",
            "PIP_FIND_LINKS",
            "PIP_CERT",
            "PIP_CLIENT_CERT",
            "PIP_CONFIG_FILE",
        ):
            injected[name] = f"https://secret.invalid/{secret}"
        probe = subprocess.run(
            [
                str(CI_ENV_WRAPPER),
                sys.executable,
                "-c",
                "import os; print(sorted((k, v) for k, v in os.environ.items() if k.startswith('PIP_') or k.endswith('PROXY')))",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=injected,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "[('PIP_CONFIG_FILE', '/dev/null')]")
        self.assertNotIn(secret, probe.stdout + probe.stderr)
        pip_probe = subprocess.run(
            [str(CI_ENV_WRAPPER), sys.executable, "-m", "pip", "--isolated", "config", "debug"],
            check=False,
            capture_output=True,
            text=True,
            env=injected,
        )
        self.assertEqual(pip_probe.returncode, 0, pip_probe.stderr)
        self.assertNotIn(secret, pip_probe.stdout + pip_probe.stderr)

        # Update mode must reject an adversarial output target before creating
        # the tool venv or replacing any existing lock/metadata path.
        for target_kind in (
            "lock-symlink",
            "metadata-symlink",
            "lock-directory",
            "metadata-directory",
        ):
            with tempfile.TemporaryDirectory(prefix="ci-lock-target-") as temp_dir:
                temp_root = Path(temp_dir) / "platform"
                tools_root = temp_root / "tools"
                tools_root.mkdir(parents=True)
                for source in (
                    CI_INPUT,
                    PLATFORM_ROOT / "requirements-platform.txt",
                    PLATFORM_ROOT / "requirements-quality.txt",
                    CI_LOCK,
                    CI_LOCKER,
                    CI_METADATA,
                    CI_LOCK_GENERATOR,
                    CI_ENV_WRAPPER,
                ):
                    destination = temp_root / source.name
                    if source.parent == PLATFORM_ROOT / "tools":
                        destination = tools_root / source.name
                    shutil.copy2(source, destination)

                output_path = temp_root / CI_LOCK.name
                metadata_path = temp_root / CI_METADATA.name
                marker = temp_root / "python-invoked"
                fake_python = temp_root / "fake-python"
                fake_python.write_text(
                    "#!/bin/sh\nprintf invoked > \"$PLATFORM_CI_FAKE_MARKER\"\nexit 99\n",
                    encoding="utf-8",
                )
                fake_python.chmod(0o755)

                if target_kind == "lock-symlink":
                    output_path.unlink()
                    lock_target = temp_root / "lock-target.txt"
                    shutil.copy2(CI_LOCK, lock_target)
                    output_path.symlink_to(lock_target)
                elif target_kind == "metadata-symlink":
                    metadata_path.unlink()
                    metadata_target = temp_root / "metadata-target.json"
                    shutil.copy2(CI_METADATA, metadata_target)
                    metadata_path.symlink_to(metadata_target)
                elif target_kind == "lock-directory":
                    output_path.unlink()
                    output_path.mkdir()
                else:
                    metadata_path.unlink()
                    metadata_path.mkdir()

                environment = os.environ.copy()
                environment["PLATFORM_CI_PYTHON"] = str(fake_python)
                environment["PLATFORM_CI_FAKE_MARKER"] = str(marker)
                result = subprocess.run(
                    [str(tools_root / CI_LOCK_GENERATOR.name), "--update"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0, target_kind)
                self.assertIn("regular file", result.stderr, target_kind)
                self.assertFalse(marker.exists(), target_kind)

    def test_security_workflow_uses_only_the_canonical_ci_installer(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        job_blocks = {
            match.group("job_id"): match.group("body")
            for match in re.finditer(
                r"^  (?P<job_id>[A-Za-z0-9_-]+):\n"
                r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                workflow,
                re.MULTILINE | re.DOTALL,
            )
        }
        setup_jobs = {
            job_id: block
            for job_id, block in job_blocks.items()
            if re.search(
                r"^\s*(?:-\s+)?uses:\s*actions/setup-python@",
                block,
                re.MULTILINE,
            )
        }
        self.assertIn("release-runtime", setup_jobs)
        self.assertGreater(len(setup_jobs), 1)
        for job_id, block in sorted(setup_jobs.items()):
            steps = _workflow_step_blocks(block)
            setup_steps = tuple(
                step
                for step in steps
                if re.search(
                    r"^\s*(?:-\s+)?uses:\s*actions/setup-python@",
                    step,
                    re.MULTILINE,
                )
            )
            installer_steps = tuple(
                step
                for step in steps
                if re.search(
                    r"^\s*run:\s*platform/tools/platform_install_ci_python\.sh\s*$",
                    step,
                    re.MULTILINE,
                )
            )
            self.assertEqual(len(setup_steps), 1, job_id)
            self.assertEqual(len(installer_steps), 1, job_id)
            setup_step = setup_steps[0]
            self.assertEqual(
                len(re.findall(r'^\s+python-version:\s*"3\.12"\s*$', setup_step, re.MULTILINE)),
                1,
                job_id,
            )
            self.assertEqual(
                len(re.findall(r"^\s+cache:\s*pip\s*$", setup_step, re.MULTILINE)),
                1,
                job_id,
            )
            self.assertEqual(
                len(
                    re.findall(
                        r"^\s+cache-dependency-path:\s*platform/requirements-ci\.lock\.txt\s*$",
                        setup_step,
                        re.MULTILINE,
                    )
                ),
                1,
                job_id,
            )
        self.assertNotIn("requirements-platform.txt", workflow)
        self.assertNotIn("requirements-quality.txt", workflow)
        self.assertNotIn("requirements-platform.lock.txt", workflow)
        self.assertNotIn("python -m venv platform/.venv_platform", workflow)
        self.assertNotIn("python -m pip install", workflow)

    def test_validator_accepts_any_exact_pip_version_from_inputs(self) -> None:
        pins = {"demo": "1.0", "pip": "999.88.77"}
        with mock.patch.object(validator, "_read_pins", return_value=pins):
            self.assertEqual(
                validator._validate_requirements(Path("requirements-platform.txt")),
                pins,
            )
            self.assertEqual(
                validator._validate_lock(Path("requirements-platform.lock.txt")),
                pins,
            )
            self.assertEqual(
                validator._validate_freeze(Path("requirements-platform.freeze.txt")),
                pins,
            )

    def test_validator_still_requires_an_exact_pip_pin(self) -> None:
        with mock.patch.object(
            validator,
            "_read_pins",
            return_value={"demo": "1.0"},
        ):
            with self.assertRaisesRegex(validator.WheelhouseError, "exact pip pin"):
                validator._validate_requirements(Path("requirements-platform.txt"))

    def test_validator_does_not_duplicate_the_pip_version(self) -> None:
        source = Path(validator.__file__).read_text(encoding="utf-8")
        self.assertNotIn("26.1.2", source)
        self.assertNotIn("26.2", source)

    def test_release_installer_uses_the_verified_pip_wheel_without_version_hardcode(
        self,
    ) -> None:
        installer = (
            REPO_ROOT / "platform/tools/platform_release_install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PIP_WHEELS=("$RELEASE_DIR"/wheelhouse/pip-*.whl)', installer
        )
        self.assertIn(
            "class=wheelhouse", installer
        )
        self.assertNotIn("pip-26.1.2-", installer)
        self.assertNotIn("pip==26.1.2", installer)
        self.assertNotIn("pip-26.2-", installer)
        self.assertNotIn("pip==26.2", installer)


if __name__ == "__main__":
    unittest.main()
