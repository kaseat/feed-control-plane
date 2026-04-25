#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

HEALTH_FILE="${HEALTH_FILE:-/var/run/node-health.json}"
ROUTE_FILE="${ROUTE_FILE:-/var/run/node-route-policy.state.json}"
FEED_FILE="${FEED_FILE:-/var/lib/node-feeds/status.json}"

: "${HUB_HEALTH_URL:?missing HUB_HEALTH_URL}"
: "${HUB_PUSH_TOKEN:?missing HUB_PUSH_TOKEN}"

[ -f "$HEALTH_FILE" ] || { echo "health file missing: $HEALTH_FILE" >&2; exit 1; }

health_json="$(cat "$HEALTH_FILE")"
route_json="{}"
feed_json="{}"
node_class="${NODE_CLASS:-router}"

[ -f "$ROUTE_FILE" ] && route_json="$(cat "$ROUTE_FILE")"
[ -f "$FEED_FILE" ] && feed_json="$(cat "$FEED_FILE")"

payload="$(printf '{"node":"%s","node_class":"%s","collected_at":"%s","health":%s,"route_policy":%s,"feed_status":%s}\n' \
  "$ROUTER_NAME" "$node_class" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$health_json" "$route_json" "$feed_json")"

curl -fsSL \
  --connect-timeout 5 \
  --max-time 15 \
  -H "Authorization: Bearer $HUB_PUSH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "$payload" \
  "$HUB_HEALTH_URL"

echo "health push completed"
