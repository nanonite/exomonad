"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .plan_manifest import PlanManifest
from .plan_revision_types import PlanRevisionBlocker
from .schema import RunState, SliceStatus

_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:token|api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+"
    r"|\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{10,})\b"
    r"|\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s]+"
    r"|\beyj[a-z0-9_-]+\.eyj[a-z0-9_-]+\.[a-z0-9_-]+\b)"
)


def _stage_projection(manifest: PlanManifest | None) -> tuple[tuple[int, tuple[str, ...]], ...]:
    if manifest is None:
        return ()
    return tuple(
        (order, tuple(manifest.node(node_id).name for node_id in node_ids))
        for order, node_ids in manifest.ordered_stages
    )


def _retained_state_summary(state: RunState) -> dict[str, int]:
    records = tuple(state.slices.values())
    return {
        "slices": len(records),
        "attempted_slices": sum(record.attempts > 0 for record in records),
        "merged_slices": sum(record.status is SliceStatus.MERGED for record in records),
        "publication_bindings": sum(record.publication is not None for record in records),
        "dispatch_bindings": sum(record.dispatch_intent_id is not None for record in records),
        "post_merge_receipts": sum(record.post_merge is not None for record in records),
    }


def _persisted_stages(state: RunState) -> tuple[tuple[int, tuple[str, ...]], ...]:
    return tuple((stage.order, tuple(stage.sub_tls)) for stage in state.ordered_stages)


def _safe_changes(changes: tuple[object, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "scope_id": _safe_text(change.scope_id),
            "node_id": _safe_text(change.node_id),
            "action": change.action,
            "fields": list(change.fields),
        }
        for change in changes
    )


def _safe_blocker(blocker: PlanRevisionBlocker) -> dict[str, object]:
    return {
        "code": blocker.code,
        "reason": _safe_text(blocker.reason),
        "scope_id": _safe_text(blocker.scope_id) if blocker.scope_id else None,
        "node_id": _safe_text(blocker.node_id) if blocker.node_id else None,
    }


def _safe_text(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = _SECRET.sub("[REDACTED]", value)
    escaped = "".join(
        character
        if character.isprintable() and unicodedata.category(character) != "Cf"
        else f"\\u{ord(character):04x}"
        for character in cleaned
    )
    return escaped if len(escaped) <= 160 else escaped[:157] + "..."


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list | tuple):
        return [_safe_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {
            _safe_text(key): _safe_value(item)
            for key, item in sorted(value.items())[:50]
            if isinstance(key, str)
        }
    if value is None or isinstance(value, bool | int | float):
        return value
    return f"[{type(value).__name__}]"


def _safe_stages(stages: tuple[tuple[int, tuple[str, ...]], ...]) -> list[dict[str, object]]:
    return [
        {"order": order, "sub_tls": [_safe_text(name) for name in names]} for order, names in stages
    ]


def _input_hashes(inputs: dict[str, bytes | None]) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (path, hashlib.sha256(raw).hexdigest() if raw is not None else None)
        for path, raw in sorted(inputs.items())
    )


def _fingerprint(inputs: tuple[tuple[str, str | None], ...]) -> str:
    digest = hashlib.sha256(b"exo-plan-reconciliation-evidence-v1\0")
    for path, content_hash in inputs:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((content_hash or "missing").encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
