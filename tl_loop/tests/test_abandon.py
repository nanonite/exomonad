from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.loop.abandon import AbandonmentError, abandon_slice
from tl_loop.state.schema import BudgetLedger, SliceState, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.fsm.phase import TLPhase


@dataclass
class RecordingTransport:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        del role, name
        self.calls.append((tool_name, arguments))
        return {"success": True, "result": {"event_id": "event-1", "run_seq": 2}}


def _slice(status: SliceStatus = SliceStatus.SPAWNED) -> SliceState:
    return SliceState(
        id="slice-a",
        status=status,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model="model-a",
        branch="task/slice-a",
        worktree=".exo/worktrees/slice-a",
        pr_number=42,
        reviewed_head="abc123",
        attempts=1,
        verdict=None,
        dispatch_intent_id="intent-a",
        dispatch_agent_id="slice-a-opencode",
        dispatch_invocation_id="inv-a",
        dispatch_authoritative_event_seq=1,
    )


def _store(tmp_path: Path, slice_state: SliceState) -> RunStore:
    state_root = tmp_path / ".exo" / "tl-loop"
    create("root", {}, root_dir=state_root)
    store = RunStore("root", state_root)
    store.checkpoint(
        TLPhase.TLDispatching,
        {slice_state.id: slice_state},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    return store


def test_abandon_disposes_once_and_parks_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path, _slice())
    transport = RecordingTransport()
    client = EffectClient(transport)

    result = abandon_slice(tmp_path, "root", "slice-a", effects=client)
    assert result["status"] == "abandoned"
    assert store.load().slices["slice-a"].status is SliceStatus.PARKED
    assert [name for name, _ in transport.calls] == [
        "emit_controller_event",
        "cleanup",
    ]

    second = abandon_slice(tmp_path, "root", "slice-a", effects=client)
    assert second["status"] == "already_abandoned"
    assert len(transport.calls) == 2


@pytest.mark.parametrize("status", [SliceStatus.PENDING, SliceStatus.MERGED, SliceStatus.FAILED])
def test_abandon_rejects_non_live_attempt(tmp_path: Path, status: SliceStatus) -> None:
    _store(tmp_path, _slice(status))

    with pytest.raises(AbandonmentError, match="not live"):
        abandon_slice(tmp_path, "root", "slice-a", effects=EffectClient(RecordingTransport()))


def test_abandon_rejects_unknown_slice(tmp_path: Path) -> None:
    _store(tmp_path, _slice())

    with pytest.raises(AbandonmentError, match="does not exist"):
        abandon_slice(tmp_path, "root", "missing", effects=EffectClient(RecordingTransport()))
