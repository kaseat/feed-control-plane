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

NFT_SRC="$RELEASE_DIR/nft-fs4.txt"
NFT_DST="/etc/nftables.d/90-node-feeds.nft"

[ -f "$NFT_SRC" ] || { echo "no nft static file present, skipping" >&2; exit 0; }

join_cidrs() {
  awk 'NF {printf "%s%s", sep, $0; sep=", "} END {print ""}' "$NFT_SRC"
}

TMP_FILE="$(mktemp /etc/nftables.d/.node-feeds.XXXXXX)"
trap 'rm -f "$TMP_FILE"' EXIT INT TERM

{
  echo 'set fd4 {'
  echo '    type ipv4_addr'
  echo '    flags interval'
  echo '}'
  echo
  echo 'set fs4 {'
  echo '    type ipv4_addr'
  echo '    flags interval'
  echo "    elements = { $(join_cidrs) }"
  echo '}'
  echo
  echo 'chain node_feeds_prerouting {'
  echo '    type filter hook prerouting priority mangle - 1; policy accept;'
  echo '    iifname "br-lan" ip daddr @fd4 meta mark set 0x0100'
  echo '    iifname "br-lan" ip daddr @fs4 meta mark set 0x0100'
  echo '}'
  echo
  echo 'chain node_feeds_output {'
  echo '    type route hook output priority mangle - 1; policy accept;'
  echo '    ip daddr @fd4 meta mark set 0x0100'
  echo '    ip daddr @fs4 meta mark set 0x0100'
  echo '}'
} > "$TMP_FILE"

mv "$TMP_FILE" "$NFT_DST"
fw4 -q reload

echo "nft static loaded: $NFT_DST"
