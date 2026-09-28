"""One dispatch attempt, one provenance, on every path that records it (#1120).

A dispatch attempt is minted once by ``_new_dispatch_attempt`` with the run's
controller epoch and its own attempt generation. Exactly two things may change
it afterwards: the policy path attaches the selected harness route, and a
reconstruction reads the attempt back from the slice's own dispatch provenance.
Both are single methods, so a newly added ``DispatchAttempt`` field cannot be
dropped without one of these tests failing.

The dispatch pass here is ``_dispatch_children``, the real one the loop calls:
it emits both durable boundaries, calls the spawn effect, and persists the
result. Nothing here sleeps or reads wall time to decide an outcome.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import pytest

import tl_loop
from tl_loop.client.effects import EffectClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import (
    DispatchAttempt,
    TLLoopConfig,
    TLLoopError,
    WorkPlan,
    _dispatch_children,
)
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import load_policy
from tl_loop.state.schema import BudgetLedger, SliceState, SliceStatus
from tl_loop.state.store import RunStore, create

PACKAGE_ROOT = Path(tl_loop.__file__).parent
POLICY = Path(__file__).parent / "fixtures" / "selector_policy_cheap_only.toml"
SLICE = "leaf-a"
RUN_ID = "dispatch-provenance"
CONTROLLER_EPOCH = "epoch-of-the-dispatch-provenance-run"
HARNESS = "codex/gpt-luna"
AGENT_TYPE = "codex"
MODEL = "gpt-luna"
#: The identity both spawn paths must record, whichever one minted the attempt.
IDENTITY_FIELDS = ("intent_id", "attempt", "controller_epoch", "dispatch_generation")
#: The dispatch boundary each path persists on the slice. The recorded instant
#: is a per-run wall clock, so it is asserted present rather than compared.
PERSISTED_FIELDS = (
    "dispatch_intent_id",
    "dispatch_last_boundary",
    "dispatch_ledger_floor",
    "dispatch_generation",
    "dispatch_error",
    "dispatch_error_code",
    "dispatch_agent_id",
    "dispatch_invocation_id",
    "attempts",
)
#: The boundary events a dispatch pass emits from the one attempt.
BOUNDARY_EVENTS = ("tl.dispatch_intended", "tl.spawn_requested", "tl.spawn_request_accepted")
#: The only source sites allowed to construct an attempt. A reconstruction goes
#: through ``DispatchAttempt.recorded_for``; a fresh identity is minted here.
AUDITED_CONSTRUCTIONS = frozenset({"_new_dispatch_attempt", "_prepare_sub_tl_stage"})


@dataclass
class RecordingTransport:
    """Accept the spawn and keep every emitted controller event."""

    events: list[dict[str, Any]] = field(default_factory=list)
    spawns: list[dict[str, Any]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        del role, name
        if tool_name in {"spawn_leaf", "spawn_worker"}:
            self.spawns.append(dict(arguments))
            return {
                "success": True,
                "result": {"agent_id": f"{SLICE}-opencode", "invocation_id": "inv-1"},
            }
        if tool_name == "emit_controller_event":
            self.events.append(dict(arguments))
            return {"success": True, "result": {"event_id": "controller-event"}}
        return {"success": True, "result": {}}


@dataclass(frozen=True)
class DispatchPass:
    """What one dispatch pass recorded, and what it persisted."""

    payloads: dict[str, dict[str, Any]]
    slice_state: SliceState
    controller_epoch: str | None
    spawn: dict[str, Any]


def _dispatch(root: Path, *, policy: bool) -> DispatchPass:
    store = _store(root)
    config = TLLoopConfig(
        policy=load_policy(POLICY) if policy else None,
        capabilities=CapabilityMap({HARNESS: Difficulty.STANDARD}),
        root_dir=root,
        run_id=RUN_ID,
    )
    transport = RecordingTransport()
    state = _dispatch_children(
        _plan(),
        store.load(),
        config,
        EffectClient(transport),
        [],
        store,
    )
    payloads = {
        str(event["event_type"]): dict(event["payload"])
        for event in transport.events
        if str(event.get("event_type")) in BOUNDARY_EVENTS
    }
    return DispatchPass(
        payloads=payloads,
        slice_state=state.slices[SLICE],
        controller_epoch=state.controller_epoch,
        spawn=transport.spawns[0],
    )


def _plan() -> WorkPlan:
    return WorkPlan.from_mapping({"leaves": [{"name": SLICE, "task": "implement"}]})


def _store(root: Path) -> RunStore:
    root.mkdir(parents=True, exist_ok=True)
    create(RUN_ID, {}, root_dir=root)
    store = RunStore(RUN_ID, root)
    store.checkpoint(
        TLPhase.TLDispatching,
        {SLICE: _pending_slice()},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    store.set_controller_epoch(CONTROLLER_EPOCH)
    return store


def _pending_slice() -> SliceState:
    return SliceState(
        id=SLICE,
        status=SliceStatus.PENDING,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        verdict=None,
        attempts=0,
    )


def _both_paths(tmp_path: Path) -> tuple[DispatchPass, DispatchPass]:
    return _dispatch(tmp_path / "direct", policy=False), _dispatch(
        tmp_path / "policy", policy=True
    )


def _identity(pass_result: DispatchPass, event_type: str) -> dict[str, object]:
    return {
        field_name: pass_result.payloads[event_type][field_name]
        for field_name in IDENTITY_FIELDS
    }


def _persisted(pass_result: DispatchPass) -> dict[str, object]:
    return {
        field_name: getattr(pass_result.slice_state, field_name)
        for field_name in PERSISTED_FIELDS
    }


@pytest.mark.parametrize("event_type", BOUNDARY_EVENTS)
def test_every_spawn_path_records_the_same_dispatch_identity(
    event_type: str, tmp_path: Path
) -> None:
    """A policy-path dispatch records exactly the identity a direct one mints."""
    direct, policy = _both_paths(tmp_path)

    assert _identity(policy, event_type) == _identity(direct, event_type)
    assert _identity(direct, event_type) == {
        "intent_id": direct.slice_state.dispatch_intent_id,
        "attempt": 1,
        "controller_epoch": CONTROLLER_EPOCH,
        "dispatch_generation": 1,
    }

    for pass_result in (direct, policy):
        # Every boundary of the pass is the same attempt, not three of them.
        for boundary in BOUNDARY_EVENTS:
            assert pass_result.payloads[boundary]["intent_id"] == (
                pass_result.slice_state.dispatch_intent_id
            )
        assert pass_result.spawn["intent_id"] == pass_result.slice_state.dispatch_intent_id


def test_every_spawn_path_persists_the_same_dispatch_boundary(tmp_path: Path) -> None:
    """The durable boundary belongs to the attempt, not to the path that took it."""
    direct, policy = _both_paths(tmp_path)

    assert _persisted(policy) == _persisted(direct)

    for pass_result in (direct, policy):
        boundary = pass_result.slice_state
        assert boundary.status is SliceStatus.DISPATCH_UNCONFIRMED
        assert boundary.attempts == 1
        assert boundary.dispatch_intent_id is not None
        assert boundary.dispatch_started_at is not None
        assert boundary.dispatch_last_boundary == "spawn_request_accepted"
        assert boundary.dispatch_error is None
        assert boundary.dispatch_error_code is None
        assert boundary.dispatch_agent_id == f"{SLICE}-opencode"
        assert boundary.dispatch_invocation_id == "inv-1"
    # Only the policy path selects a route, and it records it on the slice.
    assert policy.slice_state.agent_type == AGENT_TYPE
    assert policy.slice_state.model == MODEL
    assert policy.payloads["tl.dispatch_intended"]["harness"] == HARNESS
    assert direct.slice_state.agent_type is None
    assert direct.payloads["tl.dispatch_intended"].get("harness") is None


def test_a_rebuilt_attempt_reads_every_field_from_its_slice() -> None:
    """A reconstruction reports the attempt the slice's boundary records."""
    # A slice records the resolved agent type, not the qualified harness
    # identifier the policy selection chose, so the recorded attempt's routing
    # dimensions are that one value.
    recorded = DispatchAttempt(
        intent_id="intent-2",
        started_at=1_000.0,
        harness=AGENT_TYPE,
        agent_type=AGENT_TYPE,
        model=MODEL,
        attempt=2,
        controller_epoch=CONTROLLER_EPOCH,
        dispatch_generation=2,
        ledger_floor=41,
    )

    rebuilt = DispatchAttempt.recorded_for(
        _recorded_slice(recorded), recorded.controller_epoch
    )

    assert {f.name: getattr(rebuilt, f.name) for f in fields(DispatchAttempt)} == {
        f.name: getattr(recorded, f.name) for f in fields(DispatchAttempt)
    }


def test_the_rebuild_constructor_propagates_every_dispatch_field() -> None:
    """A field added to the attempt must be propagated by its one rebuild.

    This is the guard. The reconstruction assigns every field either from the
    slice's own dispatch provenance or from the epoch its caller passes, so a
    newly added field that no rebuild reads fails here instead of silently
    reaching a boundary event as its default.
    """
    source = textwrap.dedent(inspect.getsource(DispatchAttempt.recorded_for))
    assigned = _keyword_arguments(ast.parse(source))
    supplied = set(inspect.signature(DispatchAttempt.recorded_for).parameters) - {
        "slice_state"
    }

    assert assigned | supplied == {f.name for f in fields(DispatchAttempt)}


def test_routing_an_attempt_preserves_every_identity_field() -> None:
    """Selecting a harness changes the route and nothing else."""
    recorded = DispatchAttempt(
        intent_id="intent-3",
        started_at=1_000.0,
        harness="",
        attempt=4,
        controller_epoch=CONTROLLER_EPOCH,
        dispatch_generation=4,
        ledger_floor=52,
    )

    routed = recorded.routed(harness=HARNESS, agent_type=AGENT_TYPE, model=MODEL)

    changed = {
        f.name
        for f in fields(DispatchAttempt)
        if getattr(routed, f.name) != getattr(recorded, f.name)
    }
    assert changed == {"harness", "agent_type", "model"}


def test_a_slice_without_a_recorded_intent_cannot_be_rebuilt() -> None:
    """A slice with no intent has no attempt, and reconstruction fails closed."""
    with pytest.raises(TLLoopError, match="records no dispatch intent"):
        DispatchAttempt.recorded_for(_pending_slice(), CONTROLLER_EPOCH)


def test_every_dispatch_attempt_construction_is_audited() -> None:
    """Only the two audited identities may be constructed by hand.

    Every other attempt is produced by ``routed`` or ``recorded_for``, so a new
    hand-copied reconstruction fails here instead of dropping a field.
    """
    found: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "tests" in path.relative_to(PACKAGE_ROOT).parts:
            continue
        found |= _constructing_functions(ast.parse(path.read_text(encoding="utf-8")))

    assert found == set(AUDITED_CONSTRUCTIONS)


def _recorded_slice(attempt: DispatchAttempt) -> SliceState:
    """The slice whose dispatch boundary records exactly this attempt."""
    return replace(
        _pending_slice(),
        status=SliceStatus.DISPATCH_UNCONFIRMED,
        agent_type=attempt.agent_type,
        model=attempt.model,
        attempts=attempt.attempt,
        dispatch_intent_id=attempt.intent_id,
        dispatch_started_at=attempt.started_at,
        dispatch_ledger_floor=attempt.ledger_floor,
        dispatch_generation=attempt.dispatch_generation,
    )


def _keyword_arguments(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            names.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return names


def _constructing_functions(tree: ast.AST) -> set[str]:
    """The function each ``DispatchAttempt(...)`` call belongs to."""
    found: set[str] = set()

    def visit(node: ast.AST, owner: str) -> None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "DispatchAttempt"
        ):
            found.add(owner)
        name = (
            node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        )
        for child in ast.iter_child_nodes(node):
            visit(child, name or owner)

    visit(tree, "<module>")
    return found
