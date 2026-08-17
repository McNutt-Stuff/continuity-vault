#!/usr/bin/env bash
#
# Build a signed release tarball for a component (cloud | appliance) and print
# its SHA-384 hash. The hash + URL are then registered via the admin Updates
# console (POST /api/updates/releases), which produces the hybrid-signed
# (Ed25519 + ML-DSA) update manifest.
#
# Usage:
#   ./build-release.sh cloud 1.0.1
#   ./build-release.sh appliance 1.0.1
#
set -euo pipefail

COMPONENT="${1:?component required: cloud|appliance}"
VERSION="${2:?version required}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/dist}"
mkdir -p "$OUT_DIR"

case "$COMPONENT" in
  cloud)     SRC=("cloud" "shared" "web" "infra") ;;
  appliance) SRC=("appliance" "shared" "infra") ;;
  *) echo "unknown component: $COMPONENT"; exit 1 ;;
esac

TARBALL="$OUT_DIR/${COMPONENT}-${VERSION}.tar.gz"
echo "==> Building $TARBALL"
tar -czf "$TARBALL" -C "$REPO_ROOT" "${SRC[@]}"

HASH="sha384:$(openssl dgst -sha384 -binary "$TARBALL" | xxd -p -c256)"
echo "==> Package hash: $HASH"
echo ""
echo "Register in the admin console with:"
echo "  component:    $COMPONENT"
echo "  version:      $VERSION"
echo "  package_url:  https://vault.arkive.life/releases/${COMPONENT}-${VERSION}.tar.gz"
echo "  package_hash: $HASH"
