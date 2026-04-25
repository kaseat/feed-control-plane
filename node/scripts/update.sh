#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

/bin/sh "$(dirname "$0")/fetch-manifest.sh" "$CONFIG_FILE"
/bin/sh "$(dirname "$0")/download-profile.sh" "$CONFIG_FILE"

VERSION="$(awk -F '\t' '$1=="version"{print $2; exit}' "$STATE_ROOT/staging/manifest/manifest.txt")"
RELEASE_DIR="$STATE_ROOT/staging/$VERSION/$FEED_PROFILE"
/bin/sh "$(dirname "$0")/validate-release.sh" "$CONFIG_FILE" "$RELEASE_DIR"
/bin/sh "$(dirname "$0")/apply-release.sh" "$CONFIG_FILE" "$RELEASE_DIR"
