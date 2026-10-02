"""Security contracts for credential-bearing owned workflows."""

from __future__ import annotations

import re
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"

OWNED_SECRET_WORKFLOWS = {
    "platform-cloudflare-readonly-audit.yml": ("audit",),
    "platform-draft-cloudflare.yml": ("release",),
    "platform-live-user-qa.yml": ("live-user-qa",),
    "platform-production-release-abort.yml": ("abort",),
    "platform-production-release-recover.yml": ("recover",),
    "platform-production-retained-load-abort.yml": ("abort",),
    "platform-production-retained-load-cleanup.yml": ("cleanup",),
    "platform-production-service-recovery.yml": ("recover-web",),
    "platform-production-storage-diagnostics.yml": ("collect",),
    "platform-production-storage-maintenance.yml": ("maintenance",),
}


def _job_blocks(source: str) -> dict[str, str]:
    matches = re.finditer(
        r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    return {match.group("name"): match.group("body") for match in matches}


class SecretArtifactBoundaryTests(unittest.TestCase):
    def _assert_draft_dependency_audit(self, job: str) -> None:
        self.assertIn("timeout 30s npm audit", job)
        for flag in (
            "--registry=https://registry.npmjs.org/",
            "--package-lock-only",
            "--omit=dev",
            "--audit-level=high",
            "--json",
            "--offline=false",
            "--prefer-offline=false",
            "--fetch-retries=0",
            "--fetch-timeout=10000",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, job)
        self.assertNotRegex(job, r"(?m)^\s+--offline(?:\s|$)")
        self.assertNotRegex(job, r"(?m)^\s+--prefer-offline(?:\s|$)")
        self.assertIn('node - "$audit_report" "$audit_rc" "$audit_stderr"', job)
        self.assertIn("JSON.parse(fs.readFileSync(reportPath, \"utf8\"))", job)
        self.assertIn("report?.metadata?.vulnerabilities", job)
        self.assertIn("missing or malformed JSON report", job)
        self.assertIn("if (auditExit !== 0)", job)
        self.assertIn("high or critical vulnerabilities remain", job)
        self.assertIn("process.exit(1)", job)

    def test_owned_secret_jobs_are_fresh_and_do_not_load_candidate_code(self) -> None:
        for workflow_name, expected_jobs in OWNED_SECRET_WORKFLOWS.items():
            source = (WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8")
            jobs = _job_blocks(source)
            for job_name in expected_jobs:
                with self.subTest(workflow=workflow_name, job=job_name):
                    body = jobs[job_name]
                    self.assertIn("environment: production", body)
                    self.assertNotIn("actions/checkout@", body)
                    self.assertNotIn("/root/old_sparky", body)
                    self.assertNotIn("platform/tools/", body)
                    self.assertNotRegex(
                        body,
                        r"platform_(?:build_release|cloudflare_readonly_audit|"
                        r"live_user_qa(?:\.sh|_mailbox_helper)|live_qa_mailbox_helper)",
                    )

    def test_draft_release_is_a_digest_bound_data_only_consumer(self) -> None:
        source = (WORKFLOW_ROOT / "platform-draft-cloudflare.yml").read_text(
            encoding="utf-8"
        )
        jobs = _job_blocks(source)
        self._assert_draft_dependency_audit(jobs["verify-pr"])
        self._assert_draft_dependency_audit(jobs["build-release"])
        verify_audit = jobs["verify-pr"].index("timeout 30s npm audit")
        self.assertLess(verify_audit, jobs["verify-pr"].index("Check JavaScript syntax"))
        self.assertIn("actions/checkout@", jobs["build-release"])
        self.assertNotRegex(jobs["build-release"], r"CLOUDFLARE_(?:API_TOKEN|ACCOUNT_ID)")
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund --omit=dev", jobs["build-release"])
        self.assertIn("package-lock.json", jobs["build-release"])
        self.assertIn("WRANGLER_LOCK_SHA256", jobs["build-release"])
        lock_check = jobs["build-release"].index("sha256sum --check")
        npm_install = jobs["build-release"].index("npm ci --ignore-scripts")
        self.assertLess(lock_check, npm_install)
        audit = jobs["build-release"].index("timeout 30s npm audit")
        self.assertLess(audit, npm_install)
        self.assertEqual(source.count("77f835c82e07589aa27ab9b81dc259805e3d2c6b3a98546083e52a5ba234e0cd"), 3)
        self.assertEqual(source.count("30bd79b0b490096eb3d69b46629b507c810db1574eecd62b293390c5b0e5bfbf"), 2)
        self.assertNotIn("17d717b60ee2f9edf5bbf1c21d0e99c787e974aa347eea0dedb896d36dcff145", source)
        self.assertNotIn("c27608e7efe705b963aee2f3b2d5ef15ac7d7e51816f086836aac071ea59ad6a", source)
        self.assertEqual(source.count("size > 192 * 1024 * 1024 or total_bytes > 300 * 1024 * 1024"), 2)
        self.assertIn("30bd79b0b490096eb3d69b46629b507c810db1574eecd62b293390c5b0e5bfbf", jobs["build-release"])
        self.assertNotIn("4.129.0", source)
        self.assertIn('"wrangler": "4.146.0"', (REPO_ROOT / "platform/apps/platform_draft/package.json").read_text(encoding="utf-8"))
        self.assertIn('"version": "4.146.0"', (REPO_ROOT / "platform/apps/platform_draft/package-lock.json").read_text(encoding="utf-8"))
        release = jobs["release"]
        self.assertNotIn("actions/checkout@", release)
        self.assertIn("actions/download-artifact@", release)
        self.assertIn("bundle-manifest.json", release)
        self.assertIn("manifest digest mismatch", release)
        self.assertIn("platform-draft-wrangler-${{ github.run_id }}-${{ github.run_attempt }}", release)
        self.assertIn("wrangler-tool-manifest.json", release)
        self.assertIn("EXPECTED_WRANGLER_MANIFEST_SHA256", release)
        self.assertIn('node "$WRANGLER_CLI"', release)
        self.assertNotIn("npx", release)

    def test_secret_provenance_jobs_pin_validators_to_trusted_default_source(self) -> None:
        for workflow_name, job_marker in (
            ("platform-production-deploy.yml", "validate-security-provenance"),
            ("platform-production-content-diagnostics.yml", "validate-content-provenance"),
            ("platform-patch-translation-qa.yml", "validate-translation-inputs"),
        ):
            source = (WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8")
            jobs = _job_blocks(source)
            body = jobs[job_marker]
            with self.subTest(workflow=workflow_name):
                self.assertIn("Resolve immutable default-branch", body)
                self.assertIn("TRUSTED_BRANCH: dev", body)
                self.assertIn("git/ref/heads/${TRUSTED_BRANCH}", body)
                self.assertIn("refs/heads/{branch}", body)
                self.assertIn("obj.get(\"type\") != \"commit\"", body)
                self.assertIn("ref: ${{ steps.trusted_default.outputs.sha }}", body)
                self.assertNotIn("ref: ${{ env.TARGET_SHA }}", body)
                self.assertIn("TRUSTED_SOURCE_SHA", body)
                self.assertIn("git rev-parse HEAD", body)

    def test_security_classifier_executes_only_trusted_route_logic(self) -> None:
        source = (WORKFLOW_ROOT / "platform-security.yml").read_text(encoding="utf-8")
        jobs = _job_blocks(source)
        classifier = jobs["classifier"]
        self.assertIn("Checkout candidate history as data only", classifier)
        self.assertIn("Capture candidate changed files without executing candidate code", classifier)
        self.assertIn("Resolve immutable default-branch classifier source", classifier)
        self.assertIn("Checkout trusted classifier source", classifier)
        self.assertIn("--files-file", classifier)
        self.assertNotIn("--repo-root \"$GITHUB_WORKSPACE\"", classifier)

    def test_all_retained_artifact_names_are_attempt_bound(self) -> None:
        for path in sorted(WORKFLOW_ROOT.glob("*.y*ml")):
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                if "name:" in line and "${{ github.run_id }}" in line:
                    with self.subTest(workflow=path.name, line=line.strip()):
                        self.assertIn("${{ github.run_attempt }}", line)

    def test_security_web_reports_are_attempt_bound_and_playwright_install_is_hermetic(self) -> None:
        security = (WORKFLOW_ROOT / "platform-security.yml").read_text(encoding="utf-8")
        self.assertIn("platform-web-playwright-report-${{ github.run_id }}-${{ github.run_attempt }}", security)
        self.assertNotIn("playwright install chromium", security)

    def test_live_user_qa_uses_only_the_fixed_host_dispatch_boundary(self) -> None:
        source = (WORKFLOW_ROOT / "platform-live-user-qa.yml").read_text(
            encoding="utf-8"
        )
        job = _job_blocks(source)["live-user-qa"]
        self.assertNotIn("actions/checkout@", job)
        self.assertIn("actions/download-artifact@", job)
        self.assertIn(
            "/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh",
            job,
        )
        self.assertIn("PLATFORM_LIVE_QA_TARGET_SHA", job)
        self.assertNotIn("/opt/oldsparky/platform/current/tools/", job)
        self.assertIn("live-user-qa", job)
        self.assertNotIn("bash -s", job)
        self.assertNotIn("platform_live_user_qa.sh", job)
        self.assertNotIn("platform_live_qa_mailbox_helper.py", job)

    def test_cloudflare_audit_has_no_candidate_import_or_checkout(self) -> None:
        source = (WORKFLOW_ROOT / "platform-cloudflare-readonly-audit.yml").read_text(
            encoding="utf-8"
        )
        job = _job_blocks(source)["audit"]
        self.assertNotIn("actions/checkout@", job)
        self.assertNotIn("cloudflare_readonly_audit.py", job)
        self.assertIn("Fixed, value-free Cloudflare API read-only projection", job)

    def test_autodeploy_archive_path_is_bounded_and_token_free_for_candidate_code(self) -> None:
        source = (WORKFLOW_ROOT / "platform-production-autodeploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform_safe_zip", source)
        self.assertNotIn("unzip -q", source)
        self.assertIn("env -u GH_TOKEN -u GITHUB_TOKEN /usr/bin/python3", source)
        self.assertIn("classifier reason is missing", source)

    def test_production_secret_classifier_extractor_is_bounded_and_no_candidate(self) -> None:
        source = (WORKFLOW_ROOT / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        production = _job_blocks(source)["production"]
        prerequisite = _job_blocks(source)["validate-classifier"]
        validator = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("O_NOFOLLOW", validator)
        self.assertIn("object_pairs_hook=_reject_duplicate_keys", validator)
        self.assertIn("type(manifest.get(\"runtime_sensitive\")) is not bool", validator)
        for job in (prerequisite, production):
            with self.subTest(job="production" if job is production else "validate-classifier"):
                self.assertIn("platform_production_classifier_artifact.py", job)
                self.assertIn("trusted_classifier_default.outputs.sha", job)
                self.assertIn("actions/checkout@", job)
                self.assertNotIn("env.TARGET_SHA", job)
                self.assertIn("--max-filesize 4194304", job)
                self.assertIn("rm -rf -- \"$route_dir\"", job)
        self.assertIn("- validate-classifier", _job_blocks(source)["build-release"])
        self.assertNotIn("/usr/bin/python3 \"$artifacts_metadata\"", production)
        self.assertNotIn("bundle.namelist()", production)


if __name__ == "__main__":
    unittest.main()
