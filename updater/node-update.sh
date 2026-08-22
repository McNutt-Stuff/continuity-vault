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

log() { echo "[node-update] $*"; }

# The node-management console runs systemctl + reads the journal as the service
# account (cvault). Apply the scoped sudoers + journal-group membership on every
# update so permissions are always correct. Idempotent; safe if the user absent.
ensure_control_perms() {
  local user=cvault f=/etc/sudoers.d/cv-cloud unit act
  id -u "$user" >/dev/null 2>&1 || return 0
  { : > "$f"; } 2>/dev/null || return 0
  for unit in cv-cloud postgresql caddy cv-node-heartbeat.timer cv-node-update.timer cv-cloud-update.timer; do
    for act in start stop restart enable disable; do
      echo "${user} ALL=(root) NOPASSWD: /usr/bin/systemctl ${act} ${unit}" >> "$f"
    done
  done
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
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "${CP}/api/nodes/bundle" -o "$tmp/bundle.tar.gz"
# Stage into a fresh dir, then swap, so a bad download never corrupts the source.
rm -rf "$SRC_DIR.new"; mkdir -p "$SRC_DIR.new"
tar -xzf "$tmp/bundle.tar.gz" -C "$SRC_DIR.new"
rm -rf "$SRC_DIR"; mv "$SRC_DIR.new" "$SRC_DIR"
chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

if REPO_SRC="$SRC_DIR" bash "$SRC_DIR/installers/cloud-install.sh"; then
  mkdir -p "$(dirname "$VERSION_FILE")"
  echo "$remote" > "$VERSION_FILE"
  ensure_control_perms
  log "updated to ${remote}"
else
  log "installer failed; leaving previous version marker in place"
  exit 1
fi
