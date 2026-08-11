"""Global-sequence ledger replay, cursor, and queue coverage."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import FindingKind, LedgerReader, SequenceStatus
from tl_loop.state.store import create

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_projection_events.json"


def test_replay_from_arbitrary_run_seq_returns_exact_cross_segment_suffix(tmp_path: Path) -> None:
    events = _fixture_events()
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events[:2])
    _write_segment(segments, 2, events[2:4])

    result = LedgerReader(segments).read_from(101)

    assert [event.run_seq for event in result.events] == [102, 103, 104]
    assert result.sequence_status is SequenceStatus.COMPLETE


def test_cursor_persists_and_resume_spans_segment_boundary(tmp_path: Path) -> None:
    events = _fixture_events()
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events[:1])
    _write_segment(segments, 2, events[1:2])
    state_root = tmp_path / "state"
    create("run-1", {}, root_dir=state_root)
    reader = LedgerReader(segments, run_id="run-1", state_root=state_root)

    first = reader.read_from()
    assert [event.run_seq for event in first.events] == [101, 102]
    assert reader.acknowledge(first.events[0]) == 101

    resumed = LedgerReader(segments, run_id="run-1", state_root=state_root)
    assert resumed.cursor() == 101
    assert [event.run_seq for event in resumed.read_from().events] == [102]


def test_retired_segment_during_tail_does_not_corrupt_global_cursor(tmp_path: Path) -> None:
    events = _fixture_events()
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events[:1])
    _write_segment(segments, 2, events[1:2])
    reader = LedgerReader(segments)

    assert [event.run_seq for event in reader.read_from().events] == [101, 102]
    (segments / "segment-000000000001.jsonl").unlink()

    assert [event.run_seq for event in reader.read_from(101).events] == [102]


def test_partial_sequence_surfaces_with_rust_sequence_status(tmp_path: Path) -> None:
    events = _fixture_events()
    segments = tmp_path / "segments"
    _write_segment(segments, 1, [events[0], events[2]])

    result = LedgerReader(segments).read_from()

    assert result.sequence_status is SequenceStatus.PARTIAL
    assert [event.run_seq for event in result.events] == [101, 103]


def test_missing_run_seq_is_a_hard_finding_and_is_not_consumed(tmp_path: Path) -> None:
    event = deepcopy(_fixture_events()[0])
    event["run_seq"] = None
    segments = tmp_path / "segments"
    _write_segment(segments, 1, [event])

    result = LedgerReader(segments).read_from()

    assert result.events == ()
    assert result.sequence_status is SequenceStatus.UNKNOWN
    assert len(result.findings) == 1
    assert result.findings[0].kind is FindingKind.MISSING_RUN_SEQ
    assert result.findings[0].hard
    assert "server-side emit gap" in result.findings[0].message


def test_superseded_event_does_not_drive_projection(tmp_path: Path) -> None:
    original = deepcopy(_fixture_events()[0])
    correction = {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000099",
        "id": "00000000-0000-4000-8000-000000000099",
        "event_time": original["event_time"],
        "observed_at": original["observed_at"],
        "run_seq": 102,
        "type": "event.superseded",
        "agent_id": None,
        "run_id": "run-1",
        "session_id": "session-1",
        "invocation_id": None,
        "generation": None,
        "source": "rust",
        "lifecycle_state": "emitted",
        "data": {"superseded_event_id": original["event_id"], "reason": "corrected"},
    }
    segments = tmp_path / "segments"
    _write_segment(segments, 1, [original, correction])

    result = LedgerReader(segments).read_from()

    assert result.events == ()
    assert result.sequence_status is SequenceStatus.COMPLETE


def test_kill_and_restart_redelivers_only_unacknowledged_events(tmp_path: Path) -> None:
    events = _fixture_events()
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events[:2])
    state_root = tmp_path / "state"
    create("run-1", {}, root_dir=state_root)
    reader = LedgerReader(segments, run_id="run-1", state_root=state_root)
    queue = LedgerQueue(reader, maxsize=1, poll_interval=0.01).start()

    first = queue.get(timeout=2)
    assert first.run_seq == 101
    queue.acknowledge(first)
    second = queue.get(timeout=2)
    assert second.run_seq == 102
    queue.close(timeout=2)

    restarted = LedgerQueue(
        LedgerReader(segments, run_id="run-1", state_root=state_root),
        maxsize=1,
        poll_interval=0.01,
    ).start()
    redelivered = restarted.get(timeout=2)
    restarted.close(timeout=2)

    assert redelivered.run_seq == 102



def test_reader_scope_excludes_grandchild_events_from_root(tmp_path: Path) -> None:
    events = deepcopy(_fixture_events()[:3])
    for sequence, (event, agent, parent) in enumerate(
        zip(events, ("root", "child", "grandchild"), (None, "root", "child")), start=1
    ):
        event["run_id"] = "scope-run"
        event["run_seq"] = sequence
        event["agent_id"] = agent
        event["parent_agent_id"] = parent
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events)

def _fixture_events() -> list[dict[str, object]]:
    return cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _write_segment(directory: Path, index: int, events: list[dict[str, object]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"segment-{index:012}.jsonl"
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
