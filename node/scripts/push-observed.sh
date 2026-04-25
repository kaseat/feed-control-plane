#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

SENT_DIR="$OBSERVED_SPOOL/sent"
mkdir -p "$SENT_DIR"

found=0
for file in "$OBSERVED_SPOOL"/*.jsonl; do
  [ -e "$file" ] || exit 0
  found=1
  curl -fsSL \
    --connect-timeout 5 \
    --max-time 15 \
    -H "Authorization: Bearer $HUB_PUSH_TOKEN" \
    -H "Content-Type: application/x-ndjson" \
    --data-binary "@$file" \
    "$HUB_PUSH_URL"
  if [ -n "${TELEMETRY_OBSERVED_URL:-}" ] && [ -n "${TELEMETRY_PUSH_TOKEN:-}" ]; then
    curl -fsSL \
      --connect-timeout 5 \
      --max-time 15 \
      -H "Authorization: Bearer $TELEMETRY_PUSH_TOKEN" \
      -H "Content-Type: application/x-ndjson" \
      --data-binary "@$file" \
      "$TELEMETRY_OBSERVED_URL"
  fi
  mv "$file" "$SENT_DIR/$(basename "$file")"
done

if [ "$found" -eq 0 ]; then
  echo "no observed batches to push"
  exit 0
fi

echo "observed push completed"
