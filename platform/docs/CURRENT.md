# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-02

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app source S12 is `009062a7812552897e92490818bd699858a0f4f9`
  (`gha-41400373353-1-009062a78125`); previous app source S11 is
  `d74adf9df950665affa523029801d5ecc49f611c`. Installed host-tools generation
  C is `5a8d4c69e208d096af418f67dfba1ef1cf630aa0`, toolset v4; generation
  `92fd34fd81e85ac71a14b2ea8ecf68d0731849a7` remains preserved. Keep runner,
  app and host-tools source identities distinct in every receipt.
- Exact-source Security `41398260324/1`, backup `41398501956/1`, AUTO
  `41399761466/1` and deployment `41400373353/1` succeeded. The source-bound
  backup was restore-verified with 36 tables and preserved existing archives.
  That records the then-deployed S12 policy; the reviewed keep-one retention
  change is not active until its exact source is merged and deployed.
  The merged read-only readiness collector did not produce a ready record: it
  failed closed with `HostToolsPinError` before collecting the effective
  profile, health and lock fields. S12 therefore has no fresh readiness pass
  and no S12 effective-profile claim.
- The last verified profile remains the S11 snapshot, not a current S12
  readiness result. The prior run `41393226256/1` was a prefixture no-op and
  earns no measurement or capacity credit. Do not dispatch a new diagnostic
  until the release-layout pin validation fix passes exact-source CI, its
  read-only verification path is independently reviewed, and readiness passes
  for the installed app source and C generation. A separately staged verifier
  can establish readiness only; it does not authorize runtime or fixture
  mutations. Use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  dispatch, acceptance, abort and cleanup.
- Source-bound backup admission requires one successful producer bound to the
  exact merged source SHA and Security run/attempt. Follow the
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- The SSR composite has no accepted result. The next run must use the corrected
  credential transport after its normal release and readiness gates pass. The
  current readiness blocker is the release-layout host-tools pin validator;
  the prior prefixture run remains zero-credit. See the
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
