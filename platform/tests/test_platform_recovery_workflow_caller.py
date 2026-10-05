from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import urllib.request

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "platform-production-deploy.yml"
)
STEP_NAME = "Collect bounded exact failed-attempt API evidence"
RUN_ID = "23456"
RUN_ATTEMPT = "2"
SECURITY_RUN_ID = "34567"
SECURITY_RUN_ATTEMPT = "3"
SOURCE_SHA = "a" * 40
TARGET_SHA = "b" * 40
ARTIFACT_DIGEST = "sha256:" + "c" * 64


class _Response:
    def __init__(self, url: str, value: object):
        self.url = url
        self.body = json.dumps(value, separators=(",", ":")).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class _FakeOpener:
    def __init__(self, payloads: dict[str, object], *, mutations=None):
        self.payloads = payloads
        self.mutations = mutations or {}
        self.requests = []

    def open(self, request, timeout=20):
        self.requests.append((request, timeout))
        path = urlsplit(request.full_url).path.removeprefix("/repos/StrayForest/old_sparky")
        query = parse_qs(urlsplit(request.full_url).query)
        key = path
        if "page" in query:
            key += "?page=" + query["page"][0]
        if key not in self.payloads:
            raise AssertionError(f"unexpected recovery API request: {request.full_url}")
        value = self.payloads[key]
        mutate = self.mutations.get(key)
        if mutate is not None:
            value = mutate(value)
        return _Response(request.full_url, value)


def _payloads() -> dict[str, object]:
    return {
        "/actions/workflows/platform-production-deploy.yml": {
            "id": 100, "name": "Platform production deploy",
            "path": ".github/workflows/platform-production-deploy.yml",
            "state": "active",
        },
        f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}": {
            "id": int(RUN_ID), "run_attempt": int(RUN_ATTEMPT), "head_sha": SOURCE_SHA,
            "workflow_id": 100, "status": "completed", "conclusion": "failure",
        },
        f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}/jobs?page=1": {
            "total_count": 1,
            "jobs": [{
                "id": 901, "name": "Deploy production", "run_id": int(RUN_ID),
                "head_sha": SOURCE_SHA, "status": "completed",
                "conclusion": "failure",
            }],
        },
        f"/commits/{SOURCE_SHA}/statuses?page=1": [
            {"id": 1, "context": "platform/production-deploy", "state": "failure"},
        ],
        "/actions/workflows/platform-security.yml": {
            "id": 200, "name": "Platform security and build",
            "path": ".github/workflows/platform-security.yml", "state": "active",
        },
        f"/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_RUN_ATTEMPT}": {
            "id": int(SECURITY_RUN_ID), "run_attempt": int(SECURITY_RUN_ATTEMPT),
            "head_sha": TARGET_SHA, "workflow_id": 200,
            "status": "completed", "conclusion": "success",
        },
        f"/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_RUN_ATTEMPT}/jobs?page=1": {
            "total_count": 1,
            "jobs": [{
                "id": 902, "name": "status-final", "run_id": int(SECURITY_RUN_ID),
                "run_attempt": int(SECURITY_RUN_ATTEMPT), "status": "completed",
                "conclusion": "success",
            }],
        },
        f"/commits/{TARGET_SHA}/statuses?page=1": [
            {"id": 2, "context": "platform/security-build", "state": "success"},
        ],
        "/git/ref/heads/dev": {"ref": "refs/heads/dev", "object": {"sha": TARGET_SHA}},
        f"/actions/runs/{RUN_ID}/artifacts?page=1": {
            "total_count": 1,
            "artifacts": [{
                "id": 777, "name": f"platform-release-artifact-{RUN_ID}-{RUN_ATTEMPT}",
                "expired": False, "digest": ARTIFACT_DIGEST, "size_in_bytes": 1234,
                "workflow_run": {
                    "id": int(RUN_ID), "head_sha": SOURCE_SHA,
                    "run_attempt": int(RUN_ATTEMPT),
                },
            }],
        },
        "/actions/artifacts/777": {
            "id": 777, "name": f"platform-release-artifact-{RUN_ID}-{RUN_ATTEMPT}",
            "expired": False, "digest": ARTIFACT_DIGEST, "size_in_bytes": 1234,
            "workflow_run": {
                "id": int(RUN_ID), "head_sha": SOURCE_SHA,
                "run_attempt": int(RUN_ATTEMPT),
            },
        },
    }


def _collector_step() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = document["jobs"]["validate-recovery-baseline"]
    return next(step for step in job["steps"] if step.get("name") == STEP_NAME)


def _python_heredoc(run: str) -> str:
    match = re.search(r"<<'PY'\s*\n(.*?)\n\s*PY\s*$", run, re.DOTALL)
    if match is None:
        raise AssertionError("collector step must execute its Python heredoc")
    return textwrap.dedent(match.group(1))


class RecoveryWorkflowCallerTests(unittest.TestCase):
    def _environment(self, output: Path, proof_dir: Path) -> dict[str, str]:
        return {
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(proof_dir.parent),
            "GITHUB_API_URL": "https://api.github.com",
            "GITHUB_REPOSITORY": "StrayForest/old_sparky",
            "GH_TOKEN": "test-gh-token",
            "FAILED_RUN_ID": RUN_ID,
            "FAILED_RUN_ATTEMPT": RUN_ATTEMPT,
            "TARGET_SHA": TARGET_SHA,
            "SOURCE_SECURITY_RUN_ID": SECURITY_RUN_ID,
            "SOURCE_SECURITY_RUN_ATTEMPT": SECURITY_RUN_ATTEMPT,
        }

    @staticmethod
    def _argv(environment: dict[str, str]) -> list[str]:
        return [
            "-",
            str(Path(environment["RUNNER_TEMP"]) / "recovery-proof"),
            environment["GH_TOKEN"],
            environment["GITHUB_API_URL"],
            environment["GITHUB_REPOSITORY"],
            environment["FAILED_RUN_ID"],
            environment["FAILED_RUN_ATTEMPT"],
            environment["TARGET_SHA"],
            environment["SOURCE_SECURITY_RUN_ID"],
            environment["SOURCE_SECURITY_RUN_ATTEMPT"],
        ]

    def _execute(self, *, mutations=None, payload_overrides=None, env_overrides=None):
        step = _collector_step()
        run = step["run"]
        script = _python_heredoc(run)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proof_dir = root / "recovery-proof"
            output = root / "GITHUB_OUTPUT"
            environment = self._environment(output, proof_dir)
            environment.update(env_overrides or {})
            payloads = _payloads()
            payloads.update(payload_overrides or {})
            opener = _FakeOpener(payloads, mutations=mutations)
            with patch.dict(os.environ, environment, clear=False), patch.object(
                urllib.request, "build_opener", return_value=opener
            ), patch.object(sys, "argv", self._argv(environment)), patch.object(
                Path, "cwd", return_value=WORKFLOW.parents[2]
            ):
                failure = None
                try:
                    exec(compile(script, "<workflow recovery collector>", "exec"), {"__name__": "__main__"})
                except BaseException as exc:
                    failure = exc
            output_text = output.read_text(encoding="utf-8") if output.exists() else ""
            return failure, opener.requests, output_text, proof_dir.exists(), run, step

    def test_actual_collector_uses_bounded_authenticated_api_and_emits_consumed_outputs(self) -> None:
        failure, requests, output, proof_exists, _run, step = self._execute()
        self.assertIsNone(failure)
        self.assertTrue(proof_exists)
        self.assertEqual(len(requests), 11)
        self.assertIn("artifact_id=777\n", output)
        self.assertIn(f"artifact_digest={ARTIFACT_DIGEST}\n", output)
        self.assertIn("artifact_size=1234\n", output)
        self.assertIn("deploy_job_id=901\n", output)

        for request, timeout in requests:
            self.assertEqual(timeout, 20)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-gh-token")
            self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
            self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")

        requested = [request.full_url for request, _ in requests]
        api = "https://api.github.com/repos/StrayForest/old_sparky"
        self.assertEqual(
            requested,
            [
                f"{api}/actions/workflows/platform-production-deploy.yml",
                f"{api}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}",
                f"{api}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}/jobs?per_page=100&page=1",
                f"{api}/commits/{SOURCE_SHA}/statuses?per_page=100&page=1",
                f"{api}/actions/workflows/platform-security.yml",
                f"{api}/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_RUN_ATTEMPT}",
                f"{api}/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_RUN_ATTEMPT}/jobs?per_page=100&page=1",
                f"{api}/commits/{TARGET_SHA}/statuses?per_page=100&page=1",
                f"{api}/git/ref/heads/dev",
                f"{api}/actions/runs/{RUN_ID}/artifacts?per_page=100&page=1",
                f"{api}/actions/artifacts/777",
            ],
        )
        self.assertNotIn("test-gh-token", output)
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        recovery_steps = document["jobs"]["validate-recovery-baseline"]["steps"]
        downloader = next(step for step in recovery_steps if step.get("name") == "Download authenticated failed-job and release evidence")
        verifier = next(step for step in recovery_steps if step.get("name") == "Validate failed marker, report-only diagnostic, signed release, and live baseline")
        for output_name in ("artifact_id", "artifact_size", "deploy_job_id"):
            self.assertIn(f"steps.failed_attempt.outputs.{output_name}", downloader["run"])
        for output_name in ("source_sha", "artifact_id", "artifact_digest", "artifact_size"):
            self.assertIn(
                f"steps.failed_attempt.outputs.{output_name}",
                "\\n".join(str(value) for value in verifier["env"].values()),
            )
        self.assertEqual(step["id"], "failed_attempt")

    def test_actual_collector_rejects_incomplete_changed_and_malformed_api_pages(self) -> None:
        jobs_page_1 = f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}/jobs?page=1"
        jobs_page_2 = f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}/jobs?page=2"
        short_page = {
            jobs_page_1: lambda value: {**value, "total_count": 2},
        }
        full_page = [{"id": 1000 + index} for index in range(100)]
        short_page = {
            jobs_page_1: lambda value: {
                **value, "total_count": 101, "jobs": full_page,
            },
        }
        changed_total = {
            jobs_page_1: lambda value: {
                **value, "total_count": 101, "jobs": full_page,
            },
        }
        cases = (
            (
                "short object page",
                short_page,
                {jobs_page_2: {"total_count": 101, "jobs": []}},
            ),
            (
                "changed object total",
                changed_total,
                {jobs_page_2: {"total_count": 102, "jobs": [{"id": 902}]}},
            ),
            (
                "statuses are not a top-level array",
                {f"/commits/{SOURCE_SHA}/statuses?page=1": lambda _value: {"statuses": []}},
                {},
            ),
            (
                "malformed object rows",
                {jobs_page_1: lambda value: {**value, "jobs": [None]}},
                {},
            ),
            (
                "collection exceeds caller row bound",
                {jobs_page_1: lambda value: {**value, "total_count": 10001}},
                {},
            ),
        )
        for label, mutations, payload_overrides in cases:
            with self.subTest(label=label):
                failure, requests, output, _proof_exists, _run, _step = self._execute(
                    mutations=mutations, payload_overrides=payload_overrides,
                )
                self.assertIsInstance(failure, SystemExit)
                self.assertTrue(requests)
                self.assertEqual(output, "")

    def test_actual_collector_rejects_artifact_from_another_run_attempt(self) -> None:
        def wrong_attempt(value):
            changed = json.loads(json.dumps(value))
            row = changed["artifacts"][0] if "artifacts" in changed else changed
            row["workflow_run"]["run_attempt"] = int(RUN_ATTEMPT) + 1
            return changed

        mutations = {
            f"/actions/runs/{RUN_ID}/artifacts?page=1": wrong_attempt,
            "/actions/artifacts/777": wrong_attempt,
        }
        failure, _requests, output, _proof_exists, _run, _step = self._execute(mutations=mutations)
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(output, "")

    def test_actual_collector_rejects_selected_job_bound_to_another_run_source_or_attempt(self) -> None:
        jobs_page = f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}/jobs?page=1"

        def wrong_field(field, value):
            def apply(payload):
                changed = json.loads(json.dumps(payload))
                changed["jobs"][0][field] = value
                return changed
            return apply

        cases = (
            ("run id", "run_id", int(RUN_ID) + 1),
            ("source sha", "head_sha", "d" * 40),
            ("optional conflicting attempt", "run_attempt", int(RUN_ATTEMPT) + 1),
        )
        for label, field, value in cases:
            with self.subTest(label=label):
                failure, _requests, output, _proof_exists, _run, _step = self._execute(
                    mutations={jobs_page: wrong_field(field, value)},
                )
                self.assertIsInstance(failure, SystemExit)
                self.assertEqual(output, "")

    def test_actual_collector_rejects_artifact_name_run_or_source_drift(self) -> None:
        def mutate_artifact(field, value):
            def apply(payload):
                changed = json.loads(json.dumps(payload))
                row = changed["artifacts"][0] if "artifacts" in changed else changed
                if field == "name":
                    row[field] = value
                else:
                    row["workflow_run"][field] = value
                return changed
            return apply

        cases = (
            ("artifact name", "name", "platform-release-artifact-99999-2"),
            ("artifact run id", "id", 99999),
            ("artifact source", "head_sha", "d" * 40),
        )
        for label, field, value in cases:
            with self.subTest(label=label):
                mutations = {
                    f"/actions/runs/{RUN_ID}/artifacts?page=1": mutate_artifact(field, value),
                    "/actions/artifacts/777": mutate_artifact(field, value),
                }
                failure, _requests, output, _proof_exists, _run, _step = self._execute(
                    mutations=mutations,
                )
                self.assertIsInstance(failure, SystemExit)
                self.assertEqual(output, "")

    def test_actual_collector_rejects_hostile_dispatch_identity_before_api_access(self) -> None:
        hostile_value = "$" + "{{ secrets.GITHUB_TOKEN }}"
        for key, hostile in (
            ("FAILED_RUN_ID", "23456; touch /tmp/pwned"),
            ("FAILED_RUN_ATTEMPT", "2\nGH_TOKEN=leak"),
            ("SOURCE_SECURITY_RUN_ID", hostile_value),
            ("SOURCE_SECURITY_RUN_ATTEMPT", "0"),
            ("TARGET_SHA", "b" * 39 + "g"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                step = _collector_step()
                script = _python_heredoc(step["run"])
                environment = self._environment(root / "GITHUB_OUTPUT", root / "recovery-proof")
                environment[key] = hostile
                opener = _FakeOpener(_payloads())
                with patch.dict(os.environ, environment, clear=False), patch.object(
                    urllib.request, "build_opener", return_value=opener
                ), patch.object(sys, "argv", self._argv(environment)), patch.object(
                    Path, "cwd", return_value=WORKFLOW.parents[2]
                ):
                    with self.assertRaises(SystemExit):
                        exec(compile(script, "<workflow recovery collector>", "exec"), {"__name__": "__main__"})
                self.assertEqual(opener.requests, [], "invalid identities must fail before any API request")

    def test_step_keeps_dispatch_inputs_out_of_shell_interpolation(self) -> None:
        step = _collector_step()
        run = step["run"]
        for expression in ("inputs.failed_deploy_run_id", "inputs.failed_deploy_run_attempt",
                           "inputs.classifier_run_id", "inputs.classifier_run_attempt"):
            self.assertNotIn("$" + "{{ " + expression, run)
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        job_env = document["jobs"]["validate-recovery-baseline"]["env"]
        step_env = step.get("env", {})
        for name in ("FAILED_RUN_ID", "FAILED_RUN_ATTEMPT"):
            self.assertIn(name, job_env)
        for name in ("SOURCE_SECURITY_RUN_ID", "SOURCE_SECURITY_RUN_ATTEMPT"):
            self.assertIn(name, step_env)
        expression = "$" + "{{ "
        self.assertEqual(job_env["FAILED_RUN_ID"], expression + "inputs.failed_deploy_run_id }}")
        self.assertEqual(job_env["FAILED_RUN_ATTEMPT"], expression + "inputs.failed_deploy_run_attempt }}")
        self.assertEqual(step_env["SOURCE_SECURITY_RUN_ID"], expression + "inputs.classifier_run_id }}")
        self.assertEqual(step_env["SOURCE_SECURITY_RUN_ATTEMPT"], expression + "inputs.classifier_run_attempt }}")
        command = (
            '/usr/bin/python3 - "$RUNNER_TEMP/recovery-proof" "$GH_TOKEN" "$GITHUB_API_URL" '
            '"$GITHUB_REPOSITORY" "$FAILED_RUN_ID" "$FAILED_RUN_ATTEMPT" "$TARGET_SHA" '
            '"$SOURCE_SECURITY_RUN_ID" "$SOURCE_SECURITY_RUN_ATTEMPT"'
        )
        self.assertIn(command, run)

    def test_exact_job_logs_download_uses_bounded_redirected_plain_text(self) -> None:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = document["jobs"]["validate-recovery-baseline"]["steps"]
        step = next(
            item for item in steps
            if item.get("name") == "Download authenticated failed-job and release evidence"
        )
        run = step["run"]
        for name, value in (
            ("artifact_id", "777"),
            ("artifact_size", "1234"),
            ("deploy_job_id", "901"),
        ):
            expression = "$" + "{{ steps.failed_attempt.outputs." + name + " }}"
            run = run.replace(expression, value)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_temp = root / "runner"
            proof = runner_temp / "recovery-proof"
            fake_bin = root / "bin"
            proof.mkdir(parents=True)
            fake_bin.mkdir()
            requests = root / "requests.jsonl"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "url = next(value for value in args if value.startswith('https://'))\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "headers = [args[i + 1] for i, value in enumerate(args[:-1]) if value == '--header']\n"
                "assert '--location' in args\n"
                "assert any(value == 'Authorization: Bearer test-gh-token' for value in headers)\n"
                "with Path(os.environ['REQUEST_LOG']).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps({'url': url, 'output': str(output), 'args': args}) + '\\n')\n"
                "if url.endswith('/actions/jobs/901/logs'):\n"
                "    assert args[args.index('--max-filesize') + 1] == '16777216'\n"
                "    output.write_text('reason=invalid_marker child_exit=0 observed_bytes=64 dispatcher_exit=2\\n', encoding='utf-8')\n"
                "elif url.endswith('/actions/artifacts/777/zip'):\n"
                "    output.write_bytes(b'artifact fixture')\n"
                "else:\n"
                "    raise AssertionError('unexpected download URL: ' + url)\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GH_TOKEN": "test-gh-token",
                    "REQUEST_LOG": str(requests),
                }
            )
            environment.pop("BASH_ENV", None)
            environment.pop("ENV", None)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", run],
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [json.loads(value) for value in requests.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [row["url"] for row in lines],
                [
                    "https://api.github.com/repos/StrayForest/old_sparky/actions/jobs/901/logs",
                    "https://api.github.com/repos/StrayForest/old_sparky/actions/artifacts/777/zip",
                ],
            )
            log_path = proof / "failed-job-logs.txt"
            self.assertEqual(
                log_path.read_text(encoding="utf-8"),
                "reason=invalid_marker child_exit=0 observed_bytes=64 dispatcher_exit=2\n",
            )
            self.assertFalse((proof / "failed-job-logs.zip").exists())


if __name__ == "__main__":
    unittest.main()
