#!/usr/bin/env bash
#
# Arkive — fleet node self-update (public-web / customer-tenant).
#
# Pulls the node bundle FROM THE CONTROL PLANE (never GitHub), and re-runs the
# role-aware installer only when the control plane advertises a new bundle
# version. Driven by the cv-node-update.timer. All config comes from the node's
# env file written at install time.
#
set -euo pipefail

ENV_FILE="/etc/continuity-vault.env"
SRC_DIR="${CV_SRC_DIR:-/opt/arkive-src}"
VERSION_FILE="/etc/arkive/bundle-version"
# Outcome of the last self-update, shipped to the control plane once on the next
# heartbeat so a stuck/failed node update is visible in Platform Logs — the
# updater runs OUTSIDE cv-cloud and otherwise only logs to this node's journald.
REPORT_FILE="/etc/arkive/update-report.json"
RUN_LOG="$(mktemp)"
trap 'rm -f "$RUN_LOG"' EXIT

log() { echo "[node-update] $*" | tee -a "$RUN_LOG"; }

# Persist a machine-readable outcome ($1=1 ok / 0 failed) the heartbeat ships to
# the control plane and then deletes. Owned by the heartbeat's service account so
# it can be removed after delivery. Best-effort; never fail the update over this.
write_report() {
  local ok="$1"
  python3 - "$ok" "${current:-}" "$remote" "$RUN_LOG" "$REPORT_FILE" <<'PY' 2>/dev/null || return 0
import datetime, json, sys
ok, cur, rem, runlog, out = sys.argv[1:6]
try:
    with open(runlog) as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()][-120:]
except Exception:
    lines = []
rep = {"ts": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
       "ok": ok == "1", "from": cur or "", "to": rem or "", "lines": lines}
with open(out, "w") as fh:
    json.dump(rep, fh)
PY
  chown cvault:cvault "$REPORT_FILE" 2>/dev/null || true
  chmod 644 "$REPORT_FILE" 2>/dev/null || true
}

# The node-management console runs systemctl + reads the journal as the service
# account (cvault). Apply the scoped sudoers + journal-group membership on every
# update so permissions are always correct. Idempotent; safe if the user absent.
ensure_control_perms() {
  local user=cvault f=/etc/sudoers.d/cv-cloud unit act
  id -u "$user" >/dev/null 2>&1 || return 0
  { : > "$f"; } 2>/dev/null || return 0
  # Include the *.service update units (admin "Update" starts the service, not the timer).
  for unit in cv-cloud postgresql caddy cv-node-heartbeat.timer cv-node-update.timer cv-node-update.service cv-cloud-update.timer cv-cloud-update.service; do
    for act in start stop restart enable disable; do
      echo "${user} ALL=(root) NOPASSWD: /usr/bin/systemctl ${act} ${unit}" >> "$f"
    done
  done
  echo "${user} ALL=(root) NOPASSWD: /usr/bin/timedatectl set-timezone *" >> "$f"
  chmod 440 "$f" 2>/dev/null || true
  if ! id -nG "$user" 2>/dev/null | grep -qw systemd-journal; then
    usermod -aG systemd-journal "$user" 2>/dev/null || true
    systemctl restart cv-cloud 2>/dev/null || true
  fi
  chown -R "$user":"$user" /opt/continuity-vault /var/lib/continuity-vault 2>/dev/null || true
}

[[ -f "$ENV_FILE" ]] || { log "no env file; node not installed"; exit 0; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

CP="${CV_CONTROL_PLANE_URL:-}"
ROLE="${CV_NODE_ROLE:-control-plane}"
[[ "$ROLE" == "control-plane" ]] && { log "control-plane self-updates via git; skipping"; exit 0; }
[[ -n "$CP" ]] || { log "CV_CONTROL_PLANE_URL not set; skipping"; exit 0; }
CP="${CP%/}"

command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }

remote="$(curl -fsSL "${CP}/api/nodes/bundle/version" 2>/dev/null \
  | sed -n 's/.*"version"[: ]*"\([^"]*\)".*/\1/p')"
if [[ -z "$remote" ]]; then log "could not reach control plane; skipping"; exit 0; fi

current=""
[[ -f "$VERSION_FILE" ]] && current="$(cat "$VERSION_FILE")"
if [[ "$remote" == "$current" && "${CV_FORCE:-0}" != "1" ]]; then
  log "up to date (${current})"; exit 0
fi

log "updating ${current:-none} -> ${remote}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"; rm -f "$RUN_LOG"' EXIT
# Marker the heartbeat reads so the admin console shows an intentional "Updating"
# state (not a scary offline/restart blip) while we re-install. Cleared on exit.
mkdir -p /run/arkive 2>/dev/null || true
: > /run/arkive/updating 2>/dev/null || true
trap 'rm -rf "$tmp"; rm -f /run/arkive/updating "$RUN_LOG"' EXIT
curl -fsSL "${CP}/api/nodes/bundle" -o "$tmp/bundle.tar.gz"
# Stage into a fresh dir, then swap, so a bad download never corrupts the source.
rm -rf "$SRC_DIR.new"; mkdir -p "$SRC_DIR.new"
tar -xzf "$tmp/bundle.tar.gz" -C "$SRC_DIR.new"
rm -rf "$SRC_DIR"; mv "$SRC_DIR.new" "$SRC_DIR"
chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

# Stream the installer to journald AND capture it so a failure tail reaches the
# control plane via the heartbeat update-report.
set +e
REPO_SRC="$SRC_DIR" bash "$SRC_DIR/installers/cloud-install.sh" 2>&1 | tee -a "$RUN_LOG"
rc=${PIPESTATUS[0]}
set -e
if [[ "$rc" -eq 0 ]]; then
  mkdir -p "$(dirname "$VERSION_FILE")"
  echo "$remote" > "$VERSION_FILE"
  ensure_control_perms
  log "updated to ${remote}"
  write_report 1
else
  log "installer failed (exit ${rc}); leaving previous version marker in place"
  write_report 0
  exit 1
fi
