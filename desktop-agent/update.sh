#!/usr/bin/env bash
#
# Arkive desktop agent self-update (no git). Re-downloads the agent bundle from
# the cloud, reinstalls deps with the bundled Python, and restarts the service.
# Triggered by a cloud "update" command or run manually.
#
set -euo pipefail

HOME_DIR="${ARKIVE_AGENT_HOME:-$HOME/.arkive/home}"
CLOUD_URL="${ARKIVE_CLOUD_URL:-https://vault.arkive.life/api}"
PY="$HOME_DIR/python/bin/python3"
[ -x "$PY" ] || PY="python3"

curl -fsSL "${CLOUD_URL}/agent/bundle" -o "$HOME_DIR/bundle.tar.gz"
tar -xzf "$HOME_DIR/bundle.tar.gz" -C "$HOME_DIR"
rm -f "$HOME_DIR/bundle.tar.gz"
chmod +x "$HOME_DIR"/desktop-agent/*.sh 2>/dev/null || true

"$PY" -m pip install -q -r "$HOME_DIR/desktop-agent/requirements.txt" || true
"$PY" -m pip install -q -e "$HOME_DIR/shared" || true

# Restart the launchd agent so the new code runs.
uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.arkive.agent" 2>/dev/null || \
  launchctl kill TERM "gui/${uid}/com.arkive.agent" 2>/dev/null || true

echo "Arkive agent updated."
