#!/bin/sh
set -eu

CONFIG_PATH="${FORGEJO_RUNNER_CONFIG:-/data/runner-config.yml}"
ACT_TMPFS_OPTION="--tmpfs /var/run/act"
ACT_VAR_RUN_OPTION="--volume forgejo-act-var-run:/var/run"
# Shared, persistent Nix store cache across job containers. Avoids re-fetching
# packages (and re-hitting flaky upstream mirrors, e.g. the CoCoALib source
# used by the Creusot/why3/cvc5 toolchain) on every job. Content-addressed by
# Nix, so sharing it does not change build outputs or reproducibility.
ACT_NIX_STORE_OPTION="--volume beast-rs-nix-store:/nix"
FORGEJO_HOST="${FORGEJO_RUNNER_JOB_FORGEJO_HOST:-forgejo}"

if [ ! -f "$CONFIG_PATH" ]; then
  forgejo-runner generate-config > "$CONFIG_PATH"
fi

forgejo_ip="$(getent hosts "$FORGEJO_HOST" | awk '{ print $1; exit }' || true)"
if [ -n "$forgejo_ip" ]; then
  FORGEJO_HOST_OPTION="--add-host ${FORGEJO_HOST}:${forgejo_ip}"
else
  echo "warning: could not resolve ${FORGEJO_HOST}; job containers may not reach Forgejo" >&2
  FORGEJO_HOST_OPTION=""
fi

options="$ACT_VAR_RUN_OPTION $ACT_NIX_STORE_OPTION"
if [ -n "$FORGEJO_HOST_OPTION" ]; then
  options="$options $FORGEJO_HOST_OPTION"
fi

# Forgejo Runner starts job containers in a nested Docker daemon. Compose DNS
# from this container is not available there, so propagate the resolved Forgejo
# service address as an explicit job-container host mapping on each start.
tmp="$(mktemp)"
awk -v options="$options" '
  /^container:/ { in_container = 1 }
  in_container && /^[^[:space:]]/ && $0 != "container:" { in_container = 0 }
  in_container && /^  options:/ {
    print "  options: \"" options "\""
    replaced = 1
    next
  }
  in_container && /^  workdir_parent:/ && !replaced {
    print "  options: \"" options "\""
    replaced = 1
  }
  { print }
' "$CONFIG_PATH" > "$tmp"
mv "$tmp" "$CONFIG_PATH"

exec forgejo-runner daemon --config "$CONFIG_PATH"
