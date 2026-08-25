#!/usr/bin/env bash
#
# Arkive appliance headless self-update (no git). Pulls the latest install
# bundle from the cloud and re-runs the (incremental) installer only when the
# cloud version differs from what's deployed. Invoked by a systemd timer so a
# headless appliance stays current without operator action.
#
set -euo pipefail

INSTALL_DIR="/opt/continuity-vault"
SRC_DIR="${ARKIVE_SRC_DIR:-/opt/arkive-src}"
ENV_FILE="/etc/continuity-vault/appliance.env"

# Extract only the cloud URL — never `source` the env file, since values like
# `CVA_MODEL=CV Edge 8` are unquoted (valid for systemd EnvironmentFile but not
# for bash `source`, which would try to run `Edge 8`).
CLOUD_URL="https://vault.arkive.life/api"
if [ -f "$ENV_FILE" ]; then
  _u="$(sed -n 's/^CVA_CLOUD_BASE_URL=//p' "$ENV_FILE" | tr -d '"' | head -1)"
  [ -n "$_u" ] && CLOUD_URL="$_u"
fi

cur=""
[ -f "$INSTALL_DIR/appliance/VERSION" ] && cur="$(cat "$INSTALL_DIR/appliance/VERSION")"

# Cheap check first: ask the control plane which appliance version it serves and
# skip the download when we're already on it (never touches GitHub).
remote="$(curl -fsSL "${CLOUD_URL}/appliance/bundle/version" 2>/dev/null \
  | sed -n 's/.*"version"[: ]*"\([^"]*\)".*/\1/p')"
if [ -n "$remote" ] && [ "$remote" = "$cur" ] && [ "${CV_FORCE:-0}" != "1" ]; then
  echo "appliance already up to date ($cur)"
  exit 0
fi

mkdir -p "$SRC_DIR"
curl -fsSL "${CLOUD_URL}/appliance/bundle" -o /tmp/arkive-appliance.tar.gz
tar -xzf /tmp/arkive-appliance.tar.gz -C "$SRC_DIR"
rm -f /tmp/arkive-appliance.tar.gz
chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

new=""
[ -f "$SRC_DIR/appliance/VERSION" ] && new="$(cat "$SRC_DIR/appliance/VERSION")"

if [ -n "$cur" ] && [ "$cur" = "$new" ] && [ "${CV_FORCE:-0}" != "1" ]; then
  echo "appliance already up to date ($cur)"
  exit 0
fi

echo "updating appliance ${cur:-none} -> ${new:-unknown} (from control plane)"
# The installer is idempotent/incremental and restarts the service at the end.
CV_CLOUD_URL="$CLOUD_URL" bash "$SRC_DIR/installers/appliance-install.sh"
