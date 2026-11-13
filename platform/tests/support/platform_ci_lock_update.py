"""Validate and constrain an explicit one-package CI lock refresh."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.+-]+)==(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*) "
    r"--hash=sha256:(?P<digest>[0-9a-f]{64})$"
)
SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)$"
)


class LockUpdateError(ValueError):
    """A focused lock refresh would violate the existing lock contract."""


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_lock(path: Path) -> tuple[list[str], dict[str, tuple[str, str]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LockUpdateError("lock file is unavailable") from exc
    if not lines or lines != sorted(lines):
        raise LockUpdateError("lock file is empty or not canonically sorted")
    pins: dict[str, tuple[str, str]] = {}
    for line in lines:
        match = PIN_RE.fullmatch(line)
        if match is None:
            raise LockUpdateError("lock file contains a non-exact or unhashed pin")
        name = _normalize_name(match.group("name"))
        if name in pins:
            raise LockUpdateError("lock file contains duplicate normalized package names")
        pins[name] = (match.group("version"), line)
    return lines, pins


def _parse_spec(spec: str) -> tuple[str, str]:
    match = SPEC_RE.fullmatch(spec)
    if match is None:
        raise LockUpdateError("focused update requires one exact NAME==VERSION")
    return _normalize_name(match.group("name")), match.group("version")


def prepare(spec: str, lock_path: Path, constraints_path: Path) -> str:
    """Write constraints for all existing packages except the selected pin."""

    target, version = _parse_spec(spec)
    lines, pins = _read_lock(lock_path)
    if target not in pins:
        raise LockUpdateError("focused update target is not present in the current lock")
    if pins[target][0] == version:
        raise LockUpdateError("focused update target already has the requested version")
    constraints_path.write_text(
        "".join(
            f"{line}\n"
            for line in lines
            if _normalize_name(line.split("==", 1)[0]) != target
        ),
        encoding="utf-8",
    )
    return f"{target}=={version}"


def verify(spec: str, original_path: Path, candidate_path: Path) -> None:
    """Require the target version change and byte-identical remaining pins."""

    target, requested_version = _parse_spec(spec)
    _original_lines, original = _read_lock(original_path)
    _candidate_lines, candidate = _read_lock(candidate_path)
    if original.keys() != candidate.keys():
        raise LockUpdateError("focused update changed the locked package set")
    if target not in original or candidate[target][0] != requested_version:
        raise LockUpdateError("focused update did not produce the requested target pin")
    for name, (_version, original_line) in original.items():
        if name != target and candidate[name][1] != original_line:
            raise LockUpdateError("focused update changed a non-target package pin")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("spec")
    prepare_parser.add_argument("lock", type=Path)
    prepare_parser.add_argument("constraints", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("spec")
    verify_parser.add_argument("original", type=Path)
    verify_parser.add_argument("candidate", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "prepare":
            print(prepare(arguments.spec, arguments.lock, arguments.constraints))
        else:
            verify(arguments.spec, arguments.original, arguments.candidate)
    except LockUpdateError as exc:
        print(f"focused CI lock update blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
