#!/bin/sh
set -eu

manifest_version() {
  awk -F '\t' '$1=="version"{print $2; exit}' "$1"
}

manifest_safe_name() {
  case "$1" in
    ""|.*|*"/"*|*".."*|*[!A-Za-z0-9._-]*)
      return 1
      ;;
  esac
  return 0
}

manifest_profile_block() {
  awk -F '\t' -v profile="$2" '
    $1=="profile" { active = ($2 == profile); next }
    active { print }
  ' "$1"
}

manifest_has_profile() {
  awk -F '\t' -v profile="$2" '
    $1=="profile" && $2 == profile { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$1"
}

manifest_profile_files() {
  awk -F '\t' -v profile="$2" '
    $1=="profile" { active = ($2 == profile); next }
    active && $1=="file" { print $2 "\t" $3 "\t" $4 "\t" $5 }
  ' "$1"
}
