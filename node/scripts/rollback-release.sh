#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

PREVIOUS_TARGET="${2:-}"
if [ -z "$PREVIOUS_TARGET" ]; then
  [ -L "$STATE_ROOT/previous" ] || { echo "previous release missing" >&2; exit 1; }
  PREVIOUS_TARGET="$(readlink "$STATE_ROOT/previous")"
fi

[ -n "$PREVIOUS_TARGET" ] || { echo "previous release missing" >&2; exit 1; }
case "$PREVIOUS_TARGET" in
  "$STATE_ROOT/releases/"*) ;;
  *) echo "previous target outside releases root: $PREVIOUS_TARGET" >&2; exit 1 ;;
esac
[ -e "$PREVIOUS_TARGET" ] || { echo "previous release dir missing: $PREVIOUS_TARGET" >&2; exit 1; }

ln -sfn "$PREVIOUS_TARGET" "$STATE_ROOT/current"

echo "rollback applied:"
echo "  current -> $PREVIOUS_TARGET"
