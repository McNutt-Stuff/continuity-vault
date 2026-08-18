#!/usr/bin/env bash
#
# Arkive Appliance — cloud bootstrap for a clean Ubuntu host.
#
# Downloads the appliance install bundle from the cloud with curl (no git),
# extracts it, and runs the installer, which registers the appliance and
# installs a headless self-update timer. Served by the cloud at
# GET /appliance/bootstrap and invoked by the one-line install command shown in
# the portal (which bakes in CV_CLOUD_URL + CV_LINKING_CODE).
#
set -euo pipefail

CLOUD_URL="${CV_CLOUD_URL:-https://vault.arkive.life/api}"
LINKING_CODE="${CV_LINKING_CODE:-}"
SRC_DIR="${ARKIVE_SRC_DIR:-/opt/arkive-src}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }
command -v tar  >/dev/null 2>&1 || { apt-get update -y && apt-get install -y tar; }

echo "==> Downloading the appliance bundle from ${CLOUD_URL}"
mkdir -p "$SRC_DIR"
curl -fsSL "${CLOUD_URL}/appliance/bundle" -o /tmp/arkive-appliance.tar.gz
tar -xzf /tmp/arkive-appliance.tar.gz -C "$SRC_DIR"
rm -f /tmp/arkive-appliance.tar.gz
chmod +x "$SRC_DIR"/installers/*.sh "$SRC_DIR"/updater/*.sh 2>/dev/null || true

echo "==> Installing the appliance"
CV_CLOUD_URL="$CLOUD_URL" CV_LINKING_CODE="$LINKING_CODE" \
  bash "$SRC_DIR/installers/appliance-install.sh"
