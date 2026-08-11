"""Input validation and state accounting for PR repair handoffs."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, MutableMapping, Sequence
from fnmatch import fnmatchcase
from typing import Protocol, cast

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject
from tl_loop.state.schema import SliceState, SliceStatus, Verdict
from tl_loop.state.store import RunStore

from .call import MAX_ATTEMPTS
from .repair_contract import (
    REPAIR_HANDOFF_FIELDS,
    RepairBoundaryError,
    RepairHandoff,
    RepairInputError,
    RepairPRStateError,
)
from .review_contract import AdjudicationResult


class _MutableAttempts(Protocol):
    attempts: int


def pr_identity(pr: Mapping[str, object] | object) -> tuple[int, tuple[str, ...]]:
    """Extract the PR number and the owning slice paths."""
    number = member(pr, "pr_number")
    if number is None:
        number = member(pr, "number")
    if type(number) is not int or number <= 0:
        raise RepairInputError("pr must provide a positive pr_number")
    paths_value = member(pr, "paths")
    if paths_value is None:
        nested = member(pr, "slice")
        paths_value = member(nested, "paths") if nested is not None else None
    if not isinstance(paths_value, (list, tuple, set, frozenset)):
        raise RepairInputError("pr must provide a non-empty slice paths set")
    paths = tuple(
        path.strip()
        for path in paths_value
        if isinstance(path, str) and path.strip()
    )
    if not paths or len(paths) != len(paths_value):
        raise RepairInputError("slice paths must contain non-empty strings")
    return number, paths


def watch_existing_pr(client: object, number: int) -> JsonObject:
    """Require an open, unmerged PR with a branch and exact head SHA."""
    watcher = getattr(client, "watcher_pr_state", None)
    if not callable(watcher):
        raise RepairInputError("client has no watcher_pr_state capability")
    result = watcher(pr_number=number)
    payload = _watcher_payload(result)
    open_value = payload.get("open")
    is_open = open_value if isinstance(open_value, bool) else payload.get("state") == "open"
    if not is_open:
        raise RepairPRStateError(f"PR #{number} is not open")
    if payload.get("merged") is not False:
        raise RepairPRStateError(f"PR #{number} is already merged or has no merge state")
    head_branch = payload.get("head_branch")
    head_sha = payload.get("head_sha")
    if not isinstance(head_branch, str) or not head_branch:
        raise RepairPRStateError(f"PR #{number} has no head branch")
    if not isinstance(head_sha, str) or not head_sha:
        raise RepairPRStateError(f"PR #{number} has no head SHA")
    return {
        "open": True,
        "merged": False,
        "head_branch": head_branch,
        "head_sha": head_sha,
    }


def review_inputs(
    verdict: Verdict | str,
    review: Mapping[str, object] | AdjudicationResult | object,
) -> list[JsonObject]:
    """Require a matching NO-GO review with at least one blocking reason."""
    selected = as_verdict(verdict)
    review_verdict = member(review, "verdict")
    if review_verdict is not None and as_verdict(review_verdict) is not selected:
        raise RepairInputError("review verdict does not match the supplied verdict")
    if selected is not Verdict.NO_GO:
        raise RepairInputError("compose_repair requires a NO-GO verdict")
    raw_reasons = member(review, "reasons")
    if not isinstance(raw_reasons, (list, tuple)) or not raw_reasons:
        raise RepairInputError("NO-GO review must contain reasons")
    reasons: list[JsonObject] = []
    for index, reason in enumerate(raw_reasons):
        if not isinstance(reason, Mapping):
            raise RepairInputError(f"review reasons[{index}] must be objects")
        reasons.append(cast(JsonObject, copy.deepcopy(dict(reason))))
    if not any(reason.get("severity") == "blocking" for reason in reasons):
        raise RepairInputError("NO-GO review must contain a blocking reason")
    return reasons


def as_verdict(value: object) -> Verdict:
    """Normalize a string or closed Verdict enum."""
    candidate = value.value if isinstance(value, Verdict) else value
    try:
        return Verdict(candidate)
    except (TypeError, ValueError) as error:
        raise RepairInputError(f"unsupported review verdict: {value!r}") from error


def validate_owned_paths(handoff: RepairHandoff, owned: Sequence[str]) -> None:
    """Reject every path-like handoff reference outside the slice boundary."""
    violations: list[str] = []
    for field_name in REPAIR_HANDOFF_FIELDS:
        value = getattr(handoff, field_name)
        texts = (value,) if isinstance(value, str) else value
        for text in texts:
            for path in _path_references(text):
                if not any(_path_owned(path, owner) for owner in owned):
                    violations.append(f"{field_name} references unowned path {path!r}")
    if violations:
        raise RepairBoundaryError("; ".join(violations))


_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"|(?<![A-Za-z0-9_.-])"
    r"[A-Za-z0-9_.-]+[.]"
    r"(?:py|rs|hs|lhs|toml|md|json|yaml|yml|txt|sh|css|js|ts|tsx|jsx|go|c|h|cpp|hpp|sql|html|xml|proto|lock|nix|wasm)"
    r"(?![A-Za-z0-9_.-])"
)


def _path_references(text: str) -> tuple[str, ...]:
    return tuple(_normalize_path(match.group(0)) for match in _PATH_RE.finditer(text))


def _normalize_path(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _path_owned(path: str, owner: str) -> bool:
    normalized_owner = owner.rstrip("/")
    return (
        path == normalized_owner
        or path.startswith(f"{normalized_owner}/")
        or fnmatchcase(path, normalized_owner)
    )


def increment_attempts(
    pr: Mapping[str, object] | object,
    number: int,
    store: RunStore | None,
    slice_id: str | None,
) -> int | None:
    """Increment the owning slice exactly once after resume succeeds."""
    if store is not None:
        return _increment_store(store, pr, number, slice_id)
    target = member(pr, "slice")
    if target is None:
        target = pr
    if isinstance(target, MutableMapping):
        current = target.get("attempts", 0)
        if type(current) is not int or current < 0:
            raise RepairInputError("slice attempts must be a non-negative integer")
        target["attempts"] = current + 1
        return current + 1
    if hasattr(target, "attempts"):
        mutable = cast(_MutableAttempts, target)
        current = mutable.attempts
        if type(current) is not int or current < 0:
            raise RepairInputError("slice attempts must be a non-negative integer")
        try:
            mutable.attempts = current + 1
        except (AttributeError, TypeError):
            return None
        return current + 1
    return None


def _increment_store(
    store: RunStore,
    pr: Mapping[str, object] | object,
    number: int,
    slice_id: str | None,
) -> int:
    state = store.load()
    target_id = slice_id or member(pr, "slice_id")
    if target_id is None:
        nested = member(pr, "slice")
        target_id = member(nested, "id") if nested is not None else None
    if target_id is None:
        matches = [
            item.id
            for item in state.slices.values()
            if item.pr_number == number
        ]
        if len(matches) == 1:
            target_id = matches[0]
    if not isinstance(target_id, str) or not target_id:
        raise RepairInputError("repair attempts require a slice id")
    current = state.slices.get(target_id)
    if current is None:
        raise RepairInputError(f"repair attempts reference unknown slice {target_id!r}")
    updated = dict(state.slices)
    updated[target_id] = SliceState(
        id=current.id,
        status=SliceStatus.REPAIRING,
        paths=current.paths,
        depends_on=current.depends_on,
        base_ref=current.base_ref,
        test_plan=current.test_plan,
        agent_type=current.agent_type,
        model=current.model,
        branch=current.branch,
        worktree=current.worktree,
        pr_number=current.pr_number,
        reviewed_head=current.reviewed_head,
        attempts=current.attempts + 1,
        verdict=current.verdict,
        verdict_at=current.verdict_at,
        park_cause=current.park_cause,
        park_issue_id=current.park_issue_id,
        park_audit=current.park_audit,
        blocked_by=current.blocked_by,
    )
    store.checkpoint(
        state.fsm,
        updated,
        state.budgets,
        state.events.last_consumed_offset,
    )
    return current.attempts + 1


def repair_max_attempts(model_choice: object) -> int:
    """Return the model-bounded semantic retry count."""
    value = member(model_choice, "max_attempts")
    if value is None:
        return MAX_ATTEMPTS
    if type(value) is not int or not 1 <= value <= MAX_ATTEMPTS:
        raise RepairInputError("model choice max_attempts must be between one and three")
    return value


def member(value: object, key: str) -> object | None:
    """Read a field from either a mapping or a typed input object."""
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _watcher_payload(result: object) -> JsonObject:
    if isinstance(result, ToolResult):
        if result.success is not True:
            raise RepairPRStateError(result.error or "watcher_pr_state failed")
        payload = result.result
    elif isinstance(result, Mapping):
        if result.get("success") is False:
            raise RepairPRStateError(
                cast(str, result.get("error") or "watcher_pr_state failed")
            )
        payload = result.get("result", result)
    else:
        raise RepairPRStateError("watcher_pr_state returned no object")
    if not isinstance(payload, Mapping):
        raise RepairPRStateError("watcher_pr_state returned no PR state object")
    return cast(JsonObject, dict(payload))


__all__ = [
    "as_verdict",
    "increment_attempts",
    "member",
    "pr_identity",
    "repair_max_attempts",
    "review_inputs",
    "validate_owned_paths",
    "watch_existing_pr",
]
