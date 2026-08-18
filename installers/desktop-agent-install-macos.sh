#!/usr/bin/env bash
#
# Arkive Desktop Agent — macOS installer.
#
# Bundles everything needed: clones the agent, sets up a Python environment,
# ensures the 1Password CLI is present, installs a launchd service, and
# registers with the cloud using a linking code.
#
# Usage:
#   ARKIVE_CLOUD_URL=https://vault.arkive.life/api \
#   ARKIVE_LINKING_CODE=AG-XXXX-YYYY ./desktop-agent-install-macos.sh
#
# The linking code and (optional) 1Password service-account token are prompted
# for if not supplied. Runs per-user (no root required).
#
set -euo pipefail

REPO_URL="${ARKIVE_REPO_URL:-https://github.com/mcnutter1/continuity-vault.git}"
REPO_BRANCH="${ARKIVE_REPO_BRANCH:-main}"
HOME_DIR="${ARKIVE_AGENT_HOME:-$HOME/.arkive/home}"
DATA_DIR="${ARKIVE_AGENT_DIR:-$HOME/.arkive-agent}"
CLOUD_URL="${ARKIVE_CLOUD_URL:-https://vault.arkive.life/api}"
PLIST="$HOME/Library/LaunchAgents/com.arkive.agent.plist"
LABEL="com.arkive.agent"

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }

command -v git >/dev/null || { echo "git is required (install Xcode CLT: xcode-select --install)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

say "Fetching the agent into $HOME_DIR"
mkdir -p "$(dirname "$HOME_DIR")" "$DATA_DIR"
if [ -d "$HOME_DIR/.git" ]; then
  git -C "$HOME_DIR" fetch --quiet origin "$REPO_BRANCH"
  git -C "$HOME_DIR" reset --hard --quiet "origin/$REPO_BRANCH"
else
  git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$HOME_DIR"
fi
chmod +x "$HOME_DIR"/desktop-agent/*.sh "$HOME_DIR"/installers/*.sh "$HOME_DIR"/updater/*.sh 2>/dev/null || true

say "Setting up the Python environment"
[ -d "$HOME_DIR/.venv" ] || python3 -m venv "$HOME_DIR/.venv"
"$HOME_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOME_DIR/.venv/bin/pip" install --quiet -r "$HOME_DIR/desktop-agent/requirements.txt"
# Shared crypto for client-side (endpoint) encryption.
"$HOME_DIR/.venv/bin/pip" install --quiet -e "$HOME_DIR/shared"
# Friendly menu-bar UI (optional; falls back to headless if it can't install).
"$HOME_DIR/.venv/bin/pip" install --quiet rumps || echo "!! menu-bar UI unavailable; running headless"

say "Bundling the 1Password CLI (op)"
OP_VERSION="${OP_VERSION:-2.30.3}"
OP_DIR="$HOME_DIR/bin"; mkdir -p "$OP_DIR"
if [ ! -x "$OP_DIR/op" ]; then
  if command -v op >/dev/null; then
    ln -sf "$(command -v op)" "$OP_DIR/op"
  else
    tmp="$(mktemp -d)"
    if curl -fsSL "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_apple_universal_v${OP_VERSION}.zip" -o "$tmp/op.zip"; then
      unzip -o -q "$tmp/op.zip" op -d "$OP_DIR" 2>/dev/null || true
      chmod +x "$OP_DIR/op" 2>/dev/null || true
    fi
    rm -rf "$tmp"
  fi
fi
[ -x "$OP_DIR/op" ] && echo "op ready at $OP_DIR/op" || echo "!! could not bundle op; the agent will report it as unavailable"
OP_PATH="$OP_DIR/op"

# Collect linking code + optional service-account token.
LINKING_CODE="${ARKIVE_LINKING_CODE:-}"
if [ -z "$LINKING_CODE" ]; then
  read -rp "Enter the agent linking code from your Arkive portal: " LINKING_CODE
fi
OP_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-}"
if [ -z "$OP_TOKEN" ]; then
  echo "Optional: paste a 1Password service-account token for unattended sync"
  read -rsp "(leave blank to use the interactive 1Password app integration): " OP_TOKEN
  echo ""
fi

say "Installing the launchd service"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${HOME_DIR}/.venv/bin/python</string>
    <string>-m</string>
    <string>agent.menubar</string>
  </array>
  <key>WorkingDirectory</key><string>${HOME_DIR}/desktop-agent</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>${HOME_DIR}/desktop-agent</string>
    <key>ARKIVE_CLOUD_URL</key><string>${CLOUD_URL}</string>
    <key>ARKIVE_AGENT_HOME</key><string>${HOME_DIR}</string>
    <key>ARKIVE_AGENT_DIR</key><string>${DATA_DIR}</string>
    <key>ARKIVE_LINKING_CODE</key><string>${LINKING_CODE}</string>
    <key>ARKIVE_OP_PATH</key><string>${OP_PATH}</string>
    <key>OP_SERVICE_ACCOUNT_TOKEN</key><string>${OP_TOKEN}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${DATA_DIR}/agent.log</string>
  <key>StandardErrorPath</key><string>${DATA_DIR}/agent.err</string>
</dict>
</plist>
PLIST_EOF
chmod 600 "$PLIST"

say "Registering with the cloud"
PYTHONPATH="$HOME_DIR/desktop-agent" \
ARKIVE_CLOUD_URL="$CLOUD_URL" ARKIVE_AGENT_HOME="$HOME_DIR" \
ARKIVE_AGENT_DIR="$DATA_DIR" OP_SERVICE_ACCOUNT_TOKEN="$OP_TOKEN" \
  "$HOME_DIR/.venv/bin/python" -m agent.main link "$LINKING_CODE"

say "Starting the service"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "==> Arkive desktop agent installed and running."
echo "    Status: $HOME_DIR/.venv/bin/python -m agent.main status  (cwd: $HOME_DIR/desktop-agent)"
echo "    Logs:   $DATA_DIR/agent.log"
