#!/usr/bin/env bash
# Sets up a minimal test workspace for Tangled CI integration testing (issue #104).
# Called by `just e2e-tangled-ci`, which then starts spindle and watches logs.
#
# Creates a simple Python project in /tmp, pushes it to the local knot,
# seeds the spindle DB, and injects a pipeline event into the knot's event stream.
#
# Prerequisites:
#   - Knot container running: docker compose up -d  (in tests/e2e/tangled-ci/)
#
# Note: Raw git push to a bare repo does NOT trigger triggerPipeline() — that
# requires repos created via the knot's API (which installs git hooks). For the
# e2e test we inject the pipeline event directly into the knot's events table.
# Spindle is started fresh (stale DB deleted) by start-spindle.sh so it
# backfills from cursor=0 and picks up the injected event.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

KNOT_CONTAINER="tangled-knot-knot-1"
KNOT_DB="$PROJECT_ROOT/tangled-knot/server/knotserver.db"
SPINDLE_DB="$PROJECT_ROOT/spindle.db"
KNOT_HOSTNAME="localhost:5555"
OWNER_DID="did:plc:localdev"
REPO_NAME="ci-test"

echo "=== Preconditions ==="
if ! docker ps --filter name="$KNOT_CONTAINER" --filter status=running --format '{{.Names}}' | grep -q "$KNOT_CONTAINER"; then
    echo "ERROR: knot container '$KNOT_CONTAINER' is not running"
    echo "Start it: docker compose up -d  (in tests/e2e/tangled-ci/)"
    exit 1
fi
echo "  knot container: running"

if ! sqlite3 "$KNOT_DB" "SELECT 1 FROM events LIMIT 1;" > /dev/null 2>&1; then
    echo "ERROR: knot DB not accessible at $KNOT_DB"
    exit 1
fi
echo "  knot DB:        accessible"

WORK_DIR="/tmp/exomonad-ci-test"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo ""
echo "=== Step 1: Create minimal test repo ==="
cd "$WORK_DIR"
git init -b main
git config user.email "test@local"
git config user.name "Test"

mkdir -p .tangled/workflows src

cat > src/hello.py << 'EOF'
def add(a, b):
    return a + b

if __name__ == "__main__":
    print(add(2, 3))
EOF

cat > src/test_hello.py << 'EOF'
from hello import add

def test_add():
    assert add(2, 3) == 5

test_add()
print("all tests passed")
EOF

cat > .tangled/workflows/ci.yml << 'EOF'
engine: nixery
when:
  - event: [push, manual]
    branch: [main]
clone:
  depth: 1
  submodules: false
dependencies:
  nixpkgs:
    - python3
steps:
  - name: "Run tests"
    command: "python3 src/test_hello.py"
EOF

git add .
git commit -m "test: minimal CI test workspace"

echo ""
echo "=== Step 2: Push to local knot ==="
docker exec "$KNOT_CONTAINER" sh -c \
  "mkdir -p /home/git/repositories/owner/$REPO_NAME.git && \
   git init --bare /home/git/repositories/owner/$REPO_NAME.git && \
   chown -R git:git /home/git/repositories/owner/$REPO_NAME.git" 2>/dev/null || true

GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
  git push git@local-tangled:repositories/owner/$REPO_NAME.git HEAD:main --force

CURRENT_SHA="$(GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
  git ls-remote git@local-tangled:repositories/owner/$REPO_NAME.git refs/heads/main | awk '{print $1}')"
echo "Pushed: $CURRENT_SHA"

cd "$PROJECT_ROOT"

echo ""
echo "=== Step 3: Create DID-based HTTP clone path in container ==="
# securejoin requires a relative symlink (absolute targets get re-rooted under scanPath)
docker exec "$KNOT_CONTAINER" sh -c \
  "mkdir -p '/home/git/repositories/did:plc:localdev' && \
   ln -sfn '../owner/$REPO_NAME.git' '/home/git/repositories/did:plc:localdev/$REPO_NAME' && \
   echo 'OK: did:plc:localdev/$REPO_NAME -> ../owner/$REPO_NAME.git'"

echo ""
echo "=== Step 4: Seed spindle DB ==="
sqlite3 "$SPINDLE_DB" "
  CREATE TABLE IF NOT EXISTS repos (
    id integer primary key autoincrement,
    knot text not null,
    owner text not null,
    name text not null,
    addedAt text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    unique(owner, name)
  );
  INSERT OR IGNORE INTO repos (knot, owner, name) VALUES ('$KNOT_HOSTNAME', '$OWNER_DID', '$REPO_NAME');
"
echo "  spindle repos: $KNOT_HOSTNAME/$OWNER_DID/$REPO_NAME"

echo ""
echo "=== Step 5: Inject pipeline event into knot DB ==="
python3 - "$WORK_DIR/.tangled/workflows/ci.yml" "$KNOT_DB" "$KNOT_HOSTNAME" "$OWNER_DID" "$REPO_NAME" "$CURRENT_SHA" << 'PYEOF'
import sys, json, sqlite3, time

ci_yml, db_path, knot_hostname, owner_did, repo_name, new_sha = sys.argv[1:]

with open(ci_yml) as f:
    raw_yaml = f.read()

pipeline = {
    "$type": "sh.tangled.pipeline",
    "triggerMetadata": {
        "kind": "push",
        "push": {"ref": "refs/heads/main",
                 "oldSha": "0000000000000000000000000000000000000000",
                 "newSha": new_sha},
        "repo": {"did": owner_did, "knot": knot_hostname, "repo": repo_name},
    },
    "workflows": [{"engine": "nixery", "name": "ci.yml", "raw": raw_yaml,
                   "clone": {"depth": 1, "skip": False, "submodules": False}}],
}

rkey = f"ci-test-{int(time.time())}"
conn = sqlite3.connect(db_path)
conn.execute("INSERT OR REPLACE INTO events (rkey, nsid, event, created) VALUES (?, ?, ?, ?)",
             (rkey, "sh.tangled.pipeline", json.dumps(pipeline), int(time.time())))
conn.commit()
conn.close()
print(f"  injected: rkey={rkey} sha={new_sha[:12]}")
# Write rkey so the test runner can watch the specific workflow log
with open("/tmp/tangled-ci-e2e-rkey", "w") as f:
    f.write(rkey)
PYEOF

echo ""
echo "=== Setup complete ==="
echo "Test workspace: $WORK_DIR"
