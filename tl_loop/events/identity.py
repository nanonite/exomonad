"""Ambiguity-safe ownership resolution for watcher lifecycle observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from tl_loop.state.schema import RunState, SliceState

from .envelope import EventEnvelope


@dataclass(frozen=True)
class IdentityResolution:
    """The result of matching one event against persisted slice ownership."""

    slice_id: str | None
    candidates: tuple[str, ...]
    evidence: Mapping[str, tuple[str, ...]]
    rejected_aliases: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        """Return whether exactly one non-contradictory owner was proven."""
        return self.slice_id is not None and len(self.candidates) == 1

    @property
    def reason(self) -> str:
        """Return a stable diagnostic classification for the result."""
        if self.rejected_aliases:
            return "unknown_alias"
        if not self.candidates:
            return "unresolved"
        if len(self.candidates) > 1:
            return "ambiguous"
        return "resolved"


def resolve_event_slice(
    event: EventEnvelope,
    state: RunState,
    *,
    allowed_ids: Iterable[str] | None = None,
) -> IdentityResolution:
    """Resolve an event through authoritative persisted ownership aliases.

    Exact matches are intentionally the only accepted form. In particular,
    branch prefixes and harness suffixes are evidence, not parsing rules.
    Every alias must identify the same slice; otherwise the observation is
    quarantined instead of being routed by guesswork.
    """

    allowed = set(allowed_ids) if allowed_ids else set(state.slices)
    evidence: dict[str, set[str]] = {}
    rejected: set[str] = set()

    def add_alias(kind: str, value: object, *, reject_unknown: bool = True) -> None:
        if not isinstance(value, str) or not value:
            return
        matches = {
            slice_id
            for slice_id, current in state.slices.items()
            if slice_id in allowed and _alias_matches(kind, value, slice_id, current)
        }
        if matches:
            evidence.setdefault(kind, set()).update(matches)
        elif reject_unknown:
            rejected.add(f"{kind}={value}")

    add_alias("slice_id", event.slice_id)
    add_alias("slice_id", event.data.get("slice_id"))
    add_alias("intent_id", event.data.get("intent_id"))
    add_alias("intent_id", event.data.get("dispatch_intent_id"))
    add_alias("agent_id", event.agent_id, reject_unknown=False)
    for key in ("agent_id", "child_agent", "owner_id"):
        add_alias("agent_id", event.data.get(key), reject_unknown=False)
    add_alias("agent_id", event.data.get("dispatch_agent_id"))
    add_alias("branch", event.data.get("branch"), reject_unknown=False)
    add_alias("branch", event.data.get("head_branch"), reject_unknown=False)
    add_alias("branch", event.agent_id, reject_unknown=False)
    if event.pr_number is not None:
        matches = {
            slice_id
            for slice_id, current in state.slices.items()
            if slice_id in allowed and getattr(current, "pr_number", None) == event.pr_number
        }
        if matches:
            evidence.setdefault("pr_number", set()).update(matches)

    candidates = tuple(sorted({slice_id for values in evidence.values() for slice_id in values}))
    slice_id = candidates[0] if len(candidates) == 1 and not rejected else None
    return IdentityResolution(
        slice_id=slice_id,
        candidates=candidates,
        evidence=MappingProxyType(
            {key: tuple(sorted(values)) for key, values in sorted(evidence.items())}
        ),
        rejected_aliases=tuple(sorted(rejected)),
    )


def _alias_matches(kind: str, value: str, slice_id: str, current: SliceState) -> bool:
    if kind == "slice_id":
        return value == slice_id
    if kind == "intent_id":
        return value == getattr(current, "dispatch_intent_id", None)
    if kind == "agent_id":
        return value in {slice_id, getattr(current, "dispatch_agent_id", None)}
    if kind == "branch":
        return value == getattr(current, "branch", None)
    raise ValueError(f"unsupported ownership alias {kind!r}")


def envelope_document(event: EventEnvelope) -> dict[str, object]:
    """Serialize an envelope for durable quarantine without changing its type."""
    return {
        "type": event.event_type,
        "run_seq": event.run_seq,
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "invocation_id": event.invocation_id,
        "generation": event.generation,
        "harness": event.harness,
        "role": event.role,
        "lifecycle_state": event.lifecycle_state,
        "observed_at": event.observed_at,
        "parent_agent_id": event.parent_agent_id,
        "data": dict(event.data),
    }
