#!/usr/bin/env python3
"""Resolve the dedicated retained-load artifact owner and remove exact exports.

The root-only ``owner`` command validates the provisioned account before a
producer creates private export files. The ``remove`` command is run only
after the pinned root dispatcher has validated the same account and dropped
to it; it accepts two positive run identifiers from a closed stdin document
and never accepts paths, UIDs, GIDs, identities, or environment passthrough.
"""

from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import stat
import sys
from dataclasses import dataclass
from typing import Any


ACCOUNT_NAME = "oldsparky-load-artifacts"
ACCOUNT_HOME = "/nonexistent"
ACCOUNT_SHELL = "/usr/sbin/nologin"
FIXED_ENVIRONMENT = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
}
SERVICE_ACCOUNTS = (
    "oldsparky-api",
    "oldsparky-web",
    "oldsparky-worker",
    "oldsparky-liveqa",
)
TMP_ROOT = Path("/tmp")
LOAD_PREFIX = "old-sparky-production-retained-load-"
CLEANUP_PREFIX = "old-sparky-production-retained-cleanup-"
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
MAX_STDIN_BYTES = 4096
MAX_SHADOW_BYTES = 1024 * 1024

LOAD_EXPORT_NAMES = frozenset(
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
)
CLEANUP_EXPORT_NAMES = frozenset(
    {"cleanup-summary.json", "canonical.log", "cleanup.log"}
)


class ExportCleanupError(RuntimeError):
    """A fixed, non-sensitive artifact-boundary failure."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class ArtifactIdentity:
    uid: int
    gid: int


@dataclass(frozen=True)
class PlannedRoot:
    name: str
    descriptor: int
    metadata: os.stat_result
    entries: tuple[tuple[str, os.stat_result], ...]


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _locked_account_password() -> bool:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("/etc/shadow", flags)
    except OSError as exc:
        raise ExportCleanupError("account_shadow_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_nlink != 1
            or before.st_size > MAX_SHADOW_BYTES
            or stat.S_IMODE(before.st_mode) & 0o007
        ):
            raise ExportCleanupError("account_shadow_metadata")
        raw = bytearray()
        while len(raw) <= MAX_SHADOW_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_SHADOW_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > MAX_SHADOW_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ExportCleanupError("account_shadow_changed")
    finally:
        os.close(descriptor)

    try:
        text = raw.decode("ascii")
    except UnicodeError as exc:
        raise ExportCleanupError("account_shadow_encoding") from exc
    matches = [
        line.split(":")
        for line in text.splitlines()
        if line.split(":", 1)[0] == ACCOUNT_NAME
    ]
    if len(matches) != 1 or len(matches[0]) < 2:
        raise ExportCleanupError("account_shadow_entry")
    return matches[0][1].startswith(("!", "*"))


def _verify_no_sudo_access() -> None:
    sudo = "/usr/bin/sudo"
    try:
        metadata = os.stat(sudo, follow_symlinks=False)
    except OSError as exc:
        raise ExportCleanupError("sudo_policy_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise ExportCleanupError("sudo_binary_metadata")
    try:
        result = subprocess.run(
            [sudo, "-n", "-l", "-U", ACCOUNT_NAME],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            close_fds=True,
            env={"PATH": FIXED_ENVIRONMENT["PATH"], "LC_ALL": "C"},
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportCleanupError("sudo_policy_check_failed") from exc
    output = result.stdout.decode("utf-8", "replace")
    denied = re.fullmatch(
        rf"User {re.escape(ACCOUNT_NAME)} is not allowed to run sudo on [^\r\n]+\.?\s*",
        output,
    )
    if result.returncode != 1 or denied is None:
        raise ExportCleanupError("sudo_access_present_or_unknown")


def resolve_artifact_identity(
    *, require_root: bool, verify_shadow: bool = True
) -> ArtifactIdentity:
    if require_root and os.geteuid() != 0:
        raise ExportCleanupError("owner_check_requires_root")
    try:
        user = pwd.getpwnam(ACCOUNT_NAME)
        primary_group = grp.getgrnam(ACCOUNT_NAME)
        passwd_entries = pwd.getpwall()
        group_entries = grp.getgrall()
    except (KeyError, OSError) as exc:
        raise ExportCleanupError("account_lookup_failed") from exc

    uid_matches = [entry for entry in passwd_entries if entry.pw_uid == user.pw_uid]
    gid_matches = [
        entry for entry in group_entries if entry.gr_gid == primary_group.gr_gid
    ]
    named_users = [entry for entry in passwd_entries if entry.pw_name == ACCOUNT_NAME]
    named_groups = [entry for entry in group_entries if entry.gr_name == ACCOUNT_NAME]
    supplementary = [entry for entry in group_entries if ACCOUNT_NAME in entry.gr_mem]
    if (
        user.pw_uid <= 0
        or user.pw_gid <= 0
        or user.pw_gid != primary_group.gr_gid
        or user.pw_dir != ACCOUNT_HOME
        or user.pw_shell != ACCOUNT_SHELL
        or len(uid_matches) != 1
        or len(gid_matches) != 1
        or len(named_users) != 1
        or len(named_groups) != 1
        or supplementary
    ):
        raise ExportCleanupError("account_contract_invalid")
    for service_name in SERVICE_ACCOUNTS:
        try:
            service = pwd.getpwnam(service_name)
        except KeyError:
            continue
        if service.pw_uid == user.pw_uid or service.pw_gid == primary_group.gr_gid:
            raise ExportCleanupError("account_identity_collision")
    if verify_shadow and not _locked_account_password():
        raise ExportCleanupError("account_password_unlocked")
    if verify_shadow:
        _verify_no_sudo_access()
    return ArtifactIdentity(uid=user.pw_uid, gid=primary_group.gr_gid)


def _open_descriptors() -> set[int]:
    try:
        descriptors = {
            int(name) for name in os.listdir("/proc/self/fd") if name.isdecimal()
        }
    except OSError as exc:
        raise ExportCleanupError("worker_descriptors_unavailable") from exc
    # listdir's own descriptor can appear in the returned snapshot but is
    # closed before listdir returns. Ignore only entries already gone.
    return {fd for fd in descriptors if os.path.exists(f"/proc/self/fd/{fd}")}


def _verify_dropped_identity(identity: ArtifactIdentity, allowed_fds: set[int]) -> None:
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ExportCleanupError("worker_identity_unavailable") from exc
    fields: dict[str, tuple[int, ...]] = {}
    for line in status.splitlines():
        if line.startswith("Uid:") or line.startswith("Gid:"):
            name, values = line.split(":", 1)
            try:
                fields[name] = tuple(int(value) for value in values.split())
            except ValueError as exc:
                raise ExportCleanupError("worker_identity_malformed") from exc
    if (
        identity.uid == 0
        or identity.gid == 0
        or os.getuid() != identity.uid
        or os.geteuid() != identity.uid
        or os.getgid() != identity.gid
        or os.getegid() != identity.gid
        or os.getgroups()
        or fields.get("Uid") != (identity.uid,) * 4
        or fields.get("Gid") != (identity.gid,) * 4
        or os.getcwd() != "/"
        or os.environ != FIXED_ENVIRONMENT
        or _open_descriptors() != allowed_fds
    ):
        raise ExportCleanupError("worker_identity_mismatch")
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
        capabilities = {
            line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in status.splitlines()
            if line.startswith(
                ("CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:", "NoNewPrivs:")
            )
        }
    except (OSError, UnicodeError) as exc:
        raise ExportCleanupError("worker_capabilities_unavailable") from exc
    if (
        any(
            capabilities.get(key) != "0000000000000000"
            for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        or capabilities.get("NoNewPrivs") != "1"
    ):
        raise ExportCleanupError("worker_capabilities_invalid")


def _parse_payload(stream: Any) -> dict[str, str]:
    raw = stream.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ExportCleanupError("input_too_large")
    try:
        payload = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExportCleanupError("input_invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "load_run_id", "cleanup_run_id"}
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
        or type(payload.get("load_run_id")) is not str
        or RUN_ID_RE.fullmatch(payload["load_run_id"]) is None
        or type(payload.get("cleanup_run_id")) is not str
        or RUN_ID_RE.fullmatch(payload["cleanup_run_id"]) is None
    ):
        raise ExportCleanupError("input_invalid")
    return payload


def _tmp_descriptor() -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(TMP_ROOT, flags)
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(TMP_ROOT, follow_symlinks=False)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ExportCleanupError("tmp_root_unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or not stat.S_IMODE(metadata.st_mode) & stat.S_ISVTX
        or (metadata.st_dev, metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        os.close(descriptor)
        raise ExportCleanupError("tmp_root_metadata")
    return descriptor, metadata


def _entry_metadata(
    root_fd: int, name: str, root_metadata: os.stat_result, identity: ArtifactIdentity
) -> os.stat_result:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # O_PATH inspects the directory entry without opening a device or FIFO.
    flags = getattr(os, "O_PATH", os.O_RDONLY)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0) | nofollow
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise ExportCleanupError("entry_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise ExportCleanupError("entry_stat_failed") from exc
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
    )
    identity_opened = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
        opened.st_gid,
        opened.st_nlink,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
    )
    if (
        identity_before != identity_opened
        or identity_opened != identity_after
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != root_metadata.st_dev
        or opened.st_uid != identity.uid
        or opened.st_gid != identity.gid
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        raise ExportCleanupError("entry_metadata_invalid")
    return opened


def _plan_root(
    tmp_fd: int,
    tmp_metadata: os.stat_result,
    name: str,
    allowed_names: frozenset[str],
    identity: ArtifactIdentity,
) -> PlannedRoot | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=tmp_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExportCleanupError("export_root_open_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
        if (
            (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != tmp_metadata.st_dev
            or metadata.st_uid != identity.uid
            or metadata.st_gid != identity.gid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ExportCleanupError("export_root_metadata_invalid")
        try:
            entries = os.listdir(descriptor)
        except OSError as exc:
            raise ExportCleanupError("export_root_list_failed") from exc
        if any(entry not in allowed_names for entry in entries):
            raise ExportCleanupError("export_root_inventory_invalid")
        entry_records = tuple(
            (entry, _entry_metadata(descriptor, entry, metadata, identity))
            for entry in entries
        )
        return PlannedRoot(name, descriptor, metadata, entry_records)
    except Exception:
        os.close(descriptor)
        raise


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (left.st_dev, left.st_ino, left.st_mode, left.st_uid, left.st_gid)
        == (right.st_dev, right.st_ino, right.st_mode, right.st_uid, right.st_gid)
    )


def remove_exact_exports(payload: dict[str, str], identity: ArtifactIdentity) -> int:
    _verify_dropped_identity(identity, {0, 1, 2})
    tmp_fd, tmp_metadata = _tmp_descriptor()
    roots: list[PlannedRoot] = []
    try:
        for name, allowed_names in (
            (f"{LOAD_PREFIX}{payload['load_run_id']}", LOAD_EXPORT_NAMES),
            (f"{CLEANUP_PREFIX}{payload['cleanup_run_id']}", CLEANUP_EXPORT_NAMES),
        ):
            planned = _plan_root(tmp_fd, tmp_metadata, name, allowed_names, identity)
            if planned is not None:
                roots.append(planned)

        # Both exact trees are fully inventoried before the first unlink. Keep
        # their directory descriptors open and revalidate every identity at
        # the mutation boundary.
        tmp_after = os.fstat(tmp_fd)
        _verify_dropped_identity(
            identity,
            {0, 1, 2, tmp_fd, *(planned.descriptor for planned in roots)},
        )
        if (tmp_metadata.st_dev, tmp_metadata.st_ino) != (
            tmp_after.st_dev,
            tmp_after.st_ino,
        ):
            raise ExportCleanupError("tmp_root_changed")
        for planned in roots:
            current = os.stat(planned.name, dir_fd=tmp_fd, follow_symlinks=False)
            if not _same_stat(planned.metadata, current):
                raise ExportCleanupError("export_root_changed")
            actual_names = set(os.listdir(planned.descriptor))
            if actual_names != {name for name, _metadata in planned.entries}:
                raise ExportCleanupError("export_root_inventory_changed")
            for name, expected in planned.entries:
                current_entry = _entry_metadata(
                    planned.descriptor, name, planned.metadata, identity
                )
                if not _same_stat(expected, current_entry):
                    raise ExportCleanupError("export_entry_changed")

        for planned in roots:
            for name, expected in planned.entries:
                current = _entry_metadata(
                    planned.descriptor, name, planned.metadata, identity
                )
                if not _same_stat(expected, current):
                    raise ExportCleanupError("export_entry_changed")
                try:
                    os.unlink(name, dir_fd=planned.descriptor)
                except OSError as exc:
                    raise ExportCleanupError("export_entry_unlink_failed") from exc
            if os.listdir(planned.descriptor):
                raise ExportCleanupError("export_root_not_empty")
            current_root = os.stat(planned.name, dir_fd=tmp_fd, follow_symlinks=False)
            if not _same_directory_identity(planned.metadata, current_root):
                raise ExportCleanupError("export_root_changed")
            try:
                os.rmdir(planned.name, dir_fd=tmp_fd)
            except OSError as exc:
                raise ExportCleanupError("export_root_remove_failed") from exc
        return len(roots)
    except ExportCleanupError:
        raise
    except OSError as exc:
        raise ExportCleanupError("filesystem_operation_failed") from exc
    finally:
        for planned in roots:
            os.close(planned.descriptor)
        os.close(tmp_fd)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["owner"]:
        try:
            identity = resolve_artifact_identity(require_root=True)
        except ExportCleanupError as exc:
            print(
                f"ARTIFACT_EXPORT_OWNER schema=1 status=failed error_class={exc.error_class}",
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {"uid": identity.uid, "gid": identity.gid}, separators=(",", ":")
            )
        )
        return 0
    if args == ["remove"]:
        try:
            identity = resolve_artifact_identity(
                require_root=False, verify_shadow=False
            )
            payload = _parse_payload(sys.stdin)
            removed_roots = remove_exact_exports(payload, identity)
        except ExportCleanupError as exc:
            print(
                f"ARTIFACT_EXPORT_REMOVE schema=1 status=failed error_class={exc.error_class}"
            )
            return 1
        print(
            f"ARTIFACT_EXPORT_REMOVE schema=1 status=passed removed_roots={removed_roots}"
        )
        return 0
    print(
        "ARTIFACT_EXPORT_EXECUTOR schema=1 status=failed error_class=invalid_command",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
