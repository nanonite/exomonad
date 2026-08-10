#!/usr/bin/env python3
"""Compile an allowlisted, shareable Failure Atlas artifact from local L2.

The compiler deliberately reads only coarse analysis columns. Sensitive
payloads remain local in atlas.db and are never serialized for redaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

QUERY_REVISION = "mvp-d-allowlist-v1"
METHOD_REVISION = "mvp-d-aggregate-sample-v1"
EXPERIMENT_REVISION = "unregistered-observation-v1"
PRIVACY_RULESET = "mvp-d-allowlist-denylist-v1"

ALLOWLIST_COLUMNS = (
    "event_type",
    "outcome",
    "provider",
    "runtime",
    "harness",
    "role",
    "generation",
    "duration_ms",
    "lifecycle_state",
    "sink_status",
)

SAFE_CATEGORICAL_VALUES = {
    "event_type": {
        "agent.spawned", "agent.resumed", "agent.harness_switch", "agent.stuck",
        "agent.notify_parent", "agent.sibling_merged", "agent.completed", "agent.stop_check",
        "agent.invocation.started", "agent.invocation.finished", "agent.guidance.delivery",
        "pr.filed", "pr.updated", "pr.published", "pr.replaced", "pr.merge_requested",
        "pr.merged", "pr.merge_failed", "copilot.review", "ci.status_changed",
        "event.dispatched", "event.dispatch_failed", "watcher.poll_cycle",
        "watcher.pr_observation", "hook.stop", "tool.called", "message.delivery",
        "message.consumed", "agent.message_sent", "agent_inbox.duplicates_dropped",
        "agent_inbox.messages_abandoned", "session.state_changed", "memory.state_changed",
        "inbox.state_changed", "event.superseded", "ledger.segment.dropped", "sink.health",
        "custom",
    },
    "outcome": {
        "accepted", "abandoned", "cancelled", "consumed", "emitted", "failed", "finished",
        "merged", "observed", "pending", "rejected", "started", "success", "timed_out",
        "unknown",
    },
    "provider": {"claude", "codex", "gemini", "opencode", "process", "shoal", "unknown"},
    "runtime": {"claude", "codex", "gemini", "opencode", "process", "shoal", "unknown"},
    "harness": {
        "claude", "codex", "exomonad", "exo", "gemini", "hook", "opencode", "process",
        "rust", "shoal", "sqlite", "teams_inbox", "tmux", "uds", "watcher", "unknown",
    },
    "role": {"dev", "event-handler", "reviewer", "root", "supervisor", "tl", "unknown", "worker"},
    "lifecycle_state": {"accepted", "emitted", "legacy", "observed", "unknown"},
    "sink_status": {"accepted", "complete", "failed", "partial", "unknown"},
}

DENYLIST_PATTERNS = {
    "url": re.compile(r"(?:https?://|git@|ssh://)", re.IGNORECASE),
    "path": re.compile(
        r"(?:/(?:home|Users|tmp|var|workspace)/|[A-Za-z]:\\)",
        re.IGNORECASE,
    ),
    "token": re.compile(
        r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})(?![A-Za-z0-9])"
    ),
    "sensitive_text": re.compile(
        r"(?<![A-Za-z0-9])(?:transcript|reasoning|conversation|prompt|secret|payload|branch)(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _database_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_allowlisted_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = ", ".join(ALLOWLIST_COLUMNS)
    query = f"SELECT session_id, {columns} FROM resolved_events"
    cursor = connection.execute(query)
    rows = []
    for values in cursor.fetchall():
        row = dict(zip(("session_id", *ALLOWLIST_COLUMNS), values))
        for column in SAFE_CATEGORICAL_VALUES:
            row[column] = _safe_category(column, row[column])
        rows.append(row)
    return rows


def _safe_category(column: str, value: Any) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).strip().lower()
    if column == "event_type" and normalized.startswith("custom."):
        return "custom"
    return normalized if normalized in SAFE_CATEGORICAL_VALUES[column] else "other"


def _source_manifest(connection: sqlite3.Connection) -> tuple[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT source_kind, file_size, content_hash, rows_read, rows_rejected "
        "FROM sources ORDER BY source_kind, content_hash"
    ).fetchall()
    normalized = [
        {
            "source_kind": row[0],
            "file_size": row[1],
            "content_hash": row[2],
            "rows_read": row[3],
            "rows_rejected": row[4],
        }
        for row in rows
    ]
    return _sha256_json(normalized), {
        "source_count": len(normalized),
        "source_kinds": sorted({row["source_kind"] for row in normalized}),
        "rows_read": sum(row["rows_read"] for row in normalized),
        "rows_rejected": sum(row["rows_rejected"] for row in normalized),
    }


def _provenance(source_manifest_hash: str, method_hash: str) -> dict[str, str]:
    return {
        "source_manifest_hash": source_manifest_hash,
        "query_revision": QUERY_REVISION,
        "code_revision": method_hash,
        "detector_revision": "not-run",
        "method_revision": METHOD_REVISION,
        "experiment_revision": EXPERIMENT_REVISION,
    }


def _number(value: int | float, provenance: dict[str, str]) -> dict[str, Any]:
    return {"value": value, "provenance": provenance}


def _event_counts(rows: list[dict[str, Any]], provenance: dict[str, str]) -> list[dict[str, Any]]:
    counts = Counter(row["event_type"] or "unknown" for row in rows)
    return [
        {"event_type": key, "count": _number(counts[key], provenance)}
        for key in sorted(counts)
    ]


def _outcome_rates(rows: list[dict[str, Any]], provenance: dict[str, str]) -> list[dict[str, Any]]:
    totals = Counter(row["event_type"] or "unknown" for row in rows)
    outcomes = Counter(
        (row["event_type"] or "unknown", row["outcome"])
        for row in rows
        if row["outcome"] is not None
    )
    rates = []
    for (event_type, outcome), count in sorted(outcomes.items()):
        denominator = totals[event_type]
        rates.append(
            {
                "event_type": event_type,
                "outcome": outcome,
                "count": _number(count, provenance),
                "denominator": _number(denominator, provenance),
                "rate": _number(round(count / denominator, 8), provenance),
                "unit": "events",
            }
        )
    return rates


def _dimension_counts(rows: list[dict[str, Any]], provenance: dict[str, str]) -> list[dict[str, Any]]:
    dimensions = ("provider", "runtime", "harness", "role")
    output = []
    for dimension in dimensions:
        counts = Counter(row[dimension] or "unknown" for row in rows)
        output.extend(
            {
                "dimension": dimension,
                "value": value,
                "count": _number(count, provenance),
                "interpretation": "descriptive_only",
            }
            for value, count in sorted(counts.items())
        )
    return output


def _latency_buckets(rows: list[dict[str, Any]], provenance: dict[str, str]) -> list[dict[str, Any]]:
    buckets = Counter()
    for row in rows:
        duration = row["duration_ms"]
        if duration is None:
            continue
        if duration < 1000:
            bucket = "0-999ms"
        elif duration < 10000:
            bucket = "1000-9999ms"
        elif duration < 60000:
            bucket = "10000-59999ms"
        else:
            bucket = "60000ms+"
        buckets[bucket] += 1
    order = ("0-999ms", "1000-9999ms", "10000-59999ms", "60000ms+")
    return [
        {"bucket": bucket, "count": _number(buckets[bucket], provenance)}
        for bucket in order
        if buckets[bucket]
    ]


def _cooccurrence(rows: list[dict[str, Any]], provenance: dict[str, str]) -> list[dict[str, Any]]:
    pairs = Counter(
        (row["event_type"] or "unknown", row["outcome"] or "unknown")
        for row in rows
    )
    return [
        {
            "event_type": event_type,
            "outcome": outcome,
            "count": _number(count, provenance),
        }
        for (event_type, outcome), count in sorted(pairs.items())
    ]


def _pointer_sample(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    """Return deterministic, non-resolving pointers rather than source IDs."""
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["event_type"] or "unknown"].append(row)
    sample = []
    for event_type in sorted(grouped):
        for ordinal, row in enumerate(grouped[event_type][:limit], start=1):
            sample.append(
                {
                    "event_type": event_type,
                    "ordinal": ordinal,
                    "outcome": row["outcome"],
                    "generation": row["generation"],
                }
            )
    return sample[:limit]


def _complete_rows(
    rows: list[dict[str, Any]], session_statuses: dict[str, str]
) -> tuple[list[dict[str, Any]], int]:
    complete_statuses = {"complete", "known"}
    included = [
        row for row in rows if session_statuses.get(row.get("session_id")) in complete_statuses
    ]
    return included, len(rows) - len(included)


def _scan_projected(value: Any) -> dict[str, list[str]]:
    text = json.dumps(value, sort_keys=True)
    hits: dict[str, list[str]] = {}
    for category, pattern in DENYLIST_PATTERNS.items():
        matches = sorted(set(pattern.findall(text)))
        if matches:
            hits[category] = matches[:10]
    return hits


def compile_artifact(database: Path, output_dir: Path, mode: str = "aggregate") -> dict[str, Any]:
    if mode != "aggregate":
        raise ValueError("only aggregate export is shareable; internal events remain local")
    database = database.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = _read_allowlisted_rows(connection)
        source_hash, source_summary = _source_manifest(connection)
        session_statuses = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT session_id, completeness_status FROM sessions"
            )
        }
        completeness = [
            {"status": row[0], "count": row[1]}
            for row in connection.execute(
                "SELECT completeness_status, COUNT(*) FROM sessions "
                "GROUP BY completeness_status ORDER BY completeness_status"
            )
        ]
    rows, excluded_incomplete_rows = _complete_rows(rows, session_statuses)

    method_hash = _sha256_bytes(Path(__file__).read_bytes())
    provenance = _provenance(source_hash, method_hash)
    analysis = {
        "schema_version": 1,
        "artifact_kind": "failure_atlas_sample_aggregate",
        "mode": "aggregate",
        "provenance": provenance,
        "metrics": {
            "event_counts": _event_counts(rows, provenance),
            "outcome_rates": _outcome_rates(rows, provenance),
            "dimension_counts": _dimension_counts(rows, provenance),
            "latency_buckets": _latency_buckets(rows, provenance),
            "cooccurrence": _cooccurrence(rows, provenance),
            "completeness": [
                {
                    "status": item["status"],
                    "count": _number(item["count"], provenance),
                }
                for item in completeness
            ],
            "completeness_filter": {
                "included_statuses": ["complete", "known"],
                "excluded_incomplete_rows": excluded_incomplete_rows,
            },
        },
        "sample": {"pointer_only_exemplars": _pointer_sample(rows)},
    }
    projected = {
        "allowlist_columns": list(ALLOWLIST_COLUMNS),
        "row_count": len(rows),
        "analysis": analysis,
    }
    hits = _scan_projected(projected)
    privacy_report = {
        "schema_version": 1,
        "ruleset_version": PRIVACY_RULESET,
        "allowlist_first": True,
        "serialized_raw_rows": False,
        "allowlist_columns": list(ALLOWLIST_COLUMNS),
        "rows_projected": len(rows),
        "fields_projected": len(ALLOWLIST_COLUMNS),
        "dropped_field_count": "all non-allowlisted L2 fields",
        "contains_transcripts": False,
        "contains_reasoning": False,
        "contains_paths": False,
        "contains_secrets": False,
        "contains_raw_payload": False,
        "bucketed_categorical_columns": sorted(SAFE_CATEGORICAL_VALUES),
        "denylist_hits": hits,
        "passed": not hits,
    }
    if hits:
        raise ValueError(f"privacy denylist matched projected output: {sorted(hits)}")

    manifest = {
        "schema_version": 1,
        "artifact_kind": "failure_atlas_sample_aggregate_manifest",
        "database_hash": _database_hash(database),
        "source_manifest_hash": source_hash,
        "source_summary": source_summary,
        "allowlist_revision": QUERY_REVISION,
        "code_revision": method_hash,
        "method_revision": METHOD_REVISION,
        "detector_revision": "not-run",
        "experiment_revision": EXPERIMENT_REVISION,
        "privacy_report": "privacy-report.json",
    }
    _write_json(output_dir / "analysis.json", analysis)
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "privacy-report.json", privacy_report)
    return {
        "output_dir": str(output_dir),
        "rows_projected": len(rows),
        "privacy_passed": True,
        "source_manifest_hash": source_hash,
    }


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", default="aggregate")
    args = parser.parse_args()
    try:
        print(json.dumps(compile_artifact(args.database, args.output, args.mode), sort_keys=True))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
