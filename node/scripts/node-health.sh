#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${ROUTER_NAME:?missing ROUTER_NAME}"
: "${STATE_ROOT:?missing STATE_ROOT}"
: "${OBSERVED_SPOOL:?missing OBSERVED_SPOOL}"

source_json() {
  local path="$1"
  [ -f "$path" ] || { printf '{}'; return 0; }
  cat "$path"
}

json_bool() {
  case "$1" in
    1|true|yes|on|up|healthy) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

json_string_or_null() {
  if [ -n "$1" ]; then
    printf '%s' "$1"
  else
    printf 'null'
  fi
}

human_age_to_seconds() {
  value="$1"
  [ -n "$value" ] || { printf '0'; return 0; }
  hours="$(printf '%s\n' "$value" | sed -n 's/.*\([0-9][0-9]*\) hour.*/\1/p')"
  minutes="$(printf '%s\n' "$value" | sed -n 's/.*\([0-9][0-9]*\) minute.*/\1/p')"
  seconds="$(printf '%s\n' "$value" | sed -n 's/.*\([0-9][0-9]*\) second.*/\1/p')"
  hours="${hours:-0}"
  minutes="${minutes:-0}"
  seconds="${seconds:-0}"
  printf '%s\n' "$((hours * 3600 + minutes * 60 + seconds))"
}

bytes_from_human() {
  value="$1"
  [ -n "$value" ] || { printf '0'; return 0; }
  cleaned="$(printf '%s\n' "$value" | tr -d ',')"
  number="$(printf '%s\n' "$cleaned" | awk '{print $1}')"
  unit="$(printf '%s\n' "$cleaned" | awk '{print $2}')"
  case "$unit" in
    B) scale=1 ;;
    KiB) scale=1024 ;;
    MiB) scale=1048576 ;;
    GiB) scale=1073741824 ;;
    TiB) scale=1099511627776 ;;
    *) scale=1 ;;
  esac
  awk -v number="$number" -v scale="$scale" 'BEGIN { if (number == "") number = 0; printf "%.0f\n", number * scale }'
}

epoch_to_utc() {
  epoch="$1"
  [ -n "$epoch" ] || { printf 'null'; return 0; }
  date -u -d "@$epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf 'null'
}

route_table_default_dev() {
  table="$1"
  ip route show table "$table" 2>/dev/null | awk '
    $1 == "default" {
      for (i = 1; i <= NF; i++) {
        if ($i == "dev") {
          print $(i + 1)
          exit
        }
      }
    }
  '
}

route_table_has_default() {
  table="$1"
  ip route show table "$table" 2>/dev/null | awk '$1 == "default" { found = 1 } END { print found ? "1" : "0" }'
}

wan_json="$(source_json /tmp/node-health-wan.json)"
wan_status_json="$(ubus call network.interface.wan status 2>/dev/null || printf '{}')"
printf '%s\n' "$wan_status_json" > /tmp/node-health-wan.json

WAN_UP="$(printf '%s' "$wan_status_json" | jsonfilter -e '@.up' 2>/dev/null || printf 'false')"
WAN_ADDR="$(printf '%s' "$wan_status_json" | jsonfilter -e '@["ipv4-address"][0].address' 2>/dev/null || true)"
WAN_ROUTE_NEXT_HOP="$(printf '%s' "$wan_status_json" | jsonfilter -e '@.route[0].nexthop' 2>/dev/null || true)"
WAN_DEFAULT_DEV="$(ip route show default 2>/dev/null | awk '
  $1 == "default" {
    for (i = 1; i <= NF; i++) {
      if ($i == "dev") {
        print $(i + 1)
        exit
      }
    }
  }
')"
WAN_PROBE="down"
if curl -fsS --connect-timeout 3 --max-time 6 -k https://1.1.1.1/cdn-cgi/trace >/dev/null 2>&1; then
  WAN_PROBE="ok"
fi

DNSMASQ_RUNNING="down"
if pidof dnsmasq >/dev/null 2>&1; then
  DNSMASQ_RUNNING="running"
fi
QUERY_LOGGING="false"
if [ "$(uci -q get dhcp.@dnsmasq[0].logqueries 2>/dev/null || printf 0)" = "1" ]; then
  QUERY_LOGGING="true"
fi

CURRENT_RELEASE=""
if [ -L /etc/node-feeds/current ]; then
  CURRENT_RELEASE="$(readlink -f /etc/node-feeds/current 2>/dev/null || true)"
fi
CURRENT_PROFILE=""
CURRENT_RELEASE_DIR=""
if [ -n "$CURRENT_RELEASE" ]; then
  CURRENT_PROFILE="$(basename "$CURRENT_RELEASE")"
  CURRENT_RELEASE_DIR="$(dirname "$CURRENT_RELEASE")"
fi
CURRENT_VERSION=""
if [ -n "$CURRENT_RELEASE_DIR" ]; then
  CURRENT_VERSION="$(basename "$CURRENT_RELEASE_DIR")"
fi
CURRENT_RELEASE_MTIME=""
if [ -n "$CURRENT_RELEASE" ] && [ -e "$CURRENT_RELEASE" ]; then
  CURRENT_RELEASE_MTIME="$(stat -c %Y "$CURRENT_RELEASE" 2>/dev/null || true)"
fi
OBSERVED_PENDING_COUNT="$(find "$OBSERVED_SPOOL" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | wc -l | awk '{print $1}')"
OBSERVED_SENT_COUNT="$(find "$OBSERVED_SPOOL/sent" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | wc -l | awk '{print $1}')"
DHCP_LEASE_FILE="${DHCP_LEASE_FILE:-/tmp/dhcp.leases}"
CLIENTS_COUNT=0
if [ -f "$DHCP_LEASE_FILE" ]; then
  CLIENTS_COUNT="$(awk 'NF >= 5 { count++ } END { print count + 0 }' "$DHCP_LEASE_FILE" 2>/dev/null || printf '0')"
fi

TUNNEL_STATUS() {
  iface="$1"
  route_label="$2"
  active_egress="$3"
  awg_output="$(awg show "$iface" 2>/dev/null || true)"
  handshake="$(printf '%s\n' "$awg_output" | awk -F': ' '/latest handshake:/ {print $2; exit}')"
  transfer="$(printf '%s\n' "$awg_output" | awk -F': ' '/transfer:/ {print $2; exit}')"
  rx_human="$(printf '%s\n' "$transfer" | awk '{print $1, $2}')"
  tx_human="$(printf '%s\n' "$transfer" | awk '{print $4, $5}')"
  handshake_seconds="$(human_age_to_seconds "$handshake")"
  rx_bytes="$(bytes_from_human "$rx_human")"
  tx_bytes="$(bytes_from_human "$tx_human")"
  probe_status="down"
  if curl -fsS --connect-timeout 3 --max-time 6 --interface "$iface" -k https://1.1.1.1/cdn-cgi/trace >/dev/null 2>&1; then
    probe_status="ok"
  fi

  status="down"
  if [ -n "$handshake_seconds" ] && [ "$handshake_seconds" -lt 300 ]; then
    status="healthy"
  elif [ -n "$handshake_seconds" ] && [ "$handshake_seconds" -lt 900 ]; then
    status="degraded"
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$status" "$handshake_seconds" "$rx_bytes" "$tx_bytes" "$probe_status" "$route_label" "$active_egress" "$iface"
}

AWGDE_LINE="$(TUNNEL_STATUS awgde de de)"
AWGPL_LINE="$(TUNNEL_STATUS awgpl pl pl)"
AWGRU_LINE="$(TUNNEL_STATUS awgru ru ru)"

foreign_default_dev="$(route_table_default_dev foreign_active)"
corp_default_dev="$(route_table_default_dev corp_active)"
admin_default_dev="$(route_table_default_dev admin_active)"

route_table_foreign_has_default="$(route_table_has_default foreign_active)"
route_table_corp_has_default="$(route_table_has_default corp_active)"
route_table_admin_has_default="$(route_table_has_default admin_active)"

overall_status="healthy"
if [ "$WAN_UP" != "true" ] || [ "$DNSMASQ_RUNNING" != "running" ] || [ "$QUERY_LOGGING" != "true" ]; then
  overall_status="degraded"
fi
if [ "$(printf '%s\n%s\n%s' "$AWGDE_LINE" "$AWGPL_LINE" "$AWGRU_LINE" | awk -F'\t' '
  $1 == "healthy" { healthy = 1 }
  END { print healthy ? 1 : 0 }
')" != "1" ]; then
  overall_status="degraded"
fi

set +u
JSON_PREFIX=""
JSON_UNSET=""
. /usr/share/libubox/jshn.sh

json_init
json_add_int schema_version 1
json_add_string node "$ROUTER_NAME"
json_add_string generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
json_add_string status "$overall_status"

json_add_object wan
json_add_string status "$( [ "$WAN_UP" = "true" ] && printf healthy || printf down )"
json_add_string default_route "$( [ -n "$WAN_DEFAULT_DEV" ] && printf ok || printf missing )"
json_add_string probe_https "$WAN_PROBE"
[ -n "$WAN_ADDR" ] && json_add_string external_ip "$WAN_ADDR" || true
[ -n "$WAN_ROUTE_NEXT_HOP" ] && json_add_string next_hop "$WAN_ROUTE_NEXT_HOP" || true
json_close_object

json_add_object dns
json_add_string status "$( [ "$DNSMASQ_RUNNING" = "running" ] && [ "$QUERY_LOGGING" = "true" ] && printf healthy || printf degraded )"
json_add_string dnsmasq "$DNSMASQ_RUNNING"
json_add_boolean query_logging "$(json_bool "$QUERY_LOGGING")"
json_close_object

json_add_object feed
json_add_string status "$( [ -n "$CURRENT_VERSION" ] && printf healthy || printf unknown )"
json_add_string current_version "$CURRENT_VERSION"
[ -n "$CURRENT_RELEASE" ] && json_add_string current_release "$CURRENT_RELEASE" || true
[ -n "$CURRENT_PROFILE" ] && json_add_string current_profile "$CURRENT_PROFILE" || true
[ -n "$CURRENT_RELEASE_MTIME" ] && json_add_string current_release_mtime "$(epoch_to_utc "$CURRENT_RELEASE_MTIME")" || true
json_add_int observed_pending "$OBSERVED_PENDING_COUNT"
json_add_int observed_sent "$OBSERVED_SENT_COUNT"
json_add_string profile "${CURRENT_PROFILE:-${FEED_PROFILE:-critical}}"
json_close_object

json_add_object tunnels
set -- $AWGDE_LINE
json_add_object awgde
json_add_string status "$1"
json_add_int handshake_age_seconds "${2:-0}"
json_add_int rx_bytes "${3:-0}"
json_add_int tx_bytes "${4:-0}"
json_add_string probe_https "$5"
json_add_string egress "$6"
json_add_string active_egress "$7"
json_add_string interface "$8"
json_close_object

set -- $AWGPL_LINE
json_add_object awgpl
json_add_string status "$1"
json_add_int handshake_age_seconds "${2:-0}"
json_add_int rx_bytes "${3:-0}"
json_add_int tx_bytes "${4:-0}"
json_add_string probe_https "$5"
json_add_string egress "$6"
json_add_string active_egress "$7"
json_add_string interface "$8"
json_close_object

set -- $AWGRU_LINE
json_add_object awgru
json_add_string status "$1"
json_add_int handshake_age_seconds "${2:-0}"
json_add_int rx_bytes "${3:-0}"
json_add_int tx_bytes "${4:-0}"
json_add_string probe_https "$5"
json_add_string egress "$6"
json_add_string active_egress "$7"
json_add_string interface "$8"
json_close_object
json_close_object

json_add_object route_tables
json_add_object foreign_active
json_add_string status "$( [ "$route_table_foreign_has_default" = "1" ] && printf healthy || printf down )"
json_add_string active_egress "$(case "$foreign_default_dev" in awgde) printf de ;; awgpl) printf pl ;; awgru) printf ru ;; *) printf unknown ;; esac)"
[ -n "$foreign_default_dev" ] && json_add_string default_dev "$foreign_default_dev" || true
json_close_object

json_add_object corp_active
json_add_string status "$( [ "$route_table_corp_has_default" = "1" ] && printf healthy || printf unknown )"
[ -n "$corp_default_dev" ] && json_add_string default_dev "$corp_default_dev" || true
json_close_object

json_add_object admin_active
json_add_string status "$( [ "$route_table_admin_has_default" = "1" ] && printf healthy || printf unknown )"
[ -n "$admin_default_dev" ] && json_add_string default_dev "$admin_default_dev" || true
json_close_object
json_close_object

json_add_object metrics
json_add_int wan_up "$( [ "$WAN_UP" = "true" ] && printf 1 || printf 0 )"
json_add_int dnsmasq_up "$( [ "$DNSMASQ_RUNNING" = "running" ] && printf 1 || printf 0 )"
json_add_int query_logging_enabled "$( [ "$QUERY_LOGGING" = "true" ] && printf 1 || printf 0 )"
json_add_int clients_count "$CLIENTS_COUNT"
json_add_int foreign_active_default_present "$route_table_foreign_has_default"
json_add_int corp_active_default_present "$route_table_corp_has_default"
json_add_int admin_active_default_present "$route_table_admin_has_default"
json_close_object

json_dump > /var/run/node-health.json

json_init
json_add_int schema_version 1
json_add_string node "$ROUTER_NAME"
json_add_string generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
json_add_object classes
json_add_object foreign
json_add_string active_egress "$(case "$foreign_default_dev" in awgde) printf de ;; awgpl) printf pl ;; awgru) printf ru ;; *) printf unknown ;; esac)"
json_add_string active_dev "$foreign_default_dev"
json_add_string mode "auto"
json_add_string cooldown_until null
json_add_string last_decision "keep"
json_add_string last_reason "health refresh"
json_close_object
json_close_object
json_add_object tables
json_add_string foreign_active "$foreign_default_dev"
json_add_string corp_active "$corp_default_dev"
json_add_string admin_active "$admin_default_dev"
json_close_object
json_dump > /var/run/node-route-policy.state.json

mkdir -p /var/lib/node-feeds
json_init
json_add_int schema_version 1
json_add_string node "$ROUTER_NAME"
json_add_string generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
json_add_string current_version "$CURRENT_VERSION"
[ -n "$CURRENT_RELEASE" ] && json_add_string current_release "$CURRENT_RELEASE" || true
json_add_string profile "${CURRENT_PROFILE:-${FEED_PROFILE:-critical}}"
json_add_int observed_pending "$OBSERVED_PENDING_COUNT"
json_add_int observed_sent "$OBSERVED_SENT_COUNT"
json_add_string health_status "$overall_status"
json_add_string foreign_active "$(case "$foreign_default_dev" in awgde) printf de ;; awgpl) printf pl ;; awgru) printf ru ;; *) printf unknown ;; esac)"
json_dump > /var/lib/node-feeds/status.json

set -u

echo "node health refreshed: $ROUTER_NAME"
