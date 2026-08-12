"""Closed structured contract for operator intent interpretation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.plan_validation import PlanValidationError, validate_plan_proposal

from .schema import OutputSchemaError, validate_output

_QUERY_VIEWS = ("run", "slice", "transitions")
_DECISIONS = ("approve", "reject")
_KINDS = ("query", "gate_answer", "plan_proposal", "unclear")

_NON_EMPTY_STRING: JsonObject = {"type": "string", "minLength": 1}
OPERATOR_INTENT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(_KINDS)},
        "run_id": _NON_EMPTY_STRING,
        "view": {"type": "string", "enum": list(_QUERY_VIEWS)},
        "slice_id": _NON_EMPTY_STRING,
        "limit": {"type": "integer"},
        "gate_name": _NON_EMPTY_STRING,
        "decision": {"type": "string", "enum": list(_DECISIONS)},
        "plan": {"type": "object", "additionalProperties": True},
        "reason": _NON_EMPTY_STRING,
        "question": _NON_EMPTY_STRING,
    },
    "required": ["kind"],
    "additionalProperties": False,
}


class IntentValidationError(ValueError):
    """An operator intent is malformed or violates its variant contract."""


@dataclass(frozen=True)
class QueryIntent:
    """A read-only request against one bounded control projection."""

    run_id: str
    view: Literal["run", "slice", "transitions"]
    slice_id: str | None = None
    limit: int | None = None

    @property
    def kind(self) -> Literal["query"]:
        return "query"

    def to_mapping(self) -> JsonObject:
        result: JsonObject = {"kind": self.kind, "run_id": self.run_id, "view": self.view}
        if self.slice_id is not None:
            result["slice_id"] = self.slice_id
        if self.limit is not None:
            result["limit"] = self.limit
        return result


@dataclass(frozen=True)
class GateAnswerIntent:
    """A request to answer one already-named durable gate."""

    run_id: str
    gate_name: str
    decision: Literal["approve", "reject"]

    @property
    def kind(self) -> Literal["gate_answer"]:
        return "gate_answer"

    def to_mapping(self) -> JsonObject:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "gate_name": self.gate_name,
            "decision": self.decision,
        }


@dataclass(frozen=True)
class PlanProposalIntent:
    """An inert, validated replacement WorkPlan proposal."""

    run_id: str
    plan: JsonObject

    @property
    def kind(self) -> Literal["plan_proposal"]:
        return "plan_proposal"

    def to_mapping(self) -> JsonObject:
        return {"kind": self.kind, "run_id": self.run_id, "plan": _copy_object(self.plan)}


@dataclass(frozen=True)
class UnclearIntent:
    """A first-class request for clarification instead of a guessed action."""

    reason: str
    question: str

    @property
    def kind(self) -> Literal["unclear"]:
        return "unclear"

    def to_mapping(self) -> JsonObject:
        return {"kind": self.kind, "reason": self.reason, "question": self.question}


OperatorIntent: TypeAlias = QueryIntent | GateAnswerIntent | PlanProposalIntent | UnclearIntent


def parse_operator_intent(value: object) -> OperatorIntent:
    """Validate a closed model response and return its typed intent."""
    try:
        output = validate_output(value, OPERATOR_INTENT_SCHEMA)
    except OutputSchemaError as error:
        raise IntentValidationError(str(error)) from error
    kind = output.get("kind")
    if kind == "query":
        return _query(output)
    if kind == "gate_answer":
        return _gate_answer(output)
    if kind == "plan_proposal":
        return _plan_proposal(output)
    if kind == "unclear":
        return _unclear(output)
    raise IntentValidationError("intent kind is outside the closed union")


def _query(output: Mapping[str, object]) -> QueryIntent:
    _exact_keys(
        output,
        {"kind", "run_id", "view", "slice_id", "limit"},
        "query",
        required={"kind", "run_id", "view"},
    )
    run_id = _required_identifier(output, "run_id", "query")
    view = output.get("view")
    if view not in _QUERY_VIEWS:
        raise IntentValidationError("query view is outside the closed enum")
    slice_id = _optional_identifier(output, "slice_id", "query")
    limit = output.get("limit")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise IntentValidationError("query limit must be between 1 and 100")
    if view == "slice" and slice_id is None:
        raise IntentValidationError("slice queries require slice_id")
    if view != "slice" and slice_id is not None:
        raise IntentValidationError("slice_id is only valid for slice queries")
    return QueryIntent(run_id, cast(Literal["run", "slice", "transitions"], view), slice_id, limit)


def _gate_answer(output: Mapping[str, object]) -> GateAnswerIntent:
    _exact_keys(output, {"kind", "run_id", "gate_name", "decision"}, "gate_answer")
    run_id = _required_identifier(output, "run_id", "gate_answer")
    gate_name = _required_identifier(output, "gate_name", "gate_answer")
    decision = output.get("decision")
    if decision not in _DECISIONS:
        raise IntentValidationError("gate decision is outside the closed enum")
    return GateAnswerIntent(
        run_id,
        gate_name,
        cast(Literal["approve", "reject"], decision),
    )


def _plan_proposal(output: Mapping[str, object]) -> PlanProposalIntent:
    _exact_keys(output, {"kind", "run_id", "plan"}, "plan_proposal")
    run_id = _required_identifier(output, "run_id", "plan_proposal")
    raw_plan = output.get("plan")
    try:
        validated = validate_plan_proposal({"plan": raw_plan})
    except PlanValidationError as error:
        raise IntentValidationError(f"plan proposal is invalid: {error}") from error
    plan = validated.get("plan")
    if not isinstance(plan, dict):
        raise IntentValidationError("plan proposal must contain a WorkPlan object")
    return PlanProposalIntent(run_id, _copy_object(plan))


def _unclear(output: Mapping[str, object]) -> UnclearIntent:
    _exact_keys(output, {"kind", "reason", "question"}, "unclear")
    reason = _required_text(output, "reason", "unclear")
    question = _required_text(output, "question", "unclear")
    return UnclearIntent(reason, question)


def _exact_keys(
    output: Mapping[str, object],
    allowed: set[str],
    kind: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(output) - allowed)
    missing = sorted((required or allowed) - set(output))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        raise IntentValidationError(f"{kind} intent must be closed ({'; '.join(details)})")


def _required_identifier(output: Mapping[str, object], key: str, kind: str) -> str:
    value = _required_text(output, key, kind)
    if not _safe_component(value):
        raise IntentValidationError(f"{kind} {key} must be a single path component")
    return value


def _optional_identifier(output: Mapping[str, object], key: str, kind: str) -> str | None:
    value = output.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _safe_component(value):
        raise IntentValidationError(f"{kind} {key} must be null or a single path component")
    return value


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and Path(value).name == value
    )


def _required_text(output: Mapping[str, object], key: str, kind: str) -> str:
    value = output.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IntentValidationError(f"{kind} {key} must be non-empty")
    return value.strip()


def _copy_object(value: Mapping[str, object]) -> JsonObject:
    return cast(JsonObject, _copy_value(value))


def _copy_value(value: object) -> JsonValue:
    if isinstance(value, dict):
        return {str(key): _copy_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IntentValidationError("intent contains a non-JSON value")


__all__ = [
    "OPERATOR_INTENT_SCHEMA",
    "GateAnswerIntent",
    "IntentValidationError",
    "OperatorIntent",
    "PlanProposalIntent",
    "QueryIntent",
    "UnclearIntent",
    "parse_operator_intent",
]
