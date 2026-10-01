"""Policy-bound semantics for a plan's ``agent_type`` harness request.

A request is a *constrained preference*: it narrows the selector to harnesses
the role's policy allowlist already approves, and the selector then either
selects one of them or refuses the slice with a typed reason. These tests pin
every branch of that contract, plus the persistence and fail-closed migration
that keep "requested" and "selected" distinguishable afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tl_loop.loop.driver import SubTLTask, TLLoopConfig, WorkPlan
from tl_loop.plan_validation import PlanValidationError, validate_plan_document
from tl_loop.preflight import PreflightError, _validate_plan_spawn_routes
from tl_loop.select.agent_type import (
    POLICY_REQUEST_REASON,
    SelectionFailure,
    SelectionLedger,
    policy_approved_candidates,
    select_agent_type,
    selection_failure,
)
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.agent_type import HarnessChoice
from tl_loop.select.policy import HarnessPolicy, validate_policy
from tl_loop.fsm.phase import TLPhase
from tl_loop.state.schema import (
    BudgetLedger,
    FSMState,
    ParkCause,
    SliceState,
    SliceStatus,
    Verdict,
)

CAPABILITIES = CapabilityMap(
    {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
)


def test_bare_request_selects_the_policy_approved_harness_of_that_agent_type() -> None:
    choice = _select(requested_harness="codex")

    assert choice is not None
    assert choice.harness == "codex/gpt-luna"
    assert choice.requested_harness == "codex"
    assert choice.request_honored is True
    assert choice.reason == POLICY_REQUEST_REASON


def test_model_qualified_request_selects_that_exact_approved_harness() -> None:
    choice = _select(requested_harness="claude/sonnet", paths=("proto/events.proto",))

    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.requested_harness == "claude/sonnet"


def test_request_narrows_rather_than_widens_the_allowlist() -> None:
    """A request never introduces a harness policy did not approve."""
    policy = _policy(allow=("codex/gpt-luna",))
    approved = policy_approved_candidates("claude/sonnet", policy.roles["worker"])

    assert approved == ()

    choice = select_agent_type(
        _slice(),
        "worker",
        SelectionLedger(),
        policy,
        CAPABILITIES,
        requested_harness="claude/sonnet",
    )
    assert choice is None
    assert _failure(requested_harness="claude/sonnet", policy=policy) is (
        SelectionFailure.REQUEST_NOT_ALLOWED
    )


def test_disallowed_request_parks_with_the_typed_not_allowed_cause() -> None:
    from tl_loop.loop.driver import _SELECTION_PARK_CAUSES

    policy = _policy(allow=("codex/gpt-luna",))
    failure = _failure(requested_harness="opencode/deepseek", policy=policy)

    assert failure is SelectionFailure.REQUEST_NOT_ALLOWED
    assert _SELECTION_PARK_CAUSES[failure] is ParkCause.HARNESS_REQUEST_NOT_ALLOWED


def test_unparseable_request_is_refused_rather_than_forwarded() -> None:
    """An unsupported agent type answers with no candidates, not a bad spawn."""
    policy = _policy()
    assert policy_approved_candidates("unknown/runtime", policy.roles["worker"]) == ()
    assert _failure(requested_harness="unknown/runtime", policy=policy) is (
        SelectionFailure.REQUEST_NOT_ALLOWED
    )


def test_unavailable_capability_parks_the_request_rather_than_substituting() -> None:
    policy = _policy()
    capabilities = CapabilityMap({"codex/gpt-luna": Difficulty.TRIVIAL})
    hard = _slice(paths=("src/task.py", "src/task.rs"))

    choice = select_agent_type(
        hard,
        "worker",
        SelectionLedger(),
        policy,
        capabilities,
        requested_harness="claude/sonnet",
    )

    assert choice is None
    assert selection_failure(
        hard,
        "worker",
        SelectionLedger(),
        policy,
        capabilities,
        requested_harness="claude/sonnet",
    ) is SelectionFailure.REQUEST_NOT_CAPABLE


def test_exhausted_budget_parks_the_request_with_the_budget_cause() -> None:
    from tl_loop.loop.driver import _SELECTION_PARK_CAUSES

    policy = _policy()
    ledger = SelectionLedger(role_spent={"worker": 120000})

    choice = select_agent_type(
        _slice(),
        "worker",
        ledger,
        policy,
        CAPABILITIES,
        requested_harness="codex",
    )

    assert choice is None
    failure = selection_failure(
        _slice(), "worker", ledger, policy, CAPABILITIES, requested_harness="codex"
    )
    assert failure is SelectionFailure.OVER_BUDGET
    assert _SELECTION_PARK_CAUSES[failure] is ParkCause.BUDGET_EXHAUSTED


def test_omitted_request_leaves_selection_unconstrained() -> None:
    choice = _select(requested_harness=None, paths=("proto/events.proto",))

    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.requested_harness is None
    assert choice.request_honored is False
    assert choice.reason == "hard_classification"


def test_request_the_escalation_retired_is_refused_not_re_run() -> None:
    """Escalation and a pinned request disagree; the slice needs a human."""
    policy = _policy()
    retried = _slice(
        resolved_harness="codex/gpt-luna",
        verdict=Verdict.NO_GO,
        attempts=1,
    )

    choice = select_agent_type(
        retried,
        "worker",
        SelectionLedger(),
        policy,
        CAPABILITIES,
        requested_harness="codex/gpt-luna",
    )

    assert choice is None
    assert selection_failure(
        retried,
        "worker",
        SelectionLedger(),
        policy,
        CAPABILITIES,
        requested_harness="codex/gpt-luna",
    ) is SelectionFailure.REQUEST_SUPERSEDED_BY_ESCALATION


def test_escalation_reads_the_resolved_harness_so_it_actually_escalates() -> None:
    """Escalation matches allowlist entries, which are qualified identifiers.

    The slice's ``agent_type`` is only the protocol half of the harness, so
    matching on it alone never excluded anything and a NO_GO retry silently
    re-ran the same harness.
    """
    policy = _policy()
    retried = _slice(resolved_harness="codex/gpt-luna", verdict=Verdict.NO_GO, attempts=1)

    choice = select_agent_type(retried, "worker", SelectionLedger(), policy, CAPABILITIES)

    assert choice is not None
    assert choice.harness == "claude/sonnet"
    assert choice.reason == "escalated_after_no_go"


def test_plan_persists_the_request_and_the_resolved_route_separately() -> None:
    """A pending slice records the request and no route at all."""
    from tl_loop.loop.driver import _initial_slices

    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "build", "agent_type": "codex"}]}
    )
    records = _initial_slices(plan)
    record = records["leaf-a"]

    assert record["requested_harness"] == "codex"
    assert record["agent_type"] is None
    assert record["resolved_harness"] is None


def test_sub_tl_has_no_harness_request_because_it_is_not_a_model_session() -> None:
    with pytest.raises(PlanValidationError, match="unknown keys: agent_type"):
        validate_plan_document(
            {
                "plan": {
                    "sub_tls": [
                        {
                            "name": "auth",
                            "order": 1,
                            "agent_type": "codex",
                            "plan": {"leaves": []},
                        }
                    ]
                }
            }
        )

    with pytest.raises(ValueError, match="unknown keys: agent_type"):
        WorkPlan.from_mapping(
            {"sub_tls": [{"name": "auth", "order": 1, "agent_type": "codex", "plan": {}}]}
        )


def test_sub_tl_task_object_has_no_agent_type_field() -> None:
    import dataclasses

    fields = {field.name for field in dataclasses.fields(SubTLTask)}

    assert "agent_type" not in fields


def test_preflight_names_the_sub_tl_agent_type_refusal_explicitly(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="not a model session"):
        _validate_plan_spawn_routes(
            {"plan": {"sub_tls": [{"name": "auth", "order": 1, "agent_type": "codex"}]}},
            tmp_path / "plan.json",
        )


def test_status_explains_requested_versus_resolved_execution() -> None:
    """An operator reading status can tell what was asked for from what ran."""
    from tl_loop.state.read_model import _slice_model

    model = _slice_model(
        _slice(requested_harness="codex", resolved_harness="codex/gpt-luna"), {}
    )
    document = model.to_document()

    assert document["requested_harness"] == "codex"
    assert document["resolved_harness"] == "codex/gpt-luna"
    assert document["agent_type"] is None


def test_restart_replays_the_request_rather_than_the_executed_route(tmp_path: Path) -> None:
    """A reloaded checkpoint still answers "what did the plan ask for?"""
    from tl_loop.loop.driver import DispatchAttempt
    from tl_loop.state.plan_manifest import build_plan_manifest  # noqa: PLC0415
    from tl_loop.state.store import RunStore, _decode_slice, create  # noqa: PLC0415

    run_dir = tmp_path
    store = RunStore("restart", run_dir)
    create(
        "restart",
        {
            "plan_manifest": build_plan_manifest(
                {"leaves": [{"name": "task", "task": "build", "agent_type": "codex"}]},
                scope_id="restart",
            ).to_document()
        },
        root_dir=run_dir,
    )
    dispatched = _dispatched(
        _slice(requested_harness="codex", resolved_harness="codex/gpt-luna")
    )
    store.checkpoint(FSMState(TLPhase.TLWaiting, ("task",)), {"task": dispatched}, BudgetLedger(0, 0), 0)

    reloaded = RunStore("restart", run_dir).load().slices["task"]

    assert reloaded.requested_harness == "codex"
    assert reloaded.resolved_harness == "codex/gpt-luna"
    attempt = DispatchAttempt.recorded_for(reloaded, None)
    assert attempt.requested_harness == "codex"
    assert attempt.harness == "codex/gpt-luna"
    assert attempt.agent_type == "codex"
    # The persisted round trip is what makes the restart claim auditable.
    assert _decode_slice(json.loads(json.dumps(_encoded_slice(dispatched)))) == dispatched


def test_legacy_checkpoint_splits_its_single_harness_value_by_dispatch_evidence(
    tmp_path: Path,
) -> None:
    """A pre-split value is placed by what the slice actually recorded."""
    from tl_loop.state.migration import migrate_checkpoint_document

    document = {
        "run_id": "legacy",
        "slices": {
            "undispatched": {"status": "pending", "agent_type": "codex/gpt-luna"},
            "dispatched": {
                "status": "spawned",
                "agent_type": "codex/gpt-luna",
                "dispatch_intent_id": "intent-a",
                "dispatch_agent_id": "agent-a",
                "dispatch_authoritative_event_seq": 3,
            },
        },
    }

    result = migrate_checkpoint_document(document, run_id="legacy")

    migrated = _migrated_slices(result.document)
    pending = migrated["undispatched"]
    assert pending["requested_harness"] == "codex/gpt-luna"
    assert pending["agent_type"] is None
    assert pending["resolved_harness"] is None

    spawned = migrated["dispatched"]
    assert spawned["agent_type"] == "codex/gpt-luna"
    assert spawned["requested_harness"] is None
    assert spawned["resolved_harness"] is None
    assert "dispatched.requested_harness=unrecoverable" in result.changes


def test_legacy_checkpoint_refuses_an_unusable_harness_value(tmp_path: Path) -> None:
    """A value that is neither a clean request nor a route is dropped, not guessed."""
    from tl_loop.state.migration import migrate_checkpoint_document

    document = {
        "run_id": "legacy",
        "slices": {"slice-a": {"status": "pending", "agent_type": ""}},
    }

    result = migrate_checkpoint_document(document, run_id="legacy")

    migrated = _migrated_slices(result.document)["slice-a"]
    assert migrated["agent_type"] is None
    assert migrated["requested_harness"] is None
    assert "slice-a.agent_type=refused" in result.changes


def _migrated_slices(document: dict[str, object]) -> dict[str, dict[str, object]]:
    """Type-narrow the migrated document's slice map for direct assertions."""
    from typing import cast  # noqa: PLC0415

    return cast(dict[str, dict[str, object]], document["slices"])


def _encoded_slice(slice_state: SliceState) -> dict[str, object]:
    from tl_loop.state.store import _encode_slice  # noqa: PLC0415 - encoder under test

    return _encode_slice(slice_state.id, slice_state)


def _dispatched(slice_state: SliceState) -> SliceState:
    """Return the slice as a confirmed dispatch persisted it."""
    from dataclasses import replace

    return replace(
        slice_state,
        agent_type="codex",
        dispatch_intent_id="intent-a",
        dispatch_started_at=1.0,
        dispatch_last_boundary="agent.spawned",
        dispatch_agent_id="task",
        dispatch_authoritative_event_seq=2,
        attempts=1,
        status=SliceStatus.SPAWNED,
    )


def _select(
    requested_harness: str | None,
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    policy: HarnessPolicy | None = None,
) -> HarnessChoice | None:
    return select_agent_type(
        _slice(paths=paths),
        "worker",
        SelectionLedger(),
        policy or _policy(),
        CAPABILITIES,
        requested_harness=requested_harness,
    )


def _failure(
    requested_harness: str | None, *, policy: HarnessPolicy | None = None
) -> SelectionFailure:
    return selection_failure(
        _slice(),
        "worker",
        SelectionLedger(),
        policy or _policy(),
        CAPABILITIES,
        requested_harness=requested_harness,
    )


def _policy(allow: tuple[str, ...] = ("codex/gpt-luna", "claude/sonnet")) -> HarnessPolicy:
    table = {
        "allow": list(allow),
        "cost_rank": {harness: index + 1 for index, harness in enumerate(allow)},
        "token_budget": 120000,
        "per_harness_budget": {harness: 80000 for harness in allow},
        "escalate_after_attempts": 1,
    }
    return validate_policy(
        {"roles": {"tl": dict(table), "worker": dict(table), "reviewer": dict(table)}}
    )


def _slice(
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    agent_type: str | None = None,
    requested_harness: str | None = None,
    resolved_harness: str | None = None,
    verdict: Verdict | None = None,
    attempts: int = 0,
) -> SliceState:
    return SliceState(
        id="task",
        status=SliceStatus.PENDING,
        paths=paths,
        depends_on=(),
        base_ref="main",
        test_plan=("pytest",),
        agent_type=agent_type,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=attempts,
        verdict=verdict,
        requested_harness=requested_harness,
        resolved_harness=resolved_harness,
    )