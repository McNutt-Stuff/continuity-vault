#!/usr/bin/env bash
#
# Arkive Desktop Agent — cloud bootstrap for macOS.
#
# Downloads everything from the cloud with curl: a self-contained Python runtime,
# the agent code bundle, and the 1Password CLI. Requires NO git, Xcode, Homebrew,
# or system Python. Served by the cloud at GET /agent/bootstrap and invoked by the
# one-click .command (which exports ARKIVE_CLOUD_URL + ARKIVE_LINKING_CODE).
#
set -euo pipefail

CLOUD_URL="${ARKIVE_CLOUD_URL:-https://vault.arkive.life/api}"
LINKING_CODE="${ARKIVE_LINKING_CODE:-}"
OP_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-}"
HOME_DIR="${ARKIVE_AGENT_HOME:-$HOME/.arkive/home}"
DATA_DIR="${ARKIVE_AGENT_DIR:-$HOME/.arkive-agent}"
PLIST="$HOME/Library/LaunchAgents/com.arkive.agent.plist"
LABEL="com.arkive.agent"
PY_RELEASE="${ARKIVE_PY_RELEASE:-20241016}"
PY_VERSION="${ARKIVE_PY_VERSION:-3.12.7}"
OP_VERSION="${OP_VERSION:-2.30.3}"

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }

mkdir -p "$HOME_DIR" "$DATA_DIR" "$HOME/Library/LaunchAgents"

ARCH="$(uname -m)"
case "$ARCH" in arm64) PYARCH=aarch64; OPARCH=arm64 ;; x86_64) PYARCH=x86_64; OPARCH=amd64 ;; *) PYARCH=aarch64; OPARCH=arm64 ;; esac

say "Downloading a self-contained Python runtime"
PY="$HOME_DIR/python/bin/python3"
if [ ! -x "$PY" ]; then
  PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}+${PY_RELEASE}-${PYARCH}-apple-darwin-install_only.tar.gz"
  curl -fsSL "$PY_URL" -o "$HOME_DIR/python.tar.gz"
  tar -xzf "$HOME_DIR/python.tar.gz" -C "$HOME_DIR"
  rm -f "$HOME_DIR/python.tar.gz"
fi

say "Downloading the agent from the cloud"
curl -fsSL "${CLOUD_URL}/agent/bundle" -o "$HOME_DIR/bundle.tar.gz"
tar -xzf "$HOME_DIR/bundle.tar.gz" -C "$HOME_DIR"
rm -f "$HOME_DIR/bundle.tar.gz"

say "Bundling the 1Password CLI (op)"
OP_DIR="$HOME_DIR/bin"; mkdir -p "$OP_DIR"
if [ ! -x "$OP_DIR/op" ]; then
  tmp="$(mktemp -d)"
  if curl -fsSL "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_darwin_${OPARCH}_v${OP_VERSION}.zip" -o "$tmp/op.zip"; then
    unzip -o -q "$tmp/op.zip" op -d "$OP_DIR" 2>/dev/null || true
    chmod +x "$OP_DIR/op" 2>/dev/null || true
  fi
  rm -rf "$tmp"
fi
OP_PATH="$OP_DIR/op"
[ -x "$OP_PATH" ] && echo "op ready" || echo "!! op unavailable; the agent will report it so"

say "Installing dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet httpx
"$PY" -m pip install --quiet -e "$HOME_DIR/shared"
"$PY" -m pip install --quiet rumps || echo "!! menu-bar UI unavailable; running headless"

if [ -z "$LINKING_CODE" ]; then
  read -rp "Enter the agent linking code from your Arkive portal: " LINKING_CODE
fi

say "Installing the background service"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-m</string>
    <string>agent.menubar</string>
  </array>
  <key>WorkingDirectory</key><string>${HOME_DIR}/desktop-agent</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>${HOME_DIR}/desktop-agent</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>ARKIVE_CLOUD_URL</key><string>${CLOUD_URL}</string>
    <key>ARKIVE_AGENT_HOME</key><string>${HOME_DIR}</string>
    <key>ARKIVE_AGENT_DIR</key><string>${DATA_DIR}</string>
    <key>ARKIVE_LINKING_CODE</key><string>${LINKING_CODE}</string>
    <key>ARKIVE_OP_PATH</key><string>${OP_PATH}</string>
    <key>OP_SERVICE_ACCOUNT_TOKEN</key><string>${OP_TOKEN}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${DATA_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${DATA_DIR}/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF
chmod 600 "$PLIST"

say "Registering with the cloud"
PYTHONPATH="$HOME_DIR/desktop-agent" ARKIVE_CLOUD_URL="$CLOUD_URL" \
ARKIVE_AGENT_HOME="$HOME_DIR" ARKIVE_AGENT_DIR="$DATA_DIR" \
ARKIVE_OP_PATH="$OP_PATH" OP_SERVICE_ACCOUNT_TOKEN="$OP_TOKEN" \
  "$PY" -m agent.main link "$LINKING_CODE"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "==> Arkive desktop agent installed and running (look for the menu-bar icon)."
echo "    Logs: $DATA_DIR/agent.log"
