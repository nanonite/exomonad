"""Tests for operator-facing TL loop commands."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tl_loop import __main__ as launcher
from tl_loop.client.transport import JsonObject
from tl_loop.state.store import RunStore, create


@dataclass
class RecordingTransport:
    """Transport double that records controller events."""

    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        return {"success": True, "result": None}


@pytest.mark.parametrize("source", ("cli", "control"))
def test_gate_answer_emits_source_dimension(
    tmp_path: Path,
    monkeypatch,
    source: str,
) -> None:
    transport = RecordingTransport()
    project_root = tmp_path
    state_root = project_root / ".exo" / "tl-loop"
    create("run", {}, root_dir=state_root)
    RunStore("run", state_root).set_gate("review")
    monkeypatch.setattr(launcher, "TransportClient", lambda project_root: transport)

    launcher._set_gate(
        argparse.Namespace(
            project_root=project_root,
            run_id="run",
            name="review",
            approve=True,
            reject=False,
            source=source,
        )
    )

    assert transport.calls == [
        (
            "emit_controller_event",
            {
                "event_type": "tl.gate_answered",
                "payload": {
                    "gate_name": "review",
                    "decision": "approved",
                    "source": source,
                },
            },
        )
    ]
