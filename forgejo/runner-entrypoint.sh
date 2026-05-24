#!/bin/sh
set -eu

CONFIG_PATH="${FORGEJO_RUNNER_CONFIG:-/data/runner-config.yml}"
ACT_TMPFS_OPTION="--tmpfs /var/run/act"

if [ ! -f "$CONFIG_PATH" ]; then
  forgejo-runner generate-config > "$CONFIG_PATH"
fi

if ! grep -q -- "$ACT_TMPFS_OPTION" "$CONFIG_PATH"; then
  tmp="$(mktemp)"
  awk -v option="$ACT_TMPFS_OPTION" '
    /^container:/ { in_container = 1 }
    in_container && /^[^[:space:]]/ && $0 != "container:" { in_container = 0 }
    in_container && /^  options:[[:space:]]*$/ {
      print "  options: \"" option "\""
      next
    }
    { print }
  ' "$CONFIG_PATH" > "$tmp"
  mv "$tmp" "$CONFIG_PATH"
fi

exec forgejo-runner daemon --config "$CONFIG_PATH"
