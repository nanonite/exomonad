"""Parse policy harness identities at the spawn boundary."""

from __future__ import annotations

from dataclasses import dataclass


SUPPORTED_AGENT_TYPES = frozenset({"claude", "codex", "opencode", "shoal"})


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
