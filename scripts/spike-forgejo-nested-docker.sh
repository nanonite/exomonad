#!/usr/bin/env bash
#
# spike1337: reproduce the nested-docker image problem for Forgejo Actions.
#
# The runner creates job containers in a nested dockerd (docker-in-docker).
# That daemon has its own image store, so an image that exists on the host is
# invisible to a job until it is copied into the nested store.  This script
# brings up a throwaway Forgejo + runner + dind stack, runs a generic workflow
# twice, and shows:
#
#   1. the job fails when the image is absent from the nested store, and
#   2. the same job succeeds after `docker save | docker exec -i <dind> docker load`.
#
# The stack is the checked-in template itself:
#
#   tests/e2e/lib/forgejo/docker-compose.actions.yml
#   tests/e2e/lib/forgejo/runner-config.yml
#   tests/e2e/lib/forgejo/runner-entrypoint.sh
#
# `-f` points at that compose file directly, so the run exercises the template
# (including the entrypoint's host-mapping patch and its registration path),
# not a private copy.  The script prints the template's checksums and compares
# them with the files mounted inside the runner container, so the evidence shows
# which files actually ran.
#
# Everything is scoped to a unique compose project whose name starts with
# `spike1337-`.  The script never pulls an image, never touches an existing
# container, and removes its own containers, volumes and networks with
# `docker compose -p <project> down -v --remove-orphans` on exit.
#
# Requirements on the host cache (no pulls are performed):
#   - codeberg.org/forgejo/forgejo:15
#   - data.forgejo.org/forgejo/runner:12
#   - docker:28-dind  (Docker 28 is required; see docs/decisions/forgejo-actions-nested-docker.md)
#   - ubuntu:24.04
#
# Usage:
#   scripts/spike-forgejo-nested-docker.sh
#
# Environment overrides:
#   DIND_IMAGE, FORGEJO_IMAGE, RUNNER_IMAGE, JOB_IMAGE
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$REPO_ROOT/tests/e2e/lib/forgejo"
COMPOSE_FILE="$TEMPLATE_DIR/docker-compose.actions.yml"

DIND_IMAGE="${DIND_IMAGE:-docker:28-dind}"
FORGEJO_IMAGE="${FORGEJO_IMAGE:-codeberg.org/forgejo/forgejo:15}"
RUNNER_IMAGE="${RUNNER_IMAGE:-data.forgejo.org/forgejo/runner:12}"
JOB_IMAGE="${JOB_IMAGE:-ubuntu:24.04}"

PROJECT="spike1337-repro-$(date -u +%Y%m%d%H%M%S)-$$"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/spike1337.XXXXXX")"
ADMIN="spike-admin"
ADMIN_PASS="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
BASE=""
DIND_CID=""
VOLUMES_BEFORE=""

log() { printf '%s\n' "$*"; }
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# Echo the command before it runs, so the captured log names every request.
run() {
  log "\$ $*"
  "$@"
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  log "teardown: docker compose -p $PROJECT -f $COMPOSE_FILE down -v --remove-orphans"
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans
  local leftovers
  leftovers="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}' 2>/dev/null)"
  if [ -n "$leftovers" ]; then
    log "LEFTOVER CONTAINERS: $leftovers"
    status=1
  fi
  leftovers="$(docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}' 2>/dev/null)"
  if [ -n "$leftovers" ]; then
    log "LEFTOVER VOLUMES: $leftovers"
    status=1
  fi
  leftovers="$(docker network ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}' 2>/dev/null)"
  if [ -n "$leftovers" ]; then
    log "LEFTOVER NETWORKS: $leftovers"
    status=1
  fi
  # The compose project is the only thing this run creates, so any volume that
  # appears during the run and survives teardown is a leak too -- named or
  # anonymous.  Compare against the baseline taken before the run.
  if [ -n "$VOLUMES_BEFORE" ]; then
    local volumes_after new_volumes
    volumes_after="$(docker volume ls -q 2>/dev/null | sort)"
    new_volumes="$(comm -13 <(printf '%s\n' "$VOLUMES_BEFORE") <(printf '%s\n' "$volumes_after") | sed '/^$/d')"
    if [ -n "$new_volumes" ]; then
      log "NEW VOLUMES SINCE BASELINE: $new_volumes"
      status=1
    fi
  fi
  if [ "$status" -eq 0 ]; then
    log "teardown verified: no container, volume or network for project $PROJECT"
    log "teardown verified: no volume created by this run remains"
  fi
  rm -rf "$WORKDIR"
  exit "$status"
}
trap cleanup EXIT INT TERM

compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }
api() { curl -fsS -u "$ADMIN:$ADMIN_PASS" "$@"; }

for image in "$FORGEJO_IMAGE" "$RUNNER_IMAGE" "$DIND_IMAGE" "$JOB_IMAGE"; do
  docker image inspect "$image" >/dev/null 2>&1 \
    || die "cached image missing: $image (this spike does not pull images)"
done

git config --get user.name >/dev/null 2>&1 || die "set a global git user.name first"
git config --get user.email >/dev/null 2>&1 || die "set a global git user.email first"

VOLUMES_BEFORE="$(docker volume ls -q | sort)"

log "project=$PROJECT"
log "workdir=$WORKDIR"
log "template=$COMPOSE_FILE"
for template_file in docker-compose.actions.yml runner-config.yml runner-entrypoint.sh; do
  log "template file: $(sha256sum "$TEMPLATE_DIR/$template_file")"
done
if grep -qE '^[[:space:]]*container_name:' "$COMPOSE_FILE"; then
  die "the Actions template must not pin container_name"
fi
log "template check: no container_name"

log "starting forgejo and dind from the checked-in template"
run compose up -d --wait forgejo dind

PORT="$(compose port forgejo 3000 | awk -F: '{print $NF}')"
BASE="http://127.0.0.1:$PORT"
DIND_CID="$(compose ps -q dind)"
log "\$ docker compose -p $PROJECT port forgejo 3000"
log "127.0.0.1:$PORT"
log "base=$BASE dind=$DIND_CID"

log "creating admin user and runner registration token via the Forgejo container CLI"
compose exec -T -u git forgejo forgejo admin user create \
  --admin --username "$ADMIN" --password "$ADMIN_PASS" \
  --email "$ADMIN@example.invalid" --must-change-password=false >/dev/null
TOKEN="$(compose exec -T -u git forgejo forgejo actions generate-runner-token | tr -d '\r\n')"
[ -n "$TOKEN" ] || die "the Forgejo CLI returned an empty runner token"
log "runner token: ${#TOKEN} characters from forgejo actions generate-runner-token"

# The template reads these from the compose environment; the entrypoint uses
# them to register on first start.
export FORGEJO_RUNNER_TOKEN="$TOKEN"
export FORGEJO_RUNNER_NAME="$PROJECT-runner"
export FORGEJO_RUNNER_LABELS="ubuntu-latest:docker://$JOB_IMAGE"

log "starting the runner through the template entrypoint"
run compose up -d runner

RUNNER_LOG="$WORKDIR/runner.log"
deadline=$((SECONDS + 90))
until compose logs runner >"$RUNNER_LOG" 2>/dev/null \
  && grep -q 'declared successfully' "$RUNNER_LOG"; do
  [ "$SECONDS" -lt "$deadline" ] || die "the runner did not register within 90s"
  sleep 1
done
log "runner registered through the template entrypoint:"
grep -E 'Runner registered successfully|declared successfully' "$RUNNER_LOG"

log "files inside the runner container (mount + entrypoint copy):"
run compose exec -T runner sha256sum /etc/runner-config.yml /runner-entrypoint.sh /data/runner-config.yml

runner_config="$(compose exec -T runner cat /data/runner-config.yml)"
grep -q 'options: "--add-host forgejo:' <<<"$runner_config" \
  || die "the entrypoint did not patch container.options"
grep -q 'force_pull: false' <<<"$runner_config" \
  || die "the template config must keep force_pull: false"
grep -q '^  enabled: false$' <<<"$runner_config" \
  || die "the template config must keep the cache disabled"
log "runner config after the entrypoint patch:"
printf '%s\n' "$runner_config"

log "resolved compose model (docker compose config --no-interpolate)"
run compose config --no-interpolate > "$WORKDIR/compose-config.yml"
grep -nE 'image: (docker:28-dind|data\.forgejo\.org/forgejo/runner:12|codeberg\.org/forgejo/forgejo:15)|privileged: true|FORGEJO__actions__ENABLED|target: 3000|internal: true' \
  "$WORKDIR/compose-config.yml"

log "creating scratch repository"
api -H 'Content-Type: application/json' \
  -d '{"name":"spike-repo","auto_init":true,"default_branch":"main","private":false}' \
  "$BASE/api/v1/user/repos" >/dev/null

REPO="$WORKDIR/repo"
git clone -q "$BASE/$ADMIN/spike-repo.git" "$REPO"
mkdir -p "$REPO/.forgejo/workflows"
cat > "$REPO/.forgejo/workflows/probe.yml" <<'EOF'
name: nested-docker-spike
on: push
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - name: Show job identity
        run: |
          echo "job-image-probe-ok"
          uname -a
          id
          head -3 /etc/os-release
      - name: Reach Forgejo from the job container
        run: |
          getent hosts forgejo
EOF
git -C "$REPO" add .forgejo/workflows/probe.yml
git -C "$REPO" commit -q -m "add generic probe workflow"
AUTH="$(printf '%s:%s' "$ADMIN" "$ADMIN_PASS" | base64 -w0)"
git -C "$REPO" -c http.extraHeader="Authorization: Basic $AUTH" push -q origin main

latest_run() {
  api "$BASE/api/v1/repos/$ADMIN/spike-repo/actions/runs?limit=50" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); ids=[r["id"] for r in d.get("workflow_runs",[]) if r.get("workflow_id")=="probe.yml"]; print(max(ids) if ids else "")'
}

wait_new_run() {
  local previous="$1" deadline=$((SECONDS + 60)) run_id
  while [ "$SECONDS" -lt "$deadline" ]; do
    run_id="$(latest_run)"
    if [ -n "$run_id" ] && [ "$run_id" -gt "$previous" ]; then
      printf '%s' "$run_id"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_run() {
  local run_id="$1" deadline=$((SECONDS + 90)) status
  while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(api "$BASE/api/v1/repos/$ADMIN/spike-repo/actions/runs/$run_id" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
    case "$status" in
      waiting|running|"") sleep 1 ;;
      *) printf '%s' "$status"; return 0 ;;
    esac
  done
  printf 'timeout'
}

job_logs() {
  local run_id="$1" attempt
  for attempt in 1 2 3 4 5; do
    if curl -fsS -u "$ADMIN:$ADMIN_PASS" \
      --connect-to "forgejo:3000:127.0.0.1:$PORT" \
      "http://forgejo:3000/$ADMIN/spike-repo/actions/runs/$run_id/jobs/0/attempt/1/logs"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

log "run 1: image absent from the nested store"
RUN1="$(wait_new_run 0)" || die "no workflow run appeared"
STATUS1="$(wait_run "$RUN1")"
LOG1="$(job_logs "$RUN1")" || die "could not fetch logs for run $RUN1"
printf '%s\n' "$LOG1"
[ "$STATUS1" = "failure" ] || die "run $RUN1 should have failed without the image, got $STATUS1"
grep -q "docker pull image=$JOB_IMAGE" <<<"$LOG1" || die "run $RUN1 did not attempt a pull"
grep -q 'Error response from daemon' <<<"$LOG1" || die "run $RUN1 did not report the pull error"
log "run 1 failed as expected (missing image, attempted pull)"

log "loading $JOB_IMAGE into the nested store"
log "\$ docker save $JOB_IMAGE | docker exec -i $DIND_CID docker -H tcp://localhost:2375 load"
docker save "$JOB_IMAGE" | docker exec -i "$DIND_CID" docker -H tcp://localhost:2375 load

log "run 2: same workflow after the image is loaded"
git -C "$REPO" commit -q --allow-empty -m "trigger after nested image load"
git -C "$REPO" -c http.extraHeader="Authorization: Basic $AUTH" push -q origin main
RUN2="$(wait_new_run "$RUN1")" || die "no second workflow run appeared"
STATUS2="$(wait_run "$RUN2")"
LOG2="$(job_logs "$RUN2")" || die "could not fetch logs for run $RUN2"
printf '%s\n' "$LOG2"
[ "$STATUS2" = "success" ] || die "run $RUN2 should have succeeded after load, got $STATUS2"
grep -q 'job-image-probe-ok' <<<"$LOG2" || die "run $RUN2 did not execute the job step"
grep -qE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+[[:space:]]+forgejo' <<<"$LOG2" \
  || die "run $RUN2 did not resolve forgejo inside the job container"
grep -q 'Job succeeded' <<<"$LOG2" || die "run $RUN2 did not report success"
log "PASS: missing image failed; after docker save | docker load the job succeeded"
log "PASS: the job container resolved forgejo through the template's host mapping"
