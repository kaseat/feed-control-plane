#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-control/hub.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${REPO_ROOT:?missing REPO_ROOT}"
: "${DATA_ROOT:?missing DATA_ROOT}"
: "${RUNTIME_ROOT:?missing RUNTIME_ROOT}"
: "${OBSERVED_ROOT:=$DATA_ROOT/observed}"
: "${CANDIDATE_ROOT:=$DATA_ROOT/candidate}"
: "${APPROVED_CRITICAL_FILE:=$DATA_ROOT/approved/private_critical.domains}"
: "${PROCESS_STATE_FILE:=$DATA_ROOT/state/observed-processed.json}"
: "${THRESHOLDS_CONFIG:=$REPO_ROOT/seeds/thresholds.example.json}"
: "${NOISE_FILTERS_CONFIG:=$REPO_ROOT/seeds/noise.example.json}"
: "${DENY_FILTERS_CONFIG:=$REPO_ROOT/seeds/deny.example.json}"

mkdir -p \
  "$(dirname "$APPROVED_CRITICAL_FILE")" \
  "$(dirname "$PROCESS_STATE_FILE")" \
  "$CANDIDATE_ROOT"

python3 "$REPO_ROOT/builder/build_candidates.py" \
  --repo-root "$REPO_ROOT" \
  --data-root "$DATA_ROOT" \
  --runtime-root "$RUNTIME_ROOT" \
  --observed-root "$OBSERVED_ROOT" \
  --candidate-root "$CANDIDATE_ROOT" \
  --approved-file "$APPROVED_CRITICAL_FILE" \
  --state-file "$PROCESS_STATE_FILE" \
  --thresholds-config "$THRESHOLDS_CONFIG" \
  --noise-config "$NOISE_FILTERS_CONFIG" \
  --deny-config "$DENY_FILTERS_CONFIG"

echo "hub observed processing completed"
