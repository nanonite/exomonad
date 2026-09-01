"""Ambiguity-safe ownership resolution for watcher lifecycle observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

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
        if "repository_identity_conflict" in self.rejected_aliases:
            return "repository_identity_conflict"
        if "pr_number_only" in self.rejected_aliases:
            return "pr_number_only"
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
    repository_conflict = _repository_identity_conflict(event, state)
    if repository_conflict is not None:
        rejected.add(repository_conflict)

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
        if event.head_sha is not None:
            head_matches = {
                slice_id
                for slice_id, current in state.slices.items()
                if slice_id in allowed
                and _published_head_matches(current, event.pr_number, event.head_sha)
            }
            if head_matches:
                evidence.setdefault("published_head", set()).update(head_matches)

    candidates = tuple(sorted({slice_id for values in evidence.values() for slice_id in values}))
    if len(candidates) == 1 and set(evidence) == {"pr_number"}:
        rejected.add("pr_number_only")
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


def _published_head_matches(current: SliceState, pr_number: int, head_sha: str) -> bool:
    publication = getattr(current, "publication", None)
    expected_pr = (
        publication.pr_number if publication is not None else getattr(current, "pr_number", None)
    )
    expected_head = (
        publication.head_sha if publication is not None else getattr(current, "reviewed_head", None)
    )
    return expected_pr == pr_number and expected_head == head_sha


def _repository_identity_conflict(event: EventEnvelope, state: RunState) -> str | None:
    """Reject watcher facts that contradict the persisted Forgejo identity."""
    identity = getattr(state, "repository_identity", None)
    if identity is None:
        return None
    payload: Mapping[str, object] = event.data
    nested = payload.get("repository")
    if isinstance(nested, Mapping):
        payload = {**nested, **payload}
    owner = _first_text(payload, "owner", "repository_owner", "repo_owner")
    repo = _first_text(payload, "repo", "repository", "repo_name")
    host = _first_text(payload, "forge_host", "host", "forgejo_host")
    remote_url = _first_text(payload, "remote_url", "repository_url", "forge_url")
    if owner is not None and owner != getattr(identity, "owner", None):
        return "repository_identity_conflict"
    if repo is not None and repo != getattr(identity, "repo", None):
        return "repository_identity_conflict"
    expected_host = getattr(identity, "forge_host", None) or _url_host(
        getattr(identity, "remote_url", None)
    )
    actual_host = host or _url_host(remote_url)
    if (
        expected_host is not None
        and actual_host is not None
        and actual_host.lower() != expected_host.lower()
    ):
        return "repository_identity_conflict"
    return None


def _first_text(payload: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _url_host(value: str | None) -> str | None:
    if value is None:
        return None
    return urlsplit(value).hostname


def envelope_document(event: EventEnvelope) -> dict[str, object]:
    """Serialize an envelope for durable quarantine without changing its type."""
    return {
        "type": event.event_type,
        "run_seq": event.run_seq,
        "event_id": event.event_id,
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
