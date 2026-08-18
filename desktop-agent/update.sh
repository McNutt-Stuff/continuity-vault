#!/usr/bin/env bash
#
# Arkive desktop agent self-update. Pulls the latest code, reinstalls deps, and
# restarts the launchd service. Triggered by a cloud "update" command or run
# manually.
#
set -euo pipefail

HOME_DIR="${ARKIVE_AGENT_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HOME_DIR"

if [ -d .git ]; then
  git fetch --quiet origin
  git reset --hard --quiet "origin/$(git rev-parse --abbrev-ref HEAD)"
fi
"$HOME_DIR/.venv/bin/pip" install -q -r "$HOME_DIR/desktop-agent/requirements.txt"

# Restart the launchd agent so the new code runs.
uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.arkive.agent" 2>/dev/null || \
  launchctl kill TERM "gui/${uid}/com.arkive.agent" 2>/dev/null || true

echo "Arkive agent updated."
