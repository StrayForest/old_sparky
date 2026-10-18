from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from types import SimpleNamespace
from urllib.request import Request
from unittest.mock import patch

from tools import platform_maintenance_qa_evidence as evidence


REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical_workflow_report(workflow_name: str, target_sha: str) -> dict[str, object]:
    workflow_path = REPO_ROOT / ".github" / "workflows" / workflow_name
    source = workflow_path.read_text(encoding="utf-8")
    if workflow_name == "platform-live-launch.yml":
        pattern = (
            r'/usr/bin/python3 - "\$raw_report" "\$safe_report" '
            r'"\$SUPERVISOR_STATUS" \\\s*\n'
            r'\s*"\$GITHUB_SHA" "\$APP_TARGET_SHA" "\$SOURCE_BINDING_SHA256" "\$LIVE_MARKER" '
            r"<<'PY'\n(.*?)\n\s*PY"
        )
        marker = "maintenance-contract"
        marker_digest = hashlib.sha256(marker.encode("ascii")).hexdigest()
        count_line = (
            "LIVE_BROWSER_COUNTS schema=1 run_status=passed logical_total=54 logical_pass=47 "
            "logical_fail=0 logical_expected_fail=0 logical_flaky=0 logical_skip=7 "
            f"logical_interrupted=0 attempt_total=54 attempt_pass=47 attempt_fail=0 "
            f"attempt_skip=7 attempt_interrupted=0 attempt_timedout=0 source_sha={target_sha} "
            f"app_sha={target_sha} marker_sha256={marker_digest}\n"
        )
        status_line = (
            "LIVE_LAUNCH_STATUS schema=2 status=passed stage=complete check=none "
            f"child_exit=0 source_sha={target_sha}\n"
        )
        arguments = ("0", target_sha, target_sha, "", marker)
        raw = (count_line + status_line).encode("ascii")
        output_name = "safe.json"
    else:
        pattern = (
            r'/usr/bin/python3 - "\$raw_report" "\$report" "\$ssh_status" \\\s*\n'
            r'\s*"\$TARGET_SHA" "\$APP_TARGET_SHA" \\\s*\n'
            r'\s*"\$binding_digest" <<' + "'PY'\\n(.*?)\\n\\s*PY"
        )
        marker_digest = "b" * 64
        count_line = (
            "LIVE_BROWSER_COUNTS schema=1 run_status=passed logical_total=1 logical_pass=1 "
            "logical_fail=0 logical_expected_fail=0 logical_flaky=0 logical_skip=0 "
            f"logical_interrupted=0 attempt_total=1 attempt_pass=1 attempt_fail=0 "
            f"attempt_skip=0 attempt_interrupted=0 attempt_timedout=0 source_sha={target_sha} "
            f"app_sha={target_sha} marker_sha256={marker_digest}\n"
        )
        diagnostic = (
            "LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=none stdout_bytes=0 stderr_bytes=0 "
            "truncated=false child_exit=0\n"
        )
        raw = (count_line + "LIVE_USER_QA_SUCCESS\n" + diagnostic).encode("ascii")
        arguments = ("0", target_sha, target_sha, "")
        output_name = "report.json"
    match = re.search(pattern, source, flags=re.DOTALL)
    if match is None:
        raise AssertionError(f"canonical sanitizer was not found in {workflow_name}")
    script = textwrap.dedent(match.group(1))
    with tempfile.TemporaryDirectory(prefix="maintenance-qa-report-") as temporary:
        raw_path = Path(temporary) / "raw.log"
        report_path = Path(temporary) / output_name
        raw_path.write_bytes(raw)
        completed = subprocess.run(
            [sys.executable, "-c", script, str(raw_path), str(report_path), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise AssertionError("canonical workflow sanitizer rejected synthetic report")
        return json.loads(report_path.read_text(encoding="utf-8"))


def _zip_report(member_name: str, report: dict[str, object]) -> bytes:
    payload = json.dumps(report, sort_keys=True).encode() + b"\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, payload)
    return buffer.getvalue()


def _loaded_unit_properties(argv: tuple[str, ...] | None = None) -> str:
    selected_argv = argv or evidence.EXPECTED_MAINTENANCE_ARGV
    exec_start = (
        "{ path="
        + selected_argv[0]
        + " ; argv[]="
        + " ".join(selected_argv)
        + " ; ignore_errors=no ; start_time= ; stop_time="
        + " ; pid=0 ; code=(null) ; status=0/0 }"
    )
    properties = {
        "FragmentPath": "/etc/systemd/system/deadlock-maintenance.service",
        "DropInPaths": "",
        "NeedDaemonReload": "no",
        "ActiveState": "inactive",
        "SubState": "dead",
        "ExecStart": exec_start,
        "CPUQuotaPerSecUSec": "500ms",
        "MemoryMax": "536870912",
        "TimeoutStartUSec": "30min",
    }
    return "".join(f"{key}={value}\n" for key, value in properties.items())


def _legacy_preserve_maintenance_argv() -> tuple[str, ...]:
    argv = list(evidence.EXPECTED_MAINTENANCE_ARGV)
    manifest_index = argv.index("--protected-backup-manifest")
    del argv[manifest_index : manifest_index + 2]
    argv[argv.index("15", argv.index("--backup-keep"))] = "14"
    argv.insert(3, "--preserve-existing-backups")
    return tuple(argv)


class MaintenanceQaEvidenceTests(unittest.TestCase):
    def test_closed_qa_reports_reject_inconsistent_counts_and_bindings(self) -> None:
        target_sha = "a" * 40
        launch = _canonical_workflow_report("platform-live-launch.yml", target_sha)
        evidence.validate_live_launch_report(launch, target_sha)
        user = _canonical_workflow_report("platform-live-user-qa.yml", target_sha)
        evidence.validate_live_user_qa_report(user, target_sha)
        self.assertIsNone(user["failure_origin"])

        invalid = (
            ("launch-skip-count", {**launch, "logical_counts": {**launch["logical_counts"], "logical_skip": 5}}, "launch"),
            ("launch-bool-status-count", {**launch, "status_counts": {**launch["status_counts"], "passed": True}}, "launch"),
            ("launch-failure", {**launch, "success": False}, "launch"),
            ("user-bool-counter", {**user, "logical_total": True}, "user"),
            ("user-child-diagnostic-missing", {**user, "child_diagnostic": None}, "user"),
            ("user-failure-origin", {**user, "failure_origin": "dispatcher"}, "user"),
            ("user-failure-origin-missing", {key: value for key, value in user.items() if key != "failure_origin"}, "user"),
            ("user-skipped", {**user, "browser_counts": {**user["browser_counts"], "logical_skip": 1}}, "user"),
            ("user-binding", {**user, "source_binding_sha256": ""}, "user"),
        )
        for case, candidate, report_type in invalid:
            with self.subTest(case=case), self.assertRaises(evidence.EvidenceError):
                if report_type == "launch":
                    evidence.validate_live_launch_report(candidate, target_sha)
                else:
                    evidence.validate_live_user_qa_report(candidate, target_sha)

    def test_artifact_proof_binds_latest_attempt_digest_window_and_report(self) -> None:
        target_sha = "a" * 40
        member = "live-launch-sanitized.json"
        report = _canonical_workflow_report("platform-live-launch.yml", target_sha)
        archive_bytes = _zip_report(member, report)
        run_id, attempt, artifact_id = 12345, 2, 67890
        workflow = {
            "id": 77,
            "path": evidence.LIVE_LAUNCH_PATH,
            "name": evidence.LIVE_LAUNCH_NAME,
            "state": "active",
        }
        run = {
            "id": run_id,
            "workflow_id": workflow["id"],
            "name": workflow["name"],
            "path": workflow["path"],
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
            "head_sha": target_sha,
            "head_branch": "dev",
            "event": "workflow_dispatch",
            "html_url": f"https://github.com/{evidence.REPOSITORY}/actions/runs/{run_id}",
            "repository": {
                "full_name": evidence.REPOSITORY,
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
            "run_started_at": "2026-10-12T10:00:00Z",
            "updated_at": "2026-10-12T10:10:00Z",
        }
        artifact_name = f"platform-live-launch-sanitized-{run_id}-{attempt}"
        listing = {
            "total_count": 1,
            "artifacts": [{"id": artifact_id, "name": artifact_name}],
        }
        metadata = {
            "id": artifact_id,
            "name": artifact_name,
            "expired": False,
            "size_in_bytes": len(archive_bytes),
            "digest": "sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
            "created_at": "2026-10-12T10:08:00Z",
            "expires_at": "2026-10-14T10:08:00Z",
            "workflow_run": {
                "id": run_id,
                "head_sha": target_sha,
                "head_branch": "dev",
            },
        }
        validate = evidence._validate_artifact_binding
        args = {
            "workflow": workflow,
            "run": run,
            "listing": listing,
            "metadata": metadata,
            "archive_bytes": archive_bytes,
            "run_id_text": str(run_id),
            "attempt_text": str(attempt),
            "expected_sha": target_sha,
            "workflow_path": evidence.LIVE_LAUNCH_PATH,
            "workflow_name": evidence.LIVE_LAUNCH_NAME,
            "artifact_prefix": "platform-live-launch-sanitized",
            "member_name": member,
            "report_validator": evidence.validate_live_launch_report,
            "now": datetime(2026, 10, 12, 11, tzinfo=UTC),
        }
        validate(**args)

        source_request = Request(
            "https://api.github.com/repos/StrayForest/old_sparky/actions/artifacts/67890/zip",
            headers={"Authorization": "Bearer synthetic", "Cookie": "synthetic"},
        )
        class _Response(io.BytesIO):
            status = 200

            def __init__(self, final_url: str) -> None:
                super().__init__(b"bounded synthetic archive")
                self.final_url = final_url

            def geturl(self) -> str:
                return self.final_url

        class _Opener:
            def __init__(self, final_url: str) -> None:
                self.response = _Response(final_url)

            def open(self, request: Request, timeout: int) -> _Response:
                self.request = request
                self.timeout = timeout
                return self.response

        self.assertEqual(
            evidence._read_response(_Opener(source_request.full_url), source_request, 64),
            b"bounded synthetic archive",
        )
        for url in (
            "https://signed.blob.core.windows.net/container/archive?sig=opaque",
            "https://asset.actions.githubusercontent.com:443/container/archive?sig=opaque",
        ):
            redirected_response_url = url.split("?", 1)[0]
            self.assertEqual(
                evidence._read_response(
                    _Opener(redirected_response_url), source_request, 64
                ),
                b"bounded synthetic archive",
            )
            redirect = evidence._SafeArtifactRedirect()
            forwarded = redirect.redirect_request(
                source_request, None, 302, "Found", {}, url
            )
            self.assertIsNotNone(forwarded)
            assert forwarded is not None
            self.assertEqual(
                forwarded.header_items(), [("Accept", "application/octet-stream")]
            )
            self.assertIsNone(
                redirect.redirect_request(source_request, None, 302, "Found", {}, url)
            )

        for case, url in (
            ("suffix-lookalike", "https://x.blob.core.windows.net.attacker.invalid/a"),
            ("actions-suffix-lookalike", "https://x.actions.githubusercontent.com.attacker.invalid/a"),
            ("nondefault-port", "https://x.blob.core.windows.net:444/a"),
            ("trailing-dot", "https://x.blob.core.windows.net./a"),
            ("ip-host", "https://127.0.0.1/a"),
            ("userinfo", "https://user@x.blob.core.windows.net/a"),
            ("fragment", "https://x.blob.core.windows.net/a#fragment"),
            ("empty-fragment", "https://x.blob.core.windows.net/a#"),
            ("non-https", "http://x.blob.core.windows.net/a"),
        ):
            with self.subTest(redirect_case=case):
                redirect = evidence._SafeArtifactRedirect()
                self.assertIsNone(
                    redirect.redirect_request(source_request, None, 302, "Found", {}, url)
                )

        invalid = (
            ("wrong-attempt", {"run": {**run, "run_attempt": 1}}),
            ("duplicate-artifact", {"listing": {"total_count": 2, "artifacts": [*listing["artifacts"], dict(listing["artifacts"][0])]}}),
            ("expired", {"metadata": {**metadata, "expired": True}}),
            ("digest", {"archive_bytes": archive_bytes + b"x"}),
            ("outside-run-window", {"metadata": {**metadata, "created_at": "2026-10-12T10:11:00Z"}}),
            ("mutated-report", {"archive_bytes": _zip_report(member, {**report, "success": False})}),
        )
        for case, changes in invalid:
            with self.subTest(case=case), self.assertRaises(evidence.EvidenceError):
                validate(**{**args, **changes})

    def test_report_archive_rejects_duplicate_keys_and_unexpected_paths(self) -> None:
        for case, name, payload in (
            ("path", "../live-launch-sanitized.json", b"{}\n"),
            ("duplicate-json-key", "live-launch-sanitized.json", b'{"x":1,"x":2}\n'),
        ):
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr(name, payload)
            with self.subTest(case=case), self.assertRaises(evidence.EvidenceError):
                evidence._parse_report_member(
                    archive_buffer.getvalue(), "live-launch-sanitized.json"
                )

        target_sha = "a" * 40
        report = _canonical_workflow_report("platform-live-launch.yml", target_sha)
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr(
                "live-launch-sanitized.json",
                json.dumps(report, sort_keys=True).encode() + b"\n",
            )
            archive.writestr("extra.txt", b"unexpected")
        with self.subTest(case="valid-report-with-extra-member"), self.assertRaises(
            evidence.EvidenceError
        ):
            evidence._parse_report_member(
                archive_buffer.getvalue(), "live-launch-sanitized.json"
            )

    def test_protected_rotation_report_requires_bound_closed_proof(self) -> None:
        archive_name = "platformdb-20261016T120000Z.dump"
        archive_digest = "b" * 64
        manifest_digest = "a" * 64

        def report_fixture() -> dict[str, object]:
            proof = {
                "manifest": {"before": {"digest": manifest_digest}},
                "verified_selection": {
                    "selected_order": [
                        {
                            "dump_file": archive_name,
                            "claimed_sha256": archive_digest,
                            "claimed_size_bytes": 4096,
                        }
                    ]
                },
                "removed_pair_count": 2,
            }
            return {
                "mode": "apply",
                "ok": False,
                "backup": {
                    "status": "completed",
                    "created": True,
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "checksum_present": True,
                    "rotation_mode": "rotate-existing",
                    "format_version": 2,
                    "archive_name": archive_name,
                    "archive_sha256": archive_digest,
                    "size_bytes": 4096,
                    "removed_count": 4,
                    "protected_retention_proof": proof,
                },
            }

        valid = report_fixture()
        with patch.object(
            evidence.backup_restore_drill,
            "valid_protected_retention_proof",
            return_value=True,
        ) as proof_validator:
            evidence.validate_protected_retention_report(valid, manifest_digest)
        proof_validator.assert_called_once_with(
            valid["backup"]["protected_retention_proof"],
            requested_keep=evidence.PROTECTED_BACKUP_KEEP,
            new_dump_file=archive_name,
        )

        invalid_cases = (
            (
                "manifest snapshot mismatch",
                lambda candidate: candidate["backup"]["protected_retention_proof"][
                    "manifest"
                ]["before"].update(digest="c" * 64),
                manifest_digest,
            ),
            (
                "archive digest mismatch",
                lambda candidate: candidate["backup"]["protected_retention_proof"][
                    "verified_selection"
                ]["selected_order"][0].update(claimed_sha256="c" * 64),
                manifest_digest,
            ),
            (
                "archive size mismatch",
                lambda candidate: candidate["backup"]["protected_retention_proof"][
                    "verified_selection"
                ]["selected_order"][0].update(claimed_size_bytes=4095),
                manifest_digest,
            ),
            (
                "removed path count mismatch",
                lambda candidate: candidate["backup"].update(removed_count=3),
                manifest_digest,
            ),
            (
                "snapshot input malformed",
                lambda candidate: None,
                "not-a-digest",
            ),
        )
        for case, mutate, expected_digest in invalid_cases:
            candidate = report_fixture()
            mutate(candidate)
            with self.subTest(case=case), patch.object(
                evidence.backup_restore_drill,
                "valid_protected_retention_proof",
                return_value=True,
            ), self.assertRaises(evidence.EvidenceError):
                evidence.validate_protected_retention_report(
                    candidate, expected_digest
                )

        rejected_proof = report_fixture()
        with patch.object(
            evidence.backup_restore_drill,
            "valid_protected_retention_proof",
            return_value=False,
        ), self.assertRaises(evidence.EvidenceError):
            evidence.validate_protected_retention_report(
                rejected_proof, manifest_digest
            )

    def test_qa_identity_is_rejected_before_any_api_request(self) -> None:
        for case, run_id, attempt in (
            ("zero-run-id", "0", "1"),
            ("zero-attempt", "123", "0"),
            ("nonnumeric-run-id", "run-123", "1"),
            ("oversized-attempt", "123", str(2**63)),
        ):
            with self.subTest(case=case):
                with patch.object(evidence, "build_opener") as build_opener:
                    with self.assertRaises(evidence.EvidenceError):
                        evidence._verify_one_qa(
                            token="synthetic",
                            target_sha="a" * 40,
                            run_id=run_id,
                            attempt=attempt,
                            workflow_path=evidence.LIVE_LAUNCH_PATH,
                            workflow_name=evidence.LIVE_LAUNCH_NAME,
                            artifact_prefix="platform-live-launch-sanitized",
                            member_name="live-launch-sanitized.json",
                            report_validator=evidence.validate_live_launch_report,
                        )
                    build_opener.assert_not_called()

    def test_loaded_maintenance_unit_requires_exact_protected_rotation_argv_and_limits(self) -> None:
        source_bytes = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-maintenance.service"
        ).read_bytes()
        installed_path = "/etc/systemd/system/deadlock-maintenance.service"
        validate = evidence._validate_maintenance_unit_bytes
        validate(source_bytes, source_bytes, installed_path, _loaded_unit_properties())

        invalid_outputs = (
            ("legacy-preserve-unit", _loaded_unit_properties(_legacy_preserve_maintenance_argv())),
            (
                "missing-manifest",
                _loaded_unit_properties(
                    tuple(
                        value
                        for value in evidence.EXPECTED_MAINTENANCE_ARGV
                        if value != "/opt/oldsparky/platform/shared/backup-retention/protected-dump-files.json"
                    )
                ),
            ),
            (
                "extra-argv",
                _loaded_unit_properties(
                    (*evidence.EXPECTED_MAINTENANCE_ARGV, "--unexpected")
                ),
            ),
            ("drop-in", _loaded_unit_properties().replace("DropInPaths=\n", "DropInPaths=/etc/systemd/system/deadlock-maintenance.service.d/override.conf\n")),
            ("daemon-reload-needed", _loaded_unit_properties().replace("NeedDaemonReload=no", "NeedDaemonReload=yes")),
            ("cpu-quota", _loaded_unit_properties().replace("CPUQuotaPerSecUSec=500ms", "CPUQuotaPerSecUSec=1s")),
            ("memory-limit", _loaded_unit_properties().replace("MemoryMax=536870912", "MemoryMax=1073741824")),
            ("timeout", _loaded_unit_properties().replace("TimeoutStartUSec=30min", "TimeoutStartUSec=31min")),
            ("unknown-property", _loaded_unit_properties() + "Unknown=value\n"),
            ("duplicate-property", _loaded_unit_properties() + "MemoryMax=536870912\n"),
            ("missing-property", _loaded_unit_properties().replace("SubState=dead\n", "")),
            (
                "empty-exec-path",
                _loaded_unit_properties().replace(
                    "{ path=/opt/oldsparky/platform/shared/venv/bin/python",
                    "{ path=",
                    1,
                ),
            ),
            (
                "empty-exec-argv",
                _loaded_unit_properties().replace(
                    "argv[]=" + " ".join(evidence.EXPECTED_MAINTENANCE_ARGV),
                    "argv[]=",
                    1,
                ),
            ),
            (
                "empty-exec-status",
                _loaded_unit_properties().replace("status=0/0", "status=", 1),
            ),
            (
                "malformed-exec-fields",
                _loaded_unit_properties().replace(" ; pid=0 ;", " ; pid= ;", 1),
            ),
        )
        for case, output in invalid_outputs:
            with self.subTest(case=case), self.assertRaises(evidence.EvidenceError):
                validate(source_bytes, source_bytes, installed_path, output)

    def test_diagnostic_preview_executes_only_argv_derived_from_validated_unit(self) -> None:
        from tools import platform_storage_maintenance as maintenance

        source_path = REPO_ROOT / "platform/deploy/systemd/deadlock-maintenance.service"
        source_bytes = source_path.read_bytes()
        preview_argv = evidence.maintenance_preview_argv()
        self.assertNotIn("--apply", preview_argv)
        self.assertIn("--json", preview_argv)
        self.assertIn("--protected-backup-manifest", preview_argv)
        self.assertIn("/opt/oldsparky/platform/shared/backup-retention/protected-dump-files.json", preview_argv)
        self.assertIn("--backup-keep", preview_argv)
        self.assertIn("15", preview_argv)
        for slug in (
            "gha-38000614899-1-7e5769ba3f85",
            "gha-37760797674-1-80e457e57081",
        ):
            self.assertIn(slug, preview_argv)
        properties = _loaded_unit_properties()
        with patch.object(
            evidence.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run, patch.object(sys, "stdout", SimpleNamespace(buffer=io.BytesIO())), patch.object(
            evidence,
            "validate_loaded_maintenance_unit",
            side_effect=lambda _source, installed, output: evidence._validate_maintenance_unit_bytes(
                source_bytes, source_bytes, installed, output
            ),
        ):
            self.assertEqual(
                evidence.run_validated_maintenance_preview(
                    str(source_path),
                    "/etc/systemd/system/deadlock-maintenance.service",
                    properties,
                ),
                0,
            )
        self.assertEqual(run.call_args.args[0], preview_argv)
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertNotIn("--apply", run.call_args.args[0])
        previous_argv = sys.argv
        try:
            sys.argv = [preview_argv[0], *preview_argv[2:]]
            args = maintenance.parse_args()
        finally:
            sys.argv = previous_argv
        self.assertEqual(
            tuple(args.protected_release),
            (
                "gha-38000614899-1-7e5769ba3f85",
                "gha-37760797674-1-80e457e57081",
            ),
        )
        invalid = properties.replace("NeedDaemonReload=no", "NeedDaemonReload=yes")
        with patch.object(evidence.subprocess, "run") as run, patch.object(
            evidence,
            "validate_loaded_maintenance_unit",
            side_effect=lambda _source, installed, output: evidence._validate_maintenance_unit_bytes(
                source_bytes, source_bytes, installed, output
            ),
        ):
            with self.assertRaises(evidence.EvidenceError):
                evidence.run_validated_maintenance_preview(
                    str(source_path),
                    "/etc/systemd/system/deadlock-maintenance.service",
                    invalid,
                )
        run.assert_not_called()

    def test_stale_loaded_unit_preflight_exits_before_service_start(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-storage-maintenance.yml"
        ).read_text(encoding="utf-8")
        preflight_start = workflow.index('          loaded_maintenance_unit="$(systemctl show')
        preflight_end = workflow.index("          service_state_before=", preflight_start)
        preflight = textwrap.dedent(workflow[preflight_start:preflight_end])
        source_bytes = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-maintenance.service"
        ).read_bytes()
        legacy_argv = _legacy_preserve_maintenance_argv()

        with tempfile.TemporaryDirectory(prefix="maintenance-unit-preflight-") as temporary:
            root = Path(temporary)
            release = root / "release"
            source_path = release / "deploy/systemd/deadlock-maintenance.service"
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(source_bytes)
            source_path.chmod(0o600)
            tools_path = release / "tools"
            tools_path.mkdir()
            validator = tools_path / "platform_maintenance_qa_evidence.py"
            validator.write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    from pathlib import Path
                    sys.path.insert(0, {str(REPO_ROOT / "platform/tools")!r})
                    import platform_maintenance_qa_evidence as evidence
                    source = Path(sys.argv[2]).read_bytes()
                    try:
                        evidence._validate_maintenance_unit_bytes(
                            source, source, sys.argv[3], sys.stdin.read()
                        )
                    except Exception:
                        raise SystemExit(1)
                    """
                ),
                encoding="utf-8",
            )
            current = root / "current"
            current.symlink_to(release, target_is_directory=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "service-started"
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "if sys.argv[1:2] == ['show']:\n"
                "    sys.stdout.write(os.environ['FIXTURE_SYSTEMCTL_OUTPUT'])\n"
                "elif sys.argv[1:2] == ['start']:\n"
                "    Path(os.environ['FIXTURE_START_MARKER']).write_text('started')\n"
                "else:\n"
                "    raise SystemExit(9)\n",
                encoding="utf-8",
            )
            fake_systemctl.chmod(0o700)
            script = (
                "set -Eeuo pipefail\n"
                "fail_with_status() { exit \"$2\"; }\n"
                f"current={str(current)!r}\n"
                f"current_before={str(release)!r}\n"
                + preflight
                + "systemctl start deadlock-maintenance.service\n"
            )
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "FIXTURE_SYSTEMCTL_OUTPUT": _loaded_unit_properties(legacy_argv),
                "FIXTURE_START_MARKER": str(marker),
            }
            completed = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
