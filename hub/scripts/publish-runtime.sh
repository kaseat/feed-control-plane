#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-control/hub.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${RUNTIME_ROOT:?missing RUNTIME_ROOT}"

CURRENT_RELEASE=""
if [ -L "$RUNTIME_ROOT/current" ]; then
  CURRENT_RELEASE="$(readlink "$RUNTIME_ROOT/current")"
fi
[ -n "$CURRENT_RELEASE" ] || { echo "current release missing" >&2; exit 1; }
[ -d "$CURRENT_RELEASE" ] || { echo "current release dir missing: $CURRENT_RELEASE" >&2; exit 1; }

PUBLIC_ROOT="${PUBLIC_ROOT:-$RUNTIME_ROOT/public/node-feeds}"
mkdir -p "$PUBLIC_ROOT/releases"

ln -sfn "../../current/manifest.txt" "$PUBLIC_ROOT/manifest.txt"
ln -sfn "../../current/manifest.txt.sha256" "$PUBLIC_ROOT/manifest.txt.sha256"
ln -sfn "../../current/manifest.json" "$PUBLIC_ROOT/manifest.json"
ln -sfn "../../current/manifest.json.sha256" "$PUBLIC_ROOT/manifest.json.sha256"

VERSION="$(basename "$CURRENT_RELEASE")"
PUBLIC_RELEASE="$PUBLIC_ROOT/releases/$VERSION"
rm -rf "$PUBLIC_RELEASE"
mkdir -p "$PUBLIC_RELEASE"
for file in "$CURRENT_RELEASE"/*; do
  [ -f "$file" ] || continue
  cp "$file" "$PUBLIC_RELEASE/$(basename "$file")"
done

echo "hub runtime published:"
echo "  public root: $PUBLIC_ROOT"
echo "  current release: $CURRENT_RELEASE"
