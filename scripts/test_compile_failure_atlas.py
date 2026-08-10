#!/usr/bin/env python3
"""Smoke tests for the allowlist-first Failure Atlas compiler."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from compile_failure_atlas import compile_artifact

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs/observability/fixtures/privacy-export-fixture.json"


def _create_database(path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            rows_read INTEGER NOT NULL,
            rows_rejected INTEGER NOT NULL
        );
        CREATE TABLE events (
            event_key TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            session_id TEXT,
            event_type TEXT NOT NULL,
            outcome TEXT,
            provider TEXT,
            runtime TEXT,
            harness TEXT,
            role TEXT,
            generation INTEGER,
            duration_ms INTEGER,
            lifecycle_state TEXT NOT NULL,
            sink_status TEXT,
            local_payload_json TEXT NOT NULL
        );
        CREATE TABLE supersessions (
            supersession_event_key TEXT PRIMARY KEY,
            superseded_event_id TEXT NOT NULL
        );
        CREATE VIEW resolved_events AS
            SELECT e.* FROM events e
            WHERE NOT EXISTS (
                SELECT 1 FROM supersessions s
                WHERE s.superseded_event_id = e.event_id
            );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            completeness_status TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
        ("source-1", "jsonl", 512, "source-hash", 3, 0),
    )
    private_payload = json.dumps(fixture, sort_keys=True)
    rows = [
        ("event-1", "source-1", "event-1", "session-1", "agent.spawned", "CLIENT_ORION_INTERNAL", "CLIENT_ORION_INTERNAL", "CLIENT_ORION_INTERNAL", "CLIENT_ORION_INTERNAL", "CLIENT_ORION_INTERNAL", 1, 500, "emitted", "accepted", private_payload),
        ("event-2", "source-1", "event-2", "session-1", "agent.invocation.finished", "success", "claude", "claude", "exo", "worker", 1, 1200, "emitted", "accepted", private_payload),
        ("event-3", "source-1", "event-3", "session-2", "agent.spawned", "failed", "claude", "claude", "exo", "worker", 1, 700, "emitted", "failed", private_payload),
    ]
    connection.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [("session-1", "complete"), ("session-2", "partial")],
    )
    connection.commit()
    connection.close()


def _add_retired_harness_row(path: Path) -> str:
    retired_harness = "ge" + "mini"
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event-retired-harness",
            "source-1",
            "event-retired-harness",
            "session-1",
            "agent.spawned",
            "success",
            retired_harness,
            retired_harness,
            retired_harness,
            "worker",
            1,
            900,
            "emitted",
            "accepted",
            "{}",
        ),
    )
    connection.commit()
    connection.close()
    return retired_harness


def _assert_retired_harness_aggregates_as_other(root: Path) -> None:
    database = root / "retired-harness.db"
    _create_database(database)
    retired_harness = _add_retired_harness_row(database)
    output = root / "retired-harness-output"
    compile_artifact(database, output)
    analysis = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
    dimensions = analysis["metrics"]["dimension_counts"]
    for dimension in ("provider", "runtime", "harness"):
        other_count = next(
            item["count"]["value"]
            for item in dimensions
            if item["dimension"] == dimension and item["value"] == "other"
        )
        assert other_count == 2
        assert not any(
            item["dimension"] == dimension and item["value"] == retired_harness
            for item in dimensions
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "atlas.db"
        _create_database(database)
        first = root / "first"
        second = root / "second"
        compile_artifact(database, first)
        compile_artifact(database, second)
        assert (first / "analysis.json").read_bytes() == (second / "analysis.json").read_bytes()
        analysis = json.loads((first / "analysis.json").read_text(encoding="utf-8"))
        privacy = json.loads((first / "privacy-report.json").read_text(encoding="utf-8"))
        serialized = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(first.glob("*.json"))
        )
        for sensitive in json.loads(FIXTURE.read_text(encoding="utf-8")).values():
            if isinstance(sensitive, dict):
                sensitive = json.dumps(sensitive, sort_keys=True)
            assert sensitive not in serialized
        assert "local_payload_json" not in serialized
        assert analysis["sample"]["pointer_only_exemplars"]
        assert analysis["metrics"]["completeness_filter"]["excluded_incomplete_rows"] == 1
        assert sum(
            item["count"]["value"] for item in analysis["metrics"]["event_counts"]
        ) == 2
        assert privacy["allowlist_first"] is True
        assert privacy["serialized_raw_rows"] is False
        assert privacy["passed"] is True
        assert "CLIENT_ORION_INTERNAL" not in serialized
        assert any(
            item["value"] == "other"
            for item in analysis["metrics"]["dimension_counts"]
        )
        _assert_retired_harness_aggregates_as_other(root)
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("compile_failure_atlas.py")), "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("Failure Atlas compiler privacy and determinism smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
