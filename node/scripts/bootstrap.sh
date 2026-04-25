#!/bin/sh
set -eu

STATE_ROOT="${STATE_ROOT:-/etc/node-feeds}"

if [ -L "$STATE_ROOT/current" ] || [ -d "$STATE_ROOT/current" ]; then
  echo "node-feeds bootstrap: current release present"
else
  echo "node-feeds bootstrap: current release missing; skipping runtime load"
fi

echo "expected future behavior:"
echo "  1. load static nft layer from current"
echo "  2. ensure dnsmasq include points at current"
echo "  3. start background updater"
