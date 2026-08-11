"""Coverage for the durable learned dispatch-policy store."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from tl_loop.select.agent_type import SelectionLedger, select_agent_type
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.learned_policy import (
    DispatchPolicyStore,
    LearnedPolicyInvalid,
    default_document,
    load_learned_policy,
    validate_learned_policy,
)
from tl_loop.select.policy import load_policy, validate_policy
from tl_loop.state.schema import SliceState, SliceStatus, Verdict

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / ".exo/harness_policy.toml"
CAPABILITIES = CapabilityMap(
    {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
)


def test_learned_harness_outside_allowlist_is_rejected() -> None:
    document = default_document()
    document["task_class_preferences"] = {"focused_slice": {"worker": ["outside/human-policy"]}}

    with pytest.raises(LearnedPolicyInvalid, match="not present in allow"):
        validate_learned_policy(document, load_policy(POLICY_PATH))


def test_store_snapshots_mutations_and_rolls_back_exact_payload(tmp_path: Path) -> None:
    policy_path = tmp_path / "dispatch-policy.json"
    snapshots = tmp_path / "snapshots"
    store = DispatchPolicyStore(
        policy_path,
        policy_path=POLICY_PATH,
        snapshot_dir=snapshots,
    )

    updated = store.mutate(
        lambda document: {
            **document,
            "decomposition_heuristics": {"focused_slice": ["keep boundary narrow"]},
            "task_class_preferences": {
                "focused_slice": {
                    "worker": ["claude/sonnet", "codex/gpt-luna"],
                }
            },
            "repair_patterns": {
                "no_go": {"worker": ["claude/sonnet"]},
            },
        },
        trigger="repeated focused-slice repair",
        evidence={
            "decomposition_heuristics:focused_slice": [1, 2],
            "task_class_preferences:focused_slice:worker": [1, 2],
            "repair_patterns:no_go:worker": [1, 2],
        },
    )

    assert updated.revision == 1
    assert json.loads((snapshots / "0.json").read_text()) == default_document()
    assert store.history()[0].trigger == "repeated focused-slice repair"

    restored = store.rollback(0)

    assert restored.revision == 2
    assert restored.decomposition_heuristics == {}
    assert restored.task_class_preferences == {}
    assert restored.repair_patterns == {}
    assert restored.evidence == {}
    assert restored.capability_observations == {}
    assert store.history()[-1].trigger == "rollback:0"
    assert load_learned_policy(policy_path, policy_path=POLICY_PATH) == restored


def test_selector_keeps_allowlist_confinement_with_adversarial_learned_order() -> None:
    policy = load_policy(POLICY_PATH)
    valid = validate_learned_policy(default_document(), policy)
    adversarial = replace(
        valid,
        task_class_preferences=MappingProxyType(
            {
                "focused_slice": {
                    "worker": ("outside/human-policy", "claude/sonnet"),
                }
            }
        ),
    )

    choice = select_agent_type(
        _slice(),
        "worker",
        SelectionLedger(),
        policy,
        CAPABILITIES,
        adversarial,
    )

    assert choice is not None
    assert choice.harness in policy.roles["worker"].allow
    assert choice.harness == "codex/gpt-luna"


def test_selector_uses_learned_order_for_equal_cost_rank() -> None:
    role = {
        "allow": ["codex/gpt-luna", "claude/sonnet"],
        "cost_rank": {"codex/gpt-luna": 1, "claude/sonnet": 1},
        "token_budget": 120000,
        "per_harness_budget": {},
        "escalate_after_attempts": 1,
    }
    policy = validate_policy(
        {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}
    )
    document = default_document()
    document["task_class_preferences"] = {
        "focused_slice": {"worker": ["claude/sonnet", "codex/gpt-luna"]}
    }
    document["evidence"] = {"task_class_preferences:focused_slice:worker": [1, 2]}
    learned = validate_learned_policy(document, policy)

    choice = select_agent_type(_slice(), "worker", SelectionLedger(), policy, CAPABILITIES, learned)

    assert choice is not None
    assert choice.harness == "claude/sonnet"


def _slice() -> SliceState:
    return SliceState(
        id="task",
        status=SliceStatus.PENDING,
        paths=("src/task.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("pytest",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=0,
        verdict=Verdict.GO,
    )


__all__ = [
    "test_learned_harness_outside_allowlist_is_rejected",
    "test_selector_keeps_allowlist_confinement_with_adversarial_learned_order",
    "test_selector_uses_learned_order_for_equal_cost_rank",
    "test_store_snapshots_mutations_and_rolls_back_exact_payload",
]
