#!/usr/bin/env python3
"""Smoke tests for the local signal-to-contrast measurement pipeline."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from failure_atlas_measure import measure


def _event(
    key: str,
    session: str,
    seq: int,
    event_type: str,
    payload: dict,
    generation: int = 1,
) -> tuple:
    return (
        key,
        key,
        1,
        f"2026-01-01T00:00:{seq:02d}Z",
        f"2026-01-01T00:00:{seq:02d}Z",
        seq,
        session,
        None,
        "worker",
        "invocation-" + session,
        generation,
        event_type,
        "success" if event_type.endswith("finished") else "accepted",
        "emitted",
        "accepted",
        json.dumps({"data": payload}),
    )


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events (
            event_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            source_offset INTEGER NOT NULL,
            event_time TEXT,
            observed_at TEXT,
            run_seq INTEGER,
            session_id TEXT,
            run_id TEXT,
            agent_id TEXT,
            invocation_id TEXT,
            generation INTEGER,
            event_type TEXT NOT NULL,
            outcome TEXT,
            lifecycle_state TEXT NOT NULL,
            sink_status TEXT,
            local_payload_json TEXT NOT NULL
        );
        CREATE TABLE supersessions (
            supersession_event_key TEXT PRIMARY KEY,
            superseded_event_id TEXT NOT NULL
        );
        CREATE VIEW resolved_events AS SELECT * FROM events;
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            completeness_status TEXT NOT NULL
        );
        """
    )
    rows = [
        _event("b-start", "baseline-1", 1, "agent.invocation.started", {"experiment_arm": "baseline"}),
        _event("b-finish", "baseline-1", 2, "agent.invocation.finished", {"experiment_arm": "baseline"}),
        _event("t-start", "treatment-1", 1, "agent.invocation.started", {"experiment_arm": "treatment"}, 2),
        _event("t-finish", "treatment-1", 2, "agent.invocation.finished", {"experiment_arm": "treatment"}, 2),
        _event("gap-start", "unselected-partial", 1, "agent.invocation.started", {"experiment_arm": "treatment"}),
        _event("gap-delivery", "unselected-partial", 3, "message.delivery", {"message_id": "m-1", "experiment_arm": "treatment"}),
        _event("gap-sink", "unselected-partial", 4, "sink.health", {"write_failure_count": 1, "experiment_arm": "treatment"}),
    ]
    connection.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [
            ("baseline-1", "complete"),
            ("treatment-1", "complete"),
            ("unselected-partial", "partial"),
        ],
    )
    connection.commit()
    connection.close()


def main() -> int:
    preregistration = {
        "experiment_id": "mvp-e-test",
        "architecture_revision": "harness-v2",
        "primary_unit": "session",
        "estimand": "treatment_minus_baseline_completed_session_rate",
        "assignment": "blocked_matched",
        "primary_outcome": {
            "name": "completed_session_rate",
            "direction": "increase",
            "denominator": "complete_sessions",
        },
        "baseline_session_ids": ["baseline-1"],
        "treatment_session_ids": ["treatment-1"],
        "confound_controls": [
            "task_type",
            "initial_complexity",
            "human_presence",
            "model_tier",
            "repository_class",
            "topology_shape",
        ],
        "missingness_rule": "exclude partial or unknown sessions and report their count",
        "stopping_rule": "fixed session list",
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "atlas.db"
        prereg = root / "preregistration.json"
        prereg.write_text(json.dumps(preregistration), encoding="utf-8")
        _create_database(database)
        artifact = measure(
            database,
            root / "measurement",
            prereg,
            judge_models=["judge-a", "judge-b"],
        )
        assert artifact["claim_gate"]["architecture_effect_claim_allowed"] is True
        assert artifact["effect"]["denominator"] == "complete_sessions"
        adjudication = json.loads(
            (root / "measurement/adjudication.json").read_text(encoding="utf-8")
        )
        assert adjudication["detectors"]
        assert adjudication["single_judge_precision_provisional"] is False
        assert all("wilson_95" in row for row in adjudication["detectors"])
        invalid = measure(database, root / "invalid", None)
        assert invalid["claim_gate"]["architecture_effect_claim_allowed"] is False
    print("Failure Atlas measurement pipeline smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
