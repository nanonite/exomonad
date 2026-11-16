"""Seed and verify an OpenCode-to-Codex blocked-owner transition in Phase 8."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient

OWNER_SLICE = "issue-814-substitution-model-kani"
BLOCKED_ISSUE_ID = 814


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worktree_fingerprint(worktree: Path) -> str:
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return "sha256:" + hashlib.sha256(status).hexdigest()


def advance_seeded_ledger_sequence(
    repository: Path, rows: list[dict[str, Any]]
) -> None:
    """Keep the append allocator beyond the synthetic historical rows."""
    ledger = repository / ".exo/ledger"
    sequence_path = ledger / "run_seq"
    lock_path = ledger / "run_seq.lock"
    highest_seeded = max(int(row["run_seq"]) for row in rows)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current = int(sequence_path.read_text(encoding="utf-8").strip() or "0")
        if current < highest_seeded:
            temporary = ledger / f".run-seq-fixture-{uuid4().hex}.tmp"
            temporary.write_text(f"{highest_seeded}\n", encoding="utf-8")
            os.replace(temporary, sequence_path)


# The non-optional, non-defaulted fields of the Rust `LedgerEvent` envelope in
# rust/exomonad-core/src/services/immutable_ledger.rs. The fixture-schema check
# derives the same set from the Rust source and fails when they diverge.
LEDGER_EVENT_REQUIRED_FIELDS = (
    "schema_version",
    "event_id",
    "id",
    "event_time",
    "observed_at",
    "type",
    "source",
    "lifecycle_state",
    "data",
)
LEDGER_SEGMENT_MAX_BYTES = 4 * 1024 * 1024


def validate_ledger_event_row(row: Mapping[str, Any]) -> None:
    """Reject a seeded row the production ledger parser could not read back."""
    missing = [name for name in LEDGER_EVENT_REQUIRED_FIELDS if name not in row]
    if missing:
        raise RuntimeError(
            f"seeded ledger row {row.get('type')!r} is missing required fields: {missing}"
        )
    if row.get("id") != row.get("event_id"):
        raise RuntimeError(
            f"seeded ledger row {row.get('type')!r} has a mismatched id/event_id"
        )
    if not isinstance(row.get("data"), Mapping):
        raise RuntimeError(
            f"seeded ledger row {row.get('type')!r} has a non-object data payload"
        )


def ledger_event_row(
    *,
    event_id: str,
    event_time: str,
    run_seq: int,
    event_type: str,
    agent_id: str | None,
    run_id: str | None,
    invocation_id: str | None,
    generation: int | None,
    source: str,
    session_id: str | None,
    data: Mapping[str, Any],
    **envelope: object,
) -> dict[str, Any]:
    """Build one production-shaped immutable ledger row."""
    row: dict[str, Any] = {
        "schema_version": 1,
        "id": event_id,
        "event_id": event_id,
        "event_time": event_time,
        "observed_at": event_time,
        "run_seq": run_seq,
        "type": event_type,
        "agent_id": agent_id,
        "run_id": run_id,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "generation": generation,
        "source": source,
        "lifecycle_state": "emitted",
        "data": dict(data),
    }
    row.update(envelope)
    validate_ledger_event_row(row)
    return row


def _ledger_segment_paths(segment_dir: Path) -> list[Path]:
    return sorted(segment_dir.glob("segment-*.jsonl"))


def _next_ledger_segment_path(segment_dir: Path) -> Path:
    next_index = (
        max(
            (
                int(index)
                for path in _ledger_segment_paths(segment_dir)
                if (index := path.stem.removeprefix("segment-")).isdigit()
            ),
            default=0,
        )
        + 1
    )
    return segment_dir / f"segment-{next_index:012}.jsonl"


def append_seeded_ledger_rows(
    repository: Path, rows: list[dict[str, Any]]
) -> Path:
    """Append seeded rows through the ledger writer's own segment layout.

    The production ``LedgerWriter::append`` appends to the last existing
    segment (``segment-000000000000.jsonl`` when none exists) and rotates only
    at its size bound. Seeding follows that same rule so historical rows share
    the writer's segment instead of inventing a segment named after a
    ``run_seq`` value.
    """
    segment_dir = repository / ".exo/ledger/segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    existing_segments = _ledger_segment_paths(segment_dir)
    target = (
        existing_segments[-1]
        if existing_segments
        else segment_dir / "segment-000000000000.jsonl"
    )
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    for row in rows:
        validate_ledger_event_row(row)
    appended = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if (
        existing
        and len(existing.encode("utf-8")) + len(appended.encode("utf-8"))
        > LEDGER_SEGMENT_MAX_BYTES
    ):
        target = _next_ledger_segment_path(segment_dir)
        existing = ""
    target.write_text(existing + appended, encoding="utf-8")
    advance_seeded_ledger_sequence(repository, rows)
    return target


def owner_for(repository: Path) -> tuple[Path, dict[str, Any]]:
    owners = []
    for directory in (repository / ".exo/agents").iterdir():
        identity_path = directory / "identity.json"
        if not identity_path.is_file():
            continue
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("slug") == OWNER_SLICE:
            owners.append((directory, identity))
    if len(owners) != 1:
        raise RuntimeError(f"expected one #814 owner, found {len(owners)}")
    return owners[0]


def invocation(owner_dir: Path) -> dict[str, Any]:
    return json.loads((owner_dir / "invocation.json").read_text(encoding="utf-8"))


def retained_rejected_request(repository: Path) -> dict[str, Any]:
    journal = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [
        row
        for row in journal
        if row.get("operation") == "spawn_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "rejected"
        and row.get("arguments", {}).get("intent_id")
        == "seed-issue-814-rejected-spawn-intent"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one immutable original rejected Kani request, found {len(matches)}"
        )
    return matches[0]


def retained_original_dispatch(
    repository: Path, baseline: dict[str, Any]
) -> dict[str, Any]:
    """Return the exact immutable owner launch record for either fixture shape."""
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        return retained_rejected_request(repository)
    expected = baseline.get("accepted_spawn_journal")
    if not isinstance(expected, dict):
        raise TypeError(
            "terminal-provider baseline lacks its accepted spawn journal record"
        )
    rows = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    intent_id = expected.get("arguments", {}).get("intent_id")
    matches = [
        row
        for row in rows
        if row.get("operation") == "spawn_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
        and row.get("arguments", {}).get("intent_id") == intent_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one accepted historical owner launch, found {len(matches)}"
        )
    return matches[0]


def wait_for_terminal(owner_dir: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = invocation(owner_dir)
        if current.get("status") in {"exited", "failed", "killed"}:
            return current
        time.sleep(0.1)
    raise RuntimeError("historical OpenCode owner did not reach a terminal invocation")


def seed(repository: Path, output: Path) -> None:
    transport = TransportClient(project_root=repository, timeout=15)
    controller = EffectClient(transport, role="tl", name="kani")
    snapshot = json.loads(
        (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
    )
    declarations = snapshot["plan"]["sub_tls"]
    kani_declaration = next(row for row in declarations if row["name"] == "kani")
    leaves = kani_declaration["plan"]["leaves"]
    planned_task = next(row for row in leaves if row["name"] == OWNER_SLICE)["task"]
    if not isinstance(planned_task, str) or not planned_task.strip():
        raise RuntimeError(
            "accepted Kani declaration has no task for its historical owner"
        )
    spawned = controller.spawn_leaf(
        name=OWNER_SLICE,
        task=planned_task,
        intent_id="e2e-historical-opencode-814",
        agent_type="opencode",
        model="opencode-go/deepseek-v4.1-flash",
    )
    if spawned.success is not True:
        raise RuntimeError(f"historical OpenCode spawn failed: {spawned.raw!r}")
    deadline = time.monotonic() + 20
    owner_dir: Path | None = None
    identity: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            owner_dir, identity = owner_for(repository)
            break
        except (OSError, RuntimeError, json.JSONDecodeError):
            time.sleep(0.1)
    if owner_dir is None or identity is None:
        raise RuntimeError("spawn did not register the logical #814 owner")
    invocation_path = owner_dir / "invocation.json"
    original = wait_for_terminal(owner_dir)
    if (
        original.get("runtime") != "opencode"
        or original.get("model") != "opencode-go/deepseek-v4.1-flash"
        or original.get("effort") != "high"
        or original.get("exit_code") in (None, 0)
        or original.get("pr_number") is not None
        or original.get("head_sha") is not None
    ):
        raise RuntimeError(
            f"owner is not a failed pre-publication OpenCode invocation: {original!r}"
        )
    (owner_dir / "active_issue").write_text(f"{BLOCKED_ISSUE_ID}\n", encoding="utf-8")
    configured_worktree = Path(str(identity["working_dir"]))
    worktree = (
        configured_worktree
        if configured_worktree.is_absolute()
        else repository / configured_worktree
    )
    if not worktree.is_dir():
        raise RuntimeError("historical owner worktree is unavailable")
    fingerprint = worktree_fingerprint(worktree)
    history = repository / ".exo/events" / f"{identity['agent_name']}.jsonl"
    if not history.is_file():
        raise RuntimeError("historical OpenCode invocation event history is missing")
    record = {
        "owner_agent_name": identity["agent_name"],
        "owner_dir": str(owner_dir),
        "identity": identity,
        "prior_invocation": original,
        "task": planned_task,
        "rejected_request": retained_rejected_request(repository),
        "prior_invocation_file_sha256": digest(invocation_path),
        "branch": identity["birth_branch"],
        "worktree": str(worktree),
        "worktree_fingerprint": fingerprint,
        "history_sha256": digest(history),
        "history_lines": history.read_text(encoding="utf-8").splitlines(),
        "spawn_result_agent": (spawned.result or {}).get("agent"),
    }
    output.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "seeded_owner": record["owner_agent_name"],
                "prior_invocation_id": original["invocation_id"],
            }
        )
    )


def seed_terminal_provider_failure(
    repository: Path,
    output: Path,
    *,
    seed_parent_stall: bool = False,
) -> None:
    """Seed historical OpenCode records without launching that provider."""
    from tl_loop.client.effects import ToolResult
    from tl_loop.fsm.child import ChildKind, ChildRecord
    from tl_loop.fsm.scope import TLRunning
    from tl_loop.loop.driver import (
        EffectIntent,
        _terminal_owner_recovery_gate_name,
        derive_child_branch,
    )
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.state.schema import GateStatus, SliceStatus
    from tl_loop.state.store import RunStore

    transport = TransportClient(project_root=repository, timeout=30)
    controller = EffectClient(transport, role="tl", name="kani")
    snapshot = json.loads(
        (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
    )
    kani_declaration = next(
        row for row in snapshot["plan"]["sub_tls"] if row["name"] == "kani"
    )
    leaf_declaration = next(
        row for row in kani_declaration["plan"]["leaves"] if row["name"] == OWNER_SLICE
    )
    task = leaf_declaration["task"]
    if leaf_declaration.get("chainlink_issue_id") != BLOCKED_ISSUE_ID:
        raise RuntimeError(
            "accepted terminal-failure fixture lacks the typed #814 binding"
        )
    intent_id = "e2e-terminal-opencode-814"
    owner_name = f"{OWNER_SLICE}-opencode"
    parent_branch = "main.kani"
    branch = derive_child_branch(parent_branch, owner_name)
    worktree = repository / ".exo" / "worktrees" / owner_name
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--quiet",
            "-b",
            branch,
            str(worktree),
            parent_branch,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    owner_dir = repository / ".exo" / "agents" / owner_name
    owner_dir.mkdir(parents=True, exist_ok=False)
    (owner_dir / "active_issue").write_text(f"{BLOCKED_ISSUE_ID}\n", encoding="utf-8")
    identity = {
        "agent_name": owner_name,
        "slug": OWNER_SLICE,
        "slice_id": OWNER_SLICE,
        "display_name": f"OpenCode {OWNER_SLICE}",
        "agent_type": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "effort": "high",
        "birth_branch": branch,
        "parent_branch": parent_branch,
        "working_dir": str(worktree),
        "topology": "worktree_per_agent",
        "ledger_owned": False,
    }
    (owner_dir / "identity.json").write_text(
        json.dumps(identity, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    invocation_id = "f4460477-40bf-4824-941b-dcc3d0ebc9a1"
    started_at = int(time.time()) - 30
    ended_at = started_at + 5
    original: dict[str, Any] = {
        "invocation_id": invocation_id,
        "generation": 1,
        "status": "failed",
        "runtime": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "effort": "high",
        "trigger": "spawn",
        "branch": branch,
        "worktree": str(worktree),
        "runtime_agent_id": owner_name,
        "slice_id": OWNER_SLICE,
        "pr_number": None,
        "head_sha": None,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": 42,
        "exit_reason": "tmux_target_exited_with_nonzero_code",
        "exit_classification": "nonzero_exit",
        "stderr_tail": "Insufficient account funds",
        "prior_invocation_id": None,
        "recovery_round": 0,
        "authorization_source": None,
        "mode": "interactive",
        "routing": {"window_id": "@999999"},
    }
    (owner_dir / "invocation.json").write_text(
        json.dumps(original, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (owner_dir / "dispatch_intent").write_text(intent_id + "\n", encoding="utf-8")
    (owner_dir / "routing.json").write_text(
        json.dumps({"window_id": "@999999"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    arguments: dict[str, Any] = {
        "name": OWNER_SLICE,
        "task": task,
        "intent_id": intent_id,
        "agent_type": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "boundary": leaf_declaration.get("boundary", []),
        "verify": leaf_declaration.get("verify", []),
    }
    spawn_intent = EffectIntent("spawn_leaf", OWNER_SLICE, arguments, True)
    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    journal = EffectJournal("kani", child_store.run_dir / "action-journal.json")
    journal.append(spawn_intent)
    journal.mark_result(
        spawn_intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    "agent_type": "opencode",
                    "branch_name": branch,
                    "worktree_path": str(worktree),
                    "invocation_id": None,
                    "invocation_trigger": None,
                    "invocation_runtime": None,
                    "invocation_fresh": None,
                    "invocation_ready": None,
                    "invocation_outcome": None,
                    "routing_target_id": None,
                    "routing_target_type": None,
                },
            }
        ),
    )
    events = canonical_events(repository)
    runtime_run_id = (repository / ".exo/run_id").read_text(encoding="utf-8").strip()
    session_id = next(
        (row.get("session_id") for row in reversed(events) if row.get("session_id")),
        None,
    )
    first_sequence = (
        max(
            (sequence for row in events if type(sequence := row.get("run_seq")) is int),
            default=0,
        )
        + 1
    )
    spawn_seq = first_sequence + 1
    timestamps = [started_at, started_at + 1, ended_at]
    event_ids = [str(uuid4()) for _ in range(3)]
    owner_data = {
        "batch_id": None,
        "branch": branch,
        "channel": None,
        "delivery_vs_authoritative": "metadata_only",
        "generation": 1,
        "harness": "exomonad",
        "head_sha": None,
        "invocation_id": invocation_id,
        "issue_number": BLOCKED_ISSUE_ID,
        "item_id": None,
        "outcome": "started",
        "pr_number": None,
        "provider": "opencode",
        "queue_class": None,
        "queue_seq": None,
        "role": "dev",
        "rollout_mode": "enabled",
        "run_id": None,
        "runtime": "opencode",
        "runtime_agent_id": owner_name,
        "session_id": None,
        "slice_id": OWNER_SLICE,
        "source": "lifecycle",
        "source_agent": None,
        "status": "running",
        "stderr_tail": None,
        "trigger": "spawn",
        "worktree": str(worktree),
    }
    ledger_rows = []
    for index, (event_type, outcome, status) in enumerate(
        (
            ("agent.invocation.started", "started", "running"),
            ("agent.spawned", "spawned", None),
            ("agent.invocation.finished", "finished", "failed"),
        )
    ):
        event_time = (
            datetime.fromtimestamp(timestamps[index], timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        if index == 1:
            ledger_rows.append(
                ledger_event_row(
                    event_id=event_ids[index],
                    event_time=event_time,
                    run_seq=first_sequence + index,
                    event_type=event_type,
                    agent_id="kani",
                    run_id=runtime_run_id,
                    invocation_id=invocation_id,
                    generation=None,
                    source="rust",
                    session_id=session_id,
                    data={
                        "agent_type": "OpenCode",
                        "branch": branch,
                        "child_agent": owner_name,
                        "effort": "high",
                        "intent_id": intent_id,
                        "invocation_id": invocation_id,
                        "model": "opencode-go/deepseek-v4.1-flash",
                        "spawn_type": "leaf_subtree",
                        "topology": "worktreeperagent",
                    },
                )
            )
            continue
        row_data = {
            **owner_data,
            "exit_classification": "nonzero_exit" if status == "failed" else None,
            "exit_code": 42 if status == "failed" else None,
            "exit_reason": "tmux_target_exited_with_nonzero_code"
            if status == "failed"
            else None,
            "outcome": outcome,
            "status": status,
            "stderr_tail": "Insufficient account funds" if status == "failed" else None,
        }
        ledger_rows.append(
            ledger_event_row(
                event_id=event_ids[index],
                event_time=event_time,
                run_seq=first_sequence + index,
                event_type=event_type,
                agent_id=owner_name,
                run_id=runtime_run_id,
                invocation_id=invocation_id,
                generation=1,
                source="lifecycle",
                session_id=session_id,
                data=row_data,
                harness="exomonad",
                provider="opencode",
                runtime="opencode",
                role="dev",
            )
        )
    append_seeded_ledger_rows(repository, ledger_rows)
    history = repository / ".exo/events" / f"{owner_name}.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history_rows = []
    for index, (event_type, status, outcome) in enumerate(
        (
            ("agent.invocation.started", "running", "started"),
            ("agent.invocation.finished", "failed", "finished"),
        )
    ):
        event_id = str(uuid4())
        data = {
            **owner_data,
            "exit_classification": "nonzero_exit" if status == "failed" else None,
            "exit_code": 42 if status == "failed" else None,
            "exit_reason": "tmux_target_exited_with_nonzero_code"
            if status == "failed"
            else None,
            "outcome": outcome,
            "status": status,
            "stderr_tail": "Insufficient account funds" if status == "failed" else None,
        }
        history_rows.append(
            {
                "agent_id": owner_name,
                "data": data,
                "id": event_id,
                "ts": datetime.fromtimestamp(timestamps[index * 2], timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "type": event_type,
            }
        )
    history.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in history_rows),
        encoding="utf-8",
    )
    worktree = worktree.resolve(strict=True)
    identity["working_dir"] = str(worktree)
    original["worktree"] = str(worktree)
    (owner_dir / "identity.json").write_text(
        json.dumps(identity, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (owner_dir / "invocation.json").write_text(
        json.dumps(original, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    accepted_rows = [
        row
        for row in canonical_events(repository)
        if row.get("type") == "agent.spawned"
        and row.get("agent_id") == "kani"
        and row.get("run_id") == runtime_run_id
        and row.get("invocation_id") == invocation_id
        and row.get("data", {}).get("intent_id") == intent_id
        and row.get("data", {}).get("child_agent") == identity.get("agent_name")
        and row.get("data", {}).get("branch") == branch
    ]
    if len(accepted_rows) != 1:
        raise RuntimeError(
            f"accepted OpenCode launch has {len(accepted_rows)} matching authoritative spawn events"
        )
    spawn_event = accepted_rows[0]
    spawn_seq = spawn_event.get("run_seq")
    if type(spawn_seq) is not int or spawn_seq <= 0:
        raise RuntimeError("accepted OpenCode spawn event has no durable sequence")

    controller = EffectClient(
        TransportClient(project_root=repository, timeout=30), role="tl", name="kani"
    )

    state = child_store.load()
    manifest = state.plan_manifest
    if manifest is None:
        raise RuntimeError("Kani checkpoint has no accepted child manifest")
    current = state.slices.get(OWNER_SLICE)
    if current is None or current.status is not SliceStatus.PENDING:
        raise RuntimeError(
            "terminal-failure fixture must begin with an undispatched accepted leaf"
        )
    node = next((item for item in manifest.nodes if item.name == OWNER_SLICE), None)
    if node is None or node.chainlink_issue_id != BLOCKED_ISSUE_ID:
        raise RuntimeError(
            "accepted child manifest does not bind the terminal leaf to issue #814"
        )
    requested_harness = str(leaf_declaration.get("agent_type", ""))
    launched = replace(
        current,
        status=SliceStatus.SPAWNED,
        attempts=1,
        branch=branch,
        base_ref="main.kani",
        agent_type="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        requested_harness=requested_harness,
        resolved_harness="opencode/opencode-go/deepseek-v4.1-flash",
        dispatch_intent_id=intent_id,
        dispatch_started_at=float(original["started_at"]),
        dispatch_generation=2,
        dispatch_ledger_floor=spawn_seq - 1,
        dispatch_last_boundary="agent.spawned",
        dispatch_agent_id=identity["agent_name"],
        dispatch_invocation_id=invocation_id,
        dispatch_authoritative_event_seq=spawn_seq,
        chainlink_issue_id=BLOCKED_ISSUE_ID,
        worktree=str(current.worktree),
        recovery=None,
        park_cause=None,
    )
    accepted_child = ChildRecord(
        OWNER_SLICE,
        ChildKind.LEAF,
        dispatch_intent_id=intent_id,
        invocation_id=invocation_id,
        manifest_node_id=node.node_id,
        manifest_revision=manifest.manifest_revision,
    )
    child_running = TLRunning(
        current_order=0,
        pending_by_order={},
        scope_path=(manifest.scope_id,),
        plan_digest=manifest.digest,
        parallel_pending=(accepted_child,),
    )
    child_store.checkpoint(
        child_running,
        {**state.slices, OWNER_SLICE: launched},
        state.budgets,
        spawn_seq,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=manifest,
    )

    root_store = RunStore("root", repository / ".exo/tl-loop")
    root_state = root_store.load()
    root_store.checkpoint(
        root_state.recursive_fsm,
        root_state.slices,
        root_state.budgets,
        spawn_seq,
        current_order=root_state.current_order,
        ordered_stages=root_state.ordered_stages,
        integration=root_state.integration,
        plan_manifest=root_state.plan_manifest,
    )
    blocker = controller.emit_controller_event(
        event_type="agent.task_blocked",
        payload={
            "outcome": "blocked",
            "slice_id": OWNER_SLICE,
            "cause": "human_decision_required",
            "scope_attribution": "agent_lifecycle",
            "needs_human": True,
            "retryable": True,
            "recovery_action": "apply the configured worker route to the preserved assignment",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "missing_handoff:nonzero_exit",
            "attempt": 1,
        },
    )
    if blocker.success is not True or not isinstance(blocker.result, dict):
        raise RuntimeError(
            f"production task-blocked event was not recorded: {blocker.error!r}"
        )
    blocker_id = blocker.result.get("event_id")
    blocker_rows = [
        row for row in canonical_events(repository) if row.get("event_id") == blocker_id
    ]
    if len(blocker_rows) != 1 or blocker_rows[0].get("type") != "agent.task_blocked":
        raise RuntimeError(
            "post-launch human blocker is not retained in the canonical ledger"
        )
    if blocker_rows[0].get("run_seq", 0) <= spawn_seq:
        raise RuntimeError(
            "terminal blocker does not follow the accepted OpenCode spawn"
        )

    # Exercise the real compiled host refusal for a route-less retry. It must
    # leave the historical OpenCode owner untouched; this probe is not added
    # to the controller action journal because it is a fixture negative control.
    identity_before = digest(owner_dir / "identity.json")
    invocation_before = digest(owner_dir / "invocation.json")
    route_less_refusal = controller.resume_blocked_leaf(
        chainlink_issue_id=BLOCKED_ISSUE_ID,
        expected_invocation_id=invocation_id,
        expected_branch=branch,
        expected_worktree_fingerprint=worktree_fingerprint(worktree),
        task=task,
        human_approved=True,
    )
    expected_refusal = "Invalid input: blocked-leaf controller recovery requires its accepted role route"
    if (
        route_less_refusal.success is not False
        or route_less_refusal.error != expected_refusal
    ):
        raise RuntimeError(
            "compiled host did not reject the missing-role-route negative control"
        )
    if (
        digest(owner_dir / "identity.json") != identity_before
        or digest(owner_dir / "invocation.json") != invocation_before
    ):
        raise RuntimeError("route-less negative control changed the retained owner")
    terminal_gate_name = _terminal_owner_recovery_gate_name(OWNER_SLICE)
    child_store.set_gate(terminal_gate_name, GateStatus.PENDING)

    history = repository / ".exo/events" / f"{identity['agent_name']}.jsonl"
    evidence: dict[str, Any] = {
        "historical_dispatch_kind": "accepted_terminal_provider_failure",
        "owner_agent_name": identity["agent_name"],
        "owner_dir": str(owner_dir),
        "prior_invocation": original,
        "prior_invocation_file_sha256": digest(owner_dir / "invocation.json"),
        "branch": branch,
        "worktree": str(worktree),
        "planned_worktree": current.worktree,
        "worktree_fingerprint": worktree_fingerprint(worktree),
        "spawn_event_id": spawn_event["event_id"],
        "spawn_event_seq": spawn_seq,
        "accepted_spawn_event": spawn_event,
        "blocker_event_id": blocker_id,
        "blocker_event_seq": blocker_rows[0]["run_seq"],
        "accepted_spawn_journal": journal.entries_for("spawn_leaf", OWNER_SLICE)[0],
        "history_lines": history.read_text(encoding="utf-8").splitlines(),
        "route_less_host_refusal": {
            "success": route_less_refusal.success,
            "error": route_less_refusal.error,
            "error_kind": route_less_refusal.error_kind,
            "identity_sha256_before": identity_before,
            "invocation_sha256_before": invocation_before,
        },
        "terminal_recovery_gate": terminal_gate_name,
    }
    if seed_parent_stall:
        evidence.update(_seed_historical_parent_stall(repository, blocker_rows[0]))
    output.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"accepted_spawn": invocation_id, "terminal_status": original["status"]}
        )
    )


LEGACY_MODERN_MARKERS = frozenset(
    {
        "id",
        "agent_id",
        "issue",
        "intent_id",
        "role_route",
        "model",
        "effort",
        "generation",
        "prior_invocation_id",
    }
)


def legacy_successor_lifecycle_rows(
    *,
    owner_name: str,
    controller_run_id: str,
    ledger_run_id: str,
    successor_id: str,
    predecessor_id: str,
    branch: str,
    worktree: str,
    session_id: str | None,
    first_sequence: int,
    ended_at: int,
    fingerprint: str,
) -> list[dict[str, Any]]:
    """Build the historical host chronology for an old blocked resume.

    The pre-launch-row host wrote ``agent.invocation.started``,
    ``agent.resumed`` and ``agent.invocation.finished`` for the resumed
    successor and no ``agent.spawned`` confirmation. Each row carries the same
    envelope and payload fields the real producer emitted; in particular the
    invocation rows are ``source="lifecycle"`` and the resume audit row is
    ``source="rust"`` with no top-level harness/provider/role/runtime and no
    ``intent_id``.
    """

    def event_time(index: int) -> str:
        return (
            datetime.fromtimestamp(ended_at - 2 + index, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def invocation_data(
        *,
        status: str,
        outcome: str,
        exit_classification: str | None = None,
        exit_code: int | None = None,
        exit_reason: str | None = None,
        stderr_tail: str | None = None,
    ) -> dict[str, Any]:
        return {
            "batch_id": None,
            "branch": branch,
            "channel": None,
            "delivery_vs_authoritative": "metadata_only",
            "exit_classification": exit_classification,
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "generation": 2,
            "harness": "exomonad",
            "head_sha": None,
            "invocation_id": successor_id,
            "issue_number": None,
            "item_id": None,
            "outcome": outcome,
            "pr_number": None,
            "provider": "codex",
            "queue_class": None,
            "queue_seq": None,
            "role": "dev",
            "rollout_mode": "enabled",
            "run_id": None,
            "runtime": "codex",
            "runtime_agent_id": owner_name,
            "session_id": None,
            "slice_id": OWNER_SLICE,
            "source": "lifecycle",
            "source_agent": None,
            "status": status,
            "stderr_tail": stderr_tail,
            "trigger": "resume_blocked",
            "worktree": worktree,
        }

    return [
        ledger_event_row(
            event_id="legacy-f285-agent-invocation-started-0",
            event_time=event_time(0),
            run_seq=first_sequence,
            event_type="agent.invocation.started",
            agent_id=owner_name,
            run_id=ledger_run_id,
            invocation_id=successor_id,
            generation=2,
            source="lifecycle",
            session_id=session_id,
            data=invocation_data(status="running", outcome="started"),
            harness="exomonad",
            provider="codex",
            role="dev",
            runtime="codex",
        ),
        ledger_event_row(
            event_id="legacy-f285-agent-resumed-1",
            event_time=event_time(1),
            run_seq=first_sequence + 1,
            event_type="agent.resumed",
            agent_id=controller_run_id,
            run_id=ledger_run_id,
            invocation_id=successor_id,
            generation=2,
            source="rust",
            session_id=session_id,
            data={
                "agent_type": "codex",
                "branch": branch,
                "chainlink_issue_id": BLOCKED_ISSUE_ID,
                "child_agent": owner_name,
                "effort": "xhigh",
                "generation": 2,
                "invocation_id": successor_id,
                "model": "gpt-6.1-sol",
                "previous_invocation_id": predecessor_id,
                "same_owner": True,
                "spawn_type": "resume_blocked",
                "worktree_fingerprint": fingerprint,
            },
        ),
        ledger_event_row(
            event_id="legacy-f285-agent-invocation-finished-2",
            event_time=event_time(2),
            run_seq=first_sequence + 2,
            event_type="agent.invocation.finished",
            agent_id=owner_name,
            run_id=ledger_run_id,
            invocation_id=successor_id,
            generation=2,
            source="lifecycle",
            session_id=session_id,
            data=invocation_data(
                status="exited",
                outcome="finished",
                exit_classification="one_shot_complete",
                exit_code=0,
                exit_reason="one_shot_invocation_completed",
                stderr_tail="one-shot invocation completed without a pull request",
            ),
            harness="exomonad",
            provider="codex",
            role="dev",
            runtime="codex",
        ),
    ]


def _seed_legacy_successor_lifecycle(
    repository: Path,
    *,
    owner_name: str,
    controller_run_id: str,
    successor_id: str,
    predecessor_id: str,
    branch: str,
    worktree: str,
    ended_at: int,
) -> None:
    """Append the historical host chronology for an old blocked resume."""
    from tl_loop.__main__ import _authoritative_ledger_run_id

    ledger_run_id = _authoritative_ledger_run_id(repository)
    if ledger_run_id is None:
        raise RuntimeError("historical resume fixture requires the authoritative run_id")
    events = canonical_events(repository)
    session_id = next(
        (row.get("session_id") for row in reversed(events) if row.get("session_id")),
        None,
    )
    first_sequence = (
        max(
            (sequence for row in events if type(sequence := row.get("run_seq")) is int),
            default=0,
        )
        + 1
    )
    rows = legacy_successor_lifecycle_rows(
        owner_name=owner_name,
        controller_run_id=controller_run_id,
        ledger_run_id=ledger_run_id,
        successor_id=successor_id,
        predecessor_id=predecessor_id,
        branch=branch,
        worktree=worktree,
        session_id=session_id,
        first_sequence=first_sequence,
        ended_at=ended_at,
        fingerprint=worktree_fingerprint(Path(worktree)),
    )
    append_seeded_ledger_rows(repository, rows)


def seed_confirmed_wrong_route_successor(repository: Path, baseline_path: Path) -> None:
    """Retain the historical Luna-intent/Sol-successor record from #1299."""
    from tl_loop.__main__ import _authoritative_ledger_run_id
    from tl_loop.client.effects import ToolResult
    from tl_loop.loop.driver import (
        EffectIntent,
        TLLoopConfig,
        WorkPlan,
        _blocked_leaf_resume_action_arguments,
        _persist_terminal_route_authorization,
        _terminal_owner_recovery_proof,
    )
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.select.capability import load_capability
    from tl_loop.select.model import load_model_catalog
    from tl_loop.select.policy import load_policy
    from tl_loop.state.store import RunStore

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        raise RuntimeError(
            "wrong-route successor requires the accepted terminal-owner fixture"
        )
    root = repository / ".exo/tl-loop"
    child_store = RunStore("kani", root / "root")
    state = child_store.load()
    current = state.slices.get(OWNER_SLICE)
    if current is None or not isinstance(current.base_ref, str) or not current.base_ref:
        raise RuntimeError("accepted Kani checkpoint has no parent branch binding")
    if (
        baseline["prior_invocation"]["invocation_id"]
        != "f4460477-40bf-4824-941b-dcc3d0ebc9a1"
    ):
        raise RuntimeError(
            "initial OpenCode fixture does not retain the exact f446 predecessor"
        )
    if current.dispatch_invocation_id != "f4460477-40bf-4824-941b-dcc3d0ebc9a1":
        raise RuntimeError(
            "Kani checkpoint no longer points at the original f446 invocation"
        )
    snapshot = json.loads((root / "plan.snapshot").read_text(encoding="utf-8"))
    declarations = snapshot.get("plan", {}).get("sub_tls", [])
    kani_declaration = next(
        row
        for row in declarations
        if isinstance(row, dict) and row.get("name") == "kani"
    )
    plan = WorkPlan.from_mapping(kani_declaration["plan"])
    init_records = read_json_lines(repository / ".exo/logs/init.jsonl")
    resolved_rows = [
        row.get("resolved")
        for row in init_records
        if isinstance(row.get("resolved"), dict)
        and row["resolved"].get("role_route_config_fingerprint")
    ]
    if not resolved_rows:
        raise RuntimeError("accepted init route configuration is unavailable")
    resolved = resolved_rows[-1]
    worker_effort = resolved.get("worker_effort")
    if not isinstance(worker_effort, dict) or worker_effort.get("level") != "xhigh":
        raise RuntimeError(
            "accepted init configuration does not select xhigh worker effort"
        )
    policy_path = repository / ".exo/harness_policy.toml"
    policy = load_policy(policy_path)
    capabilities = load_capability(
        repository / ".exo/harness_capability.toml", policy_path=policy_path
    )
    catalog = load_model_catalog(repository / ".exo/model-catalog.json")
    config = TLLoopConfig(
        active=True,
        root_dir=root,
        project_root=repository,
        run_id=state.run_id,
        ledger_run_id=_authoritative_ledger_run_id(repository),
        branch=current.base_ref,
        role="worker",
        worktree=Path(state.owner_worktree)
        if state.owner_worktree is not None
        else None,
        parent_run_id=state.parent_run_id,
        parent_branch=state.parent_branch,
        parent_agent_id=state.parent_agent_id,
        depth=state.depth,
        declared_manifest=state.plan_manifest,
        policy=policy,
        capabilities=capabilities,
        catalog=catalog,
        requested_model=resolved.get("worker_model"),
        worker_effort_level=worker_effort.get("level"),
        role_route_configuration_fingerprint=resolved.get(
            "role_route_config_fingerprint"
        ),
    )
    proof, reason = _terminal_owner_recovery_proof(plan, state, config, child_store)
    if proof is None:
        raise RuntimeError(
            f"could not derive the accepted historical route authorization: {reason}"
        )
    if (
        proof.resume.desired_harness != "codex/gpt-6-luna"
        or proof.resume.desired_model != "gpt-6-luna"
        or proof.resume.desired_effort != "xhigh"
    ):
        raise RuntimeError(
            "production route selection did not resolve Codex Luna/xhigh"
        )
    state = _persist_terminal_route_authorization(proof, state, child_store)
    arguments = dict(_blocked_leaf_resume_action_arguments(proof.resume, state))
    arguments.pop("route_fingerprint", None)
    if (
        arguments.get("expected_invocation_id")
        != baseline["prior_invocation"]["invocation_id"]
        or arguments.get("prior_generation") != 1
        or arguments.get("desired_model") != "gpt-6-luna"
        or arguments.get("desired_effort") != "xhigh"
    ):
        raise RuntimeError(
            "historical action is not anchored to f446 with Luna/xhigh intent"
        )
    intent_id = str(arguments["dispatch_intent_id"])
    owner_dir = Path(baseline["owner_dir"])
    owner_name = baseline["owner_agent_name"]
    branch = baseline["branch"]
    worktree = Path(baseline["worktree"]).resolve(strict=True)
    successor_id = "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
    journal = EffectJournal("kani", child_store.run_dir / "action-journal.json")
    intent = EffectIntent("resume_blocked_leaf", OWNER_SLICE, arguments, True)
    journal.append(intent)
    predecessor_id = baseline["prior_invocation"]["invocation_id"]
    legacy_agent = {
        "agent_type": "codex",
        "branch_name": branch,
        "worktree_path": str(worktree),
        "tab_name": "@43",
        "issue_title": "",
        "pane_id": None,
        "invocation_id": successor_id,
        "invocation_trigger": "resume_blocked",
        "invocation_runtime": "codex",
        "invocation_fresh": True,
        "invocation_ready": True,
        "invocation_outcome": "started",
        "routing_target_id": "@43",
        "routing_target_type": "window",
    }
    legacy_invocation = {
        "invocation_id": successor_id,
        "trigger": "resume_blocked",
        "runtime": "codex",
        "fresh": True,
        "ready": True,
        "outcome": "started",
        "target_id": "@43",
        "target_type": "window",
    }
    if set(legacy_agent) & LEGACY_MODERN_MARKERS or set(
        legacy_invocation
    ) & LEGACY_MODERN_MARKERS:
        raise RuntimeError("historical f285 response must omit modern provenance")
    response = ToolResult.from_raw(
        {
            "success": True,
            "result": {
                "chainlink_issue_id": BLOCKED_ISSUE_ID,
                "agent": legacy_agent,
                "invocation": legacy_invocation,
            },
        }
    )
    journal.mark_result(intent, response)
    identity_path = owner_dir / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if "role_route" in identity:
        raise RuntimeError(
            "legacy OpenCode predecessor unexpectedly has modern route provenance"
        )
    identity.update({"agent_type": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"})
    identity_path.write_text(json.dumps(identity, sort_keys=True, indent=2) + "\n")
    ended_at = int(time.time()) - 1
    persisted_successor = {
        "invocation_id": successor_id,
        "generation": 2,
        "status": "exited",
        "runtime": "codex",
        "model": "gpt-6.1-sol",
        "effort": "xhigh",
        "trigger": "resume_blocked",
        "branch": branch,
        "worktree": str(worktree),
        "runtime_agent_id": owner_name,
        "slice_id": OWNER_SLICE,
        "prior_invocation_id": predecessor_id,
        "pr_number": None,
        "head_sha": None,
        "started_at": ended_at - 1,
        "ended_at": ended_at,
        "exit_code": 0,
        "exit_reason": "one_shot_complete",
        "exit_classification": "one_shot_complete",
        "one_shot_complete": True,
        "routing": {"window_id": "@43"},
    }
    if "role_route" in persisted_successor:
        raise RuntimeError(
            "legacy Sol successor unexpectedly has a modern route receipt"
        )
    invocation_path = owner_dir / "invocation.json"
    invocation_path.write_text(
        json.dumps(persisted_successor, sort_keys=True, indent=2) + "\n"
    )
    (owner_dir / "dispatch_intent").write_text(intent_id + "\n", encoding="utf-8")
    (owner_dir / "routing.json").write_text(
        json.dumps(
            {
                "owner_agent_id": owner_name,
                "owner_invocation_id": successor_id,
                "owner_generation": 2,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_legacy_successor_lifecycle(
        repository,
        owner_name=owner_name,
        controller_run_id=state.run_id,
        successor_id=successor_id,
        predecessor_id=predecessor_id,
        branch=branch,
        worktree=str(worktree),
        ended_at=ended_at,
    )
    retained = canonical_events(repository)
    if any(
        row.get("type") == "agent.spawned"
        and (row.get("data") or {}).get("invocation_id") == successor_id
        for row in retained
    ):
        raise RuntimeError(
            "historical f285 fixture must not synthesize an agent.spawned launch row"
        )
    confirmed = journal.entries_for("resume_blocked_leaf", OWNER_SLICE)
    if len(confirmed) != 1 or confirmed[0].get("status") != "confirmed":
        raise RuntimeError("legacy confirmed resume action was not durably recorded")
    baseline["historical_wrong_route_successor"] = {
        "action": confirmed[0],
        "invocation": persisted_successor,
        "response": response.result,
        "identity": identity,
        "checkpoint_invocation_id": predecessor_id,
        "checkpoint_generation": 1,
    }
    baseline_path.write_text(json.dumps(baseline, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps(
            {
                "intent_invocation": baseline["prior_invocation"]["invocation_id"],
                "intent_generation": 1,
                "actual_invocation": successor_id,
                "actual_generation": 2,
                "actual_route": "codex/gpt-6.1-sol/xhigh",
                "checkpoint_still_bound_to": baseline["prior_invocation"][
                    "invocation_id"
                ],
            },
            sort_keys=True,
        )
    )


def _seed_historical_parent_stall(
    repository: Path,
    blocker_row: dict[str, Any],
) -> dict[str, Any]:
    """Retain the old root stall after its accepted child entered human recovery."""
    from tl_loop.events.envelope import project
    from tl_loop.fsm.recovery import RecoveryPhase, begin_recovery, transition_recovery
    from tl_loop.fsm.scope import TLFailed
    from tl_loop.loop.driver import _child_recovery_projection
    from tl_loop.ordered import SubTLLifecycle
    from tl_loop.state.schema import ParkCause, SliceStatus
    from tl_loop.state.store import RunStore

    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    child = child_store.load()
    leaf = child.slices.get(OWNER_SLICE)
    if (
        leaf is None
        or leaf.dispatch_invocation_id is None
        or leaf.dispatch_agent_id is None
    ):
        raise RuntimeError(
            "historical stall fixture lacks accepted child invocation identity"
        )
    event = project(blocker_row)
    blocked = event.task_blocked
    if blocked is None or event.run_seq is None:
        raise RuntimeError("historical blocker fixture has no typed blocked payload")
    audit = dict(event.data)
    audit.update(
        {
            "slice_id": OWNER_SLICE,
            "attempt": blocked.attempt,
            "invocation_id": leaf.dispatch_invocation_id,
            "scope_attribution": blocked.scope_attribution,
            "matched_difficulty_rule": blocked.matched_difficulty_rule,
            "outcome": "blocked",
            "needs_human": blocked.needs_human,
            "retryable": blocked.retryable,
        }
    )
    recovery = begin_recovery(
        cause=blocked.cause.value,
        owner_run_id=child.run_id,
        slice_attempt=blocked.attempt,
        owner_agent_id=leaf.dispatch_agent_id,
        invocation_generation=0,
        plan_revision=child.revision,
        evidence=audit,
        next_action="open_human_gate",
    )
    recovery = transition_recovery(
        recovery,
        RecoveryPhase.HUMAN_GATE,
        next_action="open_human_gate",
        evidence=audit,
    )
    child = child_store.checkpoint(
        child.fsm,
        {**child.slices, OWNER_SLICE: replace(leaf, recovery=recovery)},
        child.budgets,
        event.run_seq,
        current_order=child.current_order,
        ordered_stages=child.ordered_stages,
        integration=child.integration,
        plan_manifest=child.plan_manifest,
    )
    projection = _child_recovery_projection(child, "root")
    if projection is None:
        raise RuntimeError("child human recovery did not produce a parent projection")
    lifecycle, summary = projection
    if lifecycle is not SubTLLifecycle.HUMAN_GATE:
        raise RuntimeError(
            "terminal provider fixture did not reach a human-gated child state"
        )

    root_store = RunStore("root", repository / ".exo/tl-loop")
    root = root_store.load()
    kani = root.slices.get("kani")
    if kani is None or kani.dispatch_last_boundary != "sub_tl_started":
        raise RuntimeError(
            "historical parent stall fixture lacks accepted Kani dispatch"
        )
    parked = replace(
        kani,
        status=SliceStatus.PARKED,
        park_cause=ParkCause.STALL_DETECTED,
        park_audit={"attempts": 1, "reason": "pane_alive=false"},
    )
    integration = replace(
        root.integration,
        sub_tl_states={**root.integration.sub_tl_states, "kani": lifecycle},
        sub_tl_recovery={**root.integration.sub_tl_recovery, "kani": summary},
    )
    phase = TLFailed("heartbeat parked the root while its Kani child awaited recovery")
    root = root_store.checkpoint(
        phase,
        {**root.slices, "kani": parked},
        root.budgets,
        event.run_seq,
        current_order=root.current_order,
        ordered_stages=root.ordered_stages,
        integration=integration,
        plan_manifest=root.plan_manifest,
    )
    root_controller = EffectClient(
        TransportClient(project_root=repository, timeout=30), role="tl", name="root"
    )
    stall_event = root_controller.emit_controller_event(
        event_type="tl.slice_parked",
        payload={"slice_id": "kani", "park_cause": "stall_detected", "attempts": 1},
    )
    if stall_event.success is not True or not isinstance(stall_event.result, dict):
        raise RuntimeError(
            f"historical parent stall event was not retained: {stall_event.error!r}"
        )
    event_id = stall_event.result.get("event_id")
    rows = [
        row for row in canonical_events(repository) if row.get("event_id") == event_id
    ]
    if len(rows) != 1 or rows[0].get("type") != "tl.slice_parked":
        raise RuntimeError("historical parent stall is missing from canonical ledger")
    refreshed = root_store.load()
    current_audit = refreshed.slices["kani"].park_audit or {}
    stall_row = rows[0]
    stall_data = stall_row.get("data")
    if (
        refreshed.run_id != "root"
        or refreshed.current_order != 2
        or refreshed.fsm.phase.value != "tl_failed"
        or refreshed.slices["kani"].status is not SliceStatus.PARKED
        or refreshed.slices["kani"].park_cause is not ParkCause.STALL_DETECTED
        or current_audit.get("controller_epoch") is not None
        or current_audit.get("reason") != "pane_alive=false"
        or not isinstance(stall_data, dict)
        or stall_data.get("slice_id") != "kani"
        or stall_data.get("park_cause") != ParkCause.STALL_DETECTED.value
        or stall_row.get("run_seq", 0) <= event.run_seq
        or refreshed.integration.sub_tl_recovery.get("kani") != summary
    ):
        raise RuntimeError(
            "historical root stall checkpoint does not match the accepted fixture"
        )
    return {
        "historical_parent_stall_event_id": event_id,
        "historical_parent_stall_event_seq": rows[0].get("run_seq"),
        "historical_parent_stall_event": stall_row,
        "historical_parent_stall_audit": dict(current_audit),
        "historical_parent_stall_controller_epoch": refreshed.controller_epoch,
        "historical_parent_stall_phase": refreshed.fsm.phase.value,
        "historical_parent_recovery_summary": {
            "owner_run_id": summary.owner_run_id,
            "child_path": list(summary.child_path),
            "slice_id": summary.slice_id,
            "cause": summary.cause,
            "recovery_round": summary.recovery_round,
            "next_probe_at": summary.next_probe_at,
        },
    }


def owner_for_issue_id(owner_dir: Path) -> int | None:
    try:
        return int((owner_dir / "active_issue").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for_agent_event(path: Path, expected: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if path.is_file() and any(
            row.get("event") == expected for row in read_json_lines(path)
        ):
            return
        time.sleep(0.1)
    raise RuntimeError(f"worker did not record actual activity {expected!r}")


def verify_controller(
    repository: Path,
    baseline_path: Path,
    output: Path,
    gate_approval_path: Path | None = None,
) -> None:
    """Verify recovery, publication and review came from the continued controller."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("historical_dispatch_kind") == "accepted_terminal_provider_failure":
        if (
            baseline.get("prior_invocation", {}).get("status")
            not in {"failed", "exited"}
            or "insufficient account funds"
            not in str(
                baseline.get("prior_invocation", {}).get("stderr_tail", "")
            ).casefold()
        ):
            raise RuntimeError(
                "terminal-provider baseline does not retain the exact OpenCode failure"
            )
        matching_spawns = [
            row
            for row in canonical_events(repository)
            if row.get("event_id") == baseline.get("spawn_event_id")
        ]
        if matching_spawns != [baseline.get("accepted_spawn_event")]:
            raise RuntimeError(
                "accepted OpenCode spawn ledger evidence changed after launch"
            )
    if baseline.get("historical_parent_stall_event_id") is not None:
        stall_event_id = baseline["historical_parent_stall_event_id"]
        stall_rows = [
            row
            for row in canonical_events(repository)
            if row.get("event_id") == stall_event_id
        ]
        if stall_rows != [baseline.get("historical_parent_stall_event")]:
            raise RuntimeError("controller changed the historical parent stall event")
        root_checkpoint = json.loads(
            (repository / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8")
        )
        kani_projection = root_checkpoint.get("slices", {}).get("kani", {})
        stall_audit = kani_projection.get("park_audit") or {}
        superseded_epoch = stall_audit.get("superseded_by_epoch")
        if (
            kani_projection.get("status") == "parked"
            or kani_projection.get("park_cause") is not None
            or stall_audit.get("reason") != "pane_alive=false"
            or stall_audit.get("supersession_reason")
            != "fresh_continue_reconciles_invocation_local_stall"
            or not isinstance(superseded_epoch, str)
            or not superseded_epoch.strip()
        ):
            raise RuntimeError(
                "continued root did not retain a valid prior Kani stall supersession"
            )
    root_dir = repository / ".exo/tl-loop/root"
    child_path = root_dir / "kani/run.json"
    owner_dir = Path(baseline["owner_dir"])
    agent_log = Path(os.environ["E2E_AGENT_LOG"])
    deadline = time.monotonic() + 180
    expected_stall_offset = int(baseline.get("historical_parent_stall_event_seq") or 0)
    observed_root_offset = 0
    accepted: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    while time.monotonic() < deadline:
        try:
            child = json.loads(child_path.read_text(encoding="utf-8"))
            root_checkpoint = json.loads(
                (root_dir / "run.json").read_text(encoding="utf-8")
            )
            current_invocation = invocation(owner_dir)
            publications = json.loads(
                (repository / ".exo/published-heads.json").read_text(encoding="utf-8")
            ).get("heads", [])
        except (OSError, json.JSONDecodeError, TypeError):
            time.sleep(0.1)
            continue
        root_events = root_checkpoint.get("events")
        observed_root_offset = (
            root_events.get("last_consumed_offset", 0)
            if isinstance(root_events, dict)
            and isinstance(root_events.get("last_consumed_offset", 0), int)
            and not isinstance(root_events.get("last_consumed_offset", 0), bool)
            else 0
        )
        historical_stall_consumed = observed_root_offset >= expected_stall_offset
        leaf = child.get("slices", {}).get(OWNER_SLICE, {})
        publication_matches = [
            row
            for row in publications
            if row.get("author_agent") == baseline["owner_agent_name"]
            and row.get("slice_id") == OWNER_SLICE
        ]
        publication = leaf.get("publication")
        handoff = leaf.get("handoff")
        publication_head = (
            publication.get("head_sha") if isinstance(publication, dict) else None
        )
        publication_pr = (
            publication.get("pr_number") if isinstance(publication, dict) else None
        )
        head_sha = publication_head
        successor_finished_successfully = current_invocation.get("status") not in {
            "failed",
            "killed",
        } and (
            current_invocation.get("status") != "exited"
            or (
                current_invocation.get("exit_code") == 0
                and current_invocation.get("exit_classification") == "one_shot_complete"
            )
        )
        if historical_stall_consumed and (
            current_invocation.get("invocation_id")
            != baseline["prior_invocation"]["invocation_id"]
            and successor_finished_successfully
            and leaf.get("status") in {"spawned", "in_review", "repairing", "merged"}
            and leaf.get("dispatch_agent_id") == baseline["owner_agent_name"]
            and leaf.get("dispatch_invocation_id")
            == current_invocation.get("invocation_id")
            and leaf.get("branch") == baseline["branch"]
            and Path(leaf.get("worktree", "")).resolve()
            == Path(baseline["worktree"]).resolve()
            and isinstance(leaf.get("pr_number"), int)
            and isinstance(head_sha, str)
            and len(publication_matches) == 1
            and publication_matches[0].get("pr_number") == leaf.get("pr_number")
            and publication_matches[0].get("head_sha") == head_sha
            and publication_pr == leaf.get("pr_number")
            and publication.get("head_branch") == baseline["branch"]
            and publication.get("base_branch") == "main.kani"
            and publication.get("invocation_id")
            == current_invocation.get("invocation_id")
            and (leaf.get("head_sha") is None or leaf.get("head_sha") == head_sha)
            and isinstance(handoff, dict)
            and handoff.get("pr_number") == leaf.get("pr_number")
            and handoff.get("head_sha") == head_sha
            and handoff.get("invocation_id") == current_invocation.get("invocation_id")
            and publication_matches[0].get("invocation_id")
            == current_invocation.get("invocation_id")
        ):
            reviewers = _reviewer_invocations(repository, leaf["pr_number"], head_sha)
            reviewer_approvals = [
                row
                for row in read_json_lines(agent_log)
                if row.get("event") == "reviewer_approved"
                and row.get("pr_number") == leaf["pr_number"]
                and row.get("head_sha") == head_sha
            ]
            if len(reviewers) == 1 and len(reviewer_approvals) == 1:
                accepted = (child, current_invocation, reviewers[0])
                break
        time.sleep(0.1)
    if accepted is None:
        if observed_root_offset < expected_stall_offset:
            raise RuntimeError(
                "continued root did not acknowledge the historical stall within the "
                f"recovery window: offset={observed_root_offset}, "
                f"required={expected_stall_offset}"
            )
        raise RuntimeError(
            "production controller did not checkpoint the resumed #814 owner, adopt its PR, "
            "and launch the exact-head reviewer before timeout"
        )

    child, successor, reviewer = accepted
    leaf = child["slices"][OWNER_SLICE]
    publication = leaf["publication"]
    head_sha = publication["head_sha"]
    identity = json.loads((owner_dir / "identity.json").read_text(encoding="utf-8"))
    historical_wrong_route = baseline.get("historical_wrong_route_successor")
    prior = (
        historical_wrong_route["invocation"]
        if isinstance(historical_wrong_route, dict)
        else baseline["prior_invocation"]
    )
    if (
        successor.get("runtime") != "codex"
        or successor.get("model") != "gpt-6-luna"
        or successor.get("effort") != "xhigh"
        or successor.get("prior_invocation_id") != prior["invocation_id"]
        or successor.get("generation") != prior.get("generation", 1) + 1
        or identity.get("agent_type") != "codex"
        or identity.get("model") != "gpt-6-luna"
        or identity.get("effort") != "xhigh"
        or identity.get("agent_name") != baseline["owner_agent_name"]
        or identity.get("slice_id") != OWNER_SLICE
        or identity.get("birth_branch") != baseline["branch"]
        or Path(identity.get("working_dir", "")).resolve()
        != Path(baseline["worktree"]).resolve()
        or leaf.get("branch") != baseline["branch"]
        or Path(leaf.get("worktree", "")).resolve()
        != Path(baseline["worktree"]).resolve()
        or leaf.get("dispatch_agent_id") != baseline["owner_agent_name"]
        or leaf.get("dispatch_invocation_id") != successor.get("invocation_id")
        or leaf.get("status") in {"blocked", "pending"}
    ):
        raise RuntimeError(
            "controller checkpoint does not match the configured same-owner successor"
        )
    expected_original = baseline.get(
        "accepted_spawn_journal", baseline.get("rejected_request")
    )
    if retained_original_dispatch(repository, baseline) != expected_original:
        raise RuntimeError(
            "controller recovery changed the original OpenCode dispatch record"
        )
    history = repository / ".exo/events" / f"{baseline['owner_agent_name']}.jsonl"
    history_lines = history.read_text(encoding="utf-8").splitlines()
    if history_lines[: len(baseline["history_lines"])] != baseline["history_lines"]:
        raise RuntimeError(
            "controller recovery rewrote historical OpenCode lifecycle records"
        )
    activity = event_counts(agent_log)
    historical = baseline.get("prior_invocation")
    if (
        not isinstance(historical, dict)
        or historical.get("runtime") != "opencode"
        or historical.get("status") != "failed"
        or historical.get("exit_classification") != "nonzero_exit"
        or "insufficient account funds"
        not in str(historical.get("stderr_tail", "")).casefold()
    ):
        raise RuntimeError("historical OpenCode failure fixture is incomplete")
    if activity.get("historical_opencode_owner_failed", 0):
        raise RuntimeError(
            "the harness launched an OpenCode process instead of using history"
        )
    for event in (
        "recovery_worker_started",
        "recovery_worker_filed_pr",
        "recovery_worker_notified_parent",
    ):
        if activity.get(event) != 1:
            raise RuntimeError(
                f"expected one real {event} activity, got {activity.get(event, 0)}"
            )
    reviewer_invocation = reviewer["invocation"]
    reviewer_approvals = [
        row
        for row in read_json_lines(agent_log)
        if row.get("event") == "reviewer_approved"
        and row.get("pr_number") == leaf["pr_number"]
        and row.get("head_sha") == head_sha
    ]
    if (
        reviewer_invocation.get("runtime") != "codex"
        or reviewer_invocation.get("role") != "reviewer"
        or reviewer_invocation.get("trigger") != "review"
        or reviewer_invocation.get("pr_number") != leaf["pr_number"]
        or reviewer_invocation.get("head_sha") != head_sha
        or len(reviewer_approvals) != 1
        or reviewer_approvals[0].get("agent_id") != reviewer["agent_name"]
        or reviewer_approvals[0].get("model") != "gpt-6.1-sol"
        or reviewer_approvals[0].get("effort") != "medium"
    ):
        raise RuntimeError(
            "controller reviewer is not bound to the configured exact PR head"
        )

    root = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
    root_kani = root.get("slices", {}).get("kani", {})
    if not root_kani.get("dispatch_agent_id") or not root_kani.get(
        "dispatch_invocation_id"
    ):
        raise RuntimeError(
            "parent RunStore lacks its Kani controller invocation projection"
        )
    original_journal = retained_original_dispatch(repository, baseline)
    action_rows = json.loads(
        (root_dir / "kani/action-journal.json").read_text(encoding="utf-8")
    )
    resume_rows = [
        row
        for row in action_rows
        if row.get("operation") == "resume_blocked_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
    ]
    expected_resume_count = 2 if isinstance(historical_wrong_route, dict) else 1
    if len(resume_rows) != expected_resume_count:
        raise RuntimeError(
            "controller did not retain the expected immutable historical action and current "
            f"resume action: {resume_rows!r}"
        )
    if isinstance(historical_wrong_route, dict) and resume_rows[
        0
    ] != historical_wrong_route.get("action"):
        raise RuntimeError(
            "controller rewrote the confirmed historical Sol successor result"
        )
    init_log = repository / ".exo/logs/init.jsonl"
    init_records = read_json_lines(init_log)
    accepted_fingerprints = {
        record.get("resolved", {}).get("role_route_config_fingerprint")
        for record in init_records
        if isinstance(record.get("resolved"), dict)
        and isinstance(
            record.get("resolved", {}).get("role_route_config_fingerprint"), str
        )
    }
    if len(accepted_fingerprints) != 1:
        raise RuntimeError(
            "disposable init did not retain one consistent accepted route configuration fingerprint"
        )
    accepted_configuration_fingerprint = next(iter(accepted_fingerprints))
    resume_arguments = resume_rows[-1].get("arguments")
    if not isinstance(resume_arguments, dict):
        raise TypeError("confirmed resume journal lacks its accepted route arguments")
    expected_route = {
        "role": resume_arguments.get("role"),
        "harness": resume_arguments.get("harness"),
        "agent_type": resume_arguments.get("agent_type"),
        "model": resume_arguments.get("model"),
        "effort": resume_arguments.get("effort"),
        "configuration_fingerprint": resume_arguments.get(
            "route_configuration_fingerprint"
        ),
        "policy_fingerprint": resume_arguments.get("route_policy_fingerprint"),
        "manifest_digest": resume_arguments.get("route_manifest_digest"),
        "route_fingerprint": resume_arguments.get("route_fingerprint"),
        "dispatch_intent_id": resume_arguments.get("dispatch_intent_id"),
        "scope_id": OWNER_SLICE,
    }
    if (
        expected_route["role"] != "worker"
        or expected_route["harness"] != "codex/gpt-6-luna"
        or expected_route["agent_type"] != "codex"
        or expected_route["model"] != "gpt-6-luna"
        or expected_route["effort"] != "xhigh"
        or expected_route["configuration_fingerprint"]
        != accepted_configuration_fingerprint
        or any(
            not isinstance(expected_route[key], str) or not expected_route[key]
            for key in (
                "policy_fingerprint",
                "manifest_digest",
                "route_fingerprint",
                "dispatch_intent_id",
            )
        )
    ):
        raise RuntimeError(
            "resume request route does not match the accepted init configuration and Kani declaration"
        )
    terminal_gate_name = baseline.get("terminal_recovery_gate")
    recovery_evidence = (
        child.get("slices", {}).get(OWNER_SLICE, {}).get("recovery") or {}
    ).get("evidence") or {}
    terminal_gate = next(
        (
            row
            for row in child.get("gates", [])
            if row.get("name") == terminal_gate_name
        ),
        None,
    )
    route_authorization = recovery_evidence.get("configured_route_authorization")
    gate_authorization = resume_arguments.get("recovery_gate_authorization")
    if gate_approval_path is None:
        gate_rejection = recovery_evidence.get("terminal_owner_recovery_gate_rejection")
        if (
            not isinstance(terminal_gate, dict)
            or terminal_gate.get("status") != "approved"
            or not isinstance(gate_rejection, dict)
            or not isinstance(route_authorization, dict)
            or route_authorization.get("recovery_gate_authorization")
            != gate_authorization
            or not isinstance(gate_authorization, dict)
            or gate_authorization.get("status") != "approved"
        ):
            raise RuntimeError(
                "resume route authorization is not bound to the fixture's explicit fresh gate approval"
            )
    else:
        approval_observation = json.loads(
            gate_approval_path.read_text(encoding="utf-8")
        )
        expected_gate_authorization = approval_observation.get("authorization")
        if (
            approval_observation.get("gate_name") != terminal_gate_name
            or approval_observation.get("gate_status") != "approved"
            or not isinstance(expected_gate_authorization, dict)
            or expected_gate_authorization.get("gate_name") != terminal_gate_name
            or expected_gate_authorization.get("status") != "approved"
            or type(expected_gate_authorization.get("rejected_state_version"))
            is not int
            or type(expected_gate_authorization.get("approved_state_version"))
            is not int
            or expected_gate_authorization["approved_state_version"]
            <= expected_gate_authorization["rejected_state_version"]
            or gate_authorization != expected_gate_authorization
        ):
            raise RuntimeError(
                "resume route authorization does not match the captured explicit gate decision"
            )
    response_payload = (resume_rows[-1].get("result") or {}).get("result")
    if not isinstance(response_payload, dict):
        raise TypeError("confirmed resume journal lacks its typed response payload")
    response_agent = response_payload.get("agent")
    response_invocation = response_payload.get("invocation")
    identity_route = identity.get("role_route")
    invocation_route = successor.get("role_route")
    if (
        not isinstance(response_agent, dict)
        or not isinstance(response_invocation, dict)
        or response_agent.get("role_route") != expected_route
        or response_invocation.get("role_route") != expected_route
        or identity_route != expected_route
        or invocation_route != expected_route
    ):
        raise RuntimeError(
            "accepted route provenance differs across response receipts and durable owner records"
        )
    if isinstance(historical_wrong_route, dict):
        old_action = resume_rows[0]
        old_result = (old_action.get("result") or {}).get("result") or {}
        old_agent = old_result.get("agent") or {}
        old_invocation = old_result.get("invocation") or {}
        durable_history = historical_wrong_route.get("invocation") or {}
        if (
            old_action.get("arguments", {}).get("expected_invocation_id")
            != baseline["prior_invocation"]["invocation_id"]
            or old_action.get("arguments", {}).get("prior_generation") != 1
            or old_action.get("arguments", {}).get("desired_model") != "gpt-6-luna"
            or old_action.get("arguments", {}).get("desired_effort") != "xhigh"
            or "route_fingerprint" in old_action.get("arguments", {})
            or set(old_agent) & LEGACY_MODERN_MARKERS
            or set(old_invocation) & LEGACY_MODERN_MARKERS
            or old_result.get("chainlink_issue_id") != BLOCKED_ISSUE_ID
            or old_agent.get("agent_type") != "codex"
            or old_agent.get("invocation_id") != durable_history.get("invocation_id")
            or old_invocation.get("invocation_id") != durable_history.get("invocation_id")
            or old_invocation.get("trigger") != "resume_blocked"
            or old_invocation.get("runtime") != "codex"
            or old_invocation.get("outcome") != "started"
            or durable_history.get("generation") != 2
            or durable_history.get("model") != "gpt-6.1-sol"
            or durable_history.get("effort") != "xhigh"
            or durable_history.get("prior_invocation_id")
            != baseline["prior_invocation"]["invocation_id"]
            or durable_history.get("status") != "exited"
            or durable_history.get("exit_code") != 0
            or durable_history.get("exit_classification") != "one_shot_complete"
            or durable_history.get("pr_number") is not None
            or durable_history.get("head_sha") is not None
            or resume_arguments.get("expected_invocation_id")
            != durable_history.get("invocation_id")
            or resume_arguments.get("prior_generation") != 2
            or successor.get("generation") != 3
            or successor.get("prior_invocation_id")
            != durable_history.get("invocation_id")
        ):
            raise RuntimeError(
                "generation-three recovery did not preserve and supersede the exact Sol/xhigh generation-two history"
            )
    ledger = canonical_events(repository)
    legacy_invocation_id = None
    if isinstance(historical_wrong_route, dict):
        legacy_invocation_id = historical_wrong_route.get("invocation", {}).get(
            "invocation_id"
        )
        legacy_chronology = sorted(
            row.get("type")
            for row in ledger
            if row.get("invocation_id") == legacy_invocation_id
        )
        if legacy_chronology != [
            "agent.invocation.finished",
            "agent.invocation.started",
            "agent.resumed",
        ]:
            raise RuntimeError(
                "historical f285 chronology is not the old started/resumed/finished shape: "
                f"{legacy_chronology!r}"
            )
    owner_invocations = [
        row
        for row in ledger
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == baseline["owner_agent_name"]
    ]
    expected_owner_invocations = 3 if isinstance(historical_wrong_route, dict) else 2
    expected_codex_invocations = 2 if isinstance(historical_wrong_route, dict) else 1
    codex_invocation_ids = sorted(
        row.get("invocation_id")
        for row in owner_invocations
        if row.get("data", {}).get("runtime") == "codex"
    )
    codex_ids_match = legacy_invocation_id is None or codex_invocation_ids == sorted(
        [successor.get("invocation_id"), legacy_invocation_id]
    )
    if (
        len(owner_invocations) != expected_owner_invocations
        or sum(
            row.get("data", {}).get("runtime") == "opencode"
            for row in owner_invocations
        )
        != 1
        or sum(
            row.get("data", {}).get("runtime") == "codex" for row in owner_invocations
        )
        != expected_codex_invocations
        or not codex_ids_match
    ):
        raise RuntimeError(
            "owner history does not show the expected OpenCode predecessor, "
            f"legacy Sol successor and Codex successor: {owner_invocations!r}"
        )
    lifecycle = [
        row
        for row in ledger
        if row.get("type")
        in {
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
            "tl.dispatch_confirmed",
        }
        and isinstance(row.get("data"), dict)
        and row["data"].get("slice_id") == OWNER_SLICE
        and row["data"].get("dispatch_generation") == leaf.get("dispatch_generation")
    ]
    expected_events = {
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
    }
    if {row.get("type") for row in lifecycle} != expected_events or len(lifecycle) != 4:
        raise RuntimeError(
            f"controller dispatch lifecycle is incomplete or duplicated: {lifecycle!r}"
        )

    record = {
        "historical_dispatch_kind": baseline.get("historical_dispatch_kind"),
        "historical_wrong_route_successor": historical_wrong_route,
        "route_less_host_refusal": baseline.get("route_less_host_refusal"),
        "terminal_recovery_gate": baseline.get("terminal_recovery_gate"),
        "accepted_spawn_journal": baseline.get("accepted_spawn_journal"),
        "accepted_spawn_event": baseline.get("accepted_spawn_event"),
        "spawn_event_id": baseline.get("spawn_event_id"),
        "prior_invocation": baseline["prior_invocation"],
        "owner_agent_name": baseline["owner_agent_name"],
        "prior_invocation_id": prior["invocation_id"],
        "original_dispatch_journal": original_journal,
        "successor_invocation_id": successor["invocation_id"],
        "branch": baseline["branch"],
        "worktree": baseline["worktree"],
        "runtime": successor["runtime"],
        "model": successor["model"],
        "effort": successor["effort"],
        "accepted_route_configuration_fingerprint": accepted_configuration_fingerprint,
        "accepted_role_route": expected_route,
        "pr_number": leaf["pr_number"],
        "head_sha": head_sha,
        "reviewer_name": reviewer["agent_name"],
        "reviewer_invocation": reviewer_invocation,
        "reviewer_invocation_event_id": reviewer["event_id"],
        "successor_invocation_generation": successor["generation"],
        "owner_identity_sha256": digest(owner_dir / "identity.json"),
        "owner_history_lines": history_lines,
        "leaf_status": leaf["status"],
        "leaf_dispatch_generation": leaf["dispatch_generation"],
        "lifecycle_event_ids": [row.get("event_id") for row in lifecycle],
        "agent_activity": activity,
    }
    if baseline.get("historical_parent_stall_event_id") is not None:
        record.update(
            {
                "historical_parent_stall_event_id": baseline[
                    "historical_parent_stall_event_id"
                ],
                "historical_parent_stall_event_seq": baseline[
                    "historical_parent_stall_event_seq"
                ],
                "historical_parent_stall_event": baseline[
                    "historical_parent_stall_event"
                ],
                "historical_parent_stall_audit": baseline[
                    "historical_parent_stall_audit"
                ],
                "historical_parent_stall_controller_epoch": baseline[
                    "historical_parent_stall_controller_epoch"
                ],
                "historical_parent_stall_superseded_by_epoch": stall_audit[
                    "superseded_by_epoch"
                ],
            }
        )
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        record["rejected_request"] = original_journal
    output.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, sort_keys=True))


def _reviewer_invocations(
    repository: Path, pr_number: int, head_sha: str
) -> list[dict[str, Any]]:
    reviewers: list[dict[str, Any]] = []
    for row in canonical_events(repository):
        value = row.get("data")
        if (
            row.get("type") == "agent.invocation.started"
            and isinstance(value, dict)
            and value.get("role") == "reviewer"
            and value.get("trigger") == "review"
            and value.get("pr_number") == pr_number
            and value.get("head_sha") == head_sha
        ):
            reviewers.append(
                {
                    "agent_name": row.get("agent_id"),
                    "event_id": row.get("event_id"),
                    "invocation": value,
                }
            )
    return reviewers


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def event_counts(path: Path) -> dict[str, int]:
    rows = read_json_lines(path)
    return {
        name: sum(row.get("event") == name for row in rows)
        for name in sorted({row.get("event") for row in rows})
    }


def canonical_events(repository: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for segment in sorted(
            (repository / ".exo/ledger/segments").glob("segment-*.jsonl")
        )
        for line in segment.read_text(encoding="utf-8").splitlines()
    ]


def verify_repeat(repository: Path, evidence_path: Path) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    route_less_refusal = evidence.get("route_less_host_refusal")
    if (
        not isinstance(route_less_refusal, dict)
        or route_less_refusal.get("success") is not False
        or route_less_refusal.get("error")
        != "Invalid input: blocked-leaf controller recovery requires its accepted role route"
    ):
        raise RuntimeError(
            "fixture did not retain the compiled host missing-route refusal"
        )
    retained_launch_event = [
        row
        for row in canonical_events(repository)
        if row.get("event_id") == evidence.get("spawn_event_id")
    ]
    if evidence.get(
        "historical_dispatch_kind"
    ) == "accepted_terminal_provider_failure" and retained_launch_event != [
        evidence.get("accepted_spawn_event")
    ]:
        raise RuntimeError(
            "repeat init changed the accepted OpenCode launch ledger row"
        )
    if evidence.get("historical_parent_stall_event_id") is not None:
        stall_event_id = evidence["historical_parent_stall_event_id"]
        stall_rows = [
            row
            for row in canonical_events(repository)
            if row.get("event_id") == stall_event_id
        ]
        root_checkpoint = json.loads(
            (repository / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8")
        )
        kani_projection = root_checkpoint.get("slices", {}).get("kani", {})
        expected_stall_audit = dict(evidence.get("historical_parent_stall_audit") or {})
        expected_stall_audit.update(
            {
                "superseded_by_epoch": evidence.get(
                    "historical_parent_stall_superseded_by_epoch"
                ),
                "supersession_reason": "fresh_continue_reconciles_invocation_local_stall",
            }
        )
        if (
            stall_rows != [evidence.get("historical_parent_stall_event")]
            or kani_projection.get("status") == "parked"
            or kani_projection.get("park_cause") is not None
            or kani_projection.get("park_audit") != expected_stall_audit
        ):
            raise RuntimeError(
                "repeat continuation changed the stall reconciliation evidence"
            )
    owner_dir, _ = owner_for(repository)
    current_invocation = invocation(owner_dir)
    expected_successor_generation = evidence.get("successor_invocation_generation")
    if type(expected_successor_generation) is not int:
        expected_successor_generation = (
            int(evidence.get("prior_invocation", {}).get("generation", 1)) + 1
        )
    if retained_original_dispatch(repository, evidence) != evidence.get(
        "accepted_spawn_journal", evidence.get("rejected_request")
    ):
        raise RuntimeError("repeat init changed the original Kani dispatch record")
    action_rows = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    root_action_rows = json.loads(
        (repository / ".exo/tl-loop/root/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    rejected_controller_events = [
        row
        for row in [*action_rows, *root_action_rows]
        if row.get("operation") == "emit_controller_event"
        and row.get("status") == "rejected"
    ]
    if rejected_controller_events:
        raise RuntimeError(
            "repeat init retained rejected controller event calls: "
            f"{rejected_controller_events!r}"
        )
    resume_actions = [
        row
        for row in action_rows
        if row.get("operation") == "resume_blocked_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
    ]
    historical_wrong_route = evidence.get("historical_wrong_route_successor")
    expected_resume_count = 2 if isinstance(historical_wrong_route, dict) else 1
    if (
        len(resume_actions) != expected_resume_count
        or (resume_actions[-1].get("result") or {})
        .get("result", {})
        .get("agent", {})
        .get("invocation_id")
        != evidence["successor_invocation_id"]
    ):
        raise RuntimeError(
            "repeat init did not retain exactly one successful resume for the same successor"
        )
    expected_route = evidence.get("accepted_role_route")
    identity = json.loads((owner_dir / "identity.json").read_text(encoding="utf-8"))
    if isinstance(historical_wrong_route, dict) and resume_actions[
        0
    ] != historical_wrong_route.get("action"):
        raise RuntimeError(
            "repeat init changed the confirmed historical Sol successor result"
        )
    if (
        not isinstance(expected_route, dict)
        or identity.get("role_route") != expected_route
        or current_invocation.get("role_route") != expected_route
        or (resume_actions[-1].get("arguments") or {}).get(
            "route_configuration_fingerprint"
        )
        != evidence.get("accepted_route_configuration_fingerprint")
        or ((resume_actions[-1].get("result") or {}).get("result") or {})
        .get("agent", {})
        .get("role_route")
        != expected_route
        or ((resume_actions[-1].get("result") or {}).get("result") or {})
        .get("invocation", {})
        .get("role_route")
        != expected_route
    ):
        raise RuntimeError("repeat init changed the accepted route provenance")
    reviewer_actions = [
        row
        for row in action_rows
        if row.get("operation") == "spawn_reviewer"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
        and row.get("arguments", {}).get("pr_number") == evidence["pr_number"]
        and row.get("arguments", {}).get("head_sha") == evidence["head_sha"]
    ]
    if len(reviewer_actions) != 1 or (
        (reviewer_actions[0].get("result") or {}).get("result", {}).get("reviewer_name")
        != evidence["reviewer_name"]
    ):
        raise RuntimeError(
            "repeat init did not retain exactly one configured reviewer action for the same PR head"
        )
    if (
        current_invocation.get("invocation_id") != evidence["successor_invocation_id"]
        or current_invocation.get("runtime") != evidence["runtime"]
        or current_invocation.get("model") != evidence["model"]
        or current_invocation.get("effort") != evidence["effort"]
        or current_invocation.get("prior_invocation_id")
        != evidence["prior_invocation_id"]
        or current_invocation.get("generation") != expected_successor_generation
        or current_invocation.get("runtime_agent_id") != evidence["owner_agent_name"]
        or current_invocation.get("slice_id") != OWNER_SLICE
        or current_invocation.get("branch") != evidence["branch"]
        or Path(current_invocation.get("worktree", "")).resolve()
        != Path(evidence["worktree"]).resolve()
        or current_invocation.get("status") != "exited"
        or current_invocation.get("exit_code") != 0
        or current_invocation.get("exit_classification") != "one_shot_complete"
    ):
        raise RuntimeError(
            "repeat init replaced or corrupted the completed configured successor invocation"
        )
    history = repository / ".exo/events" / f"{evidence['owner_agent_name']}.jsonl"
    history_lines = history.read_text(encoding="utf-8").splitlines()
    prior_history = evidence["owner_history_lines"]
    assert history_lines[: len(prior_history)] == prior_history, (
        "repeat init rewrote or removed historical owner events"
    )
    additions = history_lines[len(prior_history) :]
    assert len(additions) <= 1, (
        f"repeat init appended unexpected owner events: {additions!r}"
    )
    if additions:
        delivery = json.loads(additions[0])
        data = delivery.get("data")
        assert delivery.get("type") == "agent.guidance.delivery"
        assert isinstance(data, dict)
        assert data.get("invocation_id") == evidence["successor_invocation_id"]
        assert data.get("outcome") == "success"
        assert data.get("channel") == "tmux_injection"
        evidence["owner_history_lines"] = history_lines
        evidence["owner_history_sha256"] = digest(history)
    reviewer_events = [
        row
        for row in canonical_events(repository)
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["reviewer_name"]
        and row.get("data", {}).get("role") == "reviewer"
        and row.get("data", {}).get("trigger") == "review"
        and row.get("data", {}).get("pr_number") == evidence["pr_number"]
        and row.get("data", {}).get("head_sha") == evidence["head_sha"]
    ]
    assert len(reviewer_events) == 1, (
        f"repeat init duplicated the reviewer invocation: {reviewer_events!r}"
    )
    assert (
        reviewer_events[0].get("event_id") == evidence["reviewer_invocation_event_id"]
    )
    assert digest(owner_dir / "identity.json") == evidence["owner_identity_sha256"]
    assert (
        len(
            [
                row
                for row in json.loads(
                    (repository / ".exo/published-heads.json").read_text(
                        encoding="utf-8"
                    )
                )["heads"]
                if row.get("author_agent") == evidence["owner_agent_name"]
                and row.get("slice_id") == OWNER_SLICE
            ]
        )
        == 1
    ), "repeat init duplicated the resumed owner's PR publication"
    assert (
        len(
            [
                row
                for row in read_json_lines(Path(os.environ["E2E_AGENT_LOG"]))
                if row.get("event") == "recovery_worker_filed_pr"
            ]
        )
        == 1
    ), "repeat init launched a duplicate resumed worker"
    assert (
        len(
            [
                row
                for row in read_json_lines(Path(os.environ["E2E_AGENT_LOG"]))
                if row.get("event") == "reviewer_approved"
                and row.get("pr_number") == evidence["pr_number"]
                and row.get("head_sha") == evidence["head_sha"]
            ]
        )
        == 1
    ), "repeat init duplicated review activity"
    events = canonical_events(repository)
    owner_invocations = [
        row
        for row in events
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["owner_agent_name"]
    ]
    historical_successor = evidence.get("historical_wrong_route_successor")
    expected_owner_invocations = (
        3 if isinstance(historical_successor, dict) else 2
    )
    assert len(owner_invocations) == expected_owner_invocations, (
        "expected the OpenCode predecessor, an optional legacy Codex successor "
        f"and one Codex invocation, got {len(owner_invocations)}"
    )
    reviewer_invocations = [
        row
        for row in events
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["reviewer_name"]
    ]
    assert len(reviewer_invocations) == 1, (
        f"repeat init created another reviewer invocation: {reviewer_invocations!r}"
    )
    assert (
        sum(
            row.get("data", {}).get("runtime") == "opencode"
            for row in owner_invocations
        )
        == 1
    )
    assert (
        sum(row.get("data", {}).get("runtime") == "codex" for row in owner_invocations)
        == (2 if isinstance(historical_successor, dict) else 1)
    )
    activity = event_counts(Path(os.environ["E2E_AGENT_LOG"]))
    historical = evidence.get("prior_invocation")
    assert isinstance(historical, dict)
    assert historical.get("runtime") == "opencode"
    assert historical.get("status") == "failed"
    assert historical.get("exit_classification") == "nonzero_exit"
    assert activity.get("historical_opencode_owner_failed", 0) == 0
    assert activity.get("recovery_worker_started") == 1
    assert activity.get("recovery_worker_filed_pr") == 1
    assert activity.get("recovery_worker_notified_parent") == 1
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "repeat init retained one OpenCode predecessor, one Codex successor, and one exact-head reviewer"
    )


def bind_child_controller_invocation(repository: Path) -> None:
    """Bind the seeded parent to the actual live Kani controller invocation."""
    from tl_loop.loop.driver import _phase_from_state
    from tl_loop.state.store import RunStore

    root_store = RunStore("root", repository / ".exo/tl-loop")
    state = root_store.load()
    current = state.slices.get("kani")
    if current is None or current.status.value != "spawned":
        raise RuntimeError("root Kani slice is not spawned")
    agent_dir = repository / ".exo/agents/kani"
    invocation = json.loads((agent_dir / "invocation.json").read_text(encoding="utf-8"))
    from tl_loop.loop.driver import _workspace_shutdown_recorded

    live_or_shutdown = invocation.get("status") == "running" or (
        invocation.get("status") == "killed"
        and (
            (
                invocation.get("exit_reason") == "workspace control-plane shutdown"
                and invocation.get("exit_classification")
                == "workspace_control_plane_shutdown"
            )
            or (
                invocation.get("exit_classification") == "missing_exit_marker"
                and _workspace_shutdown_recorded(
                    repository,
                    "kani",
                    str(invocation.get("invocation_id")),
                    int(invocation.get("generation") or 0),
                )
            )
        )
    )
    if (
        not live_or_shutdown
        or invocation.get("runtime_agent_id") != "kani"
        or invocation.get("slice_id") != "kani"
        or invocation.get("branch") != current.branch
        or Path(str(invocation.get("worktree", ""))).resolve()
        != Path(str(current.worktree)).resolve()
        or type(invocation.get("generation")) is not int
        or invocation["generation"] < 1
    ):
        raise RuntimeError(
            "live Kani controller invocation does not match the parent slice"
        )
    updated = replace(
        current,
        dispatch_invocation_id=invocation["invocation_id"],
        dispatch_generation=invocation["generation"],
    )
    if updated == current:
        print("Kani controller binding already current")
        return
    root_store.checkpoint(
        _phase_from_state(state),
        {**state.slices, "kani": updated},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        expected_revision=state.revision,
    )
    print(f"Kani controller binding adopted {invocation['invocation_id']}")


def reset_child_controller_dispatch(repository: Path) -> None:
    """Reopen the disposable child controller dispatch after a workspace restart."""
    from tl_loop.loop.driver import _phase_from_state
    from tl_loop.state.schema import SliceStatus
    from tl_loop.state.store import RunStore

    root_store = RunStore("root", repository / ".exo/tl-loop")
    state = root_store.load()
    current = state.slices.get("kani")
    if current is None:
        raise RuntimeError("root checkpoint has no Kani controller slice")
    reopened = replace(
        current, status=SliceStatus.PENDING, park_cause=None, recovery=None
    )
    root_store.checkpoint(
        _phase_from_state(state),
        {**state.slices, "kani": reopened},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    print("kani controller dispatch reopened for the next directed route")


def verify_repeated_transitions(
    repository: Path,
    baseline_path: Path,
    expected_path: Path,
) -> None:
    """Verify the compiled repeated-route continuation history end to end."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if not isinstance(expected, list) or not expected:
        raise RuntimeError("repeated-transition expectations are missing")
    journal = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    entries = [
        row
        for row in journal
        if row.get("operation") == "resume_blocked_leaf"
        and row.get("target") == OWNER_SLICE
    ]
    if len(entries) != len(expected) + 1:
        raise RuntimeError(
            f"expected {len(expected) + 1} linked resume actions, found {len(entries)}"
        )
    historical = baseline.get("historical_wrong_route_successor")
    if not isinstance(historical, dict):
        raise RuntimeError("baseline lacks the preserved f285 legacy successor")
    if entries[0] != historical.get("action"):
        raise RuntimeError("the retained legacy f285 action changed")
    if entries[0].get("status") != "confirmed":
        raise RuntimeError("the retained legacy f285 action is not confirmed")

    previous_id = historical.get("invocation", {}).get("invocation_id")
    previous_generation = historical.get("invocation", {}).get("generation")
    if (
        previous_id != "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
        or previous_generation != 2
    ):
        raise RuntimeError("baseline does not end at the exact f285 generation 2")
    intent_ids: set[str] = set()
    invocation_ids: set[str] = set()
    for index, route in enumerate(expected, start=1):
        entry = entries[index]
        arguments = entry.get("arguments") or {}
        result = entry.get("result") or {}
        payload = result.get("result") or {}
        successor = payload.get("invocation") or {}
        agent = payload.get("agent") or {}
        if entry.get("status") != "confirmed":
            raise RuntimeError(f"repeated transition {index} was not confirmed")
        if arguments.get("expected_invocation_id") != previous_id:
            raise RuntimeError(
                f"repeated transition {index} did not link to its predecessor"
            )
        if arguments.get("prior_generation") != previous_generation:
            raise RuntimeError(
                f"repeated transition {index} has a discontinuous generation"
            )
        for field in ("harness", "agent_type", "model", "effort"):
            if arguments.get(field) != route.get(field):
                raise RuntimeError(
                    f"repeated transition {index} selected {field}={arguments.get(field)!r}, "
                    f"expected {route.get(field)!r}"
                )
        if arguments.get("desired_harness") != route.get("harness"):
            raise RuntimeError(f"repeated transition {index} desired harness drifted")
        if arguments.get("desired_model") != route.get("model"):
            raise RuntimeError(f"repeated transition {index} desired model drifted")
        if arguments.get("desired_effort") != route.get("effort"):
            raise RuntimeError(f"repeated transition {index} desired effort drifted")
        intent_id = arguments.get("dispatch_intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise RuntimeError(f"repeated transition {index} lacks its intent identity")
        if intent_id in intent_ids:
            raise RuntimeError(f"repeated transition {index} reused an earlier intent")
        intent_ids.add(intent_id)
        invocation_id = successor.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise RuntimeError(
                f"repeated transition {index} lacks its actual invocation"
            )
        if invocation_id in invocation_ids:
            raise RuntimeError(
                f"repeated transition {index} reused an earlier invocation"
            )
        invocation_ids.add(invocation_id)
        next_arguments = (
            entries[index + 1].get("arguments") if index + 1 < len(entries) else None
        )
        actual_generation = (
            next_arguments.get("prior_generation")
            if isinstance(next_arguments, Mapping)
            else previous_generation + 1
        )
        if actual_generation != previous_generation + 1:
            raise RuntimeError(
                f"repeated transition {index} did not advance generation once"
            )
        if successor.get("trigger") != "resume_blocked":
            raise RuntimeError(f"repeated transition {index} was not a blocked resume")
        route_receipt = successor.get("role_route") or agent.get("role_route")
        if not isinstance(route_receipt, dict):
            raise RuntimeError(f"repeated transition {index} lacks its route receipt")
        if (
            successor.get("runtime") != route.get("agent_type")
            or route_receipt.get("model") != route.get("model")
            or route_receipt.get("effort") != route.get("effort")
        ):
            raise RuntimeError(
                f"repeated transition {index} actual route conflicts with intent"
            )
        for field, argument_key in (
            ("role", "role"),
            ("harness", "harness"),
            ("agent_type", "agent_type"),
            ("model", "model"),
            ("effort", "effort"),
            ("configuration_fingerprint", "route_configuration_fingerprint"),
            ("policy_fingerprint", "route_policy_fingerprint"),
            ("manifest_digest", "route_manifest_digest"),
            ("route_fingerprint", "route_fingerprint"),
            ("dispatch_intent_id", "dispatch_intent_id"),
            ("scope_id", "route_scope_id"),
        ):
            expected_value = (
                OWNER_SLICE if field == "scope_id" else arguments.get(argument_key)
            )
            if route_receipt.get(field) != expected_value:
                raise RuntimeError(
                    f"repeated transition {index} route receipt field {field} conflicts"
                )
        if (agent.get("id") or agent.get("agent_id")) != baseline.get(
            "owner_agent_name"
        ):
            raise RuntimeError(f"repeated transition {index} changed the logical owner")
        previous_id = invocation_id
        previous_generation = actual_generation

    if previous_id is None or previous_generation is None:
        raise RuntimeError("repeated transition history has no final invocation")
    owner_dir = Path(baseline["owner_dir"])
    current = invocation(owner_dir)
    if (
        current.get("invocation_id") != previous_id
        or current.get("generation") != previous_generation
        or current.get("status") != "exited"
        or current.get("exit_code") != 0
        or current.get("exit_classification") != "one_shot_complete"
        or current.get("pr_number") is not None
        or current.get("head_sha") is not None
    ):
        raise RuntimeError(
            "final owner invocation is not the retained terminal no-PR successor"
        )
    final_route = expected[-1]
    if (
        current.get("runtime") != final_route.get("agent_type")
        or current.get("model") != final_route.get("model")
        or current.get("effort") != final_route.get("effort")
    ):
        raise RuntimeError(
            "final owner invocation route does not match the directed route"
        )
    if not isinstance(current.get("role_route"), dict):
        raise RuntimeError("final owner invocation has no durable route receipt")

    child = json.loads(
        (repository / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8")
    )
    leaf = child.get("slices", {}).get(OWNER_SLICE, {})
    if (
        leaf.get("dispatch_invocation_id") != previous_id
        or leaf.get("dispatch_generation") != previous_generation
        or leaf.get("dispatch_agent_id") != baseline.get("owner_agent_name")
        or leaf.get("pr_number") is not None
        or leaf.get("publication") is not None
    ):
        raise RuntimeError(
            "child checkpoint did not adopt the final repeated-route successor"
        )
    recovery = leaf.get("recovery") or {}
    evidence = recovery.get("evidence") or {}
    if (
        recovery.get("phase") != "human_gate"
        or evidence.get("attempt") != len(expected) + 1
        or evidence.get("invocation_id") != previous_id
    ):
        raise RuntimeError(
            "final repeated-route human gate is not bound to the final successor"
        )

    events = read_json_lines(Path(os.environ["E2E_AGENT_LOG"]))
    starts = [
        event
        for event in events
        if event.get("event") == "repeated_route_worker_started"
    ]
    if len(starts) != len(expected):
        raise RuntimeError(
            f"expected one shim launch per repeated route, found {len(starts)}"
        )
    for index, route in enumerate(expected, start=1):
        attempt = index + 1
        matching = [event for event in starts if event.get("attempt") == attempt]
        if len(matching) != 1:
            raise RuntimeError(
                f"expected one shim start for attempt {attempt}, found {len(matching)}"
            )
        event = matching[0]
        if (
            event.get("harness") != route.get("agent_type")
            or event.get("model") != route.get("model")
            or event.get("effort") != route.get("effort")
        ):
            raise RuntimeError(
                f"shim argv for attempt {attempt} did not match the directed route"
            )
    blocked = [
        event
        for event in events
        if event.get("event") == "repeated_route_worker_blocked"
    ]
    if len(blocked) != len(expected):
        raise RuntimeError(
            f"expected one blocked lifecycle per repeated route, found {len(blocked)}"
        )

    publications_path = repository / ".exo/published-heads.json"
    if publications_path.is_file():
        publications = json.loads(publications_path.read_text(encoding="utf-8")).get(
            "heads", []
        )
        owned = [
            row
            for row in publications
            if row.get("author_agent") == baseline.get("owner_agent_name")
            or row.get("slice_id") == OWNER_SLICE
        ]
        if owned:
            raise RuntimeError("repeated-route transitions published a worker PR")

    root = json.loads(
        (repository / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8")
    )
    architecture = root.get("slices", {}).get("architecture", {})
    if (
        architecture.get("status") != "merged"
        or (architecture.get("post_merge") or {}).get("phase") != "complete"
    ):
        raise RuntimeError(
            "repeated-route transitions changed the completed sibling receipt"
        )

    summary = {
        "historical_invocation": historical.get("invocation", {}).get("invocation_id"),
        "historical_generation": historical.get("invocation", {}).get("generation"),
        "transitions": expected,
        "successor_invocations": sorted(invocation_ids),
        "final_invocation": previous_id,
        "final_generation": previous_generation,
        "final_attempt": len(expected) + 1,
        "journal_entries": len(entries),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "seed",
            "seed-terminal",
            "seed-wrong-route-successor",
            "verify-controller",
            "verify-repeat",
            "verify-repeated",
            "reset-child-controller",
            "bind-child-controller",
        ),
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gate-approval", type=Path)
    parser.add_argument("--historical-parent-stall", action="store_true")
    parser.add_argument("--expected", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "seed":
        if arguments.output is None:
            parser.error("seed requires --output")
        seed(arguments.repo, arguments.output)
    elif arguments.mode == "seed-terminal":
        if arguments.output is None:
            parser.error("seed-terminal requires --output")
        seed_terminal_provider_failure(
            arguments.repo,
            arguments.output,
            seed_parent_stall=arguments.historical_parent_stall,
        )
    elif arguments.mode == "seed-wrong-route-successor":
        if arguments.baseline is None:
            parser.error("seed-wrong-route-successor requires --baseline")
        seed_confirmed_wrong_route_successor(arguments.repo, arguments.baseline)
    elif arguments.mode == "verify-controller":
        if arguments.baseline is None or arguments.output is None:
            parser.error("verify-controller requires --baseline and --output")
        verify_controller(
            arguments.repo,
            arguments.baseline,
            arguments.output,
            arguments.gate_approval,
        )
    elif arguments.mode == "reset-child-controller":
        reset_child_controller_dispatch(arguments.repo)
    elif arguments.mode == "bind-child-controller":
        bind_child_controller_invocation(arguments.repo)
    elif arguments.mode == "verify-repeat":
        if arguments.baseline is None:
            parser.error("verify-repeat requires --baseline")
        verify_repeat(arguments.repo, arguments.baseline)
    else:
        if arguments.baseline is None or arguments.expected is None:
            parser.error("verify-repeated requires --baseline and --expected")
        verify_repeated_transitions(
            arguments.repo, arguments.baseline, arguments.expected
        )


if __name__ == "__main__":
    main()
