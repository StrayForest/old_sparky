# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-02

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app source S11 is `d74adf9df950665affa523029801d5ecc49f611c`
  (`gha-41391692559-1-d74adf9df950`); previous app source S10 is
  `e7b64e069f5874e3eab983fbcc2c3ef6d26074a9`. Installed host-tools generation
  C is `5a8d4c69e208d096af418f67dfba1ef1cf630aa0`, toolset v4; generation
  `92fd34fd81e85ac71a14b2ea8ecf68d0731849a7` remains preserved. Keep runner,
  app and host-tools source identities distinct in every receipt.
- Fresh S11 readiness passed: API and web health returned HTTP 200; API, web
  and worker were active with zero restarts. The profile was API workers 2,
  database pool 24, admission 24 concurrent / 8 waiters / 250 ms, web one
  `fetch` worker with compression enabled, worker concurrency 2 and worker
  database pool 2 from the release default. Locks were clear, diagnostic plans
  absent, and the validated shared-venv origin was retained. The bounded
  readiness evidence and collector are in the private S11 checkpoint.
- Exact-source Security `41390006169/1`, backup `41390168412/1`, AUTO
  `41391199713/1` and deployment `41391692559/1` succeeded. The maintenance
  timer is active and enabled; the service is terminal idle with PID 0. The
  latest full-maintenance preview retained current, previous and the validated
  shared-venv origin; it was not applied.
- Diagnostic run `41393226256/1` is terminal prefixture failure. Event
  validation and serialization passed, then encryption failed because the
  workflow invoked the age parent directory instead of the extracted binary.
  Fixture setup, load, CPU pair, finalizer SSH and fixture cleanup were skipped;
  no fixture manifest or remote plan was created. This run is not a measurement
  and earns no capacity, SLO or performance credit. Do not dispatch again until
  the exact-source archive-layout fix passes CI, is deployed through the normal
  `dev` chain, and fresh profile-A readiness passes. Use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  dispatch, acceptance, abort and cleanup.
- Source-bound backup admission requires one successful producer bound to the
  exact merged source SHA and Security run/attempt. Follow the
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- The SSR composite has no accepted result. The next run must use the corrected
  archive-layout path after its normal release and readiness gates pass. It
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
