"""Parse policy harness identities at the spawn boundary."""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_AGENT_TYPES = frozenset({"claude", "codex", "opencode", "shoal"})

_RUNTIME_LABELS = {
    "Claude": "claude",
    "claude": "claude",
    "Codex": "codex",
    "codex": "codex",
    "OpenCode": "opencode",
    "opencode": "opencode",
    "Shoal": "shoal",
    "shoal": "shoal",
}


def normalize_runtime_label(value: object) -> str | None:
    """Return a canonical runtime for an exact supported wire or Rust Debug label."""
    return _RUNTIME_LABELS.get(value) if isinstance(value, str) else None


def canonical_agent_type(value: str) -> str | None:
    """Return the agent type of a harness or ``harness/model`` identifier.

    This is the single Python-side spelling used by the selector, the policy
    loader, and the explicit-switch guard. ``None`` means the value is not a
    supported harness identity.
    """
    normalized = normalize_runtime_label(value)
    if normalized is not None:
        return normalized
    try:
        return parse_harness_identifier(value).agent_type
    except ValueError:
        return None


@dataclass(frozen=True)
class HarnessRoute:
    """The runtime agent type and optional model selected by policy."""

    harness: str
    agent_type: str
    model: str | None


def parse_harness_identifier(identifier: str) -> HarnessRoute:
    """Split ``agent_type/model`` while rejecting unsupported identifiers."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("harness identifier must be a non-empty string")
    harness = identifier.strip()
    agent_type, separator, model = harness.partition("/")
    agent_type = agent_type.strip().lower()
    if agent_type not in SUPPORTED_AGENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_AGENT_TYPES))
        raise ValueError(f"unsupported agent type {agent_type!r}; use one of {supported}")
    if not separator:
        return HarnessRoute(harness, agent_type, None)
    model = model.strip()
    if not model:
        raise ValueError("model-qualified harness must include a non-empty model")
    return HarnessRoute(harness, agent_type, model)
