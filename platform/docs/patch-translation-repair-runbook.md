# Patch translation cache repair

This procedure reparses only caller-selected patch details from their exact
current `v20` source cache entry or the supported `v19` source cache entry. It
registers translation rows without enqueueing Celery work, then runs the
existing bounded translator from this workflow. It does not fetch Steam,
enumerate the archive, flush Redis, or delete PostgreSQL translation rows.

## Manual exact-ID repair

Use this operation only after the matching application release is active in
production and its exact source IDs are still present in the source cache. The
workflow requires `dev` to point to the exact same source SHA as the active
production release. Check the active SHA in `platform/docs/CURRENT.md` and
confirm it against the production release receipt before dispatch. A mismatch
is an abort condition; do not dispatch normal production deployment manually.

The operator supplies one to six unique decimal Steam announcement IDs. The
list is an explicit input, not a maintained allowlist. Each source must exist
under its exact current `v20` detail key or supported previous `v19` source
key, have a positive remaining TTL, match the embedded ID, and pass source URL,
date, title, content and size validation. The helper reparses and schema-checks
only those IDs, writes only their `v20` detail keys, then registers their exact
source-hash/version rows with `enqueue=False`. The workflow rejects any
unexpected registration enqueue count and invokes the translator directly
within the supplied patch-level cache-miss budget. `translation_calls` charges
each cache-miss translation attempt before the direct call, whether it succeeds
or fails; it is not a provider HTTP request count. The service may use its
documented bounded correction/retry requests within each charged patch attempt.

For the six pages in the 2026-11-28 audit, use exactly:

```bash
gh workflow run platform-patch-translation-qa.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f max_openai_calls=6 \
  -f repair_patch_ids=1850460852992007,1850460852986472,1848664643342973,1848664643339845,1847920302240420,1846651706696737
```

For a smaller repair, pass only the intended IDs and set `max_openai_calls` no
higher than the number of selected IDs. The ordinary automatic warm-up keeps
its four-patch budget and reads exactly four IDs from the existing cached home
projection; it fails if that source is missing, expired, malformed or not four
unique valid IDs. It does not silently choose another feed or widen the repair
set.

### Success checks

1. `gh workflow run` returns a run ID. Inspect that exact run with
   `gh run view RUN_ID --repo StrayForest/old_sparky` and wait for success.
2. Confirm the public `translation_qa` summary reports `status=passed`, the
   expected number of checked patches, `records_enqueued=0`, and no translation
   failures. `records_registered` can be zero when exact rows already exist.
3. Confirm the bound active production SHA equals the run's target SHA and the
   exact selected detail pages render their Russian translation and refreshed
   structure. A successful workflow alone does not replace the separate
   post-deploy production content diagnostic for the current home four.
4. For this six-page task, verify all six selected details after the workflow;
   do not treat a four-page current-home diagnostic as evidence for the two
   historical pages.

### Abort and cleanup

Stop on any missing/expired source, source identity mismatch, schema failure,
nonzero enqueue metric, active-SHA mismatch, exhausted budget, translation
failure, or partial workflow result. Do not retry with alternate IDs, fetch an
announcement feed, call a low-level release tool, flush Redis, delete source
keys, or delete translation rows. The workflow's private temporary reports
and SSH material are removed by its exact-path cleanup steps. Preserve the
resulting Redis detail and PostgreSQL translation records; a failed repair is
investigated through the owning service/runbook and a new explicit bounded run
only after its cause is resolved.
