#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

RT_TABLES="/etc/iproute2/rt_tables"
FOREIGN_TABLE="foreign_active"

ensure_table() {
  pref="$1"
  name="$2"
  grep -Eq "^[[:space:]]*$pref[[:space:]]+$name([[:space:]]|$)" "$RT_TABLES" || echo "$pref $name" >> "$RT_TABLES"
}

add_or_keep_rule() {
  ip rule show | grep -q "lookup $FOREIGN_TABLE" || ip rule add pref 10010 fwmark 0x0100 lookup "$FOREIGN_TABLE"
}

add_route_if_up() {
  dev="$1"
  metric="$2"
  ip link show dev "$dev" >/dev/null 2>&1 || return 0
  ip route replace default dev "$dev" table "$FOREIGN_TABLE" metric "$metric"
}

ensure_table 100 foreign_active
ensure_table 101 corp_active
ensure_table 102 admin_active
add_or_keep_rule
ip route flush table "$FOREIGN_TABLE" 2>/dev/null || true
add_route_if_up awgde 10
add_route_if_up awgpl 20
add_route_if_up awgru 30

echo "foreign route table refreshed"
