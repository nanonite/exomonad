"""The roles.tl model resolution and bounded TL judgments.

The deterministic worker<->reviewer loop makes no model call. At the review
round ceiling, or at a parent integration failure, the controller makes one
bounded ``roles.tl`` judgment with a closed action schema:

* ``review_stuck`` chooses ``guide`` or ``escalate``;
* ``integration_failure`` chooses ``delegate`` or ``escalate``. Both repair
  actions carry a seven-section ``RepairHandoff`` validated against owned paths
  and dispatched through ``resume_pr`` to the same owner.

The model resolves from ``roles.tl`` in ``.exo/harness_policy.toml`` and
``.exo/model-catalog.json``. A model-qualified allow entry pins the model; a bare
agent type defers to the catalog. When neither yields a model, resolution
raises ``TLModelResolutionError`` naming the file to fix. The bare harness name
is never used as a model id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.select.harness import parse_harness_identifier
from tl_loop.select.model import ModelCatalog, ModelResolutionError, select_model
from tl_loop.select.policy import HarnessPolicy
from tl_loop.state.store import RunStore

from .call import rlm
from .repair_contract import (
    REPAIR_HANDOFF_SCHEMA,
    RepairError,
    RepairHandoff,
    RepairInputError,
)
from .store import RlmCallStore, RlmModelChoice, RlmRoleLedger
from .subprocess_backend import SubprocessRlmBackend

TL_ROLE = "tl"
REVIEW_STUCK_JUDGMENT = "review_stuck"
REVIEW_STUCK_ACTIONS = ("guide", "escalate")
INTEGRATION_FAILURE_JUDGMENT = "integration_failure"
INTEGRATION_FAILURE_ACTIONS = ("delegate", "escalate")
DEFAULT_TL_CONTEXT_LENGTH = 128_000
POLICY_FILE = ".exo/harness_policy.toml"
CATALOG_FILE = ".exo/model-catalog.json"

REVIEW_STUCK_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(REVIEW_STUCK_ACTIONS)},
        "reason": {"type": "string", "minLength": 1},
        "handoff": REPAIR_HANDOFF_SCHEMA,
    },
    "required": ["action", "reason"],
    "additionalProperties": False,
}

REVIEW_STUCK_PROMPT = """The worker and reviewer did not converge within the review-round ceiling.
Decide how to guide the slice, or escalate to the human operator.

ANTI-PATTERNS (FIRST)
- Do not create a new branch, leaf name, agent type, or PR.
- Do not propose edits outside the supplied slice paths.
- Do not return prose outside the schema.
- Do not guide when the reviewer findings contradict the slice boundary or the
  disagreement needs a human decision; escalate instead.

READ FIRST
- The reviewer findings per round are the primary evidence.
- The slice-owned paths bound every path the handoff may name.

STEPS
- Choose "guide" when a concrete, in-boundary repair would satisfy the findings.
  Return "handoff" with root_cause, proposed_solution, read_first, steps, verify,
  boundary, and done_criteria, all non-empty.
- Choose "escalate" otherwise. Return only "reason"; omit "handoff".

VERIFY
- "guide" carries a complete handoff and "escalate" carries none.

DONE CRITERIA
- Return only the schema object.
"""

INTEGRATION_FAILURE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(INTEGRATION_FAILURE_ACTIONS)},
        "reason": {"type": "string", "minLength": 1},
        "handoff": REPAIR_HANDOFF_SCHEMA,
    },
    "required": ["action", "reason"],
    "additionalProperties": False,
}

INTEGRATION_FAILURE_PROMPT = """A child integration failed against its parent branch.
Decide whether the existing aggregate owner can repair it, or whether a human
must resolve the integration.

ANTI-PATTERNS (FIRST)
- Do not create a new branch, leaf name, agent type, or PR.
- Do not propose edits outside the supplied slice-owned paths.
- Do not return prose outside the schema.
- Do not delegate an ambiguous failure or one requiring a plan or policy change.

READ FIRST
- The integration failure and parent/head evidence are the primary diagnosis.
- The supplied slice paths bound every path the handoff may name.

STEPS
- Choose "delegate" when a concrete repair within the existing aggregate
  owner's boundary can resolve the failure. Return a complete "handoff" with
  root_cause, proposed_solution, read_first, steps, verify, boundary, and
  done_criteria, all non-empty.
- Choose "escalate" otherwise. Return only "reason"; omit "handoff".

VERIFY
- "delegate" carries a complete handoff and "escalate" carries none.
- The controller resumes the existing owner and validates the handoff paths.

DONE CRITERIA
- Return only the schema object.
"""


class TLModelResolutionError(RuntimeError):
    """The roles.tl judgment model cannot be resolved; the message names the file."""


class ReviewStuckJudgmentError(RepairError):
    """The review-stuck judgment is unusable (shape or boundary violation)."""


@dataclass(frozen=True)
class ReviewStuckDecision:
    """One validated review-stuck judgment."""

    action: str
    reason: str
    handoff: RepairHandoff | None = None


class IntegrationFailureJudgmentError(RepairError):
    """The integration-failure judgment is unusable or violates its contract."""


@dataclass(frozen=True)
class IntegrationFailureDecision:
    """One validated delegate/escalate judgment for an integration failure."""

    action: str
    reason: str
    handoff: RepairHandoff | None = None


def resolve_tl_model_id(policy: HarnessPolicy, catalog: ModelCatalog | None) -> tuple[str, str]:
    """Return ``(agent_type, model_id)`` for roles.tl or raise naming the file to fix."""
    role_policy = policy.roles.get(TL_ROLE)
    if role_policy is None or not role_policy.allow:
        raise TLModelResolutionError(
            f"{POLICY_FILE} approves no harness for role {TL_ROLE!r}; add roles.tl.allow"
        )
    harness = role_policy.allow[0]
    try:
        route = parse_harness_identifier(harness)
    except ValueError as error:
        raise TLModelResolutionError(
            f"{POLICY_FILE} roles.tl.allow entry {harness!r} is invalid: {error}"
        ) from error
    if route.model is not None:
        return route.agent_type, route.model
    if catalog is None:
        raise TLModelResolutionError(
            f"roles.tl.allow entry {harness!r} pins no model and {CATALOG_FILE} does not "
            f"exist. Pin a model in {POLICY_FILE} (for example "
            f'allow = ["{route.agent_type}/<model-id>"]) or add {CATALOG_FILE}.'
        )
    try:
        return route.agent_type, select_model(route.agent_type, catalog, None).model_id
    except ModelResolutionError as error:
        raise TLModelResolutionError(
            f"roles.tl.allow entry {harness!r} pins no model and {CATALOG_FILE} resolves "
            f"none for {route.agent_type!r}: {error}. Pin a model in {POLICY_FILE} or fix "
            f"{CATALOG_FILE}."
        ) from error


def build_tl_model_choice(
    policy: HarnessPolicy,
    catalog: ModelCatalog | None,
    *,
    backend: object | None = None,
    run_store: RunStore | None = None,
) -> RlmModelChoice:
    """Resolve the TL judgment model and its tool-free backend, or refuse.

    ``backend`` is an injection point for tests; production passes ``None`` and
    gets the harness subprocess backend, which fails when the binary is absent.
    """
    agent_type, model_id = resolve_tl_model_id(policy, catalog)
    if backend is None:
        try:
            backend = SubprocessRlmBackend(agent_type, model_id)
        except Exception as error:
            raise TLModelResolutionError(
                f"roles.tl harness {agent_type!r} cannot serve judgments: {error} "
                f"(file to fix: {POLICY_FILE})"
            ) from error
    role_policy = policy.roles[TL_ROLE]
    ledger = RlmRoleLedger(budgets={TL_ROLE: role_policy.token_budget})
    if run_store is not None:
        spent = (
            run_store.load().budgets.role_spent.get(TL_ROLE, 0) if run_store.path.exists() else 0
        )
        ledger.bind(
            TL_ROLE,
            spent=spent,
            budget=role_policy.token_budget,
            charge_callback=run_store.charge_role_tokens,
        )
    return RlmModelChoice(
        model_id=model_id,
        backend=backend,
        role=TL_ROLE,
        store=RlmCallStore(ledger=ledger),
        replay={},
        max_attempts=3,
        context_length=DEFAULT_TL_CONTEXT_LENGTH,
    )


def judge_review_stuck(
    choice: RlmModelChoice,
    *,
    evidence: Mapping[str, object],
    owned_paths: Sequence[str],
) -> ReviewStuckDecision:
    """Make the one roles.tl judgment and validate its closed result.

    The guide handoff's owned-path validation runs where it is dispatched,
    in ``dispatch_guide_handoff``.
    """
    inputs: JsonObject = {
        "sections": cast(
            JsonValue,
            [
                {
                    "name": "instructions",
                    "content": REVIEW_STUCK_PROMPT,
                    "priority": 140,
                    "required": True,
                },
                {
                    "name": "slice_boundary",
                    "content": list(owned_paths),
                    "priority": 130,
                    "required": True,
                },
                {
                    "name": "review_evidence",
                    "content": cast(JsonValue, dict(evidence)),
                    "priority": 120,
                    "required": True,
                },
            ],
        )
    }
    output = rlm(REVIEW_STUCK_JUDGMENT, inputs, REVIEW_STUCK_SCHEMA, choice)
    action = cast(str, output["action"])
    reason = cast(str, output["reason"]).strip()
    handoff_value = output.get("handoff")
    if action == "escalate":
        if handoff_value is not None:
            raise ReviewStuckJudgmentError("an escalate judgment must not carry a handoff")
        return ReviewStuckDecision("escalate", reason)
    if not isinstance(handoff_value, Mapping):
        raise ReviewStuckJudgmentError("a guide judgment must carry a handoff")
    try:
        handoff = RepairHandoff.from_mapping(handoff_value)
    except RepairInputError as error:
        raise ReviewStuckJudgmentError(f"guide handoff rejected: {error}") from error
    return ReviewStuckDecision("guide", reason, handoff)


def judge_integration_failure(
    choice: RlmModelChoice,
    *,
    evidence: Mapping[str, object],
    owned_paths: Sequence[str],
) -> IntegrationFailureDecision:
    """Make one roles.tl integration judgment and validate its closed result."""
    inputs: JsonObject = {
        "sections": cast(
            JsonValue,
            [
                {
                    "name": "instructions",
                    "content": INTEGRATION_FAILURE_PROMPT,
                    "priority": 140,
                    "required": True,
                },
                {
                    "name": "slice_boundary",
                    "content": list(owned_paths),
                    "priority": 130,
                    "required": True,
                },
                {
                    "name": "integration_failure",
                    "content": cast(JsonValue, dict(evidence)),
                    "priority": 120,
                    "required": True,
                },
            ],
        )
    }
    output = rlm(INTEGRATION_FAILURE_JUDGMENT, inputs, INTEGRATION_FAILURE_SCHEMA, choice)
    action = cast(str, output["action"])
    reason = cast(str, output["reason"]).strip()
    handoff_value = output.get("handoff")
    if action == "escalate":
        if handoff_value is not None:
            raise IntegrationFailureJudgmentError("an escalate judgment must not carry a handoff")
        return IntegrationFailureDecision("escalate", reason)
    if action != "delegate":
        raise IntegrationFailureJudgmentError(f"unsupported integration action {action!r}")
    if not isinstance(handoff_value, Mapping):
        raise IntegrationFailureJudgmentError("a delegate judgment must carry a handoff")
    try:
        handoff = RepairHandoff.from_mapping(handoff_value)
    except RepairInputError as error:
        raise IntegrationFailureJudgmentError(f"delegate handoff rejected: {error}") from error
    return IntegrationFailureDecision("delegate", reason, handoff)


__all__ = [
    "CATALOG_FILE",
    "INTEGRATION_FAILURE_ACTIONS",
    "INTEGRATION_FAILURE_JUDGMENT",
    "INTEGRATION_FAILURE_PROMPT",
    "INTEGRATION_FAILURE_SCHEMA",
    "POLICY_FILE",
    "REVIEW_STUCK_JUDGMENT",
    "REVIEW_STUCK_SCHEMA",
    "IntegrationFailureDecision",
    "IntegrationFailureJudgmentError",
    "ReviewStuckDecision",
    "ReviewStuckJudgmentError",
    "TLModelResolutionError",
    "build_tl_model_choice",
    "judge_integration_failure",
    "judge_review_stuck",
    "resolve_tl_model_id",
]
