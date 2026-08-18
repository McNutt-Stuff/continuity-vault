#!/usr/bin/env bash
#
# Arkive desktop agent self-update (no git). Re-downloads the agent bundle from
# the cloud, reinstalls deps with the bundled Python, and restarts the service.
# Triggered by a cloud "update" command or run manually.
#
set -euo pipefail

HOME_DIR="${ARKIVE_AGENT_HOME:-$HOME/.arkive/home}"
DATA_DIR="${ARKIVE_AGENT_DIR:-$HOME/.arkive-agent}"
CLOUD_URL="${ARKIVE_CLOUD_URL:-https://vault.arkive.life/api}"
PY="$HOME_DIR/python/bin/python3"
[ -x "$PY" ] || PY="python3"

mkdir -p "$DATA_DIR"
LOG="$DATA_DIR/update.log"
exec >>"$LOG" 2>&1
echo "==> $(date -u +%FT%TZ) self-update starting from $CLOUD_URL"

# Pull the latest agent bundle (desktop-agent + shared) from the cloud.
curl -fsSL "${CLOUD_URL}/agent/bundle" -o "$HOME_DIR/bundle.tar.gz"
tar -xzf "$HOME_DIR/bundle.tar.gz" -C "$HOME_DIR"
rm -f "$HOME_DIR/bundle.tar.gz"
chmod +x "$HOME_DIR"/desktop-agent/*.sh 2>/dev/null || true

# Reinstall dependencies (idempotent) so new code with new deps keeps working.
"$PY" -m pip install -q --upgrade httpx rumps || true
"$PY" -m pip install -q -e "$HOME_DIR/shared" || true

if [ -f "$HOME_DIR/desktop-agent/VERSION" ]; then
  echo "==> updated to version $(cat "$HOME_DIR/desktop-agent/VERSION")"
fi

# Restart the launchd agent so the new code runs (this may terminate our parent,
# which is why the caller runs us detached in our own session).
uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.arkive.agent" 2>/dev/null || \
  launchctl kill TERM "gui/${uid}/com.arkive.agent" 2>/dev/null || true

echo "==> $(date -u +%FT%TZ) self-update complete"
