"""Coverage for evidence-gated wave-boundary refinement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.harness.refine import (
    RefinementBoundaryError,
    RefinementConfig,
    RefinementProposal,
    RefinementTrigger,
    UnevidencedProposal,
    maybe_refine,
)
from tl_loop.select.learned_policy import DispatchPolicyStore
from tl_loop.state.schema import (
    BudgetLedger,
    EventCursor,
    FSMState,
    GateState,
    RunState,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / ".exo/harness_policy.toml"


def test_single_failure_does_not_trigger_refinement(tmp_path: Path) -> None:
    result = maybe_refine(
        _state(TLPhase.TLDone),
        [{"run_seq": 1, "task_class": "focused_slice", "outcome": "failure"}],
        store=_store(tmp_path),
    )

    assert not result.changed
    assert result.proposals == ()


def test_repeated_failure_triggers_and_cites_real_sequences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = maybe_refine(
        _state(TLPhase.TLAllMerged),
        [
            {"run_seq": 11, "task_class": "focused_slice", "outcome": "failure"},
            {"run_seq": 12, "task_class": "focused_slice", "outcome": "failed"},
        ],
        store=store,
    )

    assert result.changed
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.trigger is RefinementTrigger.REPEATED_FAILURE
    assert proposal.evidence_seqs == (11, 12)
    assert result.policy is not None
    assert result.policy.evidence["decomposition_heuristics:focused_slice"] == (11, 12)
    assert result.policy.history[-1].trigger == "refinement:repeated_failure"


def test_unevidenced_proposal_is_refused() -> None:
    with pytest.raises(UnevidencedProposal, match="requires event sequence evidence"):
        RefinementProposal(
            RefinementTrigger.REPEATED_FAILURE,
            "decomposition_heuristics",
            "focused_slice",
            "missing evidence",
            (),
        )


def test_mid_wave_refinement_is_refused() -> None:
    with pytest.raises(RefinementBoundaryError, match="wave boundary"):
        maybe_refine(
            _state(TLPhase.TLWaiting),
            [],
            store=None,
        )


def test_reusable_tactic_and_repeated_role_are_learned(tmp_path: Path) -> None:
    tactic_store = _store(tmp_path / "tactic")
    tactic_result = maybe_refine(
        _state(TLPhase.TLDone),
        [
            {
                "run_seq": 21,
                "task_class": "focused_slice",
                "outcome": "success",
                "tactic": "narrow-retry",
                "role": "worker",
                "harness": "codex/gpt-luna",
            },
            {
                "run_seq": 22,
                "task_class": "focused_slice",
                "outcome": "success",
                "tactic": "narrow-retry",
                "role": "worker",
                "harness": "codex/gpt-luna",
            },
        ],
        store=tactic_store,
    )

    assert any(
        proposal.trigger is RefinementTrigger.RESOLVED_TACTIC
        for proposal in tactic_result.proposals
    )
    assert tactic_result.policy is not None
    assert tactic_result.policy.repair_patterns["narrow-retry"]["worker"] == ("codex/gpt-luna",)

    role_store = _store(tmp_path / "role")
    role_result = maybe_refine(
        _state(TLPhase.TLDone),
        [
            {
                "run_seq": 31,
                "type": "agent.spawned",
                "task_class": "focused_slice",
                "role": "worker",
                "harness": "codex/gpt-luna",
            },
            {
                "run_seq": 32,
                "type": "agent.spawned",
                "task_class": "focused_slice",
                "role": "worker",
                "harness": "codex/gpt-luna",
            },
        ],
        store=role_store,
    )

    assert any(
        proposal.trigger is RefinementTrigger.REPEATED_ROLE for proposal in role_result.proposals
    )
    assert role_result.policy is not None
    assert role_result.policy.task_class_preferences["focused_slice"]["worker"] == (
        "codex/gpt-luna",
    )


def test_behavior_policy_and_capability_observations_are_evidence_backed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    result = maybe_refine(
        _state(TLPhase.TLFailed),
        [
            {
                "run_seq": 41,
                "task_class": "focused_slice",
                "behavior_policy": "keep retries bounded",
                "harness": "codex/gpt-luna",
                "role": "worker",
                "outcome": "success",
            },
            {
                "run_seq": 42,
                "task_class": "focused_slice",
                "behavior_policy": "keep retries bounded",
                "harness": "codex/gpt-luna",
                "role": "worker",
                "outcome": "failure",
            },
        ],
        store=store,
        config=RefinementConfig(min_occurrences=2, min_capability_observations=2),
    )

    assert any(
        proposal.trigger is RefinementTrigger.BEHAVIOR_POLICY for proposal in result.proposals
    )
    assert result.policy is not None
    capability = result.policy.capability_observations["codex/gpt-luna"]
    assert (capability.passed, capability.failed) == (1, 1)
    assert capability.evidence_seqs == (41, 42)
    assert result.policy.evidence["decomposition_heuristics:focused_slice"] == (41, 42)


def test_policy_rejects_learned_entry_without_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document = store.load()
    raw = {
        "version": document.version,
        "revision": document.revision,
        "decomposition_heuristics": {"focused_slice": ["without proof"]},
        "task_class_preferences": {},
        "repair_patterns": {},
        "evidence": {},
        "capability_observations": {},
        "history": [],
    }
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    from tl_loop.select.learned_policy import load_learned_policy

    with pytest.raises(ValueError, match="missing evidence"):
        load_learned_policy(path, policy_path=POLICY_PATH)


def _store(path: Path) -> DispatchPolicyStore:
    return DispatchPolicyStore(
        path / "dispatch-policy.json",
        policy_path=POLICY_PATH,
        snapshot_dir=path / "snapshots",
    )


def _state(phase: TLPhase) -> RunState:
    return RunState(
        version=1,
        revision=0,
        run_id="run-refine",
        fsm=FSMState(phase, ()),
        slices={},
        budgets=BudgetLedger(tokens=0, wall_seconds=0),
        gates=tuple[GateState](),
        events=EventCursor(last_consumed_offset=0),
    )
