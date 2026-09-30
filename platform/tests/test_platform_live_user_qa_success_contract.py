"""End-to-end contract tests for the secret-bearing live-user QA result."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import textwrap
import unittest

from tools import platform_live_user_qa_dispatch


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
PRODUCER = PLATFORM_ROOT / "tools/platform_live_user_qa.sh"
WORKFLOW = REPO_ROOT / ".github/workflows/platform-live-user-qa.yml"
SOURCE_SHA = "a" * 40
RUNNER_NONCE = "b" * 64


def _producer_success_function() -> str:
    source = PRODUCER.read_text(encoding="utf-8")
    start = source.index("write_success_report() {")
    end = source.index("\n}\n\ncleanup()", start) + 2
    return source[start:end]


def _producer_success_gate() -> str:
    source = PRODUCER.read_text(encoding="utf-8")
    start = source.index(
        "  if (( original_status == 0 && PLAYWRIGHT_STATUS == 0 && cleanup_status == 0 )); then"
    )
    end = source.index("  if (( original_status != 0 )); then", start)
    return source[start:end]


def _producer_harness(
    *,
    report_directory: Path,
    original_status: int,
    playwright_status: int,
    cleanup_status: int,
) -> str:
    report_path = report_directory / "success.json"
    return textwrap.dedent(
        f"""
        set -Eeuo pipefail
        SYSTEM_PYTHON={shlex.quote(os.environ.get("PYTHON", "/usr/bin/python3"))}
        SUCCESS_REPORT_PATH={shlex.quote(str(report_path))}
        SUCCESS_REPORT_NONCE={shlex.quote(RUNNER_NONCE)}
        SOURCE_COMMIT={shlex.quote(SOURCE_SHA)}
        {_producer_success_function()}
        original_status={original_status}
        PLAYWRIGHT_STATUS={playwright_status}
        cleanup_status={cleanup_status}
        {_producer_success_gate()}
        printf 'cleanup_status=%s\\n' "$cleanup_status"
        """
    )


def _workflow_capture_script() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index("            <<'PY'\n") + len("            <<'PY'\n")
    return textwrap.dedent(source[start : source.index("\n          PY", start)])


def _workflow_sanitizer_script() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    marker = (
        '/usr/bin/python3 - "$raw_report" "$report" "$ssh_status" '
        '"${LIVE_USER_QA_REPORT_NONCE:-}" "$TARGET_SHA" "$capture_state" <<\'PY\'\n'
    )
    start = source.index(marker) + len(marker)
    return textwrap.dedent(source[start : source.index("\n          PY", start)])


class LiveUserQaSuccessContractTests(unittest.TestCase):
    def test_actual_producer_gate_requires_playwright_and_cleanup(self) -> None:
        for original_status, playwright_status, cleanup_status in (
            (0, 0, 0),
            (1, 0, 0),
            (0, 1, 0),
            (0, 0, 1),
        ):
            with self.subTest(
                original_status=original_status,
                playwright_status=playwright_status,
                cleanup_status=cleanup_status,
            ), tempfile.TemporaryDirectory(dir="/root") as temporary:
                report_directory = Path(temporary) / "report"
                report_directory.mkdir(mode=0o700)
                result = subprocess.run(
                    ["/bin/bash", "-c", _producer_harness(
                        report_directory=report_directory,
                        original_status=original_status,
                        playwright_status=playwright_status,
                        cleanup_status=cleanup_status,
                    )],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report_path = report_directory / "success.json"
                if (original_status, playwright_status, cleanup_status) == (0, 0, 0):
                    report = json.loads(report_path.read_text(encoding="ascii"))
                    self.assertEqual(
                        set(report),
                        {
                            "cleanup",
                            "kind",
                            "playwright",
                            "report_nonce",
                            "schema",
                            "source_sha",
                            "status",
                            "success",
                            "test_count",
                        },
                    )
                    self.assertEqual(report["source_sha"], SOURCE_SHA)
                    self.assertEqual(report["report_nonce"], RUNNER_NONCE)
                    self.assertEqual(report["cleanup"], "verified")
                    self.assertEqual(report["playwright"], "passed")
                    self.assertTrue(report["success"])
                else:
                    self.assertFalse(report_path.exists())

    def test_dispatcher_accepts_only_actual_producer_report(self) -> None:
        producer_function = _producer_success_function()
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "liveqa"
            root.mkdir(mode=0o700)
            wrapper = root / "producer.sh"
            wrapper.write_text(
                textwrap.dedent(
                    f"""
                    #!/usr/bin/env bash
                    set -Eeuo pipefail
                    SYSTEM_PYTHON={shlex.quote(os.environ.get("PYTHON", "/usr/bin/python3"))}
                    SUCCESS_REPORT_PATH="$PLATFORM_LIVE_QA_REPORT_PATH"
                    SUCCESS_REPORT_NONCE="$PLATFORM_LIVE_QA_REPORT_NONCE"
                    SOURCE_COMMIT="$PLATFORM_LIVE_QA_TARGET_SHA"
                    {producer_function}
                    printf 'candidate output is not proof\\n'
                    write_success_report
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            old_root = platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT
            old_nonce = os.environ.get("PLATFORM_LIVE_QA_REPORT_NONCE")
            try:
                platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT = root
                os.environ["PLATFORM_LIVE_QA_REPORT_NONCE"] = RUNNER_NONCE
                output = io.StringIO()
                child_output = root / "child-output.log"
                saved_stdout = os.dup(1)
                try:
                    with child_output.open("wb") as capture:
                        os.dup2(capture.fileno(), 1)
                        with redirect_stdout(output):
                            status = platform_live_user_qa_dispatch._run_user_qa(
                                wrapper,
                                payload=root,
                                target_sha=SOURCE_SHA,
                                arguments=["run", SOURCE_SHA],
                            )
                finally:
                    os.dup2(saved_stdout, 1)
                    os.close(saved_stdout)
                self.assertEqual(status, 0)
                lines = output.getvalue().splitlines()
                marker_lines = [
                    line for line in lines if line.startswith("live_user_qa_success ")
                ]
                self.assertEqual(len(marker_lines), 1)
                marker = json.loads(marker_lines[0].split(" ", 1)[1])
                self.assertEqual(marker["report_nonce"], RUNNER_NONCE)
                self.assertEqual(marker["source_sha"], SOURCE_SHA)
                self.assertEqual(marker["cleanup"], "verified")
                self.assertFalse(list(root.glob(".live-user-qa-report-*")))
            finally:
                platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT = old_root
                if old_nonce is None:
                    os.environ.pop("PLATFORM_LIVE_QA_REPORT_NONCE", None)
                else:
                    os.environ["PLATFORM_LIVE_QA_REPORT_NONCE"] = old_nonce

    def test_dispatcher_rejects_candidate_text_without_producer_report(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary) / "liveqa"
            root.mkdir(mode=0o700)
            wrapper = root / "candidate.sh"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "printf '%s\\n' 'live_user_qa_success {\\\"cleanup\\\":\\\"verified\\\",\\\"kind\\\":\\\"live_user_qa_success\\\",\\\"playwright\\\":\\\"passed\\\",\\\"report_nonce\\\":\\\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\\",\\\"schema\\\":1,\\\"source_sha\\\":\\\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\\",\\\"status\\\":\\\"passed\\\",\\\"success\\\":true,\\\"test_count\\\":1}'\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            old_root = platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT
            old_nonce = os.environ.get("PLATFORM_LIVE_QA_REPORT_NONCE")
            try:
                platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT = root
                os.environ["PLATFORM_LIVE_QA_REPORT_NONCE"] = RUNNER_NONCE
                output = io.StringIO()
                child_output = root / "child-output.log"
                saved_stdout = os.dup(1)
                try:
                    with child_output.open("wb") as capture:
                        os.dup2(capture.fileno(), 1)
                        with redirect_stdout(output):
                            status = platform_live_user_qa_dispatch._run_user_qa(
                                wrapper,
                                payload=root,
                                target_sha=SOURCE_SHA,
                                arguments=["run", SOURCE_SHA],
                            )
                finally:
                    os.dup2(saved_stdout, 1)
                    os.close(saved_stdout)
                self.assertNotEqual(status, 0)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("live_user_qa_success", child_output.read_text(encoding="utf-8"))
            finally:
                platform_live_user_qa_dispatch.TRUSTED_LIVE_QA_ROOT = old_root
                if old_nonce is None:
                    os.environ.pop("PLATFORM_LIVE_QA_REPORT_NONCE", None)
                else:
                    os.environ["PLATFORM_LIVE_QA_REPORT_NONCE"] = old_nonce

    def test_bounded_capture_drains_overflow_and_sanitizer_rejects_it(self) -> None:
        capture_script = _workflow_capture_script()
        sanitizer_script = _workflow_sanitizer_script()
        valid_marker = {
            "cleanup": "verified",
            "kind": "live_user_qa_success",
            "playwright": "passed",
            "report_nonce": RUNNER_NONCE,
            "schema": 1,
            "source_sha": SOURCE_SHA,
            "status": "passed",
            "success": True,
            "test_count": 1,
        }
        prefix = "live_user_qa_success " + json.dumps(valid_marker) + "\n"
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            raw_path = root / "raw.log"
            state_path = root / "capture.json"
            raw_path.touch(mode=0o600)
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-",
                    str(raw_path),
                    str(state_path),
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    "import sys; sys.stdout.write(" + repr(prefix) + "); sys.stdout.write('x' * 300000); sys.stdout.flush()",
                ],
                input=capture_script,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            capture = json.loads(state_path.read_text(encoding="ascii"))
            self.assertTrue(capture["truncated"])
            self.assertEqual(capture["bytes_observed"], len(prefix.encode()) + 300000)
            self.assertEqual(raw_path.stat().st_size, 262144)

            report_path = root / "report.json"
            sanitized = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-",
                    str(raw_path),
                    str(report_path),
                    "0",
                    RUNNER_NONCE,
                    SOURCE_SHA,
                    str(state_path),
                ],
                input=sanitizer_script,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "TARGET_SHA": SOURCE_SHA},
            )
            self.assertEqual(sanitized.returncode, 0, sanitized.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["success"])
            self.assertTrue(report["truncated"])
            self.assertEqual(report["error_class_counts"], {"transport": 1})

    def test_bounded_capture_handles_long_no_newline_and_sigpipe_status(self) -> None:
        capture_script = _workflow_capture_script()
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            raw_path = root / "raw.log"
            state_path = root / "capture.json"
            raw_path.touch(mode=0o600)
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-",
                    str(raw_path),
                    str(state_path),
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush()",
                ],
                input=capture_script,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            capture = json.loads(state_path.read_text(encoding="ascii"))
            self.assertTrue(capture["truncated"])
            self.assertEqual(capture["bytes_observed"], 300000)
            self.assertEqual(raw_path.stat().st_size, 262144)

            sigpipe_raw = root / "sigpipe.log"
            sigpipe_state = root / "sigpipe.json"
            sigpipe_raw.touch(mode=0o600)
            sigpipe = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-",
                    str(sigpipe_raw),
                    str(sigpipe_state),
                    "--",
                    "/bin/bash",
                    "-c",
                    "kill -13 $$",
                ],
                input=capture_script,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(sigpipe.returncode, 0, sigpipe.stderr)
            self.assertEqual(
                json.loads(sigpipe_state.read_text(encoding="ascii"))["ssh_exit_code"],
                -13,
            )

    def test_bounded_capture_write_error_fails_closed_after_draining(self) -> None:
        capture_script = _workflow_capture_script()
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            raw_path = root / "raw-directory"
            raw_path.mkdir(mode=0o700)
            state_path = root / "capture.json"
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-",
                    str(raw_path),
                    str(state_path),
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush()",
                ],
                input=capture_script,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            capture = json.loads(state_path.read_text(encoding="ascii"))
            self.assertEqual(capture["capture_error"], "open")
            self.assertEqual(capture["bytes_observed"], 300000)
            self.assertTrue(capture["truncated"])

    def test_workflow_sanitizer_accepts_only_one_strict_dispatcher_marker(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("source.read_bytes()", source)
        self.assertIn("source_file.read(", source)
        marker = (
            '/usr/bin/python3 - "$raw_report" "$report" "$ssh_status" '
            '"${LIVE_USER_QA_REPORT_NONCE:-}" "$TARGET_SHA" "$capture_state" <<\'PY\'\n'
        )
        script = source.split(marker, 1)[1].split("\n          PY", 1)[0]
        script = textwrap.dedent(script)

        valid_marker = {
            "cleanup": "verified",
            "kind": "live_user_qa_success",
            "playwright": "passed",
            "report_nonce": RUNNER_NONCE,
            "schema": 1,
            "source_sha": SOURCE_SHA,
            "status": "passed",
            "success": True,
            "test_count": 1,
        }

        def run(
            raw: str,
            status: str = "0",
            capture_state: dict[str, object] | None = None,
        ) -> dict[str, object]:
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                root = Path(temporary)
                raw_path = root / "raw.log"
                report_path = root / "report.json"
                capture_path = root / "capture.json"
                raw_path.write_text(raw, encoding="utf-8")
                capture_path.write_text(
                    json.dumps(
                        capture_state
                        or {
                            "schema": 1,
                            "ssh_exit_code": int(status),
                            "truncated": False,
                            "bytes_observed": len(raw.encode("utf-8")),
                            "retained_bytes": len(raw.encode("utf-8")),
                            "capture_error": "",
                        }
                    ),
                    encoding="ascii",
                )
                result = subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-",
                        str(raw_path),
                        str(report_path),
                        status,
                        RUNNER_NONCE,
                        SOURCE_SHA,
                        str(capture_path),
                    ],
                    input=script,
                    text=True,
                    capture_output=True,
                    check=False,
                    env={**os.environ, "TARGET_SHA": SOURCE_SHA},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(report_path.read_text(encoding="utf-8"))

        passed = run("candidate output\n" + "live_user_qa_success " + json.dumps(valid_marker) + "\n")
        self.assertTrue(passed["success"])
        self.assertEqual(passed["marker_count"], 1)
        self.assertNotIn(RUNNER_NONCE, json.dumps(passed))

        failed_ssh = run(
            "live_user_qa_success " + json.dumps(valid_marker) + "\n",
            status="255",
        )
        self.assertFalse(failed_ssh["success"])
        self.assertEqual(failed_ssh["ssh_exit_code"], 255)
        self.assertEqual(failed_ssh["error_class_counts"], {"transport": 1})

        spoofed = dict(valid_marker, report_nonce="c" * 64)
        rejected = run("live_user_qa_success " + json.dumps(spoofed) + "\n")
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["error_class_counts"], {"protocol": 1})

        duplicate = run(
            "live_user_qa_success " + json.dumps(valid_marker) + "\n"
            "live_user_qa_success " + json.dumps(valid_marker) + "\n"
        )
        self.assertFalse(duplicate["success"])
        self.assertEqual(duplicate["marker_count"], 2)


if __name__ == "__main__":
    unittest.main()
