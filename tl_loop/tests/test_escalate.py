"""End-to-end escalation, atomic parking, and harness-switch coverage."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Mapping, cast

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.loop.escalate import (
    HarnessSwitchDecision,
    IssueCreationError,
    ParkResult,
    authorize_harness_switch,
    park,
    switch_harness,
)
from tl_loop.state.schema import BudgetLedger, ParkCause, SliceState, SliceStatus, Verdict
from tl_loop.state.store import RunStore, create


CAUSES = tuple(ParkCause)


def test_each_closed_cause_produces_a_parked_state() -> None:
    for cause in CAUSES:
        result = park(_slice(), cause)

        assert isinstance(result, SliceState)
        assert result.status is SliceStatus.PARKED
        assert result.park_cause is cause
        assert result.park_issue_id is None
        assert result.park_audit is not None
        assert result.park_audit["attempts"] == 2
        assert result.park_audit["verdict"] == Verdict.NO_GO.value
        assert result.park_audit["harness"] == "codex"
        assert result.park_audit["model"] == "gpt-test"


@pytest.mark.parametrize("cause", CAUSES)
def test_each_cause_creates_issue_and_blocks_transitive_dependents(
    tmp_path: Path, cause: ParkCause
) -> None:
    store = _store(tmp_path)
    issues: list[tuple[str, str]] = []

    def create_issue(title: str, description: str) -> int:
        issues.append((title, description))
        return 700

    result = park(
        _slice(),
        cause,
        store=store,
        issue_creator=create_issue,
        ledger=BudgetLedger(tokens=42, wall_seconds=3),
    )

    assert result == ParkResult(700, "root", ("child", "grandchild"))
    state = store.load()
    assert state.slices["root"].status is SliceStatus.PARKED
    assert state.slices["root"].park_cause is cause
    assert state.slices["root"].park_issue_id == 700
    assert state.slices["root"].park_audit is not None
    assert state.slices["root"].park_audit is not None
    ledger_audit = cast(Mapping[str, object], state.slices["root"].park_audit["ledger"])
    assert ledger_audit["tokens"] == 42
    assert state.slices["child"].status is SliceStatus.BLOCKED
    assert state.slices["child"].blocked_by == "root"
    assert state.slices["child"].park_issue_id == 700
    assert state.slices["grandchild"].status is SliceStatus.BLOCKED
    assert state.slices["grandchild"].blocked_by == "root"
    assert issues[0][0] == f"Escalate slice root: {cause.value}"
    assert cause.value in issues[0][1]
    assert '"needs-human"' not in issues[0][1]


def test_effect_issue_creation_has_needs_human_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    creator = RecordingCreator()

    result = park(_slice(), ParkCause.REVIEW_STUCK, store=store, issue_creator=creator)

    assert isinstance(result, ParkResult)
    assert creator.labels == ("needs-human",)
    assert creator.priority == "high"


def test_failed_issue_creation_does_not_mutate_state(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(IssueCreationError, match="positive issue ID"):
        park(_slice(), ParkCause.RETRIES_EXHAUSTED, store=store, issue_creator=lambda *_: 0)

    assert store.load().slices["root"].status is SliceStatus.PENDING


def test_declared_harness_switch_is_allowed_and_audited() -> None:
    decision = authorize_harness_switch(
        "codex", "claude", "review repair", "model-a", "high", ["claude"], env={}
    )

    assert isinstance(decision, HarnessSwitchDecision)
    assert decision.allowed is True
    assert decision.cause is None
    assert decision.audit["from_harness"] == "codex"
    assert decision.audit["to_harness"] == "claude"


def test_ungated_harness_switch_parks_with_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    creator = RecordingCreator()

    result = switch_harness(
        _slice(),
        "codex",
        "claude",
        "review repair",
        "model-a",
        "high",
        [],
        env={},
        store=store,
        issue_creator=creator,
    )

    assert isinstance(result, ParkResult)
    state = store.load()
    audit = state.slices["root"].park_audit
    assert state.slices["root"].park_cause is ParkCause.HARNESS_SWITCH_REQUESTED
    assert audit is not None
    assert audit["from_harness"] == "codex"
    assert audit["to_harness"] == "claude"
    assert audit["reason"] == "review repair"
    assert audit["model"] == "model-a"
    assert audit["effort"] == "high"


def test_operator_flag_allows_ungated_harness_switch() -> None:
    decision = authorize_harness_switch(
        "codex",
        "claude",
        "review repair",
        "model-a",
        "high",
        [],
        env={"EXOMONAD_ALLOW_HARNESS_SWITCH": "1"},
    )

    assert decision.allowed is True
    assert decision.cause is None


def _store(tmp_path: Path) -> RunStore:
    create(
        "escalate-test",
        {
            "slices": {
                "root": _record("root"),
                "child": _record("child", depends_on=["root"]),
                "grandchild": _record("grandchild", depends_on=["child"]),
            }
        },
        root_dir=tmp_path,
    )
    return RunStore("escalate-test", root_dir=tmp_path)


def _slice() -> SliceState:
    return SliceState(
        id="root",
        status=SliceStatus.PENDING,
        paths=("src/root.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-test",
        branch="task/root",
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=2,
        verdict=Verdict.NO_GO,
    )


def _record(slice_id: str, *, depends_on: list[str] | None = None) -> dict[str, object]:
    return {
        "id": slice_id,
        "status": "pending",
        "paths": [f"src/{slice_id}.py"],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "agent_type": "codex",
        "model": "gpt-test",
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }


class RecordingCreator:
    """Effect-shaped issue creator used to verify the boundary payload."""

    labels: tuple[str, ...] | None = None
    priority: str | None = None

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: Sequence[str] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        del title, description
        self.labels = tuple(labels) if labels is not None else None
        self.priority = priority
        return _tool_result({"issue_id": 701})


def _tool_result(value: dict[str, object]) -> ToolResult:
    raw = cast(JsonObject, {"success": True, "result": value})
    return ToolResult(
        raw=raw,
        success=True,
        result=cast(JsonValue, value),
        error=None,
    )
