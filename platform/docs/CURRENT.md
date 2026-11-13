# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-13

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app and runner source is
  `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31` (release
  `gha-43367073524-1-cb1a75114aac`); previous app is
  `79b3b95ede586da84c3d49138bf88888f866c6b3` (release
  `gha-43176891484-1-79b3b95ede58`). Active host-tools C3 is
  `625bbaa11023ffaf059dfa03a34a4850b02d4f93`. Exact-SHA Security
  `43365424621/1`, source-bound restore-verified backup `43366747281/1`, AUTO
  `43366680112/1`, and automatic production child `43367073524/1` completed
  successfully. The app source was reverted to the previous production visual
  design, then published through the standard production chain. Strict release
  preflight and origin/SNI plus public-edge smoke passed. API, worker, web and
  Nginx are active; the database remains at Alembic head `20261112_0055` with
  no downgrade. The removed public `/api/v1/appearance` endpoint returns HTTP
  404. The site and tournament page return HTTP 200.
- The retained immutable LiveQA runtime still targets the previous production
  source `79b3b95ede586da84c3d49138bf88888f866c6b3` at
  `/root/.oldsparky/liveqa/releases/79b3b95ede586da84c3d49138bf88888f866c6b3/runtime`;
  its prior public browser and DTO results remain historical evidence for that
  source, not QA for the current appearance release. The release-operation
  marker is absent. No LiveQA runtime was changed for this app-only release.
- Read-only storage diagnostics `43368405441/1` confirmed the current cb1a and
  previous 79b3 releases, unlocked release lock, active API/worker/web
  services, verified backup state, and 10,503,340,032 bytes free (73.0% used).
  The guarded maintenance workflow `43368589822/1` completed successfully for
  cb1a. Its fresh backup passed restore and Alembic verification (37 tables);
  the locked retention sweep removed one unprotected production release while
  preserving current, previous and the shared-venv origin. One verified backup
  pair remains. No LiveQA runtime or transient browser/build/screenshot
  artifacts were removed. The maintenance service exited successfully, with
  10,629,095,424 bytes free (73% used) afterward. Current/previous pointers,
  API/web readiness and absence of a pending release operation were rechecked.
- SSR composite `42862469594/1` measured on app A
  `645a5ee0d14b98e4784be2dbf276f64961c9eb66`, runner source S
  `154be3f847007accd83f077f3079b3f3ccf85813` and host-tools C2. Its unprofiled
  120-second capacity phase completed with 3,911/3,911 HTTP 200 responses.
  API service CPU was 83.797 CPU-seconds over 120.009 seconds and Web service
  CPU was 88.967 CPU-seconds over 120.028 seconds. The separate 20-second
  profile phase completed its HTTP window, but the public report remained
  incomplete because its profile-row projection rejected a valid signed Web
  end lag. A bounded, exact-window journal recovery independently validated
  two API cProfile rows, one Web V8 profile and all three ON service-CPU rows;
  the Web end lag was -12 ms, within the intended ±250 ms contract. This
  recovered attribution does not rewrite the workflow report or grant capacity,
  SLO or performance credit. The dispatcher now accepts the signed lag. Treat
  cProfile/V8 results as profiled attribution, not an unprofiled benchmark or
  a capacity ceiling; traced ORM and serialization shares are prioritization
  signals, not route-causal shares or guaranteed gains. The private analysis
  report retains the detailed breakdown.
- Earlier SSR diagnostics `42808854934/1` and `42824003998/1` remain incomplete
  and zero-credit; the latter's abort `42825813580/1` and exact fixture cleanup
  `42826290267/1` completed. These results are not upgraded by the recovered
  profile from `42862469594/1`. Use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  phase acceptance and cleanup requirements.
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
- SSR diagnostic `42849326329/1` on app A
  `645a5ee0d14b98e4784be2dbf276f64961c9eb66` had 31/32 successful ON
  responses and one `remote_disconnect` at response headers; its separate OFF
  probe completed 32/32 HTTP 200. The bounded Nginx projection found no error
  entry in the failure second, but has no request-ID join. The cause remains
  unknown; neither the 19.5–25 second OFF-to-ON idle interval nor a 30-second
  downstream keepalive setting proves that a reused connection expired.
- The older SSR composite diagnostic `42081912538/1` on app `9a73f635` stopped
  after 70.385 seconds of its 120-second capacity phase. Four body-receive
  `incomplete_response` failures observed HTTP 200 before the response body
  ended; its cause and any server capacity ceiling remain unproven. The later
  corrected run `42788095845/1` does not retroactively explain or validate
  that older result. The separate CPU-only diagnostic `42085780534/1` also
  remains unclassified because its parent report has no phase data or failure
  code. Keep these older runs at zero credit and follow the owner runbook before
  any further production profile.

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
