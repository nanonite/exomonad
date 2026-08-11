"""Evidence-gated learned-policy refinement at TL wave boundaries."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import ReadResult, SequenceStatus
from tl_loop.fsm.phase import TLPhase
from tl_loop.select.classify import CLASSIFICATION_RULES
from tl_loop.select.learned_policy import (
    CapabilityObservation,
    DispatchPolicyStore,
    LearnedPolicy,
    to_document,
)
from tl_loop.select.policy import ROLE_NAMES, HarnessPolicy, load_policy
from tl_loop.state.schema import RunState

_TASK_CLASSES = frozenset(rule.name for rule in CLASSIFICATION_RULES)
_WAVE_BOUNDARY_PHASES = frozenset({TLPhase.TLAllMerged, TLPhase.TLDone, TLPhase.TLFailed})
_FAILURE_EVENT_TYPES = frozenset(
    {"agent.stuck", "pr.merge_failed", "ci.status_changed", "copilot.review"}
)
_SUCCESS_EVENT_TYPES = frozenset({"agent.completed", "agent.notify_parent", "pr.merged"})
_FAILURE_OUTCOMES = frozenset({"failure", "failed", "no-go", "no_go", "changes_requested", "stuck"})
_SUCCESS_OUTCOMES = frozenset(
    {"success", "succeeded", "completed", "passed", "merged", "go", "merge_ready"}
)
EventLog: TypeAlias = ReadResult | Iterable[EventEnvelope | Mapping[str, object]]


class RefinementError(ValueError):
    """Evidence is malformed, incomplete, or outside the closed refinement set."""


class RefinementBoundaryError(RefinementError):
    """Refinement was requested while a wave was still in progress."""


class UnevidencedProposal(RefinementError):
    """A learned entry was proposed without immutable event sequence evidence."""


class RefinementTrigger(str, Enum):
    """Closed reasons that are permitted to create learned policy entries."""

    REPEATED_FAILURE = "repeated_failure"
    RESOLVED_TACTIC = "resolved_tactic"
    REPEATED_ROLE = "repeated_role"
    BEHAVIOR_POLICY = "behavior_policy"


class Outcome(str, Enum):
    """The only outcomes that contribute to pass/fail capability evidence."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class RefinementConfig:
    """Minimum repeated observations required for each refinement family."""

    min_occurrences: int = 2
    min_capability_observations: int = 2

    def __post_init__(self) -> None:
        for name in ("min_occurrences", "min_capability_observations"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class RefinementObservation:
    """One normalized ledger observation used by the closed trigger set."""

    seq: int
    task_class: str | None = None
    role: str | None = None
    harness: str | None = None
    outcome: Outcome | str | None = None
    tactic: str | None = None
    behavior_policy: str | None = None
    delegation: bool = False

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 0:
            raise RefinementError("observation sequence must be a non-negative integer")
        if self.task_class is not None and self.task_class not in _TASK_CLASSES:
            raise RefinementError(f"unknown task class: {self.task_class!r}")
        for name in ("role", "harness", "tactic", "behavior_policy"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise RefinementError(f"{name} must be null or a non-empty string")
        if not isinstance(self.delegation, bool):
            raise RefinementError("delegation must be boolean")
        if self.outcome is not None:
            try:
                normalized = (
                    self.outcome if isinstance(self.outcome, Outcome) else Outcome(self.outcome)
                )
            except ValueError as error:
                raise RefinementError(f"unknown observation outcome: {self.outcome!r}") from error
            object.__setattr__(self, "outcome", normalized)


@dataclass(frozen=True)
class RefinementProposal:
    """One learned value and the ledger sequences that justify it."""

    trigger: RefinementTrigger | str
    category: str
    key: str
    value: str
    evidence_seqs: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, RefinementTrigger):
            try:
                object.__setattr__(self, "trigger", RefinementTrigger(self.trigger))
            except ValueError as error:
                raise RefinementError(f"unknown refinement trigger: {self.trigger!r}") from error
        for name in ("category", "key", "value"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise RefinementError(f"{name} must be a non-empty string")
        if not self.evidence_seqs:
            raise UnevidencedProposal(
                f"{self.category}:{self.key} requires event sequence evidence"
            )
        if any(type(seq) is not int or seq < 0 for seq in self.evidence_seqs):
            raise UnevidencedProposal(
                f"{self.category}:{self.key} has invalid event sequence evidence"
            )
        if len(set(self.evidence_seqs)) != len(self.evidence_seqs):
            raise UnevidencedProposal(
                f"{self.category}:{self.key} has duplicate event sequence evidence"
            )


@dataclass(frozen=True)
class RefinementResult:
    """The durable result of one boundary refinement attempt."""

    changed: bool
    policy: LearnedPolicy | None
    proposals: tuple[RefinementProposal, ...]
    capability_observations: Mapping[str, CapabilityObservation]


def maybe_refine(
    run_state: RunState,
    event_log: EventLog,
    *,
    store: DispatchPolicyStore | None = None,
    config: RefinementConfig | None = None,
) -> RefinementResult:
    """Apply repeated, evidence-backed refinements only after a wave boundary."""
    if run_state.fsm.phase not in _WAVE_BOUNDARY_PHASES:
        raise RefinementBoundaryError(
            f"refinement is only legal at a wave boundary, got {run_state.fsm.phase.value}"
        )
    events = _read_events(event_log)
    if config is None:
        config = RefinementConfig()
    observations = tuple(_normalize_event(event) for event in events)
    selected_store = store or DispatchPolicyStore()
    policy = selected_store.load()
    human_policy = load_policy(selected_store.policy_path)
    proposals = _proposals(observations, policy, human_policy, config)
    capability_updates = _capability_updates(observations, policy, human_policy, config)
    current = to_document(policy)
    candidate = _apply_changes(current, proposals, capability_updates)
    if candidate == current:
        return RefinementResult(False, policy, proposals, capability_updates)
    trigger_names = sorted({_trigger_name(proposal.trigger) for proposal in proposals})
    if capability_updates:
        trigger_names.append("capability_observations")
    trigger = "refinement:" + ",".join(trigger_names)
    refined = selected_store.mutate(
        lambda _document: candidate,
        trigger=trigger,
    )
    return RefinementResult(True, refined, proposals, capability_updates)


def _read_events(event_log: EventLog) -> tuple[EventEnvelope | Mapping[str, object], ...]:
    if isinstance(event_log, ReadResult):
        if event_log.findings:
            raise RefinementError("cannot refine from a ledger with hard read findings")
        if event_log.sequence_status is SequenceStatus.PARTIAL:
            raise RefinementError("cannot refine from a partial ledger sequence")
        return event_log.events
    if not isinstance(event_log, Iterable):
        raise RefinementError("event_log must be a ReadResult or iterable of events")
    return tuple(event_log)


def _normalize_event(
    event: EventEnvelope | Mapping[str, object],
) -> RefinementObservation:
    if isinstance(event, EventEnvelope):
        data = dict(event.data)
        return _observation(
            event.run_seq,
            event.event_type,
            data,
            role=event.role,
            harness=event.harness,
        )
    if not isinstance(event, Mapping):
        raise RefinementError("event log contains a non-object observation")
    raw_data = event.get("data", {})
    if not isinstance(raw_data, Mapping):
        raise RefinementError("event data must be an object")
    data = dict(raw_data)
    data.update({key: value for key, value in event.items() if key != "data"})
    event_type = data.get("event_type", data.get("type"))
    if event_type is not None and not isinstance(event_type, str):
        raise RefinementError("event type must be a string")
    return _observation(
        data.get("run_seq", data.get("seq")),
        event_type,
        data,
        role=_text_or_none(data.get("role")),
        harness=_text_or_none(data.get("harness")),
    )


def _observation(
    seq: object,
    event_type: str | None,
    data: Mapping[str, object],
    *,
    role: str | None,
    harness: str | None,
) -> RefinementObservation:
    if type(seq) is not int or seq < 0:
        raise RefinementError("every refinement observation must cite run_seq")
    task_class = _text_or_none(_first(data, ("task_class", "classification", "task_class_name")))
    outcome = _outcome(data, event_type)
    behavior = _text_or_none(_first(data, ("behavior_policy", "behavior", "policy_behavior")))
    tactic = _text_or_none(_first(data, ("tactic", "repair_pattern", "pattern")))
    delegation = event_type == "agent.spawned"
    raw_delegation = data.get("delegation")
    if raw_delegation is not None:
        if not isinstance(raw_delegation, bool):
            raise RefinementError("delegation must be boolean")
        delegation = raw_delegation
    return RefinementObservation(
        seq=seq,
        task_class=task_class,
        role=role,
        harness=harness,
        outcome=outcome,
        tactic=tactic,
        behavior_policy=behavior,
        delegation=delegation,
    )


def _outcome(data: Mapping[str, object], event_type: str | None) -> Outcome | None:
    explicit = _first(data, ("outcome", "result", "review_state"))
    if explicit is not None:
        if not isinstance(explicit, str):
            raise RefinementError("observation outcome must be a string")
        normalized = explicit.lower().replace(" ", "_")
        if normalized in _SUCCESS_OUTCOMES:
            return Outcome.SUCCESS
        if normalized in _FAILURE_OUTCOMES:
            return Outcome.FAILURE
        raise RefinementError(f"unknown observation outcome: {explicit!r}")
    status = data.get("status")
    if isinstance(status, str):
        normalized = status.lower().replace(" ", "_")
        if normalized in _SUCCESS_OUTCOMES:
            return Outcome.SUCCESS
        if normalized in _FAILURE_OUTCOMES:
            return Outcome.FAILURE
    elif status is not None:
        raise RefinementError("observation status must be a string")
    if event_type in _FAILURE_EVENT_TYPES:
        return Outcome.FAILURE
    if event_type in _SUCCESS_EVENT_TYPES:
        return Outcome.SUCCESS
    return None


def _proposals(
    observations: tuple[RefinementObservation, ...],
    learned: LearnedPolicy,
    policy: HarnessPolicy,
    config: RefinementConfig,
) -> tuple[RefinementProposal, ...]:
    proposals: dict[tuple[str, str, str], RefinementProposal] = {}
    failures: defaultdict[str, list[RefinementObservation]] = defaultdict(list)
    for observation in observations:
        if observation.outcome is Outcome.FAILURE and observation.task_class:
            failures[observation.task_class].append(observation)
    for task_class, matches in failures.items():
        if len(matches) >= config.min_occurrences:
            _add_proposal(
                proposals,
                RefinementProposal(
                    RefinementTrigger.REPEATED_FAILURE,
                    "decomposition_heuristics",
                    task_class,
                    "review failure recurrence before retry",
                    tuple(item.seq for item in matches),
                ),
            )

    tactics: defaultdict[tuple[str, str, str, str], list[RefinementObservation]] = defaultdict(list)
    roles: defaultdict[tuple[str, str, str], list[RefinementObservation]] = defaultdict(list)
    behaviors: defaultdict[tuple[str, str], list[RefinementObservation]] = defaultdict(list)
    for observation in observations:
        if (
            observation.outcome is Outcome.SUCCESS
            and observation.task_class
            and observation.tactic
            and observation.role
            and observation.harness
            and _allowed(policy, observation.role, observation.harness)
        ):
            tactics[
                (
                    observation.task_class,
                    observation.tactic,
                    observation.role,
                    observation.harness,
                )
            ].append(observation)
        if (
            observation.delegation
            and observation.task_class
            and observation.role
            and observation.harness
            and _allowed(policy, observation.role, observation.harness)
        ):
            roles[(observation.task_class, observation.role, observation.harness)].append(
                observation
            )
        if observation.behavior_policy and observation.task_class:
            behaviors[(observation.task_class, observation.behavior_policy)].append(observation)

    for (_task_class, tactic, role, harness), matches in tactics.items():
        if len(matches) >= config.min_occurrences:
            _add_proposal(
                proposals,
                RefinementProposal(
                    RefinementTrigger.RESOLVED_TACTIC,
                    "repair_patterns",
                    f"{tactic}:{role}",
                    harness,
                    tuple(item.seq for item in matches),
                ),
            )
    for (task_class, role, harness), matches in roles.items():
        if len(matches) >= config.min_occurrences:
            _add_proposal(
                proposals,
                RefinementProposal(
                    RefinementTrigger.REPEATED_ROLE,
                    "task_class_preferences",
                    f"{task_class}:{role}",
                    harness,
                    tuple(item.seq for item in matches),
                ),
            )
    for (task_class, behavior), matches in behaviors.items():
        if len(matches) >= config.min_occurrences:
            _add_proposal(
                proposals,
                RefinementProposal(
                    RefinementTrigger.BEHAVIOR_POLICY,
                    "decomposition_heuristics",
                    task_class,
                    behavior,
                    tuple(item.seq for item in matches),
                ),
            )
    return tuple(proposals.values())


def _add_proposal(
    proposals: dict[tuple[str, str, str], RefinementProposal],
    proposal: RefinementProposal,
) -> None:
    identity = (proposal.category, proposal.key, proposal.value)
    previous = proposals.get(identity)
    if previous is None:
        proposals[identity] = proposal
        return
    merged = tuple(dict.fromkeys(previous.evidence_seqs + proposal.evidence_seqs))
    proposals[identity] = RefinementProposal(
        previous.trigger,
        previous.category,
        previous.key,
        previous.value,
        merged,
    )


def _capability_updates(
    observations: tuple[RefinementObservation, ...],
    learned: LearnedPolicy,
    policy: HarnessPolicy,
    config: RefinementConfig,
) -> dict[str, CapabilityObservation]:
    grouped: defaultdict[str, list[RefinementObservation]] = defaultdict(list)
    for observation in observations:
        if (
            observation.harness
            and observation.outcome is not None
            and _allowed_any_role(policy, observation.harness)
        ):
            grouped[observation.harness].append(observation)
    result: dict[str, CapabilityObservation] = {}
    for harness, matches in grouped.items():
        previous = learned.capability_observations.get(harness)
        old_seqs = set(previous.evidence_seqs) if previous else set()
        fresh = [item for item in matches if item.seq not in old_seqs]
        if len(fresh) < config.min_capability_observations:
            continue
        passed = sum(item.outcome is Outcome.SUCCESS for item in fresh)
        failed = sum(item.outcome is Outcome.FAILURE for item in fresh)
        result[harness] = CapabilityObservation(
            passed=passed,
            failed=failed,
            evidence_seqs=tuple(item.seq for item in fresh),
        )
    return result


def _apply_changes(
    document: dict[str, object],
    proposals: tuple[RefinementProposal, ...],
    capability_updates: Mapping[str, CapabilityObservation],
) -> dict[str, object]:
    candidate = copy.deepcopy(document)
    evidence = candidate.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        raise RefinementError("dispatch-policy.evidence must be an object")
    for proposal in proposals:
        _append_learning(candidate, evidence, proposal)
    capability = candidate.setdefault("capability_observations", {})
    if not isinstance(capability, dict):
        raise RefinementError("dispatch-policy.capability_observations must be an object")
    for harness, update in capability_updates.items():
        raw = capability.get(harness)
        if raw is None:
            raw = {"passed": 0, "failed": 0, "evidence_seqs": []}
            capability[harness] = raw
        if not isinstance(raw, dict):
            raise RefinementError(f"capability observation for {harness!r} is malformed")
        old_seqs = raw.get("evidence_seqs", [])
        if not isinstance(old_seqs, list):
            raise RefinementError(f"capability evidence for {harness!r} is malformed")
        raw["passed"] = raw.get("passed", 0) + update.passed
        raw["failed"] = raw.get("failed", 0) + update.failed
        raw["evidence_seqs"] = list(dict.fromkeys(old_seqs + list(update.evidence_seqs)))
    return candidate


def _append_learning(
    document: dict[str, object],
    evidence: dict[str, object],
    proposal: RefinementProposal,
) -> None:
    if proposal.category == "decomposition_heuristics":
        table = document[proposal.category]
        if not isinstance(table, dict):
            raise RefinementError(f"{proposal.category} must be an object")
        values = table.setdefault(proposal.key, [])
        if not isinstance(values, list):
            raise RefinementError(f"{proposal.category}.{proposal.key} must be an array")
        if proposal.value not in values:
            values.append(proposal.value)
    elif proposal.category == "repair_patterns":
        _append_preference(document, proposal, task_class=False)
    elif proposal.category == "task_class_preferences":
        _append_preference(document, proposal, task_class=True)
    else:
        raise RefinementError(f"unknown learned-policy category: {proposal.category!r}")
    current = evidence.setdefault(_evidence_key(proposal), [])
    if not isinstance(current, list):
        raise RefinementError(f"evidence for {_evidence_key(proposal)!r} is malformed")
    evidence[_evidence_key(proposal)] = list(dict.fromkeys(current + list(proposal.evidence_seqs)))


def _append_preference(
    document: dict[str, object],
    proposal: RefinementProposal,
    *,
    task_class: bool,
) -> None:
    table = document[proposal.category]
    if not isinstance(table, dict):
        raise RefinementError(f"{proposal.category} must be an object")
    parts = proposal.key.split(":")
    if len(parts) != 2:
        raise RefinementError(f"preference key must contain task/role: {proposal.key!r}")
    first, role = parts
    if task_class and first not in _TASK_CLASSES:
        raise RefinementError(f"unknown task class: {first!r}")
    roles = table.setdefault(first, {})
    if not isinstance(roles, dict):
        raise RefinementError(f"{proposal.category}.{first} must be an object")
    values = roles.setdefault(role, [])
    if not isinstance(values, list):
        raise RefinementError(f"{proposal.category}.{first}.{role} must be an array")
    if proposal.value not in values:
        values.append(proposal.value)


def _evidence_key(proposal: RefinementProposal) -> str:
    return f"{proposal.category}:{proposal.key}"


def _trigger_name(trigger: RefinementTrigger | str) -> str:
    return trigger.value if isinstance(trigger, RefinementTrigger) else trigger


def _allowed(policy: HarnessPolicy, role: str, harness: str) -> bool:

    return role in ROLE_NAMES and harness in policy.roles[role].allow


def _allowed_any_role(policy: HarnessPolicy, harness: str) -> bool:
    return any(harness in role.allow for role in policy.roles.values())


def _first(data: Mapping[str, object], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RefinementError("event text fields must be null or non-empty strings")
    return value


__all__ = [
    "EventLog",
    "Outcome",
    "RefinementBoundaryError",
    "RefinementConfig",
    "RefinementError",
    "RefinementObservation",
    "RefinementProposal",
    "RefinementResult",
    "RefinementTrigger",
    "UnevidencedProposal",
    "maybe_refine",
]
