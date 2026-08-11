"""Review-policy loading and deterministic diff metadata extraction."""

from __future__ import annotations

import copy
import fnmatch
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonValue

from .review_contract import AdjudicationInputError, ReviewPolicy

DEFAULT_REVIEW_POLICY = Path(".exo/review-policy.toml")


@dataclass(frozen=True)
class DiffContext:
    """The diff payload and policy metadata used for one judgment."""

    payload: JsonValue
    lines_changed: int
    paths: tuple[str, ...]
    review_rounds: int


def load_review_policy(path: str | Path = DEFAULT_REVIEW_POLICY) -> ReviewPolicy:
    """Load the canonical TOML policy used by the review controller."""
    try:
        with Path(path).open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AdjudicationInputError(
            f"could not load review policy {path}: {error}"
        ) from error
    return ReviewPolicy.from_mapping(document)


def resolve_policy(
    policy: ReviewPolicy | Mapping[str, object] | None,
    path: str | Path,
) -> tuple[ReviewPolicy, str]:
    """Resolve injected policy data or the canonical policy file."""
    if isinstance(policy, ReviewPolicy):
        return policy, "injected policy"
    if isinstance(policy, Mapping):
        return ReviewPolicy.from_mapping(policy), "injected policy"
    return load_review_policy(path), str(path)


def diff_context(
    pr_diff: Mapping[str, object] | str,
    criteria: JsonValue,
) -> DiffContext:
    """Normalize a diff and derive only explicit policy metadata."""
    if isinstance(pr_diff, str):
        payload: JsonValue = pr_diff
        lines_changed, paths = _patch_metadata(pr_diff)
    elif isinstance(pr_diff, Mapping):
        payload = cast(JsonValue, copy.deepcopy(dict(pr_diff)))
        patch = pr_diff.get("diff", pr_diff.get("patch"))
        if isinstance(patch, str):
            parsed_lines, parsed_paths = _patch_metadata(patch)
        else:
            parsed_lines, parsed_paths = 0, ()
        lines_changed = _metadata_int(
            pr_diff.get("lines_changed"), parsed_lines, "lines_changed"
        )
        paths = _metadata_paths(pr_diff.get("paths"), parsed_paths)
    else:
        raise AdjudicationInputError("pr_diff must be a string or object")
    rounds = _review_rounds(pr_diff, criteria)
    return DiffContext(payload, lines_changed, paths, rounds)


def policy_gates(
    diff: DiffContext,
    policy: ReviewPolicy,
) -> tuple[str, ...]:
    """Return every Python-owned gate that requires a second review."""
    gates: list[str] = []
    if diff.review_rounds < policy.min_review_rounds:
        gates.append(
            f"minimum review rounds not met ({diff.review_rounds} < "
            f"{policy.min_review_rounds})"
        )
    if diff.lines_changed > policy.external_review_threshold:
        gates.append(
            f"diff exceeds external review threshold "
            f"({diff.lines_changed} > {policy.external_review_threshold})"
        )
    matched = tuple(
        path
        for path in diff.paths
        if any(
            fnmatch.fnmatchcase(path, pattern)
            for pattern in policy.external_review_paths
        )
    )
    if matched:
        gates.append(f"external review path match: {', '.join(matched)}")
    if (
        policy.require_second_reviewer_complexity
        and diff.lines_changed > policy.complexity_line_threshold
    ):
        gates.append(
            f"complexity threshold exceeded "
            f"({diff.lines_changed} > {policy.complexity_line_threshold})"
        )
    return tuple(gates)


def require_json(value: object, name: str) -> None:
    """Reject non-JSON review context instead of silently stringifying it."""
    if not _is_json(value):
        raise AdjudicationInputError(f"{name} must be canonical JSON")


def _patch_metadata(patch: str) -> tuple[int, tuple[str, ...]]:
    lines = 0
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            lines += 1
        if line.startswith(("+++ b/", "--- a/")):
            paths.append(line[6:])
    return lines, tuple(dict.fromkeys(path for path in paths if path != "/dev/null"))


def _metadata_int(value: object, fallback: int, key: str) -> int:
    if value is None:
        return fallback
    if type(value) is not int or value < 0:
        raise AdjudicationInputError(f"pr_diff.{key} must be a non-negative integer")
    return value


def _metadata_paths(
    value: object,
    fallback: Sequence[str],
) -> tuple[str, ...]:
    if value is None:
        return tuple(fallback)
    if not isinstance(value, list) or any(
        not isinstance(path, str) or not path for path in value
    ):
        raise AdjudicationInputError("pr_diff.paths must be an array of strings")
    return tuple(dict.fromkeys(value))


def _review_rounds(pr_diff: object, criteria: JsonValue) -> int:
    for value in (pr_diff, criteria):
        if not isinstance(value, Mapping):
            continue
        candidate = value.get("review_rounds", value.get("rounds"))
        if candidate is not None:
            if type(candidate) is not int or candidate < 0:
                raise AdjudicationInputError(
                    "review rounds must be a non-negative integer"
                )
            return candidate
    return 0


def _is_json(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json(item)
            for key, item in value.items()
        )
    return False


__all__ = [
    "DEFAULT_REVIEW_POLICY",
    "DiffContext",
    "diff_context",
    "load_review_policy",
    "policy_gates",
    "require_json",
    "resolve_policy",
]
