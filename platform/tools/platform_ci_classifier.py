#!/usr/bin/env python3
"""Classify a repository change for the deterministic platform CI router.

The classifier is deliberately conservative.  Only a complete, explicit list
of files under ``platform/docs`` is allowed to use the docs-only route.  Any
missing repository state, unknown event/path, malformed input or merge queue
event falls back to the full deterministic suite and is never deployable.

The JSON manifest is an artifact contract between the security workflow and
the production workflows.  Its digest covers every decision-bearing field so
that a downstream workflow cannot silently broaden a route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Mapping, Sequence

try:  # Imported as ``tools.platform_ci_classifier`` in the contract tests.
    from .platform_workflow_provenance import (
        parse_run_id,
        validate_security_marker,
    )
except ImportError:  # Executed directly by the runner-side classifier.
    from platform_workflow_provenance import (  # type: ignore[no-redef]
        parse_run_id,
        validate_security_marker,
    )


MANIFEST_SCHEMA = 1
MANIFEST_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
SECURITY_WORKFLOW_NAME = "Platform security and build"
MAX_OUTPUT_VALUE_LENGTH = 512

FULL_GATE_IDS: tuple[str, ...] = (
    "backend",
    "python-quality",
    "security",
    "migration",
    "docs",
    "web-quality",
    "web-hermetic",
    "verification-contract",
)
DOCS_ONLY_GATE_IDS: tuple[str, ...] = ("docs", "verification-contract")
OUT_OF_SCOPE_GATE_IDS: tuple[str, ...] = ("verification-contract",)
KNOWN_EVENTS = frozenset({"pull_request", "push", "merge_group", "workflow_dispatch"})

# These paths are intentionally narrow.  The repository guide declares these
# trees outside the active platform, so they receive a repository-contract
# check but can never authorize a production release.
OUT_OF_SCOPE_EXACT = frozenset(
    {
        ".gitignore",
        ".gitattributes",
        "AGENTS.md",
        "LICENSE",
        "README.md",
    }
)
OUT_OF_SCOPE_PREFIXES = (
    ".agents/",
    ".codex/",
    "oldsparky_app/",
    "oldsparky_core/",
    "tests/",
    "tools/",
)
FULL_PREFIXES = (".github/", "platform/")
DOCS_PREFIX = "platform/docs/"

_DIGEST_FIELDS = (
    "schema",
    "version",
    "target_sha",
    "event",
    "class",
    "expected_gates",
    "deployable",
    "fallback",
    "reason",
    "files",
)


class ClassifierError(ValueError):
    """Raised when a classifier manifest is malformed or inconsistent."""


def _single_line_output(value: object, *, field: str) -> str:
    """Return a bounded GITHUB_OUTPUT-safe value.

    GitHub's line-oriented output protocol treats a newline in a value as a
    second output record.  Classifier reasons originate in repository/event
    metadata, so they must fail closed rather than being copied into a shell
    protocol.  ASCII printable text is sufficient for this small public
    contract and also excludes terminal/control escapes.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_OUTPUT_VALUE_LENGTH
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise ClassifierError(f"classifier {field} is not a bounded single line")
    return value


def validate_security_workflow_run(
    workflow: Mapping[str, object],
    run: Mapping[str, object],
    statuses: Sequence[Mapping[str, object]],
    *,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_target_sha: str,
) -> None:
    """Validate the exact completed security run that owns a route artifact.

    This is the data contract mirrored by the release workflows' inline
    validators.  Keeping the contract executable here gives the local
    self-tests coverage for every provenance dimension without making a
    production workflow execute source code from the candidate checkout.
    """

    if not isinstance(workflow, Mapping) or not isinstance(run, Mapping):
        raise ClassifierError("security workflow/run payload is malformed")

    # The workflow-dispatch inputs arrive as strings, while GitHub's JSON
    # identifiers are numbers.  Parse the canonical decimal form once, then
    # use the same provenance validator as the production workflow callers.
    try:
        run_id = parse_run_id(expected_run_id, "security run id")
        run_attempt = parse_run_id(expected_run_attempt, "security run attempt")
        validate_security_marker(
            workflow,
            run,
            statuses,
            expected_run_id=run_id,
            expected_attempt=run_attempt,
            expected_target_sha=expected_target_sha,
        )
    except ValueError as exc:
        raise ClassifierError(str(exc)) from exc


def _canonical_payload(manifest: Mapping[str, object]) -> bytes:
    try:
        payload = {field: manifest[field] for field in _DIGEST_FIELDS}
    except KeyError as exc:
        raise ClassifierError(f"manifest is missing digest field: {exc.args[0]}") from exc
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def manifest_digest(manifest: Mapping[str, object]) -> str:
    """Return the digest for the decision-bearing portion of a manifest."""

    return hashlib.sha256(_canonical_payload(manifest)).hexdigest()


def _normalise_files(files: Iterable[str]) -> tuple[list[str], str | None]:
    normalised: list[str] = []
    for value in files:
        if not isinstance(value, str) or not value:
            return [], "malformed changed-file list"
        if "\x00" in value or "\\" in value:
            return [], "malformed changed-file path"
        if value.startswith("/") or value.startswith("../") or "/../" in value:
            return [], "malformed changed-file path"
        if value.startswith("./"):
            value = value[2:]
        if not value or value.endswith("/"):
            return [], "malformed changed-file path"
        normalised.append(value)
    unique = sorted(set(normalised))
    if not unique:
        return [], "changed-file list is empty"
    return unique, None


def _is_out_of_scope(path: str) -> bool:
    return path in OUT_OF_SCOPE_EXACT or path.startswith(OUT_OF_SCOPE_PREFIXES)


def _route_for_files(files: Sequence[str]) -> tuple[str, tuple[str, ...], str, bool]:
    """Return class, gates, reason and whether the path set is a fallback."""

    if all(path.startswith(DOCS_PREFIX) for path in files):
        return (
            "docs-only",
            DOCS_ONLY_GATE_IDS,
            "strict platform/docs-only change",
            False,
        )
    if all(_is_out_of_scope(path) for path in files):
        return (
            "out-of-scope",
            OUT_OF_SCOPE_GATE_IDS,
            "change is outside the active platform application",
            False,
        )
    if all(path.startswith(FULL_PREFIXES) for path in files):
        return (
            "full",
            FULL_GATE_IDS,
            "platform or workflow change requires the full deterministic suite",
            False,
        )
    return (
        "full",
        FULL_GATE_IDS,
        "unknown or global path requires fail-closed full verification",
        True,
    )


def _build_manifest(
    *,
    target_sha: str,
    event: str,
    files: Sequence[str],
    branch: str,
    fallback: bool,
    reason: str,
    route_class: str,
    expected_gates: Sequence[str],
) -> dict[str, object]:
    reason = _single_line_output(reason, field="reason")
    deployable = (
        route_class == "full"
        and not fallback
        and event == "push"
        and branch == "dev"
        and bool(SHA_RE.fullmatch(target_sha))
    )
    payload: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "target_sha": target_sha,
        "event": event,
        "class": route_class,
        "expected_gates": list(expected_gates),
        "deployable": deployable,
        "fallback": fallback,
        "reason": reason,
        "files": list(files),
    }
    payload["digest"] = manifest_digest(payload)
    return payload


def classify(
    files: Iterable[str],
    *,
    event: str,
    target_sha: str,
    branch: str = "",
    repository_ready: bool = True,
    fallback_reason: str | None = None,
) -> dict[str, object]:
    """Build a validated route manifest from a changed-file list.

    ``repository_ready=False`` models shallow/unavailable git state.  It is
    intentionally separate from file classification so tests can prove that
    a seemingly docs-only list still fails closed when provenance is weak.
    """

    raw_files = list(files)
    normalised, malformed_reason = _normalise_files(raw_files)
    target_sha = target_sha.lower() if isinstance(target_sha, str) else ""
    event = event if isinstance(event, str) else ""
    branch = branch if isinstance(branch, str) else ""

    if not repository_ready:
        return _build_manifest(
            target_sha=target_sha,
            event=event,
            files=normalised,
            branch=branch,
            fallback=True,
            reason=fallback_reason or "repository state is shallow or unavailable",
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )
    if event not in KNOWN_EVENTS:
        return _build_manifest(
            target_sha=target_sha,
            event=event or "unknown",
            files=normalised,
            branch=branch,
            fallback=True,
            reason="event is missing or unknown",
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )
    if event == "merge_group":
        return _build_manifest(
            target_sha=target_sha,
            event=event,
            files=normalised,
            branch=branch,
            fallback=True,
            reason="merge_group requires full CI and has no deployment authority",
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )
    if malformed_reason:
        return _build_manifest(
            target_sha=target_sha,
            event=event,
            files=[],
            branch=branch,
            fallback=True,
            reason=malformed_reason,
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )
    if not SHA_RE.fullmatch(target_sha):
        return _build_manifest(
            target_sha=target_sha,
            event=event,
            files=normalised,
            branch=branch,
            fallback=True,
            reason="target SHA is missing or malformed",
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )
    if not normalised:
        return _build_manifest(
            target_sha=target_sha,
            event=event,
            files=[],
            branch=branch,
            fallback=True,
            reason="changed-file list is missing",
            route_class="full",
            expected_gates=FULL_GATE_IDS,
        )

    route_class, expected_gates, reason, fallback = _route_for_files(normalised)
    return _build_manifest(
        target_sha=target_sha,
        event=event,
        files=normalised,
        branch=branch,
        fallback=fallback,
        reason=reason,
        route_class=route_class,
        expected_gates=expected_gates,
    )


def validate_manifest(
    manifest: Mapping[str, object],
    *,
    expected_target_sha: str | None = None,
    require_deployable: bool = False,
) -> None:
    """Validate a classifier artifact and optional exact-release authority."""

    if not isinstance(manifest, Mapping):
        raise ClassifierError("classifier manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ClassifierError("unsupported classifier manifest schema")
    if manifest.get("version") != MANIFEST_VERSION:
        raise ClassifierError("unsupported classifier manifest version")
    route_class = manifest.get("class")
    expected_by_class = {
        "docs-only": DOCS_ONLY_GATE_IDS,
        "out-of-scope": OUT_OF_SCOPE_GATE_IDS,
        "full": FULL_GATE_IDS,
    }
    if route_class not in expected_by_class:
        raise ClassifierError("classifier manifest class is invalid")
    if tuple(manifest.get("expected_gates", ())) != expected_by_class[route_class]:
        raise ClassifierError("classifier expected gates do not match its class")
    target_sha = manifest.get("target_sha")
    if not isinstance(target_sha, str):
        raise ClassifierError("classifier target_sha must be a string")
    event = manifest.get("event")
    if not isinstance(event, str) or not event:
        raise ClassifierError("classifier event is missing")
    for field in ("deployable", "fallback"):
        if not isinstance(manifest.get(field), bool):
            raise ClassifierError(f"classifier {field} must be boolean")
    if not manifest["fallback"] and (
        not target_sha or not SHA_RE.fullmatch(target_sha)
    ):
        raise ClassifierError("classifier target_sha is malformed")
    if not isinstance(manifest.get("reason"), str) or not manifest["reason"]:
        raise ClassifierError("classifier reason is missing")
    _single_line_output(manifest["reason"], field="reason")
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
        raise ClassifierError("classifier files must be a list of strings")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ClassifierError("classifier digest is malformed")
    if digest != manifest_digest(manifest):
        raise ClassifierError("classifier digest does not match the manifest")
    if manifest["fallback"] and manifest["deployable"]:
        raise ClassifierError("fallback classifier route cannot be deployable")
    if route_class != "full" and manifest["deployable"]:
        raise ClassifierError("only the full route can be deployable")
    if expected_target_sha is not None and target_sha != expected_target_sha:
        raise ClassifierError("classifier target_sha does not match the release SHA")
    if require_deployable:
        if (
            route_class != "full"
            or event != "push"
            or manifest["fallback"]
            or not manifest["deployable"]
        ):
            raise ClassifierError("classifier route is not deployable")


def _git_changed_files(
    *,
    repo_root: Path,
    event: str,
    event_payload: Mapping[str, object],
) -> tuple[list[str], bool, str | None]:
    """Read changed files from complete git history, without shell parsing."""

    try:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return [], False, "git repository state is unavailable"
    if shallow == "true":
        return [], False, "git checkout is shallow"

    if event == "pull_request":
        pull_request = event_payload.get("pull_request")
        if not isinstance(pull_request, Mapping):
            return [], False, "pull request metadata is missing"
        base = (pull_request.get("base") or {})
        head = (pull_request.get("head") or {})
        base_sha = base.get("sha") if isinstance(base, Mapping) else None
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
    elif event == "push":
        base_sha = event_payload.get("before")
        head_sha = event_payload.get("after")
        if not head_sha:
            head_sha = event_payload.get("head_commit", {})
            head_sha = head_sha.get("id") if isinstance(head_sha, Mapping) else None
    else:
        return [], False, "event does not provide a safe changed-file range"

    if (
        not isinstance(base_sha, str)
        or not isinstance(head_sha, str)
        or not SHA_RE.fullmatch(base_sha)
        or not SHA_RE.fullmatch(head_sha)
        or set(base_sha) == {"0"}
    ):
        return [], False, "changed-file range is missing or malformed"
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", base_sha, head_sha, "--"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        decoded = result.stdout.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
        return [], False, "changed-file range cannot be resolved"
    return decoded.rstrip("\x00").split("\x00") if decoded else [], True, None


def _load_event_payload(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClassifierError(f"GitHub event payload is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ClassifierError("GitHub event payload must be an object")
    return payload


def _write_github_output(path: Path, manifest: Mapping[str, object]) -> None:
    values = {
        "class": manifest["class"],
        "event": manifest["event"],
        "deployable": str(manifest["deployable"]).lower(),
        "fallback": str(manifest["fallback"]).lower(),
        "expected_gates": json.dumps(manifest["expected_gates"], separators=(",", ":")),
        "target_sha": manifest["target_sha"],
        "digest": manifest["digest"],
        "reason": manifest["reason"],
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if isinstance(value, (dict, list, tuple)):
                encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            else:
                encoded = str(value)
            output.write(f"{key}={_single_line_output(encoded, field=key)}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--target-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--files-file", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    event_payload: Mapping[str, object] = {}
    if args.event_file is not None:
        try:
            event_payload = _load_event_payload(args.event_file)
        except ClassifierError as exc:
            manifest = classify(
                [],
                event=args.event,
                target_sha=args.target_sha,
                branch=args.branch,
                repository_ready=False,
                fallback_reason=str(exc),
            )
        else:
            event = args.event
            if args.files_file is not None:
                try:
                    files = args.files_file.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError) as exc:
                    manifest = classify(
                        [],
                        event=event,
                        target_sha=args.target_sha,
                        branch=args.branch,
                        repository_ready=False,
                        fallback_reason=f"changed-file list is unreadable: {exc}",
                    )
                else:
                    manifest = classify(
                        files,
                        event=event,
                        target_sha=args.target_sha,
                        branch=args.branch,
                    )
            else:
                files, ready, reason = _git_changed_files(
                    repo_root=args.repo_root,
                    event=event,
                    event_payload=event_payload,
                )
                manifest = classify(
                    files,
                    event=event,
                    target_sha=args.target_sha,
                    branch=args.branch,
                    repository_ready=ready,
                    fallback_reason=reason,
                )
    elif args.files_file is not None:
        try:
            files = args.files_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            manifest = classify(
                [],
                event=args.event,
                target_sha=args.target_sha,
                branch=args.branch,
                repository_ready=False,
                fallback_reason=f"changed-file list is unreadable: {exc}",
            )
        else:
            manifest = classify(
                files,
                event=args.event,
                target_sha=args.target_sha,
                branch=args.branch,
            )
    else:
        try:
            event_payload = _load_event_payload(
                args.event_file or Path(os.environ.get("GITHUB_EVENT_PATH", ""))
            )
        except ClassifierError as exc:
            manifest = classify(
                [],
                event=args.event,
                target_sha=args.target_sha,
                branch=args.branch,
                repository_ready=False,
                fallback_reason=str(exc),
            )
        else:
            files, ready, reason = _git_changed_files(
                repo_root=args.repo_root,
                event=args.event,
                event_payload=event_payload,
            )
            manifest = classify(
                files,
                event=args.event,
                target_sha=args.target_sha,
                branch=args.branch,
                repository_ready=ready,
                fallback_reason=reason,
            )

    validate_manifest(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.github_output is not None:
        args.github_output.parent.mkdir(parents=True, exist_ok=True)
        _write_github_output(args.github_output, manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClassifierError as exc:
        print(f"classifier error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
