#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-control/hub.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${REPO_ROOT:?missing REPO_ROOT}"
: "${RUNTIME_ROOT:?missing RUNTIME_ROOT}"
export APPROVED_CRITICAL_FILE="${APPROVED_CRITICAL_FILE:-}"

BUILD_VERSION="$(date -u +%Y%m%d%H%M%S)"
RELEASE_ROOT="$RUNTIME_ROOT/releases/$BUILD_VERSION"

mkdir -p "$RELEASE_ROOT" "$RUNTIME_ROOT/staging" "$RUNTIME_ROOT/releases"

python3 "$REPO_ROOT/builder/build_runtime_bundle.py" \
  --repo-root "$REPO_ROOT" \
  --output-dir "$RELEASE_ROOT" \
  --version "$BUILD_VERSION"

if [ -L "$RUNTIME_ROOT/current" ]; then
  PREVIOUS_TARGET="$(readlink "$RUNTIME_ROOT/current")"
  ln -sfn "$PREVIOUS_TARGET" "$RUNTIME_ROOT/previous" || true
fi

ln -sfn "$RELEASE_ROOT" "$RUNTIME_ROOT/current"

echo "hub runtime built:"
echo "  version: $BUILD_VERSION"
echo "  release: $RELEASE_ROOT"
echo "  current -> $RELEASE_ROOT"
