#!/usr/bin/env bash
#
# Arkive — Appliance update applier.
#
# Appliance updates are triggered from the cloud as a signed STAGE_UPDATE
# command. The agent verifies the hybrid signature and writes the staged
# descriptor to <data>/staged_update.json. This script applies a staged update
# with a retained rollback partition, then relies on secure-boot + remote
# attestation on next heartbeat to confirm the new measurements (spec 11).
#
# Invoked by the agent (or manually) once an update is staged and approved.
#
set -euo pipefail

DATA_DIR="${CVA_DATA_DIR:-/var/lib/continuity-vault-appliance/data}"
INSTALL_DIR="/opt/continuity-vault"
BACKUP_DIR="/var/lib/continuity-vault-appliance/rollback"
STAGE_FILE="$DATA_DIR/staged_update.json"
STAGE_DIR="/tmp/cv-appliance-update"

log() { echo "[appliance-update] $*"; }

if [[ ! -f "$STAGE_FILE" ]]; then log "no staged update"; exit 0; fi

VERSION="$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["version"])' "$STAGE_FILE")"
URL="$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["packageUrl"])' "$STAGE_FILE")"
HASH="$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["packageHash"])' "$STAGE_FILE")"
log "applying staged appliance update $VERSION"

rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
curl -fsSL "$URL" -o "$STAGE_DIR/pkg.tar.gz"

ACTUAL="sha384:$(openssl dgst -sha384 -binary "$STAGE_DIR/pkg.tar.gz" | xxd -p -c256)"
if [[ "$ACTUAL" != "$HASH" ]]; then log "HASH MISMATCH — aborting"; exit 1; fi
log "hash verified"

# Retain rollback partition.
rm -rf "$BACKUP_DIR"; mkdir -p "$BACKUP_DIR"
cp -r "$INSTALL_DIR/." "$BACKUP_DIR/"

tar -xzf "$STAGE_DIR/pkg.tar.gz" -C "$INSTALL_DIR"
"$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR/shared"
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/appliance/requirements.txt"

# Persist the new version for attestation reporting.
sed -i "s/^CVA_SOFTWARE_VERSION=.*/CVA_SOFTWARE_VERSION=${VERSION}/" /etc/continuity-vault/appliance.env

systemctl restart cv-appliance-agent.service
sleep 5

if curl -fsS "http://127.0.0.1:8090/status" | grep -q '"activated":true'; then
  log "appliance update $VERSION applied; attestation confirms on next heartbeat"
  rm -f "$STAGE_FILE"
else
  log "HEALTH CHECK FAILED — rolling back to previous partition"
  rm -rf "$INSTALL_DIR"; mv "$BACKUP_DIR" "$INSTALL_DIR"
  systemctl restart cv-appliance-agent.service
  exit 1
fi
