"""Global-sequence ledger replay, cursor, and queue coverage."""

from __future__ import annotations

import json
import queue
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from tl_loop.events.queue import LedgerQueue, QueueError
from tl_loop.events.reader import (
    ActiveTail,
    FindingKind,
    LedgerReader,
    LedgerReadError,
    SequenceStatus,
)
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


def test_reader_finds_terminal_invocation_payload_with_shared_segment_parser(
    tmp_path: Path,
) -> None:
    segments = tmp_path / "segments"
    _write_segment(
        segments,
        1,
        [
            {
                "type": "agent.invocation.finished",
                "agent_id": "slice-a-opencode",
                "data": {
                    "slice_id": "slice-a",
                    "invocation_id": "inv-1",
                    "status": "exited",
                },
            }
        ],
    )

    result = LedgerReader(segments).find_invocation_finished("slice-a-opencode", "slice-a")

    assert result == {
        "slice_id": "slice-a",
        "invocation_id": "inv-1",
        "status": "exited",
    }


def test_reader_keeps_empty_recorded_slice_compatible_with_agent_match(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    _write_segment(
        segments,
        1,
        [
            {
                "type": "agent.invocation.finished",
                "agent_id": "slice-a-opencode",
                "data": {"slice_id": "", "status": "exited"},
            }
        ],
    )

    assert LedgerReader(segments).find_invocation_finished("slice-a-opencode", "slice-a") == {
        "slice_id": "",
        "status": "exited",
    }


def test_reader_reuses_unchanged_segment_for_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = tmp_path / "segments"
    _write_segment(
        segments,
        1,
        [
            {
                "type": "agent.invocation.finished",
                "agent_id": "slice-a-opencode",
                "data": {"slice_id": "slice-a", "status": "exited"},
            }
        ],
    )
    reader = LedgerReader(segments)
    reads: list[Path] = []
    original = reader._read_segment

    def counted(segment: Path, active_segment: Path | None) -> object:
        reads.append(segment)
        return original(segment, active_segment)

    monkeypatch.setattr(reader, "_read_segment", counted)

    reader.find_invocation_finished("slice-a-opencode", "slice-a")
    reader.find_invocation_finished("slice-a-opencode", "slice-a")

    assert reads == [segments / "segment-000000000001.jsonl"]


def test_reader_drops_cached_active_tail_when_new_segment_becomes_active(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    (segments / "segment-000000000001.jsonl").write_bytes(b'{"partial":')
    reader = LedgerReader(segments)

    assert reader.read_from().active_tail is not None

    (segments / "segment-000000000002.jsonl").write_text(
        json.dumps(_fixture_events()[0]) + "\n", encoding="utf-8"
    )

    assert reader.read_from().active_tail is None


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


def test_reader_scopes_authoritative_id_separately_from_local_checkpoint(tmp_path: Path) -> None:
    events = deepcopy(_fixture_events()[:2])
    events[0]["run_id"] = "swarm-uuid"
    events[1]["run_id"] = "stale-swarm-uuid"
    segments = tmp_path / "segments"
    _write_segment(segments, 1, events)
    state_root = tmp_path / "state"
    create("root", {}, root_dir=state_root)

    reader = LedgerReader(
        segments,
        run_id="root",
        state_root=state_root,
        ledger_run_id="swarm-uuid",
    )

    assert [event.run_seq for event in reader.read_from().events] == [101]
    result = reader.read_from()
    assert len(result.findings) == 1
    assert result.findings[0].kind is FindingKind.RUN_ID_MISMATCH
    assert "stale-swarm-uuid" in result.findings[0].message
    assert reader.cursor() == 0


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


def test_active_segment_partial_tail_is_retried_after_append(tmp_path: Path) -> None:
    event = _fixture_events()[0]
    encoded = json.dumps(
        {**event, "data": {"slice_id": "leaf-é", "large": "x" * 4096}},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "segment-000000000001.jsonl"
    split = len(encoded) // 2
    path.write_bytes(encoded[:split])

    reader = LedgerReader(segments)
    assert reader.read_from().events == ()

    with path.open("ab") as stream:
        stream.write(encoded[split:] + b"\n")

    result = reader.read_from()
    assert [item.run_seq for item in result.events] == [101]


def test_finalized_malformed_record_remains_a_hard_failure(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    _write_raw_segment(segments, 1, ['{"committed":\n'])

    with pytest.raises(LedgerReadError, match=r"line 1"):
        LedgerReader(segments).read_from()


def test_finalized_invalid_utf8_preserves_segment_and_line(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "segment-000000000001.jsonl"
    path.write_bytes(b"\xff\n")

    with pytest.raises(LedgerReadError, match=r"line 1") as raised:
        LedgerReader(segments).read_from()

    assert raised.value.segment == path
    assert raised.value.line_number == 1


def test_concurrent_writer_partial_tail_is_retried_without_queue_failure(tmp_path: Path) -> None:
    event = _fixture_events()[0]
    encoded = json.dumps(
        {**event, "data": {"slice_id": "leaf-é", "large": "x" * 32_768}},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "segment-000000000001.jsonl"
    split = len(encoded) // 2
    ready = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with path.open("wb") as stream:
            stream.write(encoded[:split])
            stream.flush()
            ready.set()
            assert release.wait(timeout=2)
            stream.write(encoded[split:] + b"\n")
            stream.flush()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert ready.wait(timeout=2)
    event_queue = LedgerQueue(
        LedgerReader(segments),
        poll_interval=0.001,
        active_tail_timeout=0.5,
    ).start()
    try:
        with pytest.raises(queue.Empty):
            event_queue.get(timeout=0.05)
        time.sleep(0.16)
        release.set()
        assert event_queue.get(timeout=2).run_seq == 101
    finally:
        release.set()
        event_queue.close(timeout=2)
        writer_thread.join(timeout=2)
    assert not writer_thread.is_alive()


def test_abandoned_active_tail_eventually_fails_with_context(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "segment-000000000001.jsonl"
    path.write_bytes(b'{"abandoned":')
    event_queue = LedgerQueue(
        LedgerReader(segments),
        poll_interval=0.001,
        active_tail_timeout=0.01,
    ).start()
    try:
        with pytest.raises(QueueError, match=r"remained incomplete") as raised:
            event_queue.get(timeout=2)
        cause = raised.value.__cause__
        assert isinstance(cause, LedgerReadError)
        assert cause.segment == path
        assert cause.line_number == 1
        assert cause.byte_length == len(b'{"abandoned":')
        assert cause.elapsed_seconds is not None
        assert cause.elapsed_seconds >= cause.timeout_seconds == 0.01
    finally:
        event_queue.close(timeout=2)


def test_active_tail_timeout_uses_elapsed_time_for_any_poll_interval(tmp_path: Path) -> None:
    class ManualClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    path = tmp_path / "segment-000000000001.jsonl"
    tail = ActiveTail(path, line_number=1, byte_length=12)
    for poll_interval in (0.001, 0.05):
        clock = ManualClock()
        event_queue = LedgerQueue(
            LedgerReader(tmp_path),
            poll_interval=poll_interval,
            active_tail_timeout=0.25,
            clock=clock,
        )
        event_queue._check_active_tail(tail)
        clock.now = 0.20
        event_queue._check_active_tail(tail)
        clock.now = 0.20
        event_queue._check_active_tail(ActiveTail(path, line_number=1, byte_length=13))
        clock.now = 0.44
        event_queue._check_active_tail(ActiveTail(path, line_number=1, byte_length=13))
        clock.now = 0.46
        with pytest.raises(LedgerReadError, match=r"0.260s.*timeout 0.250s"):
            event_queue._check_active_tail(ActiveTail(path, line_number=1, byte_length=13))


def test_queue_retries_partial_tail_and_reports_hard_failures(tmp_path: Path) -> None:
    event = _fixture_events()[0]
    encoded = json.dumps(event, sort_keys=True).encode("utf-8")
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "segment-000000000001.jsonl"
    split = len(encoded) // 2
    path.write_bytes(encoded[:split])
    event_queue = LedgerQueue(LedgerReader(segments), poll_interval=0.005).start()
    try:
        with pytest.raises(queue.Empty):
            event_queue.get(timeout=0.05)
        with path.open("ab") as stream:
            stream.write(encoded[split:] + b"\n")
        received = event_queue.get(timeout=2)
        assert received.run_seq == 101
    finally:
        event_queue.close(timeout=2)

    _write_raw_segment(segments, 2, ['{"committed":\n'])
    broken = LedgerQueue(LedgerReader(segments), poll_interval=0.005).start()
    try:
        with pytest.raises(QueueError, match=r"cursor=0.*parse .*line 1") as raised:
            broken.get(timeout=2)
        assert isinstance(raised.value.__cause__, LedgerReadError)
    finally:
        broken.close(timeout=2)


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
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8"
    )


def _write_raw_segment(directory: Path, index: int, lines: list[str | bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"segment-{index:012}.jsonl"
    path.write_bytes(b"".join(line if isinstance(line, bytes) else line.encode() for line in lines))
