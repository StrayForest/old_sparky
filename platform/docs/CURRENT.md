# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-10-29

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- The latest recorded production state is source
  `9cbb73b930b71689843f58eb7b5492789f865ca1` with A restored by automatic
  deployment
  [40788852387](https://github.com/StrayForest/old_sparky/actions/runs/40788852387).
  The last retained status observation at `2026-10-29T04:28Z` recorded API `2`,
  web `1`, and admission `24` per worker across `8` workers. This timestamped
  observation is not a fresh health check; see the
  [performance evidence](../performance/README.md#ssr-c32-comparison-checkpoint-2026-10-29)
  for the comparison outcome and limits.
- Source-bound backups must have exactly one producer run ID per Security
  run, attempt and SHA. Failed or cancelled IDs count; multiple IDs make that
  proof ambiguous. Follow the [backup and restore runbook](backup-restore-runbook.md).
- A prior canonical maintenance pass
  [40150735297](https://github.com/StrayForest/old_sparky/actions/runs/40150735297)
  succeeded. A daily maintenance attempt during deployment was refused by the
  release lock and preserved its evidence. The backup-first post-QA canonical
  run
  [40154998086/1](https://github.com/StrayForest/old_sparky/actions/runs/40154998086)
  passed on the active source: a fresh checksum-present v2 backup was
  restore-verified, Alembic verified 36 restored tables, and protected-plus-
  latest retention passed. Rotation removed four older archives and one
  unprotected production release (`280,882,508` bytes); two releases remained
  protected and six retained. No live-QA runtime or source release artifact
  was deleted; one live-QA runtime remains. Free space after the final check
  is `8,603,553,792` bytes (about 21.5%).
  Endpoints and services are healthy, no jobs or locks remain, and both expired
  SSR plans are absent. The maintenance timer was active and persistent for
  that run; its present state is not verified here. This is a successful
  bounded pass, not a long-term capacity guarantee.
- Bounded storage work reclaimed `276,296,431` bytes from exact duplicate
  shared build artifacts and about `1.15 GB` from an unreferenced LiveQA
  provider payload. The protected raw recovery archive, pinned backups, and
  private histories remain outside cleanup scope. Daily maintenance does not
  prune provider payloads; installer retention owns that namespace.

## Open QA and cleanup gates

- Authenticated-page load [40114878581](https://github.com/StrayForest/old_sparky/actions/runs/40114878581)
  on source `70915e23fef5b225b6e272c0aeb50e0d2d253aec`
  returned `20,000/20,000` HTTP 200 responses but failed its logical SLO.
  Fixture cleanup passed; exact profile and comparison evidence live in the
  [performance reference](../performance/README.md#authenticated-page-load-comparison-2026-10-24).
- The SSR C32 comparison on source `9cbb73b930b71689843f58eb7b5492789f865ca1`
  completed four accepted A1/B1/A2/B2 rows, together `2,484/2,484` HTTP 200
  responses. B did not meet the pairwise improvement threshold. The final
  automatic deployment
  [40788852387](https://github.com/StrayForest/old_sparky/actions/runs/40788852387)
  restored A. The last retained status observation at `2026-10-29T04:28Z`
  recorded API `2`, web `1`, and admission `24` per worker across `8` workers;
  this is not a fresh health check. Offline analysis continues. No further A/B
  load is authorized, C64 is forbidden, and these rows do not establish a
  capacity ceiling. See the [performance reference](../performance/README.md#ssr-c32-comparison-checkpoint-2026-10-29)
  for the evidence summary.
- The installed canonical content sample on e920 exited successfully. This is
  not a new full GitHub content-diagnostics result; details were lost from its
  bounded prefix. Earlier full diagnostic [40113453588](https://github.com/StrayForest/old_sparky/actions/runs/40113453588)
  remains failed and is not superseded by that sample.
- LivingInk Violet Russian ability descriptions are `4/4`. Broader
  human-visible authentication and Turnstile QA remains **NOT RUN**.

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
