"""Authority-boundary routing tests for translated operator intents."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.rlm.intent_contract import (
    GateAnswerIntent,
    PlanProposalIntent,
    QueryIntent,
    UnclearIntent,
)
from tl_loop.rlm.intent_routing import IntentRoutingError, route_operator_intent
from tl_loop.state.store import create


def _root(tmp_path: Path) -> Path:
    root = tmp_path / ".exo" / "tl-loop"
    create(
        "root",
        {"gates": [{"name": "tl-timeout", "status": "pending"}]},
        root_dir=root,
    )
    return tmp_path


def test_gate_routing_requires_an_existing_gate_and_does_not_write(tmp_path: Path) -> None:
    project = _root(tmp_path)
    state_path = project / ".exo" / "tl-loop" / "root" / "run.json"
    before = state_path.read_bytes()

    routed = route_operator_intent(GateAnswerIntent("root", "tl-timeout", "approve"), project)

    assert routed == GateAnswerIntent("root", "tl-timeout", "approve")
    assert state_path.read_bytes() == before
    with pytest.raises(IntentRoutingError, match="does not exist"):
        route_operator_intent(GateAnswerIntent("root", "missing", "approve"), project)


def test_query_routing_uses_cli_run_and_slice_validation(tmp_path: Path) -> None:
    project = _root(tmp_path)

    assert route_operator_intent(QueryIntent("root", "run"), project) == QueryIntent("root", "run")
    with pytest.raises(IntentRoutingError, match="does not exist"):
        route_operator_intent(QueryIntent("root", "slice", slice_id="missing"), project)
    with pytest.raises(IntentRoutingError, match="not available"):
        route_operator_intent(QueryIntent("missing", "run"), project)


def test_plan_routing_revalidates_model_payload(tmp_path: Path) -> None:
    project = _root(tmp_path)
    valid = PlanProposalIntent("root", {"leaves": [{"name": "api", "task": "implement api"}]})
    assert route_operator_intent(valid, project) == valid

    invalid = PlanProposalIntent(
        "root",
        {
            "leaves": [
                {"name": "api", "task": "api", "boundary": ["src/**"]},
                {"name": "tests", "task": "tests", "boundary": ["src/api.py"]},
            ]
        },
    )
    with pytest.raises(IntentRoutingError, match="validation"):
        route_operator_intent(invalid, project)


def test_unclear_routing_never_requires_or_mutates_a_run(tmp_path: Path) -> None:
    intent = UnclearIntent("ambiguous", "Which run?")

    assert route_operator_intent(intent, tmp_path) == intent
