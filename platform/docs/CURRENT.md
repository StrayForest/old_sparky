# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-04

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app release P4 (`72bafc12` prefix) is active with host-tools
  generation C `da8e1275` prefix. Main-load workflow source S is
  `01b6c0991ed33218e5abae9bcabe7f5c1322ee4d`; it is a separate runner identity,
  not the deployed application source. Exact-source Security
  `41795419693/1`, AUTO `41797244771/1`, and the verified no-op child
  `41797485924/1` bind runner S to the unchanged active app P4. One verified
  source-bound backup is retained; storage review `41800306654/1` completed.
  Keep S, app P4 and host-tools C distinct in every row and artifact.
- The corrected 120-second SSR plus 20-second CPU-profile diagnostic
  `41798744468/1` completed successfully and its exact fixture cleanup passed.
  It is an SSR capacity/diagnostic result, not a cell in the 11-profile
  external-load matrix. Use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  its separate acceptance and cleanup contract.
- The 11-profile external-load matrix remains in progress against P4/C with
  runner source S above. Cell 10, `ready-vote-stress-15k-v2` run
  `41823168149/1`, completed exact cleanup (30 tournaments and 15,000 users
  deleted; control preserved) but failed its stress-behavior contract under
  503 overload; idempotency checks found no state violation. Cell 11,
  `ready-vote-stress-20k-v2` run `41825666178/1`, completed exact cleanup
  (40 tournaments and 20,000 users deleted; control preserved) but failed the
  stress-behavior contract: 16,520/21,000 logical actions succeeded and
  24,224/40,744 HTTP requests returned 503. Cell 9,
  `read-mix-concurrency-ramp-v1`, was
  deferred before dispatch because the original fixture's 10-minute Ready
  Check TTL could expire during its ~27-minute ramp. It remains pending until
  the corrected runner and installed origin-helper release; record actual app
  and host-tools pins from that receipt. Cell outcomes and scope are in the
  [performance evidence](../performance/README.md#current-11-profile-production-matrix).
  Do not switch app or host-tools profiles during an active cell.
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- The 11-profile matrix is not complete while any cell is running, omitted or
  missing authoritative cleanup. Record each profile's own contract outcome;
  a complete execution can still fail its SLO/behavior target. Do not describe
  this external HTTP matrix as whole-site functional coverage. See the
  [matrix procedure and scope](../performance/README.md#current-11-profile-production-matrix).
- Original cell 7 `authenticated-page-load-v2` had 53 timeouts out of 20,000
  requests (0.265%) whose transport stage and observed response status were
  absent from its public summary. After the corrected runner and origin helper
  are released, run one targeted `authenticated-page-load-v2` diagnostic
  repeat; do not repeat the full matrix for this question.

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
