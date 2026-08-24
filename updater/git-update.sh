#!/usr/bin/env bash
#
# Arkive — Git-based updater.
#
# Pulls the latest code from a GitHub repository into a source checkout, then
# redeploys via the idempotent, resumable installer. If the installer fails
# (e.g. the health check), it rolls back to the previous commit and redeploys.
#
# Usage (as root):
#   CV_REPO_URL=https://github.com/you/arkive.git CV_DOMAIN=vault.arkive.life \
#     ./git-update.sh cloud
#   CV_REPO_URL=https://github.com/you/arkive.git \
#     CV_CLOUD_URL=https://vault.arkive.life/api ./git-update.sh appliance
#
# If the source checkout already exists, CV_REPO_URL is optional (its origin is
# used). Config can also live in /etc/arkive-update.env. CV_FORCE=1 redeploys
# even when already up to date. For private repos, use an https token URL or an
# SSH deploy key on the host.
#
set -Eeuo pipefail

COMPONENT="${1:-cloud}"
CV_SRC_DIR="${CV_SRC_DIR:-/opt/arkive-src}"
CV_REPO_BRANCH="${CV_REPO_BRANCH:-}"   # empty => auto-detect the default branch
DEFAULT_REPO="https://github.com/McNutt-Stuff/continuity-vault.git"

# Optional config file.
if [[ -f /etc/arkive-update.env ]]; then
  # shellcheck disable=SC1091
  set -a; source /etc/arkive-update.env; set +a
fi

if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi
command -v git >/dev/null || { echo "git is required (apt-get install -y git)."; exit 1; }

# Resolve the repo URL: explicit env wins, else reuse an existing checkout's
# origin, else the project default.
if [[ -z "${CV_REPO_URL:-}" && -d "$CV_SRC_DIR/.git" ]]; then
  CV_REPO_URL="$(git -C "$CV_SRC_DIR" remote get-url origin)"
fi
CV_REPO_URL="${CV_REPO_URL:-$DEFAULT_REPO}"

echo "==> Arkive updater [$COMPONENT] from ${CV_REPO_URL}"

git config --global --add safe.directory "$CV_SRC_DIR" 2>/dev/null || true

freshly_cloned=0
if [[ ! -d "$CV_SRC_DIR/.git" ]]; then
  echo "==> Cloning into $CV_SRC_DIR"
  clone_args=()
  [[ -n "$CV_REPO_BRANCH" ]] && clone_args+=(--branch "$CV_REPO_BRANCH")
  git clone "${clone_args[@]}" "$CV_REPO_URL" "$CV_SRC_DIR"
  freshly_cloned=1
fi

cd "$CV_SRC_DIR"
git remote set-url origin "$CV_REPO_URL"

# Auto-detect the remote's default branch when one wasn't specified.
if [[ -z "$CV_REPO_BRANCH" ]]; then
  CV_REPO_BRANCH="$(git remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p')"
  CV_REPO_BRANCH="${CV_REPO_BRANCH:-main}"
fi
echo "==> Branch: ${CV_REPO_BRANCH}"

git fetch --quiet origin "$CV_REPO_BRANCH"
PREV="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "origin/${CV_REPO_BRANCH}")"

# A fresh clone has never been deployed here, so always deploy it.
if [[ "$PREV" == "$TARGET" && "${CV_FORCE:-0}" != "1" && "$freshly_cloned" != "1" ]]; then
  echo "==> Already up to date (${PREV:0:12})."
  exit 0
fi

echo "==> Updating ${PREV:0:12} -> ${TARGET:0:12}"
git checkout --quiet "$CV_REPO_BRANCH"
git reset --hard --quiet "origin/${CV_REPO_BRANCH}"

ensure_executable() {
  # git only preserves the exec bit if committed with it — make sure the
  # updater/installer scripts are runnable regardless.
  chmod +x "$CV_SRC_DIR"/updater/*.sh "$CV_SRC_DIR"/installers/*.sh 2>/dev/null || true
}
ensure_executable

# The node-management console runs systemctl + reads the journal as the service
# account (cvault). Apply the scoped sudoers + journal-group membership here too
# so an update ALWAYS fixes permissions — even a rollback that re-runs an older
# installer without this step. Idempotent and safe on nodes without the user.
ensure_control_perms() {
  local user="${CV_USER:-cvault}" f=/etc/sudoers.d/cv-cloud
  id -u "$user" >/dev/null 2>&1 || return 0
  { : > "$f"; } 2>/dev/null || return 0
  local unit act
  for unit in cv-cloud postgresql caddy cv-node-heartbeat.timer cv-node-update.timer cv-cloud-update.timer; do
    for act in start stop restart enable disable; do
      echo "${user} ALL=(root) NOPASSWD: /usr/bin/systemctl ${act} ${unit}" >> "$f"
    done
  done
  chmod 440 "$f" 2>/dev/null || true
  # Journal-group membership only takes effect on a fresh process start, so
  # restart the app when we just added it.
  if ! id -nG "$user" 2>/dev/null | grep -qw systemd-journal; then
    usermod -aG systemd-journal "$user" 2>/dev/null || true
    systemctl restart cv-cloud 2>/dev/null || true
  fi
  # Code + data must stay owned by the service account after a root-run update.
  chown -R "$user":"$user" /opt/continuity-vault /var/lib/continuity-vault 2>/dev/null || true
}

run_installer() {
  if [[ "$COMPONENT" == "cloud" ]]; then
    # Preserve this node's role across updates so only its components redeploy.
    # Priority: explicit env > persisted marker > env file. If none is known
    # the installer defaults to control-plane (it never prompts).
    local role="${CV_NODE_ROLE:-}"
    if [[ -z "$role" && -f /etc/arkive/role ]]; then role="$(cat /etc/arkive/role)"; fi
    if [[ -z "$role" && -f /etc/continuity-vault.env ]]; then
      role="$(sed -n 's/^CV_NODE_ROLE=//p' /etc/continuity-vault.env | head -1)"
    fi
    [[ -n "$role" ]] && echo "==> Node role: ${role}"
    # Export so the value is inherited by the installer without relying on a
    # parse-time assignment prefix (which can't come from an expansion).
    export REPO_SRC="$CV_SRC_DIR"
    export CV_DOMAIN="${CV_DOMAIN:-vault.arkive.life}"
    [[ -n "$role" ]] && export CV_NODE_ROLE="$role"
    bash "$CV_SRC_DIR/installers/cloud-install.sh"
  else
    REPO_SRC="$CV_SRC_DIR" CV_CLOUD_URL="${CV_CLOUD_URL:-https://vault.arkive.life/api}" \
      bash "$CV_SRC_DIR/installers/appliance-install.sh"
  fi
}

if run_installer; then
  echo "==> Update to ${TARGET:0:12} complete."
  [[ "$COMPONENT" == "cloud" ]] && ensure_control_perms
else
  echo "!! Update failed — rolling back to ${PREV:0:12}"
  git reset --hard --quiet "$PREV"
  ensure_executable
  if run_installer; then
    echo "==> Rolled back to ${PREV:0:12}."
    [[ "$COMPONENT" == "cloud" ]] && ensure_control_perms
    exit 1
  fi
  echo "!! Rollback redeploy also failed; manual intervention required."
  exit 2
fi
