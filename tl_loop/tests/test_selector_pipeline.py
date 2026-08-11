"""Hermetic M4 selector pipeline and bounded-property coverage."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tl_loop.select.agent_type import SelectionFailure, SelectionLedger, select_agent_type
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty, classify_task
from tl_loop.select.ledger import charge_spawn
from tl_loop.select.model import ModelCatalog, ModelResolutionError, select_model
from tl_loop.select.policy import HarnessPolicy, load_policy, validate_policy
from tl_loop.state.schema import SliceState, SliceStatus, Verdict

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tl_loop" / "tests" / "fixtures"
CAPABILITIES = CapabilityMap(
    {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
)
FULL_CATALOG = ModelCatalog.from_fixture(FIXTURES / "selector_catalog_full.json")
DEGRADED_CATALOG = ModelCatalog.from_fixture(FIXTURES / "selector_catalog_degraded.json")


def test_cheap_only_policy_never_selects_expensive_harness() -> None:
    policy = _policy("selector_policy_cheap_only.toml")
    choice = select_agent_type(_slice(), "worker", SelectionLedger(), policy, CAPABILITIES)

    assert choice is not None
    assert choice.harness == "codex/gpt-luna"
    assert choice.candidate_set == ("codex/gpt-luna",)


def test_escalation_requires_no_go_threshold_or_hard_classification() -> None:
    policy = _policy("selector_policy_cheap_with_escalation.toml")
    before_threshold = _slice(agent_type="codex/gpt-luna", verdict=Verdict.NO_GO, attempts=0)
    after_threshold = _slice(agent_type="codex/gpt-luna", verdict=Verdict.NO_GO, attempts=1)
    hard = _slice(paths=("proto/events.proto",))

    first_choice = select_agent_type(
        before_threshold, "worker", SelectionLedger(), policy, CAPABILITIES
    )
    escalated_choice = select_agent_type(
        after_threshold, "worker", SelectionLedger(), policy, CAPABILITIES
    )
    hard_choice = select_agent_type(hard, "worker", SelectionLedger(), policy, CAPABILITIES)

    assert first_choice is not None
    assert first_choice.harness == "codex/gpt-luna"
    assert escalated_choice is not None
    assert escalated_choice.harness == "claude/sonnet"
    assert escalated_choice.reason == "escalated_after_no_go"
    assert hard_choice is not None
    assert hard_choice.harness == "claude/sonnet"
    assert hard_choice.reason == "hard_classification"


def test_per_harness_exhaustion_keeps_other_candidate_selectable() -> None:
    policy = _policy("selector_policy_cheap_with_escalation.toml")
    ledger = SelectionLedger(harness_spent={"codex/gpt-luna": 80000})

    choice = select_agent_type(_slice(), "worker", ledger, policy, CAPABILITIES)

    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.candidate_set == ("claude/sonnet",)


def test_over_constrained_fixture_refuses_even_a_trivial_slice() -> None:
    policy = _policy("selector_policy_over_constrained.toml")

    choice = select_agent_type(_slice(), "worker", SelectionLedger(), policy, CAPABILITIES)

    assert choice is None
    assert _selection_failure(_slice(), SelectionLedger(), policy) is SelectionFailure.OVER_BUDGET


def test_total_exhaustion_returns_over_budget_for_needs_human_parking() -> None:
    policy = _policy("selector_policy_cheap_with_escalation.toml")
    ledger = SelectionLedger(role_spent={"worker": 120000})

    choice = select_agent_type(_slice(), "worker", ledger, policy, CAPABILITIES)

    assert choice is None
    assert (
        _selection_failure(_slice(), ledger, policy) is SelectionFailure.OVER_BUDGET
    )


def test_full_slice_to_charge_pipeline_has_an_auditable_ledger() -> None:
    policy = _policy("selector_policy_cheap_with_escalation.toml")
    slice_state = _slice()
    classification = classify_task(slice_state)

    choice = select_agent_type(
        slice_state, "worker", SelectionLedger(), policy, CAPABILITIES
    )
    assert choice is not None
    model = select_model(choice.harness, FULL_CATALOG, "gpt-5.5")

    charged = charge_spawn(
        {"tokens": 0, "wall_seconds": 0}, choice, slice_state
    )

    assert classification == (Difficulty.TRIVIAL, "focused_slice")
    assert model.model_id == "gpt-5.5"
    assert charged == {
        "tokens": 0,
        "wall_seconds": 0,
        "role_spent": {},
        "harness_spent": {},
        "role_reserved": {"worker": 250},
        "harness_reserved": {"codex/gpt-luna": 250},
        "charges": [
            {
                "slice_id": "task",
                "attempt": 1,
                "role": "worker",
                "harness": "codex/gpt-luna",
                "estimated_tokens": 250,
                "actual": "unknown",
                "delta_tokens": None,
                "warning": False,
                "reconciled": False,
            }
        ],
    }


def test_degraded_catalog_fails_closed_for_escalated_harness() -> None:
    policy = _policy("selector_policy_cheap_with_escalation.toml")
    hard = _slice(paths=("proto/events.proto",))
    choice = select_agent_type(hard, "worker", SelectionLedger(), policy, CAPABILITIES)

    assert choice is not None
    with pytest.raises(ModelResolutionError, match="no models available"):
        select_model(choice.harness, DEGRADED_CATALOG, None)


def test_randomized_policies_and_slices_remain_bounded() -> None:
    rng = random.Random(688)
    harnesses = ("codex/gpt-luna", "claude/sonnet")
    path_options = (
        ("src/task.py",),
        ("src/a.py", "src/b.py"),
        ("proto/events.proto",),
        ("src/worker.py", "src/runtime.rs"),
    )
    test_plan_options = (("pytest",), (), ("one", "two", "three", "four"))

    for _ in range(250):
        policy = _random_policy(rng, harnesses)
        slice_state = _slice(
            paths=rng.choice(path_options),
            test_plan=rng.choice(test_plan_options),
        )
        choice = select_agent_type(
            slice_state, "worker", SelectionLedger(), policy, CAPABILITIES
        )

        if choice is None:
            continue
        role = policy.roles["worker"]
        assert choice.harness in role.allow
        assert choice.harness in choice.candidate_set
        assert choice.estimated_cost <= role.token_budget
        harness_limit = role.per_harness_budget.get(choice.harness)
        if harness_limit is not None:
            assert choice.estimated_cost <= harness_limit


def _policy(filename: str) -> HarnessPolicy:
    return load_policy(FIXTURES / filename)


def _selection_failure(
    slice_state: SliceState, ledger: SelectionLedger, policy: HarnessPolicy
) -> SelectionFailure:
    from tl_loop.select.agent_type import selection_failure

    return selection_failure(slice_state, "worker", ledger, policy, CAPABILITIES)


def _random_policy(rng: random.Random, harnesses: tuple[str, ...]) -> HarnessPolicy:
    allowed_count = rng.randint(1, len(harnesses))
    allowed = list(rng.sample(harnesses, allowed_count))
    allowed.sort(key=harnesses.index)
    cost_rank = {harness: index + 1 for index, harness in enumerate(allowed)}
    table: dict[str, object] = {
        "allow": allowed,
        "cost_rank": cost_rank,
        "token_budget": rng.randint(1, 5000),
        "per_harness_budget": {
            harness: rng.randint(1, 5000) for harness in allowed
        },
        "escalate_after_attempts": rng.randint(1, 3),
    }
    return validate_policy(
        {"roles": {"tl": dict(table), "worker": dict(table), "reviewer": dict(table)}}
    )


def _slice(
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    test_plan: tuple[str, ...] = ("pytest",),
    depends_on: tuple[str, ...] = (),
    agent_type: str | None = None,
    verdict: Verdict | None = None,
    attempts: int = 0,
) -> SliceState:
    return SliceState(
        id="task",
        status=SliceStatus.PENDING,
        paths=paths,
        depends_on=depends_on,
        base_ref="main",
        test_plan=test_plan,
        agent_type=agent_type,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=attempts,
        verdict=verdict,
    )
