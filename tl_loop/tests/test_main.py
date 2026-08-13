"""Tests for operator-facing TL loop commands."""

from __future__ import annotations

import argparse
import io
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


def test_accepted_plan_proposal_emits_no_plan_body(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    transport = RecordingTransport()
    monkeypatch.setattr(launcher, "TransportClient", lambda project_root: transport)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"plan": {"leaves": []}}'))

    launcher._print_plan_proposal(argparse.Namespace(project_root=tmp_path, run_id="run"))

    assert transport.calls[0][1] == {
        "event_type": "tl.plan_proposed",
        "payload": {"run_id": "run", "accepted": True},
    }
    assert "plan" not in transport.calls[0][1]["payload"]
    assert '"inert": true' in capsys.readouterr().out


def test_rejected_plan_proposal_emits_bounded_reason_without_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transport = RecordingTransport()
    monkeypatch.setattr(launcher, "TransportClient", lambda project_root: transport)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"plan": {"leaves": []}, "body": "secret"}'))

    with pytest.raises(launcher.PlanValidationError):
        launcher._print_plan_proposal(argparse.Namespace(project_root=tmp_path, run_id="run"))

    payload = transport.calls[0][1]["payload"]
    assert payload["run_id"] == "run"
    assert payload["accepted"] is False
    assert isinstance(payload["rejection_reason"], str)
    assert len(payload["rejection_reason"]) <= launcher.PLAN_REJECTION_REASON_LIMIT
    assert "secret" not in str(payload)
