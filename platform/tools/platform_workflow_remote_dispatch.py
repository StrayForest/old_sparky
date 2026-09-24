#!/usr/bin/env python3
"""Fixed-argv remote entry point for production workflow SSH calls.

The SSH command line is deliberately constant.  Dispatch data is read from
stdin as a bounded JSON document and validated before any production helper or
filesystem mutation is reached.  Values then cross only a local
``subprocess`` argv boundary; they are never interpreted by a remote shell.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess  # nosec B404 - all argv below is fixed or validated data.
import sys

try:
    from platform_workflow_input_guard import (
        DELETE_CONFIRMATION,
        WorkflowInputError,
        load_stdin_payload,
    )
except ModuleNotFoundError:  # Imported as ``tools.*`` by local tests.
    try:
        from tools.platform_workflow_input_guard import (
            DELETE_CONFIRMATION,
            WorkflowInputError,
            load_stdin_payload,
        )
    except ModuleNotFoundError:
        # Isolated staged execution deliberately removes the script directory
        # from sys.path.  Load only the fixed sibling guard in that contour.
        guard_path = Path(__file__).with_name("platform_workflow_input_guard.py")
        guard_spec = importlib.util.spec_from_file_location(
            "platform_workflow_input_guard_staged", guard_path
        )
        if guard_spec is None or guard_spec.loader is None:
            raise
        guard_module = importlib.util.module_from_spec(guard_spec)
        guard_spec.loader.exec_module(guard_module)
        DELETE_CONFIRMATION = guard_module.DELETE_CONFIRMATION
        WorkflowInputError = guard_module.WorkflowInputError
        load_stdin_payload = guard_module.load_stdin_payload


RUNTIME_ROOT = Path("/opt/oldsparky/platform")
ACTIVE_TOOLS_DIR = Path(__file__).parent
HOST_TOOLS_ROOT = RUNTIME_ROOT / "shared" / "host-tools"
EXTERNAL_HELPER = ACTIVE_TOOLS_DIR / "platform_production_external_fixture_qa.sh"
CLEANUP_HELPER = ACTIVE_TOOLS_DIR / "platform_production_retained_load_cleanup_qa.sh"
LIVE_HELPER = ACTIVE_TOOLS_DIR / "platform_live_launch_supervisor.sh"
LIVE_USER_QA_HELPER = ACTIVE_TOOLS_DIR / "platform_live_user_qa_dispatch.py"
TRUSTED_LIVE_ROOT = Path("/root/.oldsparky/liveqa")
TRUSTED_LIVE_LAUNCH = TRUSTED_LIVE_ROOT / "platform_live_launch_trusted.sh"
DEPLOY_HELPER = ACTIVE_TOOLS_DIR / "platform_production_deploy_supervisor.sh"
ARTIFACT_DIR_HELPER = ACTIVE_TOOLS_DIR / "platform_prepare_artifact_dir.py"
EXTERNAL_EXPORT_PREFIX = "/tmp/old-sparky-production-retained-load-"
CLEANUP_EXPORT_PREFIX = "/tmp/old-sparky-production-retained-cleanup-"
SUDO = "/usr/bin/sudo"
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
HOST_GENERATION_RE = re.compile(r"^[0-9a-f]{40}$")
HOST_TOOL_FILES = (
    "platform_workflow_remote_dispatch.py",
    "platform_workflow_input_guard.py",
    "platform_prepare_artifact_dir.py",
    "platform_production_deploy_supervisor.sh",
    "platform_release_lock.sh",
    "platform_release_preflight.sh",
    "platform_release_install.sh",
    "platform_release_transaction.py",
    "platform_release_restore_runtime.sh",
    "platform_validate_release_artifact.py",
    "platform_validate_wheelhouse.py",
    "platform_deploy_smoke.py",
    "platform_deploy_smoke_impl.py",
    "platform_safe_env_exec.py",
    "platform_render_service_envs.py",
    "platform_validate_edge_policy.py",
    "platform_update_cloudflare_ips.py",
    "platform_backup_restore_drill.py",
    "platform_configure_shared_env.py",
    "platform_storage_evidence_summary.py",
)


def _fail() -> int:
    # Never include payload, parser details, or command output in the public
    # workflow stream.  The caller may retain a separate fixed summary.
    print("remote workflow input is invalid", file=sys.stderr)
    return 2


def _run_sudo(helper: Path, arguments: list[str]) -> int:
    if not _trusted_helper(helper):
        return 2
    command = [SUDO, "-n", "--", str(helper), *arguments]
    completed = subprocess.run(command, check=False)  # nosec B603
    return completed.returncode


def _run_trusted_live_launch(arguments: list[str]) -> int:
    if not _trusted_live_launch_helper():
        return 2
    command = [SUDO, "-n", "--", str(TRUSTED_LIVE_LAUNCH), *arguments]
    completed = subprocess.run(command, check=False)  # nosec B603
    return completed.returncode


def _trusted_helper(helper: Path) -> bool:
    """Check the installed helper inode before crossing the sudo boundary."""

    if helper.parent != ACTIVE_TOOLS_DIR:
        return False
    try:
        metadata = helper.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and metadata.st_nlink == 1
        and not stat.S_IMODE(metadata.st_mode) & 0o022
        and stat.S_IMODE(metadata.st_mode) & 0o111
    )


def _trusted_data(path: Path) -> bool:
    if path.parent != ACTIVE_TOOLS_DIR:
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o444
    )


def _trusted_host_helper(path: Path) -> bool:
    if not _trusted_helper(path):
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_IMODE(metadata.st_mode) == 0o555


def _trusted_generation() -> bool:
    dispatcher_path = Path(__file__)
    if (
        not ACTIVE_TOOLS_DIR.is_absolute()
        or ACTIVE_TOOLS_DIR.parent != HOST_TOOLS_ROOT
        or dispatcher_path.parent != ACTIVE_TOOLS_DIR
        or dispatcher_path.name != "platform_workflow_remote_dispatch.py"
    ):
        return False
    if HOST_GENERATION_RE.fullmatch(ACTIVE_TOOLS_DIR.name) is None:
        return False
    try:
        metadata = ACTIVE_TOOLS_DIR.lstat()
        dispatcher_metadata = dispatcher_path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and metadata.st_nlink == 2
        and stat.S_IMODE(metadata.st_mode) == 0o555
        and stat.S_ISREG(dispatcher_metadata.st_mode)
        and dispatcher_metadata.st_uid == 0
        and dispatcher_metadata.st_gid == 0
        and dispatcher_metadata.st_nlink == 1
        and stat.S_IMODE(dispatcher_metadata.st_mode) == 0o555
    )


def _host_capabilities() -> int:
    """Return one bounded token only from an exact immutable generation."""

    if (
        not _trusted_generation()
        or not all(
            _trusted_host_helper(ACTIVE_TOOLS_DIR / name)
            for name in HOST_TOOL_FILES
        )
        or not _trusted_data(ACTIVE_TOOLS_DIR / "manifest.json")
        or not _trusted_data(ACTIVE_TOOLS_DIR / "capabilities.txt")
    ):
        return 2
    print(
        "HOST_TOOLS schema=1 "
        f"source_sha={ACTIVE_TOOLS_DIR.name} generation={ACTIVE_TOOLS_DIR.name} "
        "dispatcher=2 artifact_prepare=2 supervisor=2 input_guard=1 python_isolated=1"
    )
    return 0


def _trusted_live_launch_helper() -> bool:
    """Check the fixed installed launch entrypoint before crossing sudo."""

    try:
        root_metadata = TRUSTED_LIVE_ROOT.lstat()
        metadata = TRUSTED_LIVE_LAUNCH.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(root_metadata.st_mode)
        and root_metadata.st_uid == 0
        and root_metadata.st_gid == 0
        and not stat.S_IMODE(root_metadata.st_mode) & 0o022
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o755
    )


def _external_fixture(payload: dict[str, str]) -> int:
    arguments = [
        payload["confirmation"],
        payload["target_sha"],
        payload["control_email"],
        payload["setup_concurrency"],
        payload["run_id"],
        payload["profile"],
        payload["tournament_count"],
        payload["users_per_tournament"],
        payload["timeout_diagnostics"],
    ]
    if (
        EXTERNAL_HELPER.parent != ACTIVE_TOOLS_DIR
        or not EXTERNAL_HELPER.is_file()
        or EXTERNAL_HELPER.is_symlink()
    ):
        return 2
    # The SSH caller must return immediately while the origin supervisor owns
    # the long-running fixture.  ``start_new_session`` severs the SSH session;
    # the helper publishes its fixed supervisor.exit barrier on completion.
    command = [SUDO, "-n", "--", str(EXTERNAL_HELPER), *arguments]
    process = subprocess.Popen(  # nosec B603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    del process
    return 0


def _run_id_path(prefix: str, run_id: str, leaf: str) -> Path:
    # Keep this guard here as well as in the JSON parser because this function
    # owns a filesystem boundary and is directly unit-testable.  Construct the
    # path from constants only; no input is treated as an arbitrary fragment.
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid run id")
    return Path(f"{prefix}{run_id}") / leaf


def _touch_complete(payload: dict[str, str]) -> int:
    root = _run_id_path(EXTERNAL_EXPORT_PREFIX, payload["run_id"], "complete").parent
    if root.is_symlink() or not root.is_dir():
        return 1
    target = root / "complete"
    if target.is_symlink() or target.exists() and not target.is_file():
        return 1
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return 1
    else:
        os.close(descriptor)
    return 0


def _remove_exports(
    *, load_run_id: str, cleanup_run_id: str
) -> int:
    load_root = _run_id_path(EXTERNAL_EXPORT_PREFIX, load_run_id, "complete").parent
    cleanup_root = _run_id_path(
        CLEANUP_EXPORT_PREFIX, cleanup_run_id, "cleanup-summary.json"
    ).parent
    # These are the only files the two supervisors are allowed to export.  A
    # cleanup must fail closed on any extra entry instead of recursively
    # deleting an operator-created file, socket, mount or symlink.
    inventories = (
        (
            load_root,
            frozenset(
                {
                    "complete",
                    "ready",
                    "manifest.json",
                    "matrix-summary.json",
                    "canonical.log",
                    "server-observability.json",
                    "qa-command.log",
                    "server-observer.log",
                    "timeout-diagnostic-ids.json",
                    "supervisor.exit",
                }
            ),
        ),
        (
            cleanup_root,
            frozenset({"cleanup-summary.json", "canonical.log", "cleanup.log"}),
        ),
    )
    expected_uid = os.getuid()
    planned_removals: list[
        tuple[Path, os.stat_result, tuple[tuple[Path, os.stat_result], ...]]
    ] = []
    for root, allowed_names in inventories:
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            # A second cleanup after a successful first cleanup is a safe
            # no-op; this is the only absent-path case that is accepted.
            continue
        except OSError:
            return 1
        try:
            resolved_root = root.resolve()
        except (OSError, RuntimeError):
            return 1
        try:
            parent_metadata = root.parent.lstat()
        except OSError:
            return 1
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
            or root_metadata.st_uid == 0
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or root_metadata.st_dev != parent_metadata.st_dev
            or resolved_root != root
        ):
            return 1
        try:
            entries = list(root.iterdir())
        except OSError:
            return 1
        # Inventory before deletion, so an unknown entry cannot leave a
        # partially erased directory that looks successful to the caller.
        entry_metadata: list[tuple[Path, os.stat_result]] = []
        for entry in entries:
            if entry.name not in allowed_names:
                return 1
            try:
                metadata = entry.lstat()
            except OSError:
                return 1
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != root_metadata.st_dev
                or metadata.st_uid != expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                return 1
            entry_metadata.append((entry, metadata))
        planned_removals.append((root, root_metadata, tuple(entry_metadata)))

    # Inventory both roots before deleting either one.  This keeps an unknown
    # entry in the second root from turning the first root into a partial,
    # apparently successful cleanup.
    for root, root_metadata, entry_metadata in planned_removals:
        try:
            current_root_metadata = root.lstat()
        except OSError:
            return 1
        if (
            current_root_metadata.st_dev != root_metadata.st_dev
            or current_root_metadata.st_ino != root_metadata.st_ino
            or current_root_metadata.st_uid != expected_uid
            or stat.S_IMODE(current_root_metadata.st_mode) != 0o700
        ):
            return 1
        for entry, metadata in entry_metadata:
            try:
                current_entry_metadata = entry.lstat()
            except OSError:
                return 1
            if (
                current_entry_metadata.st_dev != metadata.st_dev
                or current_entry_metadata.st_ino != metadata.st_ino
                or current_entry_metadata.st_uid != metadata.st_uid
                or current_entry_metadata.st_gid != metadata.st_gid
                or current_entry_metadata.st_nlink != metadata.st_nlink
                or current_entry_metadata.st_mode != metadata.st_mode
            ):
                return 1
            try:
                entry.unlink()
            except OSError:
                return 1
        try:
            root.rmdir()
        except OSError:
            # An unexpected rmdir failure is a cleanup failure, never a
            # tolerated leftover.  The caller must fail the workflow.
            return 1
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return 1
        return 1
    return 0


def _prepare_deployment(payload: dict[str, str]) -> int:
    if payload["mode"] != "deploy" or not _trusted_generation():
        return 2
    if not _trusted_host_helper(ARTIFACT_DIR_HELPER):
        return 2
    # The host helper performs the privileged directory-relative open/mkdir
    # and inode recheck.  Do not replace it with ``sudo install -d``: a
    # pathname-only privileged mkdir leaves a symlink race at the final leaf.
    command = [
        SUDO,
        "-n",
        "--",
        str(ARTIFACT_DIR_HELPER),
        payload["artifact_remote_dir"],
    ]
    completed = subprocess.run(command, check=False)  # nosec B603
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["host-capabilities"]:
        return _host_capabilities()
    if arguments == ["external-fixture"]:
        mode = "external"
    elif arguments == ["external-finalize"]:
        mode = "external"
    elif arguments == ["external-cleanup"]:
        # Cleanup gets its own reduced identity document.  It must remain
        # usable when the measurement handoff (which also carries load
        # parameters and temporary session material) is unavailable or has
        # failed revalidation after fixture setup.
        mode = "cleanup"
    elif arguments == ["external-cleanup-exports"]:
        mode = "cleanup"
    elif arguments == ["retained-cleanup"]:
        mode = "cleanup"
    elif arguments == ["retained-cleanup-exports"]:
        mode = "cleanup"
    elif arguments == ["live-launch"]:
        mode = "live"
    elif arguments == ["live-user-qa"]:
        mode = "live"
    elif arguments in (["production-deploy"], ["production-prepare-artifact"]):
        mode = "deployment"
    else:
        return _fail()

    try:
        if mode == "deployment" and not _trusted_generation():
            return _fail()
        payload = load_stdin_payload(mode=mode)
        if arguments == ["external-fixture"]:
            return _external_fixture(payload)
        if arguments == ["external-finalize"]:
            return _touch_complete(payload)
        if arguments == ["external-cleanup"]:
            return _run_sudo(
                CLEANUP_HELPER,
                [
                    DELETE_CONFIRMATION,
                    payload["target_sha"],
                    payload["run_id"],
                    payload["control_email"],
                    payload["run_id"],
                ],
            )
        if arguments == ["external-cleanup-exports"]:
            return _remove_exports(
                load_run_id=payload["run_id"],
                cleanup_run_id=payload["run_id"],
            )
        if arguments == ["production-prepare-artifact"]:
            if payload["mode"] != "deploy":
                return _fail()
            return _prepare_deployment(payload)
        if arguments == ["retained-cleanup"]:
            return _run_sudo(
                CLEANUP_HELPER,
                [
                    DELETE_CONFIRMATION,
                    payload["target_sha"],
                    payload["load_run_id"],
                    payload["control_email"],
                    payload["cleanup_run_id"],
                ],
            )
        if arguments == ["retained-cleanup-exports"]:
            return _remove_exports(
                load_run_id=payload["load_run_id"],
                cleanup_run_id=payload["cleanup_run_id"],
            )
        if arguments == ["production-deploy"]:
            return _run_sudo(
                DEPLOY_HELPER,
                [
                    payload["target_sha"],
                    payload["release_slug"],
                    payload["mode"],
                    payload["artifact_remote_dir"],
                    payload["runtime_profile"],
                ],
            )
        if arguments == ["live-user-qa"]:
            if (
                payload["base_url"] != "https://old-sparky.com"
                or payload["provision"] != "false"
                or payload["marker"] != ""
            ):
                return _fail()
            return _run_sudo(LIVE_USER_QA_HELPER, [payload["target_sha"]])
        return _run_trusted_live_launch(
            [
                payload["base_url"],
                payload["provision"],
                payload["marker"],
                payload["target_sha"],
            ],
        )
    except (WorkflowInputError, OSError, ValueError, subprocess.SubprocessError):
        return _fail()


if __name__ == "__main__":
    raise SystemExit(main())
