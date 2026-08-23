"""Authenticated recovery-command validation and idempotency coverage."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.fsm.recovery import RecoveryPhase, transition_recovery
from tl_loop.loop.recovery_control import (
    HumanAuthorization,
    PolicyAuthorization,
    RecoveryCommandError,
    ResumeRecoveryRequest,
    execute_recovery_command,
)
from tl_loop.state.schema import GateStatus
from tl_loop.state.store import RunStore, create


def test_policy_retry_is_durable_and_idempotent(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    request = _request("retry-1", "retry", generation=2)

    first = execute_recovery_command(tmp_path, request)
    second = execute_recovery_command(tmp_path, request)

    assert first == second
    assert first["recovery_phase"] == "resume_intended"
    assert store.load().slices["leaf"].recovery.phase is RecoveryPhase.RESUME_INTENDED
    journal = (store.run_dir / "action-journal.json").read_text(encoding="utf-8")
    assert journal.count('"operation":"recovery_command"') == 1


def test_policy_cannot_authorize_scope_approval(tmp_path: Path) -> None:
    _store(tmp_path)
    request = _request("scope-1", "approve_scope", generation=2)

    with pytest.raises(RecoveryCommandError, match="cannot authorize"):
        execute_recovery_command(tmp_path, request)


def test_stale_invocation_generation_fails_closed(tmp_path: Path) -> None:
    _store(tmp_path)
    request = _request("stale-1", "retry", generation=1)

    with pytest.raises(RecoveryCommandError, match="generation changed"):
        execute_recovery_command(tmp_path, request)


def test_human_scope_approval_requires_current_gate_revision(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    current = state.slices["leaf"]
    gated = replace(
        current,
        recovery=transition_recovery(
            current.recovery,
            RecoveryPhase.HUMAN_GATE,
            next_action="open_human_gate",
            entered_at=20.0,
        ),
    )
    store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": gated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    state = store.set_gate("scope-gate", GateStatus.PENDING)
    request = ResumeRecoveryRequest(
        run_id="run",
        slice_id="leaf",
        expected_invocation_id="inv",
        expected_generation=2,
        expected_worktree_fingerprint="fp",
        action="approve_scope",
        authorization=HumanAuthorization("scope-gate", state.revision),
        idempotency_key="scope-2",
    )

    result = execute_recovery_command(tmp_path, request)

    assert result["recovery_phase"] == "resume_intended"


def test_inspect_returns_recovery_without_mutating_journal(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    request = _request("inspect-1", "inspect", generation=2)

    result = execute_recovery_command(tmp_path, request)

    assert result["status"] == "observed"
    assert not (store.run_dir / "action-journal.json").exists()


def _request(key: str, action: str, *, generation: int) -> ResumeRecoveryRequest:
    return ResumeRecoveryRequest(
        run_id="run",
        slice_id="leaf",
        expected_invocation_id="inv",
        expected_generation=generation,
        expected_worktree_fingerprint="fp",
        action=action,  # type: ignore[arg-type]
        authorization=PolicyAuthorization("external_dependency", 0),
        idempotency_key=key,
    )


def _store(tmp_path: Path) -> tuple[RunStore, object]:
    record = {
        "id": "leaf",
        "status": "spawned",
        "paths": ["src/leaf.py"],
        "depends_on": [],
        "base_ref": "main",
        "test_plan": [],
        "agent_type": "codex",
        "model": "test",
        "branch": "task/leaf",
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 1,
        "verdict": None,
        "dispatch_intent_id": "intent",
        "dispatch_agent_id": "agent-leaf",
        "dispatch_authoritative_event_seq": 1,
        "recovery": {
            "cause": "external_dependency",
            "phase": "diagnosing",
            "recovery_round": 0,
            "next_action": "diagnose",
            "owner_run_id": "run",
            "entered_at": 10.0,
            "slice_attempt": 1,
            "owner_agent_id": "agent-leaf",
            "invocation_generation": 2,
            "plan_revision": 0,
            "evidence": {"invocation_id": "inv", "worktree_fingerprint": "fp"},
            "probe_count": 0,
        },
    }
    create(
        "run",
        {
            "fsm": {"phase": "tl_waiting", "waiting": ["leaf"]},
            "slices": {"leaf": record},
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=tmp_path / ".exo" / "tl-loop",
    )
    store = RunStore("run", tmp_path / ".exo" / "tl-loop")
    return store, store.load()
