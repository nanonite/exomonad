"""One dispatch-exhaustion gate per slice, never one gate per run.

Before #1119 the exhaustion gate was run-global, so a run's second parked slice
reused the first one's gate and emitted no ``tl.gate_opened``: the operator was
never told. The gate is now named from the slice that exhausted, so each
exhaustion is one pending gate and one event, and answering one slice's gate
leaves every other slice's question standing.

Every test here is deterministic. The dispatch clock is injected through
``TLLoopConfig.wall_clock`` and each refusal is handed its machine code directly,
so nothing sleeps, polls, or reads wall time to decide an outcome. A restart is a
second bounded ``run_tl_loop`` invocation over the same checkpoint, which is
exactly what a resumed controller does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.dispatch_classification import OWNERSHIP_CONFLICT_CODE
from tl_loop.loop.driver import (
    DISPATCH_FAILURE_GATE_PREFIX,
    DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX,
    LEGACY_DISPATCH_FAILURE_GATE_NAME,
    LEGACY_DISPATCH_OWNERSHIP_CONFLICT_GATE_NAME,
    DispatchAttempt,
    TLLoopConfig,
    TLLoopError,
    _dispatch_failure_gate_name,
    _record_dispatch_failure,
    run_tl_loop,
)
from tl_loop.state.schema import (
    BudgetLedger,
    GateStatus,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_dispatch_retry import (
    ScriptedClock,
    ScriptedTransport,
    SyntheticQueue,
)

RUN_ID = "per-slice-gate-run"
#: A code that proves nothing was created, so it retries and then exhausts.
BRANCH_EXISTS = "worktree.branch_exists"
RETRY_LIMIT = 1


def _config(tmp_path: Path) -> TLLoopConfig:
    return TLLoopConfig(
        poll_interval=0.0,
        keep_alive_on_waiting=False,
        dispatch_retry_limit=RETRY_LIMIT,
        dispatch_retry_base_delay_seconds=10.0,
        dispatch_retry_max_delay_seconds=40.0,
        wall_clock=ScriptedClock(),
        project_root=tmp_path,
        ledger_run_id=RUN_ID,
    )


def _transport(tmp_path: Path) -> ScriptedTransport:
    return ScriptedTransport(
        code=BRANCH_EXISTS,
        rejections=0,
        project_root=tmp_path,
        run_id=RUN_ID,
    )


def _dispatching_slice(slice_id: str, path: str) -> SliceState:
    return SliceState(
        id=slice_id,
        status=SliceStatus.DISPATCHING,
        paths=(path,),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        verdict=None,
        attempts=1,
        dispatch_intent_id=f"intent-{slice_id}-1",
        dispatch_started_at=1_000.0,
    )


def _parked_slice(
    slice_id: str,
    code: str | None,
    *,
    status: SliceStatus = SliceStatus.DISPATCH_FAILED,
    park_cause: ParkCause = ParkCause.DISPATCH_FAILED,
) -> SliceState:
    return SliceState(
        id=slice_id,
        status=status,
        paths=(f"src/{slice_id}.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        verdict=None,
        attempts=2,
        park_cause=park_cause,
        dispatch_last_boundary="spawn_request_failed",
        dispatch_error="spawn request rejected",
        dispatch_error_code=code,
    )


def _store_for(tmp_path: Path) -> RunStore:
    create(RUN_ID, {}, root_dir=tmp_path)
    return RunStore(RUN_ID, tmp_path)


def _exhaust(
    store: RunStore,
    state: RunState,
    slice_id: str,
    config: TLLoopConfig,
    effects: EffectClient,
    code: str,
) -> RunState:
    """Spend one slice's whole retry budget through the real failure path.

    The first rejection schedules the one configured retry; the next one exhausts
    the budget and parks the slice.
    """
    for attempt in range(1, RETRY_LIMIT + 2):
        state = _record_dispatch_failure(
            store,
            state,
            slice_id,
            DispatchAttempt(
                f"intent-{slice_id}-{attempt}",
                1_000.0,
                "",
                attempt=attempt,
            ),
            "spawn request rejected",
            config,
            effects,
            [],
            code=code,
        )
    return state


def _exhaust_both(
    tmp_path: Path, code: str = BRANCH_EXISTS
) -> tuple[RunStore, RunState, ScriptedTransport]:
    """Park two slices, each through its own exhausted budget."""
    store = _store_for(tmp_path)
    state = store.checkpoint(
        TLPhase.TLDispatching,
        {
            "leaf-a": _dispatching_slice("leaf-a", "src/a.py"),
            "leaf-b": _dispatching_slice("leaf-b", "src/b.py"),
        },
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    config = _config(tmp_path)
    transport = _transport(tmp_path)
    effects = EffectClient(transport)
    state = _exhaust(store, state, "leaf-a", config, effects, code)
    state = _exhaust(store, state, "leaf-b", config, effects, code)
    return store, state, transport


def _restart(tmp_path: Path, transport: ScriptedTransport) -> RunState:
    """Resume the parked run exactly as a restarted controller would."""
    result = run_tl_loop(
        RUN_ID,
        None,
        SyntheticQueue(),
        EffectClient(transport),
        config=_config(tmp_path),
        root_dir=tmp_path,
    )
    return result.final_state


def test_two_slices_exhausting_retries_open_two_distinct_gates(tmp_path: Path) -> None:
    _store, state, transport = _exhaust_both(tmp_path)

    assert sorted(gate.name for gate in state.gates) == [
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a",
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b",
    ]
    assert all(gate.status is GateStatus.PENDING for gate in state.gates)
    # One opening per exhaustion: each gate is announced exactly once, and the
    # second slice is not swallowed by the first one's gate.
    opened = transport.payloads("tl.gate_opened")
    assert [payload["gate_name"] for payload in opened] == [
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a",
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b",
    ]
    for slice_id in ("leaf-a", "leaf-b"):
        parked = state.slices[slice_id]
        assert parked.status is SliceStatus.DISPATCH_FAILED
        assert parked.park_cause is ParkCause.DISPATCH_FAILED


def test_a_slice_id_containing_a_slash_still_opens_one_answerable_gate(
    tmp_path: Path,
) -> None:
    """A per-slice gate name stays answerable when the slice id has a ``/``.

    The gate name embeds the slice id, so a slice id holding a path separator
    produces a gate name the HTTP control route cannot write as one path level.
    The name itself is the operator's handle on the decision, so the gate must
    still open, be announced, and be answerable -- the control route reaches it
    through the canonical percent-encoding of the same name.
    """
    slice_id = "feat/auth"
    store = _store_for(tmp_path)
    state = store.checkpoint(
        TLPhase.TLDispatching,
        {slice_id: _dispatching_slice(slice_id, "src/auth.py")},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    config = _config(tmp_path)
    transport = _transport(tmp_path)

    state = _exhaust(store, state, slice_id, config, EffectClient(transport), BRANCH_EXISTS)

    gate = f"{DISPATCH_FAILURE_GATE_PREFIX}{slice_id}"
    assert [(entry.name, entry.status) for entry in state.gates] == [(gate, GateStatus.PENDING)]
    assert [payload["gate_name"] for payload in transport.payloads("tl.gate_opened")] == [gate]

    store.answer_gate(gate, GateStatus.APPROVED)

    assert [(entry.name, entry.status) for entry in store.load().gates] == [
        (gate, GateStatus.APPROVED)
    ]


def test_answering_one_slices_gate_does_not_resolve_the_other(tmp_path: Path) -> None:
    store, state, _transport = _exhaust_both(tmp_path)
    answered = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    other = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-b"
    version_before = state.state_version

    store.answer_gate(answered, GateStatus.APPROVED)
    reloaded = store.load()

    assert {gate.name: gate.status for gate in reloaded.gates} == {
        answered: GateStatus.APPROVED,
        other: GateStatus.PENDING,
    }
    # The answer is a recorded decision, not a release: the other slice keeps
    # its recorded dispatch failure and its own pending question, and answering
    # one gate re-drives nothing by itself.
    assert reloaded.slices["leaf-b"].status is SliceStatus.DISPATCH_FAILED
    assert reloaded.slices["leaf-a"].status is SliceStatus.DISPATCH_FAILED
    # A changed gate status advances the convergence epoch, so a live controller
    # re-evaluates the released action instead of treating it as a repeat.
    assert reloaded.state_version == version_before + 1


def test_a_restart_reopens_neither_gate(tmp_path: Path) -> None:
    _store, state, transport = _exhaust_both(tmp_path)
    names = sorted(gate.name for gate in state.gates)
    events_before = len(transport.events)

    resumed = _restart(tmp_path, transport)

    assert sorted(gate.name for gate in resumed.gates) == names
    assert all(gate.status is GateStatus.PENDING for gate in resumed.gates)
    assert [slice_state.status for slice_state in resumed.slices.values()] == [
        SliceStatus.DISPATCH_FAILED,
        SliceStatus.DISPATCH_FAILED,
    ]
    # A restart is a bounded pass over a parked run: it re-announces nothing.
    assert "tl.gate_opened" not in transport.event_types()[events_before:]


def test_two_ownership_conflicts_open_one_gate_each(tmp_path: Path) -> None:
    """The conflict gate is scoped per slice too.

    Two slices can own different birth branches, so a conflict on one branch is
    not a decision about the other: a run-global conflict gate would also let the
    second conflict go unannounced.
    """
    _store, state, transport = _exhaust_both(tmp_path, code=OWNERSHIP_CONFLICT_CODE)

    assert sorted(gate.name for gate in state.gates) == [
        f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-a",
        f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-b",
    ]
    assert [payload["gate_name"] for payload in transport.payloads("tl.gate_opened")] == [
        f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-a",
        f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-b",
    ]


def test_the_gate_name_is_derived_from_the_parked_slices_own_code() -> None:
    assert (
        _dispatch_failure_gate_name("leaf-a", OWNERSHIP_CONFLICT_CODE)
        == f"{DISPATCH_OWNERSHIP_CONFLICT_GATE_PREFIX}leaf-a"
    )
    assert _dispatch_failure_gate_name("leaf-a", BRANCH_EXISTS) == (
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    )
    # An absent code is terminal too, so it names the refusal gate, not a
    # conflict gate, and the slice id is the whole scope either way.
    assert _dispatch_failure_gate_name("leaf-a", None) == (
        f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    )
    assert _dispatch_failure_gate_name("leaf-a", BRANCH_EXISTS) != (
        _dispatch_failure_gate_name("leaf-b", BRANCH_EXISTS)
    )


@pytest.mark.parametrize(
    ("legacy_name", "code"),
    [
        (LEGACY_DISPATCH_FAILURE_GATE_NAME, BRANCH_EXISTS),
        (LEGACY_DISPATCH_OWNERSHIP_CONFLICT_GATE_NAME, OWNERSHIP_CONFLICT_CODE),
    ],
)
def test_a_legacy_run_global_gate_is_migrated_onto_its_parked_slice(
    tmp_path: Path, legacy_name: str, code: str
) -> None:
    """A checkpoint written before per-slice naming keeps its pending decision.

    The migration is deterministic: the slice parked on ``DISPATCH_FAILED`` names
    the exhaustion the run-global gate was opened for, so the pending gate is
    renamed to that slice's name and stays pending. No ``tl.gate_opened`` is
    re-emitted -- that exhaustion already announced itself under the old name.
    """
    store = _store_for(tmp_path)
    store.checkpoint(
        TLPhase.TLFailed,
        {"leaf-a": _parked_slice("leaf-a", code)},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_gate(legacy_name)
    transport = _transport(tmp_path)
    version_before = store.load().state_version

    resumed = _restart(tmp_path, transport)

    assert [(gate.name, gate.status) for gate in resumed.gates] == [
        (f"{_dispatch_failure_gate_name('leaf-a', code)}", GateStatus.PENDING)
    ]
    assert "tl.gate_opened" not in transport.event_types()
    # The rename is a durable semantic transition, so the convergence epoch
    # advances exactly once, as answering a gate does.
    assert resumed.state_version == version_before + 1


def test_an_answered_legacy_gate_is_left_as_the_recorded_decision(tmp_path: Path) -> None:
    """A resolved run-global gate is history, not a pending question to migrate."""
    store = _store_for(tmp_path)
    store.checkpoint(
        TLPhase.TLFailed,
        {"leaf-a": _parked_slice("leaf-a", BRANCH_EXISTS)},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME)
    store.answer_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME, GateStatus.APPROVED)
    transport = _transport(tmp_path)

    resumed = _restart(tmp_path, transport)

    assert [(gate.name, gate.status) for gate in resumed.gates] == [
        (LEGACY_DISPATCH_FAILURE_GATE_NAME, GateStatus.APPROVED)
    ]


def test_a_legacy_gate_without_one_parked_slice_fails_closed(tmp_path: Path) -> None:
    """An unattributable run-global gate is refused, never renamed by guesswork."""
    store = _store_for(tmp_path)
    store.checkpoint(
        TLPhase.TLFailed,
        {
            "leaf-a": _parked_slice("leaf-a", BRANCH_EXISTS),
            "leaf-b": _parked_slice("leaf-b", BRANCH_EXISTS),
        },
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME)
    transport = _transport(tmp_path)

    with pytest.raises(TLLoopError) as failure:
        _restart(tmp_path, transport)

    assert "cannot be attributed to exactly one parked dispatch slice" in str(failure.value)
    assert "leaf-a, leaf-b" in str(failure.value)
    # Fail closed means unchanged: the operator's pending gate is still there,
    # still answerable, with the command to retire it in the message.
    assert LEGACY_DISPATCH_FAILURE_GATE_NAME in str(failure.value)
    assert [(gate.name, gate.status) for gate in store.load().gates] == [
        (LEGACY_DISPATCH_FAILURE_GATE_NAME, GateStatus.PENDING)
    ]


def test_a_legacy_gate_with_no_parked_slice_fails_closed(tmp_path: Path) -> None:
    store = _store_for(tmp_path)
    # The dispatch park that opened the gate is gone, so no slice answers for it.
    store.checkpoint(
        TLPhase.TLFailed,
        {
            "leaf-a": _parked_slice(
                "leaf-a",
                None,
                status=SliceStatus.FAILED,
                park_cause=ParkCause.RETRIES_EXHAUSTED,
            )
        },
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME)
    transport = _transport(tmp_path)

    with pytest.raises(TLLoopError, match="candidates: none"):
        _restart(tmp_path, transport)


def test_a_legacy_gate_contradicting_the_recorded_code_fails_closed(tmp_path: Path) -> None:
    """The parked slice's machine code decides the name, or the run refuses."""
    store = _store_for(tmp_path)
    store.checkpoint(
        TLPhase.TLFailed,
        {"leaf-a": _parked_slice("leaf-a", OWNERSHIP_CONFLICT_CODE)},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME)
    transport = _transport(tmp_path)

    with pytest.raises(TLLoopError, match="contradicts the machine code"):
        _restart(tmp_path, transport)

    assert [(gate.name, gate.status) for gate in store.load().gates] == [
        (LEGACY_DISPATCH_FAILURE_GATE_NAME, GateStatus.PENDING)
    ]


def test_a_legacy_gate_already_scoped_per_slice_fails_closed(tmp_path: Path) -> None:
    """Two pending gates for one slice are two questions, so neither is merged."""
    store = _store_for(tmp_path)
    store.checkpoint(
        TLPhase.TLFailed,
        {"leaf-a": _parked_slice("leaf-a", BRANCH_EXISTS)},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    scoped = f"{DISPATCH_FAILURE_GATE_PREFIX}leaf-a"
    store.set_gate(LEGACY_DISPATCH_FAILURE_GATE_NAME)
    store.set_gate(scoped)
    transport = _transport(tmp_path)

    with pytest.raises(TLLoopError, match="is already pending for the parked slice"):
        _restart(tmp_path, transport)

    assert sorted(gate.name for gate in store.load().gates) == sorted(
        (LEGACY_DISPATCH_FAILURE_GATE_NAME, scoped)
    )
