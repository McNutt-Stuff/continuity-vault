#!/usr/bin/env bash
#
# Arkive — one-line node bootstrap for a clean Ubuntu host.
#
# Served by the control plane at GET /api/nodes/bootstrap and invoked by the
# one-line install command shown on the admin Nodes page, which bakes in the
# node role, domain, control-plane URL and fleet enrollment secret as env vars:
#
#   curl -fsSL "https://vault.arkive.life/api/nodes/bootstrap" -o /tmp/arkive-node.sh && \
#     sudo CV_NODE_ROLE="public-web" CV_DOMAIN="arkive.life" \
#       CV_CONTROL_PLANE_URL="https://vault.arkive.life" \
#       CV_NODE_SECRET="..." bash /tmp/arkive-node.sh
#
# Installs git, fetches (or updates) the source checkout, and hands off to the
# git-based updater, which runs the idempotent, role-aware installer. The same
# command performs a first install AND every subsequent update. When the role
# and fleet settings aren't provided as env vars, the installer prompts for them.
#
set -euo pipefail

SRC_DIR="${CV_SRC_DIR:-/opt/arkive-src}"
REPO_URL="${CV_REPO_URL:-https://github.com/mcnutter1/continuity-vault.git}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (use: curl ... | sudo bash)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }
git config --global --add safe.directory "$SRC_DIR" 2>/dev/null || true

if [ -d "$SRC_DIR/.git" ]; then
  echo "==> Updating source checkout in $SRC_DIR"
  git -C "$SRC_DIR" fetch --quiet origin || true
else
  echo "==> Cloning Arkive into $SRC_DIR"
  git clone --quiet "$REPO_URL" "$SRC_DIR"
fi
chmod +x "$SRC_DIR"/updater/*.sh "$SRC_DIR"/installers/*.sh 2>/dev/null || true

exec "$SRC_DIR/updater/git-update.sh" cloud
