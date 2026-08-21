#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOW_TEST_FIXTURE_MARKER = "secret-scan: allow-test-fixture"
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
TOKEN_PATTERNS = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
SENSITIVE_ENV_KEYS = frozenset(
    {
        "PLATFORM_SECRET_KEY",
        "PLATFORM_R2_ACCESS_KEY_ID",
        "PLATFORM_R2_SECRET_ACCESS_KEY",
        "PLATFORM_RESEND_API_KEY",
        "PLATFORM_TURNSTILE_SECRET_KEY",
        "PLATFORM_SUPPORT_SMTP_PASSWORD",
        "PLATFORM_BACKUP_R2_ACCESS_KEY_ID",
        "PLATFORM_BACKUP_R2_SECRET_ACCESS_KEY",
        "PLATFORM_BACKUP_ADMIN_URL",
        "PLATFORM_DATABASE_URL",
    }
)
SAFE_ENV_VALUE_MARKERS = (
    "CHANGE_ME",
    "replace-me",
    "platform_password",
    "example",
    "${",
    "<",
    "...",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [root / value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def _looks_like_real_env_secret(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    key, value = stripped.split("=", 1)
    if key.strip() not in SENSITIVE_ENV_KEYS:
        return False
    normalized = value.strip().strip("'\"")
    if not normalized or any(marker.lower() in normalized.lower() for marker in SAFE_ENV_VALUE_MARKERS):
        return False
    return True


def scan_file(path: Path, root: Path) -> list[Finding]:
    relative = path.relative_to(root).as_posix()
    if path.name.startswith(".env") and not path.name.endswith(".example"):
        return [Finding(relative, 0, "tracked_env_file")]
    if path.suffix.lower() in {".key", ".p12", ".pfx"}:
        return [Finding(relative, 0, "tracked_private_key_file")]
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_TEST_FIXTURE_MARKER in line:
            continue
        if PRIVATE_KEY_RE.search(line):
            findings.append(Finding(relative, line_number, "private_key_material"))
        for rule, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(relative, line_number, rule))
        if _looks_like_real_env_secret(line) and path.name != ".env.platform.example":
            findings.append(Finding(relative, line_number, "literal_sensitive_env_value"))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan tracked repository files for high-confidence secret material."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    paths = tracked_files(root)
    findings = [finding for path in paths for finding in scan_file(path, root)]
    payload = {"ok": not findings, "files_scanned": len(paths), "findings": findings}
    if args.as_json:
        print(
            json.dumps(
                {**payload, "findings": [asdict(finding) for finding in findings]},
                sort_keys=True,
            )
        )
    elif findings:
        for finding in findings:
            print(f"[FAIL] {finding.path}:{finding.line} ({finding.rule})")
    else:
        print(f"[OK] Secret scan passed for {payload['files_scanned']} tracked files.")
    return 0 if not findings else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Secret scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
