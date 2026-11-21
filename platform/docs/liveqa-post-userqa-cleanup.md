# LiveQA credential-file cleanup after UserQA

- Status: Active operator procedure
- Owner: Production operator

Use this after the standalone public QA and exact-source UserQA workflows both
pass for the same active app and LiveQA payload. The validated UserQA report
must show complete success, `app_sha` equal to the active source SHA, and its
`counts.marker_sha256`. The trusted UserQA wrapper only publishes its success
marker after the exact inventory cleanup, database absence verification, and
root-state removal succeed. If UserQA failed or cleanup state remains, stop and
use the exact-state recovery procedure in the [CSP/live QA runbook](csp-live-qa-runbook.md).

This procedure removes only `/root/.oldsparky/liveqa/csp-live-qa.json` and
`/root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py`. It does not edit
database rows. It requires the active application and immutable LiveQA payload
to remain at the same exact source SHA, no pending release transaction, and no
active or retained LiveQA operation. Do not reuse the removed roster.

Set `S` to the exact installed app/payload SHA, and `MARKER_SHA` to
`counts.marker_sha256` from the validated successful UserQA report. Do not use
the raw marker or any credential as an argument. Run as root; the release-lock
executor acquires the application lock and the embedded script takes the
existing LiveQA machine lock. It verifies active source/payload identity,
absence of recovery/gate/manual state, the fixed root-owned directory and
files, complete bundle shape, the report-bound marker digest, and the helper's
active-payload binding before removing either file.

```bash
S='<exact installed app and LiveQA payload SHA>'
MARKER_SHA='<marker_sha256 from validated UserQA report>'
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLATFORM_LIVE_QA_INSTALL_ROOT="/root/.oldsparky/liveqa/releases/$S" \
PLATFORM_LIVE_QA_TARGET_SHA="$S" \
PLATFORM_LIVE_QA_EXPECTED_MARKER_SHA256="$MARKER_SHA" \
/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh \
  --expected-sha "$S" -- \
  /opt/oldsparky/platform/shared/venv/bin/python -I -B - <<'PY'
#!/usr/bin/env python3.12
"""Remove only the two fixed LiveQA credential files after accepted UserQA."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys

EXPECTED_TARGET = os.environ.get("PLATFORM_LIVE_QA_TARGET_SHA", "")
EXPECTED_MARKER_SHA256 = os.environ.get(
    "PLATFORM_LIVE_QA_EXPECTED_MARKER_SHA256", ""
)
INSTALL_ROOT = Path(os.environ.get("PLATFORM_LIVE_QA_INSTALL_ROOT", ""))
SECRET_ROOT = Path("/root/.oldsparky/liveqa")
BUNDLE_PATH = SECRET_ROOT / "csp-live-qa.json"
HELPER_PATH = SECRET_ROOT / "platform_live_qa_mailbox_helper.py"


def fail() -> None:
    raise SystemExit("LiveQA post-success file cleanup refused.")


def fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


if (
    re.fullmatch(r"[0-9a-f]{40}", EXPECTED_TARGET) is None
    or re.fullmatch(r"[0-9a-f]{64}", EXPECTED_MARKER_SHA256) is None
    or INSTALL_ROOT != Path(f"/root/.oldsparky/liveqa/releases/{EXPECTED_TARGET}")
    or os.environ.get("PLATFORM_APP_DIR") != "/opt/oldsparky/platform"
    or os.environ.get("PLATFORM_LIVE_CSP_QA_BUNDLE") != str(BUNDLE_PATH)
):
    fail()

sys.path.insert(0, str(INSTALL_ROOT / "platform"))
from tools import platform_live_qa_guard as guard  # noqa: E402
from tools.platform_live_qa_machine_lock import open_machine_lock  # noqa: E402
from tools.platform_provision_live_csp_qa import load_bundle  # noqa: E402

guard._assert_release_lock_supervisor()
if (
    guard._active_release_commit() != EXPECTED_TARGET
    or guard._validate_installed_payload_root(
        INSTALL_ROOT, target_sha=EXPECTED_TARGET
    )
    != EXPECTED_TARGET
):
    fail()

lock_fd = open_machine_lock()
try:
    guard.assert_no_recovery_state(BUNDLE_PATH, include_manual=True)
    guard._validate_root_secret_parent(BUNDLE_PATH)

    directory_fd = os.open(
        SECRET_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        directory_meta = os.fstat(directory_fd)
        path_meta = SECRET_ROOT.lstat()
        if (
            not stat.S_ISDIR(directory_meta.st_mode)
            or (directory_meta.st_dev, directory_meta.st_ino)
            != (path_meta.st_dev, path_meta.st_ino)
            or directory_meta.st_uid != 0
            or directory_meta.st_gid != 0
            or stat.S_IMODE(directory_meta.st_mode) != 0o700
        ):
            fail()

        bundle_stat = os.stat(
            BUNDLE_PATH.name, dir_fd=directory_fd, follow_symlinks=False
        )
        helper_stat = os.stat(
            HELPER_PATH.name, dir_fd=directory_fd, follow_symlinks=False
        )
        for metadata, expected_mode in (
            (bundle_stat, 0o600),
            (helper_stat, 0o500),
        ):
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                fail()

        bundle = load_bundle(BUNDLE_PATH)
        if (
            bundle.payload.get("mailbox_helper") != str(HELPER_PATH)
            or bundle.fingerprint
            != (
                bundle_stat.st_dev,
                bundle_stat.st_ino,
                bundle_stat.st_size,
                bundle_stat.st_mtime_ns,
            )
            or hashlib.sha256(
                str(bundle.payload.get("marker", "")).encode("ascii")
            ).hexdigest()
            != EXPECTED_MARKER_SHA256
        ):
            fail()
        guard._verify_active_immutable_helper_binding(
            platform_root=INSTALL_ROOT / "platform",
            installed_helper=HELPER_PATH,
        )
        if (
            fingerprint(os.stat(
                BUNDLE_PATH.name, dir_fd=directory_fd, follow_symlinks=False
            ))
            != fingerprint(bundle_stat)
            or fingerprint(os.stat(
                HELPER_PATH.name, dir_fd=directory_fd, follow_symlinks=False
            ))
            != fingerprint(helper_stat)
        ):
            fail()

        for name, expected in (
            (BUNDLE_PATH.name, bundle_stat),
            (HELPER_PATH.name, helper_stat),
        ):
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if fingerprint(current) != fingerprint(expected):
                fail()
            os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        for name in (BUNDLE_PATH.name, HELPER_PATH.name):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            fail()
    finally:
        os.close(directory_fd)
finally:
    os.close(lock_fd)

print("LiveQA credential files removed; exact fixed paths are absent.")
PY
```

Success prints `LiveQA credential files removed; exact fixed paths are absent.`
Then confirm the exact two paths are absent. Any refusal or error aborts the
operation; do not bypass a lock/identity check or repeat after a partial
unlink. Preserve any remaining exact file and route recovery through the
production operator after checking the lock and source bindings. Never print
the bundle or helper contents or include them in reports.
