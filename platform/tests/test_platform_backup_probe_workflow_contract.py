"""Behavior contracts for the production authorization-probe workflow."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest

from tools import platform_backup_restore_drill


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-backup.yml"
RUN_ID = "12345"
RUN_ATTEMPT = "2"
SECURITY_ID = "67890"
SECURITY_ATTEMPT = "1"
SOURCE_SHA = "a" * 40
BUNDLE_SHA = "b" * 64
FAULT = {
    "schema": 1,
    "origin": "worker",
    "site": "worker_role_setup",
    "exception_kind": "permission",
    "errno": 13,
}
HOST_GUARD = {
    "schema": 2,
    "guard_site": "parent_before_authorization_probe",
    "guard_reason": "memory_reserve",
    "available_bytes": 900_000_000,
    "required_bytes": 1_073_741_824,
}


def _heredoc_after(source: str, anchor: str) -> str:
    start = source.index(anchor)
    heredoc = source.index("<<'PY'\n", start) + len("<<'PY'\n")
    end = source.index("\n          PY", heredoc)
    return textwrap.dedent(source[heredoc:end])


def _heredoc_with_suffix_after(source: str, anchor: str) -> str:
    start = source.index(anchor)
    match = re.search(r"<<'PY'[^\n]*\n", source[start:])
    if match is None:
        raise AssertionError(f"no Python heredoc after {anchor!r}")
    heredoc = start + match.end()
    end = source.index("\n          PY", heredoc)
    return textwrap.dedent(source[heredoc:end])


def _workflow_run_scalar(source: str, step_name: str) -> str:
    step_start = source.index(f"- name: {step_name}")
    run_start = source.index("\n        run: |\n", step_start) + len("\n        run: |\n")
    next_step = source.find("\n      - name:", run_start)
    if next_step < 0:
        next_step = len(source)
    return textwrap.dedent(source[run_start:next_step])


def _heredocs(source: str, delimiter: str) -> list[str]:
    lines = source.splitlines()
    bodies = []
    index = 0
    while index < len(lines):
        if f"<<'{delimiter}'" not in lines[index]:
            index += 1
            continue
        index += 1
        body = []
        while index < len(lines) and lines[index] != delimiter:
            body.append(lines[index])
            index += 1
        if index == len(lines):
            raise AssertionError(f"unterminated {delimiter} heredoc")
        bodies.append("\n".join(body) + "\n")
        index += 1
    return bodies


def _literal_set_assignment(tree: ast.AST, name: str) -> frozenset[str]:
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if len(assignments) != 1:
        raise AssertionError(f"expected one closed {name} set in the workflow handler")
    value = ast.literal_eval(assignments[0].value)
    if not isinstance(value, set) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"workflow {name} is not a literal string set")
    return frozenset(value)


class _StatView:
    def __init__(self, value: os.stat_result, *, fake_root: bool = True, inode_delta: int = 0):
        for field in (
            "st_mode", "st_nlink", "st_size", "st_dev", "st_ino",
            "st_mtime_ns", "st_ctime_ns",
        ):
            setattr(self, field, getattr(value, field))
        self.st_ino += inode_delta
        self.st_uid = 0 if fake_root else value.st_uid
        self.st_gid = 0 if fake_root else value.st_gid


class _FaultOS:
    """Delegate real filesystem operations while injecting narrow syscall faults."""

    def __init__(self, fault: str | None = None):
        self.fault = fault
        self.regular_fsyncs = 0
        self.directory_fsyncs = 0
        self.writes = 0
        self.last_regular_kind = None
        self.final_stat_calls = 0
        self.final_receipt_open_calls = 0
        self.short_read_fds = set()

    def __getattr__(self, name):
        return getattr(os, name)

    def fstat(self, descriptor):
        return _StatView(os.fstat(descriptor))

    def stat(self, path, *args, **kwargs):
        value = os.stat(path, *args, **kwargs)
        if (
            self.fault == "pending_identity"
            and isinstance(path, str)
            and path.endswith(".pending.json")
        ):
            return _StatView(value, inode_delta=1)
        if path == f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json":
            self.final_stat_calls += 1
            if self.fault == "identity_swap" and self.final_stat_calls == 1:
                return _StatView(value, inode_delta=1)
        return _StatView(value)

    def open(self, path, flags, *args, **kwargs):
        is_final_receipt = (
            path == f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"
            and flags & (os.O_WRONLY | os.O_RDWR) == 0
        )
        if is_final_receipt:
            self.final_receipt_open_calls += 1
            if self.fault == "final_inode_swap" and self.final_receipt_open_calls == 2:
                directory_fd = kwargs["dir_fd"]
                displaced = path + ".displaced"
                old_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    content = bytearray()
                    while True:
                        block = os.read(old_descriptor, 4096)
                        if not block:
                            break
                        content.extend(block)
                finally:
                    os.close(old_descriptor)
                os.rename(path, displaced, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                replacement = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                    dir_fd=directory_fd,
                )
                try:
                    view = memoryview(content)
                    while view:
                        view = view[os.write(replacement, view):]
                    os.fsync(replacement)
                finally:
                    os.close(replacement)
                os.unlink(displaced, dir_fd=directory_fd)
        return os.open(path, flags, *args, **kwargs)

    def lstat(self, path):
        return _StatView(os.lstat(path))

    def fchown(self, descriptor, uid, gid):
        if (uid, gid) != (0, 0):
            raise AssertionError("workflow owner must remain root:root")

    def fsync(self, descriptor):
        value = os.fstat(descriptor)
        if stat.S_ISREG(value.st_mode):
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if target.endswith(f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json"):
                self.last_regular_kind = "diagnostic"
                if self.fault == "diagnostic_fsync":
                    raise OSError("injected diagnostic fsync failure")
            elif target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json"):
                self.last_regular_kind = "pending"
                if self.fault == "pending_fsync":
                    raise OSError("injected pending projection fsync failure")
            elif target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"):
                self.last_regular_kind = "final"
                if self.fault == "final_fsync":
                    raise OSError("injected final receipt fsync failure")
        else:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if target.endswith("/shared"):
                if self.fault == "pending_parent_fsync" and self.last_regular_kind == "pending":
                    raise OSError("injected pending parent fsync failure")
                if self.fault == "final_parent_fsync" and self.last_regular_kind == "final":
                    raise OSError("injected final parent fsync failure")
                if self.fault in {"final_content_mutation", "final_binding_mutation"} and self.last_regular_kind == "final":
                    name = f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"
                    target_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        content = os.pread(target_fd, 4096, 0)
                        if self.fault == "final_content_mutation":
                            mutated = b"2" + content[1:]
                        else:
                            original = f'"workflow_run_id":"{RUN_ID}"'.encode("ascii")
                            replacement = b'"workflow_run_id":"99999"'
                            if len(original) != len(replacement) or original not in content:
                                raise AssertionError("receipt mutation fixture did not match expected binding")
                            mutated = content.replace(original, replacement, 1)
                        if len(mutated) != len(content):
                            raise AssertionError("receipt mutation fixture changed content size")
                        os.pwrite(target_fd, mutated, 0)
                    finally:
                        os.close(target_fd)
        return os.fsync(descriptor)

    def read(self, descriptor, size):
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"):
            if self.fault == "final_read_error":
                raise OSError("injected final receipt read failure")
            if self.fault == "final_read_truncation":
                if descriptor not in self.short_read_fds:
                    self.short_read_fds.add(descriptor)
                    return os.read(descriptor, max(1, size // 2))
                return b""
        return os.read(descriptor, size)

    def write(self, descriptor, payload):
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if self.fault == "diagnostic_write" and target.endswith(f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json"):
            raise OSError("injected diagnostic write failure")
        if self.fault == "pending_write" and target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json"):
            raise OSError("injected pending projection write failure")
        if self.fault == "pending_short_write" and target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json"):
            return 0
        if self.fault == "final_write" and target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"):
            raise OSError("injected final receipt write failure")
        if self.fault == "final_short_write" and target.endswith(f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"):
            return 0
        return os.write(descriptor, payload)

    def unlink(self, path, *args, **kwargs):
        if self.fault == "pending_cleanup" and str(path).endswith(".pending.json"):
            raise OSError("injected pending cleanup failure")
        return os.unlink(path, *args, **kwargs)


class BackupProbeWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        handler = _heredoc_after(cls.source, 'if probe_marker="$(/usr/bin/python3 -I - "$backup_report_file"')
        cls.handler = {"__name__": "probe_contract"}
        exec(compile(handler, str(WORKFLOW), "exec"), cls.handler)
        marker_parser = _heredoc_after(
            cls.source,
            'success_marker="$(/usr/bin/python3 -I - "$remote_report" "$GITHUB_RUN_ID"',
        )
        cls.marker_parser = {"__name__": "probe_marker_contract"}
        exec(compile(marker_parser, str(WORKFLOW), "exec"), cls.marker_parser)
        legacy_handler = _heredoc_with_suffix_after(cls.source, "backup_failure_marker() {")
        legacy_tree = ast.parse(legacy_handler)
        cls.backup_first_fault_sites = _literal_set_assignment(legacy_tree, "first_fault_sites")
        cls.backup_first_fault_kinds = _literal_set_assignment(legacy_tree, "first_fault_kinds")
        cls.backup_host_guard_sites = _literal_set_assignment(legacy_tree, "host_guard_sites")
        resource_validator = next(
            (
                node
                for node in ast.walk(legacy_tree)
                if isinstance(node, ast.FunctionDef) and node.name == "valid_resource_attribution"
            ),
            None,
        )
        if resource_validator is None:
            raise AssertionError("could not find the closed private resource-attribution validator")
        cls.resource_validator = compile(
            ast.fix_missing_locations(ast.Module(body=[resource_validator], type_ignores=[])),
            str(WORKFLOW),
            "exec",
        )
        phase_validator = next(
            (
                node
                for node in ast.walk(legacy_tree)
                if isinstance(node, ast.FunctionDef) and node.name == "valid_phase_attribution"
            ),
            None,
        )
        if phase_validator is None:
            raise AssertionError("could not find the closed private phase-attribution validator")
        cls.phase_validator = compile(
            ast.fix_missing_locations(ast.Module(body=[phase_validator], type_ignores=[])),
            str(WORKFLOW),
            "exec",
        )
        allocation_validator = next(
            (
                node
                for node in ast.walk(legacy_tree)
                if isinstance(node, ast.FunctionDef) and node.name == "valid_scratch_allocation_attribution"
            ),
            None,
        )
        if allocation_validator is None:
            raise AssertionError("could not find the closed private scratch-allocation attribution validator")
        cls.allocation_validator = compile(
            ast.fix_missing_locations(ast.Module(body=[allocation_validator], type_ignores=[])),
            str(WORKFLOW),
            "exec",
        )
        allocation_parser = next(
            (
                node
                for node in ast.walk(legacy_tree)
                if isinstance(node, ast.FunctionDef) and node.name == "parse_bound_scratch_allocation_attribution"
            ),
            None,
        )
        if allocation_parser is None:
            raise AssertionError("could not find the private scratch-allocation frame binder")
        cls.allocation_parser = compile(
            ast.fix_missing_locations(ast.Module(body=[allocation_parser], type_ignores=[])),
            str(WORKFLOW),
            "exec",
        )
        probe_tree = ast.parse(handler)
        cls.probe_first_fault_sites = _literal_set_assignment(probe_tree, "sites")
        cls.probe_host_guard_sites = _literal_set_assignment(probe_tree, "host_guard_sites")
        validator_function = next(
            (
                node
                for node in ast.walk(legacy_tree)
                if isinstance(node, ast.FunctionDef) and node.name == "valid_first_fault"
            ),
            None,
        )
        if validator_function is None:
            raise AssertionError("could not isolate the exact backup first-fault validator")
        validator_module = ast.Module(
            body=[validator_function], type_ignores=[]
        )
        cls.legacy_fault_validator = compile(ast.fix_missing_locations(validator_module), str(WORKFLOW), "exec")

    def _legacy_first_fault_valid(self, value) -> bool:
        namespace = {
            "first_fault_sites": self.backup_first_fault_sites,
            "first_fault_kinds": self.backup_first_fault_kinds,
        }
        exec(self.legacy_fault_validator, namespace)
        return namespace["valid_first_fault"](value)

    def _workflow_resource_attribution_valid(self, value) -> bool:
        namespace = {}
        exec(self.resource_validator, namespace)
        return namespace["valid_resource_attribution"](value)

    def _workflow_phase_attribution_valid(self, value) -> bool:
        namespace = {}
        exec(self.phase_validator, namespace)
        return namespace["valid_phase_attribution"](value)

    def _workflow_allocation_attribution_valid(self, value) -> bool:
        namespace = {"re": re, "json": json}
        exec(self.allocation_validator, namespace)
        return namespace["valid_scratch_allocation_attribution"](value)

    def _workflow_allocation_binding(self, report, stderr_state, stderr_bytes):
        namespace = {"re": re, "json": json}
        exec(self.allocation_validator, namespace)
        exec(self.allocation_parser, namespace)
        return namespace["parse_bound_scratch_allocation_attribution"](
            report, stderr_state, stderr_bytes
        )

    @staticmethod
    def _complete_allocation_sample(phase: str) -> dict:
        return {
            "schema": 1,
            "phase": phase,
            "status": "complete",
            "unknown_reason": None,
            "sample_unix_ns": 1_760_000_000_000_000_000,
            "sample_offset_ms": 500,
            "guard_available_bytes": None,
            "guard_sample_lag_ms": None,
            "mount": {
                "dev": 2049,
                "ino": 123,
                "mount_id": 55,
                "total_bytes": 1_073_741_824,
                "used_bytes": 500_000_000,
                "free_bytes": 573_741_824,
            },
            "host": {
                "mem_available_bytes": 2_000_000_000,
                "rss_bytes": 50_000_000,
                "memory_current_bytes": 60_000_000,
                "anon_bytes": 45_000_000,
                "file_bytes": 10_000_000,
                "shmem_bytes": 5_000_000,
            },
            "tree": {
                "data": [1000, 2048, 5, 2],
                "base": [2000, 4096, 8, 3],
                "pg_wal": [3000, 4096, 9, 2],
            },
        }

    @classmethod
    def _complete_allocation_attribution(cls) -> dict:
        return {
            "schema": 1,
            "status": "complete",
            "unknown_reason": None,
            "archive_size_bytes": 123456,
            "archive_sha256": "c" * 64,
            "samples": [
                cls._complete_allocation_sample(phase)
                for phase in (
                    "initdb_end", "postgres_start_end", "restore_platform_start", "restore_platform_end"
                )
            ],
        }

    def _assert_scratch_allocation_validator_parity(self) -> None:
        complete = self._complete_allocation_attribution()
        unknown = {
            "schema": 1,
            "status": "unknown",
            "unknown_reason": "capture_prefix",
            "archive_size_bytes": 123456,
            "archive_sha256": "c" * 64,
            "samples": [],
        }
        partial_sample = self._complete_allocation_sample("initdb_end")
        partial_sample.update(status="unknown", unknown_reason="read_error")
        partial_sample["sample_offset_ms"] = None
        partial_sample["mount"]["mount_id"] = None
        partial_sample["tree"]["pg_wal"] = None
        partial = {
            **complete,
            "status": "partial",
            "unknown_reason": "read_error",
            "samples": [partial_sample],
        }
        pg_restore_failure = {
            **partial,
            "samples": [
                self._complete_allocation_sample(phase)
                for phase in ("initdb_end", "postgres_start_end", "restore_platform_start")
            ],
        }
        worker_failure = self._complete_allocation_sample("worker_failure")
        worker_failure.update(status="unknown", unknown_reason="read_error")
        parent_after_failure = self._complete_allocation_sample("parent_last_guard")
        parent_after_failure.update(status="unknown", unknown_reason="sample_unavailable")
        failure_after_restore_end = {
            **partial,
            "samples": [*complete["samples"], worker_failure, parent_after_failure],
        }

        def sample_with(path: tuple[str, ...], replacement) -> dict:
            sample = self._complete_allocation_sample("initdb_end")
            sample.update(status="unknown", unknown_reason="read_error")
            target = sample
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            if path == ("mount", "total_bytes") and replacement is None:
                sample["mount"]["used_bytes"] = None
                sample["mount"]["free_bytes"] = None
            return {**partial, "samples": [sample]}

        cases = [
            (complete, True),
            (partial, True),
            (unknown, True),
            (pg_restore_failure, True),
            (failure_after_restore_end, True),
            ({**partial, "samples": [self._complete_allocation_sample("parent_last_guard")]}, True),
            ({**complete, "samples": [{**complete["samples"][0], "sample_offset_ms": 0}, *complete["samples"][1:]]}, True),
            ({**complete, "samples": [{**complete["samples"][0], "sample_offset_ms": 960_000}, *complete["samples"][1:]]}, True),
            ({**complete, "samples": [{**complete["samples"][0], "phase": "unknown_phase"}, *complete["samples"][1:]]}, False),
            ({**complete, "samples": [{**complete["samples"][0], "sample_offset_ms": True}, *complete["samples"][1:]]}, False),
            ({**complete, "samples": [{**complete["samples"][0], "sample_offset_ms": -1}, *complete["samples"][1:]]}, False),
            ({**complete, "samples": [{**complete["samples"][0], "sample_offset_ms": 960_001}, *complete["samples"][1:]]}, False),
            ({**complete, "samples": [{**complete["samples"][0], "tree": {**complete["samples"][0]["tree"], "data": [1, 2, True, 4]}}, *complete["samples"][1:]]}, False),
            ({**complete, "samples": [{**complete["samples"][0], "mount": {"dev": 1}} , *complete["samples"][1:]]}, False),
            ({**complete, "archive_size_bytes": True}, False),
            ({**complete, "archive_sha256": "C" * 64}, False),
            ({**complete, "unknown_reason": "open-ended"}, False),
            ({**complete, "samples": [*complete["samples"], complete["samples"][0]]}, False),
            ({**complete, "samples": [*complete["samples"], self._complete_allocation_sample("worker_failure")]}, False),
        ]
        nullable_paths = (
            ("sample_offset_ms",),
            ("guard_available_bytes",),
            ("guard_sample_lag_ms",),
            ("mount", "dev"),
            ("mount", "ino"),
            ("mount", "mount_id"),
            ("mount", "total_bytes"),
            ("mount", "used_bytes"),
            ("mount", "free_bytes"),
            ("host", "mem_available_bytes"),
            ("host", "rss_bytes"),
            ("host", "memory_current_bytes"),
            ("host", "anon_bytes"),
            ("host", "file_bytes"),
            ("host", "shmem_bytes"),
            ("tree", "data"),
            ("tree", "base"),
            ("tree", "pg_wal"),
        )
        cases.extend((sample_with(path, None), True) for path in nullable_paths)
        cases.extend((sample_with(path, True), False) for path in nullable_paths)
        cases.append((sample_with(("unknown_reason",), None), False))
        for attribution, expected in cases:
            with self.subTest(attribution=attribution):
                workflow_valid = self._workflow_allocation_attribution_valid(attribution)
                helper_valid = platform_backup_restore_drill.valid_scratch_allocation_attribution(
                    attribution
                )
                self.assertEqual(workflow_valid, expected)
                self.assertEqual(helper_valid, expected)

    def _report(self, status: str = "completed", *, first_fault=None, host_guard=None, extra: dict | None = None) -> bytes:
        if status == "completed":
            report = {
                "schema": 1,
                "kind": "authorization_probe",
                "status": "completed",
                "authorization_probe": {"schema": 1, "status": "completed"},
            }
        else:
            report = {
                "schema": 1,
                "kind": "authorization_probe",
                "status": "failed",
                "cleanup_status": "confirmed",
            }
            if first_fault is not None:
                report["first_fault"] = first_fault
            if host_guard is not None:
                report["scratch_host_guard_diagnostic"] = host_guard
        if extra:
            report.update(extra)
        return (json.dumps(report, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _frame(value=FAULT, *, canonical=True) -> bytes:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if not canonical:
            encoded = json.dumps(value, sort_keys=True)
        return b"SCRATCH_FIRST_FAULT_V1\n" + encoded.encode("ascii") + b"\n"

    @staticmethod
    def _host_guard_frame(value=HOST_GUARD) -> bytes:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return b"OLDS_PG_SCRATCH_HOST_GUARD_V1:" + encoded.encode("ascii") + b"\n"

    def _fixture(self, *, status="completed", first_fault=None, host_guard=None, fault_os=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        temporary = root / "tmp"
        temporary.mkdir(mode=0o1777)
        temporary.chmod(0o1777)
        shared = root / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        report = temporary / "oldsparky-production-backup-report.ABC123"
        capture = temporary / f"oldsparky-production-backup-{RUN_ID}-{RUN_ATTEMPT}.stderr"
        report.write_bytes(self._report(status, first_fault=first_fault, host_guard=host_guard))
        capture.write_bytes(
            (self._host_guard_frame(host_guard) if host_guard is not None else b"")
            + (self._frame(first_fault) if first_fault is not None else b"")
        )
        report.chmod(0o600)
        capture.chmod(0o600)
        self.handler["os"] = fault_os or _FaultOS()
        return temp, temporary, shared, report, capture

    def _process(self, paths, *, status="completed", child_status=None, bindings=None):
        temp, temporary, shared, report, capture = paths
        values = bindings or (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA)
        if child_status is None:
            child_status = "0" if status == "completed" else "1"
        return self.handler["process_probe"](
            str(report), str(capture), child_status, str(shared), *values, str(temporary)
        )

    def _dispatch_with_probe_marker(self, marker: str):
        expected = (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA)
        try:
            self.marker_parser["validate_probe_success_marker"](marker.encode("ascii") + b"\n", expected)
        except ValueError:
            accepted = False
        else:
            accepted = True
        guard_start = self.source.index(
            'if [[ "$AUTHORIZATION_PROBE_BEFORE_CREATE" = true && "$BACKUP_OPERATION" != create ]]; then'
        )
        guard_end = self.source.index("\n          fi", guard_start) + len("\n          fi")
        dispatch_start = self.source.index(
            'if [[ "$BACKUP_OPERATION" = create && "$AUTHORIZATION_PROBE_BEFORE_CREATE" = true ]]; then',
            guard_end,
        )
        dispatch_end = self.source.index("\n          fi", dispatch_start) + len("\n          fi")
        script = "\n".join((
            "set -euo pipefail",
            textwrap.dedent(self.source[guard_start:guard_end]),
            "run_remote_operation() {",
            "  printf '%s %s\\n' \"$1\" \"$2\"",
            "  if [[ \"$1\" = authorization-probe && \"$PROBE_ACCEPTED\" != true ]]; then return 1; fi",
            "}",
            textwrap.dedent(self.source[dispatch_start:dispatch_end]),
        ))
        environment = os.environ.copy()
        environment.update({
            "BACKUP_OPERATION": "create",
            "AUTHORIZATION_PROBE_BEFORE_CREATE": "true",
            "PROBE_ACCEPTED": "true" if accepted else "false",
        })
        dispatched = subprocess.run(
            ["bash", "-c", script],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        return accepted, dispatched

    def test_probe_is_opt_in_and_uses_exact_source_helper_gates(self) -> None:
        operation_shell = _workflow_run_scalar(self.source, "Create/verify backup or run authorization probe")
        shell_sources = [operation_shell, *_heredocs(operation_shell, "REMOTE")]
        for source in shell_sources:
            syntax = subprocess.run(
                ["bash", "-n"], input=source, check=False, capture_output=True, text=True
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn("- authorization-probe", self.source)
        self.assertIn(
            "inputs.operation == 'create' || inputs.operation == 'verify-existing' || inputs.operation == 'authorization-probe'",
            self.source,
        )
        self.assertIn('[[ "$operation" = create || "$operation" = verify-existing || "$operation" = authorization-probe ]]', self.source)
        self.assertIn('command.insert(-3, "--authorization-probe-only")', self.source)
        self.assertIn('"--private-backup-diagnostics"', self.source)
        self.assertIn('probe_custody_status=unavailable', self.source)
        self.assertIn('"$operation" != authorization-probe || "$probe_custody_status" = committed', self.source)
        self.assertIn("authorization_probe_before_create:", self.source)
        self.assertIn("type: boolean\n        default: false", self.source)
        self.assertIn('group: platform-production-deploy\n  cancel-in-progress: false', self.source)

        gate_start = self.source.index(
            'if [[ "$AUTHORIZATION_PROBE_BEFORE_CREATE" = true && "$BACKUP_OPERATION" != create ]]; then'
        )
        gate_end = self.source.index("\n          fi", gate_start) + len("\n          fi")
        policy_guard = textwrap.dedent(self.source[gate_start:gate_end])
        dispatch_start = self.source.index(
            'if [[ "$BACKUP_OPERATION" = create && "$AUTHORIZATION_PROBE_BEFORE_CREATE" = true ]]; then',
            gate_end,
        )
        dispatch_end = self.source.index("\n          fi", dispatch_start) + len("\n          fi")
        dispatch = textwrap.dedent(self.source[dispatch_start:dispatch_end])
        self.assertLess(dispatch.index("run_remote_operation authorization-probe true"), dispatch.index("run_remote_operation create false"))
        self.assertIn('"$REMOTE_OPERATION" = authorization-probe', self.source)
        self.assertLess(
            self.source.index("validate_probe_success_marker(raw, expected)"),
            self.source.index("return 0", self.source.index("validate_probe_success_marker(raw, expected)")),
        )

        security_gate = self.source.index('run.get("conclusion") != "success"')
        ssh_setup_step = self.source.index("- name: Configure SSH")
        stage_step = self.source.index("- name: Stage the verified helper bundle on the production host")
        operation_step = self.source.index("- name: Create/verify backup or run authorization probe")
        self.assertLess(security_gate, ssh_setup_step)
        self.assertLess(ssh_setup_step, stage_step)
        self.assertLess(security_gate, stage_step)
        self.assertLess(stage_step, operation_step)
        self.assertIn('if [[ "$poll_state" = success ]]; then', self.source)
        self.assertIn('run.get("status") != "completed"', self.source)
        self.assertIn('run.get("conclusion") != "success"', self.source)
        wrapper_start = self.source.index("run_remote_operation() {")
        ssh_invocation = self.source.index("if ssh \\", wrapper_start)
        self.assertLess(self.source.index("verify_current_dev_source", wrapper_start), ssh_invocation)
        self.assertGreater(self.source.index('"bash -s -- $remote_arguments"', ssh_invocation), ssh_invocation)
        remote_arguments_assignment = next(
            line.strip()
            for line in self.source.splitlines()
            if line.strip().startswith('remote_arguments="$GITHUB_RUN_ID')
        )
        self.assertIn('[[ "$#" = 15 ]]', self.source)
        self.assertIn('security_run_id="${12:-}"', self.source)
        self.assertIn('security_run_attempt="${13:-}"', self.source)
        self.assertIn('retain_helper_stage="${14:-false}"', self.source)
        self.assertIn('scratch_backend="${15:-}"', self.source)
        self.assertIn('root / "bundle.zip"', self.source)
        self.assertIn('root / "tools"', self.source)

        def exercise_dispatch(
            operation: str,
            before_create: bool,
            probe_accepted: bool = True,
            scratch_backend: str = "tmpfs_v1",
        ):
            script = "\n".join((
                "set -euo pipefail",
                policy_guard,
                "run_remote_operation() {",
                "  local REMOTE_OPERATION=\"$1\"",
                "  local retain_helper_stage=\"$2\"",
                "  local scratch_backend=tmpfs_v1",
                "  if [[ \"$REMOTE_OPERATION\" = create ]]; then scratch_backend=\"$SCRATCH_BACKEND\"; fi",
                f"  {remote_arguments_assignment}",
                "  local -a transported_args",
                "  read -r -a transported_args <<<\"$remote_arguments\"",
                "  [[ \"${#transported_args[@]}\" = 15 ]]",
                "  printf '%s\\n' \"${transported_args[@]}\"",
                "  if [[ \"$1\" = authorization-probe && \"$PROBE_ACCEPTED\" != true ]]; then return 1; fi",
                "}",
                dispatch,
            ))
            environment = os.environ.copy()
            environment.update({
                "BACKUP_OPERATION": operation,
                "AUTHORIZATION_PROBE_BEFORE_CREATE": "true" if before_create else "false",
                "PROBE_ACCEPTED": "true" if probe_accepted else "false",
                "GITHUB_RUN_ID": RUN_ID,
                "GITHUB_RUN_ATTEMPT": RUN_ATTEMPT,
                "SOURCE_SHA": SOURCE_SHA,
                "BUNDLE_SHA256": BUNDLE_SHA,
                "EVICT_BUILD_NODE_CACHE": "false",
                "COMPACT_FALLBACK_RUNTIME_CACHE": "false",
                "RESUME_FALLBACK_CACHE_COMPACTION": "false",
                "RESUME_COMPACTION_RUN_ID": "none",
                "RESUME_COMPACTION_RUN_ATTEMPT": "none",
                "RESUME_COMPACTION_BUNDLE_SHA256": "none",
                "SECURITY_RUN_ID": SECURITY_ID,
                "SECURITY_RUN_ATTEMPT": SECURITY_ATTEMPT,
                "SCRATCH_BACKEND": scratch_backend,
            })
            return subprocess.run(
                ["bash", "-c", script],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        def expected_transport(operation: str, retain: str, backend: str) -> str:
            return "\n".join((
                RUN_ID, RUN_ATTEMPT, operation, SOURCE_SHA, BUNDLE_SHA,
                "false", "false", "false", "none", "none", "none",
                SECURITY_ID, SECURITY_ATTEMPT, retain,
                backend,
            )) + "\n"

        default_create = exercise_dispatch("create", False)
        self.assertEqual(
            (default_create.returncode, default_create.stdout),
            (0, expected_transport("create", "false", "tmpfs_v1")),
        )
        disk_create = exercise_dispatch(
            "create", False, scratch_backend="disk_ext4_image_v1"
        )
        self.assertEqual(
            (disk_create.returncode, disk_create.stdout),
            (0, expected_transport("create", "false", "disk_ext4_image_v1")),
        )
        standalone_probe = exercise_dispatch("authorization-probe", False)
        self.assertEqual(
            (standalone_probe.returncode, standalone_probe.stdout),
            (0, expected_transport("authorization-probe", "false", "tmpfs_v1")),
        )
        rejected_operation = exercise_dispatch("verify-existing", True)
        self.assertNotEqual(rejected_operation.returncode, 0)
        self.assertEqual(rejected_operation.stdout, "")

        success_marker = (
            f"AUTHORIZATION_PROBE_RESULT schema=1 run_id={RUN_ID} attempt={RUN_ATTEMPT} "
            f"security_run_id={SECURITY_ID} security_run_attempt={SECURITY_ATTEMPT} source_sha={SOURCE_SHA} "
            f"bundle_sha256={BUNDLE_SHA} status=completed cleanup_status=confirmed receipt=written custody=committed\n"
        ).encode("ascii")
        self.marker_parser["validate_probe_success_marker"](
            success_marker,
            (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA),
        )
        expected_binding = (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA)

        def marker_is_accepted(raw: bytes) -> bool:
            try:
                self.marker_parser["validate_probe_success_marker"](raw, expected_binding)
            except ValueError:
                return False
            return True

        accepted_create = exercise_dispatch("create", True, probe_accepted=marker_is_accepted(success_marker))
        self.assertEqual(
            (accepted_create.returncode, accepted_create.stdout),
            (
                0,
                expected_transport("authorization-probe", "true", "tmpfs_v1")
                + expected_transport("create", "false", "tmpfs_v1"),
            ),
        )
        rejected_markers = (
            ("failed", success_marker.replace(b"status=completed", b"status=failed")),
            ("unknown", success_marker.replace(b"status=completed", b"status=unknown")),
            ("cleanup_incomplete", success_marker.replace(b"cleanup_status=confirmed", b"cleanup_status=unknown")),
            ("missing_receipt", success_marker.replace(b"receipt=written", b"receipt=unavailable")),
            ("uncommitted_receipt", success_marker.replace(b"custody=committed", b"custody=pending")),
            ("missing_marker", b""),
            ("mismatched_binding", success_marker.replace(f"run_id={RUN_ID}".encode(), b"run_id=99999")),
            ("receipt_collision", success_marker.replace(b"receipt=written", b"receipt=unavailable")),
        )
        for failure_case, invalid_marker in rejected_markers:
            with self.subTest(probe_before_create=failure_case):
                accepted = marker_is_accepted(invalid_marker)
                self.assertFalse(accepted)
                rejected_create = exercise_dispatch("create", True, probe_accepted=accepted)
                self.assertNotEqual(rejected_create.returncode, 0)
                self.assertEqual(
                    rejected_create.stdout,
                    expected_transport("authorization-probe", "true", "tmpfs_v1"),
                )

        self.assertIn('if: ${{ inputs.operation == \'create\' || inputs.operation == \'verify-existing\' || inputs.operation == \'authorization-probe\' }}', self.source)
        self.assertIn('if: ${{ success() && inputs.operation == \'create\' && steps.cleanup_ssh.outcome == \'success\' }}', self.source)
        self.assertNotIn("platform-authorization-probe-success", self.source)
        autodeploy = (REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml").read_text(encoding="utf-8")
        self.assertIn(
            "group: platform-production-auto-deploy-${{ github.event.workflow_run.id }}-${{ github.event.workflow_run.run_attempt }}",
            autodeploy,
        )
        self.assertIn("cancel-in-progress: false", autodeploy)
        backup_guard = autodeploy.index("- name: Verify exact source-bound restore-verified backup")
        child_dispatch = autodeploy.index("- name: Dispatch production deployment or baseline reconciliation")
        self.assertLess(backup_guard, child_dispatch)
        self.assertIn("SOURCE_SECURITY_RUN_ID: ${{ github.event.workflow_run.id }}", autodeploy)
        self.assertIn("SOURCE_SECURITY_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}", autodeploy)
        self.assertIn("BACKUP_GUARD_TOOL: ${{ github.workspace }}/platform/tools/platform_backup_success_guard.py", autodeploy)
        self.assertIn("github.event.workflow_run.status == 'completed'", autodeploy)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", autodeploy)
        self.assertIn("host-readiness:\n    name: Wait for exact host-tools generation readiness", autodeploy)
        readiness_start = autodeploy.index("  host-readiness:")
        readiness_end = autodeploy.index("  dispatch-production:", readiness_start)
        readiness_job = autodeploy[readiness_start:readiness_end]
        self.assertIn("environment: production", readiness_job)
        self.assertIn("needs.dispatch.outputs.deploy == 'true'", readiness_job)
        self.assertIn("platform_host_tools_installed_check.py", readiness_job)
        self.assertIn("--wait-for-generation-seconds 1800", readiness_job)
        self.assertIn("if: ${{ always() }}", readiness_job)
        self.assertIn('rm -rf -- "$RUNNER_TEMP/host-tools-bootstrap" "$RUNNER_TEMP/platform-host-capability-ssh"', readiness_job)
        final_dispatch = autodeploy[readiness_end:]
        self.assertIn("needs.host-readiness.result == 'success'", final_dispatch)
        self.assertIn("Revalidate current dev and exact successful Security run", final_dispatch)
        self.assertIn("Revalidate exact successful source-bound backup", final_dispatch)
        self.assertIn("platform-production-deploy.yml/dispatches", final_dispatch)
        self.assertNotIn("workflow_run:\n    workflows:\n      - Platform production auto-deploy", autodeploy)

    def test_probe_skips_database_env_and_backup_summary_paths(self) -> None:
        self.assertIn('if [[ "$operation" != authorization-probe ]]; then\n            test -f "$runtime/shared/.env.platform"', self.source)
        self.assertIn('if [[ "$operation" != authorization-probe ]]; then\n            test -f "$summary_tool"', self.source)
        probe_start = self.source.index('if [[ "$operation" = authorization-probe ]]; then\n            failure_stage=authorization_probe_receipt')
        probe_end = self.source.index('if (( operation_status != 0 )); then', probe_start)
        probe_branch = self.source[probe_start:probe_end]
        self.assertNotIn("--backup-only", probe_branch)
        self.assertNotIn("--verify-existing-backup-only", probe_branch)
        self.assertNotIn("--archive", probe_branch)
        self.assertNotIn(".env.platform", probe_branch)
        self.assertNotIn("platform_storage_evidence_summary.py", probe_branch)

    def test_probe_envelopes_require_verified_cleanup_and_reject_extra_fields(self) -> None:
        parse = self.handler["parse_probe_report"]
        resource_attribution = {
            "schema": 1,
            "status": "complete",
            "sample_offset_ms": 1200,
            "unknown_reason": None,
            "controller": {"identity_bound": True, "rss_bytes": 40_000_000},
            "worker": {"identity_bound": True, "rss_bytes": 452_661_248},
            "cgroup": {
                "identity_bound": True,
                "memory_current_bytes": 331_210_752,
                "anon_bytes": 280_000_000,
                "file_bytes": 40_000_000,
                "shmem_bytes": 11_210_752,
                "events": {"high": 0, "max": 0, "oom": 0, "oom_kill": 0},
            },
        }
        self.assertTrue(self._workflow_resource_attribution_valid(resource_attribution))
        for malformed_attribution in (
            {**resource_attribution, "schema": True},
            {**resource_attribution, "worker": {"identity_bound": False, "rss_bytes": 1}},
            {**resource_attribution, "unexpected": 1},
            {
                **resource_attribution,
                "cgroup": {**resource_attribution["cgroup"], "memory_current_bytes": True},
            },
            {**resource_attribution, "status": "unknown", "unknown_reason": "not-closed"},
        ):
            with self.subTest(resource_attribution=malformed_attribution):
                self.assertFalse(self._workflow_resource_attribution_valid(malformed_attribution))
        unknown_attribution = {
            "schema": 1,
            "status": "unknown",
            "sample_offset_ms": None,
            "unknown_reason": "sample_unavailable",
            "controller": {"identity_bound": False, "rss_bytes": None},
            "worker": {"identity_bound": False, "rss_bytes": None},
            "cgroup": {
                "identity_bound": False,
                "memory_current_bytes": None,
                "anon_bytes": None,
                "file_bytes": None,
                "shmem_bytes": None,
                "events": {"high": None, "max": None, "oom": None, "oom_kill": None},
            },
        }
        self.assertTrue(self._workflow_resource_attribution_valid(unknown_attribution))
        self.assertEqual(parse(self._report(), b"", "0"), ("completed", "confirmed", None, None))
        failed = self._report("failed", first_fault=FAULT)
        self.assertEqual(parse(failed, self._frame(), "1"), ("failed", "confirmed", FAULT, None))
        guarded_fault = dict(FAULT, origin="controller", site="controller_host_guard")
        guarded = self._report("failed", first_fault=guarded_fault, host_guard=HOST_GUARD)
        self.assertEqual(
            parse(guarded, self._host_guard_frame() + self._frame(guarded_fault), "1"),
            ("failed", "confirmed", guarded_fault, HOST_GUARD),
        )
        invalid_guard_values = (
            dict(HOST_GUARD, schema=True),
            dict(HOST_GUARD, schema=2.0),
            dict(HOST_GUARD, available_bytes=True),
            dict(HOST_GUARD, available_bytes=900_000_000.0),
            dict(HOST_GUARD, required_bytes=1_073_741_823),
            dict(HOST_GUARD, raw="forbidden"),
        )
        for invalid_guard in invalid_guard_values:
            with self.subTest(host_guard=invalid_guard):
                raw = self._report("failed", first_fault=guarded_fault, host_guard=invalid_guard)
                with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
                    parse(raw, self._host_guard_frame(invalid_guard) + self._frame(guarded_fault), "1")
        with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
            parse(guarded, self._frame(guarded_fault), "1")
        with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
            parse(
                guarded,
                self._host_guard_frame() + self._host_guard_frame() + self._frame(guarded_fault),
                "1",
            )
        with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
            parse(guarded, self._host_guard_frame() + self._frame(), "1")
        mismatched_guard = dict(HOST_GUARD, available_bytes=800_000_000)
        with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
            parse(
                guarded,
                self._host_guard_frame(mismatched_guard) + self._frame(guarded_fault),
                "1",
            )
        controller_fault = {
            "schema": 1,
            "origin": "controller",
            "site": "controller_entry",
            "exception_kind": "permission",
            "errno": 13,
        }
        controller_report = self._report("failed", first_fault=controller_fault)
        self.assertEqual(
            parse(controller_report, self._frame(controller_fault), "1"),
            ("failed", "confirmed", controller_fault, None),
        )
        wrong_pair = dict(FAULT, origin="controller")
        with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
            parse(self._report("failed", first_fault=wrong_pair), self._frame(wrong_pair), "1")
        for field, invalid_value in (
            ("schema", True),
            ("schema", 1.0),
            ("errno", True),
            ("errno", 13.0),
            ("exception_kind", 7),
        ):
            malformed_fault = dict(FAULT, **{field: invalid_value})
            with self.subTest(first_fault_field=field, invalid_value=invalid_value):
                with self.assertRaises((ValueError, json.JSONDecodeError, TypeError)):
                    parse(
                        self._report("failed", first_fault=malformed_fault),
                        self._frame(malformed_fault),
                        "1",
                    )
        invalid_reports = (
            self._report(extra={"ok": True}),
            self._report(extra={"error": "text"}),
            self._report(extra={"backup": {}}),
            self._report(extra={"scratch_host_guard_diagnostic": HOST_GUARD}),
            self._report("failed", extra={"unexpected": 1}),
            self._report(extra={"schema": True}),
            self._report(extra={"schema": 1.0}),
            json.dumps({
                "schema": 1,
                "kind": "authorization_probe",
                "status": "completed",
                "authorization_probe": {"schema": True, "status": "completed"},
            }, separators=(",", ":")).encode("ascii"),
            json.dumps({
                "schema": 1,
                "kind": "authorization_probe",
                "status": "completed",
                "authorization_probe": {"schema": 1.0, "status": "completed"},
            }, separators=(",", ":")).encode("ascii"),
            b'{"schema":1,"schema":1,"kind":"authorization_probe","status":"completed","authorization_probe":{"schema":1,"status":"completed"}}',
        )
        for raw in invalid_reports:
            with self.subTest(report=raw[:48]):
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    parse(raw, b"", "0")
        for child_status in ("1", "2", "not-an-exit", "01", "256"):
            with self.subTest(child_status=child_status):
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    parse(self._report(), b"", child_status)
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(self._report("failed"), b"", "0")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(failed, self._frame(FAULT, canonical=False), "1")
        mismatched_fault = dict(FAULT, errno=1)
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(failed, self._frame(mismatched_fault), "1")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(failed, self._frame()[:-1], "1")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(failed, self._frame() + self._frame(), "1")
        extra_fault = dict(FAULT, raw="must-not-pass")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(self._report("failed", first_fault=extra_fault), self._frame(extra_fault), "1")
        duplicate_fault = b'{"schema":1,"origin":"worker","site":"worker_role_setup","exception_kind":"permission","errno":13,"errno":13}'
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            parse(self._report("failed", first_fault=FAULT), b"SCRATCH_FIRST_FAULT_V1\n" + duplicate_fault + b"\n", "1")

    def test_private_receipt_is_bound_no_overwrite_and_never_uploaded(self) -> None:
        paths = self._fixture()
        try:
            status, marker = self._process(paths)
            self.assertEqual(status, 0)
            self.assertIn("status=completed cleanup_status=confirmed receipt=written", marker)
            self.assertIn("custody=committed", marker)
            shared = paths[2]
            receipt = json.loads((shared / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json").read_text())
            self.assertEqual(receipt, {
                "schema": 1,
                "kind": "authorization_probe_receipt",
                "status": "completed",
                "workflow_run_id": RUN_ID,
                "workflow_run_attempt": RUN_ATTEMPT,
                "security_run_id": SECURITY_ID,
                "security_run_attempt": SECURITY_ATTEMPT,
                "source_sha": SOURCE_SHA,
                "helper_bundle_sha256": BUNDLE_SHA,
                "cleanup_status": "confirmed",
            })
            self.assertFalse((shared / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json").exists())
            self.assertFalse(
                (shared / f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json").exists()
            )
            self.assertFalse(paths[3].exists())
            self.assertFalse(paths[4].exists())

            expected = (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA)
            encoded_marker = (marker + "\n").encode("ascii")
            self.assertEqual(
                self.marker_parser["validate_probe_success_marker"](encoded_marker, expected),
                marker + "\n",
            )
            for index, replacement in (
                (0, "99999"), (1, "3"), (2, "99998"), (3, "2"),
                (4, "c" * 40), (5, "d" * 64),
            ):
                mismatched = list(expected)
                mismatched[index] = replacement
                with self.subTest(binding_index=index):
                    with self.assertRaises(ValueError):
                        self.marker_parser["validate_probe_success_marker"](encoded_marker, tuple(mismatched))
            with self.assertRaises(ValueError):
                self.marker_parser["validate_probe_success_marker"](encoded_marker + b"extra", expected)
            for encoded_attempt in (b"attempt=true", b"attempt=2.0"):
                malformed_marker = encoded_marker.replace(b"attempt=2 ", encoded_attempt + b" ")
                with self.subTest(encoded_attempt=encoded_attempt):
                    with self.assertRaises(ValueError):
                        self.marker_parser["validate_probe_success_marker"](malformed_marker, expected)
        finally:
            paths[0].cleanup()

        self.assertRegex(
            self.source,
            re.compile(r"Publish source-bound backup success receipt\n\s+if: \$\{\{ success\(\) && inputs\.operation == 'create'"),
        )
        upload_start = self.source.index("- name: Publish source-bound backup success receipt")
        upload_end = self.source.find("\n      - name:", upload_start)
        upload_step = self.source[upload_start:upload_end]
        self.assertIn("path: ${{ runner.temp }}/platform-backup-success-receipt.json", upload_step)
        self.assertNotIn("authorization-probe-diagnostic", upload_step)
        self.assertNotIn("platform-authorization-probe-success", self.source)
        self._assert_probe_receipt_failure_keeps_only_pending_projection()

    def _assert_scratch_allocation_attribution_is_closed_private_and_frame_bound(self) -> None:
        self._assert_scratch_allocation_validator_parity()
        complete = self._complete_allocation_attribution()
        self.assertTrue(self._workflow_allocation_attribution_valid(complete))
        unknown = {
            "schema": 1,
            "status": "unknown",
            "unknown_reason": "capture_prefix",
            "archive_size_bytes": 123456,
            "archive_sha256": "c" * 64,
            "samples": [],
        }
        self.assertTrue(self._workflow_allocation_attribution_valid(unknown))
        partial_sample = self._complete_allocation_sample("initdb_end")
        partial_sample["status"] = "unknown"
        partial_sample["unknown_reason"] = "read_error"
        partial_sample["sample_offset_ms"] = None
        partial_sample["mount"]["mount_id"] = None
        partial_sample["tree"]["pg_wal"] = None
        partial = {
            **complete,
            "status": "partial",
            "unknown_reason": "read_error",
            "samples": [partial_sample],
        }
        self.assertTrue(self._workflow_allocation_attribution_valid(partial))

        for malformed in (
            {**complete, "schema": True},
            {**complete, "archive_size_bytes": True},
            {**complete, "archive_sha256": "C" * 64},
            {**complete, "unknown_reason": "not-closed"},
            {**complete, "samples": complete["samples"] + [complete["samples"][0]]},
            {**complete, "unexpected": 1},
            {
                **complete,
                "samples": [
                    {**complete["samples"][0], "sample_offset_ms": True},
                    *complete["samples"][1:],
                ],
            },
            {
                **complete,
                "samples": [
                    {
                        **complete["samples"][0],
                        "tree": {**complete["samples"][0]["tree"], "data": [1, 2, True, 4]},
                    },
                    *complete["samples"][1:],
                ],
            },
            {**complete, "status": "unknown"},
            {**complete, "status": "partial", "unknown_reason": None},
            {**unknown, "samples": [self._complete_allocation_sample("initdb_end")]},
        ):
            with self.subTest(allocation_attribution=malformed):
                self.assertFalse(self._workflow_allocation_attribution_valid(malformed))

        marker = b"SCRATCH_ALLOCATION_ATTRIBUTION_V1:"
        canonical = json.dumps(complete, sort_keys=True, separators=(",", ":")).encode("ascii")
        bound_report = {"scratch_allocation_attribution": complete}
        self.assertEqual(
            self._workflow_allocation_binding(bound_report, "read", b"prior output\n" + marker + canonical + b"\n"),
            (True, complete),
        )
        self.assertEqual(self._workflow_allocation_binding({}, "read", b"legacy failure\n"), (True, None))
        self.assertEqual(self._workflow_allocation_binding({}, "absent", b""), (True, None))
        self.assertEqual(self._workflow_allocation_binding({}, "unsafe", b""), (False, None))
        for report, capture in (
            (bound_report, b""),
            ({}, marker + canonical + b"\n"),
            (bound_report, marker + canonical + b"\n" + marker + canonical + b"\n"),
            (bound_report, b"prefix" + marker + canonical + b"\n"),
            (bound_report, marker + json.dumps(complete, sort_keys=True).encode("ascii") + b"\n"),
            ({"scratch_allocation_attribution": {**complete, "archive_size_bytes": 7}}, marker + canonical + b"\n"),
        ):
            with self.subTest(allocation_capture=capture[:80]):
                self.assertEqual(self._workflow_allocation_binding(report, "read", capture), (False, None))

        private_record_region = self.source.split('"event": "platform_backup_failure"', 1)[-1].split(
            'encoded = (json.dumps(record', 1
        )[0]
        public_summary_region = self.source.split("run_public_summary_normalizer() {", 1)[-1].split(
            "\n          }", 1
        )[0]
        self.assertIn('record["scratch_allocation_attribution"] = scratch_allocation_attribution', private_record_region)
        self.assertNotIn("scratch_allocation_attribution", public_summary_region)
        self.assertIn('"public_summary_normalizer_diagnostic": normalizer_diagnostic', private_record_region)
        self.assertNotIn("public_summary_normalizer_diagnostic", public_summary_region)
        self.assertIn('"SCRATCH_ALLOCATION_ATTRIBUTION_V1:"', self.source)

    def test_first_fault_is_closed_frame_bound_private_data(self) -> None:
        self._assert_scratch_allocation_attribution_is_closed_private_and_frame_bound()
        phase_names = (
            "archive_verify", "initdb", "postgres_start", "restore_platform",
            "restore_public", "alembic_verify",
        )
        complete_phase = {
            "schema": 1,
            "status": "complete",
            "unknown_reason": None,
            "controller_guard_offset_ms": None,
            "controller_drain_offset_ms": None,
            "frames": [
                {
                    "ordinal": index + 1,
                    "phase": phase,
                    "event": event,
                    "worker_offset_ms": index,
                }
                for index, (phase, event) in enumerate(
                    (item for phase in phase_names for item in ((phase, "start"), (phase, "end")))
                )
            ],
        }
        self.assertTrue(self._workflow_phase_attribution_valid(complete_phase))
        prefix_phase = {
            **complete_phase,
            "status": "unknown",
            "unknown_reason": "capture_prefix",
            "controller_guard_offset_ms": 4,
            "controller_drain_offset_ms": 5,
            "frames": complete_phase["frames"][:4],
        }
        self.assertTrue(self._workflow_phase_attribution_valid(prefix_phase))
        for malformed_phase in (
            {**complete_phase, "status": "unknown"},
            {**complete_phase, "unknown_reason": "unrecognized"},
            {**prefix_phase, "controller_drain_offset_ms": 3},
            {**prefix_phase, "frames": [{**prefix_phase["frames"][0], "worker_offset_ms": True}] + prefix_phase["frames"][1:]},
            {**prefix_phase, "frames": prefix_phase["frames"] + [{"ordinal": 5, "phase": "restore_public", "event": "start", "worker_offset_ms": 2}]},
            {**complete_phase, "unexpected": True},
        ):
            with self.subTest(phase_attribution=malformed_phase):
                self.assertFalse(self._workflow_phase_attribution_valid(malformed_phase))
        private_record_region = self.source.split('"event": "platform_backup_failure"', 1)[-1].split('encoded = (json.dumps(record', 1)[0]
        public_summary_region = self.source.split('public_report="$({', 1)[-1].split("failure_stage=private_capture_cleanup", 1)[0]
        self.assertIn('"scratch_phase_attribution": scratch_phase_attribution', private_record_region)
        self.assertNotIn("scratch_phase_attribution", public_summary_region)
        self.assertIn('"scratch_cleanup_status": scratch_cleanup_status', private_record_region)
        self.assertNotIn("scratch_cleanup_status", public_summary_region)
        self.assertIn('{"confirmed", "failed", "unknown"}', self.source)

        worker_sites = frozenset({
            "worker_entry", "worker_start_token", "worker_root_identity", "worker_mount_snapshot",
            "worker_cgroup_snapshot", "worker_host_reserve", "worker_archive_open",
            "worker_directory_setup", "worker_postgres_tools", "worker_initdb",
            "worker_after_initdb_guard", "worker_role_setup", "worker_postgres_start",
            "worker_after_postgres_start_guard", "worker_pg_restore_resource_guard",
            "worker_restore", "worker_after_restore_guard", "worker_postgres_stop",
            "worker_result_frame", "worker_done_token",
        })
        controller_sites = frozenset({
            "controller_entry", "controller_archive_identity", "controller_unit_setup",
            "controller_host_guard", "controller_launch", "controller_unit_snapshot",
            "controller_identity", "controller_start_token", "controller_active_poll",
            "controller_worker_frame", "controller_completion_snapshot", "controller_done_token",
            "controller_final_snapshot", "controller_cleanup",
        })
        expected_host_guard_sites = frozenset({
            "parent_before_archive_open", "parent_before_authorization_probe",
            "parent_before_unit_launch", "parent_active_poll", "parent_after_worker_cleanup",
            "worker_after_start_before_archive_open", "worker_after_start_before_authorization_probe",
            "worker_after_initdb_before_postgres_start", "worker_after_postgres_start",
            "worker_pg_restore_resource_guard", "worker_after_restore_before_postgres_stop",
        })
        self.assertEqual(self.backup_first_fault_sites, worker_sites | controller_sites)
        self.assertEqual(self.probe_first_fault_sites, worker_sites | controller_sites)
        self.assertEqual(self.backup_host_guard_sites, expected_host_guard_sites)
        self.assertEqual(self.probe_host_guard_sites, expected_host_guard_sites)
        paths = self._fixture(status="failed", first_fault=FAULT)
        try:
            code, marker = self._process(paths, status="failed")
            self.assertEqual(code, 1)
            self.assertIn("status=failed cleanup_status=confirmed receipt=written", marker)
            receipt = json.loads((paths[2] / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json").read_text())
            diagnostic = json.loads(
                (paths[2] / f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json").read_text()
            )
            self.assertEqual(receipt["first_fault"], FAULT)
            self.assertIsNone(receipt.get("scratch_host_guard_diagnostic"))
            self.assertEqual(set(receipt), {
                "schema", "kind", "status", "workflow_run_id", "workflow_run_attempt",
                "security_run_id", "security_run_attempt", "source_sha", "helper_bundle_sha256",
                "cleanup_status", "first_fault",
            })
            self.assertEqual(diagnostic, {
                "schema": 1,
                "kind": "authorization_probe_diagnostic",
                "status": "failed",
                "workflow_run_id": RUN_ID,
                "workflow_run_attempt": RUN_ATTEMPT,
                "security_run_id": SECURITY_ID,
                "security_run_attempt": SECURITY_ATTEMPT,
                "source_sha": SOURCE_SHA,
                "helper_bundle_sha256": BUNDLE_SHA,
                "stage": "validated_failure",
                "cleanup_status": "confirmed",
                "probe_operation_exit_code": 1,
                "helper_exit_code": None,
                "ssh_exit_code": None,
                "first_fault": FAULT,
                "scratch_host_guard_diagnostic": None,
            })
            self.assertFalse(any(field in receipt for field in ("stderr", "message", "path", "argv", "env", "pid")))
            self.assertFalse(any(field in diagnostic for field in ("stderr", "message", "path", "argv", "env", "pid")))
        finally:
            paths[0].cleanup()

        controller_fault = dict(FAULT, origin="controller", site="controller_host_guard")
        guarded_paths = self._fixture(
            status="failed", first_fault=controller_fault, host_guard=HOST_GUARD
        )
        try:
            code, marker = self._process(guarded_paths, status="failed")
            self.assertEqual(code, 1)
            self.assertIn("status=failed cleanup_status=confirmed", marker)
            diagnostic = json.loads(
                (guarded_paths[2] / f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json").read_text()
            )
            self.assertEqual(diagnostic["first_fault"], controller_fault)
            self.assertEqual(diagnostic["scratch_host_guard_diagnostic"], HOST_GUARD)
            authorization_receipt = json.loads(
                (guarded_paths[2] / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json").read_text()
            )
            self.assertEqual(authorization_receipt["first_fault"], controller_fault)
            self.assertNotIn("scratch_host_guard_diagnostic", authorization_receipt)
        finally:
            guarded_paths[0].cleanup()

        # A rejected frame leaves no typed diagnostic projection and keeps the
        # original private captures for triage; the durable record says UNKNOWN.
        invalid_paths = self._fixture(status="failed", first_fault=FAULT)
        invalid_paths[4].write_bytes(self._host_guard_frame(HOST_GUARD))
        invalid_paths[4].chmod(0o600)
        try:
            code, marker = self._process(invalid_paths, status="failed")
            self.assertEqual(code, 1)
            self.assertIn("status=unknown", marker)
            diagnostic = json.loads(
                (invalid_paths[2] / f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json").read_text()
            )
            self.assertEqual(diagnostic["status"], "unknown")
            self.assertEqual(diagnostic["stage"], "report_frame_validation")
            self.assertIsNone(diagnostic["first_fault"])
            self.assertIsNone(diagnostic["scratch_host_guard_diagnostic"])
            self.assertTrue(invalid_paths[3].exists())
            self.assertTrue(invalid_paths[4].exists())
            self.assertFalse((invalid_paths[2] / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json").exists())
        finally:
            invalid_paths[0].cleanup()

        for fault in ("diagnostic_collision", "diagnostic_write", "diagnostic_fsync"):
            with self.subTest(diagnostic_failure=fault):
                paths = self._fixture(status="failed", first_fault=FAULT, fault_os=_FaultOS(fault))
                diag_path = paths[2] / f"authorization-probe-diagnostic-{RUN_ID}-{RUN_ATTEMPT}.json"
                if fault == "diagnostic_collision":
                    diag_path.write_text("preserve-existing", encoding="ascii")
                try:
                    code, marker = self._process(paths, status="failed")
                    self.assertEqual(code, 1)
                    self.assertIn("status=unknown", marker)
                    self.assertTrue(paths[3].exists())
                    self.assertTrue(paths[4].exists())
                    self.assertFalse((paths[2] / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json").exists())
                    self.assertFalse((paths[2] / f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json").exists())
                    if fault == "diagnostic_collision":
                        self.assertEqual(diag_path.read_text(encoding="ascii"), "preserve-existing")
                finally:
                    paths[0].cleanup()

        public_summary = self.source.split('public_report="$({', 1)[-1].split("failure_stage=private_capture_cleanup", 1)[0]
        self.assertNotIn("first_fault", public_summary)
        self.assertNotIn("scratch_host_guard_diagnostic", public_summary)
        self.assertNotIn("scratch_resource_attribution", public_summary)
        self.assertNotIn("authorization_probe_diagnostic", public_summary)
        self.assertNotIn("SCRATCH_FIRST_FAULT_V1", public_summary)
        self.assertIn('"scratch_first_fault": scratch_first_fault', self.source)
        self.assertIn('"scratch_resource_attribution": scratch_resource_attribution', self.source)
        controller_fault = {
            "schema": 1,
            "origin": "controller",
            "site": "controller_entry",
            "exception_kind": "permission",
            "errno": 13,
        }
        for fault, expected in (
            (FAULT, True),
            (controller_fault, True),
            (dict(FAULT, schema=True), False),
            (dict(FAULT, schema=1.0), False),
            (dict(FAULT, errno=True), False),
            (dict(FAULT, errno=13.0), False),
            (dict(FAULT, exception_kind=7), False),
            (dict(FAULT, origin="controller"), False),
            (dict(FAULT, site="controller_entry"), False),
            (dict(FAULT, unexpected_private_field="must-not-pass"), False),
        ):
            with self.subTest(legacy_first_fault=fault):
                self.assertIs(self._legacy_first_fault_valid(fault), expected)
                self.assertIs(platform_backup_restore_drill.valid_scratch_first_fault(fault), expected)

    def _assert_probe_receipt_failure_keeps_only_pending_projection(self) -> None:
        scenarios = (
            "pending_collision", "pending_write", "pending_short_write", "pending_fsync",
            "pending_parent_fsync", "pending_identity", "final_collision", "final_write",
            "final_short_write", "final_fsync", "final_parent_fsync", "identity_swap",
            "pending_cleanup", "unsafe_parent", "final_content_mutation", "final_binding_mutation",
            "final_read_error", "final_read_truncation", "final_inode_swap",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                fault_os = _FaultOS(scenario)
                precommit = scenario in {
                    "pending_collision", "pending_write", "pending_short_write", "pending_fsync",
                    "pending_parent_fsync", "pending_identity", "unsafe_parent",
                }
                operation_status = "failed" if precommit or scenario == "final_write" else "completed"
                paths = self._fixture(
                    status=operation_status,
                    first_fault=FAULT if operation_status == "failed" else None,
                    fault_os=fault_os,
                )
                temp, temporary, shared, report, capture = paths
                pending_name = f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.pending.json"
                final_name = f"authorization-probe-{RUN_ID}-{RUN_ATTEMPT}.json"
                target_shared = shared
                if scenario == "unsafe_parent":
                    target_shared = Path(temp.name) / "shared-link"
                    target_shared.symlink_to(shared, target_is_directory=True)
                elif scenario == "pending_collision":
                    (shared / pending_name).write_text("preserve-existing", encoding="ascii")
                elif scenario == "final_collision":
                    (shared / final_name).write_text("preserve-existing", encoding="ascii")
                try:
                    result, marker = self.handler["process_probe"](
                        str(report), str(capture), "1" if operation_status == "failed" else "0", str(target_shared),
                        RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA,
                        str(temporary),
                    )
                    self.assertEqual(result, 1)
                    self.assertIn("AUTHORIZATION_PROBE_EVIDENCE_UNAVAILABLE", marker)
                    self.assertIn("status=unknown", marker)
                    accepted, dispatched = self._dispatch_with_probe_marker(marker)
                    self.assertFalse(accepted)
                    self.assertNotEqual(dispatched.returncode, 0)
                    self.assertEqual(dispatched.stdout, "authorization-probe true\n")
                    if precommit:
                        self.assertIn("custody=unavailable", marker)
                        self.assertTrue(report.exists())
                        self.assertTrue(capture.exists())
                        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
                        self.assertEqual(stat.S_IMODE(capture.stat().st_mode), 0o600)
                    else:
                        self.assertFalse(report.exists())
                        self.assertFalse(capture.exists())
                        self.assertIn("custody=pending", marker)
                    if scenario == "pending_collision":
                        self.assertEqual((shared / pending_name).read_text(encoding="ascii"), "preserve-existing")
                    elif scenario == "final_collision":
                        self.assertEqual((shared / final_name).read_text(encoding="ascii"), "preserve-existing")
                        pending = json.loads((shared / pending_name).read_text(encoding="ascii"))
                        self.assertEqual(pending["kind"], "authorization_probe_pending")
                        self.assertEqual(pending["status"], "pending")
                        self.assertEqual(pending["probe_status"], operation_status)
                        expected_pending_fields = {
                            "schema", "kind", "status", "probe_status", "workflow_run_id",
                            "workflow_run_attempt", "security_run_id", "security_run_attempt",
                            "source_sha", "helper_bundle_sha256", "cleanup_status",
                        }
                        if operation_status == "failed":
                            expected_pending_fields.add("first_fault")
                        self.assertEqual(set(pending), expected_pending_fields)
                        self.assertEqual(pending["workflow_run_id"], RUN_ID)
                        self.assertEqual(pending["security_run_id"], SECURITY_ID)
                        self.assertEqual(pending["source_sha"], SOURCE_SHA)
                        self.assertEqual(pending["helper_bundle_sha256"], BUNDLE_SHA)
                        if operation_status == "failed":
                            self.assertEqual(pending["first_fault"], FAULT)
                            self.assertIn("first_fault", pending)
                    elif scenario in {
                        "final_write", "final_short_write", "final_fsync", "final_parent_fsync",
                        "identity_swap", "pending_cleanup", "final_content_mutation",
                        "final_binding_mutation", "final_read_error", "final_read_truncation",
                        "final_inode_swap",
                    }:
                        pending = json.loads((shared / pending_name).read_text(encoding="ascii"))
                        self.assertEqual(pending["kind"], "authorization_probe_pending")
                        self.assertEqual(pending["status"], "pending")
                        self.assertEqual(pending["probe_status"], operation_status)
                        self.assertEqual(pending["workflow_run_id"], RUN_ID)
                        self.assertEqual(pending["security_run_id"], SECURITY_ID)
                        self.assertEqual(pending["source_sha"], SOURCE_SHA)
                        self.assertEqual(pending["helper_bundle_sha256"], BUNDLE_SHA)
                        if operation_status == "failed":
                            self.assertEqual(pending["first_fault"], FAULT)
                        if scenario == "pending_cleanup":
                            final = json.loads((shared / final_name).read_text(encoding="ascii"))
                            self.assertEqual(final["kind"], "authorization_probe_receipt")
                        else:
                            # A partial or even complete O_EXCL entry is never promoted
                            # to workflow success after any write/identity/fsync error.
                            self.assertTrue((shared / final_name).exists())
                finally:
                    temp.cleanup()

        for invalid in (
            ("00123", RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, SOURCE_SHA, BUNDLE_SHA),
            (RUN_ID, RUN_ATTEMPT, SECURITY_ID, SECURITY_ATTEMPT, "x" * 40, BUNDLE_SHA),
        ):
            paths = self._fixture()
            try:
                result, marker = self._process(paths, bindings=invalid)
                self.assertEqual(result, 1)
                self.assertIn("status=unknown", marker)
                self.assertFalse(list(paths[2].glob("authorization-probe-*.json")))
            finally:
                paths[0].cleanup()


if __name__ == "__main__":
    unittest.main()
