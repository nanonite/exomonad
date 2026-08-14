"""Fail-closed harness capability ratings."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import (
    DEFAULT_POLICY_PATH,
    HarnessPolicy,
    PolicyInvalid,
    PolicyMissing,
    load_policy,
)

DEFAULT_CAPABILITY_PATH = Path(".exo/harness_capability.toml")
DEFAULT_CAPABILITY_CONTENT = """# Static capability ratings. Each entry records the operator's basis.

[capabilities]
"""
_CAPABILITY_KEYS = frozenset({"capabilities"})
_DIFFICULTY_RANK = {
    Difficulty.TRIVIAL: 0,
    Difficulty.STANDARD: 1,
    Difficulty.HARD: 2,
}


@dataclass(frozen=True)
class CapabilityMap:
    """Maximum supported difficulty for each harness/model entry."""

    ratings: Mapping[str, Difficulty]

    def is_capable(self, harness_model: str, difficulty: Difficulty) -> bool:
        """Return whether a known harness meets the requested difficulty."""
        maximum = self.ratings.get(harness_model)
        return maximum is not None and _DIFFICULTY_RANK[maximum] >= _DIFFICULTY_RANK[difficulty]

    def __getitem__(self, harness_model: str) -> Difficulty:
        return self.ratings[harness_model]


def load_capability(
    path: str | Path = DEFAULT_CAPABILITY_PATH,
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> CapabilityMap:
    """Load capability data and verify it covers every allowed harness."""
    target = Path(path)
    try:
        with target.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError as error:
        raise PolicyMissing(f"{target}: capability file is missing") from error
    except tomllib.TOMLDecodeError as error:
        raise PolicyInvalid(f"{target}: invalid TOML: {error}") from error
    policy = load_policy(policy_path)
    try:
        return validate_capability(document, policy)
    except PolicyInvalid as error:
        raise PolicyInvalid(f"{target}: {error}") from error


def load_capabilities(
    path: str | Path = DEFAULT_CAPABILITY_PATH,
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> CapabilityMap:
    """Plural alias for callers that load the complete capability map."""
    return load_capability(path, policy_path=policy_path)


def validate_capability(document: object, policy: HarnessPolicy) -> CapabilityMap:
    """Validate decoded capability data against a validated harness policy."""
    root = _mapping(document, "capability")
    _reject_unknown(root, _CAPABILITY_KEYS, "capability")
    entries = _mapping(_required(root, "capabilities", "capability"), "capability.capabilities")
    ratings = {
        harness: _difficulty(value, f"capability.capabilities.{harness}")
        for harness, value in entries.items()
    }
    _require_policy_coverage(ratings, policy)
    return CapabilityMap(MappingProxyType(ratings))


def is_capable(
    harness_model: str,
    difficulty: Difficulty,
    capability_map: CapabilityMap | None = None,
) -> bool:
    """Check a harness against a capability map, loading the authoritative map by default."""
    selected = capability_map or load_capability()
    return selected.is_capable(harness_model, difficulty)


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PolicyInvalid(f"{path}: must be a table")
    if any(not isinstance(key, str) for key in value):
        raise PolicyInvalid(f"{path}: keys must be strings")
    return cast(Mapping[str, object], value)


def _required(table: Mapping[str, object], key: str, path: str) -> object:
    if key not in table:
        raise PolicyInvalid(f"{path}.{key}: required key is missing")
    return table[key]


def _reject_unknown(table: Mapping[str, object], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise PolicyInvalid(f"{path}: unknown key(s): {', '.join(unknown)}")


def _difficulty(value: object, path: str) -> Difficulty:
    if not isinstance(value, str):
        raise PolicyInvalid(f"{path}: must be one of trivial, standard, hard")
    try:
        return Difficulty(value)
    except ValueError as error:
        raise PolicyInvalid(f"{path}: unknown difficulty {value!r}") from error


def _require_policy_coverage(
    ratings: Mapping[str, Difficulty],
    policy: HarnessPolicy,
) -> None:
    allowed = {
        harness
        for role in policy.roles.values()
        for harness in role.allow
    }
    missing = sorted(allowed - set(ratings))
    if missing:
        raise PolicyInvalid(
            "capability.capabilities: missing capability entry for " + ", ".join(missing)
        )


__all__ = [
    "DEFAULT_CAPABILITY_PATH",
    "DEFAULT_CAPABILITY_CONTENT",
    "CapabilityMap",
    "is_capable",
    "load_capabilities",
    "load_capability",
    "validate_capability",
]
