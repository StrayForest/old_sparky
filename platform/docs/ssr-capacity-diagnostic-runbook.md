# Production SSR capacity diagnostic

- Status: Active operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-30

Use this runbook for the zero-credit authenticated SSR composite diagnostic.
It runs one unprofiled 120-second capacity window, then a separate profiled
20-second attribution window against the same 14-actor fixture. The diagnostic
does not change runtime settings and does not earn SLO, capacity, or release
acceptance credit.

## Release and host readiness

Run only after the source fix and its operational instructions pass the full
exact-head CI route. For a runtime-sensitive source change:

1. Merge the reviewed change through the normal `dev` route. Record the exact
   merged source SHA `S` and its successful Security run and attempt.
2. Start exactly one source-bound TMPFS backup for that same `S` and Security
   run/attempt while the Security run is active. Confirm the backup production
   job started before the latest required Security job completed. Require the
   backup receipt to bind `S`, the Security identity and the backup run; require
   create, restore, Alembic and scratch-cleanup success.
3. Let the normal auto-deploy parent verify its signed host-tools bundle and
   attestation. Install a new host-tools generation only from that normal,
   source-bound parent artifact. A pull-request candidate artifact is never
   production authority. Keep runner source `S`, active app source `A`, and
   host-tools generation `C` distinct in every receipt.
4. Let the normal deployment child install the app. Require strict shared-venv
   reuse, the exact `S` release marker, the expected previous-release pointer,
   healthy `deadlock-api`, `deadlock-web`, and `deadlock-worker`, and no pending
   operation or diagnostic lock. Do not manually install or dispatch a normal
   production deployment.
5. Confirm the diagnostic profile is active and the exact app source is
   deployed. Run these read-only checks from the authorized host session; pass
   the expected deployed app SHA as a public value. They print only selected
   systemd fields, release identity and allowlisted runtime settings. The
   preflight filters the process environment to the listed keys, emits only
   those allowlisted values, and never opens or prints the shared environment
   file:

   ```bash
   set -euo pipefail
   systemctl is-active deadlock-api.service deadlock-web.service deadlock-worker.service
   curl --fail --silent --show-error --max-time 5 --output /dev/null http://127.0.0.1:8010/api/v1/health/ready
   curl --fail --silent --show-error --max-time 5 --output /dev/null http://127.0.0.1:3000/
   /usr/bin/python3 -I -B - '<expected-app-sha>' <<'PY'
   import json
   import re
   import subprocess
   import sys
   from pathlib import Path

   expected_sha = sys.argv[1]
   if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
       raise SystemExit("expected app SHA is invalid")
   release = json.loads(Path("/opt/oldsparky/platform/current/RELEASE.json").read_text(encoding="ascii"))
   if release.get("source_git_commit") != expected_sha:
       raise SystemExit("active release source differs from expected app SHA")

   properties = "ActiveState,MainPID,ControlGroup,User,Group,InvocationID,NRestarts"
   units = {
       "deadlock-api.service": ("oldsparky-api", "oldsparky-api"),
       "deadlock-web.service": ("oldsparky-web", "oldsparky-web"),
       "deadlock-worker.service": ("oldsparky-worker", "oldsparky-worker"),
   }
   selected_env = {
       "deadlock-api.service": {
           "PLATFORM_API_WORKERS": "2",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED": "true",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY": "24",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS": "8",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS": "250",
       },
       "deadlock-web.service": {
           "PLATFORM_WEB_WORKERS": "1",
           "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "fetch",
       },
   }
   web_defaults = {
       "PLATFORM_WEB_WORKERS": "1",  # platform_run_web.sh
       "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "fetch",  # server-auth.ts
   }
   web_allowed = {
       "PLATFORM_WEB_WORKERS": {"1", "2"},
       "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": {"fetch", "node"},
   }
   result = {"release_slug": release.get("release_slug"), "source_git_commit": expected_sha}
   for unit, (expected_user, expected_group) in units.items():
       raw = subprocess.check_output(
           ["systemctl", "show", unit, *(f"--property={name}" for name in properties.split(","))],
           text=True,
       )
       fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
       pid = int(fields.get("MainPID", "0"))
       if (fields.get("ActiveState") != "active" or pid <= 0
               or fields.get("User") != expected_user or fields.get("Group") != expected_group):
           raise SystemExit(f"service identity or state is invalid: {unit}")
       row = {name: fields.get(name, "") for name in properties.split(",")}
       expected = selected_env.get(unit)
       if expected is not None:
           environment = {}
           for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
               key, separator, value = item.partition(b"=")
               if separator and key.decode("ascii", "strict") in expected:
                   name = key.decode("ascii", "strict")
                   if name in environment:
                       raise SystemExit("duplicate allowlisted runtime key")
                   environment[name] = value.decode("ascii", "strict")
           effective = dict(environment)
           value_sources = {name: "environment" for name in environment}
           if unit == "deadlock-web.service":
               for name, default in web_defaults.items():
                   if name not in environment:
                       effective[name] = default
                       value_sources[name] = "launcher_default"
                   elif environment[name] not in web_allowed[name]:
                       raise SystemExit(f"unsupported explicit runtime value: {name}")
           if effective != expected:
               print(json.dumps({
                   "unit": unit,
                   "expected_allowlisted_runtime": expected,
                   "observed_allowlisted_runtime": environment,
                   "effective_allowlisted_runtime": effective,
                   "value_sources": value_sources,
               }, sort_keys=True, separators=(",", ":")))
               raise SystemExit(f"runtime profile values differ: {unit}")
           row["allowlisted_runtime"] = {
               "observed": environment,
               "effective": effective,
               "value_sources": value_sources,
           }
       result[unit] = row
   print(json.dumps(result, sort_keys=True, separators=(",", ":")))
   PY
   ```

   Record `runtime_profile` from the exact successful deployment receipt; the
   live checks above confirm its effective API and web values. Missing web
   environment keys use only the exact S2 launcher/application defaults shown
   in the script and are labeled `launcher_default`; unsupported explicit
   values fail. The check emits only allowlisted process-environment values;
   it never opens a shared environment file or prints other environment data.
   Record each
   `NRestarts` value and compare it after the diagnostic and cleanup; any
   increase invalidates the measurement. Stop if a source, service identity,
   profile value or readiness check differs.

The source-bound backup and release admission rules are in the
[backup and restore runbook](backup-restore-runbook.md) and
[deployment runbook](deployment-runbook.md). Never use a PR artifact to fill a
production source, backup, host-tools or release identity.

## Dispatch one diagnostic

Confirm `dev` points to the merged source SHA and that no load, maintenance,
release, abort, or cleanup operation is active. Dispatch the external-load
workflow with all workflow inputs represented as strings. The control account
is read by a hidden prompt and sent only in the JSON request body on stdin:

```bash
set -euo pipefail
python3 - <<'PY' | gh api --repo StrayForest/old_sparky --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-external-load.yml/dispatches \
  --input -
import getpass
import json
import sys

payload = {
    "ref": "dev",
    "inputs": {
        "confirmation": "RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC",
        "control_email": getpass.getpass("Control account email: "),
        "profile_id": "authenticated-page-load-v1",
        "timeout_diagnostics": "false",
        "cpu_diagnostic_pair": "false",
        "cpu_diagnostic_only": "false",
        "ssr_capacity_diagnostic_only": "true",
        "ssr_capacity_concurrency": "32",
        "ssr_capacity_workload": "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1",
    },
}
json.dump(payload, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY
```

The email must not be placed in shell arguments, environment variables, files,
workflow logs, or artifacts. Reconcile the one newly created run by workflow,
event, branch and exact `head_sha`; record its run ID and attempt before
watching it:

```bash
gh api --repo StrayForest/old_sparky \
  'repos/StrayForest/old_sparky/actions/workflows/platform-production-external-load.yml/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-load-run-id> --repo StrayForest/old_sparky --exit-status
```

Do not dispatch a second run after an uncertain response. Reconcile the run
list and artifacts first. Do not download the private input artifact; review
only the sanitized report, manifest, phase evidence, and cleanup/abort receipts.

## Decide whether the measurement is complete

`report_complete` is an envelope marker, not acceptance. Count a measurement
complete only when the report and canonical validators bind the exact run,
attempt, source, profile, workload, concurrency, fixture manifest and phase
identities, and all of these conditions hold:

- The unprofiled capacity phase completes its full 120-second ON window with
  every submitted request accounted for, successful HTTP status and HTML
  validation, complete required SSR request identity/timing and origin CPU
  evidence, no abort, and successful plan cleanup.
- The separate profile phase completes its 20-second ON window against the
  same fixture manifest after the planned gap. Its profile data is attribution
  only; do not merge it into capacity-window latency or CPU results.
- Both phase records, the top-level completion state and the full source and
  fixture bindings pass their closed validators. The phases fit within the
  600-second outer deadline, and finalization and exact fixture cleanup pass.
- No active run-owned process, diagnostic plan, lock, fixture export, or run
  root remains after the canonical cleanup workflow succeeds.

The capacity phase is descriptive and has no latency or throughput threshold.
Any missing phase, request identity, required metric, cleanup proof, binding,
or deadline condition makes the composite incomplete; retain the partial
observations as partial and assign no capacity, SLO, or matrix credit. The
profiled phase is never capacity evidence. Do not report function-level
attribution from aggregate-only summaries; the V8 sampling interval is
100 milliseconds and API thread CPU excludes other threads.

## Abort and close the exact fixture

If the selected run has an active or uncertain remote process tree, use the
canonical abort workflow for that exact load run and attempt. `target_sha` is
the active app SHA `A`; the abort workflow resolves the runner SHA from the
selected load run. Supply the successful Security run and attempt for the
current `dev` source. The request contains no control-account value:

```bash
gh api --repo StrayForest/old_sparky --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-retained-load-abort.yml/dispatches \
  --input - <<'JSON'
{"ref":"dev","inputs":{"confirmation":"ABORT-PRODUCTION-RETAINED-LOAD","load_run_id":"<exact-load-run-id>","load_run_attempt":"<exact-load-attempt>","target_sha":"<active-app-sha-A>","security_run_id":"<successful-security-run-id>","security_run_attempt":"<successful-security-attempt>"}}
JSON
gh api --repo StrayForest/old_sparky \
  'repos/StrayForest/old_sparky/actions/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | select(.path == ".github/workflows/platform-production-retained-load-abort.yml") | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-abort-run-id> --repo StrayForest/old_sparky --exit-status
```

Reconcile and review the exact abort receipt. It must bind the load run and
attempt, runner source, active app source, fixture manifest and Security
identity; require the selected process tree absent and shared lock clear. An
abort receipt with `namespace_closed: false` does not prove namespace closure
and grants no measurement credit. Proceed to cleanup only when the canonical
abort normalizer permits exact-fixture cleanup.

Then dispatch exact cleanup. Enter the same control account at the hidden
prompt; the email travels only in the JSON request body on stdin:

```bash
set -euo pipefail
python3 - <<'PY' | gh api --repo StrayForest/old_sparky --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-retained-load-cleanup.yml/dispatches \
  --input -
import getpass
import json
import sys

payload = {
    "ref": "dev",
    "inputs": {
        "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD",
        "load_run_id": "<exact-load-run-id>",
        "control_email": getpass.getpass("Control account email: "),
    },
}
json.dump(payload, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY
gh api --repo StrayForest/old_sparky \
  'repos/StrayForest/old_sparky/actions/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | select(.path == ".github/workflows/platform-production-retained-load-cleanup.yml") | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-cleanup-run-id> --repo StrayForest/old_sparky --exit-status
```

Review the cleanup run's exact source-binding, summary, diagnostic and
canonical-log artifacts. Require `ok=true`, control account preserved, the
expected fixture rows deleted, zero scoped residuals as enforced by the
canonical helper, Redis read-model cleanup, exact run-root/export/marker
removal, successful remote and workflow statuses, and a passed untruncated
canonical log. The helper's zero-residual enforcement is the database and
Redis closure proof; do not substitute manual SQL, Redis commands, filesystem
deletion, or a green job conclusion. If any required artifact is absent,
truncated, mismatched or failed, stop and retain the evidence for exact-run
recovery. Never retry cleanup blindly.

## Owner references

- [Retained-load cleanup and hermetic web verification](retained-load-and-web-verification.md)
- [Performance contract and evidence](../performance/README.md)
- [Abort and cleanup workflows](../../.github/workflows/platform-production-retained-load-abort.yml) and [cleanup workflow](../../.github/workflows/platform-production-retained-load-cleanup.yml)
