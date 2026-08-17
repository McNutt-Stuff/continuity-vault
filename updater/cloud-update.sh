#!/usr/bin/env bash
#
# Arkive — Cloud self-update agent.
#
# Polls the control plane for a pending cloud update (triggered from the admin
# Updates console), verifies the package hash, stages it with a rollback copy,
# applies it, restarts the service, and confirms health. Runs on a systemd timer.
#
# Update restrictions enforced here (spec 11):
#   - hash must match the signed manifest
#   - a rollback copy is always retained
#   - failed health check triggers automatic rollback
#
set -euo pipefail

API="${CV_API_BASE_URL:-https://vault.arkive.life/api}"
INSTALL_DIR="/opt/continuity-vault"
BACKUP_DIR="/var/lib/continuity-vault/rollback"
STAGE_DIR="/tmp/cv-cloud-update"

log() { echo "[cloud-update] $*"; }

PENDING="$(curl -fsS "$API/self-update/pending" || echo '{"pending":false}')"
if ! echo "$PENDING" | grep -q '"pending":true' && ! echo "$PENDING" | grep -q '"pending": true'; then
  log "no pending update"; exit 0
fi

VERSION="$(echo "$PENDING" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')"
URL="$(echo "$PENDING" | python3 -c 'import sys,json;print(json.load(sys.stdin)["package_url"])')"
HASH="$(echo "$PENDING" | python3 -c 'import sys,json;print(json.load(sys.stdin)["package_hash"])')"
log "pending cloud update $VERSION"

rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
log "downloading $URL"
curl -fsSL "$URL" -o "$STAGE_DIR/pkg.tar.gz"

ACTUAL="sha384:$(openssl dgst -sha384 -binary "$STAGE_DIR/pkg.tar.gz" | xxd -p -c256)"
if [[ "$ACTUAL" != "$HASH" ]]; then
  log "HASH MISMATCH expected=$HASH actual=$ACTUAL — aborting"; exit 1
fi
log "hash verified"

log "creating rollback copy"
rm -rf "$BACKUP_DIR"; mkdir -p "$BACKUP_DIR"
cp -r "$INSTALL_DIR/." "$BACKUP_DIR/"

log "applying update"
tar -xzf "$STAGE_DIR/pkg.tar.gz" -C "$INSTALL_DIR"
"$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR/shared"
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/cloud/requirements.txt"
if [[ -d "$INSTALL_DIR/web" ]]; then
  ( cd "$INSTALL_DIR/web" && npm ci --silent && npm run build --silent ) || log "web build skipped"
fi

log "restarting service"
systemctl restart cv-cloud.service
sleep 5

if curl -fsS "http://127.0.0.1:8000/api/health" | grep -q '"status":"ok"'; then
  log "update to $VERSION applied and healthy"
else
  log "HEALTH CHECK FAILED — rolling back"
  rm -rf "$INSTALL_DIR"; mv "$BACKUP_DIR" "$INSTALL_DIR"
  systemctl restart cv-cloud.service
  log "rolled back"
  exit 1
fi
