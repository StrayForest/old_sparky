# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-05

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Last verified production baseline is app A
  `c116de3f78f85833646aa23d117c6a0a313db6cb` (deployment run
  `42059556545/1`; previous app source
  `82956d85cdc7cec034c74ac22e8cd7091c229fcc`), with active host-tools
  generation C `0de51aca4b4d04a80a2f0d392003cc258d8f1400`. The exact read-only
  baseline observation is bound in deployment run `42071875137`; that run did
  not deploy the current runner source. Before future production work, use the
  canonical full-readiness receipt to confirm the live baseline and locks.
  Resolve A from a successful deployment receipt and C from the checked-in
  active pin for each operation. Keep workflow runner S, application A and
  host-tools C distinct; a matching runner SHA does not prove deployment or
  installation.
- The targeted API read-mix run `42034147480/1` has closed with authoritative
  fixture cleanup; no load or fixture is active. One restore-verified backup
  pair is retained. Post-run FULL readiness is not yet evidenced: two canonical
  collector attempts returned generic failures without JSON, and a separate
  traced invocation returned `ready` but retained only its trace projection,
  not the canonical receipt. Preserve the failures as unexplained and obtain
  and validate a complete canonical FULL receipt before the next consequential
  production operation.
- Cell 9, `read-mix-concurrency-ramp-v1`, completed as run `41937909055/1` on
  runner S and app A, both `7c6034efe3878c3413090ca1bcdb5be6e93154e8`, with
  host-tools C `0de51aca4b4d04a80a2f0d392003cc258d8f1400`; exact cleanup
  passed. All eight stages observed 40/40 active Ready Checks and passed the
  state guard, with minimum deadline headroom of about 2,424 seconds. All
  170,000 HTTP actions succeeded (160,000 HTTP 200 and 10,000 conditional
  304), with no errors, retries or drops. The stress contract failed, and its
  sole failed evaluator check was `capacity_ramp_evidence`; per-stage logical
  p95/p99 budgets failed at all eight stages. Aggregate logical p95/p99 were
  209.4/331.2 seconds against 12/20-second limits; HTTP p95/p99 were
  886/1,135 ms. Capacity attribution is unknown (`runner_window_incomplete`):
  all offered ramp tasks started late, none were
  dropped, and this run certifies no server capacity ceiling. See the
  [performance matrix](../performance/README.md#current-11-profile-production-matrix).
- Cells 1–8 and 10–11 completed on the previous P4/C baseline. Their contract
  outcomes and cleanup status remain in the matrix table. The corrected SSR
  capacity diagnostic is separate evidence, not a matrix cell; use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  its acceptance and cleanup contract. Do not switch app or host-tools
  profiles during an active cell.
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- All 11 matrix cells have run with authoritative cleanup. Cell 9 passed its
  state guard but failed the stress contract; its capacity attribution is
  unknown and no server ceiling is certified. No matrix rerun is pending. Do
  not repeat completed cells or describe this external HTTP matrix as whole-site
  functional coverage. See the
  [matrix procedure and scope](../performance/README.md#current-11-profile-production-matrix).
- Cell 7's authenticated-page diagnostic still has unresolved client/origin
  reconciliation: the repeat had 19,971/20,000 successes, 28 status-0 and one
  status-502 failure, while Nginx recorded 19,990 HTML requests, all 200.
  There is no client-request join, so the transport cause remains unknown.

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
