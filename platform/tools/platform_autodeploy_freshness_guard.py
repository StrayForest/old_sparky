#!/usr/bin/env python3
"""Fail closed unless the live ``dev`` branch still names the tested SHA."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_RESPONSE_BYTES = 64 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def validate_dev_response(raw: bytes, expected_sha: str) -> bool:
    """Return true only for a bounded GitHub branch response bound to SHA."""

    if not isinstance(expected_sha, str) or SHA_RE.fullmatch(expected_sha) is None:
        return False
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESPONSE_BYTES:
        return False
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    commit = payload.get("commit")
    return isinstance(commit, dict) and commit.get("sha") == expected_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args(argv)
    try:
        with Path(args.response).open("rb") as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
    except OSError:
        return 2
    if not validate_dev_response(raw, args.expected_sha):
        print("current dev source does not match the exact tested commit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
