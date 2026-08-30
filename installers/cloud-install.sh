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
export DEBIAN_FRONTEND=noninteractive

# --- node role --------------------------------------------------------------
# Which node role this host runs. Only the components for that role are deployed:
#   control-plane   full stack: control plane API, workers, web admin, CMS
#   customer-tenant data-plane node: API + workers only, NO portal; the control
#                   plane talks to it over /api. Customers use the CP's UI.
#   public-web      marketing website only (no API/DB); heartbeats to CP
# Non-control-plane roles register with the control plane using a shared secret.
# Values are resolved (in order): explicit env > persisted config > interactive
# prompt > default. Prompting happens in prompt_config() during the run.
_persisted() {  # read a KEY from the env file, empty if absent
  [[ -f /etc/continuity-vault.env ]] || return 0
  sed -n "s/^$1=//p" /etc/continuity-vault.env | head -1
}
CV_NODE_ROLE="${CV_NODE_ROLE:-$( [[ -f /etc/arkive/role ]] && cat /etc/arkive/role || _persisted CV_NODE_ROLE)}"
CV_NODE_NAME="${CV_NODE_NAME:-$(_persisted CV_NODE_NAME)}"
CV_NODE_SECRET="${CV_NODE_SECRET:-$(_persisted CV_NODE_SECRET)}"
CV_CONTROL_PLANE_URL="${CV_CONTROL_PLANE_URL:-$(_persisted CV_CONTROL_PLANE_URL)}"
# Domain default only applies to a first control-plane install; prompt_config
# refines it for other roles.
[[ -n "$(_persisted CV_DOMAIN)" && "$CV_DOMAIN" == "vault.arkive.life" ]] && CV_DOMAIN="$(_persisted CV_DOMAIN)"

# Roles that run the Python app (API + workers). Only control-plane also builds
# and serves the customer portal; customer-tenant is API-only.
_runs_app() { [[ "$CV_NODE_ROLE" != "public-web" ]]; }

# Resolve node settings non-interactively. The installer NEVER prompts — a
# node's role and fleet settings come from env vars (baked into the one-line
# install command from the admin Nodes page), a persisted role marker, or the
# existing env file. Anything still unknown defaults to a control-plane node.
# This guarantees existing nodes are never asked to re-choose their role, even
# when a failed update rolls back and re-runs the installer.
prompt_config() {
  CV_NODE_ROLE="${CV_NODE_ROLE:-control-plane}"
  case "$CV_NODE_ROLE" in
    control-plane|customer-tenant|public-web) ;;
    *) echo "Unknown CV_NODE_ROLE='$CV_NODE_ROLE' (control-plane|customer-tenant|public-web)"; exit 1 ;;
  esac
  # Non-control-plane nodes need the control plane URL; default to the canonical
  # one when unset so a misconfigured run still points somewhere sensible.
  if [[ "$CV_NODE_ROLE" != "control-plane" ]]; then
    CV_CONTROL_PLANE_URL="${CV_CONTROL_PLANE_URL:-https://vault.arkive.life}"
  fi
  CV_NODE_NAME="${CV_NODE_NAME:-$CV_DOMAIN}"
  CV_ADMIN_EMAIL="admin@${CV_DOMAIN#*.}"
}

# Keep the DB password stable across runs so the role and the app config never
# drift: prefer an explicit override, then the value already in the env file,
# else generate a fresh one.
_existing_db_pw() {
  [[ -f /etc/continuity-vault.env ]] || return 0
  sed -n 's#^CV_DATABASE_URL=postgresql+psycopg://cvault:\(.*\)@localhost/continuity#\1#p' \
    /etc/continuity-vault.env | head -1
}
DB_PASSWORD="${CV_DB_PASSWORD:-$(_existing_db_pw)}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"

# Shared control-plane DB (opt-in). A customer-tenant node can point at the
# control plane's Postgres so it sees the same tenants/sources and writes the
# same search index — required for per-node sync scoping (CV_NODE_SYNC_SCOPE).
# Provide CV_DATABASE_URL at install time (or leave a non-local URL in the env
# file) to use it; the installer then skips local Postgres and never overrides it.
_current_db_url() {
  [[ -f /etc/continuity-vault.env ]] || return 0
  sed -n 's#^CV_DATABASE_URL=##p' /etc/continuity-vault.env | head -1
}
EXTERNAL_DB_URL="${CV_DATABASE_URL:-}"
if [[ -z "$EXTERNAL_DB_URL" ]]; then
  _cur="$(_current_db_url)"
  [[ -n "$_cur" && "$_cur" != *"@localhost/"* ]] && EXTERNAL_DB_URL="$_cur"
fi
USE_EXTERNAL_DB=0
[[ -n "$EXTERNAL_DB_URL" && "$EXTERNAL_DB_URL" != *"@localhost/"* ]] && USE_EXTERNAL_DB=1
if [[ "$USE_EXTERNAL_DB" == "1" ]]; then
  DB_URL="$EXTERNAL_DB_URL"
else
  DB_URL="postgresql+psycopg://cvault:${DB_PASSWORD}@localhost/continuity"
fi

# Only the control plane seeds demo tenants; a customer-tenant node's tenant data
# arrives by replication from the control plane, so it must not seed its own.
if [[ "$CV_NODE_ROLE" == "control-plane" ]]; then SEED_DEMO=true; else SEED_DEMO=false; fi

# Native liboqs version to build for real post-quantum crypto (ML-KEM / ML-DSA /
# SLH-DSA). MUST match the liboqs-python binding version installed below so the
# binding loads our system library instead of auto-building its own.
LIBOQS_VERSION="${LIBOQS_VERSION:-0.16.0}"
# Where the native liboqs is installed; the binding and the service load from here.
OQS_PREFIX="/usr/local"

# --- step implementations ---------------------------------------------------

install_os_deps() {
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl ca-certificates \
    build-essential libssl-dev postgresql postgresql-contrib debian-keyring \
    debian-archive-keyring apt-transport-https openssl gnupg rsync
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
    # Keep the built web/ and site/ dist dirs: --delete would otherwise wipe the
    # live site before it's rebuilt, taking a public-web node down if the new
    # build fails. The build steps overwrite dist on success.
    rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'node_modules' \
      --exclude 'web/dist' --exclude 'site/dist' "$REPO_SRC/" "$INSTALL_DIR/"
  else
    cp -r "$REPO_SRC/." "$INSTALL_DIR/"
  fi
  chmod +x "$INSTALL_DIR"/installers/*.sh "$INSTALL_DIR"/updater/*.sh 2>/dev/null || true
}

set_node_hostname() {
  # System hostname = the node's configured name (CV_HOSTNAME override wins).
  set_system_hostname "${CV_HOSTNAME:-${CV_NODE_NAME:-$CV_DOMAIN}}"
}

setup_node_control() {
  # Let the service account control its managed units + read the journal so the
  # admin node console can restart/stop services and stream logs (scoped, no pw).
  local f=/etc/sudoers.d/cv-cloud
  : > "$f"
  for u in cv-cloud postgresql caddy cv-node-heartbeat.timer cv-backup.timer cv-backup.service cv-node-update.timer cv-cloud-update.timer; do
    for a in start stop restart enable disable; do
      echo "${CV_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl ${a} ${u}" >> "$f"
    done
  done
  # Allow setting the host timezone so a CV_TIMEZONE config profile reflects in
  # journalctl / OS logs (the app calls `sudo -n timedatectl set-timezone <tz>`).
  echo "${CV_USER} ALL=(root) NOPASSWD: /usr/bin/timedatectl set-timezone *" >> "$f"
  chmod 440 "$f"
  usermod -aG systemd-journal "${CV_USER}" 2>/dev/null || true
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
}

# True when the real liboqs binding is importable and the required algorithms
# are available (i.e. genuine post-quantum crypto, not the classical fallback).
build_pqcrypto() {
  ensure_liboqs "$INSTALL_DIR/.venv" "$LIBOQS_VERSION" "$OQS_PREFIX"
}

build_web() {
  cd "$INSTALL_DIR/web"
  # No lockfile is shipped, so use a plain install (not `npm ci`).
  npm install --no-audit --no-fund
  npm run build
}

# Marketing website (public-web node). Builds site/ into site/dist.
build_site() {
  cd "$INSTALL_DIR/site"
  npm install --no-audit --no-fund
  npm run build
}

validate_app() {
  # Import the app in a throwaway environment so import/route errors surface
  # here with a full traceback, before the service is started.
  cd "$INSTALL_DIR/cloud"
  CV_DATABASE_URL="sqlite:////tmp/cv_probe.db" \
  CV_SEED_DEMO_DATA=false \
  CV_KEY_STORE=/tmp/cv_probe_keys \
  CV_OBJECT_STORE=/tmp/cv_probe_obj \
  CV_FLEET_SIGNER=/tmp/cv_probe_signer.json \
  OQS_INSTALL_PATH="$OQS_PREFIX" \
    "$INSTALL_DIR/.venv/bin/python" -c "import app.main; print('app import OK:', len(app.main.app.routes), 'routes')"
  rm -rf /tmp/cv_probe.db /tmp/cv_probe_keys /tmp/cv_probe_obj /tmp/cv_probe_signer.json
}

write_env() {
  # Generate secrets only on first write so re-runs don't invalidate sessions.
  if [[ ! -f /etc/continuity-vault.env ]]; then
    local session_secret kek_secret
    # Fleet-shared for federation: a node must validate the control plane's
    # session tokens (proxy auth) and use its wrapped keys/creds — provide the
    # SAME CV_SESSION_SECRET + CV_KEK_SECRET across every node.
    session_secret="${CV_SESSION_SECRET:-$(openssl rand -hex 32)}"
    kek_secret="${CV_KEK_SECRET:-$(openssl rand -hex 32)}"
    cat > /etc/continuity-vault.env <<EOF
CV_ENVIRONMENT=production
CV_DOMAIN=${CV_DOMAIN}
CV_API_BASE_URL=https://${CV_DOMAIN}/api
CV_DATABASE_URL=${DB_URL}
CV_SESSION_SECRET=${session_secret}
CV_RP_ID=${CV_DOMAIN}
CV_RP_ORIGIN=https://${CV_DOMAIN}
CV_KEK_SECRET=${kek_secret}
CV_KEY_STORE=${DATA_DIR}/keystore
CV_OBJECT_STORE=${DATA_DIR}/object_store
CV_FLEET_SIGNER=${DATA_DIR}/fleet_signer.json
CV_SEED_DEMO_DATA=${SEED_DEMO}
CV_ALLOW_SIGNUP=true
# Node role & fleet membership. Non-control-plane nodes heartbeat to the CP.
CV_NODE_ROLE=${CV_NODE_ROLE}
CV_NODE_NAME=${CV_NODE_NAME}
CV_NODE_SECRET=${CV_NODE_SECRET}
CV_CONTROL_PLANE_URL=${CV_CONTROL_PLANE_URL}
CV_SITE_CONTENT_PATH=${INSTALL_DIR}/site/dist/site.json
# Email delivery for sign-in / verification codes. Without SMTP, codes are
# written to the service log (journalctl -u cv-cloud). Uncomment to enable:
# CV_SMTP_HOST=smtp.example.com
# CV_SMTP_PORT=587
# CV_SMTP_USER=apikey
# CV_SMTP_PASSWORD=your-smtp-password
# CV_SMTP_FROM=no-reply@arkive.life
# Connector OAuth apps (redirect URI: https://${CV_DOMAIN}/api/connectors/oauth/callback).
# Gmail (Google Cloud Console):
# CV_GOOGLE_CLIENT_ID=...
# CV_GOOGLE_CLIENT_SECRET=...
# Outlook / OneDrive (Microsoft Entra ID):
# CV_MICROSOFT_CLIENT_ID=...
# CV_MICROSOFT_CLIENT_SECRET=...
# Dropbox (dropbox.com/developers):
# CV_DROPBOX_CLIENT_ID=...
# CV_DROPBOX_CLIENT_SECRET=...
EOF
  else
    # Keep the DB URL current, but never clobber an external (shared) DB URL.
    if [[ "$USE_EXTERNAL_DB" != "1" ]]; then
      sed -i "s#^CV_DATABASE_URL=.*#CV_DATABASE_URL=postgresql+psycopg://cvault:${DB_PASSWORD}@localhost/continuity#" \
        /etc/continuity-vault.env
    fi
  fi
  # Keep node-role membership in sync on every run (upsert each key).
  local k v
  for kv in "CV_NODE_ROLE=${CV_NODE_ROLE}" "CV_NODE_NAME=${CV_NODE_NAME}" \
            "CV_NODE_SECRET=${CV_NODE_SECRET}" "CV_CONTROL_PLANE_URL=${CV_CONTROL_PLANE_URL}" \
            "CV_SITE_CONTENT_PATH=${INSTALL_DIR}/site/dist/site.json" \
            "CV_SUPPORT_CONTENT_PATH=${INSTALL_DIR}/site/dist/support.json"; do
    k="${kv%%=*}"; v="${kv#*=}"
    if grep -q "^${k}=" /etc/continuity-vault.env; then
      sed -i "s#^${k}=.*#${k}=${v}#" /etc/continuity-vault.env
    else
      echo "${k}=${v}" >> /etc/continuity-vault.env
    fi
  done
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

# API-only reverse proxy for a customer-tenant (data-plane) node: proxies /api
# to the local app for control-plane communication, serves NO public portal.
configure_caddy_node() {
  sed "s/{{DOMAIN}}/${CV_DOMAIN}/g" \
    "$INSTALL_DIR/infra/Caddyfile.node" > /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
}

# TLS reverse proxy for a public-web node: serves the static marketing site,
# no /api proxy.
configure_caddy_site() {
  sed "s/{{SITE_DOMAIN}}/${CV_DOMAIN}/g; s#{{SITE_WEBROOT}}#${INSTALL_DIR}/site/dist#g" \
    "$INSTALL_DIR/infra/Caddyfile.site" > /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
}

# Record the node role + a version marker the updater/heartbeat consume.
write_node_marker() {
  mkdir -p /etc/arkive
  echo "$CV_NODE_ROLE" > /etc/arkive/role
  git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null > /etc/arkive/version || true
  # Bundle nodes ship a NODE_VERSION stamp; record it so self-update can diff.
  [[ -f "$REPO_SRC/NODE_VERSION" ]] && cp "$REPO_SRC/NODE_VERSION" /etc/arkive/bundle-version
  chown -R "$CV_USER":"$CV_USER" /etc/arkive 2>/dev/null || true
}

# Install the fleet heartbeat timer on non-control-plane nodes so they report
# health to the control plane and receive their role blueprint.
install_heartbeat() {
  cp "$INSTALL_DIR/infra/systemd/cv-node-heartbeat.service" /etc/systemd/system/
  cp "$INSTALL_DIR/infra/systemd/cv-node-heartbeat.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now cv-node-heartbeat.timer
}

# Install the self-update timer on fleet nodes so they pull new bundles from the
# control plane (never GitHub) on a schedule.
install_node_update() {
  cp "$INSTALL_DIR/infra/systemd/cv-node-update.service" /etc/systemd/system/
  cp "$INSTALL_DIR/infra/systemd/cv-node-update.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now cv-node-update.timer
}

# Install the infrastructure backup timer (database + keys + config → backup
# storage services). Runs on every app node (control plane + customer-tenant).
install_backup() {
  cp "$INSTALL_DIR/infra/systemd/cv-backup.service" /etc/systemd/system/
  cp "$INSTALL_DIR/infra/systemd/cv-backup.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now cv-backup.timer
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
prompt_config
note "Domain: ${CV_DOMAIN}"
note "Node role: ${CV_NODE_ROLE}"

step "Installing system packages"          install_os_deps
step "Installing Node.js 20"               install_node
step "Installing Caddy (Let's Encrypt)"    install_caddy
step "Creating service user & directories" create_user_dirs
step_always "Copying application files"    sync_code

if [[ "$CV_NODE_ROLE" == "public-web" ]]; then
  # Public Web Node — marketing site only, no API/DB. A minimal Python venv is
  # installed solely to run the fleet heartbeat.
  step_if_changed "Installing Python (heartbeat)" \
    "$INSTALL_DIR/cloud/requirements.txt $INSTALL_DIR/shared $INSTALL_DIR/.venv/pyvenv.cfg" \
    install_python
  step_if_changed "Building marketing website" \
    "$INSTALL_DIR/site/src $INSTALL_DIR/site/package.json $INSTALL_DIR/site/index.html $INSTALL_DIR/site/vite.config.ts $INSTALL_DIR/site/dist/index.html" \
    build_site
  step_always "Writing configuration"        write_env
  step_always "Recording node marker"        write_node_marker
  step_always "Configuring TLS reverse proxy" configure_caddy_site
  step_always "Installing fleet heartbeat"   install_heartbeat
  step_always "Installing self-update timer" install_node_update
  finish
  printf "  Website: %shttps://%s%s\n" "$BOLD" "$CV_DOMAIN" "$RESET"
  printf "  Role:    public-web (reports to %s)\n\n" "${CV_CONTROL_PLANE_URL:-<control plane>}"
  return 0 2>/dev/null || exit 0
fi

# control-plane and customer-tenant both run the Python application stack.
# Skip local Postgres when this node uses the shared control-plane database.
if [[ "$USE_EXTERNAL_DB" == "1" ]]; then
  note "Using shared control-plane database (skipping local PostgreSQL setup)"
else
  step_always "Configuring PostgreSQL database" setup_database
fi
step_if_changed "Installing Python control plane" \
  "$INSTALL_DIR/cloud/requirements.txt $INSTALL_DIR/shared $INSTALL_DIR/.venv/pyvenv.cfg" \
  install_python
step_if_changed "Building quantum-safe crypto (liboqs)" \
  "$INSTALL_DIR/shared $INSTALL_DIR/.venv/.pq-ok" \
  build_pqcrypto
# Only the control plane serves the customer portal. A customer-tenant
# (data-plane) node runs the API + workers and talks to the control plane only.
if [[ "$CV_NODE_ROLE" == "control-plane" ]]; then
  step_if_changed "Building web portal" \
    "$INSTALL_DIR/web/src $INSTALL_DIR/web/package.json $INSTALL_DIR/web/index.html $INSTALL_DIR/web/vite.config.ts $INSTALL_DIR/web/tsconfig.json $INSTALL_DIR/web/tsconfig.node.json $INSTALL_DIR/web/dist/index.html" \
    build_web
fi
step_always "Validating application"       validate_app
step_always "Writing configuration"        write_env
step_always "Recording node marker"        write_node_marker
step_always "Setting system hostname"      set_node_hostname
step_always "Enabling node control"        setup_node_control
step_always "Starting control-plane service" install_service
step_always "Installing infrastructure backup timer" install_backup
if [[ "$CV_NODE_ROLE" == "customer-tenant" ]]; then
  step_always "Configuring API-only reverse proxy" configure_caddy_node
  step_always "Installing fleet heartbeat"   install_heartbeat
  step_always "Installing self-update timer" install_node_update
else
  step_always "Configuring TLS reverse proxy" configure_caddy
fi
step_always "Verifying service health"     health_check

finish
if [[ "$CV_NODE_ROLE" == "customer-tenant" ]]; then
  printf "  API:     %shttps://%s/api/health%s (data-plane — no portal)\n" "$BOLD" "$CV_DOMAIN" "$RESET"
  printf "  Role:    customer-tenant (reports to %s)\n\n" "${CV_CONTROL_PLANE_URL:-<control plane>}"
else
  printf "  Portal:  %shttps://%s%s\n" "$BOLD" "$CV_DOMAIN" "$RESET"
  printf "  API:     https://%s/api/health\n" "$CV_DOMAIN"
  printf "  Role:    %s\n" "$CV_NODE_ROLE"
  printf "  Sign in: owner@northwind.example (demo seed data enabled)\n\n"
fi
