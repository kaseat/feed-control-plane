#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
RELEASE_DIR="${2:?release dir required}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
[ -d "$RELEASE_DIR" ] || { echo "release dir missing: $RELEASE_DIR" >&2; exit 1; }
. "$CONFIG_FILE"

mkdir -p "$STATE_ROOT/releases"

VERSION="$(cat "$RELEASE_DIR/version.txt" 2>/dev/null || basename "$RELEASE_DIR")"
PROFILE="$(cat "$RELEASE_DIR/profile.txt" 2>/dev/null || printf '%s' "$FEED_PROFILE")"
TARGET="$STATE_ROOT/releases/$VERSION/$PROFILE"

mkdir -p "$(dirname "$TARGET")"

if [ "$RELEASE_DIR" != "$TARGET" ]; then
  rm -rf "$TARGET"
  mv "$RELEASE_DIR" "$TARGET"
fi

if [ -L "$STATE_ROOT/current" ]; then
  PREVIOUS_TARGET="$(readlink "$STATE_ROOT/current")"
  ln -sfn "$PREVIOUS_TARGET" "$STATE_ROOT/previous" || true
fi

ln -sfn "$TARGET" "$STATE_ROOT/current"

"$(dirname "$0")/scripts/setup-routing.sh" "$CONFIG_FILE" "$TARGET"

echo "release applied:"
echo "  current -> $TARGET"
