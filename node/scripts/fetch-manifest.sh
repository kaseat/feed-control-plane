#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

MANIFEST_DIR="$STATE_ROOT/staging/manifest"
mkdir -p "$STATE_ROOT/staging"
TMP_DIR="$(mktemp -d "$STATE_ROOT/staging/.manifest.XXXXXX")"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

curl -fsSL "$FEED_BASE_URL/manifest.txt" -o "$TMP_DIR/manifest.txt"
curl -fsSL "$FEED_BASE_URL/manifest.txt.sha256" -o "$TMP_DIR/manifest.txt.sha256"
if curl -fsSL "$FEED_BASE_URL/manifest.json" -o "$TMP_DIR/manifest.json"; then
  curl -fsSL "$FEED_BASE_URL/manifest.json.sha256" -o "$TMP_DIR/manifest.json.sha256" || true
fi
cd "$TMP_DIR"
sha256sum -c manifest.txt.sha256
if [ -f manifest.json.sha256 ]; then
  sha256sum -c manifest.json.sha256
fi
rm -rf "$MANIFEST_DIR"
mv "$TMP_DIR" "$MANIFEST_DIR"
trap - EXIT INT TERM
echo "manifest saved to $MANIFEST_DIR"
