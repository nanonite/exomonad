"""Bounded, tool-free plan authoring through the typed decompose boundary.

Plan authoring is the only path by which model judgment proposes work. It is
deliberately narrow:

* the input is an explicit authoring request, never the executable
  ``plan.json`` document, so a proposal can never be mistaken for authority;
* the judgment goes through the existing stateless ``decompose`` boundary with
  ``RlmRequest.tools == ()`` and no effect client;
* the resulting ``SliceSpec`` records are adapted into the canonical
  ``WorkPlan`` mapping and manifest declaration, then re-validated by the same
  closed-key validator ``plan.json`` uses; and
* the result is inert. It becomes execution authority only through
  :mod:`tl_loop.rlm.plan_acceptance`, which requires an explicit human gate.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.plan_validation import PlanValidationError, validate_plan_document
from tl_loop.select.agent_type import policy_approved_candidates
from tl_loop.select.harness import parse_harness_identifier
from tl_loop.select.model import ModelCatalog, select_model
from tl_loop.select.policy import HarnessPolicy
from tl_loop.state.plan_manifest import ManifestError, PlanManifest, build_plan_manifest

from .call import MAX_ATTEMPTS
from .decompose import decompose
from .slice_spec import SliceSpec
from .store import RlmCallStore, RlmModelChoice, RlmRoleLedger

AUTHORING_ROLE = "tl"
WORKER_ROLE = "worker"
DEFAULT_AUTHORING_CONTEXT_LENGTH = 128_000
AUTHORING_INPUT_KEYS = frozenset(
    {"task", "read_first", "constraints", "base_ref", "agent_type", "budgets"}
)
EXECUTABLE_PLAN_KEYS = frozenset(
    {"run_id", "plan", "workers", "leaves", "sub_tls", "clarification"}
)
AUTHORING_JUDGMENT = "decompose"
AUTHORING_AUDIT_KEYS = frozenset(
    {"judgment", "model", "attempts", "tokens", "failures", "violations"}
)
MAX_AUDIT_VIOLATIONS = 8
MAX_AUDIT_VIOLATION_CHARS = 160
_AUTHORING_BUDGET_KEYS = frozenset({"tokens", "wall_seconds"})


class PlanAuthoringError(ValueError):
    """An authoring request or proposal cannot become a validated plan."""


class PlanAuthoringInputError(PlanAuthoringError):
    """The plan-authoring input document is not a closed authoring request."""


class PlanAuthoringUnsupported(PlanAuthoringError):
    """The proposal declares something the executable plan cannot express."""


@dataclass(frozen=True)
class PlanAuthoringInput:
    """A human-authored request for a plan, distinct from ``plan.json``."""

    task: str
    read_first: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    base_ref: str = "main"
    agent_type: str | None = None
    budgets: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.task, "task")
        _require_text(self.base_ref, "base_ref")
        _require_component(self.base_ref, "base_ref")
        for name, values in (("read_first", self.read_first), ("constraints", self.constraints)):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise PlanAuthoringInputError(f"{name} must contain non-empty strings")
            if name == "read_first":
                for item in values:
                    _require_repository_relative(item, f"read_first entry {item!r}")
        if self.agent_type is not None:
            try:
                parse_harness_identifier(self.agent_type)
            except ValueError as error:
                raise PlanAuthoringInputError(f"agent_type: {error}") from error
        _validate_authoring_budgets(self.budgets)

    @classmethod
    def from_mapping(cls, value: object) -> PlanAuthoringInput:
        """Parse the closed authoring-request document."""
        if not isinstance(value, Mapping):
            raise PlanAuthoringInputError("plan-authoring input must be an object")
        executable = sorted(set(value) & EXECUTABLE_PLAN_KEYS)
        if executable:
            raise PlanAuthoringInputError(
                "plan-authoring input is a request, not an executable plan; "
                "remove: " + ", ".join(executable)
            )
        unknown = sorted(set(value) - AUTHORING_INPUT_KEYS)
        if unknown:
            raise PlanAuthoringInputError(
                "plan-authoring input contains unknown keys: " + ", ".join(unknown)
            )
        return cls(
            task=cast(str, value.get("task")),
            read_first=_text_tuple(value.get("read_first")),
            constraints=_text_tuple(value.get("constraints")),
            base_ref=cast(str, value.get("base_ref", "main")),
            agent_type=cast("str | None", value.get("agent_type")),
            budgets=cast("Mapping[str, int]", value.get("budgets", {})),
        )


@dataclass(frozen=True)
class AuthoredPlan:
    """A validated, digest-bound, still-inert plan proposal."""

    run_id: str
    document: Mapping[str, object]
    manifest: PlanManifest
    audit: Mapping[str, object] = field(default_factory=dict)
    requires_acceptance: bool = True

    @property
    def digest(self) -> str:
        """Return the manifest digest that identifies this exact proposal."""
        return cast(str, self.manifest.digest)

    def plan_mapping(self) -> Mapping[str, object]:
        """Return the canonical WorkPlan object the manifest was built from."""
        plan = self.document.get("plan")
        if not isinstance(plan, Mapping):
            raise PlanAuthoringError("authored plan document lost its WorkPlan object")
        return plan


def resolve_authoring_model_choice(
    policy: HarnessPolicy,
    *,
    backend: object,
    catalog: ModelCatalog | None = None,
    role: str = AUTHORING_ROLE,
    requested_model: str | None = None,
    context_length: int = DEFAULT_AUTHORING_CONTEXT_LENGTH,
    replay: MutableMapping[str, object] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> RlmModelChoice:
    """Resolve the policy-owned model the authoring judgment may use.

    The harness comes from the role's policy allowlist and the token spend is
    charged against that same role budget, so authoring cannot route around
    either. A model-qualified allowlist entry pins the model exactly and is
    never overridden; a bare agent type defers to the catalog, which chooses
    among that agent type's models only. An absent catalog defers to the
    configured harness exactly as ``load_model_catalog`` defers for workers.
    """
    if not isinstance(policy, HarnessPolicy):
        raise TypeError("policy must be a HarnessPolicy")
    role_policy = policy.roles.get(role)
    if role_policy is None:
        raise PlanAuthoringError(f"harness policy has no {role!r} role")
    if not role_policy.allow:
        raise PlanAuthoringError(f"harness policy approves no harness for role {role!r}")
    harness = role_policy.allow[0]
    model_id = _authoring_model_id(harness, catalog, requested_model)
    return RlmModelChoice(
        model_id=model_id,
        backend=backend,
        role=role,
        store=RlmCallStore(ledger=RlmRoleLedger(budgets={role: role_policy.token_budget})),
        replay=replay if replay is not None else {},
        max_attempts=max_attempts,
        context_length=context_length,
    )


def author_plan(
    authoring: PlanAuthoringInput,
    *,
    model_choice: RlmModelChoice,
    run_id: str,
    policy: HarnessPolicy | None = None,
    owned_branch: str = "main",
) -> AuthoredPlan:
    """Propose one validated, digest-bound plan through bounded judgment."""
    if not isinstance(authoring, PlanAuthoringInput):
        raise TypeError("authoring must be a PlanAuthoringInput")
    if not isinstance(model_choice, RlmModelChoice):
        raise TypeError(
            "author_plan requires a policy-resolved RlmModelChoice, not a bare backend"
        )
    _validate_harness_request(authoring, policy)
    slices = decompose(authoring_root_spec(authoring), model_choice)
    document = slices_to_plan_document(
        slices,
        run_id=run_id,
        agent_type=authoring.agent_type,
        budgets=authoring.budgets,
    )
    return AuthoredPlan(
        run_id=run_id,
        document=document,
        manifest=build_authoring_manifest(document, run_id=run_id, owned_branch=owned_branch),
        audit=judgment_audit(model_choice),
    )


def judgment_audit(
    choice: RlmModelChoice,
    name: str = AUTHORING_JUDGMENT,
) -> JsonObject:
    """Project the bounded authoring judgment record for one proposal.

    Only scalar identities, attempt counts, token totals, and truncated
    validation reasons survive. The model's prose, the request, and the plan
    body never reach this record.
    """
    events = list(choice.store.events)
    relevant = [event for event in events if event.get("name") == name]
    return {
        "judgment": name,
        "model": choice.model_id,
        "attempts": len(relevant),
        "tokens": sum(_event_tokens(event) for event in relevant),
        "failures": sum(1 for event in relevant if event.get("validation_error")),
        "violations": cast(JsonValue, _bounded_violations(relevant)),
    }


def authoring_root_spec(authoring: PlanAuthoringInput) -> JsonObject:
    """Return the bounded root specification handed to ``decompose``.

    The harness request and the budgets are deliberately absent: harness and
    budget selection are policy-owned, and showing them to the judgment would
    invite it to reason about authority it does not hold.
    """
    return {
        "task": authoring.task,
        "base_ref": authoring.base_ref,
        "read_first": [item for item in authoring.read_first],
        "constraints": [item for item in authoring.constraints],
    }


def slices_to_plan_document(
    slices: Sequence[SliceSpec],
    *,
    run_id: str,
    agent_type: str | None = None,
    budgets: Mapping[str, int] | None = None,
) -> Mapping[str, object]:
    """Adapt validated slices into one closed, executable plan document.

    Every slice becomes a leaf. The slice ``test_plan`` is merged ahead of its
    ``verify`` commands because the controller derives a slice's run-state
    ``test_plan`` from the leaf's ``verify`` list, and an authored test plan is
    authority-bearing: dropping it would let a slice declare itself verified.
    """
    _reject_unexpressible_dependencies(slices)
    leaves: list[JsonObject] = [
        _leaf(spec, agent_type=agent_type) for spec in slices
    ]
    document: JsonObject = {"run_id": run_id, "plan": {"leaves": cast(JsonValue, leaves)}}
    if budgets:
        document["budgets"] = cast(JsonValue, dict(budgets))
    try:
        return validate_plan_document(document)
    except PlanValidationError as error:
        raise PlanAuthoringError(f"authored plan is invalid: {error}") from error


def build_authoring_manifest(
    document: Mapping[str, object],
    *,
    run_id: str,
    owned_branch: str,
    manifest_revision: int = 1,
) -> PlanManifest:
    """Build the immutable manifest declaration for one authored plan."""
    plan = document.get("plan")
    if not isinstance(plan, Mapping):
        raise PlanAuthoringError("authored plan document must contain a WorkPlan object")
    try:
        return build_plan_manifest(
            plan,
            scope_id=run_id,
            owned_branch=owned_branch,
            manifest_revision=manifest_revision,
        )
    except ManifestError as error:
        raise PlanAuthoringError(f"authored plan manifest is invalid: {error}") from error


def _leaf(spec: SliceSpec, *, agent_type: str | None) -> JsonObject:
    _require_component(spec.id, f"slice id {spec.id!r}")
    verification = _merged_verification(spec)
    if not verification:
        raise PlanAuthoringError(f"slice {spec.id!r} declares no verification command")
    leaf: JsonObject = {
        "name": spec.id,
        "task": spec.title,
        "boundary": [item for item in spec.paths],
        "steps": [item for item in spec.steps],
        "verify": [item for item in verification],
        "done_criteria": [item for item in spec.done_criteria],
    }
    if agent_type is not None:
        leaf["agent_type"] = agent_type
    return leaf


def _merged_verification(spec: SliceSpec) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*spec.test_plan, *spec.verify)))


def _reject_unexpressible_dependencies(slices: Sequence[SliceSpec]) -> None:
    """Refuse dependency edges the executable plan cannot carry today.

    ``WorkPlan.LeafTask`` has no ``depends_on`` field, so authoring a
    dependency edge would silently drop an ordering the judgment believed it
    had declared. Refusing keeps the proposal from becoming a plan that
    dispatches a dependent slice before its dependency.
    """
    blocked = [spec.id for spec in slices if spec.depends_on]
    if blocked:
        raise PlanAuthoringUnsupported(
            "direct leaf dependency edges are not part of the executable plan "
            "contract; express the ordering with ordered sub_tls stages or "
            "remove depends_on from: " + ", ".join(sorted(blocked))
        )


def _validate_harness_request(
    authoring: PlanAuthoringInput,
    policy: HarnessPolicy | None,
) -> None:
    """Keep a declared harness request inside the worker policy allowlist."""
    if authoring.agent_type is None:
        return
    if policy is None:
        raise PlanAuthoringError(
            "an agent_type request must be validated against the harness policy"
        )
    worker_policy = policy.roles.get(WORKER_ROLE)
    if worker_policy is None:
        raise PlanAuthoringError(f"harness policy has no {WORKER_ROLE!r} role")
    if not policy_approved_candidates(authoring.agent_type, worker_policy):
        raise PlanAuthoringError(
            f"agent_type {authoring.agent_type!r} is not approved for the "
            f"{WORKER_ROLE!r} role; allowed: " + ", ".join(worker_policy.allow)
        )


def _authoring_model_id(
    harness: str,
    catalog: ModelCatalog | None,
    requested_model: str | None,
) -> str:
    """Resolve one model id without letting policy be overridden."""
    route = parse_harness_identifier(harness)
    if route.model is not None:
        if requested_model is not None and requested_model != route.model:
            raise PlanAuthoringError(
                f"policy pins {harness!r}; requested model {requested_model!r} is refused"
            )
        return route.model
    return _catalog_model_id(route.agent_type, catalog, requested_model)


def _catalog_model_id(
    harness: str,
    catalog: ModelCatalog | None,
    requested_model: str | None,
) -> str:
    if catalog is None:
        return harness
    return select_model(harness, catalog, requested_model).model_id


def _event_tokens(event: Mapping[str, object]) -> int:
    total = event.get("total_tokens", 0)
    return total if type(total) is int and total >= 0 else 0


def _bounded_violations(events: Sequence[Mapping[str, object]]) -> list[str]:
    reasons: list[str] = []
    for event in events:
        reason = event.get("validation_error")
        if not isinstance(reason, str) or not reason:
            continue
        text = " ".join(reason.split())
        reasons.append(
            text[: MAX_AUDIT_VIOLATION_CHARS - 3] + "..."
            if len(text) > MAX_AUDIT_VIOLATION_CHARS
            else text
        )
    return reasons[:MAX_AUDIT_VIOLATIONS]


def _validate_authoring_budgets(budgets: object) -> None:
    if not isinstance(budgets, Mapping):
        raise PlanAuthoringInputError("budgets must be an object")
    unknown = sorted(set(budgets) - _AUTHORING_BUDGET_KEYS)
    if unknown:
        raise PlanAuthoringInputError("budgets contains unknown keys: " + ", ".join(unknown))
    for key, value in budgets.items():
        if type(value) is not int or value < 0:
            raise PlanAuthoringInputError(f"budgets.{key} must be a non-negative integer")


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise PlanAuthoringInputError("expected an array of non-empty strings")
    return tuple(cast("list[str]", value))


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PlanAuthoringInputError(f"{name} must be a non-empty string")


def _require_component(value: str, name: str) -> None:
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise PlanAuthoringInputError(f"{name} must be a single path component")


def _require_repository_relative(path: str, name: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise PlanAuthoringInputError(f"{name} must be repository-relative")


__all__ = [
    "AUTHORING_AUDIT_KEYS",
    "AUTHORING_INPUT_KEYS",
    "AUTHORING_JUDGMENT",
    "AUTHORING_ROLE",
    "DEFAULT_AUTHORING_CONTEXT_LENGTH",
    "EXECUTABLE_PLAN_KEYS",
    "MAX_AUDIT_VIOLATIONS",
    "MAX_AUDIT_VIOLATION_CHARS",
    "WORKER_ROLE",
    "AuthoredPlan",
    "PlanAuthoringError",
    "PlanAuthoringInput",
    "PlanAuthoringInputError",
    "PlanAuthoringUnsupported",
    "author_plan",
    "authoring_root_spec",
    "build_authoring_manifest",
    "judgment_audit",
    "resolve_authoring_model_choice",
    "slices_to_plan_document",
]
