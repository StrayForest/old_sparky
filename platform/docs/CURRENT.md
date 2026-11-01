# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-02

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app source is `4d33d1c3c8495ab4cdd341dd3809e7ca0067ce4a`; installed
  host-tools generation is `92fd34fd81e85ac71a14b2ea8ecf68d0731849a7`. The
  normal deployment and strict shared-venv reuse checks passed. Keep runner
  source, app source and host-tools generation distinct in every receipt.
- The latest paired SSR diagnostic is incomplete and earns no capacity, SLO
  or matrix credit. Its exact fixture was closed through the canonical abort
  and cleanup workflows. The profile phase reported `invalid_profile`; this is
  not a passing measurement or a server-wide capacity result.
- The reviewed profile-rejection and API category-conservation source candidate
  is `5a8d4c69e208d096af418f67dfba1ef1cf630aa0`; its pin-only successor is
  `5c17be7362e19ca786eefb89d3bf38f2482ab048`. Exact-head CI, encrypted
  credential transport review, source-bound host-tools provisioning and fresh
  profile-A readiness are still pending. Raw fixture-manifest artifacts must
  use the reviewed encrypted transport because they contain synthetic session
  credentials. Do not dispatch another load until the integrated source passes
  exact-head CI, its host-tools generation is deployed, and fresh profile-A
  readiness passes. Use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  dispatch, acceptance, abort and cleanup.
- Source-bound backup admission requires one successful producer bound to the
  exact merged source SHA and Security run/attempt. Follow the
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- The SSR composite remains incomplete pending the profile producer and
  rejection-projection corrections. A workload may not be retried until its
  exact-run fixture is closed and all report/parser gates pass. The diagnostic
  remains zero-credit even when complete; see the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md).

## Hard operational limits

- Preserve at least `max(5 GiB, ceil(10% of total disk))` free space. Include
  backup, restore-image, scratch and temporary allocations when admitting a
  production operation. See the [disk free-space policy](adr/disk-free-space-thresholds.md)
  and [operations runbook](operations-runbook.md).
- Keep local linked-worktree release builds in the shared canonical
  `platform/dist/releases` owner path; production CI keeps its per-run output
  override. Installer retention removes unreferenced runtime extras while
  preserving current, previous and recursive references. Daily maintenance
  owns browser-cache cleanup, not provider-payload pruning.
- GitHub Actions is the release authority. Deploy only through the successful
  exact-SHA Security and automatic production chain on `dev`; do not call
  release installers directly or dispatch the normal deployment manually.
  Rollback switches application releases and does not downgrade Alembic.
  See the [deployment runbook](deployment-runbook.md) and
  [release state machine](release-state-machine.md).
- Platform data uses `platformdb`, schema `platform`; do not touch the retired
  legacy bot or `sparkydb`. Preserve authorization, lock, restore and
  provenance checks. See [production architecture](production-architecture.md)
  and [product reference](product-reference.md).
- Operator inputs remain in
  `/root/old_sparky/.local/private/operator-inputs.md`; never put their values,
  private reports or live personal data in source, logs or artifacts.

## Owner references

- Current product and workflow contracts: [product reference](product-reference.md).
- Release, recovery and retained-load procedures: [deployment runbook](deployment-runbook.md),
  [service recovery](service-recovery-runbook.md), and
  [retained-load cleanup](retained-load-and-web-verification.md).
- Storage and backup policy: [operations](operations-runbook.md),
  [backup and restore](backup-restore-runbook.md), and
  [test-suite governance](test-suite-governance.md).
- Detailed performance, storage and historical measurements:
  [performance evidence](../performance/README.md) and the linked
  [archive](archive/).
- Historical release and storage-recovery decisions:
  [October 2026 checkpoints](archive/storage-release-checkpoints-2026-10.md).
- Prioritized engineering work: [platform roadmap](platform-roadmap.md).
