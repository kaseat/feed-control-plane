#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

MANIFEST="$STATE_ROOT/staging/manifest/manifest.txt"
[ -f "$MANIFEST" ] || { echo "manifest missing: $MANIFEST" >&2; exit 1; }

VERSION="$(awk -F '\t' '$1=="version"{print $2; exit}' "$MANIFEST")"
TARGET_DIR="$STATE_ROOT/staging/$VERSION/$FEED_PROFILE"
mkdir -p "$STATE_ROOT/staging"
TMP_DIR="$(mktemp -d "$STATE_ROOT/staging/.${VERSION}.${FEED_PROFILE}.XXXXXX")"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

cp "$MANIFEST" "$TMP_DIR/manifest.txt"
[ -f "$STATE_ROOT/staging/manifest/manifest.txt.sha256" ] && cp "$STATE_ROOT/staging/manifest/manifest.txt.sha256" "$TMP_DIR/manifest.txt.sha256" || true
[ -f "$STATE_ROOT/staging/manifest/manifest.json" ] && cp "$STATE_ROOT/staging/manifest/manifest.json" "$TMP_DIR/manifest.json" || true
[ -f "$STATE_ROOT/staging/manifest/manifest.json.sha256" ] && cp "$STATE_ROOT/staging/manifest/manifest.json.sha256" "$TMP_DIR/manifest.json.sha256" || true

. "$(dirname "$0")/manifest-util.sh"

manifest_has_profile "$MANIFEST" "$FEED_PROFILE" || { echo "profile missing in manifest: $FEED_PROFILE" >&2; exit 1; }
manifest_profile_files "$MANIFEST" "$FEED_PROFILE" > "$TMP_DIR/files.tsv"
if [ ! -s "$TMP_DIR/files.tsv" ]; then
  echo "no files for profile: $FEED_PROFILE" >&2
  exit 1
fi
while IFS="$(printf '\t')" read -r name sha size kind; do
  [ -n "$name" ] || continue
  manifest_safe_name "$name" || { echo "unsafe file name in manifest: $name" >&2; exit 1; }
  curl -fsSL "$FEED_BASE_URL/releases/$VERSION/$name" -o "$TMP_DIR/$name"
  printf '%s  %s\n' "$sha" "$TMP_DIR/$name" | sha256sum -c -
done < "$TMP_DIR/files.tsv"

echo "$VERSION" > "$TMP_DIR/version.txt"
echo "$FEED_PROFILE" > "$TMP_DIR/profile.txt"
mkdir -p "$(dirname "$TARGET_DIR")"
rm -rf "$TARGET_DIR"
mv "$TMP_DIR" "$TARGET_DIR"
trap - EXIT INT TERM
echo "profile downloaded to $TARGET_DIR"
