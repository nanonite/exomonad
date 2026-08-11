"""Durable, allowlist-bounded learned dispatch policy."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast

from tl_loop.select.classify import CLASSIFICATION_RULES
from tl_loop.select.policy import (
    DEFAULT_POLICY_PATH,
    ROLE_NAMES,
    HarnessPolicy,
    PolicyInvalid,
    load_policy,
)
from tl_loop.state.write import Document, WriteHooks, apply, publish

DEFAULT_LEARNED_POLICY_PATH = Path(".exo/tl-loop/dispatch-policy.json")
DEFAULT_SNAPSHOT_DIR = Path(".exo/tl-loop/dispatch-policy.snapshots")
LEARNED_POLICY_VERSION = 1
_ROOT_KEYS = frozenset(
    {
        "version",
        "revision",
        "decomposition_heuristics",
        "task_class_preferences",
        "repair_patterns",
        "evidence",
        "capability_observations",
        "history",
    }
)
_CAPABILITY_OBSERVATION_KEYS = frozenset({"passed", "failed", "evidence_seqs"})
_HISTORY_KEYS = frozenset({"revision", "trigger", "created_at"})
_TASK_CLASSES = frozenset(rule.name for rule in CLASSIFICATION_RULES)


class LearnedPolicyInvalid(PolicyInvalid):
    """Raised when learned policy data is malformed or out of bounds."""


@dataclass(frozen=True)
class PolicyHistoryEntry:
    """One durable policy mutation and its human-readable trigger."""

    revision: int
    trigger: str
    created_at: str


@dataclass(frozen=True)
class CapabilityObservation:
    """Observed harness outcomes, each backed by immutable ledger sequences."""

    passed: int
    failed: int
    evidence_seqs: tuple[int, ...]


@dataclass(frozen=True)
class LearnedPolicy:
    """Validated learned choices that cannot widen the human harness policy."""

    version: int
    revision: int
    evidence: Mapping[str, tuple[int, ...]]
    capability_observations: Mapping[str, CapabilityObservation]
    decomposition_heuristics: Mapping[str, tuple[str, ...]]
    task_class_preferences: Mapping[str, Mapping[str, tuple[str, ...]]]
    repair_patterns: Mapping[str, Mapping[str, tuple[str, ...]]]
    history: tuple[PolicyHistoryEntry, ...]

    def preference_order(self, task_class: str, role: str) -> tuple[str, ...]:
        """Return the learned tie-break order for one classified role."""
        return self.task_class_preferences.get(task_class, {}).get(role, ())


def default_document() -> Document:
    """Return the empty versioned document used before the first mutation."""
    return {
        "version": LEARNED_POLICY_VERSION,
        "revision": 0,
        "evidence": {},
        "capability_observations": {},
        "decomposition_heuristics": {},
        "task_class_preferences": {},
        "repair_patterns": {},
        "history": [],
    }


def validate_learned_policy(document: object, policy: HarnessPolicy) -> LearnedPolicy:
    """Validate learned data and enforce the authoritative allowlist boundary."""
    root = _mapping(document, "dispatch-policy")
    _reject_unknown(root, _ROOT_KEYS, "dispatch-policy")
    version = _version(root.get("version"), "dispatch-policy.version")
    revision = _non_negative_int(root.get("revision"), "dispatch-policy.revision")
    heuristics = _parse_heuristics(
        _required(root, "decomposition_heuristics", "dispatch-policy"),
        "dispatch-policy.decomposition_heuristics",
    )
    preferences = _parse_preferences(
        _required(root, "task_class_preferences", "dispatch-policy"),
        "dispatch-policy.task_class_preferences",
        policy,
        task_classes=True,
    )
    repairs = _parse_preferences(
        _required(root, "repair_patterns", "dispatch-policy"),
        "dispatch-policy.repair_patterns",
        policy,
        task_classes=False,
    )
    evidence = _parse_evidence(
        root.get("evidence", {}),
        "dispatch-policy.evidence",
        heuristics,
        preferences,
        repairs,
    )
    capability_observations = _parse_capability_observations(
        root.get("capability_observations", {}),
        "dispatch-policy.capability_observations",
        policy,
    )
    history = _parse_history(
        _required(root, "history", "dispatch-policy"),
        "dispatch-policy.history",
    )
    if history and history[-1].revision > revision:
        raise LearnedPolicyInvalid(
            "dispatch-policy.history: latest revision exceeds document revision"
        )
    return LearnedPolicy(
        version=version,
        revision=revision,
        decomposition_heuristics=MappingProxyType(heuristics),
        task_class_preferences=MappingProxyType(preferences),
        repair_patterns=MappingProxyType(repairs),
        evidence=MappingProxyType(evidence),
        capability_observations=MappingProxyType(capability_observations),
        history=history,
    )


def load_learned_policy(
    path: str | Path = DEFAULT_LEARNED_POLICY_PATH,
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> LearnedPolicy:
    """Load learned data, treating an absent document as an empty policy."""
    policy = load_policy(policy_path)
    target = Path(path)
    if not target.exists():
        return validate_learned_policy(default_document(), policy)
    try:
        with target.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LearnedPolicyInvalid(f"{target}: cannot read JSON: {error}") from error
    try:
        return validate_learned_policy(document, policy)
    except LearnedPolicyInvalid as error:
        raise LearnedPolicyInvalid(f"{target}: {error}") from error


class DispatchPolicyStore:
    """Atomic learned-policy mutations with revision snapshots and rollback."""

    def __init__(
        self,
        path: str | Path = DEFAULT_LEARNED_POLICY_PATH,
        *,
        policy_path: str | Path = DEFAULT_POLICY_PATH,
        snapshot_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
    ) -> None:
        self.path = Path(path)
        self.policy_path = Path(policy_path)
        self.snapshot_dir = Path(snapshot_dir)

    def load(self) -> LearnedPolicy:
        """Return the current learned policy or the empty default."""
        return load_learned_policy(self.path, policy_path=self.policy_path)

    def history(self) -> tuple[PolicyHistoryEntry, ...]:
        """List durable mutations in revision order."""
        return self.load().history

    def mutate(
        self,
        mutator: Callable[[Document], Mapping[str, object]],
        *,
        evidence: Mapping[str, Sequence[int]] | None = None,
        trigger: str,
    ) -> LearnedPolicy:
        """Validate, snapshot, and atomically apply one policy mutation."""
        _require_text(trigger, "trigger")
        policy = load_policy(self.policy_path)
        validator = _validator(policy)
        self._ensure_document(validator)

        def snapshot_before_replace() -> None:
            previous = _read_document(self.path, validator)
            revision = _revision(previous)
            snapshot = self.snapshot_dir / f"{revision}.json"
            if snapshot.exists():
                existing = _read_document(snapshot, validator)
                if existing != previous:
                    raise LearnedPolicyInvalid(
                        f"snapshot {snapshot} already contains a different revision"
                    )
                return
            publish(snapshot, previous, validator=validator)

        def apply_mutator(document: Document) -> Document:
            candidate_value = mutator(copy.deepcopy(document))
            if not isinstance(candidate_value, Mapping):
                raise TypeError("learned-policy mutator must return an object")
            candidate = dict(candidate_value)
            if evidence is not None:
                candidate["evidence"] = _merge_evidence(document.get("evidence", {}), evidence)
            history = _history_documents(document.get("history"))
            history.append(
                {
                    "revision": _revision(document) + 1,
                    "trigger": trigger,
                    "created_at": _now(),
                }
            )
            candidate["history"] = history
            return candidate

        result = apply(
            self.path.parent,
            apply_mutator,
            target_name=self.path.name,
            lock_name=f".{self.path.stem}.lock",
            validator=validator,
            hooks=WriteHooks(before_rename=snapshot_before_replace),
        )
        return validate_learned_policy(result, policy)

    def replace(
        self,
        document: Mapping[str, object] | LearnedPolicy,
        *,
        trigger: str,
    ) -> LearnedPolicy:
        """Replace learned data through the same validated mutation path."""
        replacement = (
            to_document(document) if isinstance(document, LearnedPolicy) else dict(document)
        )
        return self.mutate(lambda _current: replacement, trigger=trigger)

    def rollback(self, revision: int) -> LearnedPolicy:
        """Restore a prior snapshot while recording a new rollback revision."""
        _require_non_negative(revision, "revision")
        snapshot = self.snapshot_dir / f"{revision}.json"
        policy = load_policy(self.policy_path)
        validator = _validator(policy)
        return self.replace(_read_document(snapshot, validator), trigger=f"rollback:{revision}")

    def _ensure_document(self, validator: Callable[[object], None]) -> None:
        if self.path.exists():
            return
        try:
            apply(
                self.path.parent,
                lambda document: document,
                target_name=self.path.name,
                lock_name=f".{self.path.stem}.lock",
                validator=validator,
                initial=default_document(),
            )
        except FileExistsError:
            return


def to_document(policy: LearnedPolicy) -> Document:
    """Serialize a validated policy without runtime-only objects."""
    return {
        "version": policy.version,
        "revision": policy.revision,
        "decomposition_heuristics": {
            key: list(values) for key, values in policy.decomposition_heuristics.items()
        },
        "task_class_preferences": _serialize_preferences(policy.task_class_preferences),
        "repair_patterns": _serialize_preferences(policy.repair_patterns),
        "evidence": {key: list(seqs) for key, seqs in policy.evidence.items()},
        "capability_observations": {
            harness: {
                "passed": observation.passed,
                "failed": observation.failed,
                "evidence_seqs": list(observation.evidence_seqs),
            }
            for harness, observation in policy.capability_observations.items()
        },
        "history": [
            {
                "revision": event.revision,
                "trigger": event.trigger,
                "created_at": event.created_at,
            }
            for event in policy.history
        ],
    }


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LearnedPolicyInvalid(f"{path}: must be an object")
    if any(not isinstance(key, str) for key in value):
        raise LearnedPolicyInvalid(f"{path}: keys must be strings")
    return cast(Mapping[str, object], value)


def _required(table: Mapping[str, object], key: str, path: str) -> object:
    if key not in table:
        raise LearnedPolicyInvalid(f"{path}.{key}: required key is missing")
    return table[key]


def _reject_unknown(table: Mapping[str, object], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise LearnedPolicyInvalid(f"{path}: unknown key(s): {', '.join(unknown)}")


def _version(value: object, path: str) -> int:
    if type(value) is not int or value != LEARNED_POLICY_VERSION:
        raise LearnedPolicyInvalid(f"{path}: must equal supported version {LEARNED_POLICY_VERSION}")
    return cast(int, value)


def _non_negative_int(value: object, path: str) -> int:
    if type(value) is not int or value < 0:
        raise LearnedPolicyInvalid(f"{path}: must be a non-negative integer")
    return cast(int, value)


def _require_non_negative(value: object, path: str) -> int:
    return _non_negative_int(value, path)


def _require_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise LearnedPolicyInvalid(f"{path}: must be a non-empty string")
    return value


def _parse_heuristics(value: object, path: str) -> dict[str, tuple[str, ...]]:
    table = _mapping(value, path)
    result: dict[str, tuple[str, ...]] = {}
    for task_class, raw_values in table.items():
        _task_class(task_class, path)
        result[task_class] = _strings(raw_values, f"{path}.{task_class}")
    return result


def _parse_preferences(
    value: object,
    path: str,
    policy: HarnessPolicy,
    *,
    task_classes: bool,
) -> dict[str, dict[str, tuple[str, ...]]]:
    table = _mapping(value, path)
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for key, raw_roles in table.items():
        if task_classes:
            _task_class(key, path)
        else:
            _require_text(key, f"{path}.{key}")
        roles = _mapping(raw_roles, f"{path}.{key}")
        _reject_unknown(roles, frozenset(ROLE_NAMES), f"{path}.{key}")
        parsed_roles: dict[str, tuple[str, ...]] = {}
        for role, raw_harnesses in roles.items():
            harnesses = _strings(raw_harnesses, f"{path}.{key}.{role}")
            allowed = policy.roles[role].allow
            outside = sorted(set(harnesses) - set(allowed))
            if outside:
                raise LearnedPolicyInvalid(
                    f"{path}.{key}.{role}: harness not present in allow: {', '.join(outside)}"
                )
            parsed_roles[role] = harnesses
        result[key] = parsed_roles
    return result


def _parse_history(value: object, path: str) -> tuple[PolicyHistoryEntry, ...]:
    if not isinstance(value, list):
        raise LearnedPolicyInvalid(f"{path}: must be an array")
    entries: list[PolicyHistoryEntry] = []
    previous = -1
    for index, raw_entry in enumerate(value):
        entry = _mapping(raw_entry, f"{path}[{index}]")
        _reject_unknown(entry, _HISTORY_KEYS, f"{path}[{index}]")
        revision = _non_negative_int(
            _required(entry, "revision", f"{path}[{index}]"),
            f"{path}[{index}].revision",
        )
        if revision <= previous:
            raise LearnedPolicyInvalid(f"{path}[{index}].revision: entries must be increasing")
        trigger = _require_text(
            _required(entry, "trigger", f"{path}[{index}]"),
            f"{path}[{index}].trigger",
        )
        created_at = _require_text(
            _required(entry, "created_at", f"{path}[{index}]"),
            f"{path}[{index}].created_at",
        )
        entries.append(PolicyHistoryEntry(revision, trigger, created_at))
        previous = revision
    return tuple(entries)


def _parse_evidence(
    value: object,
    path: str,
    heuristics: Mapping[str, tuple[str, ...]],
    preferences: Mapping[str, Mapping[str, tuple[str, ...]]],
    repairs: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> dict[str, tuple[int, ...]]:
    table = _mapping(value, path)
    expected: set[str] = set()
    expected.update(
        f"decomposition_heuristics:{task_class}"
        for task_class, values in heuristics.items()
        if values
    )
    expected.update(
        f"task_class_preferences:{task_class}:{role}"
        for task_class, roles in preferences.items()
        for role, values in roles.items()
        if values
    )
    expected.update(
        f"repair_patterns:{pattern}:{role}"
        for pattern, roles in repairs.items()
        for role, values in roles.items()
        if values
    )
    unknown = sorted(set(table) - expected)
    if unknown:
        raise LearnedPolicyInvalid(
            f"{path}: evidence has no corresponding learned entry: {', '.join(unknown)}"
        )
    result = {key: _sequences(raw, f"{path}.{key}") for key, raw in table.items()}
    missing = sorted(expected - set(result))
    if missing:
        raise LearnedPolicyInvalid(
            f"{path}: learned entry is missing evidence: {', '.join(missing)}"
        )
    return result


def _parse_capability_observations(
    value: object,
    path: str,
    policy: HarnessPolicy,
) -> dict[str, CapabilityObservation]:
    table = _mapping(value, path)
    allowed = {harness for role in policy.roles.values() for harness in role.allow}
    result: dict[str, CapabilityObservation] = {}
    for harness, raw_observation in table.items():
        if harness not in allowed:
            raise LearnedPolicyInvalid(f"{path}.{harness}: harness is not present in allow")
        observation = _mapping(raw_observation, f"{path}.{harness}")
        _reject_unknown(observation, _CAPABILITY_OBSERVATION_KEYS, f"{path}.{harness}")
        passed = _non_negative_int(
            _required(observation, "passed", f"{path}.{harness}"),
            f"{path}.{harness}.passed",
        )
        failed = _non_negative_int(
            _required(observation, "failed", f"{path}.{harness}"),
            f"{path}.{harness}.failed",
        )
        if passed + failed == 0:
            raise LearnedPolicyInvalid(f"{path}.{harness}: at least one outcome is required")
        evidence = _sequences(
            _required(observation, "evidence_seqs", f"{path}.{harness}"),
            f"{path}.{harness}.evidence_seqs",
        )
        result[harness] = CapabilityObservation(passed, failed, evidence)
    return result


def _task_class(value: str, path: str) -> None:
    if value not in _TASK_CLASSES:
        names = ", ".join(sorted(_TASK_CLASSES))
        raise LearnedPolicyInvalid(f"{path}: unknown task class {value!r}; expected {names}")


def _strings(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LearnedPolicyInvalid(f"{path}: must be an array of strings")
    values = tuple(value)
    if any(not isinstance(item, str) or not item for item in values):
        raise LearnedPolicyInvalid(f"{path}: must contain non-empty strings")
    if len(values) != len(set(values)):
        raise LearnedPolicyInvalid(f"{path}: entries must be unique")
    return cast(tuple[str, ...], values)


def _sequences(value: object, path: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LearnedPolicyInvalid(f"{path}: must be an array of sequence numbers")
    values = tuple(value)
    if any(type(item) is not int or item < 0 for item in values):
        raise LearnedPolicyInvalid(f"{path}: must contain unique non-negative sequence numbers")
    if not values:
        raise LearnedPolicyInvalid(f"{path}: must contain at least one sequence number")
    if len(values) != len(set(values)):
        raise LearnedPolicyInvalid(f"{path}: entries must be unique")
    return cast(tuple[int, ...], values)


def _merge_evidence(current: object, updates: Mapping[str, Sequence[int]]) -> dict[str, list[int]]:
    merged = {
        key: list(_sequences(value, f"dispatch-policy.evidence.{key}"))
        for key, value in _mapping(current, "dispatch-policy.evidence").items()
    }
    for key, values in updates.items():
        if not isinstance(key, str) or not key:
            raise LearnedPolicyInvalid("dispatch-policy.evidence keys must be non-empty strings")
        additions = _sequences(values, f"dispatch-policy.evidence.{key}")
        merged[key] = list(dict.fromkeys(merged.get(key, []) + list(additions)))
    return merged


def _serialize_preferences(
    preferences: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> dict[str, dict[str, list[str]]]:
    return {
        key: {role: list(harnesses) for role, harnesses in roles.items()}
        for key, roles in preferences.items()
    }


def _history_documents(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LearnedPolicyInvalid("dispatch-policy.history: must be an array")
    return [dict(cast(Mapping[str, object], entry)) for entry in value]


def _revision(document: Mapping[str, object]) -> int:
    return _non_negative_int(document.get("revision"), "dispatch-policy.revision")


def _validator(policy: HarnessPolicy) -> Callable[[object], None]:
    def validate(value: object) -> None:
        validate_learned_policy(value, policy)

    return validate


def _read_document(path: Path, validator: Callable[[object], None]) -> Document:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LearnedPolicyInvalid(f"{path}: cannot read JSON: {error}") from error
    if not isinstance(value, dict):
        raise LearnedPolicyInvalid(f"{path}: must contain an object")
    validator(value)
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DEFAULT_LEARNED_POLICY_PATH",
    "DEFAULT_SNAPSHOT_DIR",
    "LEARNED_POLICY_VERSION",
    "CapabilityObservation",
    "DispatchPolicyStore",
    "LearnedPolicy",
    "LearnedPolicyInvalid",
    "PolicyHistoryEntry",
    "default_document",
    "load_learned_policy",
    "to_document",
    "validate_learned_policy",
]
