"""Read-only authority validation for translated operator intents."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonObject
from tl_loop.plan_validation import PlanValidationError, validate_plan_proposal
from tl_loop.state.schema import RunState
from tl_loop.state.store import CorruptCheckpoint, RunStore

from .intent_contract import (
    GateAnswerIntent,
    OperatorIntent,
    PlanProposalIntent,
    QueryIntent,
    UnclearIntent,
    parse_operator_intent,
)


class IntentRoutingError(ValueError):
    """A translated intent fails the live CLI authority checks."""


def route_operator_intent(
    intent: OperatorIntent,
    project_root: str | Path,
) -> OperatorIntent:
    """Revalidate an intent against durable state without applying it.

    This is deliberately a validation-only boundary. Gate answers are checked
    against existing durable gate names, and plan proposals are validated a
    second time at the routing boundary. Neither path calls a state writer.
    """
    reparsed = _reparse(intent)
    if isinstance(reparsed, UnclearIntent):
        return reparsed

    root = Path(project_root)
    state = _load_run(root, reparsed.run_id)
    if isinstance(reparsed, QueryIntent):
        if reparsed.view == "slice" and reparsed.slice_id not in state.slices:
            raise IntentRoutingError(f"slice {reparsed.slice_id!r} does not exist")
        return reparsed
    if isinstance(reparsed, GateAnswerIntent):
        if not any(gate.name == reparsed.gate_name for gate in state.gates):
            raise IntentRoutingError(f"gate {reparsed.gate_name!r} does not exist")
        return reparsed

    if isinstance(reparsed, PlanProposalIntent):
        try:
            validated = validate_plan_proposal({"plan": reparsed.plan})
        except PlanValidationError as error:
            raise IntentRoutingError(f"plan proposal is invalid: {error}") from error
        plan = validated.get("plan")
        if not isinstance(plan, dict):
            raise IntentRoutingError("plan proposal must contain a WorkPlan object")
        return PlanProposalIntent(reparsed.run_id, cast(JsonObject, plan))
    raise IntentRoutingError("unsupported operator intent")


def _reparse(intent: OperatorIntent) -> OperatorIntent:
    try:
        return parse_operator_intent(intent.to_mapping())
    except AttributeError as error:
        raise IntentRoutingError("operator intent is not a typed closed intent") from error
    except ValueError as error:
        raise IntentRoutingError(f"operator intent failed closed validation: {error}") from error


def _load_run(project_root: Path, run_id: str) -> RunState:
    store = RunStore(run_id, project_root / ".exo" / "tl-loop")
    try:
        return store.load()
    except (CorruptCheckpoint, OSError, ValueError) as error:
        raise IntentRoutingError(f"run {run_id!r} is not available: {error}") from error


__all__ = ["IntentRoutingError", "route_operator_intent"]
