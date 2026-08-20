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
# Fleet nodes (public-web, customer-tenant) download their code + content as a
# bundle FROM THE CONTROL PLANE — never from GitHub — then run the role-aware
# installer. A control-plane node (the source of truth) uses the git updater.
#
set -euo pipefail

SRC_DIR="${CV_SRC_DIR:-/opt/arkive-src}"
ROLE="${CV_NODE_ROLE:-control-plane}"
CP="${CV_CONTROL_PLANE_URL:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (use: curl ... | sudo bash)." >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive

if [ "$ROLE" != "control-plane" ]; then
  # ---- Fleet node: pull the bundle from the control plane (no git/GitHub) ----
  if [ -z "$CP" ]; then
    echo "CV_CONTROL_PLANE_URL is required for a ${ROLE} node." >&2
    exit 1
  fi
  command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }
  command -v tar  >/dev/null 2>&1 || { apt-get update -y && apt-get install -y tar; }
  echo "==> Downloading node bundle from ${CP}"
  mkdir -p "$SRC_DIR"
  curl -fsSL "${CP%/}/api/nodes/bundle" -o /tmp/arkive-node-bundle.tar.gz
  tar -xzf /tmp/arkive-node-bundle.tar.gz -C "$SRC_DIR"
  rm -f /tmp/arkive-node-bundle.tar.gz
  chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true
  echo "==> Installing (${ROLE})"
  exec env REPO_SRC="$SRC_DIR" bash "$SRC_DIR/installers/cloud-install.sh"
fi

# ---- Control plane: git-based from the upstream repo ----
REPO_URL="${CV_REPO_URL:-https://github.com/mcnutter1/continuity-vault.git}"
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
