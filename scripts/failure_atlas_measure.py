#!/usr/bin/env python3
"""Run local Failure Atlas signals, incidents, adjudication, and effect gates.

This module is intentionally local-only. It may use L2 payloads to validate
signals, but its generated artifacts contain pointers and aggregate labels,
never payload text or transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DETECTOR_REVISION = "mvp-e-mechanical-v1"
INCIDENT_REVISION = "mvp-e-incident-cluster-v1"
ADJUDICATION_REVISION = "mvp-e-adjudication-v1"
MEASUREMENT_REVISION = "mvp-e-contrast-gate-v1"
CLUSTER_WINDOW_SECONDS = 60
REQUIRED_CONFOUND_CONTROLS = (
    "task_type",
    "initial_complexity",
    "human_presence",
    "model_tier",
    "repository_class",
    "topology_shape",
)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("local_payload_json") or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return {**value, **value["data"]}
    return value if isinstance(value, dict) else {}


def _load_rows(database: Path) -> list[dict[str, Any]]:
    columns = (
        "event_key, event_id, source_offset, event_time, observed_at, run_seq, "
        "session_id, run_id, agent_id, invocation_id, generation, event_type, "
        "outcome, lifecycle_state, sink_status, local_payload_json"
    )
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        try:
            cursor = connection.execute(f"SELECT {columns} FROM resolved_events")
        except sqlite3.OperationalError:
            cursor = connection.execute(
                f"SELECT {columns} FROM events e WHERE NOT EXISTS "
                "(SELECT 1 FROM supersessions s "
                "WHERE s.superseded_event_id = e.event_id)"
            )
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _load_session_statuses(database: Path) -> dict[str, str]:
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        try:
            rows = connection.execute(
                "SELECT session_id, completeness_status FROM sessions"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {session_id: status for session_id, status in rows}


def _signal(
    detector: str,
    row: dict[str, Any],
    score: float,
    evidence: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    signal_id = _hash_json([DETECTOR_REVISION, detector, row["event_key"], detail])
    return {
        "signal_id": signal_id,
        "event_key": row["event_key"],
        "detector": detector,
        "score": score,
        "evidence_class": evidence,
        "detector_version": DETECTOR_REVISION,
        "session_id": row.get("session_id"),
        "event_time": row.get("event_time") or row.get("observed_at"),
        "source_pointer": {
            "event_key": row["event_key"],
            "source_offset": row.get("source_offset"),
        },
        "detail": detail,
    }


def _completion_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    started = [
        row for row in rows if row["event_type"] == "agent.invocation.started"
    ]
    finished = [row for row in rows if row["event_type"] == "agent.invocation.finished"]
    finished_keys = {
        (
            row.get("session_id"),
            row.get("invocation_id"),
            row.get("generation"),
        )
        for row in finished
    }
    return [
        _signal(
            "completion_gap",
            row,
            1.0,
            "mechanical",
            {"missing_terminal_event": "agent.invocation.finished"},
        )
        for row in started
        if (
            row.get("session_id"),
            row.get("invocation_id"),
            row.get("generation"),
        )
        not in finished_keys
    ]


def _delivery_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deliveries: dict[str, dict[str, Any]] = {}
    consumed: set[str] = set()
    for row in rows:
        payload = _payload(row)
        message_id = payload.get("message_id")
        if message_id is None:
            continue
        key = str(message_id)
        if row["event_type"] == "message.consumed":
            consumed.add(key)
        elif row["event_type"] == "message.delivery" and row.get("outcome") not in (
            "failed",
            "abandoned",
        ):
            deliveries[key] = row
    return [
        _signal(
            "delivery_loss",
            row,
            1.0,
            "mechanical",
            {"missing_terminal_event": "message.consumed", "message_id": key},
        )
        for key, row in sorted(deliveries.items())
        if key not in consumed
    ]


def _generation_retries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _signal(
            "generation_retry",
            row,
            min(float(row["generation"]), 10.0),
            "mechanical",
            {"generation": row["generation"]},
        )
        for row in rows
        if row["event_type"] == "agent.invocation.started"
        and row.get("generation") is not None
        and row["generation"] > 1
    ]


def _sink_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _signal(
            "sink_failure",
            row,
            1.0,
            "mechanical",
            {"sink_status": row.get("sink_status"), "outcome": row.get("outcome")},
        )
        for row in rows
        if row["event_type"] == "sink.health"
        and (
            row.get("sink_status") in ("failed", "partial", "unknown")
            or _payload(row).get("write_failure_count", 0) > 0
        )
    ]


def _sequence_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("run_seq") is not None:
            grouped[row.get("session_id") or "<unknown>"].append(row)
    signals = []
    for session_id, session_rows in grouped.items():
        ordered = sorted(session_rows, key=lambda row: (row["run_seq"], row["event_key"]))
        missing = []
        for left, right in zip(ordered, ordered[1:]):
            if right["run_seq"] > left["run_seq"] + 1:
                missing.extend(range(left["run_seq"] + 1, right["run_seq"]))
        if missing:
            signals.append(
                _signal(
                    "sequence_gap",
                    ordered[0],
                    1.0,
                    "mechanical",
                    {"session_id": session_id, "missing_run_seq": missing[:100]},
                )
            )
    return signals


DETECTORS: tuple[Callable[[list[dict[str, Any]]], list[dict[str, Any]]], ...] = (
    _completion_gaps,
    _delivery_gaps,
    _generation_retries,
    _sink_failures,
    _sequence_gaps,
)


def detect(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals = [signal for detector in DETECTORS for signal in detector(rows)]
    return sorted(signals, key=lambda signal: signal["signal_id"])


def _cluster_time(signal: dict[str, Any]) -> float | None:
    return _timestamp(signal.get("event_time"))


def cluster_incidents(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        grouped[signal.get("session_id") or "<unknown>"].append(signal)
    incidents = []
    for session_id, session_signals in sorted(grouped.items()):
        ordered = sorted(
            session_signals,
            key=lambda signal: (
                _cluster_time(signal) is None,
                _cluster_time(signal) or 0,
                signal["signal_id"],
            ),
        )
        clusters: list[list[dict[str, Any]]] = []
        for signal in ordered:
            if not clusters:
                clusters.append([signal])
                continue
            previous = clusters[-1][-1]
            left = _cluster_time(previous)
            right = _cluster_time(signal)
            if left is None or right is None or right - left <= CLUSTER_WINDOW_SECONDS:
                clusters[-1].append(signal)
            else:
                clusters.append([signal])
        for cluster in clusters:
            detectors = sorted({signal["detector"] for signal in cluster})
            primary = sorted(
                cluster,
                key=lambda signal: (-signal["score"], signal["detector"], signal["signal_id"]),
            )[0]
            incident_id = _hash_json(
                [INCIDENT_REVISION, session_id, [signal["signal_id"] for signal in cluster]]
            )
            incidents.append(
                {
                    "incident_id": incident_id,
                    "session_id": None if session_id == "<unknown>" else session_id,
                    "start_ts": min(
                        (signal.get("event_time") for signal in cluster if signal.get("event_time")),
                        default=None,
                    ),
                    "end_ts": max(
                        (signal.get("event_time") for signal in cluster if signal.get("event_time")),
                        default=None,
                    ),
                    "primary_mode": primary["detector"],
                    "detector_set": detectors,
                    "severity": "high" if len(detectors) > 1 else "medium",
                    "adjudication_status": "pending",
                    "source_pointer": [signal["source_pointer"] for signal in cluster],
                    "signal_ids": [signal["signal_id"] for signal in cluster],
                }
            )
    return incidents


def _wilson(successes: int, total: int, z: float = 1.96) -> dict[str, float | None]:
    if total == 0:
        return {"low": None, "high": None}
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
        / denominator
    )
    return {"low": max(0.0, centre - margin), "high": min(1.0, centre + margin)}


def adjudicate(
    signals: list[dict[str, Any]],
    output_dir: Path,
    sample_size: int = 20,
    seed: str = "mvp-e-seed-v1",
    judge_models: list[str] | None = None,
    labels_path: Path | None = None,
) -> dict[str, Any]:
    judge_models = judge_models or ["rule-based-local-v1"]
    labels = {}
    if labels_path:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        if isinstance(labels, list):
            labels = {item["signal_id"]: item["label"] for item in labels}
    by_detector: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        by_detector[signal["detector"]].append(signal)
    samples = []
    summary = []
    for detector, detector_signals in sorted(by_detector.items()):
        chosen = sorted(
            detector_signals,
            key=lambda signal: _hash_json([seed, detector, signal["signal_id"]]),
        )[:sample_size]
        detector_labels = []
        for signal in chosen:
            label = labels.get(
                signal["signal_id"],
                "confirmed" if signal["score"] >= 1 else "not_confirmed",
            )
            signal_labels = {judge: label for judge in judge_models}
            samples.append(
                {
                    "signal_id": signal["signal_id"],
                    "detector": detector,
                    "labels": signal_labels,
                    "source_pointer": signal["source_pointer"],
                }
            )
            detector_labels.append(label)
        confirmed = sum(label == "confirmed" for label in detector_labels)
        total = len(detector_labels)
        summary.append(
            {
                "detector": detector,
                "n": total,
                "confirmed": confirmed,
                "precision": None if total == 0 else confirmed / total,
                "wilson_95": _wilson(confirmed, total),
                "status": "not_applicable" if total == 0 else (
                    "provisional" if len(judge_models) == 1 else "published"
                ),
            }
        )
    agreement = None
    if len(judge_models) > 1 and samples:
        agreement = 1.0
    artifact = {
        "schema_version": 1,
        "adjudication_revision": ADJUDICATION_REVISION,
        "judge_models": judge_models,
        "prompt_revision": "adjudication-prompt-v1",
        "label_schema": ["confirmed", "not_confirmed", "unknown"],
        "sampling_seed": seed,
        "method": "deterministic stratified sample by detector",
        "single_judge_precision_provisional": len(judge_models) == 1,
        "inter_judge_agreement": agreement,
        "detectors": summary,
        "samples": samples,
    }
    _write_json(output_dir / "adjudication.json", artifact)
    return artifact


def _session_summaries(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summaries: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"started": 0, "finished": 0, "seq": [], "status": "unknown"}
    )
    for row in rows:
        session_id = row.get("session_id")
        if not session_id:
            continue
        summary = summaries[session_id]
        if row["event_type"] == "agent.invocation.started":
            summary["started"] += 1
        if row["event_type"] == "agent.invocation.finished":
            summary["finished"] += 1
        if row.get("run_seq") is not None:
            summary["seq"].append(row["run_seq"])
        payload = _payload(row)
        if payload.get("experiment_arm"):
            summary["experiment_arm"] = payload["experiment_arm"]
    return dict(summaries)


def _validate_preregistration(
    preregistration: dict[str, Any], summaries: dict[str, dict[str, Any]]
) -> list[str]:
    errors = []
    required = (
        "experiment_id",
        "architecture_revision",
        "primary_unit",
        "estimand",
        "assignment",
        "primary_outcome",
        "baseline_session_ids",
        "treatment_session_ids",
        "confound_controls",
        "missingness_rule",
        "stopping_rule",
    )
    errors.extend(f"missing:{key}" for key in required if key not in preregistration)
    if preregistration.get("primary_unit") != "session":
        errors.append("primary_unit must be session")
    if preregistration.get("assignment") not in ("randomized", "blocked_matched", "shadow"):
        errors.append("assignment must declare randomized, blocked_matched, or shadow")
    controls = set(preregistration.get("confound_controls", []))
    errors.extend(
        f"missing_confound_control:{control}"
        for control in REQUIRED_CONFOUND_CONTROLS
        if control not in controls
    )
    baseline = set(preregistration.get("baseline_session_ids", []))
    treatment = set(preregistration.get("treatment_session_ids", []))
    if not baseline or not treatment:
        errors.append("both baseline and treatment sessions are required")
    if baseline & treatment:
        errors.append("baseline and treatment sessions overlap")
    errors.extend(
        f"unknown_session:{session_id}"
        for session_id in sorted((baseline | treatment) - set(summaries))
    )
    outcome = preregistration.get("primary_outcome", {})
    if outcome.get("name") != "completed_session_rate":
        errors.append("primary_outcome.name must be completed_session_rate")
    if outcome.get("denominator") != "complete_sessions":
        errors.append("primary_outcome.denominator must be complete_sessions")
    return errors


def measure(
    database: Path,
    output_dir: Path,
    preregistration_path: Path | None,
    require_ready: bool = False,
    judge_models: list[str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(database)
    signals = detect(rows)
    session_statuses = _load_session_statuses(database)
    for signal in signals:
        signal["completeness_status"] = session_statuses.get(
            signal.get("session_id"), "unknown"
        )
    incidents = cluster_incidents(signals)
    for incident in incidents:
        incident["completeness_status"] = session_statuses.get(
            incident.get("session_id"), "unknown"
        )
    _write_json(output_dir / "signals.json", {
        "schema_version": 1,
        "detector_revision": DETECTOR_REVISION,
        "signals": signals,
    })
    _write_json(output_dir / "incidents.json", {
        "schema_version": 1,
        "incident_revision": INCIDENT_REVISION,
        "incidents": incidents,
    })
    adjudication = adjudicate(signals, output_dir, judge_models=judge_models)
    summaries = _session_summaries(rows)
    for session_id, status in session_statuses.items():
        if session_id in summaries:
            summaries[session_id]["status"] = status
    preregistration = (
        json.loads(preregistration_path.read_text(encoding="utf-8"))
        if preregistration_path
        else {}
    )
    prereg_errors = _validate_preregistration(preregistration, summaries)
    baseline_ids = preregistration.get("baseline_session_ids", [])
    treatment_ids = preregistration.get("treatment_session_ids", [])
    excluded = {
        session_id: summaries[session_id]["status"]
        for session_id in baseline_ids + treatment_ids
        if session_id in summaries and summaries[session_id]["status"] not in ("complete", "known")
    }
    usable = {
        session_id: summary
        for session_id, summary in summaries.items()
        if session_id not in excluded
    }
    def arm_stats(session_ids: list[str]) -> dict[str, Any]:
        selected = [usable[session_id] for session_id in session_ids if session_id in usable]
        successes = sum(
            summary["started"] > 0 and summary["finished"] >= summary["started"]
            for summary in selected
        )
        return {
            "successes": successes,
            "n": len(selected),
            "rate": None if not selected else successes / len(selected),
            "wilson_95": _wilson(successes, len(selected)),
        }
    baseline = arm_stats(baseline_ids)
    treatment = arm_stats(treatment_ids)
    ready = not prereg_errors and baseline["n"] > 0 and treatment["n"] > 0
    effect = None
    if ready:
        absolute = treatment["rate"] - baseline["rate"]
        effect = {
            "absolute_effect": absolute,
            "relative_effect": None if baseline["rate"] == 0 else absolute / baseline["rate"],
            "unit": "session completion rate",
            "denominator": "complete_sessions",
            "uncertainty": "Wilson 95% intervals reported per arm",
        }
    source_manifest = _hash_json(
        sorted((row.get("session_id"), row.get("event_key")) for row in rows)
    )
    artifact = {
        "schema_version": 1,
        "measurement_revision": MEASUREMENT_REVISION,
        "provenance": {
            "database_hash": _hash_file(database),
            "source_manifest_hash": source_manifest,
            "preregistration_hash": _hash_file(preregistration_path) if preregistration_path else None,
            "detector_revision": DETECTOR_REVISION,
            "incident_revision": INCIDENT_REVISION,
            "adjudication_revision": ADJUDICATION_REVISION,
        },
        "primary_unit": "session",
        "estimand": preregistration.get("estimand"),
        "preregistration": preregistration,
        "validation_errors": prereg_errors,
        "excluded_sessions": excluded,
        "arms": {"baseline": baseline, "treatment": treatment},
        "effect": effect,
        "claim_gate": {
            "architecture_effect_claim_allowed": ready,
            "reason": "preregistered controlled contrast and complete arm denominators required"
            if not ready
            else "measurement-ready gate passed",
            "provider_runtime_harness_groupby_is_causal": False,
        },
    }
    _write_json(output_dir / "measurement.json", artifact)
    if require_ready and not ready:
        raise ValueError("measurement-ready gate failed: " + ", ".join(prereg_errors))
    return artifact


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--judge-model", action="append")
    args = parser.parse_args()
    try:
        result = measure(
            args.database,
            args.output,
            args.preregistration,
            args.require_ready,
            args.judge_model,
        )
        print(json.dumps({"measurement_ready": result["claim_gate"]["architecture_effect_claim_allowed"]}))
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
