from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PLATFORM_ROOT.parent / ".github" / "workflows"
PINNED_FINGERPRINT = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
FIXTURE_KEY = (
    "prod.example.test ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIGVDMqhOghijWNsE8pR7t9GsEB1QjjUlq1Eo0P3NSXSb"
)
FIXTURE_FINGERPRINT = "SHA256:ZoemANTrCA2lBr+MDvubTkeeysMSMMBm/nqGzh+1/rw"


def _extract_scan_template(source: str) -> str:
    start = source.find('ssh_scan="')
    keyscan = source.find("ssh-keyscan", start)
    install = re.search(
        r'^\s+install -m \d+ "\$ssh_scan" .*known_hosts',
        source[keyscan:],
        re.MULTILINE,
    )
    if start < 0 or keyscan < 0 or install is None:
        raise AssertionError("workflow did not contain a complete SSH scan setup")
    return textwrap.dedent(source[start : keyscan + install.start()])


class ProductionSSHHostKeyScanContractTests(unittest.TestCase):
    def test_every_workflow_site_uses_the_same_bounded_pinned_contract(self) -> None:
        site_count = 0
        paths = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))
        for path in paths:
            source = path.read_text(encoding="utf-8")
            occurrences = list(re.finditer("ssh-keyscan", source))
            if not occurrences:
                continue
            self.assertNotIn("ssh-keyscan -T 10", source, path.name)
            self.assertNotIn("StrictHostKeyChecking no", source, path.name)
            self.assertRegex(source, r"StrictHostKeyChecking(?: yes|=yes)", path.name)
            self.assertIn("UserKnownHostsFile", source, path.name)
            if "as12-known-hosts" in source:
                self.assertIn("as12-known-hosts.1", source, path.name)
                self.assertIn("as12-known-hosts.2", source, path.name)
            else:
                self.assertIn("known_hosts.scan.1", source, path.name)
                self.assertIn("known_hosts.scan.2", source, path.name)
            for occurrence in occurrences:
                site_count += 1
                start = source.rfind("ssh_scan=", 0, occurrence.start())
                self.assertGreaterEqual(start, 0, path.name)
                block = _extract_scan_template(source[start:])
                self.assertIn("for ssh_attempt in 1 2; do", block, path.name)
                self.assertIn(
                    "timeout --foreground 4s ssh-keyscan -T 3 -t ed25519",
                    block,
                    path.name,
                )
                self.assertIn('>"$ssh_scan_attempt" 2>/dev/null', block, path.name)
                self.assertIn('install -m 600 /dev/null "$ssh_scan_attempt"', block, path.name)
                self.assertIn(
                    'NF == 3 && $1 == host && $2 == "ssh-ed25519"',
                    block,
                    path.name,
                )
                self.assertIn("NR == 1", block, path.name)
                self.assertIn(
                    'ssh-keygen -lf "$ssh_scan_attempt" -E sha256 2>/dev/null',
                    block,
                    path.name,
                )
                self.assertIn(PINNED_FINGERPRINT, block, path.name)
                self.assertIn("attempt %s/2 failed", block, path.name)
                self.assertIn("attempt %s/2 succeeded", block, path.name)
                self.assertIn("(( ssh_attempt == 2 )) || sleep 0.5", block, path.name)
                self.assertGreaterEqual(block.count('rm -f -- "$ssh_scan_attempt"'), 2, path.name)
        self.assertEqual(site_count, 23)

    def test_extracted_template_retries_once_and_cleans_every_candidate(self) -> None:
        source = (
            WORKFLOW_DIR / "platform-live-user-qa.yml"
        ).read_text(encoding="utf-8")
        template = _extract_scan_template(source)
        template = template.replace(PINNED_FINGERPRINT, FIXTURE_FINGERPRINT)

        stub = textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            count=0
            if [[ -s "$SCAN_COUNT_FILE" ]]; then
              count="$(cat "$SCAN_COUNT_FILE")"
            fi
            count=$((count + 1))
            printf '%s\n' "$count" > "$SCAN_COUNT_FILE"
            case "$SCAN_SCENARIO" in
              first-fail-success)
                if (( count == 1 )); then
                  printf '%s\n' '# prod.example.test:22 SSH-2.0-banner'
                  exit 1
                fi
                printf '%s\n' "$SCAN_KEY"
                ;;
              persistent-empty)
                exit 1
                ;;
              multiple)
                printf '%s\n%s\n' "$SCAN_KEY" "$SCAN_KEY"
                ;;
              wrong-host)
                printf '%s\n' "other.example.test ${SCAN_KEY#* }"
                ;;
              malformed)
                printf '%s\n' 'prod.example.test ssh-ed25519 not-base64'
                ;;
              *)
                echo "unknown scenario" >&2
                exit 2
                ;;
            esac
            """
        ).strip()

        scenarios = ("persistent-empty", "multiple", "wrong-host", "malformed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub_dir = root / "bin"
            stub_dir.mkdir()
            keyscan = stub_dir / "ssh-keyscan"
            keyscan.write_text(stub + "\n", encoding="utf-8")
            keyscan.chmod(0o755)

            def run(scenario: str) -> subprocess.CompletedProcess[str]:
                ssh_dir = root / f"ssh-{scenario}"
                ssh_dir.mkdir()
                count_file = root / f"{scenario}.count"
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        f"ssh_dir={shlex.quote(str(ssh_dir))}",
                        'install -d -m 700 "$ssh_dir"',
                        'PROD_SSH_HOST="prod.example.test"',
                        template,
                    )
                )
                env = {
                    **os.environ,
                    "PATH": f"{stub_dir}:{os.environ['PATH']}",
                    "SCAN_COUNT_FILE": str(count_file),
                    "SCAN_SCENARIO": scenario,
                    "SCAN_KEY": FIXTURE_KEY,
                }
                return subprocess.run(
                    ["bash", "-c", script],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

            success = run("first-fail-success")
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(
                (root / "first-fail-success.count").read_text(encoding="utf-8").strip(),
                "2",
            )
            success_dir = root / "ssh-first-fail-success"
            self.assertEqual((success_dir / "known_hosts.scan").stat().st_mode & 0o777, 0o600)
            self.assertFalse((success_dir / "known_hosts.scan.1").exists())
            self.assertFalse((success_dir / "known_hosts.scan.2").exists())

            for scenario in scenarios:
                result = run(scenario)
                self.assertNotEqual(result.returncode, 0, scenario)
                self.assertEqual(
                    (root / f"{scenario}.count").read_text(encoding="utf-8").strip(),
                    "2",
                    scenario,
                )
                failure_dir = root / f"ssh-{scenario}"
                self.assertFalse((failure_dir / "known_hosts.scan").exists(), scenario)
                self.assertFalse((failure_dir / "known_hosts.scan.1").exists(), scenario)
                self.assertFalse((failure_dir / "known_hosts.scan.2").exists(), scenario)


if __name__ == "__main__":
    unittest.main()
