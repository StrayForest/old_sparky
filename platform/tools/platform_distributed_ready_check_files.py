#!/usr/bin/env python3
"""Privileged, allowlisted rendezvous-file access for distributed Ready Check QA.

The public generators use SSH only for this small control channel.  SSE and
agenda traffic never traverses the production host.  This helper deliberately
has no arbitrary path argument: it can write two marker kinds and read only
the exact files belonging to one numeric GitHub run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any


RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|password|secret|session.?token|ticket|credential)",
    re.IGNORECASE,
)
RUN_ID_RE = re.compile(r"^[0-9]+$")
SHARD_RE = re.compile(r"^[0-9]+$")
MAX_MARKER_BYTES = 256 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("read-file", "write-marker"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shard", default=None)
    parser.add_argument(
        "--kind",
        choices=("ready", "done", "control", "event-triggered", "manifest", "summary", "report", "qa-log"),
        required=True,
    )
    parser.add_argument(
        "--optional",
        action="store_true",
        help="Return a pending JSON object when an allowlisted marker is not present.",
    )
    return parser.parse_args()


def checked_run_root(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run id must be numeric")
    root = RUN_ROOT_BASE / f"gha-{run_id}"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("distributed Ready Check run root is missing")
    resolved = root.resolve(strict=True)
    if resolved != root or resolved.parent != RUN_ROOT_BASE.resolve(strict=True):
        raise ValueError("distributed Ready Check run root escaped the fixed base")
    return root


def checked_shard(shard: str | None) -> int:
    if shard is None or not SHARD_RE.fullmatch(shard):
        raise ValueError("shard must be numeric")
    value = int(shard)
    if not 0 <= value < 32:
        raise ValueError("shard is outside the bounded range")
    return value


def regular_file(path: Path, *, allow_manifest: bool = False) -> Path:
    resolved = path.resolve(strict=True)
    metadata = path.lstat()
    if (
        resolved != path.resolve()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("rendezvous files must be root-owned regular 0600 files")
    if not allow_manifest and path.stat().st_size > MAX_MARKER_BYTES:
        raise ValueError("rendezvous marker is too large")
    return path


def marker_path(root: Path, shard: int, kind: str) -> Path:
    if kind not in {"ready", "done"}:
        raise ValueError("marker kind is not writable")
    return root / "distributed" / f"shard-{shard}-{kind}.json"


def read_path(root: Path, shard: int | None, kind: str) -> Path:
    distributed = root / "distributed"
    if kind == "control":
        return distributed / "control.json"
    if kind == "event-triggered":
        return distributed / "event-triggered.json"
    if kind == "summary":
        return distributed / "matrix-summary.json"
    if kind == "report":
        return distributed / "distributed-report.json"
    if kind == "qa-log":
        return root / "qa-command.log"
    if kind == "manifest":
        if shard is None:
            raise ValueError("manifest reads require a shard")
        return distributed / f"shard-{shard}.json"
    raise ValueError("file kind is not readable")


def reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                raise ValueError("rendezvous markers must not contain credentials")
            reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive_keys(child)


def write_marker(root: Path, shard: int, kind: str) -> None:
    raw = sys.stdin.buffer.read(MAX_MARKER_BYTES + 1)
    if len(raw) > MAX_MARKER_BYTES:
        raise ValueError("rendezvous marker is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("rendezvous marker is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("rendezvous marker must be a JSON object")
    if str(payload.get("run_id")) != root.name.removeprefix("gha-"):
        raise ValueError("rendezvous marker run id does not match the selected run")
    if int(payload.get("shard", -1)) != shard:
        raise ValueError("rendezvous marker shard does not match the selected shard")
    reject_sensitive_keys(payload)
    destination = marker_path(root, shard, kind)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    os.chmod(destination, 0o600)


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("distributed rendezvous file helper must run as root")
    root = checked_run_root(args.run_id)
    shard = checked_shard(args.shard) if args.shard is not None else None
    if args.action == "write-marker":
        if shard is None:
            raise SystemExit("write-marker requires --shard")
        write_marker(root, shard, args.kind)
        return 0
    if args.optional and args.kind != "event-triggered":
        raise SystemExit("--optional is allowed only for event-triggered")
    path = read_path(root, shard, args.kind)
    if args.optional and not path.exists():
        sys.stdout.write(json.dumps({"status": "pending"}) + "\n")
        return 0
    allow_manifest = args.kind == "manifest"
    path = regular_file(path, allow_manifest=allow_manifest)
    data = path.read_bytes()
    if not allow_manifest and len(data) > MAX_MARKER_BYTES:
        raise SystemExit("rendezvous file is too large")
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
