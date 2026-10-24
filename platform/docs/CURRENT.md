# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-10-25

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production is live on source `12b6a818268da426eaa3b737566c5f05d2b52ac4`;
  previous is `e9208294ef7bfaaa7f163d15469dfa274a3df5c4`. Exact-SHA Security
  [40136167275/1](https://github.com/StrayForest/old_sparky/actions/runs/40136167275),
  its sole source-bound backup
  [40136375447/1](https://github.com/StrayForest/old_sparky/actions/runs/40136375447),
  and automatic deployment child
  [40137325477/1](https://github.com/StrayForest/old_sparky/actions/runs/40137325477)
  passed. Final read-only diagnostics
  [40139895165](https://github.com/StrayForest/old_sparky/actions/runs/40139895165)
  confirmed these pointers, no operation lock, active services with zero
  restarts, and `6,180,192,256` bytes free. This point-in-time sample does not
  establish ongoing cleanup or capacity headroom.
- Source-bound backups must have exactly one producer run ID per Security
  run, attempt and SHA. Failed or cancelled IDs count; multiple IDs make that
  proof ambiguous. Follow the [backup and restore runbook](backup-restore-runbook.md).
- Storage maintenance [40139765922](https://github.com/StrayForest/old_sparky/actions/runs/40139765922)
  failed its receipt-contract gate. Bounded failure evidence showed the
  backup scratch-image admission guard refused at the unchanged free-space
  floor; scratch cleanup was confirmed and pruning did not run. After a
  separate locked release-only retention operation removed six verified
  unpinned releases, maintenance [40140934751](https://github.com/StrayForest/old_sparky/actions/runs/40140934751)
  completed successfully: its `127,972,590`-byte backup checksum and restore
  were verified, Alembic verification passed, protected retention passed, and
  20 canonical rotation entries were removed. Free space is now
  `8,653,447,168` bytes (`21.65%`); current, previous and all protected pins
  are unchanged, services are active, endpoints returned HTTP 200, and no
  jobs, locks or release transaction remain. The timer is enabled, active and
  waiting with source-unit parity; its next trigger is
  `2026-10-25T04:27:13` Helsinki time. This proves one successful automatic
  maintenance pass, not a long-term cadence or capacity guarantee.
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
- SSR run [40143438718](https://github.com/StrayForest/old_sparky/actions/runs/40143438718)
  was refused at remote-plan preparation because an expired API/web plan pair
  from e920 remained; cleanup under a new run ID was correctly refused. Exact
  fixture cleanup [40143901154](https://github.com/StrayForest/old_sparky/actions/runs/40143901154)
  passed, and canonical cleanup of the expired pair removed both plans. Abort
  [40143759494](https://github.com/StrayForest/old_sparky/actions/runs/40143759494)
  stopped the exact process tree, but normalization did not produce CPU proof.
  Retry [40144108246](https://github.com/StrayForest/old_sparky/actions/runs/40144108246)
  terminated in the guarded step with exit 2; report sanitization failed with
  `ValueError`, so no sanitized artifact or primary marker was produced. Its
  precise cause is unproven. Exact fixture cleanup
  [40144546228](https://github.com/StrayForest/old_sparky/actions/runs/40144546228)
  passed, removing 14 users, 1 tournament and 1 marker while preserving control
  state; the expired pair was canonically removed and both plans are absent.
  Activity is clear. No capacity credit is awarded. See the
  [performance reference](../performance/README.md) for expiry, cleanup
  ownership and failure-report closure.
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
