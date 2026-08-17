"""Coverage for bounded, auditable harness selection."""

from __future__ import annotations

import pytest

from tl_loop.select.agent_type import (
    SelectionFailure,
    SelectionLedger,
    estimate_cost,
    parse_harness_identifier,
    select_agent_type,
    selection_failure,
)
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import SliceState, SliceStatus, Verdict

CAPABILITIES = CapabilityMap(
    {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
)


def test_parse_model_qualified_harness_separates_protocol_fields() -> None:
    route = parse_harness_identifier("opencode/deepseek-v4-pro")

    assert route.harness == "opencode/deepseek-v4-pro"
    assert route.agent_type == "opencode"
    assert route.model == "deepseek-v4-pro"


def test_parse_bare_harness_leaves_model_for_catalog_resolution() -> None:
    route = parse_harness_identifier("codex")

    assert route.agent_type == "codex"
    assert route.model is None


@pytest.mark.parametrize("value", ["", "unknown/model", "opencode/"])
def test_parse_harness_identifier_rejects_invalid_routes(value: str) -> None:
    with pytest.raises(ValueError):
        parse_harness_identifier(value)


def test_trivial_task_uses_cheapest_allowed_harness() -> None:
    choice = select_agent_type(_slice(), "worker", SelectionLedger(), _policy(), CAPABILITIES)

    assert choice is not None
    assert choice.harness == "codex/gpt-luna"
    assert choice.reason == "cheapest_capable"
    assert choice.difficulty is Difficulty.TRIVIAL
    assert choice.matched_rule == "focused_slice"
    assert choice.candidate_set == ("codex/gpt-luna", "claude/sonnet")


def test_hard_task_can_reach_expensive_harness() -> None:
    choice = select_agent_type(
        _slice(paths=("src/task.py", "src/task.rs")),
        "worker",
        SelectionLedger(),
        _policy(),
        CAPABILITIES,
    )

    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.reason == "hard_classification"


def test_no_go_escalates_only_after_attempt_threshold() -> None:
    first = _slice(agent_type="codex/gpt-luna", verdict=Verdict.NO_GO, attempts=0)
    choice = select_agent_type(first, "worker", SelectionLedger(), _policy(), CAPABILITIES)
    assert choice is not None
    assert choice.harness == "codex/gpt-luna"

    retry = _slice(agent_type="codex/gpt-luna", verdict=Verdict.NO_GO, attempts=1)
    choice = select_agent_type(retry, "worker", SelectionLedger(), _policy(), CAPABILITIES)
    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.reason == "escalated_after_no_go"


def test_total_budget_exhaustion_returns_none_and_has_typed_cause() -> None:
    ledger = SelectionLedger(role_spent={"worker": 120000})
    choice = select_agent_type(_slice(), "worker", ledger, _policy(), CAPABILITIES)

    assert choice is None
    assert (
        selection_failure(_slice(), "worker", ledger, _policy(), CAPABILITIES)
        is SelectionFailure.OVER_BUDGET
    )


def test_no_capable_harness_returns_none_and_has_typed_cause() -> None:
    capability = CapabilityMap({"codex/gpt-luna": Difficulty.TRIVIAL})
    slice_state = _slice(paths=("src/task.py", "src/task.rs"))

    assert (
        select_agent_type(slice_state, "worker", SelectionLedger(), _policy(), capability) is None
    )
    assert (
        selection_failure(slice_state, "worker", SelectionLedger(), _policy(), capability)
        is SelectionFailure.NO_CAPABLE_HARNESS
    )


def test_per_harness_budget_exhaustion_drops_only_that_candidate() -> None:
    ledger = SelectionLedger(harness_spent={"codex/gpt-luna": 80000})
    choice = select_agent_type(_slice(), "worker", ledger, _policy(), CAPABILITIES)

    assert choice is not None
    assert choice.harness == "claude/sonnet"


def test_estimated_cost_is_positive_and_difficulty_sensitive() -> None:
    slice_state = _slice()

    assert estimate_cost(slice_state, Difficulty.TRIVIAL) < estimate_cost(
        slice_state, Difficulty.HARD
    )


def _policy():
    role = {
        "allow": ["codex/gpt-luna", "claude/sonnet"],
        "cost_rank": {"codex/gpt-luna": 1, "claude/sonnet": 2},
        "token_budget": 120000,
        "per_harness_budget": {"codex/gpt-luna": 80000, "claude/sonnet": 40000},
        "escalate_after_attempts": 1,
    }
    return validate_policy(
        {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}
    )


def _slice(
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    agent_type: str | None = None,
    verdict: Verdict | None = None,
    attempts: int = 0,
) -> SliceState:
    return SliceState(
        id="task",
        status=SliceStatus.PENDING,
        paths=paths,
        depends_on=(),
        base_ref="main",
        test_plan=("pytest",),
        agent_type=agent_type,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=attempts,
        verdict=verdict,
    )
