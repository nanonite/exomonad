#!/usr/bin/env bash
# Sets up a minimal test workspace for CI integration testing (issue #104).
#
# Creates a simple Python project in /tmp, pushes it to the local knot,
# and injects a pipeline event so the spindle picks it up on first connect.
#
# Run once, then: ./start-spindle.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

KNOT_CONTAINER="tangled-knot-knot-1"
KNOT_DB="$SCRIPT_DIR/server/knotserver.db"
SPINDLE_DB="$PROJECT_ROOT/spindle.db"
KNOT_HOSTNAME="localhost:5555"
OWNER_DID="did:plc:localdev"
REPO_NAME="ci-test"

WORK_DIR="/tmp/exomonad-ci-test"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

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
sqlite3 "$SPINDLE_DB" "INSERT OR IGNORE INTO repos (knot, owner, name) VALUES ('$KNOT_HOSTNAME', '$OWNER_DID', '$REPO_NAME');"
echo "  spindle repos: $KNOT_HOSTNAME/$OWNER_DID/$REPO_NAME"

echo ""
echo "=== Step 5: Inject pipeline event ==="
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
PYEOF

echo ""
echo "=== Setup complete ==="
echo "Test workspace: $WORK_DIR"
echo "Test: python3 src/test_hello.py  (nixery image = just python3, fast)"
echo ""
echo "Start spindle:  ./tangled-knot/start-spindle.sh"
echo "Watch for:      pipeline enqueued → pulling python3 image → Run tests → passed"
