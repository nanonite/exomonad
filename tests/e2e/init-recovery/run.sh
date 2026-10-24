#!/usr/bin/env bash
set -euo pipefail

# E2E init-recovery test (chainlink #907).
#
# Drives the real `exomonad init` binary against a real tmux session and
# asserts the crash-recovery properties fixed in #903: a dead Server,
# Watcher, or TL window is replaced by exactly one healthy window instead
# of accumulating duplicates, and concurrent `exomonad init` invocations
# converge on the same single-window-per-role outcome instead of racing.
#
# Phases 1-6 do not exercise TL business logic — the seeded plan has no
# work, so the TL controller exits immediately after each `exomonad init`
# call there. That failure is expected and tolerated: those phases assert
# tmux window state and server socket health, not controller outcomes.
#
# Phase 7 exercises what phases 1-6 deliberately don't: continuation of a
# reviewed, completed leaf through the embedded controller, real watcher,
# real ledger, and production merge tool. It verifies merge and post-merge
# completion, not just tmux repair. The checkpoint's review, handoff, and
# publication evidence are seeded from exact mock-Forgejo records so this
# phase tests the installed controller/tool boundary without simulating an
# agent or claiming a reviewer was recovered from the event stream.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"
# shellcheck source=tl-window-state.sh
source "$SCRIPT_DIR/tl-window-state.sh"

# This recovery fixture never invokes a model. Keep project scaffolding
# deterministic when the isolated Codex home has no host model configuration.
export EXOMONAD_CODEX_MODEL="${EXOMONAD_CODEX_MODEL:-e2e-unused-model}"

SESSION="e2e-init-recovery"
CONTINUATION_SESSION="e2e-init-recovery-continuation"
AGGREGATE_SESSION="e2e-init-aggregate-recovery"
MOCK_PID=""
WASM_ARTIFACT_DIR=""
INITIAL_TMUX="${TMUX:-}"
INITIAL_TMUX_TMPDIR="${TMUX_TMPDIR:-}"
AGGREGATE_TMUX_TMPDIR=""

build_guest_wasm() {
    local role="$1"
    local package="wasm-guest-$role"
    local build_dir="$WORK_DIR/wasm-build"
    mkdir -p "$build_dir" "$WASM_ARTIFACT_DIR"
    (
        cd "$PROJECT_ROOT"
        nix develop .#wasm --command bash -c '
            set -euo pipefail
            export PATH="$PWD/.codex/tmp/bin:$PATH"
            build_dir="$1"
            package="$2"
            artifact_dir="$3"
            wasm32-wasi-cabal build --project-file=cabal.project.wasm \
                --builddir="$build_dir" "$package"
            mapfile -t artifacts < <(find "$build_dir" -type f -name "$package.wasm" -print)
            if (( ${#artifacts[@]} != 1 )); then
                printf "expected one fresh %s artifact under %s, found %s\\n" \
                    "$package" "$build_dir" "${#artifacts[@]}" >&2
                exit 1
            fi
            install -m 0644 "${artifacts[0]}" "$artifact_dir/$package.wasm"
            sha256sum "$artifact_dir/$package.wasm"
        ' e2e-wasm-build "$build_dir" "$package" "$WASM_ARTIFACT_DIR"
    )
}

install_verified_wasm_and_roles() {
    mkdir -p .exo/wasm
    for wasm_file in "$WASM_ARTIFACT_DIR"/wasm-guest-*.wasm; do
        [[ -s "$wasm_file" ]] || e2e_fail "verified scratch WASM artifact is missing: $wasm_file"
        ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
    done
    if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
        rm -rf .exo/roles
        cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
    fi
}

count_pull_publications() {
    python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
count = 0
if log_path.is_file():
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = str(request.get("path", "")).split("?", 1)[0]
        if request.get("method") == "POST" and path.endswith("/api/v1/repos/owner/repo/pulls"):
            count += 1
print(count)
PY
}

assert_configured_retry_dispatch_events() {
    python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

repository = Path(sys.argv[1])
required = {
    "tl.dispatch_intended",
    "tl.spawn_requested",
    "tl.spawn_request_accepted",
    "tl.dispatch_confirmed",
}
source = "proven_preexecution_retry"
slice_id = "issue-814-substitution-model-kani"
ledger = [
    json.loads(line)
    for segment in sorted((repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
accepted = [
    event for event in ledger
    if event.get("agent_id") == "kani"
    and event.get("type") in required
    and isinstance(event.get("data"), dict)
    and event["data"].get("slice_id") == slice_id
    and event["data"].get("route_reconciliation_source") == source
]
assert {event["type"] for event in accepted} == required, (
    f"canonical ledger is missing configured retry dispatch events: "
    f"{sorted(required - {event['type'] for event in accepted})}"
)
for event_type in required:
    matching = [event for event in accepted if event["type"] == event_type]
    assert len(matching) == 1, f"expected one canonical {event_type}, found {len(matching)}"

attempts = [
    event for event in ledger
    if event.get("agent_id") == "kani"
    and event.get("type") == "tool.called"
    and isinstance(event.get("data"), dict)
    and isinstance(event["data"].get("arguments"), dict)
    and event["data"]["arguments"].get("event_type") in required
    and isinstance(event["data"]["arguments"].get("payload"), dict)
    and event["data"]["arguments"]["payload"].get("slice_id") == slice_id
    and event["data"]["arguments"]["payload"].get("route_reconciliation_source") == source
]
assert {event["data"]["arguments"]["event_type"] for event in attempts} == required
for event_type in required:
    matching = [
        event for event in attempts
        if event["data"]["arguments"]["event_type"] == event_type
    ]
    assert len(matching) == 1, f"expected one attempted {event_type}, found {len(matching)}"
    assert matching[0]["data"].get("success") is True, (
        f"{event_type} controller emit was rejected: {matching[0]['data'].get('error')}"
    )
PY
}

emit_active_kani_blocker() {
    python3 - "$PROJECT_ROOT" "$AGG_REPO" "$AGG_DIR/blocker-before.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

project_root = Path(sys.argv[1])
repository = Path(sys.argv[2])
before_path = Path(sys.argv[3])
sys.path.insert(0, str(project_root))

from tl_loop.client.transport import TransportClient

root_dir = repository / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
kani_path = root_dir / "kani/run.json"
kani = json.loads(kani_path.read_text(encoding="utf-8"))
leaf = kani["slices"]["issue-814-substitution-model-kani"]
assert leaf["status"] == "blocked", "#814 must be actively blocked before publication"
assert leaf["attempts"] == 1
assert leaf["pr_number"] is None and leaf.get("publication") is None
assert root["slices"]["architecture"]["status"] == "merged"
assert root["current_order"] == 2
assert root["slices"]["kani"]["status"] != "merged"
assert root["slices"]["handoff-proof"]["status"] == "pending"

response = TransportClient(project_root=repository, timeout=10).call_tool(
    "tl",
    "kani",
    "emit_controller_event",
    {
        "event_type": "agent.task_blocked",
        "payload": {
            "outcome": "blocked",
            "slice_id": "issue-814-substitution-model-kani",
            "cause": "human_decision_required",
            "scope_attribution": "agent_lifecycle",
            "needs_human": True,
            "retryable": True,
            "recovery_action": "resume the preserved configured assignment",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "missing_handoff:nonzero_exit",
            "attempt": leaf["attempts"],
        },
    },
)
assert response.get("success") is True, f"production blocker event emit failed: {response!r}"
result = response.get("result")
assert isinstance(result, dict), f"blocker emit omitted its durable result: {response!r}"
event_id = result.get("event_id")
assert isinstance(event_id, str) and event_id

segments = repository / ".exo/ledger/segments"
records = [
    json.loads(line)
    for segment in sorted(segments.glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
matches = [record for record in records if record.get("event_id") == event_id]
assert len(matches) == 1, "production event was not retained exactly once"
record = matches[0]
run_seq = record.get("run_seq")
assert type(run_seq) is int and run_seq > 0, "canonical event omitted its durable sequence"
runtime_run_id = (repository / ".exo/run_id").read_text(encoding="utf-8").strip()
assert record.get("run_id") == runtime_run_id
assert record.get("agent_id") == "kani"
assert record.get("type") == "agent.task_blocked"
assert record.get("invocation_id") is None and record.get("generation") is None
assert record["data"].get("slice_id") == "issue-814-substitution-model-kani"
assert record["data"].get("attempt") == leaf["attempts"]

def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def optional_fingerprint(path: Path) -> str | None:
    return fingerprint(path) if path.is_file() else None

before = {
    "event_id": event_id,
    "run_seq": run_seq,
    "runtime_run_id": runtime_run_id,
    "architecture_slice": root["slices"]["architecture"],
    "kani_slice": root["slices"]["kani"],
    "kani_leaf": leaf,
    "architecture_run_sha256": fingerprint(root_dir / "architecture/run.json"),
    "architecture_journal_sha256": optional_fingerprint(root_dir / "architecture/action-journal.json"),
    "kani_run_sha256": fingerprint(kani_path),
    "kani_journal_sha256": optional_fingerprint(root_dir / "kani/action-journal.json"),
    "existing_gate_names": sorted(gate["name"] for gate in root.get("gates", [])),
}
before_path.write_text(json.dumps(before, sort_keys=True) + "\n", encoding="utf-8")

# Reproduce the generated unresolved owner gate left by the earlier routing
# failure. This is a pending gate, never an approval.
sys.path.insert(0, str(project_root / "tl_loop"))
from tl_loop.state.schema import GateStatus
from tl_loop.state.store import RunStore

RunStore("root", root_dir.parent).set_gate(f"task-blocked-owner:{run_seq}", GateStatus.PENDING)
print(event_id, run_seq)
PY
}

assert_active_kani_blocker_replayed() {
    python3 - "$PROJECT_ROOT" "$AGG_REPO" "$AGG_DIR/blocker-before.json" "$1" "$2" <<'PY'
import hashlib
import json
import sys
import time
from pathlib import Path

project_root = Path(sys.argv[1])
repository = Path(sys.argv[2])
before = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
event_id = sys.argv[4]
run_seq = int(sys.argv[5])
sys.path.insert(0, str(project_root))

from tl_loop.state.store import RunStore

root_dir = repository / ".exo/tl-loop/root"
store = RunStore("root", root_dir.parent)
child_store = RunStore("kani", root_dir)
deadline = time.monotonic() + 30
state = store.load()
while time.monotonic() < deadline:
    child_state = child_store.load()
    recovery = child_state.slices["issue-814-substitution-model-kani"].recovery
    if state.events.last_consumed_offset >= run_seq and recovery is not None and recovery.phase.value == "human_gate":
        state = store.load()
        break
    time.sleep(0.1)

child_state = child_store.load()
leaf_state = child_state.slices["issue-814-substitution-model-kani"]
recovery = leaf_state.recovery
assert recovery is not None and recovery.phase.value == "human_gate"
assert recovery.cause == "human_decision_required"
assert recovery.slice_attempt == 1
assert recovery.evidence.get("matched_difficulty_rule") == "missing_handoff:nonzero_exit"
assert state.events.last_consumed_offset >= run_seq, "event was not acknowledged after durable projection"
assert not any(gate.name == f"task-blocked-owner:{run_seq}" for gate in state.gates)
assert any(
    gate.name == "tl-ordered-child-recovery-kani" and gate.status.value == "pending"
    for gate in state.gates
), "root recovery projection did not retain its pending approval gate"
assert state.slices["architecture"].status.value == "merged"
assert state.slices["kani"].status.value != "merged"
assert state.current_order == 2

root_document = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
assert root_document["slices"]["architecture"] == before["architecture_slice"]
assert root_document["slices"]["kani"]["status"] == before["kani_slice"]["status"]
assert root_document["slices"]["handoff-proof"]["status"] == "pending"
assert root_document["slices"]["kani"]["dispatch_intent_id"] == before["kani_slice"]["dispatch_intent_id"]
assert root_document["slices"]["kani"]["dispatch_invocation_id"] == before["kani_slice"]["dispatch_invocation_id"]
assert leaf_state.status.value == "blocked"
assert any(
    gate.name == "tl-dispatch-failed-issue-814-substitution-model-kani"
    and gate.status.value == "pending"
    for gate in child_state.gates
), "child human-recovery projection did not retain its pending approval gate"

def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def optional_fingerprint(path: Path) -> str | None:
    return fingerprint(path) if path.is_file() else None

for path, key in ((root_dir / "architecture/run.json", "architecture_run_sha256"),):
    assert fingerprint(path) == before[key], f"blocker replay mutated completed sibling evidence: {path}"
for path, key in ((root_dir / "architecture/action-journal.json", "architecture_journal_sha256"),):
    assert optional_fingerprint(path) == before[key], f"blocker replay mutated retained journal state: {path}"
assert optional_fingerprint(root_dir / "kani/action-journal.json") == before["kani_journal_sha256"], \
    "blocker replay rewrote the original rejected child spawn journal"

current_gate_names = {gate.name for gate in state.gates}
for gate_name in before["existing_gate_names"]:
    assert gate_name in current_gate_names, f"replay removed unrelated gate {gate_name!r}"

owner_scopes = []
for checkpoint in root_dir.glob("*/run.json"):
    child = json.loads(checkpoint.read_text(encoding="utf-8"))
    if "issue-814-substitution-model-kani" in child.get("slices", {}):
        owner_scopes.append(child.get("run_id"))
assert owner_scopes == ["kani"], f"expected unique Kani leaf ownership, got {owner_scopes!r}"

ledger = [
    json.loads(line)
    for segment in sorted((repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
events = [row for row in ledger if row.get("event_id") == event_id]
assert len(events) == 1 and events[0].get("type") == "agent.task_blocked"
assert events[0].get("run_id") == before["runtime_run_id"]
assert events[0].get("agent_id") == "kani"
assert events[0].get("invocation_id") is None and events[0].get("generation") is None
print("active Kani blocker projected to the child human gate before acknowledgment")
PY
}

assert_kani_blocker_acknowledged_once() {
    python3 - "$AGG_REPO" "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

repository = Path(sys.argv[1])
event_id = sys.argv[2]
run_seq = int(sys.argv[3])
root_dir = repository / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
events = [
    json.loads(line)
    for segment in sorted((repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
    if json.loads(line).get("event_id") == event_id
]
assert len(events) == 1 and events[0].get("type") == "agent.task_blocked"
assert root["events"]["last_consumed_offset"] >= run_seq
assert not any(gate.get("name") == f"task-blocked-owner:{run_seq}" for gate in root.get("gates", []))
PY
}

assert_init_status() {
    local status="$1"
    local log_path="$2"
    local context="$3"
    if [[ "$status" -eq 0 ]]; then
        return
    fi
    if rg -Fq 'open terminal failed: not a terminal' "$log_path"; then
        e2e_log "$context returned the known detached-terminal status after init completed"
        return
    fi
    cat "$log_path"
    e2e_fail "$context exited with status $status"
}

assert_blocked_human_gate_hold() {
    local status="$1"
    local log_path="$2"
    [[ "$status" -ne 0 ]] \
        || e2e_fail "active blocker replay unexpectedly passed an unapproved human recovery gate"
    rg -Fq "required leaf recovery gate 'tl-dispatch-failed-issue-814-substitution-model-kani' is not approved" "$log_path" \
        || { cat "$log_path"; e2e_fail "active blocker replay did not stop at the precise pending leaf recovery gate"; }
    rg -Fq 'tl-ordered-child-recovery-kani status=pending' "$log_path" \
        || { cat "$log_path"; e2e_fail "active blocker replay omitted the pending parent recovery gate"; }
}

assert_one_live_window() {
    local role="$1"
    local count pane_dead
    count="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc "$role" || true)"
    [[ "$count" == "1" ]] || e2e_fail "repeat init did not retain exactly one $role window"
    pane_dead="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{pane_dead}' | awk -v role="$role" '$1 == role { print $2; exit }')"
    [[ "$pane_dead" == "0" ]] || e2e_fail "repeat init returned without a live $role window"
}

capture_local_edit_state() {
    python3 - "$AGG_REPO" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout

status = git("status", "--porcelain", "--untracked-files=all").decode()
expected_status = (
    "M  .exo/harness_policy.toml",
    "M  .exo/tracked-runtime-policy.toml",
    "M  preserved-staged.txt",
    " M CHANGELOG.md",
    " M preserved-unstaged.txt",
    " D preserved-deleted.txt",
    "?? preserved-untracked.txt",
    "?? .exo/config.toml",
)
for row in expected_status:
    assert row in status, f"expected dirty path {row!r} in:\n{status}"
print(json.dumps({
    "status": status,
    "staged_diff_sha256": hashlib.sha256(git("diff", "--cached", "--binary")).hexdigest(),
    "unstaged_diff_sha256": hashlib.sha256(git("diff", "--binary")).hexdigest(),
    "untracked_sha256": hashlib.sha256((repo / "preserved-untracked.txt").read_bytes()).hexdigest(),
    "ignored_untracked_sha256": hashlib.sha256(
        (repo / ".exo/e2e-runtime-ignored-untracked/runtime-state.txt").read_bytes()
    ).hexdigest(),
}, sort_keys=True))
PY
}

capture_index_and_worktree_bytes() {
    python3 - "$AGG_REPO" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
paths = [
    repo / ".git/index",
    repo / "preserved-staged.txt",
    repo / "preserved-unstaged.txt",
    repo / "preserved-untracked.txt",
    repo / "preserved-deleted.txt",
    repo / "CHANGELOG.md",
    repo / ".exo/harness_policy.toml",
    repo / ".exo/tracked-runtime-policy.toml",
    repo / ".exo/config.toml",
    repo / ".exo/e2e-runtime-ignored-untracked/runtime-state.txt",
]
print(json.dumps({
    str(path.relative_to(repo)): (
        hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"
    )
    for path in paths
}, sort_keys=True))
PY
}

assert_scope_handoff_commit() {
    python3 - "$AGG_REPO" "$AGGREGATE_REMOTE_HEAD" "$AGGREGATE_DIRTY_BEFORE" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
aggregate_head = sys.argv[2]
dirty_before = json.loads(sys.argv[3])

def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

head = git("rev-parse", "HEAD")
assert head != aggregate_head, "dirty handoff did not create a commit"
assert git("rev-parse", "HEAD^") == aggregate_head, "handoff commit has an unexpected parent"
assert git("show", "-s", "--format=%s", head) == (
    "Prepare parent worktree for ordered subTL handoff"
)
body = git("show", "-s", "--format=%B", head)
for trailer in (
    "Exomonad-Handoff-Identity:",
    "Exomonad-Handoff-Fingerprint:",
    "Exomonad-Handoff-Parent:",
    "Exomonad-Handoff-Branch-SHA256:",
    "Exomonad-Handoff-Tree:",
):
    assert trailer in body, f"handoff commit omitted {trailer}"
expected_paths = {
    ".exo/config.toml",
    ".exo/harness_policy.toml",
    ".exo/tracked-runtime-policy.toml",
    "CHANGELOG.md",
    "preserved-deleted.txt",
    "preserved-staged.txt",
    "preserved-unstaged.txt",
    "preserved-untracked.txt",
}
committed_paths = set(
    git("diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines()
)
assert committed_paths == expected_paths, (committed_paths, expected_paths)
assert ".exo/e2e-runtime-ignored-untracked/runtime-state.txt" not in committed_paths
ignored_file = repo / ".exo/e2e-runtime-ignored-untracked/runtime-state.txt"
assert ignored_file.is_file()
assert hashlib.sha256(ignored_file.read_bytes()).hexdigest() == dirty_before[
    "ignored_untracked_sha256"
]
assert git("status", "--porcelain", "--untracked-files=all") == ""
print(json.dumps({"handoff_commit": head, "changed_paths": sorted(committed_paths)}, sort_keys=True))
PY
}

capture_ledger_segments_fingerprint() {
    python3 - "$1/.exo/ledger/segments" <<'PY'
import hashlib
import sys
from pathlib import Path

segments = Path(sys.argv[1])
hasher = hashlib.sha256()
for path in sorted(segments.glob("segment-*.jsonl")):
    hasher.update(path.name.encode())
    hasher.update(path.read_bytes())
print(hasher.hexdigest())
PY
}

assert_legacy_recovery_receipt() {
    local project="$1"
    local expected_lock_contents="$2"
    local expected_fingerprint="$3"
    python3 - "$project" "$expected_lock_contents" "$expected_fingerprint" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
expected = sys.argv[2].encode()
expected_fingerprint = sys.argv[3]
segments = repo / ".exo/ledger/segments"
events = [
    json.loads(line)
    for path in sorted(segments.glob("segment-*.jsonl"))
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
intents = [event for event in events if event.get("type", event.get("event_type")) == "git.native_lock.legacy_recovery_intent"]
receipts = [event for event in events if event.get("type", event.get("event_type")) == "git.native_lock.legacy_recovered"]
assert len(intents) == 1, f"expected one legacy recovery intent, found {len(intents)}"
assert len(receipts) == 1, f"expected one legacy recovery receipt, found {len(receipts)}"
intent_id = intents[0].get("event_id", intents[0].get("id"))
intent_data = intents[0].get("data")
assert isinstance(intent_data, dict), "legacy recovery intent has no structured data"
legacy_lock = intent_data.get("original_lock")
assert isinstance(legacy_lock, dict), "legacy recovery intent has no original lock identity"
assert intent_data.get("recovery_fingerprint") == expected_fingerprint
assert intent_data.get("writer_stopped_authorization") is True
receipt = receipts[0]
data = receipt.get("data")
assert isinstance(data, dict), "legacy recovery receipt has no structured data"
assert data.get("intent_event_id") == intent_id, "legacy receipt is not linked to its authorization intent"
assert data.get("writer_stopped_authorization") is True, "receipt omitted explicit writer-stopped authorization"
assert data.get("recovery_fingerprint") == expected_fingerprint, "receipt changed the approved fingerprint"
assert data.get("index_lock_file") == str((repo / ".git/index.lock").resolve()), "receipt is for a different lock path"
quarantine = Path(data["quarantine_file"])
assert quarantine.read_bytes() == expected, "quarantined lock bytes differ from the approved legacy lock"
assert not (repo / ".git/index.lock").exists(), "recovered native lock still exists"
def matches_legacy_inode(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    observed = data.get("original_lock", data.get("lock"))
    return (
        isinstance(observed, dict)
        and observed.get("device") == legacy_lock.get("device")
        and observed.get("inode") == legacy_lock.get("inode")
    )

assert not any(
    event.get("type", event.get("event_type")) == "git.native_lock.held"
    and isinstance(event.get("data"), dict)
    and matches_legacy_inode(event["data"])
    for event in events
), "legacy recovery fabricated a managed lock ownership proof"
assert not any(
    event.get("type", event.get("event_type")) == "git.native_lock.quarantined"
    and isinstance(event.get("data"), dict)
    and matches_legacy_inode(event["data"])
    for event in events
), "legacy recovery fabricated a managed quarantine receipt"
PY
}

e2e_phase "Phase 0" "Checking preconditions..."
e2e_require_commands nix tmux git curl python3

cleanup() {
    local code=$?
    # Cleanup must always reach e2e_cleanup, including on a failing run.
    # Under `set -e`, `(exit "$code")` below would itself trigger errexit
    # for any nonzero code and abort this trap before e2e_cleanup runs,
    # leaking the tmux session and scratch work dir. Disable errexit for
    # the remainder of the trap so every step below always executes.
    set +e
    if [[ -n "$AGGREGATE_TMUX_TMPDIR" ]]; then
        env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$AGGREGATE_TMUX_TMPDIR" \
            tmux kill-session -t "$AGGREGATE_SESSION" 2>/dev/null
    fi
    if [[ -n "$INITIAL_TMUX" ]]; then
        env TMUX="$INITIAL_TMUX" tmux kill-session -t "$SESSION" 2>/dev/null
        env TMUX="$INITIAL_TMUX" tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null
    elif [[ -n "$INITIAL_TMUX_TMPDIR" ]]; then
        env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$INITIAL_TMUX_TMPDIR" \
            tmux kill-session -t "$SESSION" 2>/dev/null
        env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$INITIAL_TMUX_TMPDIR" \
            tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null
    else
        env -u TMUX -u TMUX_PANE -u TMUX_TMPDIR tmux kill-session -t "$SESSION" 2>/dev/null
        env -u TMUX -u TMUX_PANE -u TMUX_TMPDIR \
            tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null
    fi
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    if (( code != 0 )); then
        KEEP_E2E_WORKDIR=1
    fi
    trap - EXIT
    (exit "$code")
    e2e_cleanup
}
e2e_create_work_dir "init-recovery"
trap cleanup EXIT INT TERM

e2e_phase "Runtime build" "Building the current ExoMonad binary before the disposable run..."
nix develop --command cargo build -p exomonad
HOST_ARTIFACT_DIR="$WORK_DIR/host-artifacts"
mkdir -p "$HOST_ARTIFACT_DIR"
cp "$PROJECT_ROOT/target/debug/exomonad" "$HOST_ARTIFACT_DIR/exomonad"
chmod 0755 "$HOST_ARTIFACT_DIR/exomonad"
EXOMONAD_BIN="$HOST_ARTIFACT_DIR/exomonad"
EXOMONAD_BIN_SHA256="$(sha256sum "$EXOMONAD_BIN" | cut -d ' ' -f 1)"
export EXOMONAD_BIN
e2e_log "Disposable ExoMonad binary=$EXOMONAD_BIN sha256=$EXOMONAD_BIN_SHA256"

WASM_ARTIFACT_DIR="$WORK_DIR/wasm-artifacts"
e2e_phase "WASM build" "Building production guest WASM into isolated scratch output..."
build_guest_wasm devswarm
build_guest_wasm e2e-test
GUEST_WASM="$WASM_ARTIFACT_DIR/wasm-guest-devswarm.wasm"
E2E_WASM="$WASM_ARTIFACT_DIR/wasm-guest-e2e-test.wasm"
[[ -s "$GUEST_WASM" && -s "$E2E_WASM" ]] \
    || e2e_fail "isolated current-source guest WASM artifacts are missing"
GUEST_WASM_SHA256="$(sha256sum "$GUEST_WASM" | cut -d ' ' -f 1)"
E2E_WASM_SHA256="$(sha256sum "$E2E_WASM" | cut -d ' ' -f 1)"
e2e_log "Fresh devswarm WASM sha256=$GUEST_WASM_SHA256"
e2e_log "Fresh e2e-test WASM sha256=$E2E_WASM_SHA256"

if [[ "${E2E_AGGREGATE_ONLY:-0}" != "1" ]]; then
e2e_phase "Phase 1" "Creating scratch repository..."
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q
e2e_init_repo "Exomonad init-recovery E2E" "init-recovery-e2e@example.invalid"
git remote add origin "$REMOTE_DIR"
git push -u origin main -q
e2e_run_exomonad_new
install_verified_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[]}}\n' > .exo/tl-loop/plan.json
e2e_write_basic_config "$SESSION"
FIRST_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
printf 'port = %s\n' "$FIRST_SERVER_PORT" >> .exo/config.toml
export FORGEJO_TOKEN="test-token-e2e"
tmux kill-session -t "$SESSION" 2>/dev/null || true

window_line() {
    # Prints "<window_id> <pane_pid> <pane_dead>" for the first window
    # matching the given name, or nothing if no such window exists.
    tmux list-windows -t "$SESSION" -F '#{window_name} #{window_id} #{pane_pid} #{pane_dead}' 2>/dev/null \
        | awk -v name="$1" '$1 == name { print $2, $3, $4; exit }'
}

window_count() {
    tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -Fxc "$1" || true
}

run_init() {
    # exomonad init is expected to fail here: the seeded plan has no work,
    # so the TL controller exits during startup and the whole invocation
    # returns nonzero even though Server/Watcher recovery already
    # succeeded. That failure is intentional and tolerated everywhere this
    # helper is used — only tmux window state is asserted.
    "$EXOMONAD_BIN" init --session "$SESSION" > "$WORK_DIR/init-$1.log" 2>&1 || true
}

e2e_phase "Phase 2" "Running exomonad init to create Server, Watcher, and TL windows..."
run_init "first"
for name in Server Watcher TL; do
    [[ "$(window_count "$name")" == "1" ]] || {
        cat "$WORK_DIR/init-first.log"
        e2e_fail "expected exactly one $name window after first init, got $(window_count "$name")"
    }
done
e2e_log "PASS: first init created exactly one Server, Watcher, and TL window"

e2e_phase "Phase 3" "Killing the Watcher process and verifying dead-window replacement..."
read -r watcher_id watcher_pid _ < <(window_line Watcher)
kill -9 "$watcher_pid"
for _ in $(seq 1 50); do
    read -r _ _ dead < <(window_line Watcher)
    [[ "$dead" == "1" ]] && break
    sleep 0.1
done
read -r _ _ dead < <(window_line Watcher)
[[ "$dead" == "1" ]] || e2e_fail "Watcher window did not persist dead after its process was killed (remain-on-exit regression)"
e2e_log "Watcher window $watcher_id confirmed dead and still present"

run_init "watcher-recovery"
[[ "$(window_count Watcher)" == "1" ]] || {
    cat "$WORK_DIR/init-watcher-recovery.log"
    e2e_fail "expected exactly one Watcher window after recovery, got $(window_count Watcher) (duplicate-window regression)"
}
read -r new_watcher_id _ new_dead < <(window_line Watcher)
[[ "$new_watcher_id" != "$watcher_id" ]] || e2e_fail "Watcher window was not replaced (still $watcher_id)"
[[ "$new_dead" == "0" ]] || e2e_fail "recovered Watcher window is not alive"
e2e_log "PASS: dead Watcher window $watcher_id was replaced by live window $new_watcher_id, no duplicate"

e2e_phase "Phase 4" "Killing the Server process and verifying single-window recovery..."
read -r server_id server_pid _ < <(window_line Server)
kill -9 "$server_pid"
sleep 1
run_init "server-recovery"
[[ "$(window_count Server)" == "1" ]] || {
    cat "$WORK_DIR/init-server-recovery.log"
    e2e_fail "expected exactly one Server window after recovery, got $(window_count Server) (duplicate-window regression)"
}
for _ in $(seq 1 50); do
    [[ -S "$REPO_DIR/.exo/server.sock" ]] && curl -fsS --unix-socket "$REPO_DIR/.exo/server.sock" http://localhost/health >/dev/null 2>&1 && break
    sleep 0.2
done
curl -fsS --unix-socket "$REPO_DIR/.exo/server.sock" http://localhost/health >/dev/null \
    || e2e_fail "recovered Server socket is not healthy"
e2e_log "PASS: Server recovered to exactly one window with a healthy socket"

e2e_phase "Phase 5" "Running two concurrent init invocations and verifying no duplicate windows..."
read -r pre_watcher_id pre_watcher_pid _ < <(window_line Watcher)
kill -9 "$pre_watcher_pid"
read -r pre_server_id pre_server_pid _ < <(window_line Server)
kill -9 "$pre_server_pid"
sleep 1
( run_init "race-a" ) &
race_a=$!
( run_init "race-b" ) &
race_b=$!
wait "$race_a"
wait "$race_b"
for name in Server Watcher TL; do
    [[ "$(window_count "$name")" == "1" ]] || {
        cat "$WORK_DIR/init-race-a.log" "$WORK_DIR/init-race-b.log"
        e2e_fail "expected exactly one $name window after concurrent init, got $(window_count "$name") (concurrent-init lock regression)"
    }
done
e2e_log "PASS: two concurrent exomonad init invocations converged on exactly one window per role"

e2e_phase "Phase 6" "Confirming TL dead-window replacement never accumulated duplicates..."
[[ "$(window_count TL)" == "1" ]] || e2e_fail "TL window duplicated across repeated crash-and-reinit cycles: $(window_count TL)"
e2e_log "PASS: exactly one TL window survived five exomonad init invocations across three crash scenarios"

tmux kill-session -t "$SESSION" 2>/dev/null || true

e2e_phase "Phase 7" "Seeding a reviewed checkpoint and confirming production merge continuation..."

CONT_DIR="$WORK_DIR/continuation"
CONT_REPO="$CONT_DIR/repo"
CONT_REMOTE="$CONT_DIR/remote/owner/repo.git"
CONT_HOME="$CONT_DIR/home"
mkdir -p "$CONT_DIR"
mkdir -p "$CONT_HOME/.exo"
python3 "$PROJECT_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$PROJECT_ROOT/tl_loop" --output "$CONT_HOME/.exo/tl_loop.pyz"
python3 "$PROJECT_ROOT/scripts/check_tl_loop_archive.py" \
    "$CONT_HOME/.exo/tl_loop.pyz" --source "$PROJECT_ROOT/tl_loop"
CONTROLLER_ARCHIVE_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$CONT_HOME/.exo/tl_loop.pyz")"
BUILD_ID="$(python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("tl_loop/_build_fingerprint.json"))["git_commit"])' \
    "$CONT_HOME/.exo/tl_loop.pyz")"
e2e_log "Built isolated controller archive sha256=$CONTROLLER_ARCHIVE_SHA256 build=$BUILD_ID"
cat > "$CONT_DIR/git-ssh" <<'EOF'
#!/bin/sh
E2E_GIT_REMOTE_DIR="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$CONT_REMOTE|" "$CONT_DIR/git-ssh"
chmod +x "$CONT_DIR/git-ssh"
export GIT_SSH_COMMAND="$CONT_DIR/git-ssh"
export E2E_GIT_REMOTE_DIR="$CONT_REMOTE"

# Bare remote lives at .../owner/repo.git: exomonad's owner/repo parser reads
# the last two path segments of the git remote URL, independent of what
# forgejo_url points at. origin uses file:// so `git fetch` genuinely works;
# forgejo_url (below) is queried over real HTTP for PR/review/CI state.
mkdir -p "$(dirname "$CONT_REMOTE")"
git init --bare -q "$CONT_REMOTE"
mkdir -p "$CONT_REPO"
git -C "$CONT_REPO" init -q -b main
git -C "$CONT_REPO" remote add origin "file://$CONT_REMOTE"
git -C "$CONT_REPO" config user.name "Exomonad init-recovery E2E"
git -C "$CONT_REPO" config user.email "init-recovery-e2e@example.invalid"
git -C "$CONT_REPO" commit --allow-empty -q -m init
git -C "$CONT_REPO" push -u origin main -q
git -C "$CONT_REPO" checkout -q -b main.leaf-a
echo "leaf change" > "$CONT_REPO/leaf.txt"
git -C "$CONT_REPO" add leaf.txt
git -C "$CONT_REPO" commit -q -m "leaf-a change"
git -C "$CONT_REPO" push -q origin main.leaf-a
git -C "$CONT_REPO" checkout -q main
LEAF_SHA="$(git -C "$CONT_REPO" rev-parse main.leaf-a)"

MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
REMOTE_DIR="$CONT_REMOTE" MOCK_LOG="$CONT_DIR/mock.log" MOCK_ADVANCE_BASE_ON_MERGE=1 \
    python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
    > "$CONT_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$CONT_DIR/mock.stderr"; e2e_fail "mock Forgejo API did not become ready"; }

cd "$CONT_REPO"
"$EXOMONAD_BIN" new > "$CONT_DIR/new.log" 2>&1 || { cat "$CONT_DIR/new.log"; e2e_fail "'exomonad new' failed for the continuation fixture"; }
# Keep Git operations local while exposing the canonical SSH remote as the
# repository identity consumed by the controller and merge host.
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$CONT_DIR/git-ssh"
install_verified_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[{"name":"leaf-a","task":"leaf-a change","chainlink_issue_id":null}]}}\n' > .exo/tl-loop/plan.json
cp .exo/tl-loop/plan.json .exo/tl-loop/plan.snapshot
python3 - <<'PY'
from hashlib import sha256
from pathlib import Path

snapshot = Path(".exo/tl-loop/plan.snapshot").read_bytes()
Path(".exo/tl-loop/plan.snapshot.sha256").write_text(sha256(snapshot).hexdigest() + "\n")
PY
CONT_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
shell_command = "bash"
tmux_session = "$CONTINUATION_SESSION"
yolo = true
poll_interval = 1
port = $CONT_SERVER_PORT
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"
EOF

read -r PR_NUMBER HEAD_SHA < <(
    python3 "$SCRIPT_DIR/seed_publication.py" \
        --mock-url "$MOCK_URL" --repo "$CONT_REPO" \
        --head-branch "main.leaf-a" --head-sha "$LEAF_SHA" --slice-id "leaf-a" \
        --reviewer-token "reviewer-token-e2e"
)
[[ "$HEAD_SHA" == "$LEAF_SHA" ]] || e2e_fail "seed_publication.py returned an unexpected head SHA"
e2e_log "Filed and approved PR #$PR_NUMBER against the mock Forgejo (head=$HEAD_SHA)"

PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/seed_checkpoint.py" \
    --repo "$CONT_REPO" --mock-url "$MOCK_URL" --pr-number "$PR_NUMBER" \
    --head-sha "$HEAD_SHA" --branch "main.leaf-a" --slice-id "leaf-a"

BEFORE_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
print(s['reviewed_head'], s['verdict'], d['events']['last_consumed_offset'])
")"
read -r before_reviewed_head before_verdict before_offset <<< "$BEFORE_STATUS"
[[ "$before_reviewed_head" == "$HEAD_SHA" && "$before_verdict" == "GO" && "$before_offset" == "0" ]] \
    || e2e_fail "seeded checkpoint was not in the expected pre-recovery state: $BEFORE_STATUS"
e2e_log "Seeded reviewed checkpoint: pr_number=$PR_NUMBER reviewed_head=$HEAD_SHA verdict=GO (awaiting integration)"

tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null || true
HOME="$CONT_HOME" "$EXOMONAD_BIN" init --continue --session "$CONTINUATION_SESSION" \
    > "$CONT_DIR/init.log" 2>&1 || true
rg -Fq "server_build=\"$BUILD_ID\" controller_build=\"$BUILD_ID\"" "$CONT_DIR/init.log" \
    || { cat "$CONT_DIR/init.log"; e2e_fail "continuation binary and controller compatibility fingerprints did not match"; }

AFTER_STATUS=""
for _ in $(seq 1 50); do
    AFTER_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
post = s.get('post_merge') or {}
phase = post.get('phase') if isinstance(post, dict) else None
journal = json.load(open('.exo/tl-loop/root/action-journal.json'))
merges = [entry for entry in journal if entry.get('operation') == 'merge_pr' and entry.get('target') == 'leaf-a' and entry.get('status') == 'confirmed']
print(s['reviewed_head'], s['verdict'], s['status'], phase, len(merges), d['events']['last_consumed_offset'])
" 2>/dev/null)" || AFTER_STATUS=""
    [[ "$AFTER_STATUS" == "$HEAD_SHA GO merged complete 1 "* ]] && break
    sleep 0.2
done
read -r after_reviewed_head after_verdict after_status after_post_merge after_merge_count after_offset <<< "$AFTER_STATUS"

if [[ "$after_reviewed_head" != "$HEAD_SHA" || "$after_verdict" != "GO" \
    || "$after_status" != "merged" || "$after_post_merge" != "complete" \
    || "$after_merge_count" != "1" ]]; then
    cat "$CONT_DIR/init.log"
    cat "$CONT_REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    e2e_fail "embedded controller did not merge and finish the reviewed leaf after restart: got '$AFTER_STATUS'"
fi
[[ "$after_offset" -gt 0 ]] || e2e_fail "embedded controller did not consume any ledger events during recovery"
[[ "$(rg -c 'pulls/1/merge' "$CONT_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "production merge tool did not issue exactly one Forgejo merge request"
e2e_log "PASS: bundled controller resumed the reviewed checkpoint, used the production merge tool once, and completed post-merge bookkeeping (offset 0 -> $after_offset, head=$after_reviewed_head)"
fi

e2e_phase "Phase 8" "Recovering the aggregate, committing the scope handoff, and proving one post-tidy leaf spawn through actual init --continue..."

# Earlier phases use their own Git transport and may leave GIT_* selectors or
# config overrides in this shell. Phase 8 must start from its repository-local
# remotes and the transport created below.
while IFS= read -r variable; do
    case "$variable" in
        GIT_*) unset "$variable" ;;
    esac
done < <(compgen -e)

if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
fi

AGG_DIR="$WORK_DIR/aggregate-recovery"
AGG_REPO="$AGG_DIR/repo"
AGG_REMOTE="$AGG_DIR/remote/owner/repo.git"
AGG_HOME="$AGG_DIR/home"
mkdir -p "$AGG_DIR" "$AGG_HOME/.exo" "$(dirname "$AGG_REMOTE")" "$AGG_REPO"
# The earlier phases use another repository's Git transport. A separate tmux
# server keeps its inherited GIT_* environment out of the aggregate controller.
AGGREGATE_TMUX_TMPDIR="$AGG_DIR/tmux"
mkdir -p "$AGGREGATE_TMUX_TMPDIR"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$AGGREGATE_TMUX_TMPDIR"
e2e_log "Phase 8 tmux server isolated to $TMUX_TMPDIR"
python3 "$PROJECT_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$PROJECT_ROOT/tl_loop" --output "$AGG_HOME/.exo/tl_loop.pyz"
python3 "$PROJECT_ROOT/scripts/check_tl_loop_archive.py" \
    "$AGG_HOME/.exo/tl_loop.pyz" --source "$PROJECT_ROOT/tl_loop"
AGGREGATE_ARCHIVE_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_HOME/.exo/tl_loop.pyz")"
AGGREGATE_BUILD_ID="$(python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("tl_loop/_build_fingerprint.json"))["git_commit"])' \
    "$AGG_HOME/.exo/tl_loop.pyz")"
e2e_log "Built aggregate recovery archive sha256=$AGGREGATE_ARCHIVE_SHA256 build=$AGGREGATE_BUILD_ID"

git init --bare -q "$AGG_REMOTE"
git -C "$AGG_REPO" init -q -b main
git -C "$AGG_REPO" remote add origin "file://$AGG_REMOTE"
git -C "$AGG_REPO" config user.name "Exomonad aggregate recovery E2E"
git -C "$AGG_REPO" config user.email "aggregate-recovery-e2e@example.invalid"
mkdir -p "$AGG_REPO/.exo"
mkdir -p "$AGG_REPO/.chainlink"
mkdir -p "$AGG_REPO/.forgejo/workflows"
cp "$PROJECT_ROOT/CHANGELOG.md" "$AGG_REPO/CHANGELOG.md"
cp "$PROJECT_ROOT/.forgejo/workflows/ci.yml" "$AGG_REPO/.forgejo/workflows/ci.yml"
cp "$PROJECT_ROOT/.exo/harness_policy.toml" "$AGG_REPO/.exo/harness_policy.toml"
printf 'tracked runtime baseline\n' > "$AGG_REPO/.exo/tracked-runtime-policy.toml"
printf 'deleted before handoff baseline\n' > "$AGG_REPO/preserved-deleted.txt"
cat > "$AGG_REPO/.gitignore" <<'EOF'
.exo/*
!.exo/config.toml
!.exo/roles/
!.exo/lib/
!.exo/rules/
.exo/roles/
.codex/
.claude/settings.local.json
.opencode/
opencode.json
.chainlink/issues.db
.exo/e2e-runtime-ignored-untracked/
EOF
chainlink init --no-hooks --db "$AGG_REPO/.chainlink/issues.db" \
    > "$AGG_DIR/chainlink-init.log" 2>&1 \
    || { cat "$AGG_DIR/chainlink-init.log"; e2e_fail "failed to initialize disposable Chainlink database"; }
python3 - "$AGG_DIR/chainlink-issues.json" <<'PY'
import json
import sys
from pathlib import Path

rows = []
for issue_id in range(1, 815):
    is_target = issue_id == 814
    rows.append({
        "id": issue_id,
        "title": "Disposable Kani recovery issue 814" if is_target else f"Disposable closed issue {issue_id}",
        "description": "Isolated actual-init regression fixture.",
        "status": "open" if is_target else "closed",
        "priority": "high" if is_target else "low",
        "parent_id": None,
        "labels": [],
        "comments": [],
        "created_at": "2026-10-23T00:00:00Z",
        "updated_at": "2026-10-23T00:00:00Z",
        "closed_at": None if is_target else "2026-10-23T00:00:00Z",
    })
Path(sys.argv[1]).write_text(
    json.dumps({"version": 1, "exported_at": "2026-10-23T00:00:00Z", "issues": rows}),
    encoding="utf-8",
)
PY
chainlink import "$AGG_DIR/chainlink-issues.json" --db "$AGG_REPO/.chainlink/issues.db" \
    > "$AGG_DIR/chainlink-import.log" 2>&1 \
    || { cat "$AGG_DIR/chainlink-import.log"; e2e_fail "failed to seed disposable issue #814"; }
chainlink show 814 --db "$AGG_REPO/.chainlink/issues.db" \
    > "$AGG_DIR/chainlink-issue-814.log" \
    || e2e_fail "disposable issue #814 is not readable"
rg -Fq 'Status: open' "$AGG_DIR/chainlink-issue-814.log" \
    || e2e_fail "disposable issue #814 must remain open for routed recovery"
git -C "$AGG_REPO" commit --allow-empty -q -m "aggregate recovery base"
printf 'tracked staged baseline\n' > "$AGG_REPO/preserved-staged.txt"
printf 'tracked unstaged baseline\n' > "$AGG_REPO/preserved-unstaged.txt"
git -C "$AGG_REPO" add .gitignore .forgejo/workflows/ci.yml CHANGELOG.md preserved-deleted.txt preserved-staged.txt preserved-unstaged.txt
git -C "$AGG_REPO" add -f .exo/harness_policy.toml .exo/tracked-runtime-policy.toml
git -C "$AGG_REPO" commit -q -m "add local edit preservation baselines"
git -C "$AGG_REPO" push -u origin main -q

AGG_MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
AGG_MOCK_URL="http://127.0.0.1:$AGG_MOCK_PORT"
AGG_MERGED_PR_MARKER_DIR="$AGG_DIR/merged-pr-markers"
mkdir -p "$AGG_MERGED_PR_MARKER_DIR"
REMOTE_DIR="$AGG_REMOTE" MOCK_LOG="$AGG_DIR/mock.log" MOCK_ADVANCE_BASE_ON_MERGE=1 \
    MOCK_MERGED_PR_MARKER_DIR="$AGG_MERGED_PR_MARKER_DIR" \
    python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$AGG_MOCK_PORT" \
    > "$AGG_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$AGG_MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$AGG_MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$AGG_DIR/mock.stderr"; e2e_fail "aggregate recovery mock Forgejo API did not become ready"; }

cd "$AGG_REPO"
HOME="$AGG_HOME" "$EXOMONAD_BIN" new > "$AGG_DIR/new.log" 2>&1 \
    || { cat "$AGG_DIR/new.log"; e2e_fail "'exomonad new' failed for aggregate recovery fixture"; }
install_verified_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
python3 - .exo/harness_policy.toml <<'PY'
import sys
from pathlib import Path

policy_path = Path(sys.argv[1])
policy = policy_path.read_text(encoding="utf-8")
replacements = (
    (
        'allow = ["codex/gpt-5.6-luna", "claude/sonnet"]',
        'allow = ["codex/gpt-6-luna", "claude/sonnet"]',
    ),
    (
        'cost_rank = { "codex/gpt-5.6-luna" = 1, "claude/sonnet" = 2 }',
        'cost_rank = { "codex/gpt-6-luna" = 1, "claude/sonnet" = 2 }',
    ),
    (
        'per_harness_budget = { "codex/gpt-5.6-luna" = 80000, "claude/sonnet" = 40000 }',
        'per_harness_budget = { "codex/gpt-6-luna" = 80000, "claude/sonnet" = 40000 }',
    ),
    (
        '[roles.reviewer]\nallow = ["codex/gpt-5.6-luna"]\ncost_rank = { "codex/gpt-5.6-luna" = 1 }',
        '[roles.reviewer]\nallow = ["codex/gpt-6.1-sol"]\ncost_rank = { "codex/gpt-6.1-sol" = 1 }',
    ),
)
for before, after in replacements:
    if policy.count(before) != 1:
        raise SystemExit(f"expected exactly one harness policy entry: {before}")
    policy = policy.replace(before, after)
policy_path.write_text(policy, encoding="utf-8")
PY
python3 - .exo/harness_capability.toml <<'PY'
import sys
from pathlib import Path

capability_path = Path(sys.argv[1])
capabilities = capability_path.read_text(encoding="utf-8")
required_entries = (
    '"codex/gpt-6-luna" = "hard"',
    '"codex/gpt-6.1-sol" = "standard"',
)
for entry in required_entries:
    if entry not in capabilities:
        capabilities = capabilities.rstrip() + "\n" + entry + "\n"
capability_path.write_text(capabilities, encoding="utf-8")
PY
cat > "$AGG_DIR/git-ssh" <<'EOF'
#!/bin/sh
E2E_GIT_REMOTE_DIR="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$AGG_REMOTE|" "$AGG_DIR/git-ssh"
chmod +x "$AGG_DIR/git-ssh"
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$AGG_DIR/git-ssh"
export GIT_SSH_COMMAND="$AGG_DIR/git-ssh"
export E2E_GIT_REMOTE_DIR="$AGG_REMOTE"
[[ "$(git -C "$AGG_REPO" remote get-url origin)" == "git@forgejo-e2e:owner/repo.git" ]] \
    || e2e_fail "Phase 8 origin URL does not resolve to its isolated aggregate repository"
[[ "$(git -C "$AGG_REPO" config --get core.sshCommand)" == "$AGG_DIR/git-ssh" ]] \
    || e2e_fail "Phase 8 Git transport does not target its isolated remote"

SEED_AGGREGATE_ARGS=()
if [[ "${E2E_BLOCKER_ONLY:-0}" == "1" ]]; then
    SEED_AGGREGATE_ARGS+=(--active-kani-blocker)
fi
PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/seed_aggregate_checkpoint.py" \
    --repo "$AGG_REPO" --mock-url "$AGG_MOCK_URL" --output "$AGG_DIR/seed.json" \
    "${SEED_AGGREGATE_ARGS[@]}" \
    > "$AGG_DIR/seed.log" \
    || { cat "$AGG_DIR/seed.log"; cat "$AGG_DIR/mock.stderr"; e2e_fail "failed to seed completed-aggregate/later-child recovery state"; }
python3 - "$AGG_REPO" "$AGG_DIR/seed.json" <<'PY' \
    || e2e_fail "Phase 8 did not seed the failed-root/completed-aggregate/failed-child shape"
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
seed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
architecture = root["slices"]["architecture"]
kani = root["slices"]["kani"]
candidate = root["integration"]["candidates"]["architecture"]
kani_child_path = root_dir / "kani/run.json"
assert kani_child_path.is_file(), "failed Kani child checkpoint is missing"
kani_child = json.loads(kani_child_path.read_text(encoding="utf-8"))
exit_path = kani_child_path.parent / "controller-exit.json"
assert not exit_path.exists(), "legacy fixture must retain the missing normal-return diagnostic"
failed_leaf = kani_child["slices"]["issue-814-substitution-model-kani"]
child_journal = json.loads(
    (kani_child_path.parent / "action-journal.json").read_text(encoding="utf-8")
)
rejected_spawns = [
    entry for entry in child_journal
    if entry.get("operation") == "spawn_leaf"
    and entry.get("target") == "issue-814-substitution-model-kani"
    and entry.get("status") == "rejected"
]
assert root["fsm"]["phase"] == "tl_failed"
assert root["current_order"] == 2
assert architecture["status"] == "merged"
assert architecture["post_merge"]["phase"] == "complete"
assert candidate["lifecycle"] == "MERGED"
assert kani["status"] == "failed"
assert kani["dispatch_last_boundary"] == "sub_tl_started"
assert kani["dispatch_intent_id"] and kani["dispatch_authoritative_event_seq"] == 1
assert kani_child["run_id"] == "kani"
assert kani_child["parent_run_id"] == "root"
assert kani_child["fsm"]["phase"] == "tl_failed"
if seed["active_kani_blocker"]:
    assert failed_leaf["status"] == "blocked"
    assert failed_leaf["park_cause"] == "human_decision_required"
else:
    assert failed_leaf["status"] == "dispatch_failed"
    assert failed_leaf["park_cause"] == "dispatch_failed"
assert failed_leaf["requested_harness"] == "opencode/opencode-go/deepseek-v4.1-flash"
assert failed_leaf["dispatch_last_boundary"] == "spawn_request_failed"
assert failed_leaf.get("dispatch_invocation_id") is None
assert len(rejected_spawns) == 1
assert rejected_spawns[0]["arguments"]["intent_id"] == failed_leaf["dispatch_intent_id"]
assert rejected_spawns[0]["result"]["error"] == failed_leaf["dispatch_error"]
assert any(gate["name"] == "tl-dispatch-failed-issue-814-substitution-model-kani" for gate in kani_child["gates"])
assert any(gate["name"] == "tl-ordered-child-recovery-kani" for gate in root["gates"])
assert Path(kani["worktree"]).is_dir(), "dormant Kani worktree must be retained"
identity = json.loads((repo / ".exo/agents/kani/identity.json").read_text(encoding="utf-8"))
invocation = json.loads((repo / ".exo/agents/kani/invocation.json").read_text(encoding="utf-8"))
assert identity["agent_name"] == "kani"
assert identity["birth_branch"] == kani["branch"]
assert identity["working_dir"] == kani["worktree"]
assert invocation["invocation_id"] == seed["dormant_controller_invocation_id"]
assert invocation["trigger"] == "spawn" and invocation["status"] == "killed"
assert invocation["generation"] == 1 and invocation["ended_at"] is not None
assert invocation["branch"] == kani["branch"] and invocation["worktree"] == kani["worktree"]
assert invocation.get("pr_number") is None and invocation.get("head_sha") is None
PY
AGGREGATE_PR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aggregate_pr"])' "$AGG_DIR/seed.json")"
AGGREGATE_BASE_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_sha"])' "$AGG_DIR/seed.json")"
AGGREGATE_REMOTE_HEAD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aggregate_remote_head"])' "$AGG_DIR/seed.json")"
LEAF_PR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["leaf_pr"])' "$AGG_DIR/seed.json")"
LEAF_RECEIPT_SHA256="$(python3 -c 'import hashlib,json,sys; d=json.load(open(sys.argv[1])); r=json.load(open(d["child"])); e=r["slices"]["issue-811"]["post_merge"]["evidence"]; print(hashlib.sha256(json.dumps(e,sort_keys=True,separators=(",", ":")).encode()).hexdigest())' "$AGG_DIR/seed.json")"
REJECTED_KANI_SPAWN_SHA256="$(python3 - "$AGG_REPO/.exo/tl-loop/root/kani/action-journal.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

journal = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rejected = [
    entry for entry in journal
    if entry.get("operation") == "spawn_leaf"
    and entry.get("target") == "issue-814-substitution-model-kani"
    and entry.get("status") == "rejected"
]
assert len(rejected) == 1
print(hashlib.sha256(json.dumps(rejected[0], sort_keys=True, separators=(",", ":")).encode()).hexdigest())
PY
)"
ARCHITECTURE_INVOCATION_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_REPO/.exo/agents/architecture/invocation.json")"
KANI_IDENTITY_BASELINE="$AGG_DIR/kani-identity-before.json"
cp "$AGG_REPO/.exo/agents/kani/identity.json" "$KANI_IDENTITY_BASELINE"
[[ "$(git -C "$AGG_REPO" rev-parse main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "fixture did not retain the already-confirmed local parent synchronization"
[[ "$(git -C "$AGG_REPO" rev-parse origin/main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "fixture remote parent branch does not point at the confirmed aggregate merge"
AGGREGATE_MERGE_REQUESTS_BEFORE="$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)"
[[ "$AGGREGATE_MERGE_REQUESTS_BEFORE" == "1" ]] \
    || e2e_fail "fixture did not record exactly one authoritative remote aggregate merge"
PULL_PUBLICATIONS_BEFORE="$(count_pull_publications "$AGG_DIR/mock.log")"
[[ "$PULL_PUBLICATIONS_BEFORE" == "2" ]] \
    || e2e_fail "fixture expected only its leaf and aggregate PR publications, found $PULL_PUBLICATIONS_BEFORE"
e2e_log "Seeded merged aggregate PR #$AGGREGATE_PR, completed architecture evidence, and a dormant no-PR Kani controller after the #814 dirty-preflight rejection (parent HEAD=$AGGREGATE_REMOTE_HEAD)"

# The post-checkout hook runs only when the new handoff-proof source worktree
# is provisioned. It creates a dirty file in the outer root after the tidy
# commit has completed, while leaving the nested subTL source clean. A real
# leaf spawn can therefore succeed only if the production guard inspects the
# authenticated caller worktree rather than the global project root.
HANDOFF_WORKTREE="$AGG_REPO/.exo/worktrees/handoff-proof"
ROOT_ONLY_DIRTY_FILE="$AGG_REPO/post-tidy-root-only-dirty.txt"
ROOT_DIRTY_MARKER="$AGG_DIR/root-dirty-injected"
HANDOFF_HEAD_FILE="$AGG_DIR/handoff-source-head"
AGENT_EVENT_LOG="$AGG_DIR/agent-events.jsonl"
FAKE_CODEX_BIN="$AGG_DIR/fake-bin"
mkdir -p "$FAKE_CODEX_BIN"
install -m 0755 "$SCRIPT_DIR/fake-codex.sh" "$FAKE_CODEX_BIN/codex"
install -m 0755 "$SCRIPT_DIR/fake-codex.sh" "$FAKE_CODEX_BIN/opencode"
export PATH="$FAKE_CODEX_BIN:$PATH"
export E2E_AGENT_LOG="$AGENT_EVENT_LOG"
export E2E_SOURCE_WORKTREE="$HANDOFF_WORKTREE"
export E2E_ROOT_ONLY_DIRTY_FILE="$ROOT_ONLY_DIRTY_FILE"
export E2E_HANDOFF_HEAD_FILE="$HANDOFF_HEAD_FILE"
export E2E_MOCK_URL="$AGG_MOCK_URL"
export E2E_MCP_SOCKET="$AGG_REPO/.exo/server.sock"
export E2E_MOCK_LOG="$AGG_DIR/mock.log"
python3 - "$AGG_REPO/.git/hooks/post-checkout" "$HANDOFF_WORKTREE" "$ROOT_ONLY_DIRTY_FILE" \
    "$ROOT_DIRTY_MARKER" "$HANDOFF_HEAD_FILE" <<'PY'
import shlex
import sys
from pathlib import Path

hook_path, source_worktree, root_dirty_file, marker, head_file = sys.argv[1:]
hook = Path(hook_path)
hook.parent.mkdir(parents=True, exist_ok=True)
hook.write_text(
    "#!/bin/sh\n"
    "set -eu\n"
    "source=$(git rev-parse --show-toplevel)\n"
    f"if [ \"$source\" = {shlex.quote(source_worktree)} ] && [ ! -e {shlex.quote(marker)} ]; then\n"
    f"  printf '%s\\n' post-handoff-root-only > {shlex.quote(root_dirty_file)}\n"
    f"  git rev-parse HEAD > {shlex.quote(head_file)}\n"
    f"  : > {shlex.quote(marker)}\n"
    "fi\n",
    encoding="utf-8",
)
hook.chmod(0o755)
PY

LEGACY_LOCK_CONTENTS="legacy index lock evidence requiring explicit operator authorization"
printf '%s' "$LEGACY_LOCK_CONTENTS" > "$AGG_REPO/.git/index.lock"
[[ -s "$AGG_REPO/.git/index.lock" ]] \
    || e2e_fail "failed to create the nonempty legacy native index lock"
e2e_log "Created an unowned legacy lock without synthesizing a managed ownership proof"
AGGREGATE_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
shell_command = "bash"
tmux_session = "$AGGREGATE_SESSION"
yolo = true
poll_interval = 1
port = $AGGREGATE_SERVER_PORT
spawn_agent_type = "codex"
forgejo_url = "$AGG_MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"
worker_effort_level = "xhigh"

[reviewer]
agent_type = "codex"
model = "gpt-6.1-sol"
effort_level = "medium"
EOF

mkdir -p "$AGG_HOME/.codex"
cat > "$AGG_HOME/.codex/config.toml" <<'EOF'
model = "gpt-6-luna"
EOF
export CODEX_HOME="$AGG_HOME/.codex"
export EXOMONAD_CODEX_MODEL="gpt-6-luna"

LOCAL_EDIT_STATE_BEFORE="$(capture_local_edit_state)"
printf '%s\n' "$LOCAL_EDIT_STATE_BEFORE" > "$AGG_DIR/local-edits-before.json"
AGGREGATE_DIRTY_BEFORE="$LOCAL_EDIT_STATE_BEFORE"
e2e_log "Seeded staged, unstaged, deleted, tracked-ignored, nonignored-untracked, and ignored-untracked content for commit-all"
LOCAL_INDEX_AND_FILES_BEFORE="$(capture_index_and_worktree_bytes)"

tmux kill-session -t "$AGGREGATE_SESSION" 2>/dev/null || true
# Keep the test transport scoped to the session after the isolated server is
# created. This prevents Git selectors from being copied into its global env.
unset GIT_SSH_COMMAND E2E_GIT_REMOTE_DIR
tmux new-session -d -s "$AGGREGATE_SESSION" -n TL "sleep 0.2; exit 0"
if tmux show-environment -g | cut -d= -f1 | grep -q '^GIT_'; then
    e2e_fail "isolated Phase 8 tmux server inherited a Git environment override"
fi
tmux set-environment -t "$AGGREGATE_SESSION" GIT_SSH_COMMAND "$AGG_DIR/git-ssh"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_GIT_REMOTE_DIR "$AGG_REMOTE"
tmux set-environment -t "$AGGREGATE_SESSION" PATH "$PATH"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_AGENT_LOG "$AGENT_EVENT_LOG"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_SOURCE_WORKTREE "$HANDOFF_WORKTREE"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_ROOT_ONLY_DIRTY_FILE "$ROOT_ONLY_DIRTY_FILE"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_HANDOFF_HEAD_FILE "$HANDOFF_HEAD_FILE"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_MOCK_URL "$AGG_MOCK_URL"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_MCP_SOCKET "$AGG_REPO/.exo/server.sock"
tmux set-environment -t "$AGGREGATE_SESSION" E2E_AGG_REPO "$AGG_REPO"
# The accepted Kani declaration keeps its historical OpenCode route. The
# replacement invocation must use the operator's current Codex configuration.
tmux set-environment -t "$AGGREGATE_SESSION" CODEX_HOME "$CODEX_HOME"
tmux set-environment -t "$AGGREGATE_SESSION" HOME "$AGG_HOME"
tmux set-window-option -t "$AGGREGATE_SESSION:TL" remain-on-exit on
[[ "$(tmux show-environment -t "$AGGREGATE_SESSION" GIT_SSH_COMMAND)" == \
    "GIT_SSH_COMMAND=$AGG_DIR/git-ssh" ]] \
    || e2e_fail "aggregate tmux session did not retain its isolated Git transport"
for _ in $(seq 1 30); do
    [[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{pane_dead}' | awk '$1 == "TL" { print $2; exit }')" == "1" ]] && break
    sleep 0.1
done
DEAD_TL_ID="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{window_id} #{pane_dead}' | awk '$1 == "TL" { print $2, $3; exit }')"
[[ "${DEAD_TL_ID##* }" == "1" ]] || e2e_fail "aggregate fixture did not start with an exited TL pane"
read -r DEAD_TL_WINDOW _ <<< "$DEAD_TL_ID"

CONTROLLER_EPOCH_PATH="$AGG_REPO/.exo/tl-loop/root.controller-epoch"
PRE_RECOVERY_CONTROLLER_EPOCH="$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)"
PRE_REFUSAL_RUN_SHA256="$(sha256sum "$AGG_REPO/.exo/tl-loop/root/run.json" | cut -d ' ' -f 1)"
PRE_REFUSAL_JOURNAL_SHA256="$(sha256sum "$AGG_REPO/.exo/tl-loop/root/action-journal.json" | cut -d ' ' -f 1)"
PRE_REFUSAL_LEDGER_SHA256="$(capture_ledger_segments_fingerprint "$AGG_REPO")"
PRE_REFUSAL_KANI_INVOCATION_SHA256="$(sha256sum "$AGG_REPO/.exo/agents/kani/invocation.json" | cut -d ' ' -f 1)"
AGG_INIT_STATUS=0
HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e" \
    "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
    > "$AGG_DIR/init.log" 2>&1 || AGG_INIT_STATUS=$?
rg -Fq "server_build=\"$AGGREGATE_BUILD_ID\" controller_build=\"$AGGREGATE_BUILD_ID\"" "$AGG_DIR/init.log" \
    || { cat "$AGG_DIR/init.log"; e2e_fail "aggregate continuation binary/archive fingerprints did not match"; }
[[ "$AGG_INIT_STATUS" -ne 0 ]] \
    || e2e_fail "init succeeded despite an unattributed legacy lock"
rg -Fq 'exomonad git-lock inspect --project-root' "$AGG_DIR/init.log" \
    || { cat "$AGG_DIR/init.log"; e2e_fail "init omitted the supported legacy inspect command"; }
rg -Fq 'exomonad git-lock apply --project-root' "$AGG_DIR/init.log" \
    || { cat "$AGG_DIR/init.log"; e2e_fail "init omitted the supported legacy apply command"; }
rg -Fq -- '--writers-stopped' "$AGG_DIR/init.log" \
    || { cat "$AGG_DIR/init.log"; e2e_fail "init omitted explicit stopped-writer authorization"; }
[[ "$(cat "$AGG_REPO/.git/index.lock")" == "$LEGACY_LOCK_CONTENTS" ]] \
    || e2e_fail "initial init refusal changed the legacy lock bytes"
[[ "$(capture_index_and_worktree_bytes)" == "$LOCAL_INDEX_AND_FILES_BEFORE" ]] \
    || e2e_fail "initial init refusal changed the index or unrelated local files"
[[ "$(sha256sum "$AGG_REPO/.exo/tl-loop/root/run.json" | cut -d ' ' -f 1)" == "$PRE_REFUSAL_RUN_SHA256" ]] \
    || e2e_fail "initial init refusal changed the accepted root checkpoint"
[[ "$(sha256sum "$AGG_REPO/.exo/tl-loop/root/action-journal.json" | cut -d ' ' -f 1)" == "$PRE_REFUSAL_JOURNAL_SHA256" ]] \
    || e2e_fail "initial init refusal changed the durable action journal"
[[ "$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)" == "$PRE_RECOVERY_CONTROLLER_EPOCH" ]] \
    || e2e_fail "initial init refusal advanced the controller epoch"
[[ "$(capture_ledger_segments_fingerprint "$AGG_REPO")" == "$PRE_REFUSAL_LEDGER_SHA256" ]] \
    || e2e_fail "initial init refusal changed immutable ledger history"
[[ "$(sha256sum "$AGG_REPO/.exo/agents/kani/invocation.json" | cut -d ' ' -f 1)" == "$PRE_REFUSAL_KANI_INVOCATION_SHA256" ]] \
    || e2e_fail "initial init refusal changed the dormant Kani invocation"
CURRENT_TL_ID="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{window_id} #{pane_dead}' | awk '$1 == "TL" { print $2, $3; exit }')"
[[ "$CURRENT_TL_ID" == "$DEAD_TL_WINDOW 1" ]] \
    || e2e_fail "legacy-lock refusal restarted or replaced the exited TL pane"
[[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc Server || true)" == "0" ]] \
    || e2e_fail "legacy-lock refusal started a Server before Git readiness"
[[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc Watcher || true)" == "0" ]] \
    || e2e_fail "legacy-lock refusal started a Watcher before Git readiness"
HOME="$AGG_HOME" "$EXOMONAD_BIN" git-lock inspect --project-root "$AGG_REPO" \
    > "$AGG_DIR/legacy-inspect.log" 2>&1 \
    || { cat "$AGG_DIR/legacy-inspect.log"; e2e_fail "actual binary legacy inspection failed"; }
LEGACY_FINGERPRINT="$(python3 -c 'import pathlib,re,sys; m=re.search(r"\"recovery_fingerprint\"\s*:\s*\"([0-9a-f]{64})\"", pathlib.Path(sys.argv[1]).read_text()); print(m.group(1) if m else "")' "$AGG_DIR/legacy-inspect.log")"
[[ "$LEGACY_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]] \
    || { cat "$AGG_DIR/legacy-inspect.log"; e2e_fail "legacy inspection did not return a valid fingerprint"; }
python3 - "$AGG_DIR/legacy-inspect.log" <<'PY' \
    || e2e_fail "legacy inspection did not report a clear, unowned managed scope"
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
report, _ = json.JSONDecoder().raw_decode(text)
assert report["matching_managed_proof"] is False
assert report["managed_activity"]["status"] == "clear"
assert report["lock"]["size"] > 0
PY
[[ "$(cat "$AGG_REPO/.git/index.lock")" == "$LEGACY_LOCK_CONTENTS" ]] \
    || e2e_fail "read-only legacy inspection changed lock bytes"
[[ "$(capture_index_and_worktree_bytes)" == "$LOCAL_INDEX_AND_FILES_BEFORE" ]] \
    || e2e_fail "read-only legacy inspection changed the index or local files"
[[ "$(capture_ledger_segments_fingerprint "$AGG_REPO")" == "$PRE_REFUSAL_LEDGER_SHA256" ]] \
    || e2e_fail "read-only legacy inspection wrote recovery history"
HOME="$AGG_HOME" "$EXOMONAD_BIN" git-lock apply --project-root "$AGG_REPO" \
    --expected-fingerprint "$LEGACY_FINGERPRINT" --writers-stopped \
    > "$AGG_DIR/legacy-apply.log" 2>&1 \
    || { cat "$AGG_DIR/legacy-apply.log"; e2e_fail "authorized actual-binary legacy recovery failed"; }
rg -Fq 'Legacy Git lock recovered:' "$AGG_DIR/legacy-apply.log" \
    || { cat "$AGG_DIR/legacy-apply.log"; e2e_fail "legacy apply omitted its durable receipt"; }
assert_legacy_recovery_receipt "$AGG_REPO" "$LEGACY_LOCK_CONTENTS" "$LEGACY_FINGERPRINT"
[[ "$(capture_local_edit_state)" == "$LOCAL_EDIT_STATE_BEFORE" ]] \
    || e2e_fail "legacy recovery changed staged, unstaged, or untracked edits"
[[ "$(git -C "$AGG_REPO" rev-parse main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "init refusal or legacy inspection changed the already-synchronized parent branch"
[[ "$(git -C "$AGG_REPO" rev-parse origin/main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "legacy recovery changed the remote merge observation"
e2e_log "Actual init refused the legacy lock; compiled inspect/apply recovered approved fingerprint $LEGACY_FINGERPRINT"

# The blocker-only acceptance keeps #814 active before first replay. The normal
# aggregate run leaves the failure proof available for its ordered retry path.
PHASE8_SERVER_COMMAND="EXOMONAD_TMUX_SESSION=$AGGREGATE_SESSION EXOMONAD_ROOT_AGENT_TYPE=codex EXOMONAD_SPAWN_AGENT_TYPE=codex EXOMONAD_REVIEWER_AGENT_TYPE=codex EXOMONAD_CODEX_MODEL=gpt-6-luna $EXOMONAD_BIN serve"
tmux new-window -d -t "$AGGREGATE_SESSION" -n Server -c "$AGG_REPO" "$PHASE8_SERVER_COMMAND"
tmux set-window-option -t "$AGGREGATE_SESSION:Server" remain-on-exit on
for _ in $(seq 1 100); do
    if python3 - "$AGG_REPO/.exo/server.sock" <<'PY' >/dev/null 2>&1
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.2)
client.connect(sys.argv[1])
client.close()
PY
    then
        break
    fi
    sleep 0.1
done
[[ -S "$AGG_REPO/.exo/server.sock" ]] || e2e_fail "disposable Phase 8 server did not create its MCP socket"
KANI_BLOCKER_EVENT_ID=""
KANI_BLOCKER_RUN_SEQ=""
if [[ "${E2E_BLOCKER_ONLY:-0}" == "1" ]]; then
    read -r KANI_BLOCKER_EVENT_ID KANI_BLOCKER_RUN_SEQ < <(emit_active_kani_blocker)
    e2e_log "Emitted active event-shaped blocker $KANI_BLOCKER_EVENT_ID at ledger sequence $KANI_BLOCKER_RUN_SEQ before controller continuation"
fi

AGG_INIT_STATUS=0
HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e" \
    "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
    > "$AGG_DIR/init.log" 2>&1 || AGG_INIT_STATUS=$?
if [[ "${E2E_BLOCKER_ONLY:-0}" == "1" ]]; then
    assert_blocked_human_gate_hold "$AGG_INIT_STATUS" "$AGG_DIR/init.log"
else
    assert_init_status "$AGG_INIT_STATUS" "$AGG_DIR/init.log" "aggregate init --continue after legacy recovery"
fi
[[ "$(rg -F -c 'Recovered a proven orphan native Git lock' "$AGG_DIR/init.log" || printf '0')" == "0" ]] \
    || { cat "$AGG_DIR/init.log"; e2e_fail "legacy lock was misreported as a managed orphan"; }
[[ "$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)" != "$PRE_RECOVERY_CONTROLLER_EPOCH" ]] \
    || e2e_fail "post-recovery init did not start the fresh controller to reconcile the root run"
assert_one_live_window Server
if [[ -n "$KANI_BLOCKER_EVENT_ID" && -n "$KANI_BLOCKER_RUN_SEQ" ]]; then
    assert_active_kani_blocker_replayed "$KANI_BLOCKER_EVENT_ID" "$KANI_BLOCKER_RUN_SEQ"
fi
if [[ "${E2E_BLOCKER_ONLY:-0}" == "1" ]]; then
    # Seed the historical owner only after both child and parent projections
    # have durably consumed the blocker. A live owner must not affect routing
    # or acknowledgment of the event that predates it.
    tmux send-keys -t "$AGGREGATE_SESSION:Server" C-c
    for _ in $(seq 1 100); do
        [[ ! -S "$AGG_REPO/.exo/server.sock" ]] && break
        sleep 0.1
    done
    [[ ! -S "$AGG_REPO/.exo/server.sock" ]] \
        || e2e_fail "Codex server did not release its socket before historical owner setup"
    tmux kill-window -t "$AGGREGATE_SESSION:Server" 2>/dev/null || true
    python3 - "$AGG_REPO/.exo/config.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
assert text.count('worker_effort_level = "xhigh"') == 1
path.write_text(text.replace('worker_effort_level = "xhigh"', 'worker_effort_level = "high"'), encoding="utf-8")
PY
    PHASE8_OPENCODE_SERVER_COMMAND="EXOMONAD_TMUX_SESSION=$AGGREGATE_SESSION EXOMONAD_ROOT_AGENT_TYPE=codex EXOMONAD_SPAWN_AGENT_TYPE=opencode EXOMONAD_WORKER_MODEL=opencode-go/deepseek-v4.1-flash EXOMONAD_WORKER_EFFORT_LEVEL=high EXOMONAD_REVIEWER_AGENT_TYPE=codex EXOMONAD_CODEX_MODEL=gpt-6-luna $EXOMONAD_BIN serve"
    tmux new-window -d -t "$AGGREGATE_SESSION" -n Server -c "$AGG_REPO" "$PHASE8_OPENCODE_SERVER_COMMAND"
    tmux set-window-option -t "$AGGREGATE_SESSION:Server" remain-on-exit on
    for _ in $(seq 1 100); do
        if python3 - "$AGG_REPO/.exo/server.sock" <<'PY' >/dev/null 2>&1
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.2)
client.connect(sys.argv[1])
client.close()
PY
        then
            break
        fi
        sleep 0.1
    done
    [[ -S "$AGG_REPO/.exo/server.sock" ]] || e2e_fail "historical OpenCode server did not create its MCP socket"
    python3 "$SCRIPT_DIR/configured-owner-resume.py" seed \
        --repo "$AGG_REPO" --output "$AGG_DIR/preexisting-opencode-owner.json" \
        > "$AGG_DIR/preexisting-opencode-owner-seed.json" \
        || { cat "$AGG_DIR/preexisting-opencode-owner-seed.json"; e2e_fail "failed to create a real historical OpenCode #814 owner"; }
    tmux send-keys -t "$AGGREGATE_SESSION:Server" C-c
    for _ in $(seq 1 100); do
        [[ ! -S "$AGG_REPO/.exo/server.sock" ]] && break
        sleep 0.1
    done
    [[ ! -S "$AGG_REPO/.exo/server.sock" ]] \
        || e2e_fail "historical OpenCode server did not release its socket before route migration"
    tmux kill-window -t "$AGGREGATE_SESSION:Server" 2>/dev/null || true
    python3 - "$AGG_REPO/.exo/config.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
assert text.count('worker_effort_level = "high"') == 1
path.write_text(text.replace('worker_effort_level = "high"', 'worker_effort_level = "xhigh"'), encoding="utf-8")
PY
    tmux new-window -d -t "$AGGREGATE_SESSION" -n Server -c "$AGG_REPO" "$PHASE8_SERVER_COMMAND"
    tmux set-window-option -t "$AGGREGATE_SESSION:Server" remain-on-exit on
    for _ in $(seq 1 100); do
        if python3 - "$AGG_REPO/.exo/server.sock" <<'PY' >/dev/null 2>&1
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.2)
client.connect(sys.argv[1])
client.close()
PY
        then
            break
        fi
        sleep 0.1
    done
    [[ -S "$AGG_REPO/.exo/server.sock" ]] || e2e_fail "configured Codex server did not restart after owner setup"
    PYTHONPATH="$PROJECT_ROOT" "$PROJECT_ROOT/tl_loop/.venv/bin/python" -m tl_loop gate --project-root "$AGG_REPO" --run-id root \
        --name "tl-ordered-child-recovery-kani" --approve \
        > "$AGG_DIR/approve-kani-ordered-recovery.log" 2>&1 \
        || { cat "$AGG_DIR/approve-kani-ordered-recovery.log"; e2e_fail "could not approve the retained ordered Kani recovery gate"; }
    PYTHONPATH="$PROJECT_ROOT" "$PROJECT_ROOT/tl_loop/.venv/bin/python" -m tl_loop gate --project-root "$AGG_REPO" --run-id kani \
        --name "tl-dispatch-failed-issue-814-substitution-model-kani" --approve \
        > "$AGG_DIR/approve-kani-dispatch-recovery.log" 2>&1 \
        || { cat "$AGG_DIR/approve-kani-dispatch-recovery.log"; e2e_fail "could not approve the retained Kani dispatch recovery gate"; }
    BLOCKER_INIT_STATUS=0
    HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
        "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
        > "$AGG_DIR/init-blocker-controller-recovery.log" 2>&1 || BLOCKER_INIT_STATUS=$?
    assert_init_status "$BLOCKER_INIT_STATUS" "$AGG_DIR/init-blocker-controller-recovery.log" \
        "controller recovery of active #814 human gate"
    python3 "$SCRIPT_DIR/configured-owner-resume.py" verify-controller \
        --repo "$AGG_REPO" --baseline "$AGG_DIR/preexisting-opencode-owner.json" \
        --output "$AGG_DIR/configured-owner-resume.json" \
        > "$AGG_DIR/configured-owner-resume.stdout" \
        || { cat "$AGG_DIR/configured-owner-resume.stdout"; e2e_fail "controller did not checkpoint the resumed Codex owner and publication"; }
    ROOT_EPOCH_AFTER_BLOCKER="$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)"
    BLOCKER_EFFECTS_BEFORE="$AGG_DIR/blocker-effects-before-repeat.json"
    # Agent actions can complete asynchronously between init invocations; the
    # owner verifier checks exact worker/reviewer counts instead of log bytes.
    python3 - "$AGG_REPO" "$AGG_DIR/mock.log" "$BLOCKER_EFFECTS_BEFORE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo, mock_log, output = map(Path, sys.argv[1:])
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
kani = json.loads((root_dir / "kani/run.json").read_text())
leaf = kani["slices"]["issue-814-substitution-model-kani"]
def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def remote_mutations(path: Path) -> dict[str, int]:
    counts = {"publications": 0, "merges": 0}
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_path = str(row.get("path", "")).split("?", 1)[0]
        if row.get("method") == "POST" and request_path.endswith("/api/v1/repos/owner/repo/pulls"):
            counts["publications"] += 1
        if row.get("method") == "POST" and "/api/v1/repos/owner/repo/pulls/" in request_path and request_path.endswith("/merge"):
            counts["merges"] += 1
    return counts
def journal_effect_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    entries = json.loads(path.read_text(encoding="utf-8"))
    stable_effects = [
        entry for entry in entries
        if not (
            entry.get("operation") == "emit_controller_event"
            and isinstance(entry.get("arguments"), dict)
            and entry["arguments"].get("event_type") in {
                "tl.phase_changed",
                "tl.recovery_boundary",
            }
        )
    ]
    encoded = json.dumps(stable_effects, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

ledger = [
    json.loads(line)
    for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
dispatch_events = [
    row for row in ledger
    if row.get("type") in {
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
    }
    and isinstance(row.get("data"), dict)
    and row["data"].get("slice_id") == "issue-814-substitution-model-kani"
]
output.write_text(json.dumps({
    "root_journal_effects": journal_effect_fingerprint(root_dir / "action-journal.json"),
    "kani_journal": digest(root_dir / "kani/action-journal.json"),
    "kani_invocation": digest(repo / ".exo/agents/kani/invocation.json"),
    "kani_identity": digest(repo / ".exo/agents/kani/identity.json"),
    "leaf_attempts": leaf["attempts"],
    "leaf_status": leaf["status"],
    "leaf_recovery_phase": leaf.get("recovery", {}).get("phase"),
    "leaf_pr_number": leaf.get("pr_number"),
    "leaf_head_sha": leaf.get("head_sha"),
    "leaf_dispatch_agent_id": leaf.get("dispatch_agent_id"),
    "leaf_dispatch_invocation_id": leaf.get("dispatch_invocation_id"),
    "leaf_dispatch_intent_id": leaf.get("dispatch_intent_id"),
    "leaf_dispatch_generation": leaf.get("dispatch_generation"),
    "kani_dispatch_intent_id": root["slices"]["kani"].get("dispatch_intent_id"),
    "kani_dispatch_invocation_id": root["slices"]["kani"].get("dispatch_invocation_id"),
    "dispatch_event_ids": [row.get("event_id") for row in dispatch_events],
    "remote_mutations": remote_mutations(mock_log),
}, sort_keys=True) + "\n", encoding="utf-8")
PY
    REPEAT_BLOCKER_STATUS=0
    HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
        "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
        > "$AGG_DIR/init-blocker-repeat.log" 2>&1 || REPEAT_BLOCKER_STATUS=$?
    assert_init_status "$REPEAT_BLOCKER_STATUS" "$AGG_DIR/init-blocker-repeat.log" \
        "repeat init --continue with active #814 human gate"
    assert_kani_blocker_acknowledged_once "$KANI_BLOCKER_EVENT_ID" "$KANI_BLOCKER_RUN_SEQ"
    python3 "$SCRIPT_DIR/configured-owner-resume.py" verify-repeat \
        --repo "$AGG_REPO" --baseline "$AGG_DIR/configured-owner-resume.json" \
        > "$AGG_DIR/configured-owner-resume-repeat.stdout" \
        || { cat "$AGG_DIR/configured-owner-resume-repeat.stdout"; e2e_fail "repeat init duplicated the same-checkpoint Codex owner, PR or review"; }
    [[ "$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)" != "$ROOT_EPOCH_AFTER_BLOCKER" ]] \
        || e2e_fail "fresh repeat continuation did not create its own controller epoch"
    python3 - "$AGG_REPO" "$AGG_DIR/mock.log" "$BLOCKER_EFFECTS_BEFORE" <<'PY' \
        || e2e_fail "repeat blocker continuation duplicated work or rewrote historical effects"
import hashlib
import json
import sys
from pathlib import Path

repo, mock_log, before_path = map(Path, sys.argv[1:])
before = json.loads(before_path.read_text(encoding="utf-8"))
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
kani = json.loads((root_dir / "kani/run.json").read_text())
leaf = kani["slices"]["issue-814-substitution-model-kani"]
def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def remote_mutations(path: Path) -> dict[str, int]:
    counts = {"publications": 0, "merges": 0}
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_path = str(row.get("path", "")).split("?", 1)[0]
        if row.get("method") == "POST" and request_path.endswith("/api/v1/repos/owner/repo/pulls"):
            counts["publications"] += 1
        if row.get("method") == "POST" and "/api/v1/repos/owner/repo/pulls/" in request_path and request_path.endswith("/merge"):
            counts["merges"] += 1
    return counts
def journal_effect_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    entries = json.loads(path.read_text(encoding="utf-8"))
    stable_effects = [
        entry for entry in entries
        if not (
            entry.get("operation") == "emit_controller_event"
            and isinstance(entry.get("arguments"), dict)
            and entry["arguments"].get("event_type") in {
                "tl.phase_changed",
                "tl.recovery_boundary",
            }
        )
    ]
    encoded = json.dumps(stable_effects, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

ledger = [
    json.loads(line)
    for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
dispatch_events = [
    row for row in ledger
    if row.get("type") in {
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
    }
    and isinstance(row.get("data"), dict)
    and row["data"].get("slice_id") == "issue-814-substitution-model-kani"
]
after = {
    "root_journal_effects": journal_effect_fingerprint(root_dir / "action-journal.json"),
    "kani_journal": digest(root_dir / "kani/action-journal.json"),
    "kani_invocation": digest(repo / ".exo/agents/kani/invocation.json"),
    "kani_identity": digest(repo / ".exo/agents/kani/identity.json"),
    "leaf_attempts": leaf["attempts"],
    "leaf_status": leaf["status"],
    "leaf_recovery_phase": leaf.get("recovery", {}).get("phase"),
    "leaf_pr_number": leaf.get("pr_number"),
    "leaf_head_sha": leaf.get("head_sha"),
    "leaf_dispatch_agent_id": leaf.get("dispatch_agent_id"),
    "leaf_dispatch_invocation_id": leaf.get("dispatch_invocation_id"),
    "leaf_dispatch_intent_id": leaf.get("dispatch_intent_id"),
    "leaf_dispatch_generation": leaf.get("dispatch_generation"),
    "kani_dispatch_intent_id": root["slices"]["kani"].get("dispatch_intent_id"),
    "kani_dispatch_invocation_id": root["slices"]["kani"].get("dispatch_invocation_id"),
    "dispatch_event_ids": [row.get("event_id") for row in dispatch_events],
    "remote_mutations": remote_mutations(mock_log),
}
assert after == before, f"repeat continuation changed durable or external leaf effects: {before!r} -> {after!r}"
assert leaf["status"] != "blocked" and leaf.get("recovery") is None
assert leaf.get("dispatch_agent_id") and leaf.get("dispatch_invocation_id")
assert leaf.get("pr_number") and leaf.get("head_sha")
PY
    python3 - "$AGG_REPO" <<'PY' || e2e_fail "repeat blocker init released a later stage or duplicate worker"
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
kani = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text())
leaf = kani["slices"]["issue-814-substitution-model-kani"]
assert root["slices"]["kani"].get("dispatch_invocation_id")
assert leaf["status"] != "blocked" and leaf.get("recovery") is None
assert leaf.get("dispatch_agent_id") and leaf.get("dispatch_invocation_id")
assert leaf.get("pr_number") and leaf.get("head_sha")
PY
    ROOT_EPOCH_AFTER_FIRST_REPEAT="$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)"
    SECOND_REPEAT_STATUS=0
    HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
        "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
        > "$AGG_DIR/init-blocker-second-repeat.log" 2>&1 || SECOND_REPEAT_STATUS=$?
    assert_init_status "$SECOND_REPEAT_STATUS" "$AGG_DIR/init-blocker-second-repeat.log" \
        "second repeat init --continue with active #814 human gate"
    python3 "$SCRIPT_DIR/configured-owner-resume.py" verify-repeat \
        --repo "$AGG_REPO" --baseline "$AGG_DIR/configured-owner-resume.json" \
        > "$AGG_DIR/configured-owner-resume-second-repeat.stdout" \
        || { cat "$AGG_DIR/configured-owner-resume-second-repeat.stdout"; e2e_fail "second repeat init duplicated owner, PR, review, or guidance effects"; }
    [[ "$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)" != "$ROOT_EPOCH_AFTER_FIRST_REPEAT" ]] \
        || e2e_fail "second repeat continuation did not create a fresh controller epoch"
    echo "active blocker init-recovery E2E: passed"
    exit 0
fi
PULL_PUBLICATIONS_AFTER_START="$(count_pull_publications "$AGG_DIR/mock.log")"
(( PULL_PUBLICATIONS_AFTER_START <= PULL_PUBLICATIONS_BEFORE + 2 )) \
    || e2e_fail "first continuation published more than the recovered #814 and post-tidy leaf PRs"

POST_TIDY_STATE=""
KANI_BLOCKER_EVENT_ID=""
KANI_BLOCKER_RUN_SEQ=""
POST_TIDY_WAIT_SECONDS="${E2E_POST_TIDY_WAIT_SECONDS:-300}"
[[ "$POST_TIDY_WAIT_SECONDS" =~ ^[1-9][0-9]{0,2}$ ]] \
    || e2e_fail "E2E_POST_TIDY_WAIT_SECONDS must be an integer from 1 to 900"
(( POST_TIDY_WAIT_SECONDS <= 900 )) \
    || e2e_fail "E2E_POST_TIDY_WAIT_SECONDS must be an integer from 1 to 900"
POST_TIDY_POLL_COUNT=$((POST_TIDY_WAIT_SECONDS * 4))
e2e_log "Waiting up to ${POST_TIDY_WAIT_SECONDS}s for root advancement and the post-tidy leaf handoff"
for _ in $(seq 1 "$POST_TIDY_POLL_COUNT"); do
    POST_TIDY_STATE="$(python3 - "$AGG_REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root_dir = repo / ".exo/tl-loop/root"
try:
    root = json.loads((root_dir / "run.json").read_text())
except (OSError, json.JSONDecodeError):
    print("waiting root_state=unavailable")
    raise SystemExit(0)
handoff_path = root_dir / "handoff-proof/run.json"
try:
    child = json.loads(handoff_path.read_text())
except (OSError, json.JSONDecodeError):
    child = None
architecture = root["slices"].get("architecture", {})
kani = root["slices"].get("kani", {})
handoff = root["slices"].get("handoff-proof", {})
post = architecture.get("post_merge") or {}
leaf = child["slices"].get("post-tidy-leaf", {}) if child is not None else {}
phase = root.get("phase") or root.get("fsm", {}).get("phase", "unknown")
ready = (
    child is not None
    and phase in {"tl_running", "tl_waiting", "tl_done"}
    and root.get("current_order") == 3
    and architecture.get("status") == "merged"
    and post.get("phase") == "complete"
    and kani.get("status") == "merged"
    and handoff.get("status") in {"spawned", "in_review", "merged"}
    and bool(handoff.get("dispatch_intent_id"))
    and leaf.get("status") in {"in_review", "merged"}
    and bool(leaf.get("pr_number"))
    and leaf.get("agent_type") == "codex"
    and leaf.get("model") == "gpt-6-luna"
    and bool(leaf.get("reviewed_head"))
)
print(
    "ready" if ready else "waiting",
    phase,
    root.get("current_order", -1),
    architecture.get("status", "missing"),
    post.get("phase", "none"),
    kani.get("status", "missing"),
    handoff.get("status", "missing"),
    handoff.get("dispatch_intent_id") or "none",
    "present" if child is not None else "missing",
    leaf.get("status", "missing"),
    leaf.get("pr_number") or "none",
    leaf.get("agent_type") or "none",
    leaf.get("model") or "none",
    leaf.get("reviewed_head") or "none",
)
PY
    2>/dev/null)" || POST_TIDY_STATE=""
    [[ "$POST_TIDY_STATE" == ready\ * ]] && break
    sleep 0.25
done
read -r post_tidy_marker final_phase final_order architecture_status architecture_post kani_status handoff_status handoff_intent \
    handoff_child leaf_status leaf_pr leaf_agent_type leaf_model leaf_head <<< "$POST_TIDY_STATE"
if [[ "$post_tidy_marker" != "ready" || ( "$final_phase" != "tl_running" && "$final_phase" != "tl_waiting" && "$final_phase" != "tl_done" ) \
    || "$final_order" != "3" \
    || "$architecture_status" != "merged" || "$architecture_post" != "complete" \
    || "$kani_status" != "merged" \
    || ( "$handoff_status" != "spawned" && "$handoff_status" != "in_review" && "$handoff_status" != "merged" ) \
    || "$handoff_intent" == "none" || ( "$leaf_status" != "in_review" && "$leaf_status" != "merged" ) \
    || "$leaf_pr" == "none" || "$leaf_agent_type" != "codex" \
    || "$leaf_model" != "gpt-6-luna" || "$leaf_head" == "none" ]]; then
    cat "$AGG_DIR/init.log"
    cat "$AGG_REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    e2e_fail "continuation did not reach the post-tidy leaf review handoff: '$POST_TIDY_STATE'"
fi
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$((PULL_PUBLICATIONS_BEFORE + 2))" ]] \
    || e2e_fail "expected one recovered #814 publication and one post-tidy leaf publication"
KANI_LEAF_PR="$(python3 - "$AGG_REPO" "$REJECTED_KANI_SPAWN_SHA256" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
expected_rejected_spawn = sys.argv[2]
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
child = json.loads((root_dir / "kani/run.json").read_text())
leaf = child["slices"]["issue-814-substitution-model-kani"]
assert root["slices"]["kani"]["status"] == "merged"
assert leaf["status"] == "merged", "recovered #814 leaf did not complete integration"
assert leaf["attempts"] == 2, "recovery did not create exactly one new dispatch attempt"
assert leaf["pr_number"] is not None
assert leaf["agent_type"] == "codex"
assert leaf["model"] == "gpt-6-luna"
assert leaf["resolved_harness"] == "codex/gpt-6-luna"
assert leaf["requested_harness"] == "opencode/opencode-go/deepseek-v4.1-flash"
owner_id = leaf["dispatch_agent_id"]
owner_events = [
    json.loads(line)
    for line in (repo / ".exo/events" / f"{owner_id}.jsonl").read_text().splitlines()
]
owner_invocations = [
    event["data"]
    for event in owner_events
    if event.get("type") == "agent.invocation.started"
    and event.get("data", {}).get("invocation_id") == leaf["dispatch_invocation_id"]
]
assert len(owner_invocations) == 1, "configured worker invocation is not retained in lifecycle history"
owner_invocation = owner_invocations[0]
assert owner_invocation["runtime_agent_id"] == owner_id
assert owner_invocation["slice_id"] == leaf["id"]
assert owner_invocation["branch"] == leaf["branch"]
assert owner_invocation["runtime"] == "codex" and owner_invocation["provider"] == "codex"
owner_worktree = Path(owner_invocation["worktree"])
assert owner_worktree.is_absolute()
assert (repo / ".exo/worktrees") in owner_worktree.parents
assert not any(gate["name"] == "tl-dispatch-failed-issue-814-substitution-model-kani" for gate in child["gates"])
assert not any(gate["name"] == "tl-ordered-child-recovery-kani" for gate in root["gates"])
journal = json.loads((root_dir / "kani/action-journal.json").read_text())
spawns = [
    entry for entry in journal
    if entry.get("operation") == "spawn_leaf"
    and entry.get("target") == "issue-814-substitution-model-kani"
]
assert sum(entry.get("status") == "rejected" for entry in spawns) == 1
assert sum(entry.get("status") == "confirmed" for entry in spawns) == 1
rejected = next(entry for entry in spawns if entry.get("status") == "rejected")
accepted = next(entry for entry in spawns if entry.get("status") == "confirmed")
assert hashlib.sha256(
    json.dumps(rejected, sort_keys=True, separators=(",", ":")).encode()
).hexdigest() == expected_rejected_spawn, "original rejected spawn evidence changed"
assert rejected["arguments"]["intent_id"] != accepted["arguments"]["intent_id"]
assert accepted["arguments"]["intent_id"] == leaf["dispatch_intent_id"]
assert leaf["dispatch_invocation_id"]
events = [
    json.loads(line)
    for line in (repo.parent / "agent-events.jsonl").read_text().splitlines()
]
worker_starts = [event for event in events if event.get("event") == "recovery_worker_started"]
worker_prs = [event for event in events if event.get("event") == "recovery_worker_filed_pr"]
assert len(worker_starts) == 1, "the proven rejected attempt did not launch exactly one worker"
assert len(worker_prs) == 1, "the recovered worker did not publish exactly once"
worker = worker_starts[0]
assert worker["source_clean"] is True
assert worker["model"].split("#", 1)[0] == "gpt-6-luna"
assert worker["effort"] == "xhigh"
assert worker["source_branch"].startswith("main.kani."), worker["source_branch"]
reviewers = [
    event for event in events
    if event.get("event") == "reviewer_approved"
    and event.get("pr_number") == leaf["pr_number"]
]
assert len(reviewers) == 1
assert reviewers[0]["model"] == "gpt-6.1-sol" and reviewers[0]["effort"] == "medium"
# Kani was already provisioned before this continuation, so this leaf retry
# does not create a new parent-to-subTL handoff. Its source must instead match
# the authoritative aggregate synchronization receipt retained by the parent.
aggregate_sync_head = root["slices"]["architecture"]["post_merge"]["evidence"]["parent_commit_sha"]
assert worker["source_head"] == aggregate_sync_head, (
    "Kani retry did not use the synchronized aggregate commit"
)
print(leaf["pr_number"])
PY
)"
POST_TIDY_LEAF_PR="$leaf_pr"
assert_configured_retry_dispatch_events "$AGG_REPO"
read -r KANI_CONTROLLER_INVOCATION_ID KANI_CONTROLLER_GENERATION < <(python3 - "$AGG_REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
agent_dir = repo / ".exo/agents/kani"
invocation = json.loads((agent_dir / "invocation.json").read_text(encoding="utf-8"))
identity = json.loads((agent_dir / "identity.json").read_text(encoding="utf-8"))
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8"))
old_id = json.loads((repo / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8"))[
    "slices"
]["kani"]["dispatch_invocation_id"]
assert invocation["trigger"] == "controller_restart"
assert invocation["generation"] == 2
assert invocation["prior_invocation_id"] == "seed-kani-dormant-controller-invocation"
assert invocation["authorization_source"] == "controller"
assert invocation["runtime_agent_id"] == "kani"
assert invocation["branch"] == "main.kani" and invocation["worktree"] == identity["working_dir"]
assert invocation.get("pr_number") is None and invocation.get("head_sha") is None
assert invocation["routing"]["window_id"] != "@999999"
assert old_id == invocation["invocation_id"]
assert root["slices"]["kani"]["dispatch_invocation_id"] == invocation["invocation_id"]
assert identity["working_dir"] == str(repo / ".exo/worktrees/kani")
assert Path(identity["working_dir"]).is_dir()
events = [
    json.loads(line)
    for line in (repo / ".exo/events/kani.jsonl").read_text(encoding="utf-8").splitlines()
]
restarts = [
    event for event in events
    if event.get("type") == "agent.invocation.started"
    and event.get("data", {}).get("trigger") == "controller_restart"
]
assert len(restarts) == 1
print(invocation["invocation_id"], invocation["generation"])
PY
)
python3 - "$AGG_REPO/.exo/agents/kani/identity.json" "$KANI_IDENTITY_BASELINE" <<'PY' \
    || e2e_fail "controller restart changed the accepted Kani identity"
import json
import sys
from pathlib import Path

current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
accepted = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert current == accepted, "accepted Kani identity coordinates changed"
PY

curl -fsS "$AGG_MOCK_URL/api/v1/repos/owner/repo/pulls/$POST_TIDY_LEAF_PR" \
    > "$AGG_DIR/post-tidy-leaf-pr.json"
for _ in $(seq 1 120); do
    curl -fsS "$AGG_MOCK_URL/api/v1/repos/owner/repo/pulls/$POST_TIDY_LEAF_PR/reviews" \
        > "$AGG_DIR/post-tidy-leaf-reviews.json" 2>/dev/null || true
    REVIEW_COUNT="$(python3 - "$AGG_DIR/post-tidy-leaf-reviews.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    reviews = json.loads(Path(sys.argv[1]).read_text())
except (OSError, json.JSONDecodeError):
    reviews = []
print(len(reviews) if isinstance(reviews, list) else 0)
PY
)"
    if [[ "$REVIEW_COUNT" == "1" ]] && python3 - "$AGENT_EVENT_LOG" "$POST_TIDY_LEAF_PR" <<'PY'
import json
import sys
from pathlib import Path

events = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines()]
pr_number = int(sys.argv[2])
raise SystemExit(not any(
    event.get("event") == "reviewer_approved"
    and event.get("pr_number") == pr_number
    for event in events
))
PY
    then
        break
    fi
    sleep 0.25
done
curl -fsS "$AGG_MOCK_URL/api/v1/repos/owner/repo/pulls/$POST_TIDY_LEAF_PR/reviews" \
    > "$AGG_DIR/post-tidy-leaf-reviews.json"
python3 - "$AGG_DIR/post-tidy-leaf-pr.json" "$leaf_head" <<'PY'
import json
import sys
from pathlib import Path

pr = json.loads(Path(sys.argv[1]).read_text())
assert pr["base"]["ref"] == "main.handoff-proof", "leaf PR targeted the wrong parent branch"
assert pr["head"]["ref"].startswith("main.handoff-proof."), "leaf PR branch is outside its subTL"
assert isinstance(pr["merged"], bool), "mock PR omitted its merge state"
assert pr["head"]["sha"] == sys.argv[2], "published PR head differs from the worker commit"
PY
LEAF_MERGE_REQUEST_COUNT="$(rg -c "pulls/$POST_TIDY_LEAF_PR/merge" "$AGG_DIR/mock.log" || true)"
[[ "$LEAF_MERGE_REQUEST_COUNT" -le 1 ]] || e2e_fail "leaf PR was merged more than once"
python3 - "$AGG_DIR/post-tidy-leaf-reviews.json" "$leaf_head" <<'PY'
import json
import sys
from pathlib import Path

reviews = json.loads(Path(sys.argv[1]).read_text())
assert len(reviews) == 1, f"expected exactly one authenticated reviewer result, found {len(reviews)}"
assert reviews[0]["state"] == "APPROVED"
assert reviews[0]["commit_id"] in {None, sys.argv[2]}, "review record contradicts the exact worker head"
PY

[[ -f "$ROOT_DIRTY_MARKER" && -f "$ROOT_ONLY_DIRTY_FILE" ]] \
    || e2e_fail "post-checkout guard fixture did not dirty only the outer root after tidy"
python3 - "$AGG_REPO" "$HANDOFF_WORKTREE" "$HANDOFF_HEAD_FILE" "$AGENT_EVENT_LOG" "$POST_TIDY_LEAF_PR" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
source_worktree = Path(sys.argv[2])
handoff_head = Path(sys.argv[3]).read_text().strip()
events = [json.loads(line) for line in Path(sys.argv[4]).read_text().splitlines()]
leaf_pr_number = int(sys.argv[5])
worker_starts = [entry for entry in events if entry["event"] == "worker_started"]
worker_prs = [entry for entry in events if entry["event"] == "worker_filed_pr"]
reviews = [entry for entry in events if entry["event"] == "reviewer_approved"]
assert len(worker_starts) == 1, f"expected one post-tidy worker process, found {len(worker_starts)}"
assert len(worker_prs) == 1, f"expected one post-tidy worker file_pr call, found {len(worker_prs)}"
handoff_workers = [entry for entry in worker_starts if entry.get("source_worktree") == str(source_worktree)]
worker = handoff_workers[0]
handoff_reviews = [entry for entry in reviews if entry.get("pr_number") == leaf_pr_number]
assert len(handoff_reviews) == 1, f"expected one routed post-tidy reviewer approval, found {len(handoff_reviews)}"
review = handoff_reviews[0]
assert worker["role"] == "dev" and worker["model"] == "gpt-6-luna"
assert worker["effort"] == "xhigh"
assert worker["source_worktree"] == str(source_worktree)
assert worker["source_branch"] == "main.handoff-proof"
assert worker["source_head"] == handoff_head
assert worker["source_clean"] is True and worker["outer_root_dirty"] is True
assert review["role"] == "reviewer" and review["model"] == "gpt-6.1-sol"
assert review["effort"] == "medium"
assert review["pr_number"] == leaf_pr_number
pr = json.loads((Path(sys.argv[4]).parent / "post-tidy-leaf-pr.json").read_text())
assert review["head_sha"] == pr["head"]["sha"], "review was not bound to the actual PR head"

child = json.loads((repo / ".exo/tl-loop/root/handoff-proof/run.json").read_text())
leaf = child["slices"]["post-tidy-leaf"]
assert leaf["status"] in {"in_review", "merged"}
assert leaf["agent_type"] == "codex" and leaf["model"] == "gpt-6-luna"
leaf_agent = leaf["dispatch_agent_id"]
leaf_identity = json.loads((repo / ".exo/agents" / leaf_agent / "identity.json").read_text())
assert leaf_identity["agent_type"] == "codex"
leaf_worktree = Path(leaf_identity["working_dir"])
leaf_head = subprocess.run(
    ["git", "-C", str(leaf_worktree), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
leaf_parent = subprocess.run(
    ["git", "-C", str(leaf_worktree), "rev-parse", "HEAD^"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert leaf_parent == handoff_head, "worker commit was not based on the committed handoff revision"
assert subprocess.run(
    ["git", "-C", str(source_worktree), "merge-base", "--is-ancestor", handoff_head, "HEAD"],
    check=False,
).returncode == 0, "subTL source no longer contains the committed parent handoff"
journal = json.loads((repo / ".exo/tl-loop/root/handoff-proof/action-journal.json").read_text())
for operation in ("spawn_leaf", "spawn_reviewer"):
    matching = [
        entry for entry in journal
        if entry.get("operation") == operation and entry.get("target") == "post-tidy-leaf"
    ]
    assert len(matching) == 1, f"expected one {operation} action, found {len(matching)}"
assert not any(
    entry.get("operation") == "merge_pr" and entry.get("target") == "post-tidy-leaf"
    for entry in journal
), "spawn acceptance must not bypass ordinary leaf integration"
assert sum(
    entry.get("operation") == "merge_pr" and entry.get("target") == "post-tidy-leaf"
    for entry in journal
) <= 1
assert review["agent_id"] == f"review-pr-{leaf_pr_number}-codex"
print(json.dumps({
    "leaf_agent": leaf_agent,
    "reviewer_agent": review["agent_id"],
    "leaf_pr": leaf_pr_number,
    "source_head": handoff_head,
    "worker_commit": leaf_head,
}))
PY

# The dirty root file is a test-only adversarial probe. It proved the nested
# spawn succeeded while the global root was dirty; remove only this known file
# before repeat continuation checks that the original commit-all handoff remains stable.
rm -- "$ROOT_ONLY_DIRTY_FILE"

python3 - "$AGG_REPO" "$AGGREGATE_PR" "$LEAF_PR" "$LEAF_RECEIPT_SHA256" "$AGGREGATE_REMOTE_HEAD" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
aggregate_pr = int(sys.argv[2])
leaf_pr = int(sys.argv[3])
expected_receipt = sys.argv[4]
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
child = json.loads((root_dir / "architecture/run.json").read_text())
leaf_evidence = child["slices"]["issue-811"]["post_merge"]["evidence"]
receipt_hash = hashlib.sha256(
    json.dumps(leaf_evidence, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert receipt_hash == expected_receipt, "completed issue #811 receipt changed during recovery"
assert child["fsm"]["phase"] == "tl_pr_filed", "architecture handoff phase was changed"
assert leaf_evidence["pr_number"] == str(leaf_pr)
assert leaf_evidence["issue_id"] == "811"
assert leaf_evidence["issue_close_outcome"] == "closed"
assert not any(
    identity.get("slice_id") == "issue-811"
    for identity_path in (repo / ".exo/agents").glob("*/identity.json")
    for identity in [json.loads(identity_path.read_text())]
), "completed issue #811 was dispatched as a new worker"
assert not any(gate["name"] == "tl-ordered-child-recovery-ambiguous" for gate in root["gates"])
candidate = root["integration"]["candidates"]["architecture"]
assert candidate["lifecycle"] == "MERGED"
assert candidate["aggregate_pr_number"] == aggregate_pr
assert candidate["active_aggregate_approval_id"]
assert len(candidate["aggregate_approval_receipts"]) == 1
architecture = root["slices"]["architecture"]
post_merge = architecture["post_merge"]
assert post_merge["phase"] == "complete"
assert post_merge["evidence"]["changelog_decision"] == "captured"
assert post_merge["evidence"]["changelog_outcome"] == "captured"
assert post_merge["evidence"]["parent_commit_sha"] == sys.argv[5]
lane = root["integration"]["lanes"][f'{root["repository_identity"]["remote_url"]}:main']
assert lane["phase"] == "idle" and lane["child_id"] is None
assert lane["last_lane_epoch"] == 2
journal = json.loads((root_dir / "action-journal.json").read_text())
handoff_node = next(
    node for node in root["plan_manifest"]["nodes"]
    if node.get("kind") == "sub_tl" and node.get("name") == "handoff-proof"
)
aggregate_merges = [
    entry
    for entry in journal
    if entry.get("operation") == "merge_pr" and entry.get("target") == "architecture"
]
assert sum(entry.get("status") == "rejected" for entry in aggregate_merges) == 1
assert sum(entry.get("status") == "confirmed" for entry in aggregate_merges) == 1
approved_merge = next(
    entry
    for entry in aggregate_merges
    if entry.get("status") == "confirmed"
)
assert (
    approved_merge["result"]["result"].get("aggregate_approval_receipt_id")
    == candidate["active_aggregate_approval_id"]
), "the durable merge result is not bound to the retained parent approval"
sync_entries = [
    entry
    for entry in journal
    if entry.get("operation") == "post_merge_parent_sync" and entry.get("target") == "architecture"
]
prior_refusals = [
    entry
    for entry in sync_entries
    if isinstance(entry.get("result"), dict)
    and isinstance(entry["result"].get("result"), dict)
    and entry["result"]["result"].get("sync_outcome") == "pre_mutation_refusal"
]
confirmed_syncs = [
    entry
    for entry in sync_entries
    if isinstance(entry.get("result"), dict)
    and isinstance(entry["result"].get("result"), dict)
    and entry["result"]["result"].get("sync_outcome") == "confirmed"
]
assert len(prior_refusals) == 1, "the immutable prior sync refusal was not retained"
assert len(confirmed_syncs) == 1, "parent synchronization was not confirmed exactly once"
assert confirmed_syncs[0]["arguments"].get("sync_attempt") == 2
assert confirmed_syncs[0]["result"]["result"].get("remote_head_sha") == sys.argv[5]
kani_dispatches = [
    entry
    for entry in journal
    if entry.get("operation") == "emit_controller_event"
    and entry.get("target") == "kani"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("event_type") == "tl.dispatch_confirmed"
    and isinstance(entry["arguments"].get("payload"), dict)
    and entry["arguments"]["payload"].get("boundary") == "sub_tl_started"
]
assert len(kani_dispatches) == 1, f"expected one Kani stage dispatch, found {len(kani_dispatches)}"
handoffs = [
    entry for entry in journal
    if entry.get("operation") == "prepare_scope_handoff"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("child_scope_id") == handoff_node["node_id"]
]
assert handoffs, "expected an audited commit-all handoff"
assert all(entry.get("status") == "confirmed" for entry in handoffs)
handoff_results = [entry.get("result", {}).get("result", {}) for entry in handoffs]
committed_handoffs = [result for result in handoff_results if result.get("outcome") == "committed"]
assert len(committed_handoffs) == 1, (
    f"expected one committed handoff and only no-op replays, got {handoff_results!r}"
)
handoff_result = committed_handoffs[0]
assert handoff_result.get("ready") is True
assert handoff_result.get("head_after") != sys.argv[5]
for replay in handoff_results:
    assert replay.get("ready") is True
    assert replay.get("head_after") == handoff_result.get("head_after")
    assert replay.get("outcome") in {"committed", "no_changes"}
    if replay.get("outcome") == "no_changes":
        assert replay.get("head_before") == replay.get("head_after")
        assert replay.get("commit_sha") == ""
        assert replay.get("committed_paths") == []
handoff_dispatches = [
    entry for entry in journal
    if entry.get("operation") == "emit_controller_event"
    and entry.get("target") == "handoff-proof"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("event_type") == "tl.dispatch_confirmed"
    and isinstance(entry["arguments"].get("payload"), dict)
    and entry["arguments"]["payload"].get("boundary") == "sub_tl_started"
]
assert len(handoff_dispatches) == 1, (
    f"expected one post-handoff subTL dispatch, found {len(handoff_dispatches)}"
)
assert journal.index(handoffs[0]) < journal.index(handoff_dispatches[0]), (
    "subTL dispatch was journaled before its commit-all handoff"
)
assert not any(
    entry.get("operation") == "spawn_reviewer" and entry.get("target") == "architecture"
    for entry in journal
), "aggregate recovery started a root reviewer"
PY

HANDOFF_COMMIT_REPORT="$(assert_scope_handoff_commit)"
printf '%s\n' "$HANDOFF_COMMIT_REPORT" > "$AGG_DIR/scope-handoff-commit.json"
HANDOFF_COMMIT_SHA="$(git -C "$AGG_REPO" rev-parse HEAD)"
[[ "$(git -C "$AGG_REPO" rev-parse main)" == "$HANDOFF_COMMIT_SHA" ]] \
    || e2e_fail "parent branch does not point at its audited handoff commit"
[[ "$(git -C "$AGG_REPO" rev-parse refs/remotes/origin/main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "handoff commit unexpectedly pushed or changed the authoritative remote merge head"
HANDOFF_WORKTREE="$AGG_REPO/.exo/worktrees/handoff-proof"
[[ -d "$HANDOFF_WORKTREE" ]] \
    || e2e_fail "post-handoff child worktree was not provisioned"
git -C "$HANDOFF_WORKTREE" merge-base --is-ancestor "$HANDOFF_COMMIT_SHA" HEAD \
    || e2e_fail "post-handoff child no longer contains the committed parent revision"
[[ "$(git -C "$HANDOFF_WORKTREE" branch --show-current)" == "main.handoff-proof" ]] \
    || e2e_fail "post-handoff child was provisioned on an unexpected branch"
python3 - "$AGG_REPO" "$HANDOFF_COMMIT_SHA" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
identity = json.loads((repo / ".exo/agents/handoff-proof/identity.json").read_text())
invocation = json.loads((repo / ".exo/agents/handoff-proof/invocation.json").read_text())
assert identity["slice_id"] == "handoff-proof"
assert identity["parent_branch"] == "main"
assert identity["birth_branch"] == "main.handoff-proof"
assert identity["working_dir"] == str(repo / ".exo/worktrees/handoff-proof")
assert invocation["slice_id"] == "handoff-proof"
assert invocation["branch"] == "main.handoff-proof"
assert invocation["worktree"] == identity["working_dir"]
child = json.loads((repo / ".exo/tl-loop/root/handoff-proof/run.json").read_text())
assert child["run_id"] == "handoff-proof"
assert child["parent_run_id"] == "root"
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
journal = json.loads((repo / ".exo/tl-loop/root/action-journal.json").read_text())
handoff_node = next(
    node for node in root["plan_manifest"]["nodes"]
    if node.get("kind") == "sub_tl" and node.get("name") == "handoff-proof"
)
handoff_entries = [
    entry for entry in journal
    if entry.get("operation") == "prepare_scope_handoff"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("child_scope_id") == handoff_node["node_id"]
]
assert handoff_entries
committed = [
    entry for entry in handoff_entries
    if entry.get("result", {}).get("result", {}).get("outcome") == "committed"
]
assert len(committed) == 1
assert committed[0]["result"]["result"]["head_after"] == sys.argv[2]
assert all(
    entry.get("status") == "confirmed"
    and entry.get("result", {}).get("result", {}).get("head_after") == sys.argv[2]
    and entry.get("result", {}).get("result", {}).get("outcome") in {"committed", "no_changes"}
    for entry in handoff_entries
)
print(json.dumps({"child_run": child["run_id"], "handoff_commit": sys.argv[2]}))
PY
assert_legacy_recovery_receipt "$AGG_REPO" "$LEGACY_LOCK_CONTENTS" "$LEGACY_FINGERPRINT"

[[ "$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)" == "$AGGREGATE_MERGE_REQUESTS_BEFORE" ]] \
    || e2e_fail "continuation repeated the aggregate merge instead of resuming local synchronization"
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$((PULL_PUBLICATIONS_BEFORE + 2))" ]] \
    || e2e_fail "continuation published a replacement aggregate PR or duplicated the leaf publication"
[[ "$(rg -c "pulls/$KANI_LEAF_PR/merge" "$AGG_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "recovered #814 leaf PR was not merged exactly once"
[[ "$(rg -c "pulls/$LEAF_PR/merge" "$AGG_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "completed leaf PR was merged again during aggregate recovery"
[[ "$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_REPO/.exo/agents/architecture/invocation.json")" == "$ARCHITECTURE_INVOCATION_SHA256" ]] \
    || e2e_fail "completed architecture producer invocation changed during aggregate recovery"
RECOVERED_CONTROLLER_EPOCH="$(cat "$CONTROLLER_EPOCH_PATH" 2>/dev/null || true)"
[[ -n "$RECOVERED_CONTROLLER_EPOCH" ]] \
    || e2e_fail "completed aggregate continuation did not retain its controller epoch"
[[ "$RECOVERED_CONTROLLER_EPOCH" != "$PRE_RECOVERY_CONTROLLER_EPOCH" ]] \
    || e2e_fail "init --continue did not launch a fresh controller for the recovered root run"
AGENT_EVENT_SHA256_BEFORE_REPEAT="$(sha256sum "$AGENT_EVENT_LOG" | cut -d ' ' -f 1)"
POST_TIDY_DISPATCH_STATE="$(python3 - "$AGG_REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
child = json.loads((repo / ".exo/tl-loop/root/handoff-proof/run.json").read_text())
leaf = child["slices"]["post-tidy-leaf"]
print(" ".join(str(value or "none") for value in (
    root["slices"]["handoff-proof"]["dispatch_intent_id"],
    leaf["attempts"],
    leaf["dispatch_intent_id"],
    leaf["dispatch_invocation_id"],
    leaf["pr_number"],
    leaf["reviewed_head"],
)))
PY
)"

REPEAT_INIT_STATUS=0
HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
    "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
    > "$AGG_DIR/init-repeat.log" 2>&1 || REPEAT_INIT_STATUS=$?
assert_init_status "$REPEAT_INIT_STATUS" "$AGG_DIR/init-repeat.log" "repeated aggregate init --continue"
assert_configured_retry_dispatch_events "$AGG_REPO"
rg -Fq "server_build=\"$AGGREGATE_BUILD_ID\" controller_build=\"$AGGREGATE_BUILD_ID\"" "$AGG_DIR/init-repeat.log" \
    || { cat "$AGG_DIR/init-repeat.log"; e2e_fail "repeat init did not attach using the verified binary/archive"; }
assert_one_live_window Server
assert_one_live_window Watcher
[[ "$(python3 - "$AGG_REPO" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads((Path(sys.argv[1]) / ".exo/agents/kani/invocation.json").read_text())
print(record["invocation_id"], record["generation"])
PY
)" == "$KANI_CONTROLLER_INVOCATION_ID $KANI_CONTROLLER_GENERATION" ]] \
    || e2e_fail "repeat init created another Kani controller invocation"
python3 - "$AGG_REPO/.exo/events/kani.jsonl" <<'PY' \
    || e2e_fail "repeat init duplicated the Kani controller restart lifecycle event"
import json
import sys
from pathlib import Path

events = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines()]
restarts = [
    event for event in events
    if event.get("type") == "agent.invocation.started"
    and event.get("data", {}).get("trigger") == "controller_restart"
]
assert len(restarts) == 1, f"expected one restart lifecycle event, found {len(restarts)}"
PY
TL_PANE_COUNT="$(aggregate_tl_panes | awk 'NF { count += 1 } END { print count + 0 }')"
[[ "$TL_PANE_COUNT" -le 1 ]] || e2e_fail "repeat init created duplicate TL panes ($TL_PANE_COUNT found)"
REPEAT_POST_TIDY_DISPATCH_STATE="$(python3 - "$AGG_REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
child = json.loads((repo / ".exo/tl-loop/root/handoff-proof/run.json").read_text())
leaf = child["slices"]["post-tidy-leaf"]
print(" ".join(str(value or "none") for value in (
    root["slices"]["handoff-proof"]["dispatch_intent_id"],
    leaf["attempts"],
    leaf["dispatch_intent_id"],
    leaf["dispatch_invocation_id"],
    leaf["pr_number"],
    leaf["reviewed_head"],
)))
PY
)"
[[ "$REPEAT_POST_TIDY_DISPATCH_STATE" == "$POST_TIDY_DISPATCH_STATE" ]] \
    || e2e_fail "repeat init changed the post-tidy leaf's dispatch or PR identity"
[[ "$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)" == "$AGGREGATE_MERGE_REQUESTS_BEFORE" ]] \
    || e2e_fail "repeated continuation duplicated the aggregate merge"
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$((PULL_PUBLICATIONS_BEFORE + 2))" ]] \
    || e2e_fail "repeat init published a replacement aggregate PR or duplicated the leaf publication"
[[ "$(rg -c "pulls/$KANI_LEAF_PR/merge" "$AGG_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "repeat init duplicated the recovered #814 merge"
[[ "$(rg -F -c 'Recovered a proven orphan native Git lock' "$AGG_DIR/init-repeat.log" || printf '0')" == "0" ]] \
    || e2e_fail "repeated init reported an already-completed lock recovery again"
[[ "$(git -C "$AGG_REPO" rev-parse HEAD)" == "$HANDOFF_COMMIT_SHA" ]] \
    || e2e_fail "repeat continuation created or selected another handoff commit"
[[ "$(git -C "$AGG_REPO" log --format=%H --grep='^Prepare parent worktree for ordered subTL handoff$' | wc -l | tr -d ' ')" == "1" ]] \
    || e2e_fail "repeat continuation duplicated the audited handoff commit"
[[ "$(git -C "$AGG_REPO" status --porcelain --untracked-files=all)" == "" ]] \
    || e2e_fail "repeat continuation changed committed parent worktree content"
[[ "$(assert_scope_handoff_commit)" == "$HANDOFF_COMMIT_REPORT" ]] \
    || e2e_fail "repeat continuation changed the handoff commit or excluded ignored runtime content"
[[ "$(sha256sum "$AGENT_EVENT_LOG" | cut -d ' ' -f 1)" == "$AGENT_EVENT_SHA256_BEFORE_REPEAT" ]] \
    || e2e_fail "repeat continuation started another worker or reviewer process"
[[ "$(git -C "$AGG_REPO" log --format=%s --all --grep='^feat: add post-tidy leaf fixture$' | wc -l | tr -d ' ')" == "1" ]] \
    || e2e_fail "repeat continuation duplicated the post-tidy leaf commit"
python3 - "$AGG_REPO" "$handoff_intent" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
journal = json.loads((root_dir / "action-journal.json").read_text())
proof = root["slices"]["handoff-proof"]
child = json.loads((root_dir / "handoff-proof/run.json").read_text())
leaf = child["slices"]["post-tidy-leaf"]
assert proof["dispatch_intent_id"] == sys.argv[2]
handoff_scope_id = next(
    node["node_id"] for node in root["plan_manifest"]["nodes"]
    if node.get("kind") == "sub_tl" and node.get("name") == "handoff-proof"
)
handoff_actions = [
    entry for entry in journal
    if entry.get("operation") == "prepare_scope_handoff"
    and entry.get("arguments", {}).get("child_scope_id") == handoff_scope_id
]
assert sum(
    entry.get("operation") == "prepare_scope_handoff"
    and entry.get("result", {}).get("result", {}).get("outcome") == "committed"
    for entry in handoff_actions
) == 1
assert all(
    entry.get("status") == "confirmed"
    and entry.get("result", {}).get("result", {}).get("outcome") in {"committed", "no_changes"}
    for entry in handoff_actions
)
assert sum(
    entry.get("operation") == "emit_controller_event"
    and entry.get("target") == "handoff-proof"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("event_type") == "tl.dispatch_confirmed"
    and isinstance(entry["arguments"].get("payload"), dict)
    and entry["arguments"]["payload"].get("boundary") == "sub_tl_started"
    for entry in journal
) == 1
child_journal = json.loads((root_dir / "handoff-proof/action-journal.json").read_text())
for operation in ("spawn_leaf", "spawn_reviewer"):
    assert sum(
        entry.get("operation") == operation and entry.get("target") == "post-tidy-leaf"
        for entry in child_journal
    ) == 1, f"repeat init duplicated {operation}"
assert leaf["status"] in {"in_review", "merged"} and leaf["pr_number"] is not None
PY
assert_legacy_recovery_receipt "$AGG_REPO" "$LEGACY_LOCK_CONTENTS" "$LEGACY_FINGERPRINT"
e2e_phase "Configured resume" "Proving a pre-existing OpenCode #814 owner resumes through the production blocked-leaf tool..."
if ! python3 "$PROJECT_ROOT/tests/e2e/task-blocked-human-gate/run.py" \
    > "$AGG_DIR/configured-resume-blocked-leaf.json" \
    2> "$AGG_DIR/configured-resume-blocked-leaf.stderr"; then
    cat "$AGG_DIR/configured-resume-blocked-leaf.stderr" >&2
    cat "$AGG_DIR/configured-resume-blocked-leaf.json" >&2
    e2e_fail "real pre-existing OpenCode owner did not resume through resume_blocked_leaf"
fi
python3 - "$AGG_DIR/configured-resume-blocked-leaf.json" <<'PY'
import json
import sys
from pathlib import Path

runs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["runs"]
assert len(runs) == 3, f"expected three isolated resume runs, got {len(runs)}"
for run in runs:
    observed = run["observed"]
    assert observed["same_owner"] and observed["same_branch"] and observed["same_worktree"]
    assert observed["resumed_harness"] == "codex"
    assert observed["resumed_model"] == "gpt-6-luna"
    assert observed["resumed_effort"] == "xhigh"
    assert observed["reviewer_harness"] == "codex"
    assert observed["reviewer_model"] == "gpt-6.1-sol"
    assert observed["reviewer_effort"] == "medium"
    assert observed["repeat_resume_adopted"]
    assert observed["pr_published_after_resume"]
    assert run["negative_controls"]["stale_resume_rejected"]
print("pre-existing OpenCode owner resumed once as Codex; exact owner and reviewer routes verified")
PY
e2e_log "PASS: actual init --continue recovered the merged aggregate, commit-all tidied the parent before a real worker/reviewer route, preserved ignored runtime data and completed receipts, and repeat init added no duplicate commit or dispatch (handoff=$HANDOFF_COMMIT_SHA, leaf_pr=$POST_TIDY_LEAF_PR, archive=$AGGREGATE_ARCHIVE_SHA256, host=$EXOMONAD_BIN_SHA256, devswarm=$GUEST_WASM_SHA256, e2e-test=$E2E_WASM_SHA256, build=$AGGREGATE_BUILD_ID)"

echo ""
echo "============================================"
if [[ "${E2E_AGGREGATE_ONLY:-0}" == "1" ]]; then
    echo "  aggregate init-recovery E2E: passed"
else
    echo "  init-recovery E2E: all phases passed"
fi
echo "============================================"
