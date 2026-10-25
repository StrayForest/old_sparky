#!/usr/bin/env python3
"""Non-root PID-namespace entry point for the external load worker.

The privileged execution chain is assembled by ``platform_load_runtime`` and
contains only absolute system binaries.  This checkout-controlled module is
invoked *after* the inner ``setpriv`` has dropped to the original runner
UID/GID.  It must never be used as a sudo target or as the pre-unshare
bootstrap.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

try:
    from tools.platform_load_runtime import (
        NamespaceCapabilityError,
        NamespaceIntegrityError,
        NAMESPACE_STARTUP_FAILURE_PREFIX,
        NAMESPACE_STARTUP_STAGE_CODES,
        WATCHDOG_STARTUP_STAGE_CODES,
        SYSTEM_SETPRIV,
        _assert_namespace_worker_identity,
        _namespace_command,
        _pidfd_is_live,
        _read_process_descendants,
        _read_process_starttime,
        _set_parent_death_signal,
        _verify_expected_parent,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_load_runtime import (  # type: ignore[no-redef]
        NamespaceCapabilityError,
        NamespaceIntegrityError,
        NAMESPACE_STARTUP_FAILURE_PREFIX,
        NAMESPACE_STARTUP_STAGE_CODES,
        WATCHDOG_STARTUP_STAGE_CODES,
        SYSTEM_SETPRIV,
        _assert_namespace_worker_identity,
        _namespace_command,
        _pidfd_is_live,
        _read_process_descendants,
        _read_process_starttime,
        _set_parent_death_signal,
        _verify_expected_parent,
    )


_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_EXTERNAL_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
_WORKER_BINDING_KEYS = frozenset(
    {
        "source_git_sha",
        "app_target_sha",
        "source_binding",
        "source_binding_sha256",
        "external_run_id",
    }
)
_SOURCE_BINDING_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCE_BINDING_BYTES = 16 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024


def _validated_worker_binding(
    payload: dict[str, object],
) -> tuple[str, str, dict[str, object] | None, str | None, str]:
    """Return only the exact runner/app identity validated in the private config."""

    binding = payload.get("binding")
    if not isinstance(binding, dict) or set(binding) != _WORKER_BINDING_KEYS:
        raise NamespaceIntegrityError("namespace worker binding is invalid")
    source_git_sha = binding.get("source_git_sha")
    app_target_sha = binding.get("app_target_sha")
    external_run_id = binding.get("external_run_id")
    source_binding = binding.get("source_binding")
    source_binding_sha256 = binding.get("source_binding_sha256")
    if (
        not isinstance(source_git_sha, str)
        or _SOURCE_SHA_RE.fullmatch(source_git_sha) is None
        or not isinstance(app_target_sha, str)
        or _SOURCE_SHA_RE.fullmatch(app_target_sha) is None
        or not isinstance(external_run_id, str)
        or _EXTERNAL_RUN_ID_RE.fullmatch(external_run_id) is None
    ):
        raise NamespaceIntegrityError("namespace worker binding is invalid")

    if source_binding is None:
        if app_target_sha != source_git_sha or source_binding_sha256 is not None:
            raise NamespaceIntegrityError("namespace worker source binding is invalid")
        return source_git_sha, app_target_sha, None, None, external_run_id

    if (
        not isinstance(source_binding, dict)
        or app_target_sha == source_git_sha
        or not isinstance(source_binding_sha256, str)
        or _SOURCE_BINDING_SHA256_RE.fullmatch(source_binding_sha256) is None
    ):
        raise NamespaceIntegrityError("namespace worker source binding is invalid")
    try:
        canonical = json.dumps(
            source_binding,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if (
            len(canonical) > _MAX_SOURCE_BINDING_BYTES
            or hashlib.sha256(canonical).hexdigest() != source_binding_sha256
            or source_binding.get("runner_sha") != source_git_sha
            or source_binding.get("app_target_sha") != app_target_sha
        ):
            raise ValueError("worker source binding does not match its digest or identities")
        try:
            from tools.platform_noop_source_binding import (
                validate_source_binding_handoff,
            )
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_noop_source_binding import validate_source_binding_handoff
        validate_source_binding_handoff(
            source_binding,
            expected_runner_sha=source_git_sha,
            expected_app_target_sha=app_target_sha,
            expected_security_run_id=source_binding.get("source_security_run_id"),
            expected_security_attempt=source_binding.get(
                "source_security_run_attempt"
            ),
            expected_autodeploy_run_id=source_binding.get("autodeploy_run_id"),
            expected_autodeploy_attempt=source_binding.get(
                "autodeploy_run_attempt"
            ),
            expected_deploy_run_id=source_binding.get("production_deploy_run_id"),
            expected_deploy_attempt=source_binding.get(
                "production_deploy_run_attempt"
            ),
            expected_artifact_id=source_binding.get("receipt_artifact_id"),
            expected_artifact_name=source_binding.get("receipt_artifact_name"),
            expected_artifact_digest=source_binding.get("receipt_artifact_digest"),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, RecursionError) as exc:
        raise NamespaceIntegrityError("namespace worker source binding is invalid") from exc
    return (
        source_git_sha,
        app_target_sha,
        source_binding,
        source_binding_sha256,
        external_run_id,
    )


def _config(
    path: Path, *, verify_worker_identity: bool = True,
    failure_stage: list[str] | None = None,
) -> dict[str, object]:
    if failure_stage is not None:
        failure_stage[:] = ["namespace_config_read"]
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > _MAX_CONFIG_BYTES
            ):
                raise NamespaceIntegrityError("namespace config permissions are invalid")
            raw_payload = handle.read(_MAX_CONFIG_BYTES + 1)
            if len(raw_payload) > _MAX_CONFIG_BYTES:
                raise NamespaceIntegrityError("namespace config is too large")
        payload = json.loads(raw_payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NamespaceIntegrityError("namespace config is unreadable") from exc
    if not isinstance(payload, dict):
        raise NamespaceIntegrityError("namespace config must be an object")
    if failure_stage is not None:
        failure_stage[:] = ["namespace_config_validate"]
    command = payload.get("worker_command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(value, str) or not value for value in command)
        or not Path(str(command[0])).is_absolute()
    ):
        raise NamespaceIntegrityError("namespace worker command is invalid")
    try:
        expected_uid = int(payload["runner_uid"])
        expected_gid = int(payload["runner_gid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NamespaceIntegrityError("namespace runner identity is missing") from exc
    if expected_uid != os.getuid():
        raise NamespaceIntegrityError("namespace config owner does not match runner")
    _validated_worker_binding(payload)
    if verify_worker_identity:
        if failure_stage is not None:
            failure_stage[:] = ["namespace_worker_identity"]
        _assert_namespace_worker_identity(expected_uid, expected_gid)
    return payload


class _NamespaceBootstrapFailure(NamespaceIntegrityError):
    """Retain only the closed startup phase and known exception class."""

    def __init__(self, stage: str, error_class: str) -> None:
        self.startup_stage = (
            stage if stage in NAMESPACE_STARTUP_STAGE_CODES else "unknown"
        )
        self.error_class = error_class if error_class in {
            "NamespaceCapabilityError", "NamespaceIntegrityError", "OSError",
            "ValueError",
        } else "unexpected"
        super().__init__(self.error_class)


def _write_namespace_failure_marker(descriptor: int, stage: str) -> None:
    code = NAMESPACE_STARTUP_STAGE_CODES.get(stage)
    if code is None:
        code = NAMESPACE_STARTUP_STAGE_CODES["unknown"]
    try:
        os.write(descriptor, NAMESPACE_STARTUP_FAILURE_PREFIX + code)
    except OSError:
        pass


def _restore_worker_binding_environment(payload: dict[str, object]) -> None:
    """Restore only the validated runner/app binding after sudo resets env."""

    (
        source_git_sha,
        app_target_sha,
        source_binding,
        source_binding_sha256,
        external_run_id,
    ) = _validated_worker_binding(payload)
    os.environ["SOURCE_GIT_SHA"] = source_git_sha
    os.environ["GITHUB_RUN_ID"] = external_run_id
    os.environ["APP_TARGET_SHA"] = app_target_sha
    if source_binding is None:
        os.environ.pop("SOURCE_BINDING_BASE64", None)
        os.environ.pop("SOURCE_BINDING_SHA256", None)
        return
    canonical = json.dumps(
        source_binding,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if source_binding_sha256 is None:
        raise NamespaceIntegrityError("namespace worker source binding is invalid")
    # The canonical bytes and digest were checked above before any environment
    # variable is restored. The child consumes this value through the same
    # closed parser used by the parent, never as a general environment map.
    os.environ["SOURCE_BINDING_BASE64"] = base64.b64encode(canonical).decode("ascii")
    os.environ["SOURCE_BINDING_SHA256"] = source_binding_sha256


_TRUSTED_ROOT_MEDIATOR_NAMES = frozenset({"sudo", "setpriv", "unshare"})


def _is_trusted_root_mediator(pid: int, starttime: int) -> bool:
    """Recognize only root system mediators under the captured exec chain."""

    status_path = Path(f"/proc/{pid}/status")
    try:
        before = _read_process_starttime(pid)
        lines = status_path.read_text(encoding="ascii").splitlines()
        status = {
            line.partition(":")[0]: line.partition(":")[2].strip()
            for line in lines
        }
        uid_fields = status["Uid"].split()
        namespace_pids = status["NSpid"].split()
        name = status["Name"]
        effective_uid = int(uid_fields[1])
        namespace_pid = int(namespace_pids[-1])
        after = _read_process_starttime(pid)
    except (IndexError, KeyError, OSError, UnicodeError, ValueError):
        return False
    return (
        before == starttime
        and after == starttime
        and effective_uid == 0
        and name in _TRUSTED_ROOT_MEDIATOR_NAMES
        and len(namespace_pids) == 1
        and namespace_pid != 1
    )


def _kill_tree(
    root_pid: int,
    signum: int,
    *,
    allow_trusted_root_mediator_eperm: bool = False,
) -> bool:
    """Signal only captured sudo-chain identities through their pidfds."""

    if not callable(getattr(signal, "pidfd_send_signal", None)):
        raise NamespaceIntegrityError("pidfd signalling is unavailable")

    records: list[tuple[int, int, int]] = []
    skipped_trusted_mediator = False
    try:
        for pid in (*_read_process_descendants(root_pid), root_pid):
            pidfd: int | None = None
            try:
                starttime = _read_process_starttime(pid)
                pidfd = os.pidfd_open(pid, 0)
                if _read_process_starttime(pid) != starttime:
                    os.close(pidfd)
                    continue
                records.append((pid, starttime, pidfd))
            except ProcessLookupError:
                if pidfd is not None:
                    os.close(pidfd)
                continue
            except (AttributeError, OSError, NamespaceIntegrityError) as exc:
                if pidfd is not None:
                    os.close(pidfd)
                if isinstance(exc, NamespaceIntegrityError):
                    continue
                raise NamespaceIntegrityError("pidfd capture failed") from exc
        for pid, starttime, pidfd in reversed(records):
            try:
                signal.pidfd_send_signal(pidfd, signum)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    if (
                        allow_trusted_root_mediator_eperm
                        and _is_trusted_root_mediator(pid, starttime)
                    ):
                        skipped_trusted_mediator = True
                        continue
                    raise NamespaceIntegrityError(
                        "watchdog_pidfd_signal_eperm"
                    ) from exc
                raise NamespaceIntegrityError("pidfd signal failed") from exc
            except OSError as exc:
                raise NamespaceIntegrityError("pidfd signal failed") from exc
    finally:
        for _pid, _starttime, pidfd in records:
            try:
                os.close(pidfd)
            except OSError:
                pass
    return skipped_trusted_mediator


def _wait_child(child_pid: int, *, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return 0
        if waited == child_pid:
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                return -os.WTERMSIG(status)
            return 125
        time.sleep(0.01)
    return None


def _kill_tree_with_diagnostic(
    root_pid: int,
    signum: int,
    diagnostic_fd: int,
    *,
    allow_trusted_root_mediator_eperm: bool = False,
) -> None:
    try:
        skipped = _kill_tree(
            root_pid,
            signum,
            allow_trusted_root_mediator_eperm=allow_trusted_root_mediator_eperm,
        )
        if skipped:
            try:
                os.write(diagnostic_fd, b"\x01")
            except OSError:
                pass
    except NamespaceIntegrityError as exc:
        if str(exc) == "watchdog_pidfd_signal_eperm":
            try:
                os.write(diagnostic_fd, b"\x02")
            except OSError:
                pass
        raise


def _write_watchdog_startup_code(diagnostic_fd: int, stage: str) -> None:
    code = WATCHDOG_STARTUP_STAGE_CODES.get(stage)
    if code is None:
        return
    try:
        os.write(diagnostic_fd, code)
    except OSError:
        pass


def _exec_watchdog_child(
    *, parent_pidfd: int, diagnostic_fd: int, command: list[str],
) -> None:
    """Exec the system chain with one CLOEXEC startup marker on failure."""

    stage = "watchdog_parent_pidfd_close"
    try:
        os.close(parent_pidfd)
        stage = "watchdog_diagnostic_cloexec"
        os.set_inheritable(diagnostic_fd, False)
        stage = "watchdog_pdeathsig"
        _set_parent_death_signal()
        stage = "watchdog_exec"
        os.execv(command[0], command)
    except BaseException:
        _write_watchdog_startup_code(diagnostic_fd, stage)
        os._exit(125)


def _wrapper_watchdog(args: argparse.Namespace) -> int:
    """Keep the exact sudo chain reclaimable if the Python supervisor dies.

    sudo may be setuid and can clear a parent-death signal during its exec.
    This non-root checkout process therefore remains as a tiny watchdog
    parent.  It holds the supervisor pidfd, forks the absolute system chain,
    and identity-signals that chain if the pidfd closes.  It never executes as
    root and never becomes a production/load helper in the privileged chain.
    """

    stage = "watchdog_parent_identity"
    try:
        _verify_expected_parent(
            expected_parent_pid=args.expected_parent_pid,
            expected_parent_starttime=args.expected_parent_starttime,
            parent_pidfd=args.parent_pidfd_fd,
        )
        stage = "watchdog_config"
        payload = _config(args.config, verify_worker_identity=False)
        stage = "watchdog_command"
        command = _namespace_command(
            worker_command=[str(value) for value in payload["worker_command"]],
            config_path=args.config,
            helper_path=Path(__file__).resolve(),
            expected_uid=int(payload["runner_uid"]),
            expected_gid=int(payload["runner_gid"]),
        )
    # Keep a non-root setpriv process between the checkout watchdog and sudo.
    # The setuid sudo exec is allowed to clear PDEATHSIG; this outer system
    # setpriv therefore cannot be the final proof by itself, but it preserves
    # the requested signal contract for sudo implementations that exec in
    # place.  The PID1 pidfd and bounded chain reap remain authoritative.
        command = [
            SYSTEM_SETPRIV,
            "--pdeathsig",
            "SIGKILL",
            "--",
            *command,
        ]
        stage = "watchdog_fork"
        child_pid = os.fork()
    except (NamespaceCapabilityError, NamespaceIntegrityError, OSError, ValueError):
        _write_watchdog_startup_code(args.diagnostic_fd, stage)
        raise
    if child_pid == 0:
        _exec_watchdog_child(
            parent_pidfd=args.parent_pidfd_fd,
            diagnostic_fd=args.diagnostic_fd,
            command=command,
        )

    pending_signal: list[int | None] = [None]

    def request_termination(signum: int, _frame: object) -> None:
        pending_signal[0] = signum

    stage = "watchdog_supervision"
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, request_termination)
        while True:
            if not _pidfd_is_live(args.parent_pidfd_fd):
                _kill_tree_with_diagnostic(
                    child_pid,
                    signal.SIGKILL,
                    args.diagnostic_fd,
                )
                if _wait_child(child_pid, timeout=2.0) is None:
                    raise NamespaceIntegrityError(
                        "watchdog_child_did_not_reap_after_parent_death"
                    )
                return 137
            if pending_signal[0] is not None:
                requested = pending_signal[0]
                _kill_tree_with_diagnostic(
                    child_pid,
                    requested,
                    args.diagnostic_fd,
                    allow_trusted_root_mediator_eperm=True,
                )
                if _wait_child(child_pid, timeout=0.25) is None:
                    _kill_tree_with_diagnostic(
                        child_pid,
                        signal.SIGKILL,
                        args.diagnostic_fd,
                        allow_trusted_root_mediator_eperm=True,
                    )
                    _wait_child(child_pid, timeout=2.0)
                # Preserve the signal-shaped return code seen by the parent.
                signal.signal(requested, signal.SIG_DFL)
                os.kill(os.getpid(), requested)
                os._exit(128 + requested)
            result = _wait_child(child_pid, timeout=0.05)
            if result is not None:
                if result < 0:
                    signal.signal(-result, signal.SIG_DFL)
                    os.kill(os.getpid(), -result)
                    os._exit(128 - result)
                return result
    except (NamespaceCapabilityError, NamespaceIntegrityError, OSError, ValueError):
        _write_watchdog_startup_code(args.diagnostic_fd, stage)
        raise
    finally:
        try:
            os.close(args.parent_pidfd_fd)
        except OSError:
            pass
        try:
            os.close(args.diagnostic_fd)
        except OSError:
            pass


def _namespace_worker(config_path: Path) -> None:
    # This is deliberately repeated after the exec from setpriv.  It covers
    # both the exact credential/capability contract and supervisor death after
    # the helper starts but before the handshake is acknowledged.
    stage = "namespace_failure_channel"
    failure_fd: int | None = None
    try:
        failure_fd = os.dup(1)
        os.set_inheritable(failure_fd, False)
        stage = "namespace_pdeathsig"
        _set_parent_death_signal()
        config_stage = ["namespace_config_read"]
        try:
            payload = _config(config_path, failure_stage=config_stage)
        except (NamespaceCapabilityError, NamespaceIntegrityError, OSError, ValueError):
            stage = config_stage[0]
            raise
        stage = config_stage[0]
        stage = "namespace_ready_write"
        os.write(1, b"READY\n")
        stage = "namespace_ack"
        acknowledgement = os.read(0, 4)
        if acknowledgement != b"ACK\n":
            raise NamespaceIntegrityError("namespace readiness acknowledgement missing")

        # The parent only needs stdout for the bounded handshake. Discard
        # worker output after ACK; the CLOEXEC duplicate remains a private
        # failure channel only until the worker exec succeeds.
        stage = "namespace_stdio_redirect"
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        finally:
            os.close(devnull)
        command = [str(value) for value in payload["worker_command"]]
        # Restore only the validated binding after the non-root drop.
        stage = "namespace_binding_restore"
        os.environ["PLATFORM_LOAD_WORKER_CONFIG"] = str(config_path)
        _restore_worker_binding_environment(payload)
        stage = "namespace_worker_exec"
        os.execv(command[0], command)
    except (NamespaceCapabilityError, NamespaceIntegrityError, OSError, ValueError) as exc:
        _write_namespace_failure_marker(failure_fd if failure_fd is not None else 1, stage)
        error_class = (
            exc.error_class if isinstance(exc, _NamespaceBootstrapFailure)
            else type(exc).__name__
        )
        raise _NamespaceBootstrapFailure(stage, error_class) from None
    finally:
        if failure_fd is not None:
            try:
                os.close(failure_fd)
            except OSError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Non-root load namespace entry")
    parser.add_argument("--mode", choices=("wrapper-watchdog", "namespace-worker"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-parent-pid", type=int)
    parser.add_argument("--expected-parent-starttime", type=int)
    parser.add_argument("--parent-pidfd-fd", type=int)
    parser.add_argument("--diagnostic-fd", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "wrapper-watchdog":
        if (
            args.expected_parent_pid is None
            or args.expected_parent_starttime is None
            or args.parent_pidfd_fd is None
            or args.diagnostic_fd is None
        ):
            raise SystemExit("watchdog parent identity is required")
        return _wrapper_watchdog(args)
    else:
        _namespace_worker(args.config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (NamespaceCapabilityError, NamespaceIntegrityError, OSError, ValueError) as exc:
        error_class = (
            exc.error_class if isinstance(exc, _NamespaceBootstrapFailure)
            else type(exc).__name__
        )
        print(f"namespace worker failed: {error_class}", file=sys.stderr)
        raise SystemExit(125) from exc
