"""Ownership normalization and quarantine contract tests."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.events.envelope import project
from tl_loop.events.identity import envelope_document, resolve_event_slice
from tl_loop.fsm.phase import ChildHandle, TLWaiting
from tl_loop.loop.driver import LeafTask, TLLoopConfig, WorkPlan, run_tl_loop
from tl_loop.state.schema import BudgetLedger, SliceState, SliceStatus
from tl_loop.state.store import QuarantineStorageError, RunStore, create


def _event(
    event_type: str,
    *,
    agent_id: str,
    pr_number: int | None = 42,
    branch: str | None = "main.tunable-operator-body-opencode",
) -> object:
    data: dict[str, object] = {}
    if pr_number is not None:
        data["pr_number"] = pr_number
    if branch is not None:
        data["branch"] = branch
    return project(
        {
            "type": event_type,
            "run_seq": 7,
            "run_id": "swarm-uuid",
            "agent_id": agent_id,
            "lifecycle_state": "observed",
            "observed_at": "2026-08-18T00:00:00Z",
            "data": data,
        }
    )


def _state(*, duplicate_pr: bool = False) -> SimpleNamespace:
    first = SimpleNamespace(
        pr_number=42,
        dispatch_intent_id="intent-42",
        dispatch_agent_id="tunable-operator-body-opencode",
        branch="main.tunable-operator-body-opencode",
    )
    slices = {"tunable-operator-body": first}
    if duplicate_pr:
        slices["other"] = SimpleNamespace(
            pr_number=42,
            dispatch_intent_id="intent-other",
            dispatch_agent_id="other-agent",
            branch="main.other-agent",
        )
    return SimpleNamespace(slices=slices)


def test_branch_and_dispatch_agent_aliases_resolve_same_slice() -> None:
    event = _event(
        "ci.status_changed",
        agent_id="tunable-operator-body-opencode",
    )

    result = resolve_event_slice(event, _state())

    assert result.resolved
    assert result.slice_id == "tunable-operator-body"
    assert result.reason == "resolved"


def test_ambiguous_pr_is_rejected_without_first_match() -> None:
    event = _event("ci.status_changed", agent_id="unknown-owner", branch=None)

    result = resolve_event_slice(event, _state(duplicate_pr=True))

    assert not result.resolved
    assert result.slice_id is None
    assert result.reason == "ambiguous"
    assert result.candidates == ("other", "tunable-operator-body")


def test_quarantine_round_trip_preserves_observation_for_replay(tmp_path) -> None:
    create("root", {}, root_dir=tmp_path)
    store = RunStore("root", tmp_path)
    event = _event("ci.status_changed", agent_id="unknown-owner", branch=None)

    store.quarantine_event(envelope_document(event))

    entries = store.quarantined_events()
    assert len(entries) == 1
    assert entries[0]["run_seq"] == 7
    assert entries[0]["data"]["pr_number"] == 42

    store.release_quarantined_event(7)

    assert store.quarantined_events() == ()


def test_corrupt_quarantine_storage_is_visible_to_controller(tmp_path) -> None:
    create("root", {}, root_dir=tmp_path)
    store = RunStore("root", tmp_path)
    store.event_quarantine_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(QuarantineStorageError, match="invalid event quarantine"):
        store.quarantined_events()


class _ScriptedQueue:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.acknowledged: list[int] = []

    def get(self, timeout: float | None = None) -> object:
        del timeout
        if not self.events:
            raise queue.Empty
        return self.events.pop(0)

    def acknowledge(self, event: object) -> int:
        sequence = event.run_seq
        self.acknowledged.append(sequence)
        return sequence


class _NoopTransport:
    def call_tool(self, role, name, tool_name, arguments):
        del role, name, tool_name, arguments
        return {"success": True, "result": {}}


def _routing_event(
    sequence: int,
    event_type: str,
    *,
    agent_id: str,
    data: dict[str, object],
) -> object:
    return project(
        {
            "type": event_type,
            "run_seq": sequence,
            "run_id": "routing-run",
            "agent_id": agent_id,
            "lifecycle_state": "observed",
            "observed_at": "2026-08-18T00:00:00Z",
            "data": data,
        }
    )


def test_ci_before_pr_is_quarantined_then_replayed_by_persisted_pr(tmp_path) -> None:
    slice_state = SliceState(
        id="tunable-operator-body",
        status=SliceStatus.IN_REVIEW,
        paths=("src/tunable.py",),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type="opencode",
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
    )
    create(
        "routing-run",
        {
            "slices": {
                slice_state.id: {
                    "id": slice_state.id,
                    "status": slice_state.status.value,
                    "paths": list(slice_state.paths),
                    "depends_on": [],
                    "base_ref": slice_state.base_ref,
                    "test_plan": [],
                    "agent_type": slice_state.agent_type,
                    "model": None,
                    "branch": None,
                    "worktree": None,
                    "pr_number": None,
                    "review_findings": {},
                    "ci_state": {},
                    "reviewer_attempt": {},
                    "repair_attempts": 0,
                    "reviewed_head": None,
                    "attempts": 1,
                    "verdict": None,
                }
            }
        },
        root_dir=tmp_path,
    )
    RunStore("routing-run", tmp_path).checkpoint(
        TLWaiting(
            {
                slice_state.id: ChildHandle(
                    slice_state.id,
                    "main.tunable-operator-body-opencode",
                    "opencode",
                )
            }
        ),
        {slice_state.id: slice_state},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    branch = "main.tunable-operator-body-opencode"
    source = _ScriptedQueue(
        [
            _routing_event(
                1,
                "ci.status_changed",
                agent_id=branch,
                data={
                    "pr_number": 42,
                    "head_sha": "head-42",
                    "status": "pending",
                    "branch": branch,
                },
            ),
            _routing_event(
                2,
                "pr.filed",
                agent_id="tunable-operator-body",
                data={
                    "slice_id": "tunable-operator-body",
                    "pr_number": 42,
                    "head_sha": "head-42",
                    "branch": branch,
                },
            ),
            _routing_event(
                3,
                "ci.status_changed",
                agent_id=branch,
                data={
                    "pr_number": 42,
                    "head_sha": "head-42",
                    "status": "success",
                    "branch": branch,
                },
            ),
            _routing_event(
                4,
                "agent.notify_parent",
                agent_id="tunable-operator-body",
                data={"shadow_event": {"kind": "all_children_done"}},
            ),
        ]
    )

    result = run_tl_loop(
        "routing-run",
        WorkPlan(leaves=(LeafTask("tunable-operator-body", "route review and CI"),)),
        source,
        ReadOnlyEffectClient(_NoopTransport()),
        config=TLLoopConfig(
            active=False,
            keep_alive_on_waiting=True,
            max_events=8,
            poll_interval=0.001,
            idle_timeout=0.1,
            root_dir=tmp_path,
        ),
        root_dir=tmp_path,
    )

    final = result.final_state.slices["tunable-operator-body"]
    assert result.final_state.fsm.phase.value == "tl_done"
    assert final.pr_number == 42
    assert final.ci_state == {"head-42": "success"}
    assert source.acknowledged == [1, 2, 3, 4]
    assert len(result.diagnostics["unresolved_events"]) == 1
    assert RunStore("routing-run", tmp_path).quarantined_events() == ()
