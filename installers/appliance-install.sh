#!/usr/bin/env bash
#
# Arkive — Offline Appliance Installer
# Target: clean Ubuntu 26.04 LTS on appliance hardware
#
# Turnkey: installs the appliance agent, prompts once for the linking code,
# and configures the agent to pull all further configuration from the cloud.
#
# Polished, resumable installer: step output is hidden and logged; a failed run
# can be re-run to resume. Flags: CV_FORCE=1 (redo all), CV_VERBOSE=1 (stream).
#
# Usage (as root):
#   CV_CLOUD_URL=https://vault.arkive.life/api ./appliance-install.sh
#   # optionally pass the code non-interactively:
#   CV_LINKING_CODE=CV-ABC123-DEF456 ./appliance-install.sh
#
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

CV_CLOUD_URL="${CV_CLOUD_URL:-https://vault.arkive.life/api}"
INSTALL_DIR="/opt/continuity-vault"
DATA_DIR="/var/lib/continuity-vault-appliance"
REPO_SRC="${REPO_SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CV_USER="cvagent"
CV_MODEL="${CV_MODEL:-CV Edge 8}"
# Native liboqs version for real post-quantum crypto (matches the binding ABI).
LIBOQS_VERSION="${LIBOQS_VERSION:-0.16.0}"
OQS_PREFIX="/usr/local"
export DEBIAN_FRONTEND=noninteractive

# --- step implementations ---------------------------------------------------

install_os_deps() {
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip curl ca-certificates \
    build-essential libssl-dev openssl tpm2-tools git rsync
}

create_user_dirs() {
  id -u "$CV_USER" >/dev/null 2>&1 \
    || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$CV_USER"
  mkdir -p "$INSTALL_DIR" "$DATA_DIR/data"
}

sync_code() {
  if command -v rsync >/dev/null; then
    rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'node_modules' \
      "$REPO_SRC/" "$INSTALL_DIR/"
  else
    cp -r "$REPO_SRC/." "$INSTALL_DIR/"
  fi
  chmod +x "$INSTALL_DIR"/installers/*.sh "$INSTALL_DIR"/updater/*.sh 2>/dev/null || true
}

install_python() {
  [[ -d "$INSTALL_DIR/.venv" ]] || python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
  "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR/shared"
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/appliance/requirements.txt"
}

build_pqcrypto() {
  ensure_liboqs "$INSTALL_DIR/.venv" "$LIBOQS_VERSION" "$OQS_PREFIX"
}

validate_app() {
  # Import the agent in a throwaway data dir to surface import errors early.
  cd "$INSTALL_DIR/appliance"
  CVA_DATA_DIR=/tmp/cv_probe_agent \
  OQS_INSTALL_PATH="$OQS_PREFIX" \
    "$INSTALL_DIR/.venv/bin/python" -c "import agent.main; print('agent import OK')"
  rm -rf /tmp/cv_probe_agent
}

write_config() {
  mkdir -p /etc/continuity-vault
  local ver; ver="$(cat "$INSTALL_DIR/appliance/VERSION" 2>/dev/null || echo 1.0.0)"
  if [[ ! -f /etc/continuity-vault/appliance.env ]]; then
    cat > /etc/continuity-vault/appliance.env <<EOF
CVA_CLOUD_BASE_URL=${CV_CLOUD_URL}
CVA_DATA_DIR=${DATA_DIR}/data
CVA_LINKING_CODE=${LINKING_CODE}
CVA_MODEL=${CV_MODEL}
CVA_SOFTWARE_VERSION=${ver}
CVA_REQUIRE_LOCAL_RECOVERY_APPROVAL=true
EOF
  else
    # Preserve the existing config (e.g. the consumed linking code); only refresh
    # the cloud URL and the deployed version on re-runs / self-updates.
    sed -i "s#^CVA_CLOUD_BASE_URL=.*#CVA_CLOUD_BASE_URL=${CV_CLOUD_URL}#" /etc/continuity-vault/appliance.env
    if grep -q '^CVA_SOFTWARE_VERSION=' /etc/continuity-vault/appliance.env; then
      sed -i "s#^CVA_SOFTWARE_VERSION=.*#CVA_SOFTWARE_VERSION=${ver}#" /etc/continuity-vault/appliance.env
    else
      echo "CVA_SOFTWARE_VERSION=${ver}" >> /etc/continuity-vault/appliance.env
    fi
  fi
  chmod 600 /etc/continuity-vault/appliance.env
  chown -R "$CV_USER":"$CV_USER" "$INSTALL_DIR" "$DATA_DIR" /etc/continuity-vault
}

install_service() {
  cp "$INSTALL_DIR/infra/systemd/cv-appliance-agent.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable cv-appliance-agent.service
  systemctl restart cv-appliance-agent.service
}

install_selfupdate() {
  # Headless self-update: a timer periodically pulls the cloud bundle and
  # re-installs when the version changed (no git required).
  cp "$INSTALL_DIR/infra/systemd/cv-appliance-selfupdate.service" /etc/systemd/system/
  cp "$INSTALL_DIR/infra/systemd/cv-appliance-selfupdate.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now cv-appliance-selfupdate.timer
}

verify_agent() {
  local i
  for i in $(seq 1 15); do
    curl -fsS "http://127.0.0.1:8090/status" 2>/dev/null | grep -q '"activated"' && return 0
    sleep 2
  done
  return 1
}

# --- run --------------------------------------------------------------------

init_installer "Arkive Appliance" "$DATA_DIR/install-state"
require_root
note "Cloud control plane: ${CV_CLOUD_URL}"

# Collect the one-time linking code up front (before hidden steps).
LINKING_CODE="${CV_LINKING_CODE:-}"
if [[ -z "$LINKING_CODE" && -t 0 ]]; then
  printf "  %sEnter the linking code from your Arkive portal%s (blank to link later): " "$BOLD" "$RESET"
  read -r LINKING_CODE || true
fi

step "Installing system packages"          install_os_deps
step "Creating service user & directories" create_user_dirs
step_always "Copying application files"    sync_code
step_if_changed "Installing appliance agent" \
  "$INSTALL_DIR/appliance/requirements.txt $INSTALL_DIR/shared $INSTALL_DIR/.venv/pyvenv.cfg" \
  install_python
step_if_changed "Building quantum-safe crypto (liboqs)" \
  "$INSTALL_DIR/shared $INSTALL_DIR/.venv/.pq-ok" \
  build_pqcrypto
step_always "Validating agent"             validate_app
step_always "Writing appliance configuration" write_config
step_always "Starting appliance agent"     install_service
step_always "Enabling headless self-update" install_selfupdate
step_always "Verifying activation with cloud" verify_agent

finish
printf "  Reporting to: %s%s%s\n" "$BOLD" "$CV_CLOUD_URL" "$RESET"
printf "  Local status: http://127.0.0.1:8090/status\n\n"
