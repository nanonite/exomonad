"""One slice's dispatch exhaustion parks that slice, not the whole run.

Before #1134 the first exhaustion took the run to ``tl_failed`` and returned
out of the dispatch loop, so a second slice in the same plan could never reach
its own exhaustion and could never open its own gate. The per-slice gate naming
from #1119 was therefore only reachable by driving ``_record_dispatch_failure``
directly, never through the real dispatch pass.

The policy is now: a slice that exhausts its dispatch retries is parked, its own
gate is opened, and the run *holds* behind that gate. Holding is not stopping --
the siblings the refusal says nothing about still dispatch in the same pass, and
each one that exhausts opens its own gate. An unrelated failure still stops the
pass immediately, so isolation is scoped to dispatch exhaustion and no wider.

Every test here is deterministic and goes through ``run_tl_loop``: the dispatch
clock is injected through ``TLLoopConfig.wall_clock`` and each refusal is handed
its machine code by a scripted ``agent.spawn_failed`` ledger row written at the
exact intent the controller minted. Nothing sleeps, polls, or reads wall time to
decide an outcome, and no test calls a private recorder to manufacture a park.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.dispatch_classification import OWNERSHIP_CONFLICT_CODE
from tl_loop.loop.driver import (
    DISPATCH_FAILURE_GATE_PREFIX,
    DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX,
    TLLoopConfig,
    WorkPlan,
    _dispatch_pass_stops,
    _held_by_dispatch_park,
    run_tl_loop,
)
from tl_loop.state.schema import GateStatus, ParkCause, RunState, SliceStatus
from tl_loop.state.store import RunStore
from tl_loop.tests.test_dispatch_retry import ScriptedClock, SyntheticQueue

JsonObject = dict[str, Any]

RUN_ID = "exhaustion-isolation-run"
#: A code that proves nothing was created, so it retries and then exhausts.
BRANCH_EXISTS = "worktree.branch_exists"
RETRY_LIMIT = 1
#: A refusal is terminal the moment it is observed, so the park and everything
#: that follows it have to land inside a single pass.
NO_RETRY_LIMIT = 0
BASE_DELAY = 10.0
MAX_DELAY = 40.0


@dataclass
class ScriptedRefusals:
    """Refuse every spawn for the named slices, accept the rest.

    Each refusal is answered by an ``agent.spawn_failed`` ledger row carrying
    the machine code for the *exact intent the controller minted* -- the row is
    written from the spawn call's own ``intent_id`` argument, so the controller
    reads precisely the typed channel it reads in production and the test never
    re-derives an identity it would have to keep in sync. Rows get strictly
    increasing run sequences so the correlation bound is unambiguous.
    """

    refused: frozenset[str]
    code: str
    project_root: Path
    run_id: str
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)
    events: list[JsonObject] = field(default_factory=list)
    spawn_calls: list[JsonObject] = field(default_factory=list)
    listed_agents: list[JsonObject] = field(default_factory=list)
    run_seq: int = 7

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name in {"spawn_worker", "spawn_leaf"}:
            self.spawn_calls.append(arguments)
            if str(arguments.get("name")) in self.refused:
                self._write_spawn_failed(arguments)
                return {"success": False, "error": "spawn request rejected"}
            return {"success": True, "result": {"agent_id": f"{arguments['name']}-opencode"}}
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": self.listed_agents}}
        if tool_name == "emit_controller_event":
            self.events.append(arguments)
            return {
                "success": True,
                "result": {"event_id": "controller-event", "run_seq": 100 + len(self.events)},
            }
        return {"success": True, "result": {}}

    def _write_spawn_failed(self, arguments: JsonObject) -> None:
        intent_id = arguments.get("intent_id")
        assert isinstance(intent_id, str) and intent_id, "a refused spawn must carry its intent"
        segments = self.project_root / ".exo" / "ledger" / "segments"
        segments.mkdir(parents=True, exist_ok=True)
        event = {
            "schema_version": 1,
            "event_id": f"spawn-failed-{self.run_seq}",
            "id": f"spawn-failed-{self.run_seq}",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": self.run_seq,
            "type": "agent.spawn_failed",
            "agent_id": "root",
            "parent_agent_id": "root",
            "run_id": self.run_id,
            "session_id": "session-1",
            "lifecycle_state": "observed",
            "data": {
                "child_agent": str(arguments.get("name")),
                "error": "spawn request rejected",
                "code": self.code,
                "intent_id": intent_id,
                "source": "rust",
            },
        }
        segment = segments / f"segment-{self.run_seq:012d}.jsonl"
        segment.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.run_seq += 1

    def event_types(self) -> list[str]:
        return [str(arguments.get("event_type")) for arguments in self.events]

    def payloads(self, event_type: str) -> list[JsonObject]:
        return [
            arguments["payload"]
            for arguments in self.events
            if arguments.get("event_type") == event_type
        ]

    def dispatched_names(self) -> list[str]:
        return [str(arguments.get("name")) for arguments in self.spawn_calls]


def _config(tmp_path: Path, clock: ScriptedClock, retry_limit: int = RETRY_LIMIT) -> TLLoopConfig:
    return TLLoopConfig(
        poll_interval=0.0,
        keep_alive_on_waiting=False,
        dispatch_retry_limit=retry_limit,
        dispatch_retry_base_delay_seconds=BASE_DELAY,
        dispatch_retry_max_delay_seconds=MAX_DELAY,
        wall_clock=clock,
        project_root=tmp_path,
        ledger_run_id=RUN_ID,
    )


def _invoke(
    tmp_path: Path,
    clock: ScriptedClock,
    transport: ScriptedRefusals,
    leaves: list[str],
    *,
    workers: Sequence[str] = (),
    retry_limit: int = RETRY_LIMIT,
) -> RunState:
    """Run one bounded controller pass over the named slices.

    ``workers`` dispatch before ``leaves``, which is what lets a test place a
    refused slice ahead of a sibling that must still be reached in the same
    pass.
    """
    plan = WorkPlan.from_mapping(
        {
            "workers": [{"name": name, "task": f"implement {name}"} for name in workers],
            "leaves": [{"name": name, "task": f"implement {name}"} for name in leaves],
        }
    )
    result = run_tl_loop(
        RUN_ID,
        plan,
        SyntheticQueue(),
        EffectClient(transport),
        config=_config(tmp_path, clock, retry_limit),
        root_dir=tmp_path,
    )
    return result.final_state


def _pass(
    tmp_path: Path,
    clock: ScriptedClock,
    transport: ScriptedRefusals,
    leaves: list[str],
) -> RunState:
    """Drive the whole plan to exhaustion: one pass per configured attempt.

    The retry boundary is a durable scheduled instant, so exhausting a slice is
    a sequence of bounded passes with the injected clock moved past the backoff
    between them -- exactly what a controller that survives and restarts does.
    """
    state = _invoke(tmp_path, clock, transport, leaves)
    for _ in range(RETRY_LIMIT + 1):
        clock.advance(MAX_DELAY)
        state = _invoke(tmp_path, clock, transport, leaves)
    return state


def test_two_slices_exhausting_in_one_run_open_two_distinct_gates(tmp_path: Path) -> None:
    """The core regression: two exhaustions, two gates, in one run.

    Both refused leaves spend their whole retry budget inside the same run, and
    both park with their own gate. Before #1134 the first exhaustion returned
    out of the dispatch loop, so the second leaf never dispatched again and the
    operator saw exactly one question for two refusals.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a", "leaf-b"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )

    state = _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])

    assert sorted(gate.name for gate in state.gates) == [
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a",
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b",
    ]
    assert all(gate.status is GateStatus.PENDING for gate in state.gates)
    # One announcement per exhaustion: the second refusal is never swallowed by
    # the first one's already-pending gate.
    assert [payload["gate_name"] for payload in transport.payloads("tl.gate_opened")] == [
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a",
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b",
    ]
    for slice_id in ("leaf-a", "leaf-b"):
        parked = state.slices[slice_id]
        assert parked.status is SliceStatus.DISPATCH_FAILED
        assert parked.park_cause is ParkCause.DISPATCH_FAILED
        assert parked.dispatch_error_code == BRANCH_EXISTS


def test_an_exhausted_slice_does_not_starve_its_siblings(tmp_path: Path) -> None:
    """A sibling that was never refused still dispatches in the *same* pass.

    This is the sibling half of the policy, and it is arranged so that the
    pre-#1134 fail-fast return is what the test would catch. ``worker-a`` is a
    worker, and workers dispatch before leaves, so a refusal that parks
    ``worker-a`` mid-pass sits directly in front of ``leaf-b``. With a retry
    limit of zero the refusal is terminal on its first observation, so the park
    and ``leaf-b``'s dispatch have to happen inside one pass: there is no later
    pass in which ``leaf-b`` could quietly recover.

    Before #1134 that first exhaustion returned out of the dispatch loop and
    ``leaf-b`` was never attempted at all.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"worker-a"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )

    state = _invoke(
        tmp_path,
        clock,
        transport,
        ["leaf-b"],
        workers=["worker-a"],
        retry_limit=NO_RETRY_LIMIT,
    )

    assert state.slices["worker-a"].status is SliceStatus.DISPATCH_FAILED
    assert [gate.name for gate in state.gates] == [
        f"{DISPATCH_FAILURE_GATE_PREFIX}worker-a"
    ]
    # The sibling was reached in this very pass, and the accepted request left
    # it waiting for its own authoritative confirmation rather than parked.
    assert transport.dispatched_names() == ["worker-a", "leaf-b"]
    assert state.slices["leaf-b"].status is SliceStatus.DISPATCH_UNCONFIRMED
    assert state.slices["leaf-b"].dispatch_error is None
    # Exactly one slice is parked: the refusal did not spread to the sibling.
    assert [gate.name for gate in state.gates].count(
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b"
    ) == 0


def test_a_held_run_reports_failed_while_its_gates_stay_pending(tmp_path: Path) -> None:
    """The hold is visible: the run is failed and the gates are unanswered.

    Isolation changes who is parked, not what the run is waiting for. The run
    still ends on the failed terminal phase with every gate pending, so a
    controller that sees this checkpoint knows it owes the operator an answer
    per refused slice rather than a single run-fatal verdict.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a", "leaf-b"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )

    state = _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])

    assert state.fsm.phase is TLPhase.TLFailed
    assert {gate.status for gate in state.gates} == {GateStatus.PENDING}
    # The durable checkpoint is the authority a restart reads, so the hold must
    # survive it rather than living only in this invocation.
    persisted = RunStore(RUN_ID, tmp_path).load()
    assert persisted.fsm.phase is TLPhase.TLFailed
    assert sorted(gate.name for gate in persisted.gates) == sorted(
        gate.name for gate in state.gates
    )


def test_a_held_run_reopens_neither_gate_on_a_later_pass(tmp_path: Path) -> None:
    """A restart of a held run is a bounded pass that re-announces nothing.

    Holding is durable, so re-entering the same run observes the same parked
    slices and the same pending questions. The controller must not re-open a
    gate that is already pending: that would reset the operator's question
    every time the controller restarts.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a", "leaf-b"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )
    held = _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])
    events_before = len(transport.events)
    spawns_before = len(transport.spawn_calls)

    resumed = _invoke(tmp_path, clock, transport, ["leaf-a", "leaf-b"])

    assert sorted(gate.name for gate in resumed.gates) == sorted(
        gate.name for gate in held.gates
    )
    assert all(gate.status is GateStatus.PENDING for gate in resumed.gates)
    assert "tl.gate_opened" not in transport.event_types()[events_before:]
    # Neither is the exhausted slice re-driven: both are parked, not candidates.
    assert len(transport.spawn_calls) == spawns_before
    assert [resumed.slices[name].status for name in ("leaf-a", "leaf-b")] == [
        SliceStatus.DISPATCH_FAILED,
        SliceStatus.DISPATCH_FAILED,
    ]


def test_answering_one_slice_gate_leaves_the_other_pending(tmp_path: Path) -> None:
    """A recorded answer is a decision, not a release.

    Answering leaf-a's gate is exactly one operator decision. It resolves that
    question and nothing else: leaf-b keeps its recorded refusal and its own
    pending gate, so two refusals can never collapse into one answered verdict.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a", "leaf-b"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )
    _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])
    store = RunStore(RUN_ID, tmp_path)
    answered = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    other = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b"

    store.answer_gate(answered, GateStatus.APPROVED)
    reloaded = store.load()

    assert {gate.name: gate.status for gate in reloaded.gates} == {
        answered: GateStatus.APPROVED,
        other: GateStatus.PENDING,
    }
    assert reloaded.slices["leaf-b"].status is SliceStatus.DISPATCH_FAILED


def test_a_terminal_conflict_isolates_its_slice_the_same_way(tmp_path: Path) -> None:
    """Isolation is a property of the per-slice park, not of the retry path.

    A terminal ownership conflict parks on the first refusal without spending
    any retry, yet it is the same one-slice-one-gate decision, so it isolates
    identically: the conflict slice opens the conflict-prefixed gate its own
    machine code names, and the sibling keeps dispatching.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a"}),
        code=OWNERSHIP_CONFLICT_CODE,
        project_root=tmp_path,
        run_id=RUN_ID,
    )

    state = _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])

    assert state.slices["leaf-a"].status is SliceStatus.DISPATCH_FAILED
    assert [gate.name for gate in state.gates] == [
        f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-a"
    ]
    assert state.slices["leaf-b"].status is SliceStatus.DISPATCH_UNCONFIRMED


def test_an_unrelated_failure_still_stops_the_dispatch_pass(tmp_path: Path) -> None:
    """Isolation is scoped to dispatch exhaustion, and no wider.

    The pass stops at a failure it cannot attribute to an operator question. A
    run failed with an open per-slice dispatch gate is held; the same failed
    phase with no such gate is stopped, so the controller never keeps issuing
    spawns against a failure nobody is going to answer.
    """
    clock = ScriptedClock()
    transport = ScriptedRefusals(
        refused=frozenset({"leaf-a"}),
        code=BRANCH_EXISTS,
        project_root=tmp_path,
        run_id=RUN_ID,
    )
    held = _pass(tmp_path, clock, transport, ["leaf-a", "leaf-b"])
    assert _held_by_dispatch_park(held)
    assert not _dispatch_pass_stops(held), "an open dispatch gate holds the pass"

    # The identical failed phase, with the operator's question withdrawn, is a
    # plain failure again. Attribution, not the phase value, is what the policy
    # turns on -- so a failure with no open dispatch gate stops the pass.
    store = RunStore(RUN_ID, tmp_path)
    gate = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    store.answer_gate(gate, GateStatus.APPROVED)

    stopped = store.load()

    assert stopped.fsm.phase is TLPhase.TLFailed
    assert not _held_by_dispatch_park(stopped)
    assert _dispatch_pass_stops(stopped)
