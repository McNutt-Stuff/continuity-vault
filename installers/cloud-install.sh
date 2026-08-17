#!/usr/bin/env bash
#
# Arkive — Cloud All-in-One Installer
# Target: clean Ubuntu 26.04 LTS server
#
# Installs the control plane, sync workers, web portal, and a Caddy reverse
# proxy that obtains a Let's Encrypt certificate for vault.arkive.life.
#
# Usage (as root):
#   CV_DOMAIN=vault.arkive.life ./cloud-install.sh
#
set -euo pipefail

CV_DOMAIN="${CV_DOMAIN:-vault.arkive.life}"
CV_ADMIN_EMAIL="${CV_ADMIN_EMAIL:-admin@${CV_DOMAIN#*.}}"
INSTALL_DIR="/opt/continuity-vault"
DATA_DIR="/var/lib/continuity-vault"
REPO_SRC="${REPO_SRC:-$(cd "$(dirname "$0")/.." && pwd)}"
CV_USER="cvault"

echo "==> Arkive cloud installer for ${CV_DOMAIN}"

if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi

echo "==> Installing OS dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates \
  build-essential libssl-dev postgresql postgresql-contrib debian-keyring \
  debian-archive-keyring apt-transport-https

echo "==> Installing Node.js 20 (for the web portal build)"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "==> Installing Caddy (automatic Let's Encrypt TLS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> Creating service user and directories"
id -u "$CV_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$CV_USER"
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp -r "$REPO_SRC/." "$INSTALL_DIR/"

echo "==> Setting up PostgreSQL database"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='cvault'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER cvault WITH PASSWORD 'change-me-in-production';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='continuity'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE continuity OWNER cvault;"

echo "==> Creating Python virtualenv and installing the control plane"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR/shared"
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/cloud/requirements.txt"
# Post-quantum primitives (best-effort; falls back cleanly if it can't build).
"$INSTALL_DIR/.venv/bin/pip" install oqs || echo "!! liboqs not installed — running with flagged PQC fallback"

echo "==> Building the web portal"
pushd "$INSTALL_DIR/web" >/dev/null
npm ci || npm install
npm run build
popd >/dev/null

echo "==> Writing environment file"
SESSION_SECRET="$(openssl rand -hex 32)"
KEK_SECRET="$(openssl rand -hex 32)"
cat > /etc/continuity-vault.env <<EOF
CV_ENVIRONMENT=production
CV_DOMAIN=${CV_DOMAIN}
CV_API_BASE_URL=https://${CV_DOMAIN}/api
CV_DATABASE_URL=postgresql+psycopg://cvault:change-me-in-production@localhost/continuity
CV_SESSION_SECRET=${SESSION_SECRET}
CV_RP_ID=${CV_DOMAIN}
CV_RP_ORIGIN=https://${CV_DOMAIN}
CV_KEK_SECRET=${KEK_SECRET}
CV_KEY_STORE=${DATA_DIR}/keystore
CV_FLEET_SIGNER=${DATA_DIR}/fleet_signer.json
CV_SEED_DEMO_DATA=true
EOF
chown "$CV_USER":"$CV_USER" /etc/continuity-vault.env
chmod 600 /etc/continuity-vault.env
mkdir -p "$DATA_DIR/keystore"
chown -R "$CV_USER":"$CV_USER" "$INSTALL_DIR" "$DATA_DIR"

echo "==> Installing systemd services"
cp "$INSTALL_DIR/infra/systemd/cv-cloud.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cv-cloud.service

echo "==> Configuring Caddy for ${CV_DOMAIN}"
sed "s/{{DOMAIN}}/${CV_DOMAIN}/g; s#{{WEBROOT}}#${INSTALL_DIR}/web/dist#g" \
  "$INSTALL_DIR/infra/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo ""
echo "==> Done."
echo "    Portal:  https://${CV_DOMAIN}"
echo "    API:     https://${CV_DOMAIN}/api/health"
echo "    Sign in with owner@northwind.example (demo seed data enabled)."
