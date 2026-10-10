from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import re
import resource
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle
from tools import platform_host_tools_candidate as candidate
from tools import platform_host_tools_pin as pin
from tools import platform_workflow_remote_dispatch as dispatcher
from tools.platform_verify_contract import (
    _workflow_step_blocks,
    host_tools_candidate_artifact_zip_curl_blocks,
    host_tools_candidate_workflow_issues,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "platform" / "tools"
SOURCE_SHA = "d974c8b0536683d0ca8d6f1aca8331a215023fd4"
# Keep the fixture anchored to the repository's reviewed generation.  The
# provenance lifecycle intentionally changes this value in a later pin-only
# commit, so a test-side hard-coded SHA would make that commit alter unrelated
# test code.
PIN_SHA = json.loads(
    (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
)["host_tools_sha"]
ARTIFACT_DIGEST = "sha256:" + "e" * 64


def _release_receipt(
    slug: str,
    source_sha: str,
    *,
    legacy_runtime_layout: object | None = None,
    extra_field: bool = False,
) -> bytes:
    payload: dict[str, object] = {
        "artifact_format_version": 1,
        "release_slug": slug,
        "built_at_utc": "20260920T123932Z",
        "release_ref": slug,
        "source_git_commit": source_sha,
        "python_requirements_file": "requirements-platform.txt",
        "python_lock_file": "requirements-platform.lock.txt",
        "python_freeze_file": "requirements-platform.freeze.txt",
        "python_wheelhouse_dir": "wheelhouse",
        "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
        "web_package_lock_file": "apps/platform_web/package-lock.json",
        "web_build_id": "fixture-build-id",
        "node_version": "26.3.1",
        "npm_version": "11.16.0",
    }
    if legacy_runtime_layout is not None:
        payload["runtime_layout"] = legacy_runtime_layout
    if extra_field:
        payload["unexpected"] = "rejected"
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


class HostToolsBundleTests(unittest.TestCase):
    def test_external_cleanup_dispatch_uses_validated_equal_run_ids(self) -> None:
        target_sha = "a" * 40
        load_run_id = "37556438370"
        cleanup_run_id = "37560000001"
        control_email = "synthetic-control@example.test"

        class FakeChild:
            def __init__(self, returncode: int, marker: bytes | None) -> None:
                self.returncode = returncode
                self.pid = 2_000_000_000
                stdin_read, stdin_write = os.pipe()
                stdout_read, stdout_write = os.pipe()
                self._stdin_read = stdin_read
                self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
                self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
                if marker is not None:
                    os.write(stdout_write, marker)
                os.close(stdout_write)
                self.input_bytes: bytes | None = None

            def poll(self) -> int:
                if self.input_bytes is None:
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(self._stdin_read, 4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    os.close(self._stdin_read)
                    self.input_bytes = b"".join(chunks)
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                return self.returncode

        def invoke(
            command: str,
            payload: dict[str, object],
            *,
            returncode: int = 0,
            marker: bytes | None = None,
        ) -> tuple[int, str, list[str], bytes | None]:
            raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            child_args: list[str] = []
            child = FakeChild(
                returncode,
                marker
                if marker is not None
                else f"RETAINED_CLEANUP_STAGE schema=1 stage=complete exit_code={returncode}\n".encode()
                if returncode == 0
                else None,
            )

            def spawn(arguments: list[str], **_kwargs: object) -> FakeChild:
                child_args.extend(arguments)
                return child

            output = StringIO()
            with (
                patch.object(dispatcher.sys, "stdin", SimpleNamespace(buffer=BytesIO(raw))),
                patch.object(dispatcher, "_trusted_helper", return_value=True),
                patch.object(dispatcher.subprocess, "Popen", side_effect=spawn),
                redirect_stdout(output),
            ):
                result = dispatcher.main([command])
            return result, output.getvalue(), child_args, child.input_bytes

        common = {
            "schema": 1,
            "target_sha": target_sha,
            "control_email": control_email,
            "load_run_id": load_run_id,
        }
        external = {**common, "cleanup_run_id": load_run_id}
        retained = {**common, "cleanup_run_id": cleanup_run_id}
        expected_control_stdin = (
            json.dumps(
                {"schema": 1, "control_email": control_email},
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")

        for command, payload, expected_cleanup_id in (
            ("external-cleanup", external, load_run_id),
            ("retained-cleanup", retained, cleanup_run_id),
        ):
            with self.subTest(command=command):
                result, output, child_args, child_stdin = invoke(command, payload)
                self.assertEqual(result, 0)
                self.assertEqual(
                    output,
                    "RETAINED_CLEANUP_DIAGNOSTIC schema=1 stage=complete child_exit=0\n",
                )
                self.assertEqual(
                    child_args,
                    [
                        dispatcher.SUDO,
                        "-n",
                        "--",
                        str(dispatcher.CLEANUP_HELPER),
                        dispatcher.DELETE_CONFIRMATION,
                        target_sha,
                        load_run_id,
                        expected_cleanup_id,
                    ],
                )
                self.assertEqual(child_stdin, expected_control_stdin)
                self.assertNotIn(control_email, child_args)

        for command, payload, expected_cleanup_id in (
            ("external-cleanup-exports", external, load_run_id),
            ("retained-cleanup-exports", retained, cleanup_run_id),
        ):
            with self.subTest(command=command):
                raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                executor_calls: list[tuple[str, dict[str, object]]] = []

                def run_executor(
                    operation: str, executor_payload: dict[str, object]
                ) -> int:
                    executor_calls.append((operation, executor_payload))
                    return 0

                with (
                    patch.object(
                        dispatcher.sys,
                        "stdin",
                        SimpleNamespace(buffer=BytesIO(raw)),
                    ),
                    patch.object(
                        dispatcher, "_current_pin_matches_host_generation", return_value=True
                    ),
                    patch.object(
                        dispatcher, "_run_retained_export_executor", side_effect=run_executor
                    ),
                    redirect_stdout(StringIO()),
                ):
                    result = dispatcher.main([command])

                self.assertEqual(result, 0)
                self.assertEqual(
                    executor_calls,
                    [
                        (
                            "remove",
                            {
                                "schema": 1,
                                "load_run_id": load_run_id,
                                "cleanup_run_id": expected_cleanup_id,
                            },
                        )
                    ],
                )

        failed_marker = (
            b"RETAINED_CLEANUP_STAGE schema=1 stage=matrix_cleanup exit_code=1\n"
        )
        result, output, _, _ = invoke(
            "external-cleanup", external, returncode=1, marker=failed_marker
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            output,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=1 "
            "stage=matrix_cleanup child_exit=1\n",
        )
        result, output, _, _ = invoke(
            "external-cleanup", external, returncode=1, marker=b"invalid marker\n"
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            output,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=1 stage=unknown child_exit=1\n",
        )

    def test_active_release_baseline_reader_returns_stable_closed_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            release = runtime / "releases" / "gha-35511236041-1-87547df2abd4"
            (runtime / "releases").mkdir(parents=True)
            (runtime / "shared").mkdir()
            release.mkdir()
            receipt = _release_receipt(
                release.name,
                "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
                legacy_runtime_layout={
                    "app_dir": "/opt/oldsparky/platform",
                    "current_symlink": "/opt/oldsparky/platform/current",
                    "previous_symlink": "/opt/oldsparky/platform/previous",
                    "shared_dir": "/opt/oldsparky/platform/shared",
                    "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
                    "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
                },
            )
            (release / "RELEASE.json").write_bytes(receipt)
            (release / "RELEASE.json").chmod(0o444)
            (runtime / "current").symlink_to("releases/gha-35511236041-1-87547df2abd4")

            with (
                patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                patch.object(
                    dispatcher,
                    "_stable_host_file",
                    return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                ),
            ):
                baseline = dispatcher._release_baseline()

            self.assertEqual(
                set(baseline),
                {
                    "schema", "source_sha", "release_slug", "release_json_sha256",
                    "current_link_dev", "current_link_ino", "release_dev", "release_ino",
                    "pending_operation",
                },
            )
            self.assertEqual(baseline["schema"], 1)
            self.assertEqual(baseline["source_sha"], "87547df2abd4aa06a07f4dd4b4f730e9912707e1")
            self.assertEqual(baseline["release_slug"], release.name)
            self.assertEqual(baseline["release_json_sha256"], hashlib.sha256(receipt).hexdigest())
            self.assertIs(baseline["pending_operation"], False)
            for key in ("current_link_dev", "current_link_ino", "release_dev", "release_ino"):
                self.assertIs(type(baseline[key]), int)
                self.assertGreaterEqual(baseline[key], 0)

    def test_release_validator_accepts_only_the_exact_legacy_runtime_layout(self) -> None:
        validator_path = TOOLS_ROOT / "platform_validate_release_artifact.py"
        namespace: dict[str, object] = {"__name__": "_release_validator_fixture"}
        exec(compile(validator_path.read_bytes(), str(validator_path), "exec"), namespace)
        parser = namespace["_parse_release_json"]
        artifact_error = namespace["ArtifactError"]
        source_sha = "87547df2abd4aa06a07f4dd4b4f730e9912707e1"
        slug = "gha-35511236041-1-87547df2abd4"
        valid_layout = {
            "app_dir": "/opt/oldsparky/platform",
            "current_symlink": "/opt/oldsparky/platform/current",
            "previous_symlink": "/opt/oldsparky/platform/previous",
            "shared_dir": "/opt/oldsparky/platform/shared",
            "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
            "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
        }
        legacy_receipt = _release_receipt(
            slug, source_sha, legacy_runtime_layout=valid_layout
        )
        with self.assertRaises(artifact_error):
            parser(legacy_receipt, release_slug=slug)
        self.assertEqual(
            parser(
                legacy_receipt,
                release_slug=slug,
                allow_legacy_runtime_layout=True,
            )["runtime_layout"],
            valid_layout,
        )
        wrong_layout = {**valid_layout, "app_dir": "/tmp/platform"}
        for receipt in (
            _release_receipt(slug, source_sha, legacy_runtime_layout=wrong_layout),
            _release_receipt(
                slug,
                source_sha,
                legacy_runtime_layout=valid_layout,
                extra_field=True,
            ),
        ):
            with self.subTest(receipt=receipt), self.assertRaises(artifact_error):
                parser(
                    receipt,
                    release_slug=slug,
                    allow_legacy_runtime_layout=True,
                )

    def test_active_release_baseline_reader_rejects_duplicate_receipt_keys_and_pending_state(self) -> None:
        for receipt, pending_name in (
            (b'{"source_git_commit":"87547df2abd4aa06a07f4dd4b4f730e9912707e1",'
             b'"source_git_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n', False),
            (_release_receipt("gha-35511236041-1-87547df2abd4", "87547df2abd4aa06a07f4dd4b4f730e9912707e1"), ".release-operation.json"),
            (_release_receipt("gha-35511236041-1-87547df2abd4", "87547df2abd4aa06a07f4dd4b4f730e9912707e1"), ".release-systemd-state.json"),
        ):
            with self.subTest(pending=pending_name):
                with tempfile.TemporaryDirectory() as temporary:
                    runtime = Path(temporary) / "runtime"
                    release = runtime / "releases" / "gha-35511236041-1-87547df2abd4"
                    (runtime / "releases").mkdir(parents=True)
                    shared = runtime / "shared"
                    shared.mkdir()
                    release.mkdir()
                    (release / "RELEASE.json").write_bytes(receipt)
                    (release / "RELEASE.json").chmod(0o444)
                    (runtime / "current").symlink_to("releases/gha-35511236041-1-87547df2abd4")
                    if pending_name:
                        (shared / pending_name).write_text("{}", encoding="ascii")
                    with (
                        patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                        patch.object(
                            dispatcher,
                            "_stable_host_file",
                            return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                        ),
                    ):
                        with self.assertRaises(OSError):
                            dispatcher._release_baseline()

    def test_active_release_baseline_reader_rejects_mutable_or_misdirected_receipts(self) -> None:
        mutations = ("writable", "hardlink", "symlink", "wrong_slug", "outside_pointer")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary) / "runtime"
                releases = runtime / "releases"
                release = releases / "gha-35511236041-1-87547df2abd4"
                releases.mkdir(parents=True)
                (runtime / "shared").mkdir()
                release.mkdir()
                receipt_path = release / "RELEASE.json"
                receipt = _release_receipt(
                    release.name, "87547df2abd4aa06a07f4dd4b4f730e9912707e1"
                )
                receipt_path.write_bytes(receipt)
                receipt_path.chmod(0o444)
                current_target = "releases/gha-35511236041-1-87547df2abd4"
                if mutation == "writable":
                    receipt_path.chmod(0o644)
                elif mutation == "hardlink":
                    os.link(receipt_path, runtime / "shared" / "receipt-copy")
                elif mutation == "symlink":
                    receipt_path.unlink()
                    receipt_path.symlink_to("../shared/receipt-copy")
                    (runtime / "shared" / "receipt-copy").write_bytes(receipt)
                elif mutation == "wrong_slug":
                    receipt_path.chmod(0o644)
                    receipt_path.write_bytes(_release_receipt(
                        "gha-35511236041-1-aaaaaaaaaaaa",
                        "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
                    ))
                    receipt_path.chmod(0o444)
                elif mutation == "outside_pointer":
                    current_target = "../outside-release"
                    (runtime / "outside-release").mkdir()
                (runtime / "current").symlink_to(current_target)

                with (
                    patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                    patch.object(
                        dispatcher,
                        "_stable_host_file",
                        return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                    ),
                ):
                    with self.assertRaises(OSError):
                        dispatcher._release_baseline()

    def test_active_release_baseline_identity_comparison_is_closed_and_typed(self) -> None:
        baseline: dict[str, object] = {
            "schema": 1,
            "source_sha": "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
            "release_slug": "gha-35511236041-1-87547df2abd4",
            "release_json_sha256": "a" * 64,
            "current_link_dev": 8,
            "current_link_ino": 9,
            "release_dev": 8,
            "release_ino": 10,
            "pending_operation": False,
        }
        self.assertTrue(dispatcher._baseline_identity_matches(baseline, dict(baseline)))
        for field, changed in (
            ("source_sha", "a" * 40),
            ("release_slug", "gha-35511236041-1-aaaaaaaaaaaa"),
            ("release_json_sha256", "b" * 64),
            ("current_link_ino", 99),
            ("release_ino", 99),
            ("pending_operation", True),
            ("current_link_dev", True),
            ("schema", True),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    dispatcher._baseline_identity_matches(
                        baseline, {**baseline, field: changed}
                    )
                )
        self.assertFalse(
            dispatcher._baseline_identity_matches(
                baseline, {**baseline, "extra": "unexpected"}
            )
        )
        with self.assertRaises(OSError):
            dispatcher._strict_baseline_json(
                b'{"schema":1,"schema":1}'
            )

    def _assert_artifact_zip_transport_contract(self) -> None:
        with BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("fixture.txt", b"artifact ZIP fixture")
            zip_payload = buffer.getvalue()

        endpoint = "/repos/StrayForest/old_sparky/actions/artifacts/123/zip"
        requests: list[dict[str, str | None]] = []

        class ArtifactHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "accept": self.headers.get("Accept"),
                        "api_version": self.headers.get("X-GitHub-Api-Version"),
                    }
                )
                if self.path == endpoint:
                    if self.headers.get("Accept") != "application/vnd.github+json":
                        body = b"unsupported media type\n"
                        self.send_response(415)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(302)
                    self.send_header("Location", "/fixture.zip")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/fixture.zip":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(zip_payload)))
                    self.end_headers()
                    self.wfile.write(zip_payload)
                    return
                body = b"not found\n"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ArtifactHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def download(accept: str, output: Path) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [
                            "curl",
                            "--fail-with-body",
                            "--silent",
                            "--show-error",
                            "--location",
                            "--max-time",
                            "10",
                            "--max-filesize",
                            "8388608",
                            "--header",
                            "Authorization: Bearer fixture-token",
                            "--header",
                            f"Accept: {accept}",
                            "--header",
                            "X-GitHub-Api-Version: 2022-11-28",
                            f"http://127.0.0.1:{server.server_port}{endpoint}",
                            "--output",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
                        check=False,
                    )

                successful = download("application/vnd.github+json", root / "artifact.zip")
                self.assertEqual(successful.returncode, 0, successful.stderr)
                self.assertEqual((root / "artifact.zip").read_bytes(), zip_payload)
                self.assertEqual(
                    [request["path"] for request in requests],
                    [endpoint, "/fixture.zip"],
                )
                for request in requests:
                    self.assertEqual(request["authorization"], "Bearer fixture-token")
                    self.assertEqual(request["accept"], "application/vnd.github+json")
                    self.assertEqual(request["api_version"], "2022-11-28")

                requests.clear()
                rejected = download("application/zip", root / "rejected.zip")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["path"], endpoint)
                self.assertEqual(requests[0]["accept"], "application/zip")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

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
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            tools_dir = source_root / "platform" / "tools"
            tools_dir.mkdir(parents=True)
            bundle_source = tools_dir / "platform_host_tools_bundle.py"
            legacy_groups = (
                ("PREPARE_ARTIFACT_FILES", bundle.PREPARE_ARTIFACT_FILES),
                ("PRODUCTION_DEPLOY_CONTROL_FILES", bundle.PRODUCTION_DEPLOY_CONTROL_FILES),
                ("RETAINED_LOAD_ARTIFACT_FILES", bundle.RETAINED_LOAD_ARTIFACT_FILES),
            )
            extended_groups = (*legacy_groups, (
                "CPU_DIAGNOSTIC_CONTROL_FILES", ("platform_cpu_diagnostic_plan.py",),
            ))
            def group_declarations(groups):
                return "".join(
                    f"{group} = ({', '.join(json.dumps(name) for name in values)},)\n"
                    for group, values in groups
                )

            for groups in (legacy_groups, extended_groups):
                names = tuple(name for _group, values in groups for name in values)
                bundle_source.write_text(
                    group_declarations(groups)
                    + f"HOST_TOOL_FILES = ({' + '.join(group for group, _ in groups)})\n",
                    encoding="ascii",
                )
                self.assertEqual(pin._bundle_file_names(source_root), names)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            self.assertEqual(
                pin._bundle_file_names(source_root),
                tuple(name for _group, values in legacy_groups for name in values),
            )
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("unapproved_helper.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            valid_declaration = bundle_source.read_bytes()
            bundle_source.write_bytes(
                valid_declaration + b"#" + b"x" * pin.MAX_SOURCE_CONTRACT_BYTES
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_bytes(valid_declaration)
            linked_declaration = tools_dir / "linked_bundle.py"
            os.link(bundle_source, linked_declaration)
            try:
                with self.assertRaises(pin.HostToolsPinError):
                    pin._bundle_file_names(source_root)
            finally:
                linked_declaration.unlink()
            saved_declaration = tools_dir / "platform_host_tools_bundle.saved"
            bundle_source.replace(saved_declaration)
            try:
                bundle_source.symlink_to(saved_declaration.name)
                with self.assertRaises(pin.HostToolsPinError):
                    pin._bundle_file_names(source_root)
            finally:
                bundle_source.unlink(missing_ok=True)
                saved_declaration.replace(bundle_source)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'UNKNOWN_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n",
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(extended_groups)
                + 'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(extended_groups)
                + 'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + CPU_DIAGNOSTIC_CONTROL_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            cpu_layout = bundle.CPU_DIAGNOSTIC_HOST_TOOL_FILES
            cpu_pin = {
                "closure": [
                    {
                        "mode": 0o755,
                        "path": f"platform/tools/{name}",
                        "sha256": "a" * 64,
                    }
                    for name in cpu_layout
                ]
            }
            self.assertEqual(
                tuple(row["path"] for row in candidate._pin_closure(source_root, cpu_pin)),
                tuple(f"platform/tools/{name}" for name in cpu_layout),
            )
            bundle_source.write_text(
                'PREPARE_ARTIFACT_FILES = ("a.py",)\n'
                'PRODUCTION_DEPLOY_CONTROL_FILES = ("b.py",)\n'
                'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)

    def test_host_tools_provisioning_adr_matches_enforced_generation_pin(self) -> None:
        adr = (
            REPO_ROOT / "platform/docs/adr/production-host-tools-provisioning.md"
        ).read_text(encoding="utf-8")
        self.assertIn(PIN_SHA, adr)
        self.assertNotIn("25c67089fdfca99f58d801cc529bb0e987f5ecf8", adr)

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
            closure_paths = tuple(
                str(record["path"]) for record in payload["closure"]
            )
            indexed_modes: dict[str, int] = {}
            for line in git("ls-files", "--stage", "--", *closure_paths).splitlines():
                metadata, tracked_path = line.split("\t", 1)
                mode, _object_id, stage = metadata.split()
                self.assertEqual(stage, "0")
                indexed_modes[tracked_path] = {
                    "100644": 0o644,
                    "100755": 0o755,
                }[mode]
            self.assertEqual(set(indexed_modes), set(closure_paths))
            for relative_path, expected_mode in indexed_modes.items():
                (fixture / relative_path).chmod(expected_mode)
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
        artifact_validation = host_preflight.split(
            "      - name: Validate host-tools artifact envelope and bundle", 1
        )[1].split("      - name: Validate root SSH identity and installed generation", 1)[0]
        self.assertIn(
            'grep -Fqx "capability=retained_load_source_binding" "$inner_root/capabilities.txt"',
            artifact_validation,
        )
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

    def test_host_attestation_policy_accepts_flat_verified_certificate_claims(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  validate-active-baseline:", 1
        )[0]
        handoff_steps = [
            step
            for step in _workflow_step_blocks(preflight)
            if "- name: Create closed host-tools handoff after final verification" in step
        ]
        self.assertEqual(len(handoff_steps), 1)
        marker = (
            "          /usr/bin/python3 - \"$attestation_json\" \"$bundle_sha256\" "
            "\"$GITHUB_RUN_ID\" \"$GITHUB_RUN_ATTEMPT\" \"$TARGET_SHA\" "
            "\"$HOST_TOOLS_ARTIFACT_NAME\" <<'PY'\n"
        )
        self.assertIn(marker, handoff_steps[0])
        policy = handoff_steps[0].split(marker, 1)[1].split("\n          PY", 1)[0]
        policy = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in policy.splitlines()
        )
        production = workflow.split("  production:", 1)[1]
        self.assertEqual(handoff_steps[0].count('certificate.get("extensions", certificate)'), 1)
        self.assertEqual(production.count('certificate.get("extensions", certificate)'), 1)
        production_policy_start = production.index("          attestation_matches = 0\n")
        production_policy_end = production.index("\n          expected_files = {", production_policy_start)
        production_policy = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in production[production_policy_start:production_policy_end].splitlines()
        )

        source_sha = "a" * 40
        bundle_sha = "b" * 64
        run_id = "123456789"
        attempt = "2"
        artifact_name = f"platform-host-tools-bundle-{run_id}-{attempt}"
        build_uri = (
            "https://github.com/StrayForest/old_sparky/.github/workflows/"
            "platform-production-deploy.yml@refs/heads/dev"
        )
        stale_result = {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "issuer": "https://token.actions.githubusercontent.com",
                        "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
                        "sourceRepositoryRef": "refs/heads/dev",
                        "sourceRepositoryDigest": "c" * 40,
                        "buildConfigURI": build_uri,
                        "buildSignerURI": build_uri,
                        "runInvocationURI": "https://github.com/StrayForest/old_sparky/actions/runs/1/attempts/1",
                    }
                },
                "statement": {"subject": [{"digest": {"sha256": bundle_sha}}]},
            }
        }
        current_result = {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "issuer": "https://token.actions.githubusercontent.com",
                        "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
                        "sourceRepositoryRef": "refs/heads/dev",
                        "sourceRepositoryDigest": source_sha,
                        "buildConfigURI": build_uri,
                        "buildSignerURI": build_uri,
                        "runInvocationURI": (
                            "https://github.com/StrayForest/old_sparky/actions/runs/"
                            f"{run_id}/attempts/{attempt}"
                        ),
                    }
                },
                "statement": {"subject": [{"digest": {"sha256": bundle_sha}}]},
            }
        }
        legacy_stale_result = json.loads(json.dumps(stale_result))
        legacy_current_result = json.loads(json.dumps(current_result))
        for legacy_result in (legacy_stale_result, legacy_current_result):
            certificate = legacy_result["verificationResult"]["signature"]["certificate"]
            legacy_result["verificationResult"]["signature"]["certificate"] = {
                "extensions": certificate
            }

        def changed_result(path: tuple[object, ...], value: object) -> dict[str, object]:
            changed = json.loads(json.dumps(current_result))
            target: object = changed
            for part in path[:-1]:
                target = target[part]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            return changed

        arguments = [
            bundle_sha,
            run_id,
            attempt,
            source_sha,
            artifact_name,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            attestation_path = Path(temporary) / "verified-attestations.json"

            def run_policy(values: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
                attestation_path.write_text(json.dumps(values), encoding="utf-8")
                return subprocess.run(
                    [sys.executable, "-c", policy, str(attestation_path), *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            result = run_policy([stale_result, current_result])
            self.assertEqual(result.returncode, 0, result.stderr)
            legacy_result = run_policy([legacy_stale_result, legacy_current_result])
            self.assertEqual(legacy_result.returncode, 0, legacy_result.stderr)
            duplicate_result = run_policy([current_result, current_result])
            self.assertNotEqual(duplicate_result.returncode, 0)
            for invalid_result in (
                changed_result(
                    ("verificationResult", "signature", "certificate", "sourceRepositoryDigest"),
                    "c" * 40,
                ),
                changed_result(
                    ("verificationResult", "signature", "certificate", "runInvocationURI"),
                    "https://github.com/StrayForest/old_sparky/actions/runs/9/attempts/1",
                ),
                changed_result(
                    ("verificationResult", "statement", "subject", 0, "digest", "sha256"),
                    "d" * 64,
                ),
            ):
                self.assertNotEqual(run_policy([invalid_result]).returncode, 0)

        def run_production_policy(values: list[dict[str, object]]) -> None:
            namespace = {
                "attestations": values,
                "build_uri": build_uri,
                "repository_uri": "https://github.com/StrayForest/old_sparky",
                "invocation_uri": (
                    "https://github.com/StrayForest/old_sparky/actions/runs/"
                    f"{run_id}/attempts/{attempt}"
                ),
                "source_ref": "refs/heads/dev",
                "target_sha": source_sha,
                "bundle_sha": bundle_sha,
            }
            exec(production_policy, namespace)

        run_production_policy([stale_result, current_result])
        run_production_policy([legacy_stale_result, legacy_current_result])
        with self.assertRaisesRegex(
            SystemExit, "host-tools attestation certificate/source/run/subject policy failed"
        ):
            run_production_policy([current_result, current_result])
        for invalid_result in (
            changed_result(
                ("verificationResult", "signature", "certificate", "sourceRepositoryDigest"),
                "c" * 40,
            ),
            changed_result(
                ("verificationResult", "signature", "certificate", "runInvocationURI"),
                "https://github.com/StrayForest/old_sparky/actions/runs/9/attempts/1",
            ),
            changed_result(
                ("verificationResult", "statement", "subject", 0, "digest", "sha256"),
                "d" * 64,
            ),
        ):
            with self.assertRaisesRegex(
                SystemExit, "host-tools attestation certificate/source/run/subject policy failed"
            ):
                run_production_policy([invalid_result])

    def _source_fixture(self, root: Path) -> Path:
        source_root = root / "source"
        tools = source_root / "platform" / "tools"
        tools.mkdir(parents=True)
        for name in bundle.HOST_TOOL_FILES:
            destination = tools / name
            shutil.copyfile(TOOLS_ROOT / name, destination)
            os.chmod(destination, 0o755)
        shutil.copyfile(
            TOOLS_ROOT / "platform_host_tools_bundle.py",
            tools / "platform_host_tools_bundle.py",
        )
        os.chmod(tools / "platform_host_tools_bundle.py", 0o644)
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
            self.assertEqual(candidate.MAX_BUNDLE_BYTES, bundle.MAX_BUNDLE_BYTES)
            self.assertEqual(manifest["limits"]["max_bundle_bytes"], bundle.MAX_BUNDLE_BYTES)
            self.assertEqual(
                manifest["components"],
                {key: list(value) for key, value in bundle.COMPONENT_FILES.items()},
            )
            trusted_v4_capabilities = (
                "artifact_prepare",
                "input_guard",
                "production_dispatcher",
                "production_supervisor",
                "production_deploy_control",
                "release_baseline",
                "retained_load_export_cleanup",
                "retained_load_source_binding",
                "python_isolated",
                "python_bytecode_disabled",
                "cpu_diagnostic_plan_control",
            )
            self.assertEqual(manifest["capabilities"], list(trusted_v4_capabilities))
            with zipfile.ZipFile(first) as archive:
                capability_lines = archive.read("platform-host-tools/capabilities.txt")
                self.assertEqual(
                    [
                        line.removeprefix("capability=")
                        for line in capability_lines.decode("ascii").splitlines()
                        if line.startswith("capability=")
                    ],
                    list(trusted_v4_capabilities),
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
            self.assertEqual(first_summary["manifest"]["toolset_version"], "production-host-tools-v4")
            self.assertIn(
                "platform_cpu_diagnostic_plan.py",
                [record["path"].removeprefix("platform-host-tools/") for record in manifest["files"]],
            )

            # The trusted T builder accepts only the fixed v4 capability
            # order. Reordered or extra capabilities must not be normalized
            # as an equivalent set.
            wrong_order_source = self._source_fixture(root / "wrong-order")
            wrong_order_helper = wrong_order_source / "platform" / "tools" / "platform_host_tools_bundle.py"
            wrong_order_text = wrong_order_helper.read_text(encoding="utf-8")
            ordered_capability_block = (
                '    "python_isolated",\n'
                '    "python_bytecode_disabled",\n'
                '    "cpu_diagnostic_plan_control",\n'
            )
            self.assertIn(ordered_capability_block, wrong_order_text)
            wrong_order_helper.write_text(
                wrong_order_text.replace(
                    ordered_capability_block,
                    '    "cpu_diagnostic_plan_control",\n'
                    '    "python_isolated",\n'
                    '    "python_bytecode_disabled",\n',
                    1,
                ),
                encoding="utf-8",
            )
            wrong_order_archive = root / "wrong-order.zip"
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.build_bundle(wrong_order_source, SOURCE_SHA, wrong_order_archive)
            self.assertFalse(wrong_order_archive.exists())

            extra_capability_source = self._source_fixture(root / "extra-capability")
            extra_capability_helper = (
                extra_capability_source / "platform" / "tools" / "platform_host_tools_bundle.py"
            )
            extra_capability_text = extra_capability_helper.read_text(encoding="utf-8")
            self.assertIn(ordered_capability_block, extra_capability_text)
            extra_capability_helper.write_text(
                extra_capability_text.replace(
                    ordered_capability_block,
                    '    "python_isolated",\n'
                    '    "python_bytecode_disabled",\n'
                    '    "cpu_diagnostic_plan_control",\n'
                    '    "unapproved_capability",\n',
                    1,
                ),
                encoding="utf-8",
            )
            extra_capability_archive = root / "extra-capability.zip"
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.build_bundle(extra_capability_source, SOURCE_SHA, extra_capability_archive)
            self.assertFalse(extra_capability_archive.exists())

            # Exercise the immutable v3 compatibility layout as an explicit
            # legacy declaration. The production v4 active declaration above
            # is never extended to manufacture its own expected layout.
            legacy_source = self._source_fixture(root / "legacy")
            legacy_tools = legacy_source / "platform" / "tools"
            helper = legacy_tools / "platform_host_tools_bundle.py"
            helper_text = helper.read_text(encoding="utf-8")
            self.assertIn(
                'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)',
                helper_text,
            )
            self.assertIn("+ CPU_DIAGNOSTIC_CONTROL_FILES", helper_text)
            helper_text = helper_text.replace(
                "    + RETAINED_LOAD_ARTIFACT_FILES\n    + CPU_DIAGNOSTIC_CONTROL_FILES\n)",
                "    + RETAINED_LOAD_ARTIFACT_FILES\n)",
                1,
            )
            self.assertIn('    "cpu_diagnostic_control": CPU_DIAGNOSTIC_CONTROL_FILES,\n', helper_text)
            helper_text = helper_text.replace(
                '    "cpu_diagnostic_control": CPU_DIAGNOSTIC_CONTROL_FILES,\n', "", 1
            )
            self.assertIn('    "cpu_diagnostic_plan_control",\n', helper_text)
            helper_text = helper_text.replace('    "cpu_diagnostic_plan_control",\n', "", 1)
            helper_text = helper_text.replace(
                'TOOLSET_VERSION = "production-host-tools-v4"',
                'TOOLSET_VERSION = "production-host-tools-v3"',
                1,
            )
            helper.write_text(helper_text, encoding="utf-8")
            legacy_archive = root / "legacy.zip"
            legacy_summary = bundle.build_bundle(legacy_source, SOURCE_SHA, legacy_archive)
            legacy_manifest = legacy_summary["manifest"]
            self.assertEqual(legacy_manifest["toolset_version"], "production-host-tools-v3")
            self.assertEqual(
                legacy_manifest["components"],
                {key: list(value) for key, value in bundle.LEGACY_COMPONENT_FILES.items()},
            )
            self.assertNotIn("cpu_diagnostic_plan_control", legacy_manifest["capabilities"])
            self.assertEqual(len(legacy_manifest["files"]), len(bundle.LEGACY_HOST_TOOL_FILES) + 1)
            self.assertFalse(
                any(record["path"].endswith("platform_cpu_diagnostic_plan.py") for record in legacy_manifest["files"])
            )
            self.assertEqual(
                tuple(record["path"] for record in bundle.verify_bundle(legacy_archive)["manifest"]["files"]),
                tuple(sorted((*bundle.LEGACY_HOST_TOOL_FILES, "capabilities.txt"))),
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

            oversized = root / "oversized.zip"
            with oversized.open("wb") as stream:
                stream.truncate(bundle.MAX_BUNDLE_BYTES + 1)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(oversized, expected_source_sha=SOURCE_SHA)

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
module._retained_load_export_owner = lambda: {"uid": 65534, "gid": 65534}
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
                "dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 "
                "release_baseline=1 retained_load_export_cleanup=1 "
                "retained_load_source_binding=1 "
                "cpu_diagnostic_plan_control=1 "
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
        dispatcher_source = (TOOLS_ROOT / "platform_workflow_remote_dispatch.py").read_text()
        host_path_references = set(
            re.findall(r"\$host_tools_dir/(platform_[A-Za-z0-9_.-]+\.(?:py|sh))", supervisor)
        )
        self.assertTrue(host_path_references)
        self.assertTrue(host_path_references <= set(bundle.HOST_TOOL_FILES))
        self.assertTrue(
            all(name in supervisor for name in bundle.PRODUCTION_DEPLOY_CONTROL_FILES)
        )
        manifest_set = re.search(
            r"(?ms)^expected_files = \{\n(?P<body>.*?)^\}", supervisor
        )
        self.assertIsNotNone(manifest_set)
        manifest_names = set(
            re.findall(
                r'^\s+"(platform_[A-Za-z0-9_.-]+\.(?:py|sh)|capabilities\.txt)",$',
                manifest_set["body"],
                re.M,
            )
        )
        self.assertEqual(
            manifest_names, set(bundle.HOST_TOOL_FILES) | {"capabilities.txt"}
        )
        helper_loop = re.search(
            r"(?ms)^for host_helper in \\\n(?P<body>.*?)^done$", supervisor
        )
        self.assertIsNotNone(helper_loop)
        helper_names = set(
            re.findall(
                r"^\s+(platform_[A-Za-z0-9_.-]+\.(?:py|sh))",
                helper_loop["body"],
                re.M,
            )
        ) | {"platform_storage_evidence_summary.py"}
        self.assertEqual(helper_names, set(bundle.HOST_TOOL_FILES))
        self.assertTrue(set(bundle.PREPARE_ARTIFACT_FILES) <= set(bundle.HOST_TOOL_FILES))
        self.assertEqual(
            bundle.RETAINED_LOAD_ARTIFACT_FILES,
            ("platform_retained_load_export_executor.py",),
        )
        self.assertNotIn("platform_release_deploy.sh", bundle.HOST_TOOL_FILES)
        self.assertNotIn("platform_run_api.sh", bundle.HOST_TOOL_FILES)
        self.assertIn("platform_prepare_artifact_dir.py", dispatcher_source)
        self.assertIn("platform_retained_load_export_executor.py", dispatcher_source)

        class CompletedChild:
            returncode = 0

            def __init__(self) -> None:
                self.input_bytes: bytes | None = None
                self.timeout: float | None = None

            def communicate(self, *, input: bytes, timeout: float) -> tuple[bytes, bytes]:
                self.input_bytes = input
                self.timeout = timeout
                return b"", b""

        child = CompletedChild()
        with patch.object(dispatcher.os, "geteuid", return_value=0), \
            patch.object(dispatcher, "_trusted_generation", return_value=True), \
            patch.object(dispatcher, "_trusted_export_executor", return_value=True), \
            patch.object(dispatcher, "_current_pin_matches_host_generation", return_value=True), \
            patch.object(
                dispatcher,
                "_retained_load_export_owner",
                return_value={"uid": 987, "gid": 987},
            ), \
            patch.object(dispatcher.subprocess, "Popen", return_value=child) as popen:
            self.assertEqual(
                dispatcher._remove_exports(
                    load_run_id="12345",
                    cleanup_run_id="67890",
                    target_sha="a" * 40,
                ),
                0,
            )
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                dispatcher.SETPRIV,
                "--reuid=987",
                "--regid=987",
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--",
                dispatcher.SYSTEM_PYTHON,
                "-I",
                "-B",
                str(dispatcher.RETAINED_LOAD_EXPORT_EXECUTOR),
                "remove",
            ],
        )
        child_options = popen.call_args.kwargs
        self.assertEqual(child_options["cwd"], "/")
        self.assertEqual(child_options["env"], dispatcher.EXPORT_EXECUTOR_ENV)
        self.assertEqual(child_options["umask"], 0o077)
        self.assertTrue(child_options["close_fds"])
        self.assertEqual(
            json.loads(child.input_bytes.decode("ascii")),
            {"schema": 1, "load_run_id": "12345", "cleanup_run_id": "67890"},
        )
        self.assertEqual(set(json.loads(child.input_bytes)), {"schema", "load_run_id", "cleanup_run_id"})
        self.assertEqual(
            dispatcher._remove_exports(
                load_run_id="../12345",
                cleanup_run_id="67890",
                target_sha="a" * 40,
            ),
            1,
        )

        real_popen = subprocess.Popen

        def run_cleanup_child(script: str, expected_exit: int) -> tuple[str, list[str]]:
            output = StringIO()
            observed_command: list[str] = []

            def spawn(command: list[str], **options: object) -> subprocess.Popen[bytes]:
                observed_command.extend(command)
                return real_popen(
                    [sys.executable, "-c", script],
                    **options,
                )

            with patch.object(dispatcher, "_trusted_helper", return_value=True), \
                patch.object(dispatcher.subprocess, "Popen", side_effect=spawn), \
                redirect_stdout(output):
                result = dispatcher._run_retained_cleanup_sudo(
                    Path("/fixed/cleanup-helper"),
                    ["closed", "target-sha", "12345", "67890"],
                    control_email="private@example.invalid",
                )
            self.assertEqual(result, expected_exit)
            self.assertNotIn("private@example.invalid", observed_command)
            return output.getvalue(), observed_command

        failed_marker, _ = run_cleanup_child(
            "import json,sys; assert json.load(sys.stdin) == "
            "{'schema': 1, 'control_email': 'private@example.invalid'}; "
            "print('private output must not escape'); "
            "print('RETAINED_CLEANUP_STAGE schema=1 stage=external_vote_recovery exit_code=7'); "
            "raise SystemExit(7)",
            7,
        )
        self.assertEqual(
            failed_marker,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=1 "
            "stage=external_vote_recovery child_exit=7\n",
        )
        missing_marker, _ = run_cleanup_child(
            "import json,sys; assert json.load(sys.stdin) == "
            "{'schema': 1, 'control_email': 'private@example.invalid'}; "
            "print('untrusted output'); raise SystemExit(1)",
            1,
        )
        self.assertEqual(
            missing_marker,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=1 stage=unknown child_exit=1\n",
        )

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
        self.assertIn("remote=(ssh -n -T ", probe)
        self.assertIn("< /dev/null", probe)
        self.assertIn("/usr/bin/head -c 512", probe)
        self.assertIn(
            'expected_output="HOST_TOOLS schema=1 source_sha=$HOST_TOOLS_SHA '
            'generation=$HOST_TOOLS_SHA dispatcher=4 artifact_prepare=2 supervisor=3 '
            'input_guard=2 release_baseline=1 retained_load_export_cleanup=1 '
            'retained_load_source_binding=1 '
            'cpu_diagnostic_plan_control=1 '
            'python_isolated=1 python_bytecode_disabled=1"',
            probe,
        )
        self.assertIn('printf \'%s\\n\' "$expected_output" | cmp -s - "$probe_output"', probe)
        for marker in (
            "command_rc=",
            "expected_bytes=",
            "actual_bytes=",
            "expected_sha256=",
            "actual_sha256=",
            "actual_cr_count=",
            "actual_lf_count=",
            "exact_one_line=",
        ):
            self.assertIn(marker, probe)
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

    def test_host_capability_probe_is_strict_and_bounds_diagnostics(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  validate-active-baseline:", 1
        )[0]
        probe_steps = [
            step
            for step in _workflow_step_blocks(preflight)
            if "- name: Probe immutable host dispatcher capabilities" in step
        ]
        self.assertEqual(len(probe_steps), 1)
        run_marker = "        run: |\n"
        self.assertIn(run_marker, probe_steps[0])
        shell_block = probe_steps[0].split(run_marker, 1)[1]
        shell_block = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in shell_block.splitlines()
        )
        self.assertIn("remote=(ssh -n -T ", shell_block)
        self.assertIn(
            '"${remote[@]}" /usr/bin/python3.12 -I -B "$HOST_TOOLS_DISPATCHER" host-capabilities',
            shell_block,
        )
        self.assertIn("< /dev/null", shell_block)
        self.assertIn("ulimit -f 1", shell_block)
        self.assertIn("printf '%s\\n' \"$expected_output\" | cmp -s - \"$probe_output\"", shell_block)
        self.assertIn(
            'stat -c \'%u:%g\' -- "$HOST_TOOLS_SSH_DIR"',
            shell_block,
        )
        for filename in ("config", "known_hosts", "id_ed25519"):
            self.assertIn(
                f'test "$(stat -c \'%F:%h:%a\' -- "$HOST_TOOLS_SSH_DIR/{filename}")" = "regular file:1:600"',
                shell_block,
            )

        cleanup_steps = [
            step
            for step in _workflow_step_blocks(preflight)
            if "- name: Remove host capability verifier material" in step
        ]
        self.assertEqual(len(cleanup_steps), 1)
        self.assertIn(run_marker, cleanup_steps[0])
        cleanup_shell_block = cleanup_steps[0].split(run_marker, 1)[1]
        cleanup_shell_block = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in cleanup_shell_block.splitlines()
        )
        self.assertIn('"$RUNNER_TEMP/platform-host-capabilities-output"', cleanup_shell_block)
        self.assertIn('rmdir -- "$ssh_dir"', cleanup_shell_block)

        expected_line = (
            f"HOST_TOOLS schema=1 source_sha={SOURCE_SHA} generation={SOURCE_SHA} "
            "dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 release_baseline=1 "
            "retained_load_export_cleanup=1 retained_load_source_binding=1 cpu_diagnostic_plan_control=1 "
            "python_isolated=1 python_bytecode_disabled=1"
        )
        expected_payload = (expected_line + "\n").encode("ascii")
        expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
        expected_bytes = len(expected_payload)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            ssh_dir = runner_temp / "platform-host-capability-ssh"
            ssh_dir.mkdir(mode=0o700)
            for name in ("config", "known_hosts", "id_ed25519"):
                path = ssh_dir / name
                path.write_text("fixture\n", encoding="ascii")
                path.chmod(0o600)
            payload_path = root / "probe-payload"
            output_path = runner_temp / "platform-host-capabilities-output"
            invocation_log = root / "ssh-invocations.jsonl"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
destination_index = next(
    (index for index, argument in enumerate(arguments) if "@" in argument),
    None,
)
if destination_index is None:
    raise SystemExit("missing destination")
command = arguments[destination_index + 1 :]
expected_command = [
    "/usr/bin/python3.12",
    "-I",
    "-B",
    os.environ["FAKE_EXPECTED_DISPATCHER"],
    "host-capabilities",
]
if command != expected_command:
    raise SystemExit("unexpected remote command")
with Path(os.environ["FAKE_SSH_LOG"]).open("a", encoding="ascii") as log:
    json.dump(arguments, log)
    log.write("\\n")
if sys.stdin.read() != "":
    raise SystemExit("stdin was not at EOF")
stderr_text = os.environ.get("FAKE_SSH_STDERR", "")
if stderr_text:
    sys.stderr.write(stderr_text)
sys.stdout.buffer.write(Path(os.environ["FAKE_PROBE_PAYLOAD"]).read_bytes())
sys.stdout.flush()
raise SystemExit(int(os.environ.get("FAKE_SSH_RC", "0")))
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            self.assertEqual([path.name for path in fake_bin.iterdir()], ["ssh"])
            expected_generation = f"/opt/oldsparky/platform/shared/host-tools/{SOURCE_SHA}"
            expected_dispatcher = f"{expected_generation}/platform_workflow_remote_dispatch.py"
            env = os.environ.copy()
            env.update(
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_ENV": str(root / "github-env"),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": "fixture-key",
                    "HOST_TOOLS_SHA": SOURCE_SHA,
                    "HOST_TOOLS_GENERATION": expected_generation,
                    "HOST_TOOLS_DISPATCHER": expected_dispatcher,
                    "HOST_TOOLS_SSH_DIR": str(ssh_dir),
                    "FAKE_SSH_LOG": str(invocation_log),
                    "FAKE_PROBE_PAYLOAD": str(payload_path),
                    "FAKE_EXPECTED_DISPATCHER": expected_dispatcher,
                    "FAKE_SSH_STDERR": "",
                    "FAKE_SSH_RC": "0",
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                }
            )

            cleanup_files = (
                runner_temp / "platform-host-tools-artifact.zip",
                runner_temp / "platform-host-capabilities-output",
                runner_temp / "host-tools-remote-entries.b64",
                runner_temp / "host-tools-remote-entries",
                runner_temp / "platform-host-tools-expected-members",
                runner_temp / "platform-host-tools-actual-members",
            )
            cleanup_directories = (
                runner_temp / "host-tools-download",
                runner_temp / "platform-host-tools-inner",
            )
            for path in cleanup_files:
                path.write_bytes(b"fixture")
            for path in cleanup_directories:
                path.mkdir()

            def invoke(
                payload: bytes,
                *,
                stderr_text: str = "",
                ssh_rc: int = 0,
            ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
                payload_path.write_bytes(payload)
                invocation_log.write_text("", encoding="ascii")
                env["FAKE_SSH_STDERR"] = stderr_text
                env["FAKE_SSH_RC"] = str(ssh_rc)
                completed = subprocess.run(
                    ["bash", "-c", shell_block],
                    cwd=REPO_ROOT,
                    env=env,
                    input="",
                    capture_output=True,
                    text=True,
                    check=False,
                )
                invocations = [
                    json.loads(line)
                    for line in invocation_log.read_text(encoding="ascii").splitlines()
                ]
                return completed, invocations

            try:
                passed, invocations = invoke(expected_payload, stderr_text="RAW_STDERR_SENTINEL")
                self.assertEqual(passed.returncode, 0, passed.stderr)
                self.assertEqual(passed.stdout, "")
                self.assertEqual(passed.stderr, "")
                self.assertEqual(len(invocations), 1)
                self.assertIn("-n", invocations[0])
                self.assertIn("-T", invocations[0])
                self.assertEqual(
                    subprocess.run(
                        ["/usr/bin/stat", "-c", "%F:%h:%a", "--", str(ssh_dir)],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                    "directory:2:700",
                )
                self.assertEqual(
                    subprocess.run(
                        ["/usr/bin/stat", "-c", "%u:%g", "--", str(ssh_dir)],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                    f"{os.getuid()}:{os.getgid()}",
                )

                # The former assertion rejected the real directory created by
                # ``install -d`` (nlink 2) and exited before the SSH boundary.
                invocation_log.write_text("", encoding="ascii")
                old_base = shell_block.replace(
                    'stat -c \'%F:%a\' -- "$HOST_TOOLS_SSH_DIR"',
                    'stat -c \'%F:%h:%a\' -- "$HOST_TOOLS_SSH_DIR"',
                    1,
                ).replace('= "directory:700"', '= "directory:1:700"', 1)
                vulnerable = subprocess.run(
                    ["bash", "-c", old_base],
                    cwd=REPO_ROOT,
                    env=env,
                    input="",
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(vulnerable.returncode, 0)
                self.assertEqual(invocation_log.read_text(encoding="ascii"), "")

                malformed_payloads = {
                    "prepended banner": b"RAW_SENTINEL\n" + expected_payload,
                    "appended banner": expected_payload + b"RAW_SENTINEL\n",
                    "crlf": expected_payload.replace(b"\n", b"\r\n"),
                    "hostname": b"RAW_SENTINEL production.example.test\n",
                    "duplicate output": expected_payload + expected_payload,
                }
                diagnostic_pattern = re.compile(
                    r"immutable host capability probe diagnostics: command_rc=[0-9]+ "
                    rf"expected_bytes={expected_bytes} actual_bytes=[0-9]+ "
                    r"expected_sha256=[0-9a-f]{64} actual_sha256=[0-9a-f]{64} "
                    r"expected_cr_count=0 actual_cr_count=[0-9]+ "
                    r"expected_lf_count=1 actual_lf_count=[0-9]+ exact_one_line=[01]\n$"
                )
                for label, payload in malformed_payloads.items():
                    with self.subTest(payload=label):
                        failed, invocations = invoke(payload)
                        self.assertNotEqual(failed.returncode, 0)
                        self.assertEqual(failed.stdout, "")
                        self.assertEqual(len(invocations), 1)
                        self.assertRegex(failed.stderr, diagnostic_pattern)
                        self.assertIn(f"expected_sha256={expected_sha256}", failed.stderr)
                        self.assertNotIn("RAW_SENTINEL", failed.stderr)
                        self.assertNotIn(expected_line, failed.stderr)

                first_failure, _ = invoke(malformed_payloads["appended banner"])
                second_failure, _ = invoke(malformed_payloads["appended banner"])
                self.assertEqual(first_failure.stderr, second_failure.stderr)

                remote_failure, invocations = invoke(b"", ssh_rc=7)
                self.assertNotEqual(remote_failure.returncode, 0)
                self.assertEqual(len(invocations), 1)
                self.assertIn("command_rc=7", remote_failure.stderr)
                self.assertNotIn("RAW_SENTINEL", remote_failure.stderr)

                oversized, invocations = invoke(b"RAW_SENTINEL" * 200)
                self.assertNotEqual(oversized.returncode, 0)
                self.assertEqual(len(invocations), 1)
                self.assertLessEqual(output_path.stat().st_size, 512)
                self.assertRegex(oversized.stderr, diagnostic_pattern)
                self.assertNotIn("RAW_SENTINEL", oversized.stderr)
            finally:
                cleanup_result = subprocess.run(
                    ["bash", "-c", cleanup_shell_block],
                    cwd=REPO_ROOT,
                    env=env,
                    input="",
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(cleanup_result.returncode, 0, cleanup_result.stderr)
                for path in (*cleanup_files, *cleanup_directories, output_path, ssh_dir):
                    self.assertFalse(
                        path.exists() or path.is_symlink(),
                        f"cleanup left temporary artifact: {path}",
                    )

    def test_host_capability_probe_checks_closed_generation_metadata(self) -> None:
        pin_payload = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        expected_paths = {
            f"platform/tools/{name}" for name in bundle.HOST_TOOL_FILES
        }
        checked_in_pin_records = pin_payload["closure"]
        self.assertEqual(
            {record["path"] for record in checked_in_pin_records}, expected_paths
        )
        self.assertEqual(len(checked_in_pin_records), len(expected_paths))
        self.assertEqual(
            {record["mode"] for record in checked_in_pin_records}, {0o644, 0o755}
        )
        source_modes = {
            record["path"].removeprefix("platform/tools/"): record["mode"]
            for record in checked_in_pin_records
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            for name, mode in source_modes.items():
                os.chmod(source / "platform" / "tools" / name, mode)
            built = bundle.build_bundle(source, SOURCE_SHA, root / "generation.zip")
        generation_manifest = built["manifest"]
        self.assertEqual(
            {record["path"] for record in generation_manifest["files"]},
            set(bundle.HOST_TOOL_FILES) | {"capabilities.txt"},
        )
        self.assertEqual(
            {
                record["mode"]
                for record in generation_manifest["files"]
                if record["path"] != "capabilities.txt"
            },
            {0o555},
        )
        self.assertEqual(
            next(
                record["mode"]
                for record in generation_manifest["files"]
                if record["path"] == "capabilities.txt"
            ),
            0o444,
        )
        pin_records = [
            {
                "path": f"platform/tools/{record['path']}",
                "sha256": record["sha256"],
                "mode": source_modes[record["path"]],
            }
            for record in generation_manifest["files"]
            if record["path"] != "capabilities.txt"
        ]
        test_pin = {**pin_payload, "closure": list(reversed(pin_records))}
        self.assertTrue(
            dispatcher._pin_closure_matches_generation(test_pin, generation_manifest)
        )

        def rejects(
            *,
            source_records: list[dict[str, object]] | None = None,
            installed: list[dict[str, object]] | None = None,
        ) -> None:
            candidate_pin = {
                **test_pin,
                "closure": (
                    source_records if source_records is not None else test_pin["closure"]
                ),
            }
            candidate_manifest = {
                "files": (
                    installed if installed is not None else generation_manifest["files"]
                )
            }
            self.assertFalse(
                dispatcher._pin_closure_matches_generation(
                    candidate_pin, candidate_manifest
                )
            )

        changed_digest = [dict(record) for record in test_pin["closure"]]
        changed_digest[0]["sha256"] = "b" * 64
        rejects(source_records=changed_digest)
        rejects(source_records=[dict(record) for record in test_pin["closure"][:-1]])
        duplicate_source = [dict(record) for record in test_pin["closure"]]
        duplicate_source[-1] = dict(duplicate_source[0])
        rejects(source_records=duplicate_source)
        unexpected_source = [dict(record) for record in test_pin["closure"]]
        unexpected_source[0]["path"] = "platform/tools/unapproved.py"
        rejects(source_records=unexpected_source)
        bad_source_mode = [dict(record) for record in test_pin["closure"]]
        bad_source_mode[0]["mode"] = 0o666
        rejects(source_records=bad_source_mode)

        changed_installed_digest = [dict(record) for record in generation_manifest["files"]]
        tool_record_index = next(
            index
            for index, record in enumerate(changed_installed_digest)
            if record["path"] != "capabilities.txt"
        )
        changed_installed_digest[tool_record_index]["sha256"] = "c" * 64
        rejects(installed=changed_installed_digest)
        missing_installed = [dict(record) for record in generation_manifest["files"][:-1]]
        rejects(installed=missing_installed)
        duplicate_installed = [dict(record) for record in generation_manifest["files"]]
        duplicate_installed[-1] = dict(duplicate_installed[0])
        rejects(installed=duplicate_installed)
        unexpected_installed = [dict(record) for record in generation_manifest["files"]]
        unexpected_installed[-1]["path"] = "unexpected.txt"
        rejects(installed=unexpected_installed)
        bad_installed_mode = [dict(record) for record in generation_manifest["files"]]
        bad_installed_mode[0]["mode"] = 0o755
        rejects(installed=bad_installed_mode)

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
            owner_overrides: dict[Path, int] = {}

            def root_owned_lstat(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path == generation or path.parent == generation:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_uid=owner_overrides.get(path, 0),
                        st_gid=0,
                        st_nlink=metadata.st_nlink,
                    )
                return metadata

            output = StringIO()
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])), \
                patch.object(dispatcher, "_retained_load_export_owner", return_value={"uid": 65534, "gid": 65534}), \
                redirect_stdout(output):
                self.assertEqual(dispatcher._host_capabilities(), 0)
            self.assertRegex(output.getvalue(), r"^HOST_TOOLS schema=1 source_sha=[0-9a-f]{40} ")
            self.assertNotIn(str(generation), output.getvalue())
            owner_overrides[generation / bundle.HOST_TOOL_FILES[0]] = 1000
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])):
                self.assertEqual(dispatcher._host_capabilities(), 2)
            owner_overrides.clear()
            os.chmod(generation / bundle.HOST_TOOL_FILES[-1], 0o554)
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])):
                self.assertEqual(dispatcher._host_capabilities(), 2)

    def _candidate_event_fixture(self) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        base_sha = "1" * 40
        candidate_sha = "2" * 40
        merge_sha = "3" * 40
        repository_snapshot = {"name": "old_sparky"}
        pull_snapshot = {
            "number": 115,
            "head": {"ref": "codex/host-tools-bump", "sha": candidate_sha, "repo": repository_snapshot},
            "base": {"ref": "dev", "sha": base_sha, "repo": repository_snapshot},
        }
        run = {
            "id": 9001,
            "run_attempt": 2,
            "workflow_id": candidate.SECURITY_WORKFLOW_ID,
            "name": candidate.SECURITY_WORKFLOW_NAME,
            "path": candidate.SECURITY_WORKFLOW_PATH,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            # GitHub's workflow_run and exact-attempt job/artifact identities
            # carry the source head E. The current PR merge ref/commit below
            # establishes the separately tested synthetic merge M.
            "head_sha": candidate_sha,
            "head_branch": "codex/host-tools-bump",
            "repository": {"full_name": candidate.REPOSITORY},
            "head_repository": {"full_name": candidate.REPOSITORY},
            "pull_requests": [pull_snapshot],
        }
        event = {"workflow_run": {**run, "pull_requests": [pull_snapshot]}}
        repository = {
            "full_name": candidate.REPOSITORY,
            "owner": {"login": "StrayForest"},
        }
        pr = {
            "number": 115,
            "state": "open",
            "draft": False,
            "base": {"ref": "dev", "sha": base_sha, "repo": repository},
            "head": {
                "ref": "codex/host-tools-bump",
                "sha": candidate_sha,
                "label": "StrayForest:codex/host-tools-bump",
                "repo": repository,
            },
            "merge_commit_sha": merge_sha,
        }
        return event, run, pr

    def _candidate_context_files(
        self,
        root: Path,
        event: dict[str, object],
        run: dict[str, object],
        pr: dict[str, object],
    ) -> dict[str, Path]:
        merge_sha = str(pr["merge_commit_sha"])
        base_sha = str(pr["base"]["sha"])
        head_sha = str(pr["head"]["sha"])
        files = {
            "event": root / "event.json",
            "run": root / "run.json",
            "latest": root / "latest.json",
            "pr": root / "pr.json",
            "merge_ref": root / "merge-ref.json",
            "commit": root / "commit.json",
            "context": root / "context.json",
            "output": root / "output",
        }
        files["event"].write_text(json.dumps(event), encoding="utf-8")
        files["run"].write_text(json.dumps(run), encoding="utf-8")
        files["latest"].write_text(json.dumps(run), encoding="utf-8")
        files["pr"].write_text(json.dumps(pr), encoding="utf-8")
        files["merge_ref"].write_text(
            json.dumps(
                [
                    {
                        "ref": "refs/pull/115/merge",
                        "object": {"type": "commit", "sha": merge_sha},
                    }
                ]
            ),
            encoding="utf-8",
        )
        files["commit"].write_text(
            json.dumps(
                {
                    "sha": merge_sha,
                    "commit": {"tree": {"sha": "4" * 40}},
                    "parents": [{"sha": base_sha}, {"sha": head_sha}],
                }
            ),
            encoding="utf-8",
        )
        return files

    def test_candidate_event_context_binds_canonical_run_pr_and_base(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._candidate_context_files(root, event, run, pr)
            context = candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                output=paths["context"],
                latest_run_path=paths["latest"],
            )
            self.assertEqual(context.run_id, "9001")
            self.assertEqual(context.run_attempt, "2")
            self.assertEqual(context.original_source_head_sha, "2" * 40)
            self.assertEqual(context.original_base_sha, "1" * 40)
            self.assertEqual(context.tested_merge_sha, "3" * 40)
            self.assertEqual(context.tested_parents, ("1" * 40, "2" * 40))
            payload = json.loads(paths["context"].read_text())
            self.assertEqual(payload["pull_request"]["number"], 115)
            self.assertEqual(payload["security_run"]["head_sha"], "2" * 40)
            self.assertEqual(payload["tested_merge"]["sha"], "3" * 40)

    def test_candidate_context_rejects_empty_multiple_and_wrong_pr_associations(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        payload_mutations = (
            ("event-empty", [], run["pull_requests"]),
            ("event-multiple", [{"number": 115}, {"number": 115}], run["pull_requests"]),
            ("run-empty", event["workflow_run"]["pull_requests"], []),
            ("run-multiple", event["workflow_run"]["pull_requests"], [{"number": 115}, {"number": 115}]),
            ("event-wrong-association", [{"number": 116}], run["pull_requests"]),
            ("run-wrong-association", event["workflow_run"]["pull_requests"], [{"number": 116}]),
        )
        for label, event_rows, run_rows in payload_mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mutated_event = {"workflow_run": {**event["workflow_run"], "pull_requests": event_rows}}
                mutated_run = {**run, "pull_requests": run_rows}
                paths = self._candidate_context_files(root, mutated_event, mutated_run, pr)
                with self.assertRaises(candidate.CandidateError):
                    candidate.validate_context(paths["event"], paths["run"], paths["pr"], paths["merge_ref"], paths["commit"], latest_run_path=paths["latest"], output=root / "context.json")

    def test_candidate_context_rejects_stale_fork_ref_tag_merge_and_pr_state(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        mutations = (
            ("wrong-workflow", lambda value: {**value, "workflow_id": candidate.SECURITY_WORKFLOW_ID + 1}),
            ("wrong-conclusion", lambda value: {**value, "conclusion": "failure"}),
            ("wrong-event", lambda value: {**value, "event": "push"}),
            ("wrong-source-head", lambda value: {**value, "head_sha": "4" * 40}),
            ("merge-as-source-head", lambda value: {**value, "head_sha": "3" * 40}),
            ("fork-run", lambda value: {**value, "head_repository": {"full_name": "attacker/old_sparky"}}),
            ("missing-head-repository", lambda value: {key: item for key, item in value.items() if key != "head_repository"}),
        )
        for label, mutation in mutations:
            with self.subTest(label=label):
                mutated_run = mutation(run)
                mutated_event = {"workflow_run": {**mutated_run}}
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    paths = self._candidate_context_files(root, mutated_event, mutated_run, pr)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            paths["event"],
                            paths["run"],
                            paths["pr"],
                            paths["merge_ref"],
                            paths["commit"],
                            latest_run_path=paths["latest"],
                            output=root / "context.json",
                        )

        for label, mutation in (
            ("fork-pr", lambda value: {**value, "head": {**value["head"], "repo": {"full_name": "attacker/old_sparky", "owner": {"login": "attacker"}}}}),
            ("tag-ref", lambda value: {**value, "head": {**value["head"], "ref": "refs/tags/v1"}}),
            ("synthetic-merge", lambda value: {**value, "merge_commit_sha": value["head"]["sha"]}),
            ("missing-merge", lambda value: {**value, "merge_commit_sha": None}),
            ("draft", lambda value: {**value, "draft": True}),
            ("closed", lambda value: {**value, "state": "closed"}),
            ("wrong-base", lambda value: {**value, "base": {**value["base"], "ref": "main"}}),
            ("wrong-pr-number", lambda value: {**value, "number": 116}),
            ("stale-direct-head", lambda value: {**value, "head": {**value["head"], "sha": "3" * 40}}),
            ("stale-direct-ref", lambda value: {**value, "head": {**value["head"], "ref": "old-host-tools"}}),
        ):
            with self.subTest(label=label):
                mutated_pr = mutation(pr)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    paths = self._candidate_context_files(root, event, run, mutated_pr)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            paths["event"],
                            paths["run"],
                            paths["pr"],
                            paths["merge_ref"],
                            paths["commit"],
                            latest_run_path=paths["latest"],
                            output=root / "context.json",
                        )

    def test_candidate_context_rejects_merge_ref_parent_tree_and_source_substitution(self) -> None:
        """The source head and tested synthetic merge are distinct authorities."""

        event, run, pr = self._candidate_event_fixture()
        for label, mutate in (
            (
                "head-substitution",
                lambda files, payload: payload["workflow_run"]["pull_requests"][0]["head"].update(
                    {"sha": "5" * 40}
                ),
            ),
            (
                "base-substitution",
                lambda files, payload: payload["workflow_run"]["pull_requests"][0]["base"].update(
                    {"sha": "6" * 40}
                ),
            ),
            (
                "merge-ref-mismatch",
                lambda files, payload: payload[0]["object"].update({"sha": "7" * 40}),
            ),
            ("empty-merge-ref", lambda files, payload: payload.clear()),
            (
                "multiple-merge-refs",
                lambda files, payload: payload.append(payload[0]),
            ),
            (
                "commit-sha-mismatch",
                lambda files, payload: payload.update({"sha": "8" * 40}),
            ),
            (
                "wrong-parent-order",
                lambda files, payload: payload["parents"].reverse(),
            ),
            (
                "wrong-parent-count",
                lambda files, payload: payload["parents"].append({"sha": "8" * 40}),
            ),
            (
                "invalid-tree",
                lambda files, payload: payload["commit"]["tree"].update({"sha": "not-a-sha"}),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = self._candidate_context_files(root, event, run, pr)
                target = (
                    paths["event"]
                    if label in {"head-substitution", "base-substitution"}
                    else paths["merge_ref"]
                    if label in {"merge-ref-mismatch", "empty-merge-ref", "multiple-merge-refs"}
                    else paths["commit"]
                )
                payload = json.loads(target.read_text(encoding="utf-8"))
                mutate(paths, payload)
                target.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(candidate.CandidateError):
                    candidate.validate_context(
                        paths["event"],
                        paths["run"],
                        paths["pr"],
                        paths["merge_ref"],
                        paths["commit"],
                        latest_run_path=paths["latest"],
                        output=paths["output"],
                    )

    def test_candidate_context_recheck_rejects_pr_merge_tree_and_rerun_races(self) -> None:
        """A second API snapshot must remain byte-for-byte the same context."""

        event, run, pr = self._candidate_event_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._candidate_context_files(root, event, run, pr)
            candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                latest_run_path=paths["latest"],
                output=paths["context"],
            )
            partial_latest = {**run, "pull_requests": [{"number": 115}]}
            paths["latest"].write_text(json.dumps(partial_latest), encoding="utf-8")
            candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                latest_run_path=paths["latest"],
                expected_context_path=paths["context"],
                output=paths["output"],
            )
            for label, path_key, mutate in (
                (
                    "rerun-attempt",
                    "latest",
                    lambda payload: payload.update({"run_attempt": 3}),
                ),
                (
                    "base-race",
                    "pr",
                    lambda payload: payload["base"].update({"sha": "5" * 40}),
                ),
                (
                    "head-race",
                    "pr",
                    lambda payload: payload["head"].update({"sha": "6" * 40}),
                ),
                (
                    "merge-race",
                    "pr",
                    lambda payload: payload.update({"merge_commit_sha": "7" * 40}),
                ),
                (
                    "merge-ref-race",
                    "merge_ref",
                    lambda payload: payload[0]["object"].update({"sha": "8" * 40}),
                ),
                (
                    "tree-race",
                    "commit",
                    lambda payload: payload["commit"]["tree"].update({"sha": "9" * 40}),
                ),
            ):
                with self.subTest(label=label):
                    # Restore every input to the original snapshot before
                    # introducing one race at a time.
                    fresh = self._candidate_context_files(root, event, run, pr)
                    payload = json.loads(fresh[path_key].read_text(encoding="utf-8"))
                    mutate(payload)
                    fresh[path_key].write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            fresh["event"],
                            fresh["run"],
                            fresh["pr"],
                            fresh["merge_ref"],
                            fresh["commit"],
                            latest_run_path=fresh["latest"],
                            expected_context_path=paths["context"],
                            output=fresh["output"],
                        )

    def test_candidate_jobs_and_final_summary_are_closed_and_successful(self) -> None:
        context = candidate.RunContext(
            repository=candidate.REPOSITORY,
            workflow_id=candidate.SECURITY_WORKFLOW_ID,
            workflow_name=candidate.SECURITY_WORKFLOW_NAME,
            workflow_path=candidate.SECURITY_WORKFLOW_PATH,
            run_id="9001",
            run_attempt="2",
            pull_request="115",
            original_source_head_sha="2" * 40,
            original_base_sha="1" * 40,
            original_base_ref="dev",
            original_head_ref="codex/host-tools-bump",
            original_base_repository=candidate.REPOSITORY,
            original_head_repository=candidate.REPOSITORY,
            tested_merge_ref="refs/pull/115/merge",
            tested_merge_sha="3" * 40,
            tested_tree_sha="4" * 40,
            tested_parents=("1" * 40, "2" * 40),
        )
        summary = {
            "schema": 1,
            "tested_sha": context.tested_merge_sha,
            "event": "pull_request",
            "route_event": "pull_request",
            "class": "full",
            "reason": "trusted candidate-packaging change requires full verification and is non-deployable",
            "deployable": False,
            "fallback": False,
            "manifest_digest": "a" * 64,
            "expected_gates": list(candidate.FULL_GATE_IDS),
            "gate_results": {gate: "success" for gate in candidate.FULL_GATE_IDS},
            "conditional_gate_results": {"release-runtime": "skipped", "release-runtime-real": "skipped"},
            "runtime_sensitive": False,
            "requires_release_runtime": False,
            "requires_real_release_runtime": False,
            "missing_or_failed": [],
            "route_errors": [],
            "status_start_result": "skipped",
            "passed": True,
        }
        jobs = [
            {
                "id": index + 1,
                "name": name,
                "run_id": 9001,
                "run_attempt": 2,
                "head_sha": context.original_source_head_sha,
                "head_branch": context.original_head_ref,
                "workflow_name": candidate.SECURITY_WORKFLOW_NAME,
                "check_run_url": f"https://api.github.com/repos/StrayForest/old_sparky/check-runs/{index + 100}",
                "status": "completed",
                "conclusion": "skipped" if name in candidate.CONDITIONAL_JOB_NAMES else "success",
            }
            for index, name in enumerate(sorted(candidate.EXPECTED_JOB_NAMES))
        ]
        self.assertEqual(
            candidate.PR_ALWAYS_SKIPPED_JOB_NAMES,
            {
                "Authenticate internal baseline runtime proof",
                "Dispatch exact baseline proof finalizer",
            },
        )
        self.assertTrue(candidate.PR_ALWAYS_SKIPPED_JOB_NAMES.issubset(candidate.EXPECTED_JOB_NAMES))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.json"
            path.write_text(json.dumps({"total_count": len(jobs), "jobs": jobs}), encoding="utf-8")
            candidate.verify_jobs(path, context, summary)
            for name in sorted(candidate.PR_ALWAYS_SKIPPED_JOB_NAMES):
                changed_jobs = [
                    {**job, "conclusion": "success"} if job["name"] == name else job
                    for job in jobs
                ]
                path.write_text(json.dumps({"total_count": len(changed_jobs), "jobs": changed_jobs}), encoding="utf-8")
                with self.subTest(unexpected_pr_job=name):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)
            for label, changed in (
                ("extra-job", {**jobs[0], "name": "attacker-job"}),
                ("failed-required", {**jobs[0], "conclusion": "failure"}),
            ):
                bad_jobs = list(jobs)
                bad_jobs[0] = changed
                path.write_text(json.dumps({"total_count": len(bad_jobs), "jobs": bad_jobs}), encoding="utf-8")
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)
            for label, field, value in (
                ("deployable", "deployable", True),
                ("wrong-class", "class", "docs-only"),
                ("missing-failure", "missing_or_failed", ["security"]),
            ):
                bad_summary = {**summary, field: value}
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._validate_summary(bad_summary, context)

            # The exact-attempt jobs endpoint is the authority for each row's
            # identity.  A successful row from another run/attempt, source,
            # branch, or workflow is not interchangeable with this run.
            for label, field, value in (
                ("cross-run", "run_id", 9002),
                ("cross-attempt", "run_attempt", 3),
                ("source-head", "head_sha", "5" * 40),
                ("merge-as-head", "head_sha", context.tested_merge_sha),
                ("source-branch", "head_branch", "other-branch"),
                ("workflow", "workflow_name", "Other workflow"),
                ("check-run-url", "check_run_url", "https://evil.example/check-runs/1"),
            ):
                bad_jobs = list(jobs)
                bad_jobs[0] = {**bad_jobs[0], field: value}
                path.write_text(json.dumps({"total_count": len(bad_jobs), "jobs": bad_jobs}), encoding="utf-8")
                with self.subTest(job_identity=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)

            split_summary = {
                **summary,
                "source_head_sha": context.original_source_head_sha,
                "base_sha": context.original_base_sha,
                "tested_tree_sha": context.tested_tree_sha,
                "tested_parents": list(context.tested_parents),
            }
            candidate._validate_summary(split_summary, context)
            alias_split_summary = {
                **summary,
                "source_sha": context.original_source_head_sha,
                "base_sha": context.original_base_sha,
                "tree_sha": context.tested_tree_sha,
                "parents": list(context.tested_parents),
            }
            candidate._validate_summary(alias_split_summary, context)
            run_proof_summary = {
                **summary,
                "proof_mode": "standard",
                "proof_run_id": context.run_id,
                "proof_run_attempt": context.run_attempt,
                "baseline_guard_result": "skipped",
            }
            candidate._validate_summary(run_proof_summary, context)
            for label, changed in (
                ("arbitrary-tested-sha", {**summary, "tested_sha": "9" * 40}),
                ("source-as-tested-sha", {**summary, "tested_sha": context.original_source_head_sha}),
                ("malicious-extra", {**summary, "untrusted": "accepted"}),
                ("schema", {**summary, "schema": 2}),
                (
                    "split-source-mismatch",
                    {**split_summary, "source_head_sha": "5" * 40},
                ),
                (
                    "split-parent-order",
                    {**split_summary, "tested_parents": list(reversed(context.tested_parents))},
                ),
                (
                    "split-extra-key",
                    {**split_summary, "unexpected": True},
                ),
                (
                    "proof-run-id",
                    {**run_proof_summary, "proof_run_id": "9002"},
                ),
                (
                    "proof-run-id-type",
                    {**run_proof_summary, "proof_run_id": 9001},
                ),
                (
                    "proof-run-attempt",
                    {**run_proof_summary, "proof_run_attempt": "3"},
                ),
                (
                    "proof-run-attempt-bool",
                    {**run_proof_summary, "proof_run_attempt": True},
                ),
                (
                    "proof-mode",
                    {**run_proof_summary, "proof_mode": "baseline-reconcile"},
                ),
                (
                    "proof-mode-type",
                    {**run_proof_summary, "proof_mode": []},
                ),
                (
                    "proof-baseline-guard",
                    {**run_proof_summary, "baseline_guard_result": "success"},
                ),
                (
                    "proof-baseline-guard-type",
                    {**run_proof_summary, "baseline_guard_result": None},
                ),
                (
                    "proof-extra-key",
                    {**run_proof_summary, "untrusted": True},
                ),
            ):
                with self.subTest(summary_identity=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._validate_summary(changed, context)

    def test_candidate_artifacts_bind_route_target_digest_and_attempt(self) -> None:
        """Classifier output is data-only and must target the tested merge."""

        context = candidate.RunContext(
            repository=candidate.REPOSITORY,
            workflow_id=candidate.SECURITY_WORKFLOW_ID,
            workflow_name=candidate.SECURITY_WORKFLOW_NAME,
            workflow_path=candidate.SECURITY_WORKFLOW_PATH,
            run_id="9001",
            run_attempt="2",
            pull_request="115",
            original_source_head_sha="2" * 40,
            original_base_sha="1" * 40,
            original_base_ref="dev",
            original_head_ref="codex/host-tools-bump",
            original_base_repository=candidate.REPOSITORY,
            original_head_repository=candidate.REPOSITORY,
            tested_merge_ref="refs/pull/115/merge",
            tested_merge_sha="3" * 40,
            tested_tree_sha="4" * 40,
            tested_parents=("1" * 40, "2" * 40),
        )
        manifest_base = {
            "schema": 1,
            "version": 1,
            "target_sha": context.tested_merge_sha,
            "event": "pull_request",
            "class": "full",
            "expected_gates": list(candidate.FULL_GATE_IDS),
            "runtime_sensitive": False,
            "deployable": False,
            "fallback": False,
            "reason": "candidate requires full verification",
            "files": ["platform-ci-route.json"],
        }

        def write_route_fixture(
            root: Path,
            manifest: dict[str, object],
            *,
            attempt: int = 2,
            artifact_head_sha: str | None = None,
        ) -> tuple[Path, Path]:
            manifest = {**manifest}
            manifest["digest"] = candidate._route_manifest_digest(manifest)
            archive = root / f"route-{attempt}.zip"
            member = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr(candidate.ROUTE_MANIFEST_MEMBER, member)
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata = root / f"route-{attempt}.json"
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-ci-route-9001-2",
                        "expired": False,
                        "size_in_bytes": archive.stat().st_size,
                        "digest": f"sha256:{archive_digest}",
                        "workflow_run": {
                            "id": 9001,
                            "run_attempt": attempt,
                            "head_sha": artifact_head_sha or context.original_source_head_sha,
                            "head_branch": context.original_head_ref,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return metadata, archive

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path = root / "context.json"
            context_path.write_text(json.dumps(context.as_payload()), encoding="utf-8")
            metadata, archive = write_route_fixture(root, manifest_base)
            listing = root / "route-list.json"
            listing.write_text(
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "id": 123,
                                "name": "platform-ci-route-9001-2",
                                "expired": False,
                                "workflow_run": {"id": 9001, "run_attempt": 2},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                candidate.select_artifact_id(
                    listing,
                    name="platform-ci-route-9001-2",
                    run_id="9001",
                    run_attempt="2",
                ),
                "123",
            )
            result = candidate.verify_route_artifact(
                context_path,
                metadata,
                archive,
                expected_artifact_id="123",
                expected_manifest_digest=candidate._route_manifest_digest(manifest_base),
            )
            self.assertEqual(result["manifest"]["target_sha"], context.tested_merge_sha)
            bad_metadata, bad_archive = write_route_fixture(
                root,
                manifest_base,
                artifact_head_sha=context.tested_merge_sha,
            )
            with self.assertRaises(candidate.CandidateError):
                candidate.verify_route_artifact(
                    context_path,
                    bad_metadata,
                    bad_archive,
                    expected_artifact_id="123",
                    expected_manifest_digest=candidate._route_manifest_digest(manifest_base),
                )
            for label, manifest, expected_digest, attempt in (
                (
                    "source-head-target",
                    {**manifest_base, "target_sha": context.original_source_head_sha},
                    candidate._route_manifest_digest(
                        {**manifest_base, "target_sha": context.original_source_head_sha}
                    ),
                    2,
                ),
                (
                    "summary-digest-mismatch",
                    manifest_base,
                    "f" * 64,
                    2,
                ),
                (
                    "cross-attempt-artifact",
                    manifest_base,
                    candidate._route_manifest_digest(manifest_base),
                    3,
                ),
                (
                    "extra-manifest-key",
                    {**manifest_base, "unexpected": True},
                    candidate._route_manifest_digest({**manifest_base, "unexpected": True}),
                    2,
                ),
            ):
                with self.subTest(route_identity=label):
                    bad_metadata, bad_archive = write_route_fixture(root, manifest, attempt=attempt)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_route_artifact(
                            context_path,
                            bad_metadata,
                            bad_archive,
                            expected_artifact_id="123",
                            expected_manifest_digest=expected_digest,
                        )

    def test_candidate_artifact_metadata_binds_outer_digest_size_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            archive.write_bytes(b"exact outer artifact")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            payload = {
                "id": 123,
                "name": "candidate",
                "expired": False,
                "digest": f"sha256:{digest}",
                "size_in_bytes": archive.stat().st_size,
                "workflow_run": {
                    "id": 456,
                    "run_attempt": 3,
                    "head_sha": "a" * 40,
                    "head_branch": "dev",
                },
            }
            self.assertEqual(
                candidate._verify_artifact_metadata(
                    payload,
                    archive,
                    expected_id="123",
                    expected_name="candidate",
                    expected_run_id="456",
                    expected_run_attempt="3",
                    expected_head_sha="a" * 40,
                    expected_head_ref="dev",
                ),
                f"sha256:{digest}",
            )
            outer = root / "outer.zip"
            with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr("platform-host-tools-bundle.zip", b"inner bundle")
            candidate._verify_closed_archive(
                outer,
                expected_member="platform-host-tools-bundle.zip",
                maximum_member_bytes=1024,
                expected_member_digest=hashlib.sha256(b"inner bundle").hexdigest(),
            )
            with zipfile.ZipFile(outer, "a", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr("unexpected", b"extra")
            with self.assertRaises(candidate.CandidateError):
                candidate._verify_closed_archive(
                    outer,
                    expected_member="platform-host-tools-bundle.zip",
                    maximum_member_bytes=1024,
                )
            for label, mutation in (
                ("digest", lambda value: {**value, "digest": "sha256:" + "0" * 64}),
                ("size", lambda value: {**value, "size_in_bytes": value["size_in_bytes"] + 1}),
                ("attempt", lambda value: {**value, "workflow_run": {**value["workflow_run"], "run_attempt": 4}}),
            ):
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._verify_artifact_metadata(
                            mutation(payload),
                            archive,
                            expected_id="123",
                            expected_name="candidate",
                            expected_run_id="456",
                            expected_run_attempt="3",
                            expected_head_sha="a" * 40,
                            expected_head_ref="dev",
                        )

    def test_candidate_ancestry_accepts_pr_introduced_pin_after_merge_sync(self) -> None:
        """Model PR115's post-dev-merge pin and reject PR116's old pin."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "synthetic-repository"

            def git(*arguments: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(repository), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            def commit(name: str, filename: str, contents: str) -> str:
                (repository / filename).write_text(contents, encoding="ascii")
                git("add", filename)
                git("commit", "-m", name)
                return git("rev-parse", "HEAD")

            repository.mkdir()
            git("init", "--initial-branch=dev")
            git("config", "user.email", "candidate-tests@example.invalid")
            git("config", "user.name", "Candidate tests")
            root_commit = commit("synthetic root", "root.txt", "root\n")
            old_pin = commit("PR116 old pinned generation 4233", "old.txt", "old\n")
            base_before_sync = commit("base before PR branch", "base.txt", "base\n")

            git("switch", "-c", "pr115")
            introduced_pin = commit("PR115 introduces host-tools generation C", "introduced.txt", "C\n")
            git("switch", "dev")
            current_base = commit("current dev advances", "dev.txt", "dev\n")
            git("switch", "pr115")
            git("merge", "--no-ff", "dev", "-m", "PR115 merge-syncs current dev")
            candidate_head = git("rev-parse", "HEAD")

            # Merge-syncing current dev leaves base reachable from E, while
            # the newly introduced C remains outside base's reachable set.
            self.assertTrue(
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=introduced_pin,
                        source_head_sha=candidate_head,
                )
            )

            # PR116's old 4233 pin is still an ancestor of E, but current dev
            # already reaches it; it is not a PR-introduced generation.
            self.assertFalse(
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=old_pin,
                        source_head_sha=candidate_head,
                )
            )

            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha="malformed-host-tools-pin",
                        source_head_sha=candidate_head,
                )

            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=candidate_head,
                        source_head_sha=candidate_head,
                )

            git("switch", "-c", "unrelated", root_commit)
            unrelated_pin = commit("unrelated host-tools generation", "unrelated.txt", "unrelated\n")
            unrelated_base = commit("unrelated current base", "other-base.txt", "other\n")
            git("switch", "pr115")
            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=unrelated_pin,
                        source_head_sha=candidate_head,
                )
            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=unrelated_base,
                        host_tools_sha=introduced_pin,
                        source_head_sha=candidate_head,
                )

            # Every SHA is explicit; this test never depends on the checkout's
            # own history or on symbolic parent expressions.
            self.assertNotEqual(base_before_sync, current_base)

    def test_candidate_workflow_is_workflow_run_only_trusted_and_non_deployable(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-host-tools-candidate.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_run:", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("\n  pull_request:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("github.event.workflow_run.head_sha", workflow)
        self.assertIn("github.event.workflow_run.pull_requests[0].number", workflow)
        exact_jobs_endpoint = "/attempts/$SECURITY_RUN_ATTEMPT/jobs?per_page=100&page=1"
        self.assertIn(exact_jobs_endpoint, workflow)
        self.assertNotIn("actions/runs/$SECURITY_RUN_ID/jobs?filter=latest", workflow)
        self.assertNotIn("actions/runs/$SECURITY_RUN_ID/pull_requests", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("statuses: write", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("overwrite: false", workflow)
        self.assertIn("retention-days: 30", workflow)
        self.assertIn("Attest exact inner host-tools ZIP", workflow)
        self.assertIn("Verify uploaded evidence artifact envelope", workflow)
        self.assertIn("github.ref == 'refs/heads/dev'", workflow)
        self.assertIn("Recheck PR, security run, attempt, and head before attestation", workflow)
        self.assertIn("Recheck PR, security run, attempt, and head before upload", workflow)
        self.assertNotRegex(workflow, r"python3[^\n]*candidate-data/platform/")
        latest_jobs_workflow = workflow.replace(
            "$api/actions/runs/$SECURITY_RUN_ID" + exact_jobs_endpoint,
            "$api/actions/runs/$SECURITY_RUN_ID/jobs?filter=latest&per_page=100",
        )
        self.assertNotEqual(latest_jobs_workflow, workflow)
        self.assertTrue(host_tools_candidate_workflow_issues(latest_jobs_workflow))
        production_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO_ROOT / ".github/workflows").glob("platform-production-*.yml")
        )
        self.assertNotIn(candidate.CANDIDATE_ARTIFACT_PREFIX, production_text)
        self.assertNotIn(candidate.EVIDENCE_ARTIFACT_PREFIX, production_text)
        self.assertEqual(host_tools_candidate_workflow_issues(), [])
        candidate_job = re.search(
            r"^  build-candidate:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(candidate_job)
        assert candidate_job is not None
        blocks = list(_workflow_step_blocks(candidate_job.group("body")))
        names = [
            re.match(r"^      - name: (?P<name>[^\n]+)", block, re.MULTILINE).group("name")
            for block in blocks
        ]
        attestation_index = names.index("Attest exact inner host-tools ZIP")
        attestation_recheck_index = names.index(
            "Recheck PR, security run, attempt, and head before attestation"
        )
        blocks[attestation_index], blocks[attestation_recheck_index] = (
            blocks[attestation_recheck_index],
            blocks[attestation_index],
        )
        broken_order = (
            workflow[: candidate_job.start("body")]
            + "".join(blocks)
            + workflow[candidate_job.end("body") :]
        )
        self.assertTrue(
            any(
                "must recheck before Attest exact inner host-tools ZIP" in issue
                for issue in host_tools_candidate_workflow_issues(broken_order)
            )
        )
        artifact_zip_blocks = host_tools_candidate_artifact_zip_curl_blocks(workflow)
        self.assertEqual(len(artifact_zip_blocks), 4)
        for block in artifact_zip_blocks:
            broken_workflow = workflow.replace(
                block,
                block.replace("application/vnd.github+json", "application/zip", 1),
                1,
            )
            self.assertTrue(
                any(
                    "must request application/vnd.github+json" in issue
                    for issue in host_tools_candidate_workflow_issues(broken_workflow)
                ),
                block,
            )
        self._assert_artifact_zip_transport_contract()

        # Exercise the exact trusted workflow invocation in isolated,
        # bytecode-free mode.  A candidate-side module with the same name is
        # deliberately present and must never satisfy the trusted import.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted_tools = root / "trusted-dev/platform/tools"
            trusted_tools.mkdir(parents=True)
            shutil.copyfile(TOOLS_ROOT / "platform_host_tools_candidate.py", trusted_tools / "platform_host_tools_candidate.py")
            shutil.copyfile(TOOLS_ROOT / "platform_host_tools_bundle.py", trusted_tools / "platform_host_tools_bundle.py")
            candidate_tools = root / "candidate-data/platform/tools"
            candidate_tools.mkdir(parents=True)
            (candidate_tools / "platform_host_tools_bundle.py").write_text(
                "raise RuntimeError('candidate code must never load')\n",
                encoding="ascii",
            )
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "workflow_run": {
                            "repository": {"full_name": "StrayForest/old_sparky"},
                            "workflow_id": 339062797,
                            "name": "Platform security and build",
                            "path": ".github/workflows/platform-security.yml",
                            "event": "pull_request",
                            "status": "completed",
                            "conclusion": "success",
                            "id": 36289064582,
                            "run_attempt": 1,
                            "head_sha": "a" * 40,
                            "head_branch": "feature/candidate",
                            "head_repository": {"full_name": "StrayForest/old_sparky"},
                            "pull_requests": [
                                {
                                    "number": 117,
                                    "base": {
                                        "ref": "dev",
                                        "sha": "b" * 40,
                                        "repo": {"name": "old_sparky"},
                                    },
                                    "head": {
                                        "ref": "feature/candidate",
                                        "sha": "a" * 40,
                                        "repo": {"name": "old_sparky"},
                                    },
                                }
                            ],
                        }
                    }
                ),
                encoding="ascii",
            )
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "trusted-dev/platform/tools/platform_host_tools_candidate.py",
                    "inspect-event",
                    "--event",
                    str(event),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(candidate_tools)},
                cwd=root,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('"source_head_sha":"' + "a" * 40 + '"', completed.stdout)
            self.assertFalse((trusted_tools / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
