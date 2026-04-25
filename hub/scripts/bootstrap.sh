#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-control/hub.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${HTTP_PORT:=18080}"
: "${HTTP_BIND_ADDR:=0.0.0.0}"
: "${RUNTIME_ROOT:?missing RUNTIME_ROOT}"
: "${OBSERVED_ROOT:=$DATA_ROOT/observed}"

CURRENT_RELEASE="$RUNTIME_ROOT/current"
if [ ! -L "$CURRENT_RELEASE" ] && [ ! -d "$CURRENT_RELEASE" ]; then
  echo "hub bootstrap: no current release available" >&2
  exit 1
fi
if [ -n "${REPO_ROOT:-}" ] && [ ! -d "$REPO_ROOT" ]; then
  echo "hub bootstrap: repo missing, serving cached runtime only" >&2
fi

/bin/sh "$(dirname "$0")/publish-runtime.sh" "$CONFIG_FILE"

export DATA_ROOT OBSERVED_ROOT HTTP_BIND_ADDR HTTP_PORT PUBLIC_ROOT HUB_INGEST_TOKEN
exec python3 "$(dirname "$0")/hub-server.py" \
  --bind "$HTTP_BIND_ADDR" \
  --port "$HTTP_PORT" \
  --public-root "$RUNTIME_ROOT/public"
