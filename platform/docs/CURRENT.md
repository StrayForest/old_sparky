# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-13

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `9a3d203c8b8d3bb09226e010e559516657d21034` in release
  `gha-43426930984-1-9a3d203c8b8d`; `previous` is the initial rollback source
  `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31` in release
  `gha-43367073524-1-cb1a75114aac`. The earlier
  `126d4114542369af9d999e219563fa1d53b09568` release was rolled back to this
  initial source before the corrected deployment. Preserve `cb1a...` as the
  rollback target.
- Host-tools C4 source `64cae2a4c8235df1ba5c044446cd2f497f4abdca` is active,
  verified installed and self-tested; C3
  `625bbaa11023ffaf059dfa03a34a4850b02d4f93` remains retained. C3 and C4
  preserve the same 15 helper files and capability set, with 17 total bundle
  files. C4 changes only the host-tools dispatcher timeout from 300 to 2,400
  seconds and the derived capability hash. The application guard allows 1,500
  seconds only for the strict canonical public command.
- PR 360 source `9a3d203c8b8d3bb09226e010e559516657d21034` passed exact-SHA
  Security `43422580427/1`, post-provision preflight `43426001349/1`,
  source-bound restore-verified backup `43426481223/1`, AUTO `43424193707`, and
  automatic production child `43426930984/1`. The child succeeded. Required
  post-deploy strict preflight, origin/SNI checks and public smokes passed.
  Fresh readiness confirms current `9a3d...`, previous `cb1a...`, selected C4
  and retained C3. Runtime schema is unchanged. The source-bound readiness
  receipt is retained in the operations checkpoint.
- The previous 13-account roster was recovered after its age threshold by the
  active M immutable payload under both canonical locks. Exact cleanup and
  absence proof passed; receipt:
  `/root/.oldsparky/reports/liveqa-recover-bundle.2PxPsM/`.
- Public workflow `43439319991` remains a successful exact-source run. After
  fresh canonical provisioning, public workflow `43449584614` also passed
  against source M: 57 logical cases and attempts, 47 passed and 10 skipped,
  with zero failures, timeouts, flaky, interrupted or expected-failure cases,
  zero retries, and process/child exit 0.
- The first automated UserQA workflow `43440684487` failed with browser child
  exit 1; the journey cause remains unknown and receives no pass credit. Its
  report-publication blocker was an unrelated Nov 11 diagnostics directory;
  operations preserved it outside retention through a reviewed, locked helper.
  This repaired the report-store precondition but does not explain the browser
  failure. Fresh exact-source UserQA `43450827101` passed its complete coverage:
  1 journey, 1 attempt, 0 skipped, failed, flaky, timed-out, interrupted or
  expected-failure cases, child exit 0. Its sanitized report is
  `/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/userqa-retry-sanitized/live-user-qa-report.json`.
  The wrapper's exact cleanup and
  absence proof passed before state removal. Final guard checks under both
  locks found zero automated/manual recovery states, public gates, QA
  processes or diagnostic entries. Final readiness is ready: current M,
  previous `cb1a`, C4 active/C3 retained, API/web 200, locks free, no pending
  jobs, and consistency passed. The root-only receipt is
  `/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/post-userqa-cleanup/readiness.json`.
- Automated public and UserQA gates are closed. Human Chrome authentication
  and Turnstile QA remain deferred for the user. The manual helper remains
  blocked until the fixed production checkout is reconciled to the active
  source with retained work preserved and its provenance guard passes.
- Historical timeout and uncredited-run detail is in the
  [closeout audit](archive/simplification-performance-audit-2026-11-13.md).
- The 11-cell production matrix is complete, but cell 9 failed its stress
  contract with unknown capacity attribution; no server capacity ceiling is
  certified. Cell 7's client/origin request join remains unresolved. A recent
  SSR composite recovered bounded profile attribution, but its workflow report
  remains incomplete and receives no capacity or SLO credit. Another SSR
  diagnostic has one unexplained ON response disconnect. These findings do not
  establish a production capacity limit or a root cause. Use the
  [performance matrix](../performance/README.md#current-11-profile-production-matrix)
  and [SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for scoped
  evidence and procedures; no matrix rerun is pending.
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- The user completes human Chrome authentication and production Turnstile QA
  after the automated cleanup recorded above. Before that phase, reconcile the
  fixed production checkout to the exact active source while preserving
  retained local work, then require the owner runbook's checkout-provenance
  guard to pass. The current checkout is not yet approved for the manual
  helper; do not run `prepare` until the source check succeeds.
- The completed external HTTP matrix is not whole-site functional coverage.
  Cell 9's attribution failure and cell 7's unresolved client/origin join are
  not closed by the separate SSR diagnostics. No cause or server capacity
  ceiling is established. Do not rerun completed cells; use the owner
  [performance matrix](../performance/README.md#current-11-profile-production-matrix)
  and [SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for any
  separately authorized follow-up.

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
