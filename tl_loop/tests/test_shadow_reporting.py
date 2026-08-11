"""Recorder, actual-call extraction, and four-bucket diff coverage."""

from __future__ import annotations

import json
from pathlib import Path

from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.shadow import IntendedAction
from tl_loop.shadow.actual import ActualAction, ActualActionReader
from tl_loop.shadow.diff import (
    ActionBucket,
    diff_actions,
    generate_report,
    normalize_arguments,
    render_report,
)
from tl_loop.shadow.recorder import IntendedActionRecorder


def test_normalizer_orders_keys_omits_nulls_and_normalizes_ids() -> None:
    assert normalize_arguments(
        {"z": None, "nested": {"b": 2, "a": 1}, "issue_id": "42", "items": ["b", "a"]}
    ) == {
        "force": False,
        "include_dead": False,
        "issue_id": 42,
        "items": ["b", "a"],
        "nested": {"a": 1, "b": 2},
        "sweep": False,
    }
    assert normalize_arguments({"force": False}) == normalize_arguments({})


def test_recorder_persists_every_intended_action(tmp_path: Path) -> None:
    action = _shadow("dispatch", "agent-a", {"issue_id": 7}, 11, "planning", "waiting")
    recorder = IntendedActionRecorder("run-1", root_dir=tmp_path)

    recorder.record(action)

    assert recorder.read() == (
        {
            "arguments": {"issue_id": 7},
            "event_seq": 11,
            "kind": "dispatch",
            "phase_after": "tl_waiting",
            "phase_before": "tl_planning",
            "rationale": "shadow rationale",
            "target": "agent-a",
        },
    )
    assert recorder.read_actions() == (action,)
    assert recorder.path == tmp_path / "run-1" / "intended.jsonl"


def test_actual_reader_filters_run_and_tool_called_rows(tmp_path: Path) -> None:
    segment = tmp_path / "segment-000000000000.jsonl"
    segment.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"run_id": "other", "run_seq": 1, "type": "tool.called", "agent_id": "a", "data": {}},
                {
                    "run_id": "run-1",
                    "run_seq": 12,
                    "type": "tool.called",
                    "agent_id": "agent-a",
                    "data": {"tool_name": "dispatch", "arguments": {"issue_id": "7"}},
                },
                {"run_id": "run-1", "run_seq": 13, "type": "pr.merged", "agent_id": "agent-a", "data": {}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    actions = ActualActionReader(tmp_path).read("run-1")

    assert actions == (
        ActualAction(
            "dispatch",
            "agent-a",
            {"issue_id": "7"},
            12,
            "actual tool call observed by the server",
            "agent-a",
        ),
    )


def test_diff_reports_match_divergent_extra_and_missing_without_dropping_rows(tmp_path: Path) -> None:
    shadow = [
        _shadow("dispatch", "agent-a", {"issue_id": 7}, 1, "planning", "waiting"),
        _shadow("merge", "agent-b", {"pr_number": 8}, 2, "waiting", "all_merged"),
        _shadow("repair", "agent-c", {}, 3, "waiting", "failed"),
    ]
    actual = [
        ActualAction("dispatch", "agent-a", {"issue_id": "7"}, 10, "actual match"),
        ActualAction("different", "agent-b", {"pr_number": 8}, 11, "actual divergence"),
        ActualAction("missing-shadow", "agent-d", {}, 12, "actual only"),
    ]

    report = diff_actions("run-1", shadow, actual)

    assert [entry.bucket for entry in report.entries] == [
        ActionBucket.MATCH,
        ActionBucket.DIVERGENT,
        ActionBucket.EXTRA,
        ActionBucket.MISSING,
    ]
    assert report.counts == {
        ActionBucket.MATCH: 1,
        ActionBucket.DIVERGENT: 1,
        ActionBucket.EXTRA: 1,
        ActionBucket.MISSING: 1,
    }
    path = render_report(report, docs_dir=tmp_path)
    markdown = path.read_text(encoding="utf-8")
    assert "| MATCH | 1 |" in markdown
    assert "| DIVERGENT | 1 |" in markdown
    assert "| EXTRA | 1 |" in markdown
    assert "| MISSING | 1 |" in markdown
    assert "| DIVERGENT | 2 / 11 |" in markdown


def test_generate_report_reads_both_streams_and_writes_required_path(tmp_path: Path) -> None:
    shadow_root = tmp_path / "shadow"
    recorder = IntendedActionRecorder("run-2", root_dir=shadow_root)
    recorder.record(_shadow("dispatch", "agent-a", {}, 1, "planning", "waiting"))
    segments = tmp_path / "segments"
    segments.mkdir()
    (segments / "segment-000000000000.jsonl").write_text(
        json.dumps(
            {
                "run_id": "run-2",
                "run_seq": 2,
                "type": "tool.called",
                "agent_id": "agent-a",
                "data": {"tool_name": "dispatch", "arguments": {}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    path = generate_report("run-2", shadow_root=shadow_root, segments_dir=segments, docs_dir=tmp_path / "docs")

    assert path == tmp_path / "docs" / "tl-loop-shadow-report-run-2.md"
    assert "| MATCH | 1 |" in path.read_text(encoding="utf-8")


def _shadow(
    kind: str,
    target: str,
    arguments: dict[str, object],
    event_seq: int,
    before: str,
    after: str,
) -> IntendedAction:
    return IntendedAction(
        kind,
        target,
        arguments,
        "shadow rationale",
        event_seq,
        TLPhase(f"tl_{before}"),
        TLPhase(f"tl_{after}"),
    )
