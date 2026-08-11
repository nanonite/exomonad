"""Hermetic coverage for bounded PR repair handoffs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject
from tl_loop.rlm.repair import (
    RepairPRStateError,
    compose_repair,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.state.schema import Verdict


@dataclass
class FakeBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


@dataclass
class FakeClient:
    open_pr: bool = True
    merged: bool = False
    calls: list[tuple[str, object]] = field(default_factory=list)

    def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
        self.calls.append(("watcher_pr_state", pr_number))
        return ToolResult(
            raw={"success": True},
            success=True,
            result={
                "state": "open" if self.open_pr else "closed",
                "merged": self.merged,
                "head_branch": "task/owned",
                "head_sha": "head-a",
            },
            error=None,
        )

    def resume_pr(self, **kwargs: object) -> ToolResult:
        self.calls.append(("resume_pr", kwargs))
        return ToolResult(
            raw={"success": True},
            success=True,
            result=None,
            error=None,
        )


def _choice(backend: FakeBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10_000,
    )


def _review() -> dict[str, object]:
    return {
        "verdict": Verdict.NO_GO.value,
        "reasons": [
            {
                "severity": "blocking",
                "file": "src/owned.py",
                "line": 8,
                "claim": "The failure path is unhandled",
            }
        ],
    }


def _handoff(*, outside: bool = False) -> dict[str, object]:
    path = "src/other.py" if outside else "src/owned.py"
    return {
        "root_cause": f"The failure in {path} is unhandled",
        "proposed_solution": f"Handle the failure in {path}",
        "read_first": [path],
        "steps": [f"Update {path}"],
        "verify": ["just tl-loop-test"],
        "boundary": [f"Only edit {path}"],
        "done_criteria": ["The failure path is covered"],
    }


def _pr(client: FakeClient, *, attempts: int = 0) -> dict[str, object]:
    return {
        "pr_number": 42,
        "paths": ["src/owned.py"],
        "attempts": attempts,
        "client": client,
    }


def test_valid_repair_round_trips_through_resume_pr() -> None:
    backend = FakeBackend([RlmResponse(_handoff())])
    client = FakeClient()
    pr = _pr(client)

    result = compose_repair(
        pr,
        Verdict.NO_GO,
        _review(),
        model_choice=_choice(backend),
    )

    assert result.read_first == ("src/owned.py",)
    assert [name for name, _ in client.calls] == ["watcher_pr_state", "resume_pr"]
    resume_arguments = client.calls[1][1]
    assert isinstance(resume_arguments, dict)
    assert resume_arguments["pr_number"] == 42
    assert "name" not in resume_arguments
    assert "branch" not in resume_arguments
    assert "agent_type" not in resume_arguments
    assert pr["attempts"] == 1


def test_out_of_boundary_handoff_is_rejected_and_retried() -> None:
    backend = FakeBackend(
        [
            RlmResponse(_handoff(outside=True)),
            RlmResponse(_handoff()),
        ]
    )
    client = FakeClient()

    result = compose_repair(
        _pr(client),
        Verdict.NO_GO,
        _review(),
        model_choice=_choice(backend),
    )

    assert result.proposed_solution == "Handle the failure in src/owned.py"
    assert len(backend.requests) == 2
    assert len(client.calls) == 2
    retry_sections = cast(list[JsonObject], backend.requests[1].inputs["sections"])
    feedback = next(
        section for section in retry_sections if section["name"] == "validation_feedback"
    )
    content = feedback["content"]
    assert isinstance(content, str)
    assert "src/other.py" in content


def test_closed_pr_refuses_composition_before_the_model() -> None:
    backend = FakeBackend([RlmResponse(_handoff())])
    client = FakeClient(open_pr=False)

    with pytest.raises(RepairPRStateError):
        compose_repair(
            _pr(client),
            Verdict.NO_GO,
            _review(),
            model_choice=_choice(backend),
        )

    assert backend.requests == []
    assert client.calls == [("watcher_pr_state", 42)]


def test_attempts_increments_once_per_repair_dispatch() -> None:
    backend = FakeBackend([RlmResponse(_handoff())])
    client = FakeClient()
    pr = _pr(client, attempts=4)

    compose_repair(
        pr,
        Verdict.NO_GO,
        _review(),
        model_choice=_choice(backend),
    )
    assert pr["attempts"] == 5
    assert len([name for name, _ in client.calls if name == "resume_pr"]) == 1


def test_closed_handoff_schema_has_exactly_seven_sections() -> None:
    backend = FakeBackend([RlmResponse(_handoff())])
    result = compose_repair(
        _pr(FakeClient()),
        Verdict.NO_GO,
        _review(),
        model_choice=_choice(backend),
    )

    assert set(result.to_mapping()) == {
        "root_cause",
        "proposed_solution",
        "read_first",
        "steps",
        "verify",
        "boundary",
        "done_criteria",
    }
