# Retained-load and web verification

- Status: Active reference and operator how-to
- Owner: Performance and web verification owners
- Last reviewed: 2026-10-10

This document owns the detailed retained-load cleanup, hermetic web verification,
external-load workflow barrier and evidence-projection contracts. The
[operations runbook](operations-runbook.md) owns the surrounding operator
procedure and links here rather than duplicating these contracts.

## Retained-load cleanup and hermetic web verification

The exact retained-load supervisor deletes each selected tournament's four
rebuildable read-model keys (`teams`, `workspace_detail`, `bracket_summary`,
`bracket_full`) and rechecks them; zero residual keys is mandatory. A Redis
outage, failed delete or surviving key fails closed even though ordinary reads
may treat Redis as optional. It also verifies zero fixture users, tournaments,
sessions and audit rows while preserving the control account. The [cleanup
tool](../tools/platform_cleanup_retained_matrix.py) calls `dispose_engine()` and
`dispose_redis_clients()`; close warnings are failures, not harmless noise.

The canonical [hermetic runner](../tools/platform_web_hermetic.sh) builds one
standalone Next artifact in a temporary immutable directory, runs the
source-only contract once, then runs smoke and participant suites sequentially
from that build. The source contract's
[`playwright.source-contract.config.ts`](../apps/platform_web/playwright.source-contract.config.ts)
has no `webServer`; smoke/participant use fresh API/web processes and
`reuseExistingServer: false`, with no ambient server/browser/database reuse.
Responsive ownership is `desktop` (1440), `wide-1300` (1300), `tablet-820`
(820) and `mobile-layout` (Pixel 5); the explicit desktop-only list runs only
in `desktop`, while participant has its own one-worker desktop/mobile projects
([configs](../apps/platform_web/playwright.config.ts), [participant
config](../apps/platform_web/playwright.participant.config.ts)).

Approved load generators may be listed in the exact-address setting
`PLATFORM_LOAD_TEST_SOURCE_IPS`. The allowlist skips only per-source/IP
throttles for authentication, invite and media paths; account, authenticated
user, byte, application-global and Nginx capacity limits remain active. It
accepts individual IPv4/IPv6 addresses only, never a CIDR or wildcard.

## External-load workflow and evidence

The workflow is fail-closed across separate runners; finalization, exact cleanup
and SSH removal use `always()`, but any failed row below keeps the run failed:

| Boundary | Passing value |
| --- | --- |
| dispatch, setup and load-client jobs | job result `success`; setup `setup_status=0` |
| candidate production | schema-2 receipt binds source/run/attempt/profile and exact report digest; `pending_origin` is accepted only for the load tool's reserved exit 3 after closed worker/namespace/binding checks, including a typed status-failure check when `contract_ok` is false |
| remote/finalization | `remote_status=0`, `observer_ready=1`, `finalize_status=0` |
| cleanup/export cleanup | `cleanup_status=0`, `cleanup_exports_status=0` |
| handoffs/artifacts/SSH | exact SHA/run/attempt/digest; cleanup statuses `0` |
| evaluation/projection | observer-bound accepted result, structurally complete profile-budget miss, or closed known-status failure; `sanitizer_status=0` |

A structurally complete budget miss may pass through origin attachment and
sanitization so its evidence is retained, but final workflow enforcement still
fails the run. A status `0` or `500` result may use the same reserved candidate
handoff only when all raw, logical and phase status counts, error-class totals,
source/run/profile bindings, timing, containment and population evidence close
exactly. Its `contract_ok` and `passed` fields remain false; origin attachment
and sanitization retain the failed measurement, and final workflow enforcement
still fails the run. Unknown statuses or error classes, extra failed checks,
incomplete counts, partial work, observer failures, cleanup failures or
artifact-identity mismatches remain invalid pipeline results and cannot be
reclassified as completed failures.

The optional CPU diagnostic pair removes its run-bound SSH files before the
summary is uploaded to Actions. Cleanup runs even after a pair failure; the
sanitized failure summary may still be retained when cleanup succeeds, while a
cleanup failure blocks third-party upload and leaves the run failed.

The QA system sampler keeps full process identity and CPU collection for all
`/proc` rows while reading per-process RSS and I/O counters only for the seven
groups it reports. The generic process iterator still collects the full
record by default. A synthetic 189-process `/proc` fixture over ten iterations
reduced RSS/I/O file reads from 3,780 to 140 and local fixture wall time from
0.760 s to 0.374 s. This is a fixture benchmark, not a production CPU or
under-load measurement; it does not establish a production performance gain.
The change remains a measured optimization hypothesis until the R load matrix
provides comparable observer evidence.

Artifact handoffs carry the exact artifact ID across jobs. Each consumer then
reads the authenticated Actions artifact metadata and requires the expected
run-derived name (including the run attempt), artifact ID, unexpired state,
workflow run ID and source SHA. The metadata's `sha256:` digest must be exactly
64 lowercase hexadecimal characters and must match the downloaded ZIP bytes;
missing metadata or any mismatch fails closed. Consumers do not depend on
cross-job digest outputs, which Actions may suppress when a value is treated as
secret-like. Every verifier receives its SHA from that job's declared source
identity; the evaluator uses `SOURCE_GIT_SHA` for both artifact metadata and
the candidate receipt binding.

The candidate receipt is not an acceptance result. The independent evaluator
validates its exact schema and report digest, then binds the origin observer and
decides the measured profile. A complete result whose only failures are
profile-owned budgets publishes the sanitized measurement artifact and leaves
the overall workflow failed. Missing or mismatched receipts, invalid or
incomplete measurements, observer failures, and remote, projection, sanitizer
or cleanup failures remain hard failures and cannot be hidden by the evaluator.

The measured client is supervised in a mandatory Linux PID namespace. The only
privileged chain is the absolute system path `/usr/bin/sudo -n
/usr/bin/setpriv --pdeathsig SIGKILL -- /usr/bin/unshare --pid --fork
--mount-proc --kill-child=SIGKILL`; no checkout-controlled helper is executed
as root and user-namespace flags are deliberately absent. Inside the namespace
a second absolute `setpriv` immediately drops to the original runner UID/GID,
clears groups, applies `--no-new-privs`, and clears inheritable, ambient and
bounding capabilities before it execs the Python worker as PID 1. A validation
preflight on the pinned `ubuntu-24.04` runner probes that exact non-root,
stdin/stdout contour before any production fixture setup, and the load runner
repeats the probe before candidate execution. The runtime also performs the
same pure preflight before it creates report directories, removes stale paths,
writes config, or creates its worker temporary directory; a root/local
rejection is an in-memory error/exit and cannot mutate the caller's report
path. Probe failure is a closed non-authoritative result and never falls back
to a process group or an uncontained worker. The probe and namespace entry
machine-check real/effective/saved UID/GID, supplementary groups, PID 1/PPID 0,
all capability fields and `NoNewPrivs=1`.

The scenario deadline and whole-runner deadline select one absolute monotonic
wall deadline. A bounded reserve inside that deadline covers wrapper wait,
`TERM`/`KILL`, captured-chain and namespace reaping, child-report read/validation
and atomic publication; it includes a one-second post-watchdog-KILL reap
interval for the hosted sudo monitor to reparent and for PID1/captured-chain
reap, in addition to the configured TERM grace. Every phase checks the
deadline before and after its bounded work; a short authored window fails
closed rather than borrowing time from teardown. A blocked DNS/socket/body read or future is terminated with
`TERM`, then `KILL` after only the remaining grace. The supervisor tracks the
complete wrapper chain and namespace PID by pidfd/start-time, waits for the
wrapper, reaps a zombie namespace PID 1 when necessary, and requires namespace
closure before publishing the final report. The final acceptance gate is
adjacent to atomic publication: a teardown, parser or write overrun always
preserves the primary deadline reason and can never produce `reason=none` or a
success envelope. Every report has mandatory boolean
`namespace_closed`; only the parent may set it true after closure. A malformed
or killed worker is represented by a failed report with partial/in-flight-
unknown flags; the primary timeout/containment reason remains intact even when
the child report is absent. The `always()` fixture finalizer has a separate
fail-visible namespace barrier: cleanup never begins when the field is missing
or false and a manual emergency action is required instead. Candidate report
upload is independent of the client exit code, so a runtime timeout cannot
skip cleanup or leave a background load mutating the fixture while cleanup
begins.

The hosted containment canary itself creates a TERM-ignoring `setsid`/double-
fork/nested descendant tree, records each outer PID and `/proc` start-time, and
proves heartbeat and every captured identity stop after closure. Because a
setuid sudo exec may clear a parent-death signal, the non-root
watchdog keeps the supervisor pidfd outside the privileged chain and
identity-signals the entire captured sudo/setpriv/unshare descendant chain if
that pidfd closes. This is a reclaim guard, not a process-group fallback; the
watchdog wraps sudo in the absolute system `setpriv --pdeathsig SIGKILL`
where the system implementation preserves that signal. PID1 pidfd signalling,
start-time checks and bounded full-chain reap remain mandatory proofs when
setuid sudo clears it; the namespace worker and inner setpriv also arm
`SIGKILL` parent-death handling.
If the watchdog's identity-pinned `pidfd_send_signal` returns `EPERM` during a
parent-alive termination, it may skip only a PIDFD/start-time-pinned process
whose `/proc/<pid>/status` proves effective UID 0 and an exact `Name` of
`sudo`, `setpriv` or `unshare`, with exactly one `NSpid` entry (outer PID
namespace only). The watchdog itself forked and execs only the fixed absolute
system chain above. It continues
signalling the remaining captured identities, and the parent still requires
independent wrapper and PID 1 closure proof before publishing a report that
claims namespace closure or allowing cleanup to proceed. This skip is
disabled on the parent-death path; unreadable, malformed, non-root or
unknown identities remain fail-closed. The bounded
`runtime_supervisor.watchdog_error` enum records either
`pidfd_signal_eperm_trusted_mediator_skipped` or
`pidfd_signal_eperm_unclassified` without exposing PIDs or paths.

Load/QA evidence is a fixed, privacy-bounded set: route classes/templates,
numeric timings/counts/statuses and allowlisted error/backend/wait classes. It
excludes control/operator email values, slugs, raw URLs/queries, request or
diagnostic IDs, user digests, edge IP/location, Cloudflare ray values and raw
PostgreSQL SQL. A control account is represented only by
`control_account_preserved: bool`.

Remote output is redirected to a mode-0600 temporary file, reduced to one
bounded canonical summary, and deleted before upload. External-load,
server-observability and timeout-diagnostics JSON each use an explicit
closed-schema projector after private evaluation; public files carry
`public_projection: true` and `authoritative: false`. Exact fixture/run/process
identities remain private for evaluation/cleanup only; required evidence uses
`if-no-files-found: error`, 14-day maximum retention; missing observer/cleanup
evidence fails. Dispatch inputs use the canonical bounded-ASCII parser and
private mode-0600 JSON stdin; SSH carries a fixed dispatcher mode and no raw
input enters reports or artifacts.

When the PID-namespace worker exits normally after catching an exception, the
parent report retains a closed worker-failure discriminator (stage, exception
class, allowlisted module, and numeric source line). The parent still records
the worker's nonzero exit and partial/in-flight status; namespace closure does
not convert that report into a pass. If the source-bound discriminator is
valid and the existing fixture, observer, namespace, and cleanup gates pass,
the failed report may be published for diagnosis while the workflow remains
failed. Exception text, traceback text, paths, and local values are never part
of the public projection.

The origin observer is a supported integration of the
[`platform_production_external_fixture_qa.sh`](../tools/platform_production_external_fixture_qa.sh)
workflow. That workflow enters the canonical retained-load supervisor before
starting the observer and keeps
`/run/lock/oldsparky-retained-load-matrix.lock` held through observer
completion, export and exit; the observer does not acquire a second lock.
The Actions concurrency group serializes this supported workflow, while the
host lock also covers deployment, cleanup and maintenance callers. Direct
observer invocation is outside the supported workflow integration.

When the diagnostic CPU-profile environment is enabled, worker signals are
sent only through Linux pidfds after the exact UID, parent and procfs
start-time identity checks. If either pidfd API is unavailable or delivery
fails, the observer refuses the signal and records a bounded reason; it still
retains its independent system, journal and database evidence. It never falls
back to numeric-PID signalling.

Profile artifacts are attributed only when their filename contains the exact
worker PID and procfs start-time generation captured for the window, the file
is new or changed after the observer baseline snapshot, and `pstats` can parse
it. Stale files, same-PID files from an earlier observer, wrong-generation
files and partial/unparseable files are excluded. The observer preserves all
caller-owned artifacts for the existing host retention procedure. Public
projection retains only bounded counts and fixed signal-rejection reasons; raw
PIDs, start times, identities and filesystem paths do not cross the artifact
boundary.
