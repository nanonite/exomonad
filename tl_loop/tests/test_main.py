"""Tests for operator-facing TL loop commands."""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tl_loop import __main__ as launcher
from tl_loop.client.transport import DEFAULT_TIMEOUT_SECONDS, JsonObject
from tl_loop.events.queue import DEFAULT_ACTIVE_TAIL_TIMEOUT_SECONDS
from tl_loop.loop.driver import TLLoopConfig
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


def test_run_passes_time_budgets_to_constructors(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, dict[str, object]] = {}

    class EmptySource:
        def start(self) -> EmptySource:
            return self

        def close(self, timeout: float) -> None:
            del timeout

    def transport_client(**kwargs: object) -> object:
        captured["transport"] = kwargs
        return object()

    def ledger_queue(*args: object, **kwargs: object) -> EmptySource:
        captured["ledger_queue"] = kwargs
        return EmptySource()

    def tlloop_config(**kwargs: object) -> object:
        captured["tlloop_config"] = kwargs
        return object()

    monkeypatch.setattr(launcher, "TransportClient", transport_client)
    monkeypatch.setattr(launcher, "LedgerQueue", ledger_queue)
    monkeypatch.setattr(launcher, "TLLoopConfig", tlloop_config)
    monkeypatch.setattr(launcher, "EffectClient", lambda *a, **kw: object())
    monkeypatch.setattr(launcher, "LedgerReader", lambda *a, **kw: object())
    monkeypatch.setattr(launcher, "_load_plan", lambda path, wait: {"plan": {}})
    monkeypatch.setattr(launcher, "_plan_from_document", lambda document: object())
    monkeypatch.setattr(launcher, "_run_id", lambda document, configured: "root")
    monkeypatch.setattr(launcher, "_authoritative_ledger_run_id", lambda root: None)
    monkeypatch.setattr(launcher, "load_policy", lambda path: object())
    monkeypatch.setattr(launcher, "load_capability", lambda path, policy_path: object())
    monkeypatch.setattr(launcher, "load_model_catalog", lambda path: None)
    monkeypatch.setattr(launcher, "tl_run", lambda *a, **kw: object())

    launcher._run(
        argparse.Namespace(
            project_root=tmp_path,
            plan=Path("plan.json"),
            wait_for_plan=False,
            run_id="root",
            poll_interval=0.25,
            max_events=256,
            transport_timeout=45.5,
            active_tail_timeout=60.0,
            task_timeout=90.0,
        )
    )

    assert captured["transport"] == {"project_root": tmp_path.resolve(), "timeout": 45.5}
    assert captured["ledger_queue"]["active_tail_timeout"] == 60.0
    assert captured["tlloop_config"]["task_timeout_seconds"] == 90.0
    assert captured["tlloop_config"]["task_timeout_source"] == "project"


def test_run_defaults_preserve_current_values() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 10.0
    assert DEFAULT_ACTIVE_TAIL_TIMEOUT_SECONDS == 30.0
    assert launcher.DEFAULT_TASK_TIMEOUT_SECONDS == 3600.0

    args = launcher._parser().parse_args(["run", "--project-root", "/tmp/repo"])
    assert args.transport_timeout == 10.0
    assert args.active_tail_timeout == 30.0
    assert args.task_timeout == 3600.0

    assert TLLoopConfig.task_timeout_seconds == 3600.0


def test_positive_float_rejects_nan() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        launcher._positive_float("nan")
    with pytest.raises(argparse.ArgumentTypeError):
        launcher._positive_float("0")
    assert launcher._positive_float("inf") == float("inf")
    assert launcher._positive_float("10.0") == 10.0


def test_non_negative_float_rejects_nan_and_negative() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        launcher._non_negative_float("nan")
    with pytest.raises(argparse.ArgumentTypeError):
        launcher._non_negative_float("-1")
    assert launcher._non_negative_float("0") == 0.0
