"""Shared closed event vocabularies."""

from enum import Enum


class BlockCause(str, Enum):
    """Closed, aggregate-safe vocabulary for why a task is blocked."""

    BASE_CI_UNSTABLE = "base_ci_unstable"
    EXTERNAL_DEPENDENCY = "external_dependency"
    SCOPE_BOUNDARY = "scope_boundary"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    TOOLING_UNAVAILABLE = "tooling_unavailable"


__all__ = ["BlockCause"]
