#!/usr/bin/env bash
#
# Arkive — Offline Appliance Installer
# Target: clean Ubuntu 26.04 LTS on appliance hardware
#
# Turnkey: installs the appliance agent, prompts once for the linking code,
# and configures the agent to pull all further configuration from the cloud.
#
# Usage (as root):
#   CV_CLOUD_URL=https://vault.arkive.life/api ./appliance-install.sh
#   # optionally pass the code non-interactively:
#   CV_LINKING_CODE=CV-ABC123-DEF456 ./appliance-install.sh
#
set -euo pipefail

CV_CLOUD_URL="${CV_CLOUD_URL:-https://vault.arkive.life/api}"
INSTALL_DIR="/opt/continuity-vault"
DATA_DIR="/var/lib/continuity-vault-appliance"
REPO_SRC="${REPO_SRC:-$(cd "$(dirname "$0")/.." && pwd)}"
CV_USER="cvagent"

echo "==> Arkive appliance installer"
echo "    Cloud control plane: ${CV_CLOUD_URL}"

if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi

echo "==> Installing OS dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl ca-certificates \
  build-essential libssl-dev tpm2-tools

echo "==> Creating service user and directories"
id -u "$CV_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$CV_USER"
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp -r "$REPO_SRC/." "$INSTALL_DIR/"

echo "==> Creating Python virtualenv and installing the agent"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR/shared"
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/appliance/requirements.txt"
"$INSTALL_DIR/.venv/bin/pip" install oqs || echo "!! liboqs not installed — flagged PQC fallback active"

# Prompt for the one-time linking code if not supplied.
LINKING_CODE="${CV_LINKING_CODE:-}"
if [[ -z "$LINKING_CODE" ]]; then
  read -rp "Enter the linking code from your Arkive portal: " LINKING_CODE
fi

echo "==> Writing appliance configuration"
mkdir -p /etc/continuity-vault
cat > /etc/continuity-vault/appliance.env <<EOF
CVA_CLOUD_BASE_URL=${CV_CLOUD_URL}
CVA_DATA_DIR=${DATA_DIR}/data
CVA_LINKING_CODE=${LINKING_CODE}
CVA_MODEL=CV Edge 8
CVA_SOFTWARE_VERSION=1.0.0
CVA_REQUIRE_LOCAL_RECOVERY_APPROVAL=true
EOF
chmod 600 /etc/continuity-vault/appliance.env
chown -R "$CV_USER":"$CV_USER" "$INSTALL_DIR" "$DATA_DIR" /etc/continuity-vault

echo "==> Installing systemd service"
cp "$INSTALL_DIR/infra/systemd/cv-appliance-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cv-appliance-agent.service

echo ""
echo "==> Appliance activated and reporting to ${CV_CLOUD_URL}"
echo "    Local status: http://127.0.0.1:8090/status"
