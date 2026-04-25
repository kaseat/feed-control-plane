#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-feeds/node.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${ROUTER_NAME:?missing ROUTER_NAME}"
: "${STATE_ROOT:?missing STATE_ROOT}"
: "${OBSERVED_SPOOL:?missing OBSERVED_SPOOL}"

WINDOW="${OBSERVED_WINDOW:-$(date -u +%Y%m%d%H0000)}"
LOG_FILE="${OBSERVED_LOG_FILE:-}"
OUTPUT_DIR="$OBSERVED_SPOOL"

mkdir -p "$OUTPUT_DIR" "$OUTPUT_DIR/sent"

TMP_DIR="$(mktemp -d "$OUTPUT_DIR/.collect.${WINDOW}.XXXXXX")"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

source_stream() {
  if [ -n "$LOG_FILE" ]; then
    [ -f "$LOG_FILE" ] || { echo "observed log file missing: $LOG_FILE" >&2; return 1; }
    cat "$LOG_FILE"
    return 0
  fi

  if command -v logread >/dev/null 2>&1; then
    logread -e dnsmasq
    return 0
  fi

  echo "no dnsmasq log source available" >&2
  return 1
}

extract_pairs() {
  source_stream | awk '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      return s
    }

    function clean_domain(s) {
      s = tolower(trim(s))
      sub(/\.$/, "", s)
      if (s == "" || s ~ /[\/:@]/) {
        return ""
      }
      return s
    }

    function clean_client(s) {
      s = trim(s)
      if (s == "" || s == "127.0.0.1" || s == "::1") {
        return ""
      }
      return s
    }

    $0 !~ /dnsmasq/ || $0 !~ /query/ { next }
    {
      line = $0
      if (line ~ /query\[[^]]+\][[:space:]]+/) {
        sub(/^.*query\[[^]]+\][[:space:]]+/, "", line)
      } else if (line ~ /query[[:space:]]+/) {
        sub(/^.*query[[:space:]]+/, "", line)
      } else {
        next
      }

      split(line, parts, /[[:space:]]+from[[:space:]]+/)
      if (parts[1] == "" || parts[2] == "") {
        next
      }
      domain = clean_domain(parts[1])
      client = clean_client(parts[2])

      if (domain != "" && client != "") {
        print domain "\t" client
      }
    }
  '
}

aggregate_pairs() {
  extract_pairs | awk -F '\t' '
    {
      count[$1]++
      key = $1 SUBSEP $2
      if (!(key in seen)) {
        seen[key] = 1
        if (clients[$1] == "") {
          clients[$1] = $2
        } else {
          clients[$1] = clients[$1] "\034" $2
        }
      }
    }
    END {
      for (domain in count) {
        print domain "\t" count[domain] "\t" clients[domain]
      }
    }
  ' | sort -t "$(printf '\t')" -k1,1
}

hash_client() {
  client="$1"
  printf '%s|%s\n' "$ROUTER_NAME" "$client" | sha256sum | awk '{print substr($1, 1, 12)}'
}

CLIENT_HASH_LIST() {
  client_blob="$1"
  if [ -z "$client_blob" ]; then
    return 0
  fi

  printf '%s' "$client_blob" | tr '\034' '\n' | while IFS= read -r client; do
    [ -n "$client" ] || continue
    hash_client "$client"
  done | sort -u
}

aggregate_file="$TMP_DIR/observed.tsv"
aggregate_pairs > "$aggregate_file" || true

OUT_FILE="$OUTPUT_DIR/$ROUTER_NAME-$WINDOW.jsonl"
TMP_OUT="$OUT_FILE.tmp"
record_count=0
: > "$TMP_OUT"

while IFS="$(printf '\t')" read -r domain count client_blob; do
  [ -n "$domain" ] || continue

  CLIENT_HASH_FILE="$TMP_DIR/$domain.hashes"
  : > "$CLIENT_HASH_FILE"
  CLIENT_HASH_LIST "$client_blob" > "$CLIENT_HASH_FILE"

  if [ -s "$CLIENT_HASH_FILE" ]; then
    client_hashes="$(awk 'BEGIN{sep=""} {printf "%s\"%s\"", sep, $0; sep=", "} END {print ""}' "$CLIENT_HASH_FILE")"
    printf '{"node":"%s","window":"%s","domain":"%s","count":%s,"client_hashes":[%s]}\n' \
      "$ROUTER_NAME" "$WINDOW" "$domain" "$count" "$client_hashes" >> "$TMP_OUT"
  else
    printf '{"node":"%s","window":"%s","domain":"%s","count":%s}\n' \
      "$ROUTER_NAME" "$WINDOW" "$domain" "$count" >> "$TMP_OUT"
  fi

  record_count=$((record_count + 1))
done < "$aggregate_file"

if [ "$record_count" -eq 0 ]; then
  rm -f "$TMP_OUT"
  echo "no dnsmasq observed records found"
  exit 0
fi

mv "$TMP_OUT" "$OUT_FILE"
echo "observed aggregate written to $OUT_FILE"
