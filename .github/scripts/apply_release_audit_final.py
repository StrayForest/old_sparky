from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PIN = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
SSH_WORKFLOWS = (
    "platform-live-launch.yml",
    "platform-live-user-qa.yml",
    "platform-media-migration-diagnostics.yml",
    "platform-patch-translation-qa.yml",
    "platform-production-content-diagnostics.yml",
    "platform-production-deploy.yml",
    "platform-production-diagnostics.yml",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


ssh_old = '          ssh-keyscan -T 10 -H "$PROD_SSH_HOST" >> ~/.ssh/known_hosts'
ssh_new = f'''          ssh_scan="$(mktemp)"
          trap 'rm -f -- "$ssh_scan"' EXIT
          ssh-keyscan -T 10 -t ed25519 "$PROD_SSH_HOST" > "$ssh_scan"
          test -s "$ssh_scan" || {{ echo "Production SSH host exposed no ED25519 key" >&2; exit 1; }}
          ssh_key_count="$(wc -l < "$ssh_scan" | tr -d ' ')"
          test "$ssh_key_count" = "1" || {{ echo "Expected exactly one production ED25519 host key" >&2; exit 1; }}
          ssh_fingerprint="$(ssh-keygen -lf "$ssh_scan" -E sha256 | awk 'NR == 1 {{print $2}}')"
          test "$ssh_fingerprint" = "{PIN}" || {{
            echo "Production SSH host fingerprint mismatch" >&2
            exit 1
          }}
          install -m 0600 "$ssh_scan" ~/.ssh/known_hosts
          {{
            printf '%s\\n' 'Host *'
            printf '%s\\n' '  StrictHostKeyChecking yes'
            printf '  UserKnownHostsFile %s/.ssh/known_hosts\\n' "$HOME"
          }} > ~/.ssh/config
          chmod 600 ~/.ssh/config'''

for name in SSH_WORKFLOWS:
    path = WORKFLOWS / name
    text = read(path)
    text = replace_once(text, ssh_old, ssh_new, label=f"{name} SSH pin")
    write(path, text)

# Live launch: bind the entire provision/browser contour to the active immutable
# release and hold the common release lock while it runs.
path = WORKFLOWS / "platform-live-launch.yml"
text = read(path)
text = replace_once(
    text,
    '      LIVE_MARKER: ${{ inputs.marker }}\n',
    '      LIVE_MARKER: ${{ inputs.marker }}\n      TARGET_SHA: ${{ github.sha }}\n',
    label="live launch TARGET_SHA",
)
text = replace_once(
    text,
    '            bash -s -- "$LIVE_BASE_URL" "$LIVE_PROVISION" "$LIVE_MARKER" <<\'REMOTE\' | tee live-launch-report.txt',
    '            bash -s -- "$LIVE_BASE_URL" "$LIVE_PROVISION" "$LIVE_MARKER" "$TARGET_SHA" <<\'REMOTE\' | tee live-launch-report.txt',
    label="live launch remote args",
)
text = replace_once(
    text,
    '          marker="${3-}"\n          test "$(id -u)" -eq 0 || { echo "Live browser supervisor must run as root" >&2; exit 1; }',
    '          marker="${3-}"\n          target_sha="${4-}"\n          test "$(id -u)" -eq 0 || { echo "Live browser supervisor must run as root" >&2; exit 1; }',
    label="live launch remote target",
)
text = replace_once(
    text,
    '          test "$base_url" = "https://old-sparky.com" \\\n            || { echo "Unexpected production origin" >&2; exit 1; }\n          case "$provision" in',
    '          test "$base_url" = "https://old-sparky.com" \\\n            || { echo "Unexpected production origin" >&2; exit 1; }\n          [[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] \\\n            || { echo "Live browser QA requires an exact target SHA" >&2; exit 1; }\n          runtime=/opt/oldsparky/platform\n          guard="$runtime/current/tools/platform_release_lock_exec.sh"\n          test -x "$guard" || { echo "Production mutation guard is missing" >&2; exit 1; }\n          /bin/bash "$guard" \\\n            --app-dir "$runtime" \\\n            --expected-sha "$target_sha" \\\n            -- /bin/bash -s -- "$base_url" "$provision" "$marker" "$target_sha" <<\'LOCKED\'\n          set -Eeuo pipefail\n          base_url="$1"\n          provision="$2"\n          marker="${3-}"\n          target_sha="$4"\n          repo=/root/old_sparky\n          test -d "$repo/.git" || { echo "Trusted production QA checkout is missing" >&2; exit 1; }\n          test "$(git -C "$repo" rev-parse --verify HEAD)" = "$target_sha" \\\n            || { echo "Trusted QA checkout does not match the active target SHA" >&2; exit 1; }\n          case "$provision" in',
    label="live launch release guard",
)
text = replace_once(
    text,
    '          /root/old_sparky/platform/tools/platform_live_browser_qa.sh public\n          REMOTE',
    '          /root/old_sparky/platform/tools/platform_live_browser_qa.sh public\n          LOCKED\n          REMOTE',
    label="live launch guard close",
)
write(path, text)

# Destructive live-user QA already has its own fixture lock. Add the release
# lock as the outer serialization boundary and require the active SHA.
path = WORKFLOWS / "platform-live-user-qa.yml"
text = read(path)
text = replace_once(
    text,
    '          export PLATFORM_APP_DIR="$runtime"\n          export PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json\n          export PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com\n          /root/old_sparky/platform/tools/platform_live_user_qa.sh\n          echo "LIVE_USER_QA_SUCCESS source_commit=$target_sha"',
    '          guard="$runtime/current/tools/platform_release_lock_exec.sh"\n          test -x "$guard" || { echo "Production mutation guard is missing" >&2; exit 1; }\n          /bin/bash "$guard" \\\n            --app-dir "$runtime" \\\n            --expected-sha "$target_sha" \\\n            -- /usr/bin/env \\\n              PLATFORM_APP_DIR="$runtime" \\\n              PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \\\n              PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com \\\n              /root/old_sparky/platform/tools/platform_live_user_qa.sh\n          echo "LIVE_USER_QA_SUCCESS source_commit=$target_sha"',
    label="live user release guard",
)
write(path, text)

# Translation warm-up mutates the production translation cache. Pass explicit
# inputs to the server, serialize it with releases, and load dotenv as data.
path = WORKFLOWS / "platform-patch-translation-qa.yml"
text = read(path)
text = replace_once(
    text,
    '          ssh -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 \\\n            -i ~/.ssh/old_sparky_prod "$PROD_SSH_USER@$PROD_SSH_HOST" \'bash -s\' <<\'REMOTE\'\n          set -Eeuo pipefail\n          runtime=/opt/oldsparky/platform\n          cd "$runtime/current"\n          export PLATFORM_ENV_FILE="$runtime/shared/.env.platform"\n          test -r "$PLATFORM_ENV_FILE"\n          set -a\n          . "$PLATFORM_ENV_FILE"\n          set +a\n          export PLATFORM_ENV_FILE\n          export PYTHONPATH="$runtime/current"',
    '          [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid target SHA" >&2; exit 1; }\n          [[ "$MAX_OPENAI_CALLS" =~ ^[1-4]$ ]] || { echo "MAX_OPENAI_CALLS must be 1..4" >&2; exit 1; }\n          ssh -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 \\\n            -i ~/.ssh/old_sparky_prod "$PROD_SSH_USER@$PROD_SSH_HOST" \\\n            "bash -s -- \'$TARGET_SHA\' \'$MAX_OPENAI_CALLS\'" <<\'REMOTE\'\n          set -Eeuo pipefail\n          target_sha="$1"\n          max_openai_calls="$2"\n          runtime=/opt/oldsparky/platform\n          guard="$runtime/current/tools/platform_release_lock_exec.sh"\n          test -x "$guard" || { echo "Production mutation guard is missing" >&2; exit 1; }\n          /bin/bash "$guard" --app-dir "$runtime" --expected-sha "$target_sha" -- /bin/bash -s -- "$max_openai_calls" <<\'MUTATION\'\n          set -Eeuo pipefail\n          max_openai_calls="$1"\n          runtime=/opt/oldsparky/platform\n          cd "$runtime/current"\n          export PLATFORM_APP_DIR="$runtime"\n          export PLATFORM_SHARED_DIR="$runtime/shared"\n          export PLATFORM_ENV_FILE="$runtime/shared/.env.platform"\n          export PLATFORM_PYTHON_BIN="$runtime/shared/venv/bin/python"\n          source "$runtime/current/tools/platform_runtime_common.sh"\n          platform_load_env_file\n          export PYTHONPATH="$runtime/current"\n          export MAX_OPENAI_CALLS="$max_openai_calls"',
    label="translation safe guarded setup",
)
text = replace_once(
    text,
    '          echo \'=== Recent translation usage logs ===\'\n          journalctl -u deadlock-worker -u deadlock-api --since \'-30 minutes\' --no-pager \\\n            | grep -E \'patch_translation_(openai_usage|completed|failed)\' \\\n            | tail -n 120 \\\n            || true\n          REMOTE',
    '          echo \'=== Recent translation usage logs ===\'\n          journalctl -u deadlock-worker -u deadlock-api --since \'-30 minutes\' --no-pager \\\n            | grep -E \'patch_translation_(openai_usage|completed|failed)\' \\\n            | tail -n 120 \\\n            || true\n          MUTATION\n          REMOTE',
    label="translation guard close",
)
write(path, text)

# The manual production diagnostics already use the release guard. Ensure their
# direct Python invocations also consume the canonical dotenv through the strict
# parser rather than ambient runner/SSH state.
path = WORKFLOWS / "platform-production-diagnostics.yml"
text = read(path)
needle = '          export PLATFORM_ENV_FILE="$runtime/shared/.env.platform"\n          export PYTHONPATH="$runtime/current"'
replacement = '          export PLATFORM_APP_DIR="$runtime"\n          export PLATFORM_SHARED_DIR="$runtime/shared"\n          export PLATFORM_ENV_FILE="$runtime/shared/.env.platform"\n          export PLATFORM_PYTHON_BIN="$runtime/shared/venv/bin/python"\n          source "$runtime/current/tools/platform_runtime_common.sh"\n          platform_load_env_file\n          export PYTHONPATH="$runtime/current"'
count = text.count(needle)
if count != 2:
    raise SystemExit(f"production diagnostics env setup: expected two matches, found {count}")
text = text.replace(needle, replacement)
write(path, text)

# Extend static contracts to every production SSH and mutating QA path.
path = ROOT / "platform" / "tests" / "test_platform_release_audit_hardening.py"
text = read(path)
insert_after = '''        for workflow in (content, diagnostics):
            self.assertIn("platform_release_lock_exec.sh", workflow)
            self.assertIn("--expected-sha", workflow)
'''
addition = f'''

    def test_all_production_ssh_workflows_pin_host_identity(self) -> None:
        workflow_names = {SSH_WORKFLOWS!r}
        expected_fingerprint = "{PIN}"
        for name in workflow_names:
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertIn(expected_fingerprint, workflow, name)
            self.assertIn("StrictHostKeyChecking yes", workflow, name)
            self.assertIn("ssh-keygen -lf", workflow, name)
            self.assertNotIn(
                'ssh-keyscan -T 10 -H "$PROD_SSH_HOST" >> ~/.ssh/known_hosts',
                workflow,
                name,
            )

    def test_live_mutations_share_release_lock_and_exact_sha(self) -> None:
        for name in (
            "platform-live-launch.yml",
            "platform-live-user-qa.yml",
            "platform-patch-translation-qa.yml",
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertIn("platform_release_lock_exec.sh", workflow, name)
            self.assertIn("--expected-sha", workflow, name)

    def test_translation_workflows_never_source_production_dotenv(self) -> None:
        for name in (
            "platform-patch-translation-qa.yml",
            "platform-production-diagnostics.yml",
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn('. "$PLATFORM_ENV_FILE"', workflow, name)
            self.assertIn("platform_load_env_file", workflow, name)
'''
text = replace_once(text, insert_after, insert_after + addition, label="audit contract tests")
write(path, text)

# The enrollment workflow and this patcher are transient and must not reach dev.
print("Final release-audit patch applied.")
