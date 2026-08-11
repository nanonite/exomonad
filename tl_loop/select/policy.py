"""Load and validate the human-authored TL harness policy."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

DEFAULT_POLICY_PATH = Path(".exo/harness_policy.toml")
ROLE_NAMES = ("tl", "worker", "reviewer")
_POLICY_KEYS = frozenset({"roles"})
_ROLE_KEYS = frozenset(
    {"allow", "cost_rank", "token_budget", "per_harness_budget", "escalate_after_attempts"}
)
StringMap: TypeAlias = Mapping[str, int]


class PolicyError(ValueError):
    """Base class for fail-closed harness-policy errors."""


class PolicyMissing(PolicyError):
    """Raised when the authoritative policy file does not exist."""


class PolicyInvalid(PolicyError):
    """Raised when the authoritative policy cannot be validated."""


@dataclass(frozen=True)
class RolePolicy:
    """Validated harness choices and ceilings for one controller role."""

    allow: tuple[str, ...]
    cost_rank: StringMap
    token_budget: int
    per_harness_budget: StringMap
    escalate_after_attempts: int


@dataclass(frozen=True)
class HarnessPolicy:
    """Validated policy for all roles used by the TL loop."""

    roles: Mapping[str, RolePolicy]


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> HarnessPolicy:
    """Load the authoritative TOML policy, refusing missing or invalid files."""
    target = Path(path)
    try:
        with target.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError as error:
        raise PolicyMissing(f"{target}: policy file is missing") from error
    except tomllib.TOMLDecodeError as error:
        raise PolicyInvalid(f"{target}: invalid TOML: {error}") from error
    try:
        return validate_policy(document)
    except PolicyInvalid as error:
        raise PolicyInvalid(f"{target}: {error}") from error


def load(path: str | Path = DEFAULT_POLICY_PATH) -> HarnessPolicy:
    """Compatibility spelling for callers that treat policy loading as ``load``."""
    return load_policy(path)


def validate_policy(document: object) -> HarnessPolicy:
    """Validate a decoded TOML document and return its typed policy."""
    root = _mapping(document, "policy")
    _reject_unknown(root, _POLICY_KEYS, "policy")
    roles = _mapping(_required(root, "roles", "policy"), "policy.roles")
    _reject_unknown(roles, frozenset(ROLE_NAMES), "policy.roles")
    parsed = {role: _parse_role(role, roles) for role in ROLE_NAMES}
    return HarnessPolicy(MappingProxyType(parsed))


def parse_policy(document: object) -> HarnessPolicy:
    """Alias for validating an already-decoded TOML document."""
    return validate_policy(document)


def _parse_role(role: str, roles: Mapping[str, object]) -> RolePolicy:
    path = f"policy.roles.{role}"
    table = _mapping(_required(roles, role, "policy.roles"), path)
    _reject_unknown(table, _ROLE_KEYS, path)
    allow = _strings(_required(table, "allow", path), f"{path}.allow")
    cost_rank = _positive_int_map(_required(table, "cost_rank", path), f"{path}.cost_rank")
    _require_exact_keys(cost_rank, allow, f"{path}.cost_rank")
    token_budget = _positive_int(_required(table, "token_budget", path), f"{path}.token_budget")
    raw_per_harness = table.get("per_harness_budget", {})
    per_harness = _positive_int_map(raw_per_harness, f"{path}.per_harness_budget")
    _require_subset(per_harness, allow, f"{path}.per_harness_budget")
    attempts = _positive_int(
        _required(table, "escalate_after_attempts", path), f"{path}.escalate_after_attempts"
    )
    return RolePolicy(
        allow=allow,
        cost_rank=MappingProxyType(cost_rank),
        token_budget=token_budget,
        per_harness_budget=MappingProxyType(per_harness),
        escalate_after_attempts=attempts,
    )


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
        names = ", ".join(unknown)
        raise PolicyInvalid(f"{path}: unknown key(s): {names}")


def _strings(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PolicyInvalid(f"{path}: must be a non-empty array of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise PolicyInvalid(f"{path}: must contain only non-empty strings")
    result = tuple(cast(str, item) for item in value)
    if len(result) != len(set(result)):
        raise PolicyInvalid(f"{path}: entries must be unique")
    return result


def _positive_int(value: object, path: str) -> int:
    if type(value) is not int or value <= 0:
        raise PolicyInvalid(f"{path}: must be a positive integer")
    return value


def _positive_int_map(value: object, path: str) -> dict[str, int]:
    table = _mapping(value, path)
    result: dict[str, int] = {}
    for key, item in table.items():
        if not isinstance(key, str) or not key:
            raise PolicyInvalid(f"{path}: keys must be non-empty strings")
        result[key] = _positive_int(item, f"{path}.{key}")
    return result


def _require_exact_keys(values: Mapping[str, int], allow: tuple[str, ...], path: str) -> None:
    missing = sorted(set(allow) - set(values))
    if missing:
        raise PolicyInvalid(f"{path}: missing cost rank for {', '.join(missing)}")
    extra = sorted(set(values) - set(allow))
    if extra:
        raise PolicyInvalid(f"{path}: entry is not present in allow: {', '.join(extra)}")


def _require_subset(values: Mapping[str, int], allow: tuple[str, ...], path: str) -> None:
    extra = sorted(set(values) - set(allow))
    if extra:
        raise PolicyInvalid(f"{path}: entry is not present in allow: {', '.join(extra)}")
