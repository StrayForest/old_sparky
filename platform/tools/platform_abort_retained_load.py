#!/usr/bin/env python3
"""Stop only one exact production retained-load supervisor process tree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time


SCRIPT_PATH = "/root/old_sparky/platform/tools/platform_production_retained_load_matrix_qa.sh"
LOCK_PATH = "/run/lock/oldsparky-retained-load-matrix.lock"
CONFIRMATION = "ABORT-PRODUCTION-RETAINED-LOAD"
TRUSTED_REPO_ROOT = "/root/old_sparky"
RELEASE_PATH = "/opt/oldsparky/platform/current/RELEASE.json"
RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
ABORT_EXPORT_BASE = Path("/tmp")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("confirmation")
    parser.add_argument("target_sha")
    parser.add_argument("load_run_id")
    return parser.parse_args()


def cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def all_process_ids() -> list[int]:
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            result.append(int(entry.name))
    return result


def exact_roots(run_id: str) -> set[int]:
    roots: set[int] = set()
    for pid in all_process_ids():
        args = cmdline(pid)
        if SCRIPT_PATH in args and run_id in args:
            roots.add(pid)
    return roots


def children_by_parent() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid in all_process_ids():
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        close = raw.rfind(")")
        if close < 0:
            continue
        fields = raw[close + 2 :].split()
        if len(fields) < 2:
            continue
        try:
            parent = int(fields[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    return children


def process_tree(roots: set[int]) -> set[int]:
    children = children_by_parent()
    result = set(roots)
    pending = list(roots)
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def alive(pids: set[int]) -> set[int]:
    return {pid for pid in pids if Path(f"/proc/{pid}").exists()}


def signal_tree(pids: set[int], signum: signal.Signals) -> None:
    for pid in sorted(pids, reverse=True):
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError(f"cannot signal exact retained-load pid {pid}") from exc


def process_snapshot(pids: set[int]) -> str:
    if not pids:
        return "no matching process ids\n"
    result = subprocess.run(
        [
            "ps",
            "-o",
            "pid=,ppid=,stat=,etime=,pcpu=,pmem=,rss=,args=",
            "-p",
            ",".join(str(pid) for pid in sorted(pids)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout or result.stderr or "ps returned no process details\n"


def export_abort_evidence(run_id: str, snapshot: str) -> Path:
    run_root = RUN_ROOT_BASE / f"gha-{run_id}"
    export_dir = ABORT_EXPORT_BASE / f"old-sparky-production-retained-abort-{run_id}"
    if export_dir.exists() or export_dir.is_symlink():
        raise RuntimeError(f"abort evidence export already exists: {export_dir}")
    export_dir.mkdir(mode=0o700)
    os.chmod(export_dir, 0o700)

    snapshot_path = export_dir / "abort-process-tree.txt"
    snapshot_path.write_text(snapshot, encoding="utf-8")
    os.chmod(snapshot_path, 0o600)
    evidence_names = (
        "matrix.log",
        "qa-command.log",
        "server-observability.log",
        "matrix-summary.json",
    )
    for name in evidence_names:
        source = run_root / name
        destination = export_dir / name
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)

    caller_uid = int(os.environ.get("SUDO_UID", "0"))
    caller_gid = int(os.environ.get("SUDO_GID", "0"))
    os.chown(export_dir, caller_uid, caller_gid)
    for path in export_dir.iterdir():
        os.chown(path, caller_uid, caller_gid)
    return export_dir


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("retained-load abort must run as root")
    if args.confirmation != CONFIRMATION:
        raise SystemExit("refusing abort without exact confirmation")
    if not re.fullmatch(r"[0-9a-f]{40}", args.target_sha):
        raise SystemExit("target_sha must be a lowercase 40-character commit SHA")
    if not re.fullmatch(r"[0-9]+", args.load_run_id):
        raise SystemExit("load_run_id must be numeric")

    checkout_sha = subprocess.run(
        ["git", "-C", TRUSTED_REPO_ROOT, "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if checkout_sha != args.target_sha:
        raise SystemExit("trusted production checkout does not match target_sha")
    try:
        release_payload = Path(RELEASE_PATH).read_text(encoding="utf-8")
        release_sha = str(json.loads(release_payload).get("source_git_commit") or "")
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit("active production release metadata is unreadable") from exc
    if release_sha != args.target_sha:
        raise SystemExit("active production release does not match target_sha")

    roots = exact_roots(args.load_run_id)
    if not roots:
        print(f"No exact retained-load supervisor found for load run {args.load_run_id}.")
        return 0
    tree = process_tree(roots)
    print(f"Exact supervisor roots: {sorted(roots)}")
    print(f"Exact process tree: {sorted(tree)}")
    snapshot = process_snapshot(tree)
    print("Exact process snapshot:")
    print(snapshot, end="" if snapshot.endswith("\n") else "\n")
    signal_tree(tree, signal.SIGTERM)
    deadline = time.monotonic() + 30.0
    remaining = alive(tree)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = alive(tree)
    if remaining:
        print(f"Escalating SIGKILL for exact remaining pids: {sorted(remaining)}")
        signal_tree(remaining, signal.SIGKILL)
        time.sleep(0.5)
    remaining = alive(tree)
    if remaining:
        raise RuntimeError(f"exact retained-load processes remain: {sorted(remaining)}")
    if exact_roots(args.load_run_id):
        raise RuntimeError("an exact retained-load supervisor still exists after abort")
    export_dir = export_abort_evidence(args.load_run_id, snapshot)
    print(f"ABORT_EVIDENCE_EXPORT={export_dir}")
    print(f"ABORTED_PRODUCTION_RETAINED_LOAD={args.load_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
