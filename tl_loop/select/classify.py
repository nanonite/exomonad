"""Deterministic, explainable task-difficulty classification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import NamedTuple

from tl_loop.state.schema import SliceState


class Difficulty(str, Enum):
    """Closed difficulty vocabulary consumed by harness selection."""

    TRIVIAL = "trivial"
    STANDARD = "standard"
    HARD = "hard"


class Classification(NamedTuple):
    """A difficulty result paired with the rule that produced it."""

    difficulty: Difficulty
    matched_rule_name: str


@dataclass(frozen=True)
class ClassificationRule:
    """One ordered, named classification rule."""

    name: str
    difficulty: Difficulty
    matches: Callable[[SliceState], bool]


# Keep this synchronized with .exo/review-policy.toml. Classification is pure and
# must not read configuration or the filesystem during a selector decision.
HIGH_RISK_PATH_GLOBS = (
    "proto/**",
    "rust/exomonad-core/src/handlers/**",
)
_SOURCE_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cpp": "cpp",
    ".ex": "elixir",
    ".go": "go",
    ".hs": "haskell",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def classify_task(slice: SliceState) -> Classification:
    """Classify one slice using the first matching deterministic rule."""
    for rule in CLASSIFICATION_RULES:
        if rule.matches(slice):
            return Classification(rule.difficulty, rule.name)
    raise RuntimeError("classification rules must include a total fallback")


def _has_high_risk_path(slice: SliceState) -> bool:
    return any(
        fnmatchcase(path, pattern)
        for path in slice.paths
        for pattern in HIGH_RISK_PATH_GLOBS
    )


def _spans_languages(slice: SliceState) -> bool:
    languages = {
        _SOURCE_LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())
        for path in slice.paths
    }
    languages.discard(None)
    return len(languages) > 1


def _has_broad_path_scope(slice: SliceState) -> bool:
    roots = {PurePosixPath(path).parts[0] for path in slice.paths if path}
    return len(slice.paths) >= 4 or len(roots) >= 3


def _has_dependency_fan_in(slice: SliceState) -> bool:
    return len(slice.depends_on) >= 3


def _has_long_test_plan(slice: SliceState) -> bool:
    return len(slice.test_plan) >= 4


def _has_no_test_plan(slice: SliceState) -> bool:
    return not slice.test_plan


def _is_focused_slice(slice: SliceState) -> bool:
    return len(slice.paths) == 1 and not slice.depends_on and bool(slice.test_plan)


def _always(_: SliceState) -> bool:
    return True


CLASSIFICATION_RULES = (
    ClassificationRule("high_risk_path", Difficulty.HARD, _has_high_risk_path),
    ClassificationRule("cross_language_span", Difficulty.HARD, _spans_languages),
    ClassificationRule("broad_path_scope", Difficulty.HARD, _has_broad_path_scope),
    ClassificationRule("dependency_fan_in", Difficulty.HARD, _has_dependency_fan_in),
    ClassificationRule("long_test_plan", Difficulty.HARD, _has_long_test_plan),
    ClassificationRule("missing_test_plan", Difficulty.STANDARD, _has_no_test_plan),
    ClassificationRule("focused_slice", Difficulty.TRIVIAL, _is_focused_slice),
    ClassificationRule("standard_slice", Difficulty.STANDARD, _always),
)


__all__ = [
    "CLASSIFICATION_RULES",
    "HIGH_RISK_PATH_GLOBS",
    "Classification",
    "ClassificationRule",
    "Difficulty",
    "classify_task",
]
