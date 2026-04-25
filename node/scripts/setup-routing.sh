#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"
RELEASE_DIR="${2:-}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

if [ -z "$RELEASE_DIR" ]; then
  RELEASE_DIR="$STATE_ROOT/current"
fi

[ -d "$RELEASE_DIR" ] || { echo "release dir missing: $RELEASE_DIR" >&2; exit 1; }

mkdir -p /etc/dnsmasq.d
uci -q set dhcp.@dnsmasq[0].confdir='/etc/dnsmasq.d'
uci -q set dhcp.@dnsmasq[0].logqueries='1'
uci -q commit dhcp

cp "$RELEASE_DIR/dnsmasq-fd4.conf" /etc/dnsmasq.d/node-feeds-fd4.conf

SCRIPT_DIR="$(dirname "$0")"
"$SCRIPT_DIR/load-nft-static.sh" "$CONFIG_FILE" "$RELEASE_DIR"
"$SCRIPT_DIR/refresh-foreign-route.sh" "$CONFIG_FILE"

ensure_forwarding() {
  section_name="$1"
  dest_zone="$2"

  if uci -q get "firewall.$section_name" >/dev/null 2>&1; then
    return 0
  fi

  uci -q set "firewall.$section_name=forwarding"
  uci -q set "firewall.$section_name.src=lan"
  uci -q set "firewall.$section_name.dest=$dest_zone"
}

ensure_forwarding node_feeds_lan_awgde awgde
ensure_forwarding node_feeds_lan_awgpl awgpl
ensure_forwarding node_feeds_lan_awgru awgru
uci -q commit firewall
/etc/init.d/firewall reload >/dev/null 2>&1 || /etc/init.d/firewall restart >/dev/null 2>&1 || true

/etc/init.d/dnsmasq restart >/dev/null 2>&1 || /etc/init.d/dnsmasq start >/dev/null 2>&1 || true

echo "routing setup complete for $RELEASE_DIR"
