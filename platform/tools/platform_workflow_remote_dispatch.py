#!/usr/bin/env python3
"""Fixed-argv remote entry point for production workflow SSH calls.

The SSH command line is deliberately constant.  Dispatch data is read from
stdin as a bounded JSON document and validated before any production helper or
filesystem mutation is reached.  Validated values cross only closed argv or
stdin boundaries; they are never interpolated into a remote shell command.
"""

from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess  # nosec B404 - all argv below is fixed or validated data.
import sys
import time
from typing import Any


def _is_immutable_host_tools_dispatcher(path: Path) -> bool:
    """Recognize the release-independent host-tools dispatcher path."""

    try:
        resolved = path.resolve()
    except OSError:
        return False
    return bool(
        resolved.name == "platform_workflow_remote_dispatch.py"
        and re.fullmatch(r"[0-9a-f]{40}", resolved.parent.name) is not None
        and resolved.parent.parent.name == "host-tools"
    )


def _invoked_with_explicit_bytecode_flag() -> bool:
    """Require a literal interpreter ``-B`` flag for immutable generations.

    ``sys.dont_write_bytecode`` is also enabled by ``PYTHONDONTWRITEBYTECODE``
    (and therefore cannot prove that the caller supplied the required
    interpreter flag).  The production host is Linux, so inspect the kernel's
    immutable process command line before importing any sibling module.  A
    missing or unreadable command line fails closed.
    """

    try:
        argv = Path("/proc/self/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [argument for argument in argv if argument]
    if len(argv) < 2:
        return False
    script_argument = os.fsencode(str(Path(__file__)))
    try:
        script_index = argv.index(script_argument, 1)
    except ValueError:
        # The regression test executes the fixed source through ``-c`` so it
        # can patch only local metadata checks.  Keep the same explicit-flag
        # contract for that interpreter contour.
        try:
            script_index = argv.index(b"-c", 1)
        except ValueError:
            return False
    return b"-B" in argv[1:script_index]


# ``-I`` isolates imports, but it does not disable Python's implicit
# ``__pycache__`` writes.  A generation is root-owned and immutable by
# contract; fail before importing the sibling guard if an operator invokes
# this entrypoint without ``-B``.  This also prevents a truncated pyc from
# being left behind when the caller applies a tight output/file-size limit.
if _is_immutable_host_tools_dispatcher(Path(__file__)) and not _invoked_with_explicit_bytecode_flag():
    raise SystemExit(2)

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
CPU_DIAGNOSTIC_PLAN_HELPER = ACTIVE_TOOLS_DIR / "platform_cpu_diagnostic_plan.py"
CPU_DIAGNOSTIC_OUTPUT_CAP = 8192
CPU_USAGE_ROW_FIELDS = {
    "service", "phase", "expected_targets", "observed_targets", "event_count",
    "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min",
    "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max", "duplicate_count",
    "timing_complete",
}
CPU_PROFILE_ROW_FIELDS = {
    "service", "expected_targets", "observed_targets", "event_count", "timer",
    "observation_unit", "total_cpu_us", "sample_count", "start_lag_ms_min",
    "start_lag_ms_max", "elapsed_ms_min", "elapsed_ms_max", "end_lag_ms_min",
    "end_lag_ms_max", "categories",
}
CPU_PROFILE_CATEGORY_FIELDS = {"category", "cpu_us", "observations"}
CPU_PROFILE_CATEGORIES = {
    "repo.get_tournament_workspace", "repo.get_tournament_workspace_by_slug",
    "repo.workspace_conditional_preflight", "repo.get_current_user",
    "repo.get_current_user_optional", "repo.get_server_request_correlation_headers",
    "repo.run_with_ssr_trace", "repo.workspace_api_fetch", "repo.workspace_page",
    "orm_result", "db_driver", "async_event_loop", "serialization_validation",
    "crypto", "web_framework", "http_client", "other",
}
TRUSTED_LIVE_ROOT = Path("/root/.oldsparky/liveqa")
TRUSTED_LIVE_LAUNCH = TRUSTED_LIVE_ROOT / "platform_live_launch_trusted.sh"
DEPLOY_HELPER = ACTIVE_TOOLS_DIR / "platform_production_deploy_supervisor.sh"
ARTIFACT_DIR_HELPER = ACTIVE_TOOLS_DIR / "platform_prepare_artifact_dir.py"
RETAINED_LOAD_EXPORT_EXECUTOR = (
    ACTIVE_TOOLS_DIR / "platform_retained_load_export_executor.py"
)
EXTERNAL_EXPORT_PREFIX = "/tmp/old-sparky-production-retained-load-"
CLEANUP_EXPORT_PREFIX = "/tmp/old-sparky-production-retained-cleanup-"
SUDO = "/usr/bin/sudo"
SETPRIV = "/usr/bin/setpriv"
SYSTEM_PYTHON = "/usr/bin/python3.12"
EXPORT_EXECUTOR_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
}
DEPLOY_OPERATION_TIMEOUT_SECONDS = 1950.0
CLEANUP_OPERATION_TIMEOUT_SECONDS = 300.0
ARTIFACT_PREP_OPERATION_TIMEOUT_SECONDS = 120.0
LIVE_USER_QA_OPERATION_TIMEOUT_SECONDS = 300.0
LIVE_LAUNCH_OPERATION_TIMEOUT_SECONDS = 300.0
CHILD_TERMINATION_GRACE_SECONDS = 5.0
LIVE_LAUNCH_STATUS_MAX_BYTES = 256
LIVE_LAUNCH_STREAM_MAX_BYTES = 4096
LIVE_LAUNCH_PROTOCOL_MAX_BYTES = 1024
LIVE_QA_DIAGNOSTIC_LINE_MAX_BYTES = 512
LIVE_QA_DIAGNOSTIC_RE = re.compile(
    rb"LIVE_QA_CHILD_DIAGNOSTIC schema=1 kind=(?P<kind>none|playwright_cli_usage|"
    rb"node_module_missing|browser_executable_missing|browser_launch_error|"
    rb"child_timeout|cleanup_failure|unclassified) stdout_bytes=(?P<stdout_bytes>[0-9]{1,16}) "
    rb"stderr_bytes=(?P<stderr_bytes>[0-9]{1,16}) "
    rb"truncated=(?P<truncated>true|false) child_exit=(?P<child_exit>[0-9]{1,3})\n"
)
LIVE_LAUNCH_CHECK_IDS = frozenset(
    {
        "none",
        "input_shape",
        "input_validation",
        "source_binding",
        "source_binding_io",
        "source_binding_schema",
        "source_binding_recheck",
        "root_uid",
        "source_identity",
        "install_root_format",
        "origin",
        "install_root_target",
        "provision_mode",
        "provision_marker",
        "marker_absent",
        "marker_digest",
        "generation_members",
        "generation_supervisor_path",
        "generation_supervisor_metadata",
        "generation_manifest",
        "identity",
        "account_install",
        "provision",
        "browser_qa",
        "release_lock",
        "trusted_entry",
        "supervisor_exec",
        "dispatch",
        "timeout",
        "stream_limit",
        "protocol",
    }
)
LIVE_BROWSER_COUNTS_MAX_BYTES = 768
LIVE_USER_QA_MARKER_MAX_BYTES = LIVE_BROWSER_COUNTS_MAX_BYTES
LIVE_BROWSER_COUNT_FIELDS = (
    "logical_total",
    "logical_pass",
    "logical_fail",
    "logical_expected_fail",
    "logical_flaky",
    "logical_skip",
    "logical_interrupted",
    "attempt_total",
    "attempt_pass",
    "attempt_fail",
    "attempt_skip",
    "attempt_interrupted",
    "attempt_timedout",
)
LIVE_LAUNCH_SUPERVISOR_FAILURE_STAGES = frozenset(
    {
        "validation",
        "trusted_generation",
        "identity",
        "account_install",
        "provision",
        "browser_qa",
    }
)
LIVE_LAUNCH_PRE_SUPERVISOR_FAILURE_STAGES = frozenset({"dispatch"})
LIVE_LAUNCH_FAILURE_STAGES = LIVE_LAUNCH_SUPERVISOR_FAILURE_STAGES | frozenset(
    {
        "dispatch",
        "trusted_entry",
        "timeout",
    }
)
LIVE_LAUNCH_STATUS_RE = re.compile(
    rb"LIVE_LAUNCH_STATUS schema=2 status=(?P<status>passed|failed) "
    rb"stage=(?P<stage>validation|trusted_generation|identity|account_install|"
    rb"provision|browser_qa|dispatch|trusted_entry|timeout|complete) "
    rb"check=(?P<check>none|input_shape|input_validation|source_binding|"
    rb"source_binding_io|source_binding_schema|source_binding_recheck|root_uid|"
    rb"source_identity|install_root_format|origin|install_root_target|"
    rb"provision_mode|provision_marker|marker_absent|marker_digest|"
    rb"generation_members|generation_supervisor_path|"
    rb"generation_supervisor_metadata|generation_manifest|identity|"
    rb"account_install|provision|browser_qa|release_lock|trusted_entry|"
    rb"supervisor_exec|dispatch|timeout|stream_limit|protocol) "
    rb"child_exit=(?P<child_exit>0|[1-9][0-9]{0,2}) "
    rb"source_sha=(?P<source_sha>[0-9a-f]{40})\n"
)
LIVE_BROWSER_COUNTS_RE = re.compile(
    rb"LIVE_BROWSER_COUNTS schema=1 run_status="
    rb"(?P<run_status>passed|failed|timedout|interrupted) "
    + b" ".join(
        rf"{field}=(?P<{field}>(?:0|[1-9][0-9]{{0,4}}))".encode("ascii")
        for field in LIVE_BROWSER_COUNT_FIELDS
    )
    + rb" source_sha=(?P<source_sha>[0-9a-f]{40}) "
    + rb"app_sha=(?P<app_sha>[0-9a-f]{40}) "
    + rb"marker_sha256=(?P<marker_sha256>[0-9a-f]{64})\n"
)
RELEASE_MARKER_MAX_BYTES = 512
RELEASE_MARKER_OBSERVED_BYTES_MAX = RELEASE_MARKER_MAX_BYTES + 1
RETAINED_CLEANUP_MARKER_MAX_BYTES = 256
RETAINED_CLEANUP_STAGES = frozenset(
    {
        "lock",
        "input",
        "identity",
        "release_binding",
        "run_root",
        "external_vote_recovery",
        "orphan_cleanup",
        "matrix_cleanup",
        "export_cleanup",
        "complete",
    }
)
RETAINED_CLEANUP_DIAGNOSTIC_STAGES = RETAINED_CLEANUP_STAGES | {
    "dispatcher",
    "timeout",
    "unknown",
}
RETAINED_CLEANUP_MARKER_RE = re.compile(
    rb"RETAINED_CLEANUP_STAGE schema=1 stage="
    rb"(?P<stage>lock|input|identity|release_binding|run_root|"
    rb"external_vote_recovery|orphan_cleanup|matrix_cleanup|export_cleanup|complete) "
    rb"exit_code=(?P<exit_code>0|[1-9][0-9]{0,2})"
)
RELEASE_MARKER_RE = re.compile(
    rb"RELEASE_DEPLOY schema=1 status=(?P<status>passed|failed) "
    rb"class=(?P<class>preflight|artifact|deployment)"
    rb"(?: phase=(?P<phase>preflight|artifact|provenance|candidate|readiness)"
    rb" reason=(?P<reason>internal|host_tools_invalid|lock|environment|"
    rb"service_state|nginx_config|preflight_failed|artifact_missing|"
    rb"artifact_count_invalid|checksum_missing|provenance_missing|"
    rb"artifact_name_invalid|release_slug_mismatch|checksum_mismatch|"
    rb"validation_failed|provenance_invalid|candidate_missing|"
    rb"activation_failed|lock_lost|runtime_profile_failed|baseline_changed)"
    rb"(?: lock_stage=(?P<lock_stage>helper_metadata|release_supervise|"
    rb"release_open|retained_supervise|retained_open))?)? "
    rb"release_slug=(?P<release_slug>[A-Za-z0-9][A-Za-z0-9._-]{0,179}) "
    rb"source_sha=(?P<source_sha>[0-9a-f]{40,64})\n\Z"
)
RELEASE_FAILURE_REASONS = {
    ("preflight", "preflight"): frozenset(
        {
            "internal",
            "host_tools_invalid",
            "lock",
            "environment",
            "service_state",
            "nginx_config",
            "preflight_failed",
            "baseline_changed",
        }
    ),
    ("artifact", "artifact"): frozenset(
        {
            "artifact_missing",
            "artifact_count_invalid",
            "checksum_missing",
            "artifact_name_invalid",
            "checksum_mismatch",
            "validation_failed",
        }
    ),
    ("artifact", "provenance"): frozenset(
        {"provenance_missing", "release_slug_mismatch", "provenance_invalid"}
    ),
    ("deployment", "candidate"): frozenset(
        {"candidate_missing", "activation_failed", "lock_lost"}
    ),
    ("deployment", "readiness"): frozenset({"runtime_profile_failed"}),
}
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
EXTERNAL_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOST_GENERATION_RE = re.compile(r"^[0-9a-f]{40}$")
HOST_TOOL_FILES = (
    "platform_workflow_remote_dispatch.py",
    "platform_workflow_input_guard.py",
    "platform_prepare_artifact_dir.py",
    "platform_retained_load_export_executor.py",
    "platform_production_deploy_supervisor.sh",
    "platform_release_lock.sh",
    "platform_release_preflight.sh",
    "platform_validate_release_artifact.py",
    "platform_safe_env_exec.py",
    "platform_render_service_envs.py",
    "platform_validate_edge_policy.py",
    "platform_update_cloudflare_ips.py",
    "platform_configure_shared_env.py",
    "platform_storage_evidence_summary.py",
    "platform_cpu_diagnostic_plan.py",
)
HOST_TOOLS_INVENTORY = frozenset((*HOST_TOOL_FILES, "manifest.json", "capabilities.txt"))
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
MAX_RELEASE_JSON_BYTES = 64 * 1024
SOURCE_BINDING_ARGUMENT_MAX_BYTES = 16 * 1024


def _parse_active_release_receipt(raw: bytes, *, release_slug: str) -> dict[str, object]:
    """Validate the live receipt with the pinned artifact validator contract."""

    validator_path = ACTIVE_TOOLS_DIR / "platform_validate_release_artifact.py"
    validator_source = _stable_host_file(validator_path, mode=0o555)
    if validator_source is None:
        raise OSError("pinned release validator is unavailable")
    namespace: dict[str, object] = {"__file__": str(validator_path), "__name__": "_pinned_release_validator"}
    try:
        exec(compile(validator_source, str(validator_path), "exec"), namespace)
        parser = namespace.get("_parse_release_json")
        if not callable(parser):
            raise OSError("pinned release validator is invalid")
        parsed = parser(
            raw,
            release_slug=release_slug,
            allow_legacy_runtime_layout=True,
        )
    except Exception as exc:
        raise OSError("active release receipt is invalid") from exc
    if not isinstance(parsed, dict):
        raise OSError("active release receipt is invalid")
    return parsed


def _release_baseline() -> dict[str, object]:
    """Read the exact active release identity without trusting release code."""

    if os.geteuid() != 0:
        raise OSError("baseline query requires root")
    runtime = RUNTIME_ROOT
    shared = runtime / "shared"
    releases = runtime / "releases"
    current = runtime / "current"
    pending_paths = (
        shared / ".release-operation.json",
        shared / ".release-systemd-state.json",
    )

    def trusted_directory(path: Path) -> os.stat_result:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink < 2
            or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise OSError("baseline directory metadata is unsafe")
        return metadata

    def pending_absent() -> None:
        for pending in pending_paths:
            try:
                pending.lstat()
            except FileNotFoundError:
                continue
            raise OSError("pending release transaction exists")

    if runtime == Path("/opt/oldsparky/platform"):
        trusted_directory(Path("/"))
        trusted_directory(Path("/opt"))
        trusted_directory(Path("/opt/oldsparky"))
    trusted_directory(runtime)
    trusted_directory(releases)
    trusted_directory(shared)
    pending_absent()

    link_before = current.lstat()
    if (
        not stat.S_ISLNK(link_before.st_mode)
        or link_before.st_uid != 0
        or link_before.st_gid != 0
        or link_before.st_nlink != 1
    ):
        raise OSError("active release pointer is unsafe")
    release = current.resolve(strict=True)
    if release.parent != releases or RELEASE_SLUG_RE.fullmatch(release.name) is None:
        raise OSError("active release path is invalid")
    release_before = trusted_directory(release)
    shared_before = shared.lstat()
    release_json = release / "RELEASE.json"
    json_before = release_json.lstat()
    if (
        not stat.S_ISREG(json_before.st_mode)
        or stat.S_ISLNK(json_before.st_mode)
        or json_before.st_uid != 0
        or json_before.st_gid != 0
        or json_before.st_nlink != 1
        or json_before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or stat.S_IMODE(json_before.st_mode) & 0o022
        or stat.S_IMODE(json_before.st_mode) != 0o444
        or json_before.st_size > MAX_RELEASE_JSON_BYTES
    ):
        raise OSError("active release receipt is unsafe")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("no-follow opens are unavailable")
    descriptor = os.open(
        release_json,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (json_before.st_dev, json_before.st_ino, json_before.st_size, json_before.st_mtime_ns, json_before.st_ctime_ns)
        ):
            raise OSError("active release receipt changed while opening")
        chunks: list[bytes] = []
        remaining = MAX_RELEASE_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > MAX_RELEASE_JSON_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            or release_json.lstat().st_ino != opened.st_ino
        ):
            raise OSError("active release receipt changed while reading")
    finally:
        os.close(descriptor)
    payload = _parse_active_release_receipt(raw, release_slug=release.name)
    source_sha = payload.get("source_git_commit")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise OSError("active release source SHA is malformed")

    link_after = current.lstat()
    release_after = release.lstat()
    shared_after = shared.lstat()
    if (
        (link_before.st_dev, link_before.st_ino, link_before.st_ctime_ns)
        != (link_after.st_dev, link_after.st_ino, link_after.st_ctime_ns)
        or (
            release_before.st_dev,
            release_before.st_ino,
            release_before.st_uid,
            release_before.st_gid,
            release_before.st_nlink,
            release_before.st_mode,
            release_before.st_ctime_ns,
        )
        != (
            release_after.st_dev,
            release_after.st_ino,
            release_after.st_uid,
            release_after.st_gid,
            release_after.st_nlink,
            release_after.st_mode,
            release_after.st_ctime_ns,
        )
        or (
            shared_before.st_dev,
            shared_before.st_ino,
            shared_before.st_uid,
            shared_before.st_gid,
            shared_before.st_mode,
            shared_before.st_ctime_ns,
        )
        != (
            shared_after.st_dev,
            shared_after.st_ino,
            shared_after.st_uid,
            shared_after.st_gid,
            shared_after.st_mode,
            shared_after.st_ctime_ns,
        )
    ):
        raise OSError("active release identity changed during query")
    pending_absent()
    return {
        "schema": 1,
        "source_sha": source_sha,
        "release_slug": release.name,
        "release_json_sha256": hashlib.sha256(raw).hexdigest(),
        "current_link_dev": link_after.st_dev,
        "current_link_ino": link_after.st_ino,
        "release_dev": release_after.st_dev,
        "release_ino": release_after.st_ino,
        "pending_operation": False,
    }


def _baseline_identity_matches(expected: object, actual: object) -> bool:
    """Require the exact, typed, closed tuple returned by the pinned reader."""

    keys = {
        "schema",
        "source_sha",
        "release_slug",
        "release_json_sha256",
        "current_link_dev",
        "current_link_ino",
        "release_dev",
        "release_ino",
        "pending_operation",
    }

    def valid(payload: object) -> bool:
        if not isinstance(payload, dict) or set(payload) != keys:
            return False
        if type(payload.get("schema")) is not int or payload["schema"] != 1:
            return False
        if payload.get("pending_operation") is not False:
            return False
        if (
            type(payload.get("source_sha")) is not str
            or SOURCE_SHA_RE.fullmatch(payload["source_sha"]) is None
            or type(payload.get("release_slug")) is not str
            or RELEASE_SLUG_RE.fullmatch(payload["release_slug"]) is None
            or type(payload.get("release_json_sha256")) is not str
            or HEX_DIGEST_RE.fullmatch(payload["release_json_sha256"]) is None
        ):
            return False
        return all(
            type(payload.get(name)) is int
            and 0 <= payload[name] <= 2**63 - 1
            for name in (
                "current_link_dev",
                "current_link_ino",
                "release_dev",
                "release_ino",
            )
        )

    return valid(expected) and valid(actual) and expected == actual


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_baseline_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise OSError("baseline tuple is malformed") from None


def _fail() -> int:
    # Never include payload, parser details, or command output in the public
    # workflow stream.  The caller may retain a separate fixed summary.
    print("remote workflow input is invalid", file=sys.stderr)
    return 2


def _run_sudo(
    helper: Path,
    arguments: list[str],
    *,
    timeout_seconds: float,
    expected_release_marker: tuple[str, str, str] | None = None,
) -> int:
    if not _trusted_helper(helper):
        return 2
    command = [SUDO, "-n", "--", str(helper), *arguments]
    return _run_bounded_child(
        command,
        timeout_seconds=timeout_seconds,
        expected_release_marker=expected_release_marker,
    )


def _run_cpu_diagnostic_plan(payload: dict[str, Any]) -> int:
    """Cross the pinned root boundary for one fixed, bounded plan operation."""

    if not _trusted_helper(CPU_DIAGNOSTIC_PLAN_HELPER):
        return 2
    operation = payload.get("operation")
    if operation == "prepare":
        helper_command = "prepare-stdin"
        expected_keys = {"status", "api_target_count", "web_target_count", "release_slug"}
        helper_payload = {key: value for key, value in payload.items() if key != "operation"}
    elif operation == "cleanup":
        helper_command = "cleanup-stdin"
        expected_keys = {
            "status", "service_count", "usage_status", "usage_reason", "usage_rows",
            "profile_status", "profile_reason", "profile_rows",
        }
        helper_payload = {"schema": payload["schema"], "run_id": payload["run_id"]}
    else:
        return 2
    helper_payload = {"schema": helper_payload["schema"], **helper_payload}
    child_input = json.dumps(
        helper_payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(child_input) > 4096:
        return 2
    try:
        process = subprocess.Popen(  # nosec B603
            [SUDO, "-n", "--", str(CPU_DIAGNOSTIC_PLAN_HELPER), helper_command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            umask=0o077,
        )
    except OSError:
        return 2
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(child_input)
        process.stdin.flush()
        process.stdin.close()
        deadline = time.monotonic() + 30
        output = bytearray()
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        eof = False
        while process.poll() is None or not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                return 124
            for key, _mask in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, CPU_DIAGNOSTIC_OUTPUT_CAP + 1 - len(output))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    eof = True
                    continue
                output.extend(chunk)
                if len(output) > CPU_DIAGNOSTIC_OUTPUT_CAP:
                    _terminate_process_group(process)
                    return 2
        child_status = process.wait(timeout=0)
        if child_status != 0 or not output.endswith(b"\n") or output.count(b"\n") != 1:
            return 2
        helper_prefix = b"CPU_DIAGNOSTIC_PLAN "
        if not output.startswith(helper_prefix):
            return 2
        result = _strict_baseline_json(output[len(helper_prefix):-1])
        if operation == "prepare":
            if (
                not isinstance(result, dict)
                or set(result) != expected_keys
                or result.get("status") != "prepared"
                or result.get("api_target_count") != 2
                or result.get("web_target_count") != 1
                or not isinstance(result.get("release_slug"), str)
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", result["release_slug"]) is None
            ):
                return 2
            target_count = 3
            marker = (
                f"CPU_DIAGNOSTIC_PLAN status=prepared targets={target_count} "
                f"release_slug={result['release_slug']}"
            )
        else:
            if (
                not isinstance(result, dict)
                or set(result) != expected_keys
                or result.get("status") != "expired_plans_removed"
                or type(result.get("service_count")) is not int
                or result["service_count"] not in {0, 2}
                or result.get("usage_status") not in {"complete", "incomplete", "unavailable"}
                or result.get("usage_reason") not in {
                    "none", "missing", "duplicate", "identity_changed", "journal_failed",
                    "byte_cap", "line_cap", "timeout", "invalid_event", "timing_incomplete",
                }
                or not isinstance(result.get("usage_rows"), list)
                or len(result["usage_rows"]) not in {0, 4}
                or result.get("profile_status") not in {"complete", "incomplete", "unavailable"}
                or result.get("profile_reason") not in {
                    "none", "missing", "duplicate", "identity_changed", "journal_failed",
                    "byte_cap", "line_cap", "timeout", "invalid_profile",
                }
                or not isinstance(result.get("profile_rows"), list)
                or len(result["profile_rows"]) not in {0, 2}
                or (len(result["usage_rows"]) == 0 and not (
                    result["service_count"] == 0
                    and result["usage_status"] == "unavailable"
                    and result["usage_reason"] == "missing"
                ))
                or (len(result["usage_rows"]) == 4 and result["service_count"] != 2)
                or (len(result["profile_rows"]) == 0 and not (
                    result["service_count"] == 0
                    and result["profile_status"] == "unavailable"
                    and result["profile_reason"] == "missing"
                ))
                or (len(result["profile_rows"]) == 2 and result["service_count"] != 2)
            ):
                return 2
            expected_rows = (
                ("api", "off", 2), ("api", "on", 2),
                ("web", "off", 1), ("web", "on", 1),
            )
            projected_rows: list[dict[str, Any]] = []
            for row, (service, phase, expected_targets) in zip(result["usage_rows"], expected_rows):
                if (
                    not isinstance(row, dict)
                    or set(row) != CPU_USAGE_ROW_FIELDS
                    or row.get("service") != service
                    or row.get("phase") != phase
                    or row.get("expected_targets") != expected_targets
                    or type(row.get("observed_targets")) is not int
                    or not 0 <= row["observed_targets"] <= expected_targets
                    or type(row.get("event_count")) is not int
                    or not 0 <= row["event_count"] <= 8
                    or type(row.get("duplicate_count")) is not int
                    or not 0 <= row["duplicate_count"] <= 8
                    or type(row.get("timing_complete")) is not bool
                ):
                    return 2
                for name in (
                    "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min",
                    "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max",
                ):
                    value = row.get(name)
                    if value is not None and (type(value) is not int or abs(value) > 10**13):
                        return 2
                if row["cpu_ns"] is not None and row["cpu_ns"] < 0:
                    return 2
                projected_rows.append({key: row[key] for key in (
                    "service", "phase", "expected_targets", "observed_targets", "event_count",
                    "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min",
                    "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max", "duplicate_count",
                    "timing_complete",
                )})
            if result["usage_status"] == "complete" and (
                result["usage_reason"] != "none"
                or len(projected_rows) != 4
                or any(
                    row["observed_targets"] != row["expected_targets"]
                    or row["event_count"] != row["expected_targets"]
                    or row["duplicate_count"] != 0
                    or row["cpu_ns"] is None
                    or row["timing_complete"] is not True
                    or type(row["window_ms_min"]) is not int
                    or type(row["window_ms_max"]) is not int
                    or not 19_750 <= row["window_ms_min"] <= row["window_ms_max"] <= 20_500
                    or type(row["start_lag_ms_min"]) is not int
                    or type(row["start_lag_ms_max"]) is not int
                    or not 0 <= row["start_lag_ms_min"] <= row["start_lag_ms_max"] <= 250
                    or type(row["end_lag_ms_min"]) is not int
                    or type(row["end_lag_ms_max"]) is not int
                    or not -250 <= row["end_lag_ms_min"] <= row["end_lag_ms_max"] <= 250
                    for row in projected_rows
                )
            ):
                return 2
            expected_profiles = (("api", 2, "thread_cpu", "calls"), ("web", 1, "v8_cpu", "samples"))
            projected_profiles: list[dict[str, Any]] = []
            for row, (service, expected_targets, timer, observation_unit) in zip(
                result["profile_rows"], expected_profiles
            ):
                if (
                    not isinstance(row, dict)
                    or set(row) != CPU_PROFILE_ROW_FIELDS
                    or row.get("service") != service
                    or row.get("expected_targets") != expected_targets
                    or type(row.get("observed_targets")) is not int
                    or not 0 <= row["observed_targets"] <= expected_targets
                    or type(row.get("event_count")) is not int
                    or not 0 <= row["event_count"] <= 8
                    or row.get("timer") != timer
                    or row.get("observation_unit") != observation_unit
                    or not isinstance(row.get("categories"), list)
                    or len(row["categories"]) > 16
                ):
                    return 2
                for numeric in (
                    "total_cpu_us", "sample_count", "start_lag_ms_min", "start_lag_ms_max",
                    "elapsed_ms_min", "elapsed_ms_max", "end_lag_ms_min", "end_lag_ms_max",
                ):
                    value = row.get(numeric)
                    if value is not None and (type(value) is not int or not 0 <= value <= 10**10):
                        return 2
                categories: list[dict[str, Any]] = []
                category_names: list[str] = []
                for category in row["categories"]:
                    if (
                        not isinstance(category, dict)
                        or set(category) != CPU_PROFILE_CATEGORY_FIELDS
                        or category.get("category") not in CPU_PROFILE_CATEGORIES
                        or type(category.get("cpu_us")) is not int
                        or not 0 <= category["cpu_us"] <= 10**10
                        or type(category.get("observations")) is not int
                        or not 0 <= category["observations"] <= 10**10
                    ):
                        return 2
                    category_names.append(category["category"])
                    categories.append({
                        "category": category["category"],
                        "cpu_us": category["cpu_us"],
                        "observations": category["observations"],
                    })
                if category_names != sorted(category_names) or len(set(category_names)) != len(category_names):
                    return 2
                projected_profiles.append({
                    "service": service,
                    "expected_targets": expected_targets,
                    "observed_targets": row["observed_targets"],
                    "event_count": row["event_count"],
                    "timer": timer,
                    "observation_unit": observation_unit,
                    "total_cpu_us": row["total_cpu_us"],
                    "sample_count": row["sample_count"],
                    "start_lag_ms_min": row["start_lag_ms_min"],
                    "start_lag_ms_max": row["start_lag_ms_max"],
                    "elapsed_ms_min": row["elapsed_ms_min"],
                    "elapsed_ms_max": row["elapsed_ms_max"],
                    "end_lag_ms_min": row["end_lag_ms_min"],
                    "end_lag_ms_max": row["end_lag_ms_max"],
                    "categories": categories,
                })
            if result["profile_status"] == "complete" and (
                result["profile_reason"] != "none"
                or len(projected_profiles) != 2
                or any(
                    row["observed_targets"] != row["expected_targets"]
                    or row["event_count"] != row["expected_targets"]
                    or not isinstance(row["total_cpu_us"], int)
                    or row["total_cpu_us"] <= 0
                    or type(row["start_lag_ms_min"]) is not int
                    or type(row["start_lag_ms_max"]) is not int
                    or not 0 <= row["start_lag_ms_min"] <= row["start_lag_ms_max"] <= 250
                    or type(row["elapsed_ms_min"]) is not int
                    or type(row["elapsed_ms_max"]) is not int
                    or not 19_750 <= row["elapsed_ms_min"] <= row["elapsed_ms_max"] <= 20_500
                    or type(row["end_lag_ms_min"]) is not int
                    or type(row["end_lag_ms_max"]) is not int
                    or not -250 <= row["end_lag_ms_min"] <= row["end_lag_ms_max"] <= 250
                    or (row["service"] == "api" and row["sample_count"] is not None)
                    or (row["service"] == "web" and (
                        not isinstance(row["sample_count"], int) or row["sample_count"] <= 0
                    ))
                    or not row["categories"]
                    for row in projected_profiles
                )
            ):
                return 2
            target_count = result["service_count"]
            cleanup_projection = {
                "status": "expired_plans_removed",
                "service_count": target_count,
                "usage_status": result["usage_status"],
                "usage_reason": result["usage_reason"],
                "usage_rows": projected_rows,
                "profile_status": result["profile_status"],
                "profile_reason": result["profile_reason"],
                "profile_rows": projected_profiles,
            }
            marker = "CPU_DIAGNOSTIC_PLAN " + json.dumps(
                cleanup_projection, ensure_ascii=True, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            )
            if len(marker.encode("ascii")) > CPU_DIAGNOSTIC_OUTPUT_CAP - 1:
                return 2
        print(marker)
        return 0
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        _terminate_process_group(process)
        return 2
    finally:
        try:
            process.stdout.close()
        except OSError:
            pass


def _run_live_user_qa_sudo(
    arguments: list[str], *, expected_sha: str, expected_app_sha: str
) -> int:
    """Capture only a fixed QA diagnostic from the trusted browser wrapper."""

    if not _trusted_helper(LIVE_USER_QA_HELPER):
        return 2
    try:
        process = subprocess.Popen(  # nosec B603
            [SUDO, "-n", "--", str(LIVE_USER_QA_HELPER), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return 2
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    selector: selectors.BaseSelector | None = None
    buffers: dict[int, bytearray] = {}
    line_too_long: set[int] = set()
    total = {"stdout": 0, "stderr": 0}
    diagnostic: bytes | None = None
    diagnostic_count = 0
    browser_counts: bytes | None = None
    browser_counts_payload: dict[str, object] | None = None
    browser_counts_count = 0
    deadline = time.monotonic() + LIVE_USER_QA_OPERATION_TIMEOUT_SECONDS
    try:
        selector = selectors.DefaultSelector()
        for name, stream in streams.items():
            if stream is None:
                raise OSError("QA child pipe is unavailable")
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, name)
            buffers[descriptor] = bytearray()
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                return 124
            for key, _ in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                channel = key.data
                total[channel] += len(chunk)
                if channel != "stdout":
                    continue
                line_buffer = buffers[key.fd]
                for byte in chunk:
                    if byte == 10:
                        if key.fd not in line_too_long:
                            line = bytes(line_buffer) + b"\n"
                            if line.startswith(b"LIVE_QA_CHILD_DIAGNOSTIC "):
                                diagnostic_count += 1
                                if len(line) <= LIVE_QA_DIAGNOSTIC_LINE_MAX_BYTES:
                                    match = LIVE_QA_DIAGNOSTIC_RE.fullmatch(line)
                                    diagnostic = line if match is not None else None
                                else:
                                    diagnostic = None
                            elif line.startswith(b"LIVE_BROWSER_COUNTS "):
                                browser_counts_count += 1
                                parsed_counts = (
                                    _parse_live_browser_counts(
                                        line,
                                        expected_sha=expected_sha,
                                        expected_app_sha=expected_app_sha,
                                        expected_marker_sha256=None,
                                    )
                                    if len(line) <= LIVE_USER_QA_MARKER_MAX_BYTES
                                    else None
                                )
                                browser_counts = line if parsed_counts is not None else None
                                browser_counts_payload = parsed_counts
                        line_buffer.clear()
                        line_too_long.discard(key.fd)
                    elif key.fd not in line_too_long:
                        if len(line_buffer) < LIVE_USER_QA_MARKER_MAX_BYTES:
                            line_buffer.append(byte)
                        else:
                            line_buffer.clear()
                            line_too_long.add(key.fd)
        child_status = process.returncode if process.returncode is not None else 2
        if diagnostic_count == 1 and diagnostic is not None:
            match = LIVE_QA_DIAGNOSTIC_RE.fullmatch(diagnostic)
            if match is not None and int(match.group("child_exit")) == child_status:
                sys.stdout.buffer.write(diagnostic)
                sys.stdout.buffer.flush()
        counts_valid = browser_counts_count == 1 and browser_counts is not None
        if counts_valid:
            sys.stdout.buffer.write(browser_counts)
            sys.stdout.buffer.flush()
        if child_status == 0 and not counts_valid:
            return 2
        counts_passed = bool(
            browser_counts_payload is not None
            and browser_counts_payload["run_status"] == "passed"
            and browser_counts_payload["logical_total"] == 1
            and browser_counts_payload["logical_pass"] == 1
            and browser_counts_payload["logical_total"] == sum(
                int(browser_counts_payload[field])
                for field in (
                    "logical_pass", "logical_fail", "logical_expected_fail",
                    "logical_flaky", "logical_skip", "logical_interrupted",
                )
            )
            and browser_counts_payload["attempt_total"] == 1
            and browser_counts_payload["attempt_pass"] == 1
            and browser_counts_payload["attempt_total"] == sum(
                int(browser_counts_payload[field])
                for field in (
                    "attempt_pass", "attempt_fail", "attempt_skip",
                    "attempt_interrupted", "attempt_timedout",
                )
            )
            and all(
                browser_counts_payload[field] == 0
                for field in (
                    "logical_fail", "logical_expected_fail", "logical_flaky",
                    "logical_skip", "logical_interrupted", "attempt_fail", "attempt_interrupted",
                    "attempt_timedout",
                )
            )
        )
        if child_status == 0 and counts_passed:
            print("LIVE_USER_QA_SUCCESS")
            return 0
        if child_status == 0:
            # The browser process itself succeeded, but the bound report may
            # describe failed/skipped tests.  Leave the actual child status
            # intact and omit the success marker; the workflow validator
            # rejects the sanitized report while preserving its counts.
            return 0
        return child_status if 0 <= child_status <= 255 else 2
    except (OSError, ValueError):
        _terminate_process_group(process)
        return 2
    finally:
        if selector is not None:
            selector.close()
        for stream in streams.values():
            if stream is not None:
                stream.close()


def _control_email_stdin(control_email: str) -> bytes:
    """Serialize the only identity field passed to fixed cleanup helpers."""

    if not isinstance(control_email, str) or len(control_email) > 254:
        raise ValueError("control identity is invalid")
    return (
        json.dumps(
            {"schema": 1, "control_email": control_email},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _run_retained_cleanup_sudo(
    helper: Path,
    arguments: list[str],
    *,
    control_email: str,
    diagnostic_binding: dict[str, str] | None = None,
) -> int:
    """Run exact cleanup while retaining only its fixed stage marker."""

    def emit_diagnostic(
        *,
        stage: str,
        dispatcher_exit: int,
        child_state: str,
        child_exit: int | str,
        stdout_eof: str,
        timed_out: bool,
    ) -> None:
        if diagnostic_binding is None:
            print(
                "RETAINED_CLEANUP_DIAGNOSTIC schema=1 "
                f"stage={stage} child_exit={dispatcher_exit}"
            )
            return
        binding_keys = {
            "source_sha", "app_sha", "run_id", "load_run_id", "run_attempt", "profile"
        }
        if (
            set(diagnostic_binding) != binding_keys
            or SOURCE_SHA_RE.fullmatch(diagnostic_binding.get("source_sha", "")) is None
            or SOURCE_SHA_RE.fullmatch(diagnostic_binding.get("app_sha", "")) is None
            or RUN_ID_RE.fullmatch(diagnostic_binding.get("run_id", "")) is None
            or RUN_ID_RE.fullmatch(diagnostic_binding.get("load_run_id", "")) is None
            or RUN_ID_RE.fullmatch(diagnostic_binding.get("run_attempt", "")) is None
            or EXTERNAL_PROFILE_ID_RE.fullmatch(
                diagnostic_binding.get("profile", "")
            ) is None
            or stage not in RETAINED_CLEANUP_DIAGNOSTIC_STAGES
            or child_state not in {"not_started", "running", "exited", "unknown"}
            or stdout_eof not in {"true", "false", "unknown"}
            or type(timed_out) is not bool
            or type(dispatcher_exit) is not int
            or not 0 <= dispatcher_exit <= 255
            or (
                child_exit != "unknown"
                and (type(child_exit) is not int or not 0 <= child_exit <= 255)
            )
        ):
            print("RETAINED_CLEANUP_DIAGNOSTIC schema=1 stage=dispatcher child_exit=2")
            return
        print(
            "RETAINED_CLEANUP_DIAGNOSTIC schema=2 "
            f"stage={stage} dispatcher_exit={dispatcher_exit} "
            f"child_state={child_state} child_exit={child_exit} "
            f"stdout_eof={stdout_eof} timed_out={'true' if timed_out else 'false'} "
            f"source_sha={diagnostic_binding['source_sha']} "
            f"app_sha={diagnostic_binding['app_sha']} "
            f"run_id={diagnostic_binding['run_id']} "
            f"run_attempt={diagnostic_binding['run_attempt']} "
            f"load_run_id={diagnostic_binding['load_run_id']} "
            f"profile={diagnostic_binding['profile']}"
        )

    def return_code(value: int) -> int:
        return min(255, 128 + abs(value)) if value < 0 else min(255, value)

    if not _trusted_helper(helper):
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="not_started",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    try:
        input_bytes = _control_email_stdin(control_email)
    except (TypeError, ValueError, UnicodeEncodeError):
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="not_started",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    command = [SUDO, "-n", "--", str(helper), *arguments]
    try:
        process = subprocess.Popen(  # nosec B603 - fixed helper and validated inputs.
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="not_started",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    stream = process.stdout
    input_stream = process.stdin
    if stream is None or input_stream is None:
        _terminate_process_group(process)
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="unknown",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    try:
        input_stream.write(input_bytes)
        input_stream.flush()
    except BrokenPipeError:
        # Preserve the child marker/status if it rejected input before reading.
        pass
    except OSError:
        _terminate_process_group(process)
        input_stream.close()
        stream.close()
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="unknown",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    finally:
        try:
            input_stream.close()
        except OSError:
            pass
    try:
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
    except (OSError, ValueError):
        _terminate_process_group(process)
        stream.close()
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="unknown",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2

    selector: selectors.BaseSelector | None = None
    pending = bytearray()
    oversized_line = False
    marker: tuple[str, int] | None = None
    marker_invalid = False
    try:
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + CLEANUP_OPERATION_TIMEOUT_SECONDS
        eof = False
        while process.poll() is None or not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                deadline_returncode = process.poll()
                deadline_child_state = (
                    "exited" if deadline_returncode is not None else "running"
                )
                _terminate_process_group(process)
                emit_diagnostic(
                    stage="timeout",
                    dispatcher_exit=124,
                    child_state=deadline_child_state,
                    child_exit=(
                        return_code(deadline_returncode)
                        if deadline_returncode is not None
                        else "unknown"
                    ),
                    stdout_eof="true" if eof else "false",
                    timed_out=True,
                )
                return 124
            for key, _ in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    eof = True
                    continue
                for byte in chunk:
                    if byte == 10:
                        if not oversized_line:
                            match = RETAINED_CLEANUP_MARKER_RE.fullmatch(pending)
                            if match is not None:
                                stage = match.group("stage").decode("ascii")
                                code = int(match.group("exit_code"))
                                candidate = (stage, code)
                                if code > 255 or marker is not None:
                                    marker_invalid = True
                                else:
                                    marker = candidate
                        pending.clear()
                        oversized_line = False
                    elif not oversized_line:
                        if len(pending) >= RETAINED_CLEANUP_MARKER_MAX_BYTES:
                            pending.clear()
                            oversized_line = True
                        else:
                            pending.append(byte)
        raw_child_status = process.returncode
        child_status = 2 if raw_child_status is None else return_code(raw_child_status)
        if (
            marker is None
            or marker_invalid
            or marker[1] != child_status
            or child_status > 255
        ):
            emit_diagnostic(
                stage="unknown",
                dispatcher_exit=child_status if child_status != 0 else 2,
                child_state="exited" if raw_child_status is not None else "unknown",
                child_exit=child_status if raw_child_status is not None else "unknown",
                stdout_eof="true" if eof else "false",
                timed_out=False,
            )
            return child_status if child_status != 0 else 2
        stage, _ = marker
        emit_diagnostic(
            stage=stage,
            dispatcher_exit=child_status,
            child_state="exited",
            child_exit=child_status,
            stdout_eof="true",
            timed_out=False,
        )
        return child_status
    except (OSError, ValueError):
        _terminate_process_group(process)
        emit_diagnostic(
            stage="dispatcher",
            dispatcher_exit=2,
            child_state="unknown",
            child_exit="unknown",
            stdout_eof="unknown",
            timed_out=False,
        )
        return 2
    finally:
        if selector is not None:
            selector.close()
        stream.close()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a timed-out dispatcher child and every process it spawned."""

    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    deadline = time.monotonic() + CHILD_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()  # Reap the leader when it exits; descendants may remain.
        if not group_exists():
            return
        time.sleep(0.05)

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass

    deadline = time.monotonic() + CHILD_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not group_exists():
            return
        time.sleep(0.05)
    # The caller remains on a failure path if a hostile process group cannot
    # be proven gone within the bounded termination window.
    try:
        process.wait(timeout=0)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _run_bounded_child(
    command: list[str],
    *,
    timeout_seconds: float,
    expected_release_marker: tuple[str, str, str] | None = None,
    expected_live_launch_sha: str | None = None,
    expected_live_app_sha: str | None = None,
    expected_live_marker_sha256: str | None = None,
) -> int:
    """Run one synchronous privileged child with process-group cleanup."""

    try:
        process = subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=(
                subprocess.PIPE
                if expected_release_marker is not None
                or expected_live_launch_sha is not None
                else subprocess.DEVNULL
            ),
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        if expected_live_launch_sha is not None:
            _emit_live_launch_status(
                status="failed",
                stage="dispatch",
                child_exit=2,
                source_sha=expected_live_launch_sha,
            )
        return 2
    if expected_release_marker is not None:
        return _wait_for_release_marker(
            process,
            timeout_seconds=timeout_seconds,
            expected=expected_release_marker,
        )
    if expected_live_launch_sha is not None:
        return _wait_for_live_launch_status(
            process,
            timeout_seconds=timeout_seconds,
            expected_sha=expected_live_launch_sha,
            expected_app_sha=expected_live_app_sha,
            expected_marker_sha256=expected_live_marker_sha256,
        )
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return 124


def _release_marker_is_valid(
    output: bytes,
    *,
    child_status: int,
    expected: tuple[str, str, str],
) -> bool:
    """Validate the one public deploy marker against its exact handoff."""

    if len(output) > RELEASE_MARKER_MAX_BYTES:
        return False
    match = RELEASE_MARKER_RE.fullmatch(output)
    if match is None:
        return False
    mode, release_slug, source_sha = expected
    if (
        match.group("release_slug").decode("ascii") != release_slug
        or match.group("source_sha").decode("ascii") != source_sha
    ):
        return False

    status = match.group("status").decode("ascii")
    marker_class = match.group("class").decode("ascii")
    phase = match.group("phase")
    reason = match.group("reason")
    lock_stage = match.group("lock_stage")
    if status == "passed":
        expected_class = "preflight" if mode == "preflight" else "deployment"
        return (
            marker_class == expected_class
            and phase is None
            and reason is None
            and lock_stage is None
            and child_status == 0
        )
    if child_status == 0 or (mode == "preflight" and marker_class != "preflight"):
        return False
    if phase is None or reason is None:
        return False
    if lock_stage is not None and (
        marker_class != "preflight"
        or phase.decode("ascii") != "preflight"
        or reason.decode("ascii") != "lock"
    ):
        return False
    return reason.decode("ascii") in RELEASE_FAILURE_REASONS.get(
        (marker_class, phase.decode("ascii")), frozenset()
    )


def _wait_for_release_marker(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    expected: tuple[str, str, str],
) -> int:
    """Drain a bounded supervisor marker while discarding all other bytes."""

    stdout = process.stdout
    if stdout is None:
        _terminate_process_group(process)
        return 2
    output = bytearray()
    oversized = False
    observed_bytes = 0
    eof = False
    try:
        descriptor = stdout.fileno()
        os.set_blocking(descriptor, False)
    except (OSError, ValueError):
        _terminate_process_group(process)
        return 2

    selector: selectors.BaseSelector | None = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            child_status = process.poll()
            if child_status is not None and eof:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                return 124
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    raise
                if not chunk:
                    try:
                        selector.unregister(key.fd)
                    except (KeyError, ValueError):
                        pass
                    eof = True
                    continue
                observed_bytes = min(
                    RELEASE_MARKER_OBSERVED_BYTES_MAX,
                    observed_bytes + len(chunk),
                )
                if not oversized:
                    if len(output) + len(chunk) > RELEASE_MARKER_MAX_BYTES:
                        output.clear()
                        oversized = True
                    else:
                        output.extend(chunk)
        child_status = process.returncode
        if child_status is None:
            return 2
        if oversized or not _release_marker_is_valid(
            bytes(output),
            child_status=child_status,
            expected=expected,
        ):
            reason = (
                "oversized_marker"
                if oversized
                else "missing_marker"
                if observed_bytes == 0
                else "invalid_marker"
            )
            dispatcher_status = (
                child_status
                if child_status > 0
                else 256 + child_status
                if child_status < 0
                else 2
            )
            # Keep the failure boundary observable without exposing child
            # output.  513 means 513 bytes or more; the collector stops
            # retaining data once the public marker bound is exceeded.
            print(
                "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                f"reason={reason} child_exit={child_status} "
                f"observed_bytes={observed_bytes} "
                f"dispatcher_exit={dispatcher_status}"
            )
            return child_status if child_status != 0 else 2
        sys.stdout.write(output.decode("ascii"))
        sys.stdout.flush()
        return child_status
    except (OSError, ValueError):
        _terminate_process_group(process)
        return 2
    finally:
        if selector is not None:
            selector.close()
        stdout.close()


def _emit_live_launch_status(
    *, status: str, stage: str, child_exit: int, source_sha: str, check: str = "none"
) -> None:
    """Write one closed live-launch result without forwarding child output."""

    if (
        status not in {"passed", "failed"}
        or stage not in LIVE_LAUNCH_FAILURE_STAGES | {"complete"}
        or check not in LIVE_LAUNCH_CHECK_IDS
        or not 0 <= child_exit <= 255
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
    ):
        return
    print(
        "LIVE_LAUNCH_STATUS schema=2 "
        f"status={status} stage={stage} check={check} child_exit={child_exit} "
        f"source_sha={source_sha}"
    )


def _parse_live_launch_status(
    output: bytes, *, child_status: int, expected_sha: str
) -> tuple[str, str, str, int] | None:
    """Accept only the exact status marker emitted by the trusted supervisor."""

    if len(output) > LIVE_LAUNCH_STATUS_MAX_BYTES:
        return None
    match = LIVE_LAUNCH_STATUS_RE.fullmatch(output)
    if match is None or match.group("source_sha").decode("ascii") != expected_sha:
        return None
    status = match.group("status").decode("ascii")
    stage = match.group("stage").decode("ascii")
    check = match.group("check").decode("ascii")
    child_exit = int(match.group("child_exit"))
    if child_status < 0:
        return None
    if status == "passed":
        if stage != "complete" or child_exit != 0 or child_status != 0:
            return None
    elif (
        stage
        not in (
            LIVE_LAUNCH_SUPERVISOR_FAILURE_STAGES
            | LIVE_LAUNCH_PRE_SUPERVISOR_FAILURE_STAGES
        )
        or child_exit == 0
        or child_status != child_exit
    ):
        return None
    return status, stage, check, child_exit


def _parse_live_browser_counts(
    line: bytes,
    *,
    expected_sha: str,
    expected_app_sha: str | None,
    expected_marker_sha256: str | None,
) -> dict[str, object] | None:
    if len(line) > LIVE_BROWSER_COUNTS_MAX_BYTES:
        return None
    match = LIVE_BROWSER_COUNTS_RE.fullmatch(line)
    if match is None:
        return None
    marker_sha256 = match.group("marker_sha256").decode("ascii")
    if (
        match.group("source_sha").decode("ascii") != expected_sha
        or expected_app_sha is None
        or match.group("app_sha").decode("ascii") != expected_app_sha
        or (
            expected_marker_sha256 is not None
            and marker_sha256 != expected_marker_sha256
        )
    ):
        return None
    counts: dict[str, object] = {
        "run_status": match.group("run_status").decode("ascii"),
        "source_sha": expected_sha,
        "app_sha": expected_app_sha,
        "marker_sha256": marker_sha256,
    }
    counts.update(
        {
            field: int(match.group(field))
            for field in LIVE_BROWSER_COUNT_FIELDS
        }
    )
    if (
        any(counts[field] > 32768 for field in LIVE_BROWSER_COUNT_FIELDS)
        or
        counts["logical_total"] > 4096
        or sum(
            counts[field]
            for field in (
                "logical_pass",
                "logical_fail",
                "logical_expected_fail",
                "logical_flaky",
                "logical_skip",
                "logical_interrupted",
            )
        )
        != counts["logical_total"]
        or sum(
            counts[field]
            for field in (
                "attempt_pass",
                "attempt_fail",
                "attempt_skip",
                "attempt_interrupted",
                "attempt_timedout",
            )
        )
        != counts["attempt_total"]
    ):
        return None
    return counts


def _parse_live_launch_protocol(
    output: bytes,
    *,
    child_status: int,
    expected_sha: str,
    expected_app_sha: str | None = None,
    expected_marker_sha256: str | None = None,
) -> tuple[tuple[str, str, str, int], dict[str, object] | None] | None:
    if len(output) > LIVE_LAUNCH_PROTOCOL_MAX_BYTES:
        return None
    lines = output.splitlines(keepends=True)
    if len(lines) == 1:
        status_line = lines[0]
        counts = None
    elif len(lines) == 2:
        if not lines[0].startswith(b"LIVE_BROWSER_COUNTS schema=1 "):
            return None
        counts = _parse_live_browser_counts(
            lines[0],
            expected_sha=expected_sha,
            expected_app_sha=expected_app_sha,
            expected_marker_sha256=expected_marker_sha256,
        )
        status_line = lines[1]
    else:
        return None
    status = _parse_live_launch_status(
        status_line,
        child_status=child_status,
        expected_sha=expected_sha,
    )
    if status is None:
        return None
    return status, counts


def _extract_live_qa_diagnostic(output: bytes) -> tuple[bytes, bytes | None] | None:
    """Remove one exact diagnostic marker while keeping the status protocol strict."""

    lines = output.splitlines(keepends=True)
    markers = [line for line in lines if line.startswith(b"LIVE_QA_CHILD_DIAGNOSTIC ")]
    if not markers:
        return output, None
    if len(markers) != 1 or LIVE_QA_DIAGNOSTIC_RE.fullmatch(markers[0]) is None:
        return None
    remainder = b"".join(line for line in lines if line is not markers[0])
    return remainder, markers[0]


def _emit_live_browser_counts(counts: dict[str, object]) -> None:
    fields = [
        f"run_status={counts['run_status']}",
        *(f"{field}={counts[field]}" for field in LIVE_BROWSER_COUNT_FIELDS),
        f"source_sha={counts['source_sha']}",
        f"app_sha={counts['app_sha']}",
        f"marker_sha256={counts['marker_sha256']}",
    ]
    print("LIVE_BROWSER_COUNTS schema=1 " + " ".join(fields))


def _wait_for_live_launch_status(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    expected_sha: str,
    expected_app_sha: str | None = None,
    expected_marker_sha256: str | None = None,
) -> int:
    """Drain output while retaining only one status and an optional count line."""

    stdout = process.stdout
    if stdout is None:
        _terminate_process_group(process)
        _emit_live_launch_status(
            status="failed", stage="dispatch", child_exit=2,
            source_sha=expected_sha, check="protocol",
        )
        return 2
    output = bytearray()
    oversized = False
    drained_bytes = 0
    eof = False
    try:
        descriptor = stdout.fileno()
        os.set_blocking(descriptor, False)
    except (OSError, ValueError):
        _terminate_process_group(process)
        _emit_live_launch_status(
            status="failed", stage="dispatch", child_exit=2,
            source_sha=expected_sha, check="dispatch",
        )
        stdout.close()
        return 2

    selector: selectors.BaseSelector | None = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            child_status = process.poll()
            if child_status is not None and eof:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                _emit_live_launch_status(
                    status="failed",
                    stage="timeout",
                    child_exit=124,
                    source_sha=expected_sha,
                    check="timeout",
                )
                return 124
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    raise
                if not chunk:
                    try:
                        selector.unregister(key.fd)
                    except (KeyError, ValueError):
                        pass
                    eof = True
                    continue
                drained_bytes += len(chunk)
                if drained_bytes > LIVE_LAUNCH_STREAM_MAX_BYTES:
                    oversized = True
                    _terminate_process_group(process)
                    _emit_live_launch_status(
                        status="failed",
                        stage="trusted_entry",
                        child_exit=2,
                        source_sha=expected_sha,
                        check="stream_limit",
                    )
                    return 2
                if not oversized:
                    if len(output) + len(chunk) > LIVE_LAUNCH_PROTOCOL_MAX_BYTES:
                        output.clear()
                        oversized = True
                    else:
                        output.extend(chunk)
        child_status = process.returncode
        if child_status is None:
            _emit_live_launch_status(
                status="failed", stage="dispatch", child_exit=2,
                source_sha=expected_sha, check="dispatch",
            )
            return 2
        extracted = None if oversized else _extract_live_qa_diagnostic(bytes(output))
        parsed = None
        child_diagnostic = None
        if extracted is not None:
            protocol_output, child_diagnostic = extracted
            parsed = _parse_live_launch_protocol(
                protocol_output,
                child_status=child_status,
                expected_sha=expected_sha,
                expected_app_sha=expected_app_sha,
                expected_marker_sha256=expected_marker_sha256,
            )
        if parsed is None:
            safe_exit = child_status if 0 < child_status <= 255 else 2
            _emit_live_launch_status(
                status="failed",
                stage="trusted_entry",
                child_exit=safe_exit,
                source_sha=expected_sha,
                check="protocol",
            )
            return safe_exit
        (status, stage, check, child_exit), counts = parsed
        if child_diagnostic is not None:
            sys.stdout.write(child_diagnostic.decode("ascii"))
            sys.stdout.flush()
        if counts is not None:
            _emit_live_browser_counts(counts)
        _emit_live_launch_status(
            status=status,
            stage=stage,
            check=check,
            child_exit=child_exit,
            source_sha=expected_sha,
        )
        return child_status
    except (OSError, ValueError):
        _terminate_process_group(process)
        _emit_live_launch_status(
            status="failed", stage="dispatch", child_exit=2,
            source_sha=expected_sha, check="dispatch",
        )
        return 2
    finally:
        if selector is not None:
            selector.close()
        stdout.close()


def _run_trusted_live_launch(
    arguments: list[str], *, expected_app_sha: str, expected_marker_sha256: str
) -> int:
    if not _trusted_live_launch_helper():
        _emit_live_launch_status(
            status="failed", stage="dispatch", child_exit=2,
            source_sha=arguments[-1], check="trusted_entry",
        )
        return 2
    command = [SUDO, "-n", "--", str(TRUSTED_LIVE_LAUNCH), *arguments]
    # The trusted helper performs the synchronous handoff to its supervisor.
    # Its only output allowed across this boundary is the bounded, source-bound
    # live-launch status marker; all ordinary stdout/stderr stays discarded.
    return _run_bounded_child(
        command,
        timeout_seconds=LIVE_LAUNCH_OPERATION_TIMEOUT_SECONDS,
        expected_live_launch_sha=arguments[-1],
        expected_live_app_sha=expected_app_sha,
        expected_live_marker_sha256=expected_marker_sha256,
    )


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


def _stable_host_file(path: Path, *, mode: int, maximum: int = 512 * 1024) -> bytes | None:
    """Read one immutable generation member through a stable descriptor."""

    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size > maximum
        ):
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != mode
        ):
            return None
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        final = os.fstat(descriptor)
        if (
            len(data) != final.st_size
            or final.st_dev != opened.st_dev
            or final.st_ino != opened.st_ino
            or final.st_uid != opened.st_uid
            or final.st_gid != opened.st_gid
            or final.st_nlink != opened.st_nlink
            or final.st_mode != opened.st_mode
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            return None
        return bytes(data)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _verify_host_tools_contract(payload: dict[str, object]) -> bool:
    """Rebind the dispatcher to the exact handoff and immutable generation."""

    handoff = payload.get("host_tools")
    if handoff is None:
        # Preflight mode remains a read-only compatibility caller, but every
        # deploy-side privileged entrypoint must carry the closed handoff.
        return False
    if not isinstance(handoff, dict):
        return False
    generation_sha = handoff.get("host_tools_sha")
    manifest_sha = handoff.get("manifest_sha256")
    capabilities_sha = handoff.get("capabilities_sha256")
    if (
        not isinstance(generation_sha, str)
        or HOST_GENERATION_RE.fullmatch(generation_sha) is None
        or not isinstance(manifest_sha, str)
        or not isinstance(capabilities_sha, str)
        or HEX_DIGEST_RE.fullmatch(manifest_sha) is None
        or HEX_DIGEST_RE.fullmatch(capabilities_sha) is None
        or ACTIVE_TOOLS_DIR != HOST_TOOLS_ROOT / generation_sha
    ):
        return False
    try:
        if {entry.name for entry in ACTIVE_TOOLS_DIR.iterdir()} != HOST_TOOLS_INVENTORY:
            return False
    except OSError:
        return False
    members: dict[str, bytes] = {}
    for name in HOST_TOOL_FILES:
        data = _stable_host_file(ACTIVE_TOOLS_DIR / name, mode=0o555)
        if data is None:
            return False
        members[name] = data
    manifest_bytes = _stable_host_file(ACTIVE_TOOLS_DIR / "manifest.json", mode=0o444)
    capabilities_bytes = _stable_host_file(ACTIVE_TOOLS_DIR / "capabilities.txt", mode=0o444)
    if manifest_bytes is None or capabilities_bytes is None:
        return False
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha:
        return False
    if hashlib.sha256(capabilities_bytes).hexdigest() != capabilities_sha:
        return False
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or manifest.get("source_sha") != generation_sha
        or manifest.get("generation") != generation_sha
        or not isinstance(records, list)
        or len(records) != len(HOST_TOOL_FILES) + 1
    ):
        return False
    seen: set[str] = set()
    expected = set(HOST_TOOL_FILES) | {"capabilities.txt"}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            return False
        name = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(name, str)
            or name not in expected
            or name in seen
            or not isinstance(digest, str)
            or HEX_DIGEST_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode not in {0o444, 0o555}
        ):
            return False
        seen.add(name)
        data = capabilities_bytes if name == "capabilities.txt" else members.get(name)
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            return False
    return seen == expected


def _trusted_host_helper(path: Path) -> bool:
    if not _trusted_helper(path):
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_IMODE(metadata.st_mode) == 0o555


def _trusted_export_executor(path: Path) -> bool:
    """Check the data-mode helper executed only by the fixed Python runtime."""

    if path.parent != ACTIVE_TOOLS_DIR or ACTIVE_TOOLS_DIR.parent != HOST_TOOLS_ROOT:
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
        and stat.S_IMODE(metadata.st_mode) == 0o555
        and metadata.st_size <= 512 * 1024
    )


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

    try:
        inventory = {entry.name for entry in ACTIVE_TOOLS_DIR.iterdir()}
    except OSError:
        return 2
    if (
        not _trusted_generation()
        or inventory != HOST_TOOLS_INVENTORY
        or not all(
            _trusted_host_helper(ACTIVE_TOOLS_DIR / name)
            for name in HOST_TOOL_FILES
        )
        or not _trusted_data(ACTIVE_TOOLS_DIR / "manifest.json")
        or not _trusted_data(ACTIVE_TOOLS_DIR / "capabilities.txt")
        or _retained_load_export_owner() is None
    ):
        return 2
    print(
        "HOST_TOOLS schema=1 "
        f"source_sha={ACTIVE_TOOLS_DIR.name} generation={ACTIVE_TOOLS_DIR.name} "
        "dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 "
        "release_baseline=1 retained_load_export_cleanup=1 "
        "retained_load_source_binding=1 "
        "cpu_diagnostic_plan_control=1 "
        "python_isolated=1 python_bytecode_disabled=1"
    )
    return 0


def _host_baseline_generation_ready() -> bool:
    if (
        not _trusted_generation()
        or not _trusted_data(ACTIVE_TOOLS_DIR / "manifest.json")
        or not _trusted_data(ACTIVE_TOOLS_DIR / "capabilities.txt")
    ):
        return False
    manifest_bytes = _stable_host_file(ACTIVE_TOOLS_DIR / "manifest.json", mode=0o444)
    capability_bytes = _stable_host_file(ACTIVE_TOOLS_DIR / "capabilities.txt", mode=0o444)
    if manifest_bytes is None or capability_bytes is None:
        return False
    return _verify_host_tools_contract(
        {
            "host_tools": {
                "host_tools_sha": ACTIVE_TOOLS_DIR.name,
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "capabilities_sha256": hashlib.sha256(capability_bytes).hexdigest(),
            }
        }
    ) and {
        b"capability=release_baseline",
        b"capability=retained_load_export_cleanup",
        b"capability=retained_load_source_binding",
        b"capability=cpu_diagnostic_plan_control",
    } <= set(capability_bytes.splitlines())


def _source_binding_context(
    payload: dict[str, object],
) -> tuple[str, dict[str, object] | None, list[str]]:
    """Return the verified app target, tuple, and fixed helper argv suffix."""

    runner_sha = payload.get("target_sha")
    if not isinstance(runner_sha, str) or SOURCE_SHA_RE.fullmatch(runner_sha) is None:
        raise ValueError("runner source identity is invalid")
    binding = payload.get("source_binding")
    if binding is None:
        return runner_sha, None, []
    if (
        not isinstance(binding, dict)
        or binding.get("binding_mode") != "verified-noop"
        or binding.get("runner_sha") != runner_sha
        or not isinstance(binding.get("app_target_sha"), str)
        or SOURCE_SHA_RE.fullmatch(binding["app_target_sha"]) is None
        or binding["app_target_sha"] == runner_sha
    ):
        raise ValueError("verified no-op source identity is invalid")
    baseline = binding.get("baseline_identity")
    if (
        not _baseline_identity_matches(baseline, baseline)
        or baseline.get("source_sha") != binding["app_target_sha"]
    ):
        raise ValueError("verified no-op baseline tuple is invalid")
    raw = json.dumps(
        binding,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not raw or len(raw) > SOURCE_BINDING_ARGUMENT_MAX_BYTES:
        raise ValueError("verified no-op binding exceeds its fixed argument bound")
    encoded = base64.b64encode(raw).decode("ascii")
    return binding["app_target_sha"], baseline, ["--source-binding-base64", encoded]


def _source_binding_arguments(payload: dict[str, object]) -> list[str]:
    """Build the fixed helper suffix after revalidating the closed binding."""

    _target_sha, _baseline, arguments = _source_binding_context(payload)
    return arguments


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


def _external_fixture(payload: dict[str, object]) -> int:
    try:
        source_arguments = _source_binding_arguments(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        return 2
    arguments = [
        payload["confirmation"],
        payload["target_sha"],
        payload["setup_concurrency"],
        payload["run_id"],
        payload["profile"],
        payload["tournament_count"],
        payload["users_per_tournament"],
        payload["timeout_diagnostics"],
        *source_arguments,
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
    try:
        control_input = _control_email_stdin(payload["control_email"])
        process = subprocess.Popen(  # nosec B603 - fixed helper and validated inputs.
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdin is None:
            _terminate_process_group(process)
            return 2
        try:
            process.stdin.write(control_input)
            process.stdin.flush()
        except BrokenPipeError:
            _terminate_process_group(process)
            return 2
        except OSError:
            _terminate_process_group(process)
            return 2
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
    except (OSError, TypeError, ValueError, UnicodeEncodeError):
        return 2
    del process
    return 0


def _run_id_path(prefix: str, run_id: str, leaf: str) -> Path:
    # Keep this guard here as well as in the JSON parser because this function
    # owns a filesystem boundary and is directly unit-testable.  Construct the
    # path from constants only; no input is treated as an arbitrary fragment.
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid run id")
    return Path(f"{prefix}{run_id}") / leaf


def _pin_closure_matches_generation(
    pin: dict[str, object], manifest: dict[str, object]
) -> bool:
    """Match source-pin records to the installed generation by exact path.

    The repository pin records Git tree modes (0644/0755). The host-tools
    builder deliberately installs every executable member as immutable 0555,
    so source modes validate against that closed source-mode set while the
    generation manifest validates the installed 0555 mode. Record ordering is
    not a trust boundary; exact unique path sets and per-path digests are.
    """

    expected_paths = {f"platform/tools/{name}" for name in HOST_TOOL_FILES}
    expected_manifest_paths = set(HOST_TOOL_FILES) | {"capabilities.txt"}
    pin_records = pin.get("closure")
    manifest_records = manifest.get("files")
    if (
        not isinstance(pin_records, list)
        or len(pin_records) != len(expected_paths)
        or not isinstance(manifest_records, list)
        or len(manifest_records) != len(expected_manifest_paths)
    ):
        return False

    pin_by_path: dict[str, dict[str, object]] = {}
    for record in pin_records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            return False
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or path not in expected_paths
            or path in pin_by_path
            or not isinstance(digest, str)
            or HEX_DIGEST_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode not in {0o644, 0o755}
        ):
            return False
        pin_by_path[path] = record
    if set(pin_by_path) != expected_paths:
        return False

    manifest_by_path: dict[str, dict[str, object]] = {}
    for record in manifest_records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            return False
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or path not in expected_manifest_paths
            or path in manifest_by_path
            or not isinstance(digest, str)
            or HEX_DIGEST_RE.fullmatch(digest) is None
            or type(mode) is not int
        ):
            return False
        installed_mode = 0o444 if path == "capabilities.txt" else 0o555
        if mode != installed_mode:
            return False
        manifest_by_path[path] = record
    if set(manifest_by_path) != expected_manifest_paths:
        return False

    return all(
        pin_by_path[f"platform/tools/{name}"]["sha256"]
        == manifest_by_path[name]["sha256"]
        for name in HOST_TOOL_FILES
    )


def _current_pin_matches_host_generation(
    *, target_sha: str, expected_baseline_identity: object | None = None
) -> bool:
    """Bind cleanup authority to the active release's exact C6 pin."""

    if SOURCE_SHA_RE.fullmatch(target_sha) is None or not _host_baseline_generation_ready():
        return False
    try:
        baseline = _release_baseline()
        if (
            baseline.get("source_sha") != target_sha
            or (
                expected_baseline_identity is not None
                and not _baseline_identity_matches(expected_baseline_identity, baseline)
            )
        ):
            return False
        release_slug = baseline.get("release_slug")
        if not isinstance(release_slug, str) or RELEASE_SLUG_RE.fullmatch(release_slug) is None:
            return False
        release = RUNTIME_ROOT / "releases" / release_slug
        if release.resolve(strict=True) != release:
            return False
        release_metadata = release.lstat()
        contracts = release / "contracts"
        contracts_metadata = contracts.lstat()
        pin_path = contracts / "host_tools_pin.json"
        pin_before = pin_path.lstat()
        if (
            not stat.S_ISDIR(release_metadata.st_mode)
            or release_metadata.st_uid != 0
            or release_metadata.st_gid != 0
            or stat.S_IMODE(release_metadata.st_mode) & 0o022
            or not stat.S_ISDIR(contracts_metadata.st_mode)
            or contracts_metadata.st_uid != 0
            or contracts_metadata.st_gid != 0
            or stat.S_IMODE(contracts_metadata.st_mode) & 0o022
            or not stat.S_ISREG(pin_before.st_mode)
            or pin_before.st_uid != 0
            or pin_before.st_gid != 0
            or pin_before.st_nlink != 1
            or stat.S_IMODE(pin_before.st_mode) != 0o644
            or pin_before.st_size > 16 * 1024
        ):
            return False
        descriptor = os.open(pin_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (pin_before.st_dev, pin_before.st_ino, pin_before.st_size, pin_before.st_mtime_ns, pin_before.st_ctime_ns)
            ):
                return False
            chunks: list[bytes] = []
            remaining = 16 * 1024 + 1
            while remaining:
                chunk = os.read(descriptor, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(raw) > 16 * 1024
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                or pin_path.lstat().st_ino != opened.st_ino
            ):
                return False
        finally:
            os.close(descriptor)
        pin = _strict_baseline_json(raw)
        if (
            not isinstance(pin, dict)
            or set(pin) != {"schema", "repository", "host_tools_sha", "closure"}
            or type(pin.get("schema")) is not int
            or pin.get("schema") != 1
            or pin.get("repository") != "StrayForest/old_sparky"
            or pin.get("host_tools_sha") != ACTIVE_TOOLS_DIR.name
            or not isinstance(pin.get("closure"), list)
        ):
            return False
        manifest_raw = _stable_host_file(ACTIVE_TOOLS_DIR / "manifest.json", mode=0o444)
        if manifest_raw is None:
            return False
        manifest = _strict_baseline_json(manifest_raw)
        if not isinstance(manifest, dict) or manifest.get("source_sha") != ACTIVE_TOOLS_DIR.name:
            return False
        return _pin_closure_matches_generation(pin, manifest)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _run_retained_export_executor(operation: str, payload: dict[str, object]) -> int:
    if (
        operation not in {"touch-complete", "remove"}
        or os.geteuid() != 0
        or not _trusted_generation()
        or not _trusted_export_executor(RETAINED_LOAD_EXPORT_EXECUTOR)
    ):
        return 1
    owner = _retained_load_export_owner()
    if owner is None:
        return 1
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(raw) > 256:
        return 1
    command = [
        SETPRIV,
        f"--reuid={owner['uid']}",
        f"--regid={owner['gid']}",
        "--clear-groups",
        "--no-new-privs",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--",
        SYSTEM_PYTHON,
        "-I",
        "-B",
        str(RETAINED_LOAD_EXPORT_EXECUTOR),
        operation,
    ]
    try:
        process = subprocess.Popen(  # nosec B603 - fixed command and closed JSON schemas.
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=EXPORT_EXECUTOR_ENV,
            umask=0o077,
            start_new_session=True,
            close_fds=True,
        )
        process.communicate(input=raw, timeout=CLEANUP_OPERATION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        if "process" in locals() and process.poll() is None:
            _terminate_process_group(process)
        return 1
    return 0 if process.returncode == 0 else 1


def _touch_complete(payload: dict[str, object]) -> int:
    try:
        app_target_sha, expected_baseline, _source_arguments = _source_binding_context(payload)
    except (TypeError, ValueError, UnicodeEncodeError):
        return 1
    if not _current_pin_matches_host_generation(
        target_sha=app_target_sha,
        expected_baseline_identity=expected_baseline,
    ):
        return 1
    return _run_retained_export_executor(
        "touch-complete", {"schema": 1, "load_run_id": payload["run_id"]}
    )


def _remove_exports(
    *,
    load_run_id: str,
    cleanup_run_id: str,
    target_sha: str,
    expected_baseline_identity: object | None = None,
) -> int:
    if (
        RUN_ID_RE.fullmatch(load_run_id) is None
        or RUN_ID_RE.fullmatch(cleanup_run_id) is None
        or not _current_pin_matches_host_generation(
            target_sha=target_sha,
            expected_baseline_identity=expected_baseline_identity,
        )
    ):
        return 1
    return _run_retained_export_executor(
        "remove",
        {
            "schema": 1,
            "load_run_id": load_run_id,
            "cleanup_run_id": cleanup_run_id,
        },
    )


def _retained_load_export_owner() -> dict[str, int] | None:
    """Resolve the one provisioned export identity through the pinned helper."""

    if os.geteuid() != 0 or not _trusted_export_executor(RETAINED_LOAD_EXPORT_EXECUTOR):
        return None
    try:
        completed = subprocess.run(
            [SYSTEM_PYTHON, "-I", "-B", str(RETAINED_LOAD_EXPORT_EXECUTOR), "owner"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=EXPORT_EXECUTOR_ENV,
            timeout=10,
            umask=0o077,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 128:
        return None
    try:
        payload = _strict_baseline_json(completed.stdout)
    except OSError:
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"uid", "gid"}
        or type(payload["uid"]) is not int
        or type(payload["gid"]) is not int
        or not 0 < payload["uid"] < 2**31
        or not 0 < payload["gid"] < 2**31
    ):
        return None
    return {"uid": payload["uid"], "gid": payload["gid"]}


def _prepare_deployment(payload: dict[str, str]) -> int:
    if payload["mode"] != "deploy" or not _trusted_generation():
        return 2
    if not _verify_host_tools_contract(payload):
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
        sys.executable,
        "-I",
        "-B",
        str(ARTIFACT_DIR_HELPER),
        payload["artifact_remote_dir"],
    ]
    return _run_bounded_child(
        command,
        timeout_seconds=ARTIFACT_PREP_OPERATION_TIMEOUT_SECONDS,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 4 and arguments[0] == "host-contract":
        generation_sha, manifest_sha, capabilities_sha = arguments[1:]
        if (
            HOST_GENERATION_RE.fullmatch(generation_sha) is None
            or HEX_DIGEST_RE.fullmatch(manifest_sha) is None
            or HEX_DIGEST_RE.fullmatch(capabilities_sha) is None
            or not _trusted_generation()
            or not _verify_host_tools_contract(
                {
                    "host_tools": {
                        "host_tools_sha": generation_sha,
                        "manifest_sha256": manifest_sha,
                        "capabilities_sha256": capabilities_sha,
                    }
                }
            )
        ):
            return _fail()
        print(
            "HOST_TOOLS_CONTRACT "
            f"source_sha={generation_sha} generation={generation_sha} "
            f"manifest_sha256={manifest_sha} capabilities_sha256={capabilities_sha}"
        )
        return 0
    if arguments == ["host-capabilities"]:
        return _host_capabilities()
    if arguments == ["host-release-baseline"] or (
        arguments and arguments[0] == "host-release-baseline-match"
    ):
        if not _host_baseline_generation_ready():
            return _fail()
        try:
            actual = _release_baseline()
            if arguments == ["host-release-baseline"]:
                print(json.dumps(actual, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                return 0
            if len(arguments) != 2 or not isinstance(arguments[1], str) or len(arguments[1]) > 4096:
                return _fail()
            expected_raw = base64.b64decode(arguments[1], validate=True)
            expected = _strict_baseline_json(expected_raw)
        except (OSError, RuntimeError, ValueError, TypeError):
            return _fail()
        if not _baseline_identity_matches(expected, actual):
            return _fail()
        print("HOST_RELEASE_BASELINE status=match")
        return 0
    if arguments == ["cpu-diagnostic-plan"]:
        mode = "cpu-diagnostic"
    elif arguments == ["external-fixture"]:
        mode = "external"
    elif arguments == ["external-finalize"]:
        mode = "external"
    elif (
        len(arguments) == 5
        and arguments[:2] == ["external-cleanup", "--run-attempt"]
        and RUN_ID_RE.fullmatch(arguments[2]) is not None
        and arguments[3] == "--profile-id"
        and EXTERNAL_PROFILE_ID_RE.fullmatch(arguments[4]) is not None
    ):
        # Cleanup gets its own reduced identity document.  It must remain
        # usable when the measurement handoff (which also carries load
        # parameters and temporary session material) is unavailable or has
        # failed revalidation after fixture setup.
        mode = "cleanup"
    elif (
        len(arguments) == 5
        and arguments[:2] == ["retained-cleanup", "--run-attempt"]
        and RUN_ID_RE.fullmatch(arguments[2]) is not None
        and arguments[3] == "--load-run-id"
        and RUN_ID_RE.fullmatch(arguments[4]) is not None
    ):
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
        if arguments == ["cpu-diagnostic-plan"]:
            return _run_cpu_diagnostic_plan(payload)
        if arguments == ["external-fixture"]:
            return _external_fixture(payload)
        if arguments == ["external-finalize"]:
            return _touch_complete(payload)
        if len(arguments) == 5 and arguments[:2] == ["external-cleanup", "--run-attempt"]:
            app_target_sha, _expected_baseline, source_arguments = _source_binding_context(payload)
            if payload["load_run_id"] != payload["cleanup_run_id"]:
                return _fail()
            return _run_retained_cleanup_sudo(
                CLEANUP_HELPER,
                [
                    DELETE_CONFIRMATION,
                    payload["target_sha"],
                    payload["load_run_id"],
                    payload["cleanup_run_id"],
                    *source_arguments,
                ],
                control_email=payload["control_email"],
                diagnostic_binding={
                    "source_sha": payload["target_sha"],
                    "app_sha": app_target_sha,
                    "run_id": payload["cleanup_run_id"],
                    "load_run_id": payload["load_run_id"],
                    "run_attempt": arguments[2],
                    "profile": arguments[4],
                },
            )
        if len(arguments) == 5 and arguments[:2] == ["retained-cleanup", "--run-attempt"]:
            app_target_sha, _expected_baseline, source_arguments = _source_binding_context(payload)
            if payload["load_run_id"] != arguments[4]:
                return _fail()
            return _run_retained_cleanup_sudo(
                CLEANUP_HELPER,
                [
                    DELETE_CONFIRMATION,
                    payload["target_sha"],
                    payload["load_run_id"],
                    payload["cleanup_run_id"],
                    *source_arguments,
                ],
                control_email=payload["control_email"],
                diagnostic_binding={
                    "source_sha": payload["target_sha"],
                    "app_sha": app_target_sha,
                    "run_id": payload["cleanup_run_id"],
                    "load_run_id": payload["load_run_id"],
                    "run_attempt": arguments[2],
                    "profile": "retained-load-cleanup",
                },
            )
        if arguments == ["external-cleanup-exports"]:
            app_target_sha, expected_baseline, _source_arguments = _source_binding_context(payload)
            return _remove_exports(
                load_run_id=payload["load_run_id"],
                cleanup_run_id=payload["cleanup_run_id"],
                target_sha=app_target_sha,
                expected_baseline_identity=expected_baseline,
            )
        if arguments == ["production-prepare-artifact"]:
            if payload["mode"] != "deploy":
                return _fail()
            return _prepare_deployment(payload)
        if arguments == ["retained-cleanup"]:
            _app_target_sha, _expected_baseline, source_arguments = _source_binding_context(payload)
            return _run_retained_cleanup_sudo(
                CLEANUP_HELPER,
                [
                    DELETE_CONFIRMATION,
                    payload["target_sha"],
                    payload["load_run_id"],
                    payload["cleanup_run_id"],
                    *source_arguments,
                ],
                control_email=payload["control_email"],
            )
        if arguments == ["retained-cleanup-exports"]:
            app_target_sha, expected_baseline, _source_arguments = _source_binding_context(payload)
            return _remove_exports(
                load_run_id=payload["load_run_id"],
                cleanup_run_id=payload["cleanup_run_id"],
                target_sha=app_target_sha,
                expected_baseline_identity=expected_baseline,
            )
        if arguments == ["production-deploy"]:
            if payload["mode"] == "deploy" and not _verify_host_tools_contract(payload):
                return _fail()
            baseline_argument: list[str] = []
            if "baseline_identity" in payload:
                if payload["mode"] != "deploy" or "host_tools" not in payload:
                    return _fail()
                baseline_json = json.dumps(
                    payload["baseline_identity"],
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                baseline_argument = [base64.b64encode(baseline_json).decode("ascii")]
            return _run_sudo(
                DEPLOY_HELPER,
                [
                    payload["target_sha"],
                    payload["release_slug"],
                    payload["mode"],
                    payload["artifact_remote_dir"],
                    payload["runtime_profile"],
                    *(
                        [
                            payload["host_tools"]["host_tools_sha"],
                            payload["host_tools"]["manifest_sha256"],
                            payload["host_tools"]["capabilities_sha256"],
                        ]
                        if "host_tools" in payload
                        else []
                    ),
                    *baseline_argument,
                ],
                timeout_seconds=DEPLOY_OPERATION_TIMEOUT_SECONDS,
                expected_release_marker=(
                    payload["mode"],
                    payload["release_slug"],
                    payload["target_sha"],
                ),
            )
        if arguments == ["live-user-qa"]:
            if (
                payload["base_url"] != "https://old-sparky.com"
                or payload["provision"] != "false"
                or payload["marker"] != ""
            ):
                return _fail()
            app_target_sha, _expected_baseline, source_arguments = _source_binding_context(payload)
            return _run_live_user_qa_sudo(
                [payload["target_sha"], *source_arguments],
                expected_sha=payload["target_sha"],
                expected_app_sha=app_target_sha,
            )
        app_target_sha, _expected_baseline, source_arguments = _source_binding_context(payload)
        return _run_trusted_live_launch(
            [
                payload["base_url"],
                payload["provision"],
                payload["marker"],
                *source_arguments,
                payload["target_sha"],
            ],
            expected_app_sha=app_target_sha,
            expected_marker_sha256=hashlib.sha256(
                payload["marker"].encode("ascii")
            ).hexdigest(),
        )
    except (WorkflowInputError, OSError, ValueError, subprocess.SubprocessError):
        return _fail()


if __name__ == "__main__":
    raise SystemExit(main())
