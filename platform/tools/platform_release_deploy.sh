#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# End-to-end release state machine. The low-level installer only stages the
# filesystem candidate; this wrapper owns the migration/runtime/smoke boundary.

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ARTIFACT=""
RESUME=0
ABORT_RETAINED=0
CONFIRM_MIGRATION_NOT_REVERSED=0
EXPECTED_CSP_MODE="enforce"
EDGE_ORIGIN="https://127.0.0.1"
EDGE_HOST="old-sparky.com"
PUBLIC_EDGE_ORIGIN="https://old-sparky.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { echo "--app-dir requires a path." >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    --artifact)
      [[ $# -ge 2 ]] || { echo "--artifact requires a path." >&2; exit 1; }
      ARTIFACT="$2"
      shift 2
      ;;
    --resume)
      RESUME=1
      shift
      ;;
    --abort-retained)
      ABORT_RETAINED=1
      shift
      ;;
    --confirm-migration-not-reversed)
      CONFIRM_MIGRATION_NOT_REVERSED=1
      shift
      ;;
    --expected-csp-mode)
      [[ $# -ge 2 ]] || { echo "--expected-csp-mode requires a value." >&2; exit 1; }
      EXPECTED_CSP_MODE="$2"
      shift 2
      ;;
    --edge-origin)
      [[ $# -ge 2 ]] || { echo "--edge-origin requires an origin." >&2; exit 1; }
      EDGE_ORIGIN="$2"
      shift 2
      ;;
    --edge-host)
      [[ $# -ge 2 ]] || { echo "--edge-host requires a hostname." >&2; exit 1; }
      EDGE_HOST="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_deploy.sh --artifact <artifact.tar.gz> [--app-dir <path>]
       platform_release_deploy.sh --resume [--app-dir <path>]
       platform_release_deploy.sh --abort-retained \
         --confirm-migration-not-reversed [--app-dir <path>]

Runs the durable production release state machine:
  stage -> migration decision -> pointer activation -> restart/readiness
  -> Nginx apply -> origin/public smoke -> transaction commit.

The transaction is intentionally retained after any migration uncertainty.
Alembic is never downgraded automatically. Use --resume after operator review
of a retained state. If code/runtime rollback is explicitly chosen, use
--abort-retained with --confirm-migration-not-reversed; do not delete
shared/.release-operation.json manually.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "End-to-end platform release deployment requires root." >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  echo "Application directory must be an absolute non-root path." >&2
  exit 1
fi
if [[ "$RESUME" -eq 1 && -n "$ARTIFACT" ]]; then
  echo "--resume cannot be combined with --artifact." >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 1 && ( "$RESUME" -eq 1 || -n "$ARTIFACT" ) ]]; then
  echo "--abort-retained cannot be combined with --resume or --artifact." >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 0 && "$RESUME" -eq 0 && -z "$ARTIFACT" ]]; then
  echo "--artifact is required unless --resume is used." >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 1 && "$CONFIRM_MIGRATION_NOT_REVERSED" -eq 0 ]]; then
  echo "--abort-retained requires --confirm-migration-not-reversed." >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 0 && "$CONFIRM_MIGRATION_NOT_REVERSED" -eq 1 ]]; then
  echo "--confirm-migration-not-reversed requires --abort-retained." >&2
  exit 1
fi
if [[ "$EXPECTED_CSP_MODE" != "enforce" ]]; then
  echo "Production deploys require the enforced CSP mode." >&2
  exit 1
fi

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_TOOL="$TOOLS_DIR/platform_release_install.sh"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"
RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"
APP_DIR="$(readlink -f "$APP_DIR")"
SHARED_DIR="$APP_DIR/shared"
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
SHARED_VENV="$SHARED_DIR/venv"

if [[ ! -d "$APP_DIR" || -L "$APP_DIR" || ! -d "$SHARED_DIR" || -L "$SHARED_DIR" ]]; then
  echo "Platform application/shared layout is missing or unsafe: $APP_DIR" >&2
  exit 1
fi
exec {RELEASE_LOCK_FD}<"$SHARED_DIR"
json_field() {
  local field="$1"
  /usr/bin/python3 -I -c \
    'import json,sys; value=json.load(sys.stdin)[sys.argv[1]]; print("" if value is None else value)' \
    "$field"
}

transaction_json() {
  /usr/bin/python3 -I "$TRANSACTION_TOOL" status \
    --state "$TRANSACTION_STATE" --json
}

transaction_phase() {
  transaction_json | json_field phase
}

set_phase() {
  local expected="$1"
  local phase="$2"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected "$expected" \
    --phase "$phase"
}

candidate_env() {
  export PLATFORM_ENV_FILE="$SHARED_DIR/.env.platform"
  export PLATFORM_PYTHON_BIN="$SHARED_VENV/bin/python"
  export PLATFORM_SHARED_DIR="$SHARED_DIR"
  export PLATFORM_APP_DIR="$APP_DIR"
  export PYTHONPATH="$CANDIDATE${PYTHONPATH:+:$PYTHONPATH}"
}

run_candidate() {
  candidate_env
  (cd "$CANDIDATE" && "$@")
}

release_preflight() {
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$APP_DIR" \
    --require-previous \
    --require-verified-backup \
    --require-edge-parity \
    --backup-max-age-hours 24
}

print_retained_state() {
  if [[ -f "$TRANSACTION_STATE" && ! -L "$TRANSACTION_STATE" ]]; then
    echo "Release transaction retained at: $TRANSACTION_STATE" >&2
    echo "Current phase: $(transaction_phase)" >&2
    echo "Resume only after reviewing the migration outcome: $TOOLS_DIR/platform_release_deploy.sh --resume --app-dir $APP_DIR" >&2
  fi
}
trap print_retained_state EXIT

restore_previous_runtime() {
  local release="$1"
  "$RUNTIME_RESTORE_TOOL" \
    --app-dir "$APP_DIR" \
    --release "$release" \
    --expected-csp-mode "$EXPECTED_CSP_MODE" \
    --edge-origin "$EDGE_ORIGIN" \
    --edge-host "$EDGE_HOST" \
    --public-edge-origin "$PUBLIC_EDGE_ORIGIN"
}

acquire_release_lock() {
  if ! /usr/bin/flock -n "$RELEASE_LOCK_FD"; then
    echo "Another platform install or rollback operation holds the release lock." >&2
    exit 3
  fi
}

abort_retained_release() {
  local retained_phase="$1"
  case "$retained_phase" in
    migration-pending|migration-failed|migration-applied|activation-pending|\
    services-restarted|nginx-pending|nginx-applied|smoke-passed|\
    activation-committed|recovery-authorized|recovery-restored)
      ;;
    staged)
      echo "The staged transaction has no migration uncertainty; use --recover-pending." >&2
      return 1
      ;;
    *)
      echo "Cannot abort retained release from phase: $retained_phase" >&2
      return 1
      ;;
  esac
  case "$retained_phase" in
    activation-pending|services-restarted|nginx-pending|nginx-applied|\
    smoke-passed|activation-committed|recovery-authorized|recovery-restored)
      local original_current
      original_current="$(transaction_json | json_field current_before)"
      if [[ -z "$original_current" ]]; then
        original_current="$(transaction_json | json_field previous_before)"
      fi
      if [[ "$retained_phase" != "recovery-restored" ]]; then
        if [[ "$retained_phase" != "recovery-authorized" ]]; then
          /usr/bin/python3 -I "$TRANSACTION_TOOL" authorize-recovery \
            --state "$TRANSACTION_STATE" \
            --confirm MIGRATION_NOT_REVERSED
        fi
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
          --retain \
          --state "$TRANSACTION_STATE"
      else
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
          --retain \
          --state "$TRANSACTION_STATE"
      fi
      restore_previous_runtime "$original_current"
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
        --state "$TRANSACTION_STATE"
      ;;
    migration-pending|migration-failed|migration-applied)
      if [[ "$retained_phase" != "recovery-authorized" ]]; then
        /usr/bin/python3 -I "$TRANSACTION_TOOL" authorize-recovery \
          --state "$TRANSACTION_STATE" \
          --confirm MIGRATION_NOT_REVERSED
      fi
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$TRANSACTION_STATE"
      ;;
  esac
  echo "Retained release aborted after explicit migration review; database was not downgraded." >&2
}

if [[ "$ABORT_RETAINED" -eq 1 ]]; then
  acquire_release_lock
  if [[ ! -f "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
    echo "No retained release transaction exists to abort." >&2
    exit 1
  fi
  abort_retained_release "$(transaction_phase)"
  trap - EXIT
  exit 0
fi

if [[ "$RESUME" -eq 0 ]]; then
  if [[ ! -f "$ARTIFACT" || -L "$ARTIFACT" ]]; then
    echo "Release artifact is missing or unsafe: $ARTIFACT" >&2
    exit 1
  fi
  ARTIFACT="$(readlink -f "$ARTIFACT")"
  release_preflight
  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"
fi

acquire_release_lock

if [[ ! -f "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  echo "No durable release transaction is available." >&2
  exit 1
fi
TRANSACTION_JSON="$(transaction_json)"
CANDIDATE="$(printf '%s' "$TRANSACTION_JSON" | json_field candidate_release)"
CURRENT_BEFORE="$(printf '%s' "$TRANSACTION_JSON" | json_field current_before)"
if [[ -z "$CANDIDATE" || ! -d "$CANDIDATE" || -L "$CANDIDATE" ]]; then
  echo "Release transaction candidate is missing or unsafe: $CANDIDATE" >&2
  exit 1
fi
CANDIDATE="$(readlink -f "$CANDIDATE")"

phase="$(printf '%s' "$TRANSACTION_JSON" | json_field phase)"
case "$phase" in
  staged|migration-pending|migration-failed)
    if [[ "$phase" == "staged" ]]; then
      set_phase staged migration-pending
    elif [[ "$phase" == "migration-failed" ]]; then
      echo "Resuming a previously failed/uncertain migration after operator review." >&2
      set_phase migration-failed migration-pending
    fi
    run_candidate tools/platform_run_alembic.sh upgrade head \
      || {
        set_phase migration-pending migration-failed || true
        exit 1
      }
    set_phase migration-pending migration-applied
    phase=migration-applied
    ;;
  migration-applied|activation-pending|services-restarted|nginx-pending|\
  nginx-applied|smoke-passed|activation-committed)
    ;;
  *)
    echo "Unsupported release transaction phase for deploy: $phase" >&2
    exit 1
    ;;
esac

phase="$(transaction_phase)"
if [[ "$phase" == "migration-applied" || "$phase" == "activation-pending" ]]; then
  if [[ -n "$CURRENT_BEFORE" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
      --state "$TRANSACTION_STATE" --name previous --target "$CURRENT_BEFORE"
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
    --state "$TRANSACTION_STATE" --name current --target "$CANDIDATE"
  if [[ "$phase" == "migration-applied" ]]; then
    set_phase migration-applied activation-pending
  fi
  phase=activation-pending
fi

if [[ "$phase" == "activation-pending" ]]; then
  run_candidate tools/platform_install_systemd_units.sh
  trap - EXIT
  trap print_retained_state EXIT
  /usr/bin/systemctl restart deadlock-api deadlock-worker deadlock-web
  for service in deadlock-api deadlock-worker deadlock-web; do
    /usr/bin/systemctl is-active --quiet "$service"
  done
  for attempt in {1..30}; do
    if /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8010/api/v1/health/ready >/dev/null \
      && /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:3000/ >/dev/null; then
      break
    fi
    if [[ "$attempt" -eq 30 ]]; then
      echo "Service readiness did not pass after restart." >&2
      exit 1
    fi
    sleep 1
  done
  set_phase activation-pending services-restarted
  phase=services-restarted
fi

if [[ "$phase" == "services-restarted" ]]; then
  set_phase services-restarted nginx-pending
  phase=nginx-pending
fi

if [[ "$phase" == "nginx-pending" ]]; then
  candidate_env
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_install_nginx.py" --json
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_install_nginx.py" \
    --apply --reload --json
  set_phase nginx-pending nginx-applied
  phase=nginx-applied
fi

if [[ "$phase" == "nginx-applied" ]]; then
  candidate_env
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_deploy_smoke.py" \
    --app-dir "$APP_DIR" \
    --env-file "$SHARED_DIR/.env.platform" \
    --edge-origin "$EDGE_ORIGIN" \
    --edge-host "$EDGE_HOST" \
    --edge-insecure-loopback \
    --expected-csp-mode "$EXPECTED_CSP_MODE"
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_deploy_smoke.py" \
    --app-dir "$APP_DIR" \
    --env-file "$SHARED_DIR/.env.platform" \
    --edge-origin "$PUBLIC_EDGE_ORIGIN" \
    --expected-csp-mode "$EXPECTED_CSP_MODE"
  release_preflight
  set_phase nginx-applied smoke-passed
  phase=smoke-passed
fi

if [[ "$phase" == "smoke-passed" ]]; then
  set_phase smoke-passed activation-committed
  phase=activation-committed
fi

if [[ "$phase" == "activation-committed" ]]; then
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
fi

trap - EXIT
echo "Platform end-to-end release deployment completed: $CANDIDATE"
