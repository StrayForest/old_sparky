from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
import time
import unittest

import yaml


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PLATFORM_ROOT.parent / ".github" / "workflows"
PINNED_FINGERPRINT = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
FIXTURE_KEY = (
    "prod.example.test ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIGVDMqhOghijWNsE8pR7t9GsEB1QjjUlq1Eo0P3NSXSb"
)
FIXTURE_FINGERPRINT = "SHA256:ZoemANTrCA2lBr+MDvubTkeeysMSMMBm/nqGzh+1/rw"
WRONG_FINGERPRINT_KEY = (
    "prod.example.test ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIJBzGGy1QsD9hu3R8JmzMooE6nLyOnebCIzQgk09x9aI"
)
WRONG_FINGERPRINT = "SHA256:LddYowaSeS4K43x8OABxsQ5x/hf1iykbdiUbyDF3CR4"


def _workflow_runs(path: Path) -> list[tuple[str, int, str]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    runs: list[tuple[str, int, str]] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            runs.append((str(job_name), step_index, step["run"]))
    return runs


def _has_scan_command(run: str) -> bool:
    return any(
        not line.lstrip().startswith("#")
        and re.search(r"(?<![\w-])ssh-keyscan\b", line)
        for line in run.splitlines()
    )


def _active_shell_text(runs: list[tuple[str, int, str]]) -> str:
    return "\n".join(
        line
        for _, _, run in runs
        for line in run.splitlines()
        if not line.lstrip().startswith("#")
    )


def _extract_scan_template(source: str) -> str:
    start = source.find('ssh_scan="')
    keyscan = source.find("ssh-keyscan", start)
    install = re.search(
        r'^[ \t]*install -m \d+ "\$ssh_scan" [^\n]*known_hosts',
        source[keyscan:],
        re.MULTILINE,
    )
    if start < 0 or keyscan < 0 or install is None:
        raise AssertionError("workflow did not contain a complete SSH scan setup")
    return textwrap.dedent(source[start : keyscan + install.start()]).strip()


class ProductionSSHHostKeyScanContractTests(unittest.TestCase):
    def test_every_workflow_site_uses_the_same_bounded_pinned_contract(self) -> None:
        site_count = 0
        workflow_count = 0
        standard_blocks: list[str] = []
        as12_blocks: list[str] = []
        paths = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))
        for path in paths:
            runs = _workflow_runs(path)
            scan_runs = [item for item in runs if _has_scan_command(item[2])]
            if not scan_runs:
                continue
            workflow_count += 1
            active_text = _active_shell_text(runs)
            self.assertNotIn("ssh-keyscan -T 10", active_text, path.name)
            self.assertNotIn("StrictHostKeyChecking no", active_text, path.name)
            self.assertRegex(active_text, r"StrictHostKeyChecking(?: yes|=yes)", path.name)
            self.assertIn("UserKnownHostsFile", active_text, path.name)
            if path.name == "platform-production-as12-proof.yml":
                self.assertIn("as12-known-hosts.1", active_text, path.name)
                self.assertIn("as12-known-hosts.2", active_text, path.name)
            else:
                self.assertIn("known_hosts.scan.1", active_text, path.name)
                self.assertIn("known_hosts.scan.2", active_text, path.name)
            for _, _, run in scan_runs:
                site_count += 1
                block = _extract_scan_template(run)
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
                target = (
                    as12_blocks
                    if path.name == "platform-production-as12-proof.yml"
                    else standard_blocks
                )
                target.append(block)
        self.assertEqual(workflow_count, 20)
        self.assertEqual(site_count, 23)
        self.assertEqual(len(standard_blocks), 22)
        self.assertEqual(len(set(standard_blocks)), 1)
        self.assertEqual(len(as12_blocks), 1)
        self.assertEqual(
            as12_blocks[0],
            standard_blocks[0].replace(
                'ssh_scan="$ssh_dir/known_hosts.scan"',
                'ssh_scan="$RUNNER_TEMP/as12-known-hosts"',
            ),
        )

    def test_extracted_template_retries_once_and_cleans_every_candidate(self) -> None:
        workflow = WORKFLOW_DIR / "platform-live-user-qa.yml"
        scan_runs = [item for item in _workflow_runs(workflow) if _has_scan_command(item[2])]
        self.assertEqual(len(scan_runs), 1)
        template = _extract_scan_template(scan_runs[0][2])
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
              wrong-fingerprint)
                printf '%s\n' "$WRONG_FINGERPRINT_KEY"
                ;;
              malformed)
                printf '%s\n' 'prod.example.test ssh-ed25519 not-base64'
                ;;
              hang)
                exec sleep 5
                ;;
              *)
                echo "unknown scenario" >&2
                exit 2
                ;;
            esac
            """
        ).strip()

        scenarios = (
            "persistent-empty",
            "multiple",
            "wrong-host",
            "wrong-fingerprint",
            "malformed",
            "hang",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, key, fingerprint in (
                ("valid", FIXTURE_KEY, FIXTURE_FINGERPRINT),
                ("wrong", WRONG_FINGERPRINT_KEY, WRONG_FINGERPRINT),
            ):
                key_path = root / f"{name}.pub"
                key_path.write_text(key + "\n", encoding="utf-8")
                parsed = subprocess.run(
                    ["ssh-keygen", "-lf", str(key_path), "-E", "sha256"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                self.assertEqual(parsed.stdout.split()[1], fingerprint)
            self.assertNotEqual(FIXTURE_FINGERPRINT, WRONG_FINGERPRINT)
            stub_dir = root / "bin"
            stub_dir.mkdir()
            keyscan = stub_dir / "ssh-keyscan"
            keyscan.write_text(stub + "\n", encoding="utf-8")
            keyscan.chmod(0o755)

            def run(
                scenario: str,
                run_template: str = template,
            ) -> subprocess.CompletedProcess[str]:
                ssh_dir = root / f"ssh-{scenario}"
                ssh_dir.mkdir()
                count_file = root / f"{scenario}.count"
                script = "\n".join(
                    (
                        "set -euo pipefail",
                        f"ssh_dir={shlex.quote(str(ssh_dir))}",
                        'install -d -m 700 "$ssh_dir"',
                        'PROD_SSH_HOST="prod.example.test"',
                        run_template,
                    )
                )
                env = {
                    **os.environ,
                    "PATH": f"{stub_dir}:{os.environ['PATH']}",
                    "SCAN_COUNT_FILE": str(count_file),
                    "SCAN_SCENARIO": scenario,
                    "SCAN_KEY": FIXTURE_KEY,
                    "WRONG_FINGERPRINT_KEY": WRONG_FINGERPRINT_KEY,
                }
                return subprocess.run(
                    ["bash", "-c", script],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=5,
                )

            shortened_template = template.replace(
                "timeout --foreground 4s",
                "timeout --foreground 1s",
            )
            self.assertEqual(template.count("timeout --foreground 4s"), 1)
            success = run("first-fail-success")
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertIn("attempt 1/2 failed", success.stderr)
            self.assertEqual(
                (root / "first-fail-success.count").read_text(encoding="utf-8").strip(),
                "2",
            )
            success_dir = root / "ssh-first-fail-success"
            self.assertEqual((success_dir / "known_hosts.scan").stat().st_mode & 0o777, 0o600)
            self.assertFalse((success_dir / "known_hosts.scan.1").exists())
            self.assertFalse((success_dir / "known_hosts.scan.2").exists())

            for scenario in scenarios:
                started = time.monotonic()
                result = run(
                    scenario,
                    shortened_template if scenario == "hang" else template,
                )
                elapsed = time.monotonic() - started
                self.assertNotEqual(result.returncode, 0, scenario)
                self.assertIn("attempt 1/2 failed", result.stderr)
                if scenario == "hang":
                    self.assertLess(elapsed, 4)
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
