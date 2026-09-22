#!/usr/bin/env bash
#
# Arkive appliance headless self-update (no git). Pulls the latest install
# bundle from the cloud and re-runs the (incremental) installer only when the
# cloud version differs from what's deployed. Invoked by a systemd timer so a
# headless appliance stays current without operator action.
#
# OBSERVABILITY: every run appends to <data>/update.log and writes a small
# <data>/update-status.json (result + from/to versions + a message tail), both
# chowned to the agent's user so the running agent can read them and forward the
# outcome to the control plane (Platform Logs + the appliance's telemetry). This
# is how a stuck / failing self-update is diagnosable WITHOUT shell access.
#
set -uo pipefail

INSTALL_DIR="/opt/continuity-vault"
SRC_DIR="${ARKIVE_SRC_DIR:-/opt/arkive-src}"
ENV_FILE="/etc/continuity-vault/appliance.env"

# Extract the cloud URL + data dir from the env file — never `source` it, since
# values like `CVA_MODEL=CV Edge 8` are unquoted (fine for systemd, not for bash).
CLOUD_URL="https://vault.arkive.life/api"
DATA_DIR="/var/lib/continuity-vault-appliance/data"
if [ -f "$ENV_FILE" ]; then
  _u="$(sed -n 's/^CVA_CLOUD_BASE_URL=//p' "$ENV_FILE" | tr -d '"' | head -1)"
  [ -n "$_u" ] && CLOUD_URL="$_u"
  _d="$(sed -n 's/^CVA_DATA_DIR=//p' "$ENV_FILE" | tr -d '"' | head -1)"
  [ -n "$_d" ] && DATA_DIR="$_d"
fi
SERVICE_USER="${ARKIVE_SERVICE_USER:-cvagent}"
UPDATE_LOG="$DATA_DIR/update.log"
STATUS_FILE="$DATA_DIR/update-status.json"
mkdir -p "$DATA_DIR" 2>/dev/null || true

cur=""
[ -f "$INSTALL_DIR/appliance/VERSION" ] && cur="$(cat "$INSTALL_DIR/appliance/VERSION" 2>/dev/null)"
remote=""

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%S+0000') $*"; }

# Persist the run outcome so the agent can surface it to the control plane.
#   $1 result (up-to-date|updated|failed)   $2 to_version   $3 message
write_status() {
  python3 - "$STATUS_FILE" "$1" "$cur" "${2:-}" "$remote" "${3:-}" <<'PY' 2>/dev/null || true
import json, sys, time
path, result, frm, to, remote, msg = sys.argv[1:7]
json.dump({
    "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
    "result": result, "from_version": frm, "to_version": to or remote,
    "remote_version": remote, "message": msg[-800:],
}, open(path, "w"))
PY
  chown "$SERVICE_USER:$SERVICE_USER" "$STATUS_FILE" "$UPDATE_LOG" 2>/dev/null || true
  if [ -f "$UPDATE_LOG" ] && [ "$(wc -l < "$UPDATE_LOG" 2>/dev/null || echo 0)" -gt 800 ]; then
    tail -n 400 "$UPDATE_LOG" > "$UPDATE_LOG.tmp" 2>/dev/null && mv "$UPDATE_LOG.tmp" "$UPDATE_LOG"
    chown "$SERVICE_USER:$SERVICE_USER" "$UPDATE_LOG" 2>/dev/null || true
  fi
}

# The work runs in a subshell whose output is timestamped + teed into the rolling
# update log (the agent tails + forwards it). No `set -e`: failures are handled
# explicitly so a stuck update always records a status instead of dying silently.
{
  log "self-update check: installed=${cur:-none} cloud=$CLOUD_URL"
  remote="$(curl -fsSL "${CLOUD_URL}/appliance/bundle/version" 2>/dev/null \
    | sed -n 's/.*"version"[: ]*"\([^"]*\)".*/\1/p')"
  if [ -z "$remote" ]; then
    log "WARNING could not reach the control plane for the latest version — will retry next cycle"
    write_status "failed" "" "could not reach control plane at ${CLOUD_URL}/appliance/bundle/version"
    exit 0
  fi
  log "control plane serves version $remote"

  if [ "$remote" = "$cur" ] && [ "${CV_FORCE:-0}" != "1" ]; then
    log "already up to date ($cur)"
    write_status "up-to-date" "$cur" "already on the latest version"
    exit 0
  fi

  log "downloading appliance bundle ${cur:-none} -> $remote"
  mkdir -p "$SRC_DIR"
  if ! curl -fsSL "${CLOUD_URL}/appliance/bundle" -o /tmp/arkive-appliance.tar.gz 2>&1; then
    log "ERROR bundle download failed"
    write_status "failed" "$remote" "bundle download failed from ${CLOUD_URL}/appliance/bundle"
    exit 1
  fi
  if ! tar -xzf /tmp/arkive-appliance.tar.gz -C "$SRC_DIR" 2>&1; then
    log "ERROR could not unpack the bundle"
    write_status "failed" "$remote" "could not unpack the downloaded bundle"
    exit 1
  fi
  rm -f /tmp/arkive-appliance.tar.gz
  chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

  new=""
  [ -f "$SRC_DIR/appliance/VERSION" ] && new="$(cat "$SRC_DIR/appliance/VERSION" 2>/dev/null)"
  if [ -n "$cur" ] && [ "$cur" = "$new" ] && [ "${CV_FORCE:-0}" != "1" ]; then
    log "already up to date ($cur)"
    write_status "up-to-date" "$cur" "already on the latest version"
    exit 0
  fi

  log "installing appliance update ${cur:-none} -> ${new:-$remote} (from control plane)"
  # The installer is idempotent/incremental and restarts the service at the end.
  if CV_CLOUD_URL="$CLOUD_URL" bash "$SRC_DIR/installers/appliance-install.sh" 2>&1; then
    log "update installed: now on ${new:-$remote}"
    write_status "updated" "${new:-$remote}" "installed ${new:-$remote} from the control plane"
    exit 0
  else
    rc=$?
    log "ERROR installer failed (exit $rc) — appliance stays on ${cur:-none}"
    write_status "failed" "${new:-$remote}" "installer failed (exit $rc); staying on ${cur:-none}. See update.log"
    exit "$rc"
  fi
} 2>&1 | while IFS= read -r line; do
  case "$line" in
    20[0-9][0-9]-*) echo "$line" ;;
    *) echo "$(date -u '+%Y-%m-%dT%H:%M:%S+0000') INFO $line" ;;
  esac
done | tee -a "$UPDATE_LOG"

