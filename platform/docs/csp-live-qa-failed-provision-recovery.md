# Public QA after fixture-provisioning failure

Use this documented recovery only when the automatic public launch failed in
fixture provisioning. The sequencer remains failed and receives no pass credit.
This procedure does not repair or retry its database operation.

## Run public QA with the retained bundle

Before dispatch, verify the fixed CSP bundle and active immutable
supervisor/helper, authenticate the installed app source, and prove no retained
recovery state, runtime gate or competing QA operation remains. Read the
current reviewed `dev` SHA and run the existing public workflow with
provisioning disabled and an empty marker:

```sh
gh api repos/StrayForest/old_sparky/commits/dev --jq .sha
EXPECTED_TARGET_SHA='<exact current dev SHA>'
gh workflow run .github/workflows/platform-live-launch.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f base_url=https://old-sparky.com \
  -f provision=false \
  -f marker= \
  -f expected_target_sha="$EXPECTED_TARGET_SHA"
```

The workflow requires `expected_target_sha` to equal the checked-out `dev`
commit. Stop if source or active runtime binding cannot be authenticated; do
not dispatch against a historical SHA. With `provision=false`, the trusted
supervisor skips account installation and roster provisioning. The public
suite still runs normal source, engine, recovery-state, runtime and browser
checks and does not consume roster credentials. This standalone result does
not change the failed sequencer outcome.

## Gate UserQA on exact preflight

Dispatch UserQA only after this public suite passes. Its canonical preflight
must validate the retained bundle/helper and exact active verified roster,
then prove there are no active sessions before fixture creation. If the failed
provision operation had an unknown publication/commit outcome, the bundle
file alone does not prove the database roster is intact. Stop on any failed or
ambiguous preflight and retain the exact state for canonical recovery. After
UserQA, require its exact cleanup and absence proof before considering this
independent follow-up complete. The failed sequencer remains failed and gets
no pass credit. Do not reprovision, bypass bundle-age checks, invoke the
provisioner directly or edit/delete records by marker.
