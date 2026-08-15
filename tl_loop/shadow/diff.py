"""Argument-normalized shadow-versus-actual action divergence reports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from tl_loop.loop.shadow import IntendedAction

from .actual import ActualAction, ActualActionReader
from .recorder import IntendedActionRecorder

Action: TypeAlias = IntendedAction | ActualAction
DEFAULT_ARGUMENTS: Mapping[str, object] = {
    "force": False,
    "include_dead": False,
    "sweep": False,
}


class ActionBucket(str, Enum):
    """The complete set of pairwise comparison outcomes."""

    MATCH = "MATCH"
    DIVERGENT = "DIVERGENT"
    EXTRA = "EXTRA"
    MISSING = "MISSING"


@dataclass(frozen=True)
class DiffEntry:
    """One report row; exactly one or both sides are present by bucket."""

    bucket: ActionBucket
    shadow: IntendedAction | None
    actual: ActualAction | None

    @property
    def event_seq(self) -> int:
        """Use the shadow sequence when available, otherwise the actual sequence."""
        action = self.shadow or self.actual
        if action is None:
            raise RuntimeError("diff entry has no action")
        return action.event_seq


@dataclass(frozen=True)
class DiffReport:
    """Complete bucketed comparison output."""

    run_id: str
    entries: tuple[DiffEntry, ...]

    @property
    def counts(self) -> Mapping[ActionBucket, int]:
        return {
            bucket: sum(entry.bucket is bucket for entry in self.entries) for bucket in ActionBucket
        }


def normalize_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    """Canonicalize arguments without comparing rationale.

    Rules are intentionally narrow: mapping keys are sorted recursively; null
    values are omitted; known optional booleans are filled with their explicit
    defaults; decimal identifiers in ``*_id``/``*_number`` fields become
    integers; and lists preserve order because action order is semantic. No
    free text is trimmed or rewritten, and no unlisted default is invented.
    """
    normalized = _normalize_mapping(arguments)
    for key, default in DEFAULT_ARGUMENTS.items():
        normalized.setdefault(key, default)
    return normalized


def diff_actions(
    run_id: str,
    shadow: Sequence[IntendedAction],
    actual: Sequence[ActualAction],
) -> DiffReport:
    """Pair exact actions first, then same-target divergences, never dropping rows."""
    unmatched_shadow = list(shadow)
    unmatched_actual = list(actual)
    entries: list[DiffEntry] = []

    index = 0
    while index < len(unmatched_shadow):
        candidate = unmatched_shadow[index]
        match_index = _find_exact(candidate, unmatched_actual)
        if match_index is None:
            index += 1
            continue
        entries.append(DiffEntry(ActionBucket.MATCH, candidate, unmatched_actual.pop(match_index)))
        unmatched_shadow.pop(index)

    index = 0
    while index < len(unmatched_shadow):
        candidate = unmatched_shadow[index]
        match_index = _find_target(candidate, unmatched_actual)
        if match_index is None:
            index += 1
            continue
        entries.append(
            DiffEntry(ActionBucket.DIVERGENT, candidate, unmatched_actual.pop(match_index))
        )
        unmatched_shadow.pop(index)

    entries.extend(DiffEntry(ActionBucket.EXTRA, action, None) for action in unmatched_shadow)
    entries.extend(DiffEntry(ActionBucket.MISSING, None, action) for action in unmatched_actual)
    entries.sort(key=lambda entry: (entry.event_seq, entry.bucket.value))
    return DiffReport(run_id, tuple(entries))


def render_report(report: DiffReport, *, docs_dir: str | Path = Path("docs/observability")) -> Path:
    """Write the complete Markdown report and return its path."""
    path = Path(docs_dir) / f"tl-loop-shadow-report-{report.run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_markdown(report), encoding="utf-8")
    return path


def generate_report(
    run_id: str,
    *,
    shadow_root: str | Path = Path(".exo/tl-loop/shadow"),
    segments_dir: str | Path = Path(".exo/ledger/segments"),
    docs_dir: str | Path = Path("docs/observability"),
) -> Path:
    """Read one run's two action streams, diff them, and render one report."""
    shadow = IntendedActionRecorder(run_id, root_dir=shadow_root).read_actions()
    actual = ActualActionReader(segments_dir).read(run_id)
    return render_report(diff_actions(run_id, shadow, actual), docs_dir=docs_dir)


def _find_exact(shadow: IntendedAction, actual: Sequence[ActualAction]) -> int | None:
    key = _comparison_key(shadow)
    for index, candidate in enumerate(actual):
        if _comparison_key(candidate) == key:
            return index
    return None


def _find_target(shadow: IntendedAction, actual: Sequence[ActualAction]) -> int | None:
    for index, candidate in enumerate(actual):
        if candidate.target == shadow.target:
            return index
    return None


def _comparison_key(action: Action) -> tuple[str, str, str]:
    return (
        action.kind,
        action.target,
        json.dumps(normalize_arguments(action.arguments), sort_keys=True, separators=(",", ":")),
    )


def _normalize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _normalize_value(key, value[key]) for key in sorted(value) if value[key] is not None
    }


def _normalize_value(key: str, value: object) -> object:
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, list):
        return [_normalize_value(key, item) for item in value]
    if key in {"intent_id", "dispatch_intent_id"} and isinstance(value, str):
        return "<intent-id>"
    if key == "started_at" and isinstance(value, (int, float)):
        return "<timestamp>"
    if key.endswith(("_id", "_number")) and isinstance(value, str) and value.isdecimal():
        return int(value)
    return value


def _markdown(report: DiffReport) -> str:
    counts = report.counts
    lines = [
        f"# TL shadow divergence report: `{report.run_id}`",
        "",
        "Arguments are compared after recursive key ordering, null omission, and decimal ID normalization. Rationale is reported but never compared; list ordering and free text are preserved.",
        "",
        "## Counts",
        "",
        "| bucket | count |",
        "|---|---:|",
    ]
    lines.extend(f"| {bucket.value} | {counts[bucket]} |" for bucket in ActionBucket)
    lines.extend(
        [
            "",
            "## Actions",
            "",
            "| bucket | event_seq | shadow | actual | rationale |",
            "|---|---:|---|---|---|",
        ]
    )
    for entry in report.entries:
        shadow = _action_cell(entry.shadow)
        actual = _action_cell(entry.actual)
        rationale = _rationale_cell(entry)
        lines.append(
            f"| {entry.bucket.value} | {_event_sequences(entry)} | {shadow} | {actual} | {rationale} |"
        )
    lines.append("")
    return "\n".join(lines)


def _action_cell(action: Action | None) -> str:
    if action is None:
        return "—"
    return f"`{action.kind}` → `{action.target}` `{json.dumps(normalize_arguments(action.arguments), sort_keys=True)}`"


def _rationale_cell(entry: DiffEntry) -> str:
    values = [action.rationale for action in (entry.shadow, entry.actual) if action is not None]
    return "<br>".join(value.replace("|", "\\|") for value in values)


def _event_sequences(entry: DiffEntry) -> str:
    if entry.shadow is None:
        return str(entry.actual.event_seq) if entry.actual else "—"
    if entry.actual is None or entry.shadow.event_seq == entry.actual.event_seq:
        return str(entry.shadow.event_seq)
    return f"{entry.shadow.event_seq} / {entry.actual.event_seq}"


__all__ = [
    "ActionBucket",
    "DiffEntry",
    "DiffReport",
    "diff_actions",
    "generate_report",
    "normalize_arguments",
    "render_report",
]
