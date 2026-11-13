#!/bin/sh
#
# Register the disposable runner on first start, then run the daemon.
#
# The job containers are created inside the dind service, not on the compose
# network, so they cannot resolve the compose DNS name `forgejo`.  Resolve it
# here (on the runner's own network) and pass the address to every job
# container as an explicit host mapping.  This is the same mechanism the
# persistent stack uses, kept in one place so the template has no hidden
# per-run patch step.
set -eu

CONFIG=/data/runner-config.yml
TEMPLATE=/etc/runner-config.yml

if [ ! -f "$CONFIG" ]; then
  cp "$TEMPLATE" "$CONFIG"
fi

forgejo_ip="$(getent hosts forgejo | awk '{ print $1; exit }')"
if [ -z "$forgejo_ip" ]; then
  echo "error: could not resolve forgejo; job containers would not reach the instance" >&2
  exit 1
fi

options="--add-host forgejo:${forgejo_ip}"
awk -v options="$options" '
  /^container:/ { in_container = 1 }
  in_container && /^[^[:space:]]/ && $0 != "container:" { in_container = 0 }
  in_container && /^  options:/ {
    print "  options: \"" options "\""
    replaced = 1
    next
  }
  { print }
  END { if (!replaced) exit 2 }
' "$CONFIG" > "$CONFIG.tmp" || {
  echo "error: runner config has no container.options line to patch" >&2
  exit 1
}
mv "$CONFIG.tmp" "$CONFIG"

if [ ! -f /data/.runner ]; then
  : "${FORGEJO_RUNNER_TOKEN:?FORGEJO_RUNNER_TOKEN is required to register the runner}"
  forgejo-runner register \
    --no-interactive \
    --instance http://forgejo:3000 \
    --token "$FORGEJO_RUNNER_TOKEN" \
    --name "${FORGEJO_RUNNER_NAME:-e2e-runner}" \
    --labels "${FORGEJO_RUNNER_LABELS:-ubuntu-latest:docker://ubuntu:24.04}" \
    --config "$CONFIG"
fi

exec forgejo-runner daemon --config "$CONFIG"
