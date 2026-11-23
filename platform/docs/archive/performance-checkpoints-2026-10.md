# Performance checkpoints: October 2026

- Status: Historical
- Owner: Platform performance
- Last reviewed: 2026-11-23

## External authenticated-page and CPU diagnostic checkpoint (2026-10-18)

The deployed application source remained `3c3b060bfb8927aa3a9ee5896a40ed1a0f6c8c84` through both authenticated-page measurements and the CPU diagnostic attempt; the CPU pair itself did not start. Profile 10 (`authenticated-page-load-v1`, HTTP/1.1 close) completed 20,000/20,000 HTTP actions with no errors, retries or drops. HTTP p95/p99 were 3,095/3,935 ms; full user-observed logical p95/p99 were 587/618 seconds. Its report bindings and fixture freshness passed review, and authoritative cleanup reported `ok=true` and `control_account_preserved=true` after deleting the 20,000 test users and 40 tournaments. The result is a valid SLO failure.

Profile 11 (`authenticated-page-load-v2`, HTTP/1.1 keep-alive) also completed 20,000/20,000 actions with no errors, retries, drops or duplicates. HTTP p95/p99 were 2,742/3,159 ms; logical p95/p99 were 522/542 seconds. Bindings, new-fixture identity and authoritative cleanup passed independent review; cleanup again preserved the control account. This is also a valid SLO failure. Keep HTTP request timing distinct from full logical user-observed timing; the latter includes bounded scheduling wait.

The separate CPU diagnostic run `39099339471/1` is invalid/incomplete and earns no matrix credit or CPU-threshold result. Its client recorded 18,164 HTTP 200, 1,835 HTTP 502 and one HTTP 500; the bounded observer evidence confirms one `deadlock-web.service` cgroup OOM kill at `2026-10-18T05:27:31Z`, followed by one web restart. It does not establish the individual 502 causes. The CPU pair did not start: the pair runner rejected the missing `SOURCE_GIT_SHA` required to bind the runner source independently from `APP_TARGET_SHA`. Source review also found that the pair's CSRF validator rejected the issuer's canonical `43 URL-safe characters + '.' + 64 lowercase hex` token shape. A reviewed local tooling fix addresses those two defects without changing application behavior, authentication rules, or diagnostic thresholds. The run is not a threshold failure and does not support a CPU capacity conclusion.

Before another CPU measurement, provide a separately guarded CPU-only path that preserves the eight-actor fixture, two Gunicorn workers plus one Node process, OFF/ON request counts, all pairing and no-restart checks, and existing thresholds. Do not rerun the current profile route blindly: it runs a 20,000-action load before the pair, and the observed web cgroup OOM makes that repeated stress inappropriate without a reviewed plan. Preserve the failed diagnostic evidence and keep production memory limits and security checks unchanged.

## Performance and backup checkpoint (2026-10-14)

The five-run matrix remains **PASS / PASS / PASS / FAIL / FAIL** for
`ready-vote-slo-v2` (38161685678), `read-mix-human-v2` (38162117136),
`ready-vote-spike-v1` (38162697642), `ready-vote-capacity-ramp-v2`
(38163212410) and `ready-vote-stress-15k-v2` (38165101863). Preserve each
result against its original source and contract. The capacity profile's
highest qualifying SLO rate is 60 logical actions/s. At 70/s, accepted and
logical p95/p99 were 621.066/7,326.342 ms and 621.453/7,326.683 ms, over the
600/1,000 ms limits. At 80/s, latency passed but retry amplification (0.3333%)
and shedding (0.3322%) exceeded the profile's zero limits. Stable goodput was
79.355 actions/s; it is a separate measure, not the SLO capacity or a proven
server limit.

The 15k stress profile failed: logical p95 was 109,811 ms versus HTTP p95
439.348 ms; final logical failures were 22.273%, shedding 60.5453%, and retry
amplification 97.0032%. These client outcomes do not establish CPU saturation
or attribute a server-side cause. The report-only retry-queue correction in
candidate source 5df binds retry HTTP attempts to their actual same-worker
execution; the historical retry queue interval included prior HTTP/backoff
time and is not server queue evidence. The correction does not change offered
load, concurrency, acceptance thresholds or security behavior, and the
candidate is not credited as deployed.

Exact-SHA Security/build run
[`38630875202`](https://github.com/StrayForest/old_sparky/actions/runs/38630875202)
for source `5dfb95c211ed37d93fde31201cb768560b1fd6c8` completed with 16
successful jobs and 2 expected skips. No eligible backup reservation preceded
its all-job maximum, so this run cannot prove a fresh backup. The saved native
observation for automatic chain
[`38632646564`](https://github.com/StrayForest/old_sparky/actions/runs/38632646564)
was still at the exact-source backup-verification step, before production
dispatch; this checkpoint contains no terminal chain result, restore/Alembic
pass or deployment proof. Earlier failed backup observations remain distinct:
one measured a 13,541,376-byte reserve shortfall at the captured controller
poll; a separate partial resource capture was 17,543,168 bytes below the
1 GiB reserve. Do not infer OOM, a precise later phase, or a common cause from
these observations.

Separate bounded local resource reads on 2026-10-14 observed `MemAvailable`
of 1,013,882,880 bytes, then 1,069,637,632 bytes in a later lightweight read;
both were below the 1 GiB reserve and are not an admission pass. A worker
cgroup read observed `memory.current=291,667,968` and `memory.max=1,073,741,824`
with zero recorded OOM counters. The read was partial because
`cpuset.cpus.effective` was absent; effective host CPU capacity and host
attribution remain unknown. A bounded task-path process scan was also
partial, with 157/157 numeric PID entries visited, zero exact selector matches,
and 103 `/proc/<pid>/exe` lookup errors reported as ENOENT; affected processes
and the cause were not identified. It does not establish historical process
completion. No tmpfs object has proven cleanup ownership; protected and unknown
roots remain retained.

External browser/Turnstile human QA is **NOT RUN**. Restore/Alembic, automatic
production deployment, the remaining six profiles, final CPU/automated QA,
retention/maintenance and external gate closure are not established by this
checkpoint. A later bounded source-prefix read found no currently loaded
matching scratch units; it did not establish absence of mountpoints/files or
cleanup ownership. Protected and unknown roots remain untouched pending new
source-bound evidence.

## Backup and restore follow-up (2026-10-15)

Two later source-bound backup attempts failed the unchanged 1 GiB memory
reserve guard: run `38662792582` reported a 786,432-byte deficit and run
`38666349694` reported a 5,001,216-byte deficit. The latter used the paired
scratch PostgreSQL `shared_buffers=32MB` initialization/start candidate, which
the [backup and restore runbook](../backup-restore-runbook.md) records as
reverted to the prior paired 64 MiB setting. It did
not complete restore/Alembic verification, so there is no causal comparison,
measured memory-saving result or demonstrated effect on the reserve refusal. Keep
durability, authentication, concurrency, confinement, tmpfs, MemoryMax,
host-reserve and timeout settings unchanged. The evidence does not establish
OOM or a common cause for the failures.

Security run `38671815760`, attempt 2, passed all 18 jobs for source
`16d956c63c1b0360d147b7916c9e513fe9b4876c` (16 success, 2 expected skips).
The one bound backup create, run `38722295410` attempt 1, failed the unchanged
1 GiB reserve in `restore_platform`: the observed `MemAvailable` was
1,066,323,968 bytes, 7,417,856 bytes below threshold. Scratch cleanup is
confirmed, while restore/Alembic completion is UNKNOWN. No pair was produced,
so the automatic deployment chain did not run; the prior 20 pairs and four
protected roots remain preserved. One cgroup observation recorded
`memory.current=494,878,720`, `anon=36,368,384`, `file=452,292,608`, and
`shmem=416,301,056` bytes, with zero reported high/max/oom/oom_kill events at
that sample. This is not a peak or causal attribution. That attempt's report
did not retain aggregate PGDATA/base/pg_wal allocation; the later run below
did. `tree.data` includes nested `base` and `pg_wal`; these are overlapping
views and must not be added. Attribution `complete` means the expected samples
were collected, not that restore, Alembic, or backup verification succeeded.

The bound create `38738894181`, attempt 1, used source
`a9360edb0c914f3240654ac4d9755df968db115b` and failed the same 1 GiB
`parent_active_poll` reserve guard. `MemAvailable` was 1,063,047,168 bytes,
10,694,656 bytes below threshold. Scratch cleanup was confirmed; restore and
Alembic completion remain UNKNOWN, no backup pair was produced, and the natural
auto-deploy parent failed its source-bound backup guard without dispatching a
child. The original 20 backup pairs and four protected roots remain preserved.
The private attribution was partial because no restore-end sample was
available. Its phase points were:

| Sample | Offset / interval | MemAvailable | 512 MiB tmpfs used | `tree.data` logical / allocated (files / dirs) | `base` allocated | `pg_wal` allocated (files) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `initdb_end` | 1.698 s | 1,392,975,872 B | 40,075,264 B | 40,029,512 / 40,075,264 B (968 / 26) | 22,585,344 B | 16,777,216 B (1) |
| `postgres_start_end` | +0.185 s | 1,386,594,304 B | 41,082,880 B | 41,018,434 / 41,074,688 B (971 / 26) | 22,585,344 B | 16,777,216 B (1) |
| `restore_platform_start` | +0.384 s | 1,381,892,096 B | 43,540,480 B | 48,974,437 / 43,532,288 B (1,272 / 27) | 24,985,600 B | 16,777,216 B (1) |
| `parent_last_guard` | +9.760 s (6 ms guard lag) | 1,063,047,168 B | 309,264,384 B | 309,979,749 / 310,050,816 B (1,368 / 27) | 157,286,400 B | 150,994,944 B (9) |

From restore start to the last guard sample, tmpfs usage rose by 265,723,904
bytes, `base` allocation by 132,300,800 bytes, and `pg_wal` by 134,217,728
bytes. These are interval observations, not an atomic peak or proof that WAL
alone caused the reserve refusal; `tree.data` already contains both nested
subtrees. The source selects PostgreSQL 16 but does not record the patch
version used by this attempt. It also passes no WAL or checkpoint overrides.
PostgreSQL 16 documents default `max_wal_size=1GB` as a soft automatic
checkpoint threshold, `min_wal_size=80MB` as a checkpoint recycling floor,
and a five-minute default checkpoint timeout
([WAL configuration](https://www.postgresql.org/docs/16/runtime-config-wal.html)).
Those defaults are large relative to this 512 MiB scratch tmpfs and the roughly
10-second restore interval, while nine 16 MiB WAL segments were present at the
guard sample. This supports a scratch-only checkpoint/recycling experiment,
not a guaranteed WAL cap or memory saving.

The reviewed local source candidate adds `max_wal_size=64MB` and
`min_wal_size=32MB` only to the disposable scratch PostgreSQL startup. It
leaves `wal_level`, `fsync`, `full_page_writes`, `synchronous_commit`, all
resource guards and timeouts unchanged. These settings may cause more
checkpoint I/O/CPU; `max_wal_size` is not a hard cap, and a lower WAL recycling
floor is useful only after checkpoints complete. The candidate is not deployed
and has no restore/Alembic result yet. Do not treat telemetry completion or a
smaller observed `pg_wal` as backup success, capacity admission, or an
application throughput result.

The published WAL-only candidate's bound create `38746516837/1` then failed the
same reserve guard by 2,363,392 bytes. Its last partial scratch sample recorded
240,975,872 allocated bytes in `base`, 67,108,864 in `pg_wal`, and 309,854,208
in `tree.data`; `tree.data` already includes both child paths. Cleanup was
confirmed, while restore/Alembic completion and the full scratch peak remain
UNKNOWN.

The next bound create, `38759048702/1` on source
`6f46b43da63f19ce0ae8ede29712d79e70d3c46e`, failed in `public_summary` with
exit 1. The helper returned success and the closed report validator accepted
the restore/Alembic success fields, but the external workflow failed; the
normalizer cause remains UNKNOWN because its stderr was redirected and the
report body was not retained. The natural auto-deploy guard failed without
dispatching a child. A metadata-only inventory found 21 dump/sidecar pairs and
stat-matched the 40 original entries to their saved nine-field baseline. The
new sidecar declares restore/Alembic verification, but its archive SHA was not
recomputed and it is not cryptographically bound to this run. This is not an
external backup success. A private diagnostic records normalizer exit,
bounded stdout/stderr lengths and hashes, fixed classes, and allowlisted
exception class; it does not change acceptance. Its stdout readback cap is
1,056,768 bytes and its stderr readback cap is 65,536 bytes; both are checked
after the normalizer writes temporary files, so they bound diagnostic reads and
hashing, not temporary-file growth.

The later create `38781187992/1` on source
`3f42b5686dc683418653c7931a4d8952f29bc6ce` also failed at `public_summary`
after the closed report validator accepted the helper report; the success
receipt was skipped. Its private normalizer diagnostic recorded zero stdout
bytes and 58 stderr bytes with SHA-256
`1b6eab82de5b2e21bfe75bbde1b6fbc8354135fbd6c8349b676eec57c0e94970`.
Offline execution of the exact remote caller reproduced those bytes: the
remote Bash script runs with `nounset`, receives the two cache flags as
positional arguments and assigns lowercase variables, while the normalizer
call expanded unassigned uppercase names. Bash exited before starting Python,
leaving stdout empty. The local correction passes the already validated
lowercase flags and does not alter summary validation, backup acceptance,
retention or restore behavior. The reviewed local fix remains unpublished and
undeployed; the regression is in
`PlatformStorageMaintenanceTests.test_manual_backup_uses_lock_aware_backup_only_workflow`
under `backend-privileged`, and no new bound CREATE or retry was dispatched.
This explains only run
`38781187992/1`. The cause of the preceding `38759048702/1` remains UNKNOWN,
and the original confinement/restore failure `38261983678` also remains
UNKNOWN; do not relabel either as this callsite bug.

At this 2026-10-14 checkpoint, the external 11-profile matrix stood at 5/11
complete; this workflow repair added no load-profile result or server-capacity
evidence. Human browser and Turnstile QA, the remaining six profiles, separate
CPU validation, final QA,
and healthy maintenance-timer completion remain outstanding.

A read-only snapshot of the current live `platformdb` (2026-10-15 14:52:18
UTC, not bound to the failed archive) counted 36 platform physical relations:
`pg_table_size` 272,400,384 bytes, attached indexes 3,571,712 bytes and combined
relation total 275,972,096 bytes. Whole-database size was 286,825,495 bytes.
The visibility flags for GUCs and activity statistics were false, so those
returned fields are visibility-limited; this is a current database inventory,
not the failed archive's complete scratch footprint or a restore peak.

The disposable PG16.15 startup measurement found that `shared_buffers=16MB`
reduced fixed main shared-memory allocation by 53,411,840 bytes versus 64 MiB,
including derived `wal_buffers`. A rough scratch estimate is current platform
relation bytes plus the 40,075,264-byte empty `PGDATA` baseline, less its initial
16 MiB WAL, plus the observed 64 MiB WAL point: 366,379,008 bytes (349.41 MiB).
This is not an upper bound: the live snapshot is not archive-bound, the failed
scratch sample was partial, and `max_wal_size=64MB` is a soft checkpoint
threshold rather than a cap. The next local candidate sets `shared_buffers=16MB`
consistently in `initdb` and server startup for the disposable restore cluster
only. A smaller cache may increase restore I/O or CPU; full restore/Alembic
success remains unproven. WAL policy, durability settings, resource guards,
confinement and timeouts stay unchanged.

The source-pinned provider-only repair restored the attested legacy
`19603dfff122386726313c86b0350812ebbe8cca` LiveQA runtime provider referenced
by protected consumers. The installed 9ce provider verifier passed under the
canonical release lock; current/previous application pointers were preserved.
This was not an application deployment or end-to-end QA pass.

After exact source and reference checks, two duplicate tmpfs artifacts were
removed individually while byte-identical private disk copies and manifests
were retained through final QA: release ZIP run `11660433667` (72,206,034
bytes, SHA-256 `7d1b4ed44559c144d58835baa2a3808f8be15c1f8f88c8f70c53bfa4e03b52e0`)
and source tar run `11674976728` (72,302,841 bytes, SHA-256
`785f31186d922f86176eaba22b7be2e1ed89fcf517eab943943dc3da15f4427f`). The
source73 reference report remains PARTIAL because its outer collector return
was exit 1 with truncated stdout; the independently preserved inner report
and offline comparison support only the exact provenance/checksum marker
pair. A separate COMPLETE FD/maps/map_files scan covered the exact tar inode
at one current point, not future opens or an atomic lease. All 20 backup pairs,
four protected `/run` roots, unrelated siblings, historical 65a, and retained
copy/manifest evidence remain preserved.

A post-cleanup point read recorded MemAvailable `1,302,296 kB`, `/dev/shm`
available `1,158,631,424 bytes`, root filesystem available `7,858,356,224
bytes`, and memory PSI some/full avg10 `0.00`. These observations are not an
admission pass or root-cause finding. A fresh locked provider verification
passed, but restore/Alembic, normal automatic deployment, the remaining six
profiles, separate CPU validation, browser/Turnstile QA and timer completion
remain NOT RUN / UNKNOWN. Preserve the existing 1 GiB reserve and all profile
thresholds.
