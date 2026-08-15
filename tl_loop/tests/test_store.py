"""Checkpoint creation, reconstruction, and corruption checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.state.schema import (
    BudgetLedger,
    FSMState,
    GateStatus,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import (
    CorruptCheckpoint,
    RunStore,
    WorktreeClaimError,
    create,
    load,
    resume,
)


def test_mid_wave_resume_reconstructs_exact_local_state(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    fsm = FSMState(TLPhase.TLWaiting, ("in-review", "spawned"))
    slices = {
        "merged": _slice("merged", SliceStatus.MERGED, "src/merged.py"),
        "in-review": _slice("in-review", SliceStatus.IN_REVIEW, "src/review.py"),
        "spawned": _slice("spawned", SliceStatus.SPAWNED, "src/spawned.py"),
    }
    budgets = BudgetLedger(tokens=321, wall_seconds=45)

    checkpointed = store.checkpoint(fsm, slices, budgets, offset=17)
    loaded = load(store.path)
    resumed = resume("run-1", root_dir=tmp_path)

    assert checkpointed.fsm == fsm
    assert loaded.fsm == fsm
    assert dict(loaded.slices) == slices
    assert resumed.fsm == fsm
    assert dict(resumed.slices) == slices
    assert resumed.budgets == budgets
    assert resumed.offset == 17
    assert loaded.revision == 1


def test_legacy_checkpoint_defaults_new_review_state(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("in-review",)),
        {"in-review": _slice("in-review", SliceStatus.IN_REVIEW, "src/review.py")},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    record = document["slices"]["in-review"]
    for key in ("review_findings", "ci_state", "reviewer_attempt", "repair_attempts"):
        record.pop(key)
    store.path.write_text(json.dumps(document), encoding="utf-8")

    restored = load(store.path).slices["in-review"]
    assert restored.review_findings == {}
    assert restored.ci_state == {}
    assert restored.reviewer_attempt == {}
    assert restored.repair_attempts == 0


def test_answer_gate_requires_an_existing_gate(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)

    with pytest.raises(ValueError, match="does not exist"):
        store.answer_gate("missing", GateStatus.APPROVED)

    store.set_gate("review")
    answered = store.answer_gate("review", GateStatus.APPROVED)
    assert answered.gates[0].status is GateStatus.APPROVED


def test_load_rejects_waiting_slice_with_terminal_status(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("merged",)),
        {"merged": _slice("merged", SliceStatus.SPAWNED, "src/merged.py")},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["slices"]["merged"]["status"] = SliceStatus.MERGED.value
    store.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CorruptCheckpoint, match="waiting set is inconsistent"):
        load(store.path)

    assert document["fsm"] == {"phase": TLPhase.TLWaiting.value, "waiting": ["merged"]}


def _slice(slice_id: str, status: SliceStatus, path: str) -> SliceState:
    return SliceState(
        id=slice_id,
        status=status,
        paths=(path,),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-5",
        branch=f"task/{slice_id}",
        worktree=f".worktrees/{slice_id}",
        pr_number=42 if status is SliceStatus.IN_REVIEW else None,
        reviewed_head="abc123" if status in {SliceStatus.IN_REVIEW, SliceStatus.MERGED} else None,
        review_findings={
            "abc123": (
                {
                    "severity": "info",
                    "path": path,
                    "rationale": "covered",
                },
            )
        },
        ci_state={"abc123": "success"},
        reviewer_attempt={"abc123": 2},
        repair_attempts=3,
        attempts=1,
        verdict=Verdict.GO if status is SliceStatus.MERGED else None,
        dispatch_intent_id="store-intent-1" if status is SliceStatus.SPAWNED else None,
        dispatch_agent_id="agent-spawned" if status is SliceStatus.SPAWNED else None,
        dispatch_authoritative_event_seq=1 if status is SliceStatus.SPAWNED else None,
    )


def test_live_run_cannot_claim_an_owned_worktree_twice(tmp_path: Path) -> None:
    worktree = str(tmp_path / "shared-worktree")
    create("first", {"owner_branch": "main", "owner_worktree": worktree}, root_dir=tmp_path)

    with pytest.raises(WorktreeClaimError, match="already claimed"):
        create(
            "second", {"owner_branch": "main.second", "owner_worktree": worktree}, root_dir=tmp_path
        )
