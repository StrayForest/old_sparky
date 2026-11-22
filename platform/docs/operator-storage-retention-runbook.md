# Shared-host operator storage retention

- Status: Active operational procedure
- Owner: Production operator
- Last reviewed: 2026-11-21

The standalone `deadlock-operator-storage-retention.timer` runs
`deadlock-operator-storage-retention.service` daily at 07:00, after backup and
platform maintenance. The service is separate from backup creation and
verified-pair rotation. The normal reviewed release path installs both units
through `platform_install_systemd_units.sh` under `/etc/systemd/system`. On
ordinary upgrades, after successful smoke checks and the durable
`activation-committed` phase, the deploy finalizer enables and starts this
required timer and verifies it enabled and active. This required timer is
re-enabled if an operator disabled it; other timer states are left unchanged.
Retries in that phase safely repeat the same operation. The implementation
owner is
[`platform_operator_storage_retention.py`](../tools/platform_operator_storage_retention.py).

The tool accepts no root or path overrides. A bare invocation is a read-only
preview; the systemd service passes `--apply`. Preview and apply output contain
only fixed category labels, candidate/removed counts, blocked/busy counts,
reclaimable bytes and a closed status/reason. Output never includes paths,
basenames, process details, arguments, environment values or report contents.
The preview is a category-level capacity check, not a per-file deletion plan.

## Exact cleanup scope

The implementation allowlist contains only:

- Direct `.git/codex-performance-YYYYMMDD` directories whose date is more
  than 14 days old.
- `/root/.npm/_cacache` and `/root/.npm/_npx`.
- These exact `/root/.cache` children: `ms-playwright`, `pip`, `pip-audit`,
  `gh`, `mesa_shader_cache`, `gstreamer-1.0`,
  `python-entrypoints` and `Microsoft`.
- `/root/.cache/oldsparky` is excluded because it may hold required installed
  verification tools such as the attested actionlint executable. Retain the
  current exact tool version until its verified replacement is installed;
  downloading caches and installed toolchains have different owners.
- Inactive VS Code `Stable-<40 lowercase hex>` and
  `Insiders-<40 lowercase hex>` server generations.
- `/tmp/oldsparky-web-hermetic.*` directories whose modification time is more
  than 24 hours old.
- Git-registered, root-owned, attached linked worktrees under
  `/root/old_sparky-*`, `/root/oldsparky-worktrees/`,
  `/root/.codex/worktrees/` and `/dev/shm/oldsparky-*`. The canonical
  checkout subtree is excluded from worktree discovery.

The primary `/root/old_sparky` checkout is never eligible. A linked worktree
is eligible only when unlocked, inactive, attached to a local branch, and
clean except for these exact ignored generated paths:
`platform/.venv_platform`, `platform/.cache/platform-ci-venv`,
`platform/apps/platform_web/node_modules`, `platform/apps/platform_web/.next`,
`platform/apps/platform_web/test-results` and
`platform/apps/platform_web/playwright-report`. A collapsed ignored
`platform/.cache/` entry is accepted only when its sole child is
`platform-ci-venv`. These paths are disposable generated dependencies or test
outputs in a closed temporary worktree; matching names elsewhere are not
eligible. The canonical checkout and its ignored data are never scanned.
The tool removes an eligible closed worktree without force and preserves its
branch ref. Dirty, untracked, unknown ignored data, detached worktrees and
Git-locked worktrees are skipped. Task owners lock active temporary worktrees
with `git worktree lock <path> --reason <task-id>`; unlock only after the
result is integrated and the task is closed. Never use a full checkout copy
as a backup or add targets through wildcard discovery.

Every candidate must be root-owned, a real directory and identity-stable.
Targets with an open file, working directory, executable or mapped-file
reference are skipped. npm caches are also protected while a relevant npm/Node
process is active. A nonzero result or validation/deletion failure stops the
service and records bounded status/reason fields; do not retry blindly or
delete a busy target manually. The tool never touches `.env*`, reports,
credentials, Codex history, branch refs, arbitrary ignored files or unlisted
caches. Add a new category only by changing its owner implementation and
focused tests; do not create a second timer.

## Preview and verification

Check the installed timer and run the read-only preview from the active
release:

```bash
systemctl show deadlock-operator-storage-retention.timer \
  --property=ActiveState,UnitFileState,NextElapseUSecRealtime --no-pager
/usr/bin/python3 -I -B \
  /opt/oldsparky/platform/current/tools/platform_operator_storage_retention.py
```

Review preview counts and reclaimable bytes by category. The daily service is
the only routine apply path; confirm its systemd result and bounded
status/reason journal fields after the scheduled run. If the timer is inactive,
units missing, output categories unexpected, or the service exits nonzero,
preserve the safe evidence and repair the implementation or unit through a
reviewed release. Do not manually run `--apply`, remove a busy target or
assume an unlisted path is disposable.
