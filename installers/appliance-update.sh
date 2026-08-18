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

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
CLOUD_URL="${CVA_CLOUD_BASE_URL:-https://vault.arkive.life/api}"

cur=""
[ -f "$INSTALL_DIR/appliance/VERSION" ] && cur="$(cat "$INSTALL_DIR/appliance/VERSION")"

mkdir -p "$SRC_DIR"
curl -fsSL "${CLOUD_URL}/appliance/bundle" -o /tmp/arkive-appliance.tar.gz
tar -xzf /tmp/arkive-appliance.tar.gz -C "$SRC_DIR"
rm -f /tmp/arkive-appliance.tar.gz
chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

new=""
[ -f "$SRC_DIR/appliance/VERSION" ] && new="$(cat "$SRC_DIR/appliance/VERSION")"

if [ -n "$cur" ] && [ "$cur" = "$new" ]; then
  echo "appliance already up to date ($cur)"
  exit 0
fi

echo "updating appliance ${cur:-none} -> ${new:-unknown}"
# The installer is idempotent/incremental and restarts the service at the end.
CV_CLOUD_URL="$CLOUD_URL" bash "$SRC_DIR/installers/appliance-install.sh"
