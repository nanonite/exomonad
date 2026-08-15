"""Operator read-model projection and body-leakage coverage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tl_loop.events.envelope import project
from tl_loop.events.reader import SequenceStatus
from tl_loop.fsm.phase import TLPhase
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.read_model import GateReadModel, project_read_model
from tl_loop.state.schema import (
    BudgetCharge,
    BudgetLedger,
    EventCursor,
    FSMState,
    GateState,
    GateStatus,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_projection_events.json"


def test_projection_carries_cursor_and_bounded_operator_evidence() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    model = project_read_model(
        state,
        events,
        sequence_status=SequenceStatus.COMPLETE,
        recent_transition_limit=3,
    )
    document = model.to_document()
    head = next(head for head in model.slices["task-a"].heads if head.head_sha == "bbb222")

    assert model.ledger_cursor == 112
    assert model.ledger_sequence_status == "complete"
    assert [transition.run_seq for transition in model.recent_transitions] == [110, 111, 112]
    assert head.head_sha == "bbb222"
    assert head.review_kind == "merge_ready"
    assert head.review_state == "changes_requested"
    assert head.review_verdict == "GO"
    assert head.review_finding_count == 1
    assert head.ci_status == "success"
    assert head.reviewer_attempt == 2
    assert model.park_causes == {"task-a": "review_stuck"}
    assert model.gates == (GateReadModel("review", "pending"),)
    budgets = cast(dict[str, object], document["budgets"])
    assert budgets["tokens"] == 321


def test_projection_does_not_leak_event_or_finding_bodies() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    encoded = json.dumps(project_read_model(state, events).to_document(), sort_keys=True)

    assert "[MERGE READY]" not in encoded
    assert "tests failed" not in encoded
    assert "private agent body" not in encoded
    assert "rationale" not in encoded


def test_projection_ignores_events_after_state_cursor() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    model = project_read_model(state, events, recent_transition_limit=100)

    assert model.recent_transitions
    assert max(transition.run_seq for transition in model.recent_transitions) == 112


def test_projection_exposes_ordered_progress_and_next_transition() -> None:
    state = _state()
    state = RunState(
        **{
            **state.__dict__,
            "current_order": 1,
            "ordered_stages": (OrderedStageState(1, ("task-a",)),),
            "integration": IntegrationRuntimeState(
                lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
                sub_tl_states={"task-a": IntegrationLifecycle.READY_FOR_INTEGRATION},
                aggregate_pr_number=101,
                aggregate_head_sha="bbb222",
                integration_owner_id="aggregate-owner",
                head_sha="bbb222",
                validated_base_sha="base-1",
                merge_tree_sha="tree-1",
                ci_status="success",
                merge_attempts=3,
                base_revalidation_count=2,
                stage_verification="passed",
            ),
        }
    )

    document = project_read_model(state).to_document()

    assert document["schema_version"] == 2
    assert document["current_order"] == 1
    stage = cast(list[dict[str, object]], document["ordered_stages"])[0]
    sub_tl = cast(list[dict[str, object]], stage["sub_tls"])[0]
    assert sub_tl["lifecycle"] == "READY_FOR_INTEGRATION"
    assert sub_tl["aggregate_pr_number"] == 101
    assert sub_tl["validated_base_sha"] == "base-1"
    assert sub_tl["integration_ci"] == "success"
    assert sub_tl["repair_count"] == 1
    integration = cast(dict[str, object], document["integration"])
    assert integration["merge_tree_sha"] == "tree-1"
    assert integration["base_revalidation_count"] == 2
    assert document["next_transition"] == "answer_gate:review"


def _state() -> RunState:
    return RunState(
        version=1,
        revision=4,
        run_id="run-1",
        fsm=FSMState(TLPhase.TLWaiting, ("task-a",)),
        slices={
            "task-a": SliceState(
                id="task-a",
                status=SliceStatus.PARKED,
                paths=("src/a.py",),
                depends_on=(),
                base_ref="main",
                test_plan=("just test",),
                agent_type="codex",
                model="gpt-5",
                branch="task-a",
                worktree=".worktrees/task-a",
                pr_number=101,
                reviewed_head="bbb222",
                attempts=2,
                verdict=Verdict.GO,
                review_findings={
                    "bbb222": (
                        {
                            "severity": "blocking",
                            "path": "src/a.py",
                            "rationale": "private agent body",
                        },
                    )
                },
                ci_state={"bbb222": "success"},
                reviewer_attempt={"bbb222": 2},
                repair_attempts=1,
                park_cause=ParkCause.REVIEW_STUCK,
                park_issue_id=404,
            )
        },
        budgets=BudgetLedger(
            tokens=321,
            wall_seconds=45,
            role_spent={"worker": 321},
            harness_spent={"codex": 321},
            charges=(
                BudgetCharge(
                    slice_id="task-a",
                    attempt=1,
                    role="worker",
                    harness="codex",
                    estimated_tokens=300,
                    actual=321,
                    delta_tokens=21,
                    warning=False,
                    reconciled=True,
                ),
            ),
        ),
        gates=(GateState("review", GateStatus.PENDING),),
        events=EventCursor(last_consumed_offset=112),
    )
