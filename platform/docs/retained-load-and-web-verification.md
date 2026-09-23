# Retained-load and web verification

- Status: Active reference and operator how-to
- Owner: Performance and web verification owners
- Last reviewed: 2026-09-19

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
| measured load | authoritative `load_status=0`; report present |
| remote/finalization | `remote_status=0`, `observer_ready=1`, `finalize_status=0` |
| cleanup/export cleanup | `cleanup_status=0`, `cleanup_exports_status=0` |
| handoffs/artifacts/SSH | exact SHA/run/attempt/digest; cleanup statuses `0` |
| evaluation/projection | `evaluation_status=0`, `sanitizer_status=0` |

The evidence artifact is published only after every row passes; missing or
mismatched artifacts and remote, projection, sanitizer or cleanup failures
cannot be hidden by the evaluator.

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
