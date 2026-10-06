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
import errno
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
        NamespaceIntegrityError,
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
        NamespaceIntegrityError,
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
_WORKER_BINDING_KEYS = frozenset({"source_git_sha", "external_run_id"})
_MAX_CONFIG_BYTES = 1024 * 1024


def _config(path: Path, *, verify_worker_identity: bool = True) -> dict[str, object]:
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
    binding = payload.get("binding")
    if not isinstance(binding, dict) or set(binding) != _WORKER_BINDING_KEYS:
        raise NamespaceIntegrityError("namespace worker binding is invalid")
    source_git_sha = binding.get("source_git_sha")
    external_run_id = binding.get("external_run_id")
    if (
        not isinstance(source_git_sha, str)
        or _SOURCE_SHA_RE.fullmatch(source_git_sha) is None
        or not isinstance(external_run_id, str)
        or _EXTERNAL_RUN_ID_RE.fullmatch(external_run_id) is None
    ):
        raise NamespaceIntegrityError("namespace worker binding is invalid")
    if verify_worker_identity:
        _assert_namespace_worker_identity(expected_uid, expected_gid)
    return payload


def _restore_worker_binding_environment(payload: dict[str, object]) -> None:
    """Restore only the validated source/run binding after sudo resets env."""

    binding = payload.get("binding")
    if not isinstance(binding, dict) or set(binding) != _WORKER_BINDING_KEYS:
        raise NamespaceIntegrityError("namespace worker binding is invalid")
    source_git_sha = binding.get("source_git_sha")
    external_run_id = binding.get("external_run_id")
    if (
        not isinstance(source_git_sha, str)
        or _SOURCE_SHA_RE.fullmatch(source_git_sha) is None
        or not isinstance(external_run_id, str)
        or _EXTERNAL_RUN_ID_RE.fullmatch(external_run_id) is None
    ):
        raise NamespaceIntegrityError("namespace worker binding is invalid")
    os.environ["SOURCE_GIT_SHA"] = source_git_sha
    os.environ["GITHUB_RUN_ID"] = external_run_id


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


def _wrapper_watchdog(args: argparse.Namespace) -> int:
    """Keep the exact sudo chain reclaimable if the Python supervisor dies.

    sudo may be setuid and can clear a parent-death signal during its exec.
    This non-root checkout process therefore remains as a tiny watchdog
    parent.  It holds the supervisor pidfd, forks the absolute system chain,
    and identity-signals that chain if the pidfd closes.  It never executes as
    root and never becomes a production/load helper in the privileged chain.
    """

    _verify_expected_parent(
        expected_parent_pid=args.expected_parent_pid,
        expected_parent_starttime=args.expected_parent_starttime,
        parent_pidfd=args.parent_pidfd_fd,
    )
    payload = _config(args.config, verify_worker_identity=False)
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
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(args.parent_pidfd_fd)
            os.close(args.diagnostic_fd)
            _set_parent_death_signal()
            os.execv(command[0], command)
        except BaseException:
            os._exit(125)

    pending_signal: list[int | None] = [None]

    def request_termination(signum: int, _frame: object) -> None:
        pending_signal[0] = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_termination)
    try:
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
    _set_parent_death_signal()
    payload = _config(config_path)
    try:
        os.write(1, b"READY\n")
        acknowledgement = os.read(0, 4)
    except OSError as exc:
        raise NamespaceIntegrityError("namespace stdio handshake failed") from exc
    if acknowledgement != b"ACK\n":
        raise NamespaceIntegrityError("namespace readiness acknowledgement missing")

    # The parent only needs stdout for the bounded handshake.  Discard worker
    # output after ACK so a chatty candidate cannot block the supervisor pipe.
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        os.close(devnull)
    command = [str(value) for value in payload["worker_command"]]
    # sudo is allowed to close/reset arbitrary inherited descriptors and
    # environment entries. Re-establish the exact validated report binding
    # only after the non-root drop, immediately before the worker exec.
    os.environ["PLATFORM_LOAD_WORKER_CONFIG"] = str(config_path)
    _restore_worker_binding_environment(payload)
    os.execv(command[0], command)


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
    except (NamespaceIntegrityError, OSError, ValueError) as exc:
        print(f"namespace worker failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(125) from exc
