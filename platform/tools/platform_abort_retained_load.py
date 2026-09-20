#!/usr/bin/env python3
"""Stop only one exact production retained-load supervisor process tree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

try:
    from tools.platform_evidence_sanitizer import project_public_artifact, sanitize_log_file
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_evidence_sanitizer import project_public_artifact, sanitize_log_file

SCRIPT_PATHS = {
    "/opt/oldsparky/platform/current/tools/platform_production_external_fixture_qa.sh",
}
LOCK_PATH = "/run/lock/oldsparky-retained-load-matrix.lock"
CONFIRMATION = "ABORT-PRODUCTION-RETAINED-LOAD"
RELEASE_PATH = "/opt/oldsparky/platform/current/RELEASE.json"
RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
ABORT_EXPORT_BASE = Path("/tmp")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")


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
        if any(path in args for path in SCRIPT_PATHS) and run_id in args:
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
            raise RuntimeError("cannot signal exact retained-load process") from exc


def process_snapshot(pids: set[int]) -> str:
    if not pids:
        return '{"schema":1,"process_count":0,"snapshot_available":false}\n'
    result = subprocess.run(
        [
            "ps",
            "-o",
            "pid=",
            "-p",
            ",".join(str(pid) for pid in sorted(pids)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    # The abort artifact needs only bounded process-count evidence; retain no
    # command line, release path, URL or argument from the exact process tree.
    process_count = sum(1 for line in (result.stdout or "").splitlines() if line.strip())
    return json.dumps(
        {
            "schema": 1,
            "process_count": process_count,
            "snapshot_available": bool(result.stdout),
        },
        sort_keys=True,
    ) + "\n"


def _safe_process_snapshot(snapshot: str) -> dict[str, object]:
    """Project an abort snapshot even when called with a non-production value."""

    try:
        payload = json.loads(snapshot)
    except (TypeError, UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    process_count = payload.get("process_count")
    if (
        isinstance(process_count, bool)
        or not isinstance(process_count, int)
        or process_count < 0
        or process_count > 100_000
    ):
        process_count = 0
    snapshot_available = payload.get("snapshot_available")
    if type(snapshot_available) is not bool:
        snapshot_available = False
    return {
        "schema": 1,
        "process_count": process_count,
        "snapshot_available": snapshot_available,
    }


def _safe_matrix_summary(source: Path, destination: Path) -> None:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    safe_rows = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        synthetic_users = row.get("synthetic_users")
        if (
            isinstance(synthetic_users, bool)
            or not isinstance(synthetic_users, int)
            or synthetic_users < 0
            or synthetic_users > 1_000_000_000
        ):
            synthetic_users = 0
        safe_rows.append(
            {
                "synthetic_users": synthetic_users,
                "result": {
                    "passed": result.get("passed") is True,
                },
            }
        )
    def safe_count(key: str) -> int:
        value = payload.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 1_000_000_000
        ):
            return value
        return 0

    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in {"scale", "read-mix", "write-burst"}:
        mode = "other"
    destination.write_text(
        json.dumps(
            {
                "schema": 1,
                "mode": mode,
                "passed": payload.get("passed") is True,
                "control_account_preserved": payload.get("control_account_preserved") is True,
                "completed_tournaments": safe_count("completed_tournaments"),
                "completed_users": safe_count("completed_users"),
                "rows": safe_rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _project_server_observability(source: Path, destination: Path) -> None:
    """Export only aggregate observer evidence; keep identities private."""

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    projected = project_public_artifact("server_observability", payload)
    destination.write_text(
        json.dumps(projected, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def export_abort_evidence(run_id: str, snapshot: str) -> Path:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("load run id is invalid")
    run_root = RUN_ROOT_BASE / f"gha-{run_id}"
    export_dir = ABORT_EXPORT_BASE / f"old-sparky-production-retained-abort-{run_id}"
    if export_dir.exists() or export_dir.is_symlink():
        raise RuntimeError(f"abort evidence export already exists: {export_dir}")
    export_dir.mkdir(mode=0o700)
    os.chmod(export_dir, 0o700)

    snapshot_path = export_dir / "abort-process-tree.txt"
    snapshot_path.write_text(
        json.dumps(_safe_process_snapshot(snapshot), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(snapshot_path, 0o600)
    canonical_source = run_root / "canonical.log"
    canonical_destination = export_dir / "canonical.log"
    if canonical_source.is_file() and not canonical_source.is_symlink():
        # Old retained runs may predate the fixed canonical-log contract.
        # Re-summarize while the source is still private instead of copying
        # arbitrary process output into the abort export.
        sanitize_log_file(canonical_source, canonical_destination)
    else:
        canonical_destination.write_text(
            '{"schema":1,"status":"unavailable","line_count":0,"retained_line_count":0,"truncated":false,"class_counts":{"other":1},"route_class_counts":{"other":1},"status_counts":{}}\n',
            encoding="utf-8",
        )
    os.chmod(canonical_destination, 0o600)
    observer_sources = sorted(run_root.rglob("server-observability.json"))
    private_observer_sources = [
        path
        for path in observer_sources
        if path.is_file() and not path.is_symlink()
    ]
    observer_destination = export_dir / "server-observability.json"
    if private_observer_sources:
        # The private source may contain fixture/run bindings and process
        # identities used by the abort/cleanup owner. Never copy that source
        # into the export contract; project it while it is still private.
        _project_server_observability(private_observer_sources[0], observer_destination)
        # Abort has completed the identity-sensitive work. Remove the exact
        # private observer source (and the transport-specific copy when the
        # external fixture created one) after projection; cleanup no longer
        # needs process identities to delete the selected fixture.
        for private_source in private_observer_sources:
            private_source.unlink()
    else:
        _project_server_observability(
            Path("/dev/null"), observer_destination
        )
    os.chmod(observer_destination, 0o600)
    summary_sources = [run_root / "matrix-summary.json"]
    summary_sources.extend(sorted(run_root.glob("*/matrix-summary.json")))
    summary_source = next(
        (path for path in summary_sources if path.is_file() and not path.is_symlink()),
        None,
    )
    summary_destination = export_dir / "matrix-summary.json"
    if summary_source is not None:
        _safe_matrix_summary(summary_source, summary_destination)
    else:
        summary_destination.write_text(
            '{"schema":1,"passed":false,"control_account_preserved":false,"rows":[]}\n',
            encoding="utf-8",
        )
    os.chmod(summary_destination, 0o600)
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
    if RUN_ID_RE.fullmatch(args.load_run_id) is None:
        raise SystemExit("load_run_id must be numeric")

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
    # Exact process identities are an in-memory abort capability.  Report
    # only bounded counts so even a caller that captures helper stdout cannot
    # turn it into a PID/process inventory.
    print(f"Exact supervisor roots matched: {len(roots)}")
    print(f"Exact process tree matched: {len(tree)} process(es)")
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
        print(f"Escalating SIGKILL for exact remaining process(es): {len(remaining)}")
        signal_tree(remaining, signal.SIGKILL)
        time.sleep(0.5)
    remaining = alive(tree)
    if remaining:
        raise RuntimeError(
            f"exact retained-load processes remain: {len(remaining)}"
        )
    if exact_roots(args.load_run_id):
        raise RuntimeError("an exact retained-load supervisor still exists after abort")
    export_dir = export_abort_evidence(args.load_run_id, snapshot)
    print(f"ABORT_EVIDENCE_EXPORT={export_dir}")
    print(f"ABORTED_PRODUCTION_RETAINED_LOAD={args.load_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
