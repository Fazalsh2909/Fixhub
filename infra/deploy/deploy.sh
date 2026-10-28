#!/usr/bin/env bash
# FixHub staging deployment — run on the EC2 host by GitHub Actions via SSM.
#
# Usage (as root):
#   sudo bash /home/ubuntu/Fixhub/infra/deploy/deploy.sh <40-hex-commit-sha>
#
# Design contract (docs/DEPLOYMENT_GITHUB_ACTIONS.md):
#   * Deploys the EXACT commit SHA from $1 — never a moving branch head. The
#     SHA must be a 40-hex commit that is an ancestor of origin/rc/public-app-v1.
#   * Never runs `git clean`; never deletes or rewrites backend/.env, either
#     virtualenv, the database, task workspaces, or any other runtime state.
#   * Touches only files the repository owns: tracked systemd drop-ins are
#     reinstalled only when infra/systemd/** changed in this deploy; the
#     host-managed base units and any server-specific configuration are never
#     edited by this script.
#   * Runs database migrations exactly once, BEFORE restarting services, and
#     NEVER downgrades (rollback restores code only; manual recovery is
#     documented when a migration is not backward-compatible).
#   * Drains the RQ worker before restarting it and refuses to kill a running
#     job: the deployment fails with a clear message instead.
#   * Restarts fixhub-netd/fixhub-egress only when their deployed code or
#     configuration actually changed. A netd restart also restarts
#     api+worker (their tracked drop-ins Require= fixhub-netd).
#   * Verifies the LIVE api process has AUTH_COOKIE_SECURE=1 (via
#     /proc/<MainPID>/environ, reusing the tested installer verifier) and
#     health-checks locally (mandatory) and publicly (configurable) with
#     bounded retries before reporting success.
#   * On failure: collects bounded diagnostics, rolls application code back
#     to the previously deployed SHA (no database downgrade), and exits
#     non-zero so GitHub Actions reports the failure.
#
# Server-tunable knobs live in /etc/fixhub/deploy.env (root-owned, NOT in
# git). This script and its logs never print secrets or tokens.

set -euo pipefail
umask 022

TARGET_SHA="${1:-}"
DEPLOY_BRANCH="rc/public-app-v1"
REPO_APP="/home/ubuntu/Fixhub"
REPO_NET="/opt/fixhub"
VENV_APP="/home/ubuntu/.venvs/fixhub312"
VENV_APP_PY="$VENV_APP/bin/python"
VENV_NET_PY="$REPO_NET/backend/.venv/bin/python"
STATE_DIR="/var/lib/fixhub"
STATE_FILE="$STATE_DIR/deploy-state.json"
LOG_DIR="/var/log/fixhub"
DEPLOY_ENV_FILE="/etc/fixhub/deploy.env"
UNIT_API="fixhub-api"
UNIT_WORKER="fixhub-worker"
UNIT_NETD="fixhub-netd"
UNIT_EGRESS="fixhub-egress"
HEALTH_LOCAL_URL="http://127.0.0.1:8000/health"
HEALTH_ATTEMPTS=30
HEALTH_INTERVAL_S=5
DRAIN_TIMEOUT_S=900
DRAIN_POLL_S=15
PUBLIC_HEALTH_URL="https://fz-dev.runs-on.dev/health"
PUBLIC_HEALTH_REQUIRED=1
FRONTEND_DIST_DIR=""

# Server-local overrides (root-owned file, never committed to git).
if [ -f "$DEPLOY_ENV_FILE" ]; then
  # shellcheck source=/dev/null
  . "$DEPLOY_ENV_FILE"
fi

PREV_SHA=""
MIGRATIONS_RAN=0
ROLLBACK_ATTEMPTED=0
ROLLBACK_OK=0
IN_ROLLBACK=0
RESTARTED_UNITS=""

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "FATAL: $*"; exit 1; }

start_logging() {
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  LOG_FILE="$LOG_DIR/deploy-$(date -u '+%Y%m%dT%H%M%SZ').log"
  # Full log stays on the server; SSM echoes the same stream to GitHub
  # Actions (SSM truncates inline output, the file here does not).
  exec > >(tee -a "$LOG_FILE") 2>&1
  ln -sfn "$LOG_FILE" "$LOG_DIR/deploy-latest.log"
  log "logging to $LOG_FILE"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (SSM Run Command); got uid $(id -u)"
}

validate_sha() {
  # Target-SHA validation: a deployment is always an exact commit.
  [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || die "TARGET_SHA '${TARGET_SHA}' is not a 40-hex commit SHA"
}

repo_owner() { stat -c '%U' "$1"; }

# Run git as the repository's owner so existing credentials and file
# ownership are preserved (SSM commands run as root by default).
git_as_owner() {
  local repo="$1"
  shift
  sudo -u "$(repo_owner "$repo")" git -C "$repo" "$@"
}

# Fetch the deployment branch and move the checkout to the exact target SHA.
sync_repo() {
  local repo="$1" actual
  log "syncing $repo to $TARGET_SHA"
  git_as_owner "$repo" fetch --quiet origin "$DEPLOY_BRANCH" \
    || die "git fetch failed in $repo (the server's existing git auth must allow fetch; it already works for manual pulls)"
  git_as_owner "$repo" cat-file -e "$TARGET_SHA^{commit}" 2>/dev/null \
    || die "$TARGET_SHA not found in $repo after fetching $DEPLOY_BRANCH"
  git_as_owner "$repo" merge-base --is-ancestor "$TARGET_SHA" FETCH_HEAD 2>/dev/null \
    || die "$TARGET_SHA is not an ancestor of origin/$DEPLOY_BRANCH; refusing to deploy an unrelated commit"
  actual="$(git_as_owner "$repo" rev-parse HEAD)"
  if [ "$actual" != "$TARGET_SHA" ]; then
    # -B keeps the repo on the deployment branch while pinning it to the
    # exact SHA. Untracked files (.env, venvs, data) are untouched; local
    # modifications to tracked files make this fail closed instead of being
    # silently discarded.
    git_as_owner "$repo" checkout --quiet -B "$DEPLOY_BRANCH" "$TARGET_SHA" \
      || die "git checkout $TARGET_SHA failed in $repo (local tracked changes? resolve them manually; this script never discards them)"
    actual="$(git_as_owner "$repo" rev-parse HEAD)"
    [ "$actual" = "$TARGET_SHA" ] || die "$repo HEAD is $actual, expected $TARGET_SHA"
  fi
  log "$repo is at $TARGET_SHA"
}

load_prev_state() {
  if [ -f "$STATE_FILE" ]; then
    PREV_SHA="$(sed -n 's/.*"deployed_sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' "$STATE_FILE" | tail -n 1)"
  fi
  if [ -n "$PREV_SHA" ]; then
    if git_as_owner "$REPO_APP" cat-file -e "$PREV_SHA^{commit}" 2>/dev/null; then
      log "previously deployed SHA: $PREV_SHA"
    else
      log "previous SHA $PREV_SHA not present in the repository; treating as first deploy"
      PREV_SHA=""
    fi
  else
    log "no previous deployment state; first automated deploy (conservative plan)"
  fi
}

compute_plan() {
  local changed=""
  if [ -n "$PREV_SHA" ]; then
    changed="$(git_as_owner "$REPO_APP" diff --name-only "$PREV_SHA" "$TARGET_SHA")" \
      || die "git diff ${PREV_SHA}..${TARGET_SHA} failed"
  fi
  PLAN_OUTPUT="$(printf '%s\n' "$changed" | "$VENV_APP_PY" "$REPO_APP/infra/deploy/plan.py" --format sh --old "$PREV_SHA" --new "$TARGET_SHA")" \
    || die "plan.py failed"
  eval "$PLAN_OUTPUT"
}

sync_deps() {
  if [ "$SYNC_APP_DEPS" = "1" ]; then
    log "syncing app venv dependencies ($VENV_APP)"
    "$VENV_APP_PY" -m pip install --quiet --disable-pip-version-check \
      -r "$REPO_APP/backend/requirements.txt" \
      || die "app venv dependency sync failed"
  fi
  if [ "$SYNC_NET_DEPS" = "1" ]; then
    log "syncing netd/egress venv dependencies ($REPO_NET/backend/.venv)"
    "$VENV_NET_PY" -m pip install --quiet --disable-pip-version-check \
      -r "$REPO_NET/backend/requirements.txt" \
      || die "net venv dependency sync failed"
  fi
}

install_systemd_config() {
  [ "$INSTALL_SYSTEMD_CONFIG" = "1" ] || return 0
  log "infra/systemd changed: reinstalling tracked drop-ins"
  # Only files the repository owns are rewritten; the host-managed base
  # units and any untracked drop-ins are never touched.
  install -D -o root -g root -m 644 \
    "$REPO_APP/infra/systemd/fixhub-api.service.d-10-netd.conf" \
    "/etc/systemd/system/$UNIT_API.service.d/10-netd.conf"
  install -D -o root -g root -m 644 \
    "$REPO_APP/infra/systemd/fixhub-api.service.d-10-netd.conf" \
    "/etc/systemd/system/$UNIT_WORKER.service.d/10-netd.conf"
  # Installs the security env file + drop-in, checks EnvironmentFile order,
  # and runs daemon-reload (--no-restart: deploy.sh restarts explicitly).
  python3 "$REPO_APP/infra/systemd/install_api_security.py" --no-restart \
    || die "install_api_security.py failed"
}

reinstall_netd_helper() {
  [ "$REINSTALL_NETD_HELPER" = "1" ] || return 0
  log "reinstalling /usr/local/sbin/fixhub-netd from the deployed source"
  install -o root -g root -m 755 \
    "$REPO_NET/infra/firecracker/fixhub-netd.py" /usr/local/sbin/fixhub-netd
}

run_migrations() {
  [ "$RUN_MIGRATIONS" = "1" ] || {
    log "migrations skipped (no backend changes in this deploy)"
    return 0
  }
  MIGRATIONS_RAN=1
  log "running database migrations (alembic upgrade head) — single explicit step, before any restart"
  # cwd=backend so app.config resolves backend/.env (DATABASE_URL).
  (cd "$REPO_APP/backend" && "$VENV_APP_PY" -m alembic upgrade head) \
    || die "database migration failed; services NOT restarted; code is at $TARGET_SHA — fix migrations and redeploy"
}

drain_worker() {
  log "drain: waiting for running RQ jobs to finish (timeout ${DRAIN_TIMEOUT_S}s)"
  local waited=0 rc
  while :; do
    rc=0
    (cd "$REPO_APP/backend" && "$VENV_APP_PY" "$REPO_APP/infra/deploy/drain.py") || rc=$?
    case "$rc" in
      0) log "drain: worker idle"; return 0 ;;
      1) ;; # busy: keep waiting
      *) die "drain: could not determine worker state (exit $rc); failing closed rather than killing jobs" ;;
    esac
    if [ "$waited" -ge "$DRAIN_TIMEOUT_S" ]; then
      die "drain: a worker job is still running after ${DRAIN_TIMEOUT_S}s; refusing to kill it. Re-run the deployment later or raise DRAIN_TIMEOUT_S in $DEPLOY_ENV_FILE."
    fi
    log "drain: job still running (${waited}s/${DRAIN_TIMEOUT_S}s)"
    sleep "$DRAIN_POLL_S"
    waited=$((waited + DRAIN_POLL_S))
  done
}

wait_active() {
  local unit="$1" i
  for ((i = 1; i <= 60; i++)); do
    if [ "$(systemctl is-active "$unit" 2>/dev/null || true)" = "active" ]; then
      log "$unit is active"
      return 0
    fi
    sleep 2
  done
  return 1
}

restart_services() {
  # Order: egress -> netd -> worker -> api. netd depends on egress; the
  # api/worker drop-ins Require= netd, so netd restarts first and api+worker
  # are restarted explicitly afterwards.
  if [ "$RESTART_EGRESS" = "1" ]; then
    log "restarting $UNIT_EGRESS (sandbox egress code changed)"
    systemctl restart "$UNIT_EGRESS" || die "$UNIT_EGRESS restart failed"
    wait_active "$UNIT_EGRESS" || die "$UNIT_EGRESS not active after restart"
    RESTARTED_UNITS="$RESTARTED_UNITS $UNIT_EGRESS"
  fi
  if [ "$RESTART_NETD" = "1" ]; then
    log "restarting $UNIT_NETD (privilege-daemon code changed)"
    systemctl restart "$UNIT_NETD" || die "$UNIT_NETD restart failed"
    wait_active "$UNIT_NETD" || die "$UNIT_NETD not active after restart"
    RESTARTED_UNITS="$RESTARTED_UNITS $UNIT_NETD"
  fi
  if [ "$RESTART_WORKER" = "1" ]; then
    drain_worker
    log "restarting $UNIT_WORKER"
    systemctl restart "$UNIT_WORKER" || die "$UNIT_WORKER restart failed"
    wait_active "$UNIT_WORKER" || die "$UNIT_WORKER not active after restart"
    RESTARTED_UNITS="$RESTARTED_UNITS $UNIT_WORKER"
  fi
  if [ "$RESTART_API" = "1" ]; then
    log "restarting $UNIT_API"
    systemctl restart "$UNIT_API" || die "$UNIT_API restart failed"
    wait_active "$UNIT_API" || die "$UNIT_API not active after restart"
    RESTARTED_UNITS="$RESTARTED_UNITS $UNIT_API"
  fi
  [ -n "$RESTARTED_UNITS" ] || log "no service restarts required for this change set"
}

# Prove the RUNNING api process still has AUTH_COOKIE_SECURE=1 and answers
# /health — reusing the installer's tested verifier (no restart, no edits).
verify_live_security() {
  log "verifying live API process: AUTH_COOKIE_SECURE=1 + /health 200"
  python3 - "$REPO_APP/infra/systemd" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import install_api_security as m  # noqa: E402

ok, detail = m.verify(m.DEFAULT_UNIT, m.DEFAULT_HEALTH_URL)
print(("[ok] " if ok else "[fail] ") + detail)
raise SystemExit(0 if ok else 1)
PY
}

# Bounded health-check retry loop: HTTP 200 required, --max-time bounds each
# probe so a hung endpoint cannot stall the deployment.
health_check() { # url attempts interval
  local url="$1" attempts="$2" interval="$3" i code
  for ((i = 1; i <= attempts; i++)); do
    code="$(curl --max-time 5 -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      log "health OK: $url (attempt ${i}/${attempts})"
      return 0
    fi
    log "health check ${i}/${attempts}: $url -> HTTP ${code:-000}; retry in ${interval}s"
    [ "$i" = "$attempts" ] || sleep "$interval"
  done
  log "health check FAILED after ${attempts} attempts: $url"
  return 1
}

run_health_checks() {
  health_check "$HEALTH_LOCAL_URL" "$HEALTH_ATTEMPTS" "$HEALTH_INTERVAL_S" \
    || die "local health check failed: $HEALTH_LOCAL_URL"
  if [ "$PUBLIC_HEALTH_REQUIRED" = "1" ]; then
    health_check "$PUBLIC_HEALTH_URL" "$HEALTH_ATTEMPTS" "$HEALTH_INTERVAL_S" \
      || die "public health check failed: $PUBLIC_HEALTH_URL (if the public host does not route /health, set PUBLIC_HEALTH_REQUIRED=0 in $DEPLOY_ENV_FILE)"
  else
    log "public health check disabled via $DEPLOY_ENV_FILE"
  fi
}

build_frontend() {
  [ "$BUILD_FRONTEND" = "1" ] || return 0
  if [ -z "$FRONTEND_DIST_DIR" ]; then
    log "frontend changed but FRONTEND_DIST_DIR is not configured in $DEPLOY_ENV_FILE; skipping frontend publish (staging may be API-only)"
    return 0
  fi
  command -v npm >/dev/null 2>&1 || die "frontend changed and FRONTEND_DIST_DIR is set, but npm is not installed"
  command -v rsync >/dev/null 2>&1 || die "frontend changed and FRONTEND_DIST_DIR is set, but rsync is not installed"
  log "building frontend -> $FRONTEND_DIST_DIR"
  # Build as the repo owner so node_modules/dist do not become root-owned.
  sudo -u "$(repo_owner "$REPO_APP")" bash -c \
    "cd '$REPO_APP/frontend' && npm ci --silent && npm run build --silent" \
    || die "frontend build failed"
  mkdir -p "$FRONTEND_DIST_DIR"
  rsync -a --delete "$REPO_APP/frontend/dist/" "$FRONTEND_DIST_DIR/" \
    || die "frontend publish to $FRONTEND_DIST_DIR failed"
}

save_state() {
  local tmp="$STATE_FILE.tmp"
  cat > "$tmp" <<EOF
{
  "deployed_sha": "$TARGET_SHA",
  "previous_sha": "${PREV_SHA:-}",
  "deployed_at_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "result": "success"
}
EOF
  mv "$tmp" "$STATE_FILE"
  chmod 644 "$STATE_FILE"
}

collect_diagnostics() {
  log "----- diagnostics (bounded) -----"
  local unit
  for unit in "$UNIT_API" "$UNIT_WORKER" "$UNIT_NETD" "$UNIT_EGRESS"; do
    log "systemctl status $unit:"
    systemctl status "$unit" --no-pager -n 5 2>&1 | tail -n 8 | sed 's/^/    /' || true
  done
  if command -v journalctl >/dev/null 2>&1; then
    log "journal tail ($UNIT_API):"
    journalctl -u "$UNIT_API" -n 40 --no-pager 2>&1 | tail -n 40 | sed 's/^/    /' || true
    log "journal tail ($UNIT_WORKER):"
    journalctl -u "$UNIT_WORKER" -n 40 --no-pager 2>&1 | tail -n 40 | sed 's/^/    /' || true
  fi
  log "----- end diagnostics -----"
}

# Code-only rollback: restore the previous SHA in BOTH checkouts, re-sync
# dependencies if they changed, restart what was restarted, re-verify.
# The database is NEVER downgraded; if the previous code cannot serve against
# the migrated schema, the failure is reported with manual recovery steps.
rollback() {
  IN_ROLLBACK=1
  set +e
  ROLLBACK_ATTEMPTED=1
  log "----- rollback -----"
  if [ -z "$PREV_SHA" ]; then
    log "no previously deployed SHA recorded; automatic rollback not possible"
    log "manual recovery: pick the last good commit, then in BOTH $REPO_APP and $REPO_NET run:"
    log "  sudo -u \"\$(stat -c %U <repo>)\" git fetch origin $DEPLOY_BRANCH"
    log "  sudo -u \"\$(stat -c %U <repo>)\" git checkout -B $DEPLOY_BRANCH <last-good-sha>"
    log "  $VENV_APP_PY -m pip install -r $REPO_APP/backend/requirements.txt"
    log "  sudo systemctl restart $UNIT_WORKER $UNIT_API"
    log "migrations were $([ "$MIGRATIONS_RAN" = "1" ] && echo APPLIED || echo NOT applied); the database was NOT downgraded."
    return 1
  fi
  if [ "$PREV_SHA" = "$TARGET_SHA" ]; then
    log "previous SHA equals target; nothing to roll back"
    return 1
  fi
  log "rolling application code back to $PREV_SHA (database is NOT downgraded)"
  git_as_owner "$REPO_APP" checkout --quiet -B "$DEPLOY_BRANCH" "$PREV_SHA" \
    || { log "git checkout $PREV_SHA failed in $REPO_APP"; return 1; }
  git_as_owner "$REPO_NET" checkout --quiet -B "$DEPLOY_BRANCH" "$PREV_SHA" \
    || { log "git checkout $PREV_SHA failed in $REPO_NET"; return 1; }
  if [ "$SYNC_APP_DEPS" = "1" ]; then
    "$VENV_APP_PY" -m pip install --quiet --disable-pip-version-check \
      -r "$REPO_APP/backend/requirements.txt" \
      || log "rollback dependency sync failed (continuing)"
  fi
  if [ "$SYNC_NET_DEPS" = "1" ]; then
    "$VENV_NET_PY" -m pip install --quiet --disable-pip-version-check \
      -r "$REPO_NET/backend/requirements.txt" \
      || log "rollback net dependency sync failed (continuing)"
  fi
  local unit
  for unit in $RESTARTED_UNITS; do
    systemctl restart "$unit" && wait_active "$unit" \
      || log "rollback restart of $unit failed"
  done
  if verify_live_security && health_check "$HEALTH_LOCAL_URL" 10 3; then
    ROLLBACK_OK=1
    log "rollback verified: previous version $PREV_SHA is serving again"
    log "DEPLOYMENT OF $TARGET_SHA STILL FAILED — fix and redeploy"
  else
    log "rollback could NOT be verified healthy"
    log "migrations ran in this deploy: $MIGRATIONS_RAN; the schema may be ahead of $PREV_SHA code"
    log "manual recovery: inspect 'journalctl -u $UNIT_API -n 100 --no-pager'. If the previous code is incompatible with the migrated schema, restore a compatible release or the database backup. Never attempt an automatic schema rollback."
  fi
  return 1
}

finish() {
  local rc="$1"
  [ "$rc" -eq 0 ] && return 0
  [ "$IN_ROLLBACK" = "1" ] && return 0
  collect_diagnostics || true
  rollback || true
  log "deployment FAILED (exit $rc); deployed_sha=$TARGET_SHA previous_sha=${PREV_SHA:-none} rollback_attempted=$ROLLBACK_ATTEMPTED rollback_ok=$ROLLBACK_OK"
  return 0
}
trap 'finish "$?"' EXIT

main() {
  start_logging
  require_root
  validate_sha
  log "deploy request: sha=$TARGET_SHA branch=$DEPLOY_BRANCH"
  sync_repo "$REPO_APP"
  sync_repo "$REPO_NET"
  load_prev_state
  compute_plan
  log "plan: api=$RESTART_API worker=$RESTART_WORKER netd=$RESTART_NETD egress=$RESTART_EGRESS app_deps=$SYNC_APP_DEPS net_deps=$SYNC_NET_DEPS systemd=$INSTALL_SYSTEMD_CONFIG netd_helper=$REINSTALL_NETD_HELPER migrations=$RUN_MIGRATIONS frontend=$BUILD_FRONTEND"
  sync_deps
  install_systemd_config
  reinstall_netd_helper
  run_migrations
  restart_services
  verify_live_security
  run_health_checks
  build_frontend
  save_state
  log "DEPLOY OK: sha=$TARGET_SHA (previous: ${PREV_SHA:-none})"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
