#!/usr/bin/env bash
#
# Arkive — Cloud All-in-One Installer
# Target: clean Ubuntu 26.04 LTS server
#
# Installs the control plane, sync workers, web portal, and a Caddy reverse
# proxy that obtains a Let's Encrypt certificate for vault.arkive.life.
#
# Polished, resumable installer: each step's output is hidden and logged; a
# failed run can simply be re-run to resume where it stopped. Flags:
#   CV_FORCE=1    redo every step
#   CV_VERBOSE=1  stream command output instead of the spinner
#
# Usage (as root):
#   CV_DOMAIN=vault.arkive.life ./cloud-install.sh
#
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

CV_DOMAIN="${CV_DOMAIN:-vault.arkive.life}"
CV_ADMIN_EMAIL="${CV_ADMIN_EMAIL:-admin@${CV_DOMAIN#*.}}"
INSTALL_DIR="/opt/continuity-vault"
DATA_DIR="/var/lib/continuity-vault"
REPO_SRC="${REPO_SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CV_USER="cvault"
DB_PASSWORD="${CV_DB_PASSWORD:-$(openssl rand -hex 16)}"
export DEBIAN_FRONTEND=noninteractive

# --- step implementations ---------------------------------------------------

install_os_deps() {
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl ca-certificates \
    build-essential libssl-dev postgresql postgresql-contrib debian-keyring \
    debian-archive-keyring apt-transport-https openssl gnupg
}

install_node() {
  command -v node >/dev/null && return 0
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
}

install_caddy() {
  command -v caddy >/dev/null && return 0
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
}

create_user_dirs() {
  id -u "$CV_USER" >/dev/null 2>&1 \
    || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$CV_USER"
  mkdir -p "$INSTALL_DIR" "$DATA_DIR/keystore" "$DATA_DIR/object_store"
}

sync_code() {
  if command -v rsync >/dev/null; then
    rsync -a --delete --exclude '.venv' --exclude 'node_modules' \
      --exclude 'web/dist' "$REPO_SRC/" "$INSTALL_DIR/"
  else
    cp -r "$REPO_SRC/." "$INSTALL_DIR/"
  fi
}

setup_database() {
  local exists
  exists=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='cvault'")
  if [[ "$exists" != "1" ]]; then
    sudo -u postgres psql -c "CREATE USER cvault WITH PASSWORD '${DB_PASSWORD}';"
  else
    sudo -u postgres psql -c "ALTER USER cvault WITH PASSWORD '${DB_PASSWORD}';"
  fi
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='continuity'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE continuity OWNER cvault;"
}

install_python() {
  [[ -d "$INSTALL_DIR/.venv" ]] || python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
  "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR/shared"
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/cloud/requirements.txt"
  # Post-quantum primitives are best-effort; the app runs with a flagged
  # fallback if liboqs cannot be built here.
  "$INSTALL_DIR/.venv/bin/pip" install oqs || true
}

build_web() {
  cd "$INSTALL_DIR/web"
  # No lockfile is shipped, so use a plain install (not `npm ci`).
  npm install --no-audit --no-fund
  npm run build
}

write_env() {
  # Generate secrets only on first write so re-runs don't invalidate sessions.
  if [[ ! -f /etc/continuity-vault.env ]]; then
    local session_secret kek_secret
    session_secret="$(openssl rand -hex 32)"
    kek_secret="$(openssl rand -hex 32)"
    cat > /etc/continuity-vault.env <<EOF
CV_ENVIRONMENT=production
CV_DOMAIN=${CV_DOMAIN}
CV_API_BASE_URL=https://${CV_DOMAIN}/api
CV_DATABASE_URL=postgresql+psycopg://cvault:${DB_PASSWORD}@localhost/continuity
CV_SESSION_SECRET=${session_secret}
CV_RP_ID=${CV_DOMAIN}
CV_RP_ORIGIN=https://${CV_DOMAIN}
CV_KEK_SECRET=${kek_secret}
CV_KEY_STORE=${DATA_DIR}/keystore
CV_OBJECT_STORE=${DATA_DIR}/object_store
CV_FLEET_SIGNER=${DATA_DIR}/fleet_signer.json
CV_SEED_DEMO_DATA=true
EOF
  else
    sed -i "s#^CV_DATABASE_URL=.*#CV_DATABASE_URL=postgresql+psycopg://cvault:${DB_PASSWORD}@localhost/continuity#" \
      /etc/continuity-vault.env
  fi
  chown "$CV_USER":"$CV_USER" /etc/continuity-vault.env
  chmod 600 /etc/continuity-vault.env
  chown -R "$CV_USER":"$CV_USER" "$INSTALL_DIR" "$DATA_DIR"
}

install_service() {
  cp "$INSTALL_DIR/infra/systemd/cv-cloud.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable cv-cloud.service
  systemctl restart cv-cloud.service
}

configure_caddy() {
  sed "s/{{DOMAIN}}/${CV_DOMAIN}/g; s#{{WEBROOT}}#${INSTALL_DIR}/web/dist#g" \
    "$INSTALL_DIR/infra/Caddyfile" > /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
}

health_check() {
  local i
  for i in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:8000/api/health" 2>/dev/null | grep -q '"status":"ok"'; then
      return 0
    fi
    sleep 2
  done
  # Surface why the service is not answering so the log tail is actionable.
  echo "---- service did not become healthy; diagnostics ----"
  systemctl --no-pager status cv-cloud.service 2>&1 || true
  echo "---- recent journal ----"
  journalctl -u cv-cloud.service --no-pager -n 80 2>&1 || true
  return 1
}

# --- run --------------------------------------------------------------------

init_installer "Arkive Cloud" "$DATA_DIR/install-state"
require_root
note "Domain: ${CV_DOMAIN}"

step "Installing system packages"          install_os_deps
step "Installing Node.js 20"               install_node
step "Installing Caddy (Let's Encrypt)"    install_caddy
step "Creating service user & directories" create_user_dirs
step "Copying application files"           sync_code
step "Configuring PostgreSQL database"     setup_database
step "Installing Python control plane"     install_python
step "Building web portal"                 build_web
step "Writing configuration"               write_env
step "Starting control-plane service"      install_service
step "Configuring TLS reverse proxy"       configure_caddy
step "Verifying service health"            health_check

finish
printf "  Portal:  %shttps://%s%s\n" "$BOLD" "$CV_DOMAIN" "$RESET"
printf "  API:     https://%s/api/health\n" "$CV_DOMAIN"
printf "  Sign in: owner@northwind.example (demo seed data enabled)\n\n"
