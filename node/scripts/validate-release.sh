#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
RELEASE_DIR="${2:?release dir required}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"
[ -d "$RELEASE_DIR" ] || { echo "release dir missing: $RELEASE_DIR" >&2; exit 1; }

MANIFEST="$RELEASE_DIR/manifest.txt"
[ -f "$MANIFEST" ] || { echo "manifest missing: $MANIFEST" >&2; exit 1; }

. "$(dirname "$0")/manifest-util.sh"

VERSION="$(manifest_version "$MANIFEST")"
PROFILE="$(cat "$RELEASE_DIR/profile.txt" 2>/dev/null || printf '%s' "$FEED_PROFILE")"
BUNDLE_VERSION="$(cat "$RELEASE_DIR/version.txt" 2>/dev/null || printf '')"
BUNDLE_PROFILE="$(cat "$RELEASE_DIR/profile.txt" 2>/dev/null || printf '')"

[ -f "$RELEASE_DIR/manifest.txt.sha256" ] || { echo "manifest checksum missing" >&2; exit 1; }
(
  cd "$RELEASE_DIR"
  sha256sum -c manifest.txt.sha256 >/dev/null
  if [ -f manifest.json.sha256 ]; then
    sha256sum -c manifest.json.sha256 >/dev/null
  fi
)

[ "$BUNDLE_VERSION" = "$VERSION" ] || { echo "version mismatch: $BUNDLE_VERSION != $VERSION" >&2; exit 1; }
[ "$BUNDLE_PROFILE" = "$PROFILE" ] || { echo "profile mismatch: $BUNDLE_PROFILE != $PROFILE" >&2; exit 1; }
manifest_has_profile "$MANIFEST" "$PROFILE" || { echo "profile missing in manifest: $PROFILE" >&2; exit 1; }

FILES_TSV="$RELEASE_DIR/.files.tsv"
manifest_profile_files "$MANIFEST" "$PROFILE" > "$FILES_TSV"
if [ ! -s "$FILES_TSV" ]; then
  echo "no files for profile: $PROFILE" >&2
  exit 1
fi
while IFS="$(printf '\t')" read -r name sha size kind; do
  [ -n "$name" ] || continue
  manifest_safe_name "$name" || { echo "unsafe file name in manifest: $name" >&2; exit 1; }
  FILE="$RELEASE_DIR/$name"
  [ -f "$FILE" ] || { echo "missing file: $FILE" >&2; exit 1; }
  actual_size="$(wc -c < "$FILE" | tr -d ' ')"
  [ "$actual_size" = "$size" ] || { echo "size mismatch for $name" >&2; exit 1; }
  printf '%s  %s\n' "$sha" "$FILE" | sha256sum -c - >/dev/null
done < "$FILES_TSV"
rm -f "$FILES_TSV"

echo "release validated: $RELEASE_DIR"
