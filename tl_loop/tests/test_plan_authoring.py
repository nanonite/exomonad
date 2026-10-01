"""Coverage for bounded, tool-free plan authoring and human acceptance."""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from tl_loop.client.transport import JsonObject
from tl_loop.rlm.budget import ContextOverflow
from tl_loop.rlm.decompose import DecompositionParked
from tl_loop.rlm.plan_acceptance import (
    ACCEPTANCE_APPROVED,
    ACCEPTANCE_PENDING,
    MAX_AUDIT_VIOLATION_CHARS,
    PlanAcceptanceError,
    PlanAcceptanceRequired,
    PlanAuthoringRecordError,
    PlanRejected,
    acceptance_gate_name,
    acceptance_status,
    gate_status_of,
    install_accepted_plan,
    open_acceptance_gate,
    plan_from_record,
    proposal_summary,
    record_authored_plan,
    recorded_plan,
    require_acceptance,
)
from tl_loop.rlm.plan_authoring import (
    AuthoredPlan,
    PlanAuthoringError,
    PlanAuthoringInput,
    PlanAuthoringInputError,
    PlanAuthoringUnsupported,
    author_plan,
    authoring_root_spec,
    judgment_audit,
    resolve_authoring_model_choice,
    slices_to_plan_document,
)
from tl_loop.rlm.slice_spec import SliceSpec
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest
from tl_loop.select.model import ModelCatalog, select_model
from tl_loop.select.policy import HarnessPolicy, validate_policy
from tl_loop.state.schema import GateStatus, SliceStatus
from tl_loop.state.store import RunStore, create

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FakeBackend:
    """A tool-free backend that records every request it is handed."""

    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


def _policy(harness: str = "codex/gpt-luna") -> HarnessPolicy:
    role = {
        "allow": [harness],
        "cost_rank": {harness: 1},
        "token_budget": 1_000_000,
        "escalate_after_attempts": 1,
    }
    return validate_policy(
        {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}
    )


def _choice(
    backend: FakeBackend,
    *,
    context_length: int = 100_000,
    max_attempts: int = 3,
) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="gpt-luna",
        backend=backend,
        store=RlmCallStore(),
        context_length=context_length,
        max_attempts=max_attempts,
    )


def _slice(
    slice_id: str,
    path: str,
    *,
    depends_on: list[str] | None = None,
    verify: list[str] | None = None,
    test_plan: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": slice_id,
        "title": f"Implement {slice_id}",
        "paths": [path],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": test_plan if test_plan is not None else ["just tl-loop-test"],
        "steps": [f"Implement {slice_id}"],
        "verify": verify if verify is not None else ["just tl-loop-lint"],
        "boundary": ["Do not edit unrelated paths"],
        "done_criteria": [f"{slice_id} is complete"],
    }


def _output(*slices: dict[str, object]) -> dict[str, object]:
    return {"slices": list(slices)}


def _request(**overrides: object) -> PlanAuthoringInput:
    values: dict[str, object] = {"task": "Split the API work into owned slices"}
    values.update(overrides)
    return PlanAuthoringInput.from_mapping(values)


def _authored(responses: list[object], **overrides: object) -> tuple[AuthoredPlan, FakeBackend]:
    backend = FakeBackend(responses)
    plan = author_plan(
        _request(**overrides),
        model_choice=_choice(backend),
        run_id="root",
        policy=_policy()
    )
    return plan, backend


def _store(tmp_path: Path, run_id: str = "root") -> RunStore:
    create(run_id, {}, root_dir=tmp_path)
    return RunStore(run_id, tmp_path)


def _project_store(project_root: Path, run_id: str = "root") -> RunStore:
    """Create a run under the project layout the launcher resolves."""
    root = project_root / ".exo" / "tl-loop"
    create(run_id, {}, root_dir=root)
    return RunStore(run_id, root)


def _dispatch(store: RunStore, slice_id: str) -> None:
    """Move one slice past PENDING so its manifest node becomes protected."""
    state = store.load()
    store.checkpoint(
        state.fsm.phase,
        {
            name: (
                replace(
                    slice_state,
                    status=SliceStatus.SPAWNED,
                    dispatch_intent_id=f"intent-{slice_id}",
                    dispatch_agent_id=f"agent-{slice_id}",
                    dispatch_authoritative_event_seq=1,
                )
                if name == slice_id
                else slice_state
            )
            for name, slice_state in state.slices.items()
        },
        state.budgets,
        state.events.last_consumed_offset,
    )


# --------------------------------------------------------------------------
# Valid authoring
# --------------------------------------------------------------------------


def test_valid_authoring_produces_a_validated_inert_plan() -> None:
    plan, backend = _authored([_output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py"))])

    assert plan.requires_acceptance is True
    assert plan.run_id == "root"
    leaves = cast(list[JsonObject], cast(JsonObject, plan.plan_mapping())["leaves"])
    assert [leaf["name"] for leaf in leaves] == ["api", "tests"]
    assert leaves[0]["boundary"] == ["src/api.py"]
    assert leaves[0]["task"] == "Implement api"
    assert [node.name for node in plan.manifest.nodes] == ["api", "tests"]
    assert plan.manifest.scope_id == "root"
    assert plan.digest == plan.manifest.digest


def test_authored_test_plan_survives_into_the_leaf_verification() -> None:
    plan, _ = _authored(
        [_output(_slice("api", "src/api.py", test_plan=["just tl-loop-replay"], verify=["just tl-loop-lint"]))]
    )

    leaves = cast(list[JsonObject], cast(JsonObject, plan.plan_mapping())["leaves"])
    # The controller derives a slice's run-state test_plan from the leaf's
    # verify list, so the authored test plan must lead it or it would be lost.
    assert leaves[0]["verify"] == ["just tl-loop-replay", "just tl-loop-lint"]


def test_authoring_never_hands_the_model_tools_or_an_effect_client() -> None:
    _plan, backend = _authored([_output(_slice("api", "src/api.py"))])

    assert backend.requests
    for request in backend.requests:
        assert request.tools == ()
        assert request.output_schema["additionalProperties"] is False


def test_authoring_request_omits_harness_and_budget_authority() -> None:
    root_spec = authoring_root_spec(
        _request(
            read_first=["tl_loop/rlm/slice_spec.py"],
            constraints=["keep paths disjoint"],
            agent_type="codex",
            budgets={"tokens": 10},
        )
    )

    assert root_spec == {
        "task": "Split the API work into owned slices",
        "base_ref": "main",
        "read_first": ["tl_loop/rlm/slice_spec.py"],
        "constraints": ["keep paths disjoint"],
    }
    assert "agent_type" not in root_spec
    assert "budgets" not in root_spec


def test_authored_plan_validates_against_the_executable_plan_contract() -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])

    from tl_loop.plan_validation import validate_plan_document

    assert validate_plan_document(dict(plan.document))["plan"]


# --------------------------------------------------------------------------
# Policy-resolved model choice
# --------------------------------------------------------------------------


def test_model_choice_is_resolved_from_the_policy_allowlist() -> None:
    backend = FakeBackend([_output(_slice("api", "src/api.py"))])

    choice = resolve_authoring_model_choice(
        _policy(),
        backend=backend,
    )

    assert choice.model_id == "gpt-luna"
    assert choice.role == "tl"
    assert choice.store.ledger.budgets == {"tl": 1_000_000}


def test_bare_agent_type_policy_defers_the_model_to_the_catalog() -> None:
    catalog = ModelCatalog.from_payload(
        {
            "models": [
                {"harness": "codex", "model_id": "gpt-luna", "coding_score": 90.0},
                {"harness": "codex", "model_id": "gpt-solo", "coding_score": 80.0},
            ]
        }
    )
    choice = resolve_authoring_model_choice(
        _policy("codex"),
        backend=FakeBackend([]),
        catalog=catalog,
        requested_model="gpt-solo",
    )

    assert choice.model_id == "gpt-solo"
    assert select_model("codex", catalog, "gpt-solo").model_id == "gpt-solo"


def test_model_qualified_policy_pins_the_model_over_a_request() -> None:
    catalog = ModelCatalog.from_payload(
        {"models": [{"harness": "codex", "model_id": "gpt-solo", "coding_score": 80.0}]}
    )

    with pytest.raises(PlanAuthoringError, match="policy pins"):
        resolve_authoring_model_choice(
            _policy(),
            backend=FakeBackend([]),
            catalog=catalog,
            requested_model="gpt-solo",
        )


def test_model_resolution_fails_closed_without_a_harness_policy() -> None:
    with pytest.raises(TypeError, match="HarnessPolicy"):
        resolve_authoring_model_choice(
            {"roles": {}},  # type: ignore[arg-type]
            backend=FakeBackend([]),
        )


def test_model_resolution_fails_closed_for_an_empty_policy() -> None:
    empty = HarnessPolicy(roles=MappingProxyType({}))

    with pytest.raises(PlanAuthoringError, match="no 'tl' role"):
        resolve_authoring_model_choice(empty, backend=FakeBackend([]))


def test_authoring_refuses_a_bare_backend_without_a_resolved_choice() -> None:
    with pytest.raises(TypeError, match="policy-resolved RlmModelChoice"):
        author_plan(
            _request(),
            model_choice=FakeBackend([]),  # type: ignore[arg-type]
            run_id="root",
        )


# --------------------------------------------------------------------------
# Repair, retry exhaustion, and context overflow
# --------------------------------------------------------------------------


def test_invalid_output_is_repaired_with_specific_feedback() -> None:
    # Schema-valid but unsafe: overlapping owned paths. The judgment's own
    # retry succeeds on the first attempt, so the outer decompose loop is the
    # one that carries the violation forward as feedback.
    backend = FakeBackend(
        [
            _output(_slice("api", "src/shared.py"), _slice("tests", "src/shared.py")),
            _output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py")),
        ]
    )

    plan = author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert [node.name for node in plan.manifest.nodes] == ["api", "tests"]
    assert len(backend.requests) == 2
    retry_sections = cast(
        "list[dict[str, object]]", cast(JsonObject, backend.requests[1].inputs)["sections"]
    )
    feedback = next(
        section for section in retry_sections if section["name"] == "validation_feedback"
    )
    assert "overlaps" in cast(str, feedback["content"])


def test_retry_exhaustion_parks_without_producing_a_plan() -> None:
    backend = FakeBackend(
        [
            _output(_slice("api", "src/shared.py"), _slice("tests", "src/shared.py"))
            for _ in range(3)
        ]
    )

    with pytest.raises(DecompositionParked) as raised:
        author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert raised.value.attempts == 3
    assert any("overlaps" in item for item in raised.value.violations)


def test_bounded_retry_limit_is_respected() -> None:
    backend = FakeBackend(
        [
            _output(_slice("api", "src/shared.py"), _slice("tests", "src/shared.py"))
            for _ in range(3)
        ]
    )

    with pytest.raises(DecompositionParked) as raised:
        author_plan(
            _request(),
            model_choice=_choice(backend, max_attempts=2),
            run_id="root",
        )

    assert raised.value.attempts == 2
    assert len(backend.requests) == 2


def test_structured_output_failure_surfaces_as_a_judgment_failure() -> None:
    backend = FakeBackend([{"slices": [{"id": "api"}]} for _ in range(9)])

    with pytest.raises(DecompositionParked) as raised:
        author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert any("structured output failed" in item for item in raised.value.violations)


def test_context_overflow_is_refused_rather_than_truncated() -> None:
    backend = FakeBackend([_output(_slice("api", "src/api.py"))])

    with pytest.raises(ContextOverflow):
        author_plan(
            _request(),
            model_choice=_choice(backend, context_length=10),
            run_id="root",
        )
    assert backend.requests == []





# --------------------------------------------------------------------------
# Cycle and ownership rejection
# --------------------------------------------------------------------------


def test_judgment_audit_records_only_bounded_dimensions() -> None:
    # Attempt 1 fails the closed schema inside the judgment, attempt 2
    # succeeds, so one decompose round produces two audited attempts.
    backend = FakeBackend(
        [
            {"slices": [{"id": "api"}]},
            _output(_slice("api", "src/api.py")),
        ]
    )
    choice = _choice(backend)
    plan = author_plan(_request(), model_choice=choice, run_id="root")

    audit = judgment_audit(choice)

    assert audit["judgment"] == "decompose"
    assert audit["model"] == "gpt-luna"
    assert audit["attempts"] == 2
    assert audit["failures"] == 1
    assert set(audit) == {
        "judgment",
        "model",
        "attempts",
        "tokens",
        "failures",
        "violations",
    }
    violations = cast("list[str]", audit["violations"])
    assert all(len(item) <= MAX_AUDIT_VIOLATION_CHARS for item in violations)
    serialized = str(audit)
    assert plan.digest not in serialized
    assert "src/api.py" not in serialized


def test_cyclic_dependencies_park_the_authoring_judgment() -> None:
    backend = FakeBackend(
        [
            _output(
                _slice("api", "src/api.py", depends_on=["tests"]),
                _slice("tests", "tests/api.py", depends_on=["api"]),
            )
            for _ in range(3)
        ]
    )

    with pytest.raises(DecompositionParked) as raised:
        author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert any("depends_on cycle" in item for item in raised.value.violations)


def test_overlapping_owned_paths_are_rejected_before_a_plan_exists() -> None:
    backend = FakeBackend(
        [
            _output(_slice("api", "src/shared.py"), _slice("tests", "src/shared.py")),
            _output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py")),
        ]
    )

    plan = author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert [node.name for node in plan.manifest.nodes] == ["api", "tests"]


def test_undeclared_dependencies_are_refused_by_the_adapter() -> None:
    slices = (
        SliceSpec(
            id="api",
            title="Implement api",
            paths=("src/api.py",),
            depends_on=("tests",),
            base_ref="main",
            test_plan=("just tl-loop-test",),
            steps=("Implement api",),
            verify=("just tl-loop-lint",),
            boundary=("Do not edit unrelated paths",),
            done_criteria=("api is complete",),
        ),
    )

    with pytest.raises(PlanAuthoringUnsupported, match="dependency edges"):
        slices_to_plan_document(slices, run_id="root")


def test_absolute_owned_paths_are_rejected_by_the_judgment() -> None:
    backend = FakeBackend([_output(_slice("api", "/etc/passwd")) for _ in range(3)])

    with pytest.raises(DecompositionParked) as raised:
        author_plan(_request(), model_choice=_choice(backend), run_id="root")

    assert any("repository-relative" in item for item in raised.value.violations)


# --------------------------------------------------------------------------
# Closed input keys, harness requests, and budgets
# --------------------------------------------------------------------------


def test_authoring_input_rejects_executable_plan_keys() -> None:
    with pytest.raises(PlanAuthoringInputError, match="not an executable plan"):
        PlanAuthoringInput.from_mapping({"task": "x", "leaves": [{"name": "a", "task": "b"}]})


def test_authoring_input_rejects_unknown_keys() -> None:
    with pytest.raises(PlanAuthoringInputError, match="unknown keys"):
        PlanAuthoringInput.from_mapping({"task": "x", "parallelism": 4})


def test_authoring_input_rejects_an_empty_task_and_unknown_budget_keys() -> None:
    with pytest.raises(PlanAuthoringInputError, match="task"):
        PlanAuthoringInput.from_mapping({"task": "  "})
    with pytest.raises(PlanAuthoringInputError, match="budgets contains unknown keys"):
        PlanAuthoringInput.from_mapping({"task": "x", "budgets": {"retries": 3}})


def test_authoring_input_rejects_negative_budgets_and_absolute_read_first() -> None:
    with pytest.raises(PlanAuthoringInputError, match="non-negative integer"):
        PlanAuthoringInput.from_mapping({"task": "x", "budgets": {"tokens": -1}})
    with pytest.raises(PlanAuthoringInputError, match="repository-relative"):
        PlanAuthoringInput.from_mapping({"task": "x", "read_first": ["/etc/shadow"]})


def test_harness_request_outside_the_allowlist_is_refused_before_the_judgment() -> None:
    backend = FakeBackend([_output(_slice("api", "src/api.py"))])

    with pytest.raises(PlanAuthoringError, match="not approved"):
        author_plan(
            _request(agent_type="codex/gpt-other"),
            model_choice=_choice(backend),
            run_id="root",
            policy=_policy()
        )
    assert backend.requests == []


def test_harness_request_without_a_policy_cannot_be_validated() -> None:
    backend = FakeBackend([_output(_slice("api", "src/api.py"))])

    with pytest.raises(PlanAuthoringError, match="must be validated against"):
        author_plan(
            _request(agent_type="codex"),
            model_choice=_choice(backend),
            run_id="root",
        )


def test_allowed_harness_request_reaches_every_leaf() -> None:
    plan, _ = _authored(
        [_output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py"))],
        agent_type="codex",
    )

    leaves = cast(list[JsonObject], cast(JsonObject, plan.plan_mapping())["leaves"])
    assert [leaf["agent_type"] for leaf in leaves] == ["codex", "codex"]


# --------------------------------------------------------------------------
# Human acceptance
# --------------------------------------------------------------------------


def test_authored_plan_is_not_authority_before_acceptance(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)

    state = store.load()
    assert acceptance_status(state, plan) is GateStatus.PENDING
    with pytest.raises(PlanAcceptanceRequired):
        require_acceptance(state, plan)
    with pytest.raises(PlanAcceptanceRequired):
        install_accepted_plan(store, plan)
    assert "api" not in store.load().slices


def test_absent_gate_is_not_acceptance(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)

    assert acceptance_status(store.load(), plan) is None
    with pytest.raises(PlanAcceptanceRequired):
        require_acceptance(store.load(), plan)


def test_rejection_is_terminal_and_never_becomes_authority(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.REJECTED)

    with pytest.raises(PlanRejected):
        require_acceptance(store.load(), plan)
    with pytest.raises(PlanRejected):
        install_accepted_plan(store, plan)
    assert "api" not in store.load().slices


def test_approval_installs_the_validated_manifest(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.APPROVED)

    state = install_accepted_plan(store, plan)

    assert state.plan_manifest is not None
    assert state.plan_manifest.digest == plan.digest
    assert sorted(state.slices) == ["api", "tests"]
    assert all(
        slice_state.status is SliceStatus.PENDING for slice_state in state.slices.values()
    )


def test_approving_one_proposal_does_not_accept_another(tmp_path: Path) -> None:
    approved, _ = _authored([_output(_slice("api", "src/api.py"))])
    other, _ = _authored([_output(_slice("api", "src/api.py"), _slice("docs", "docs/api.md"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, approved)
    store.answer_gate(acceptance_gate_name(approved), GateStatus.APPROVED)

    assert acceptance_gate_name(approved) != acceptance_gate_name(other)
    with pytest.raises(PlanAcceptanceRequired):
        require_acceptance(store.load(), other)


# --------------------------------------------------------------------------
# Restart before and after acceptance
# --------------------------------------------------------------------------


def test_restart_before_acceptance_leaves_the_run_without_authority(
    tmp_path: Path,
) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)

    # A restart is a fresh store over the same durable run directory.
    restarted = RunStore("root", tmp_path)

    with pytest.raises(PlanAcceptanceRequired):
        require_acceptance(restarted.load(), plan)
    with pytest.raises(PlanAcceptanceRequired):
        install_accepted_plan(restarted, plan)
    # The run still carries the empty planning-state declaration it was
    # created with, not the authored plan's nodes.
    persisted = restarted.load().plan_manifest
    assert persisted is not None
    assert persisted.nodes == ()
    assert persisted.digest != plan.digest


def test_restart_after_acceptance_preserves_the_accepted_identity(
    tmp_path: Path,
) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.APPROVED)
    install_accepted_plan(store, plan)

    restarted = RunStore("root", tmp_path)
    state = restarted.load()

    assert state.plan_manifest is not None
    assert state.plan_manifest.digest == plan.digest
    assert state.plan_manifest.owned_branch == "main"
    require_acceptance(state, plan)


def test_a_continuation_cannot_rewrite_dispatched_ownership(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.APPROVED)
    install_accepted_plan(store, plan)
    _dispatch(store, "api")

    renamed = author_plan(
        _request(),
        model_choice=_choice(FakeBackend([_output(_slice("api", "src/renamed.py"))])),
        run_id="root",
    )
    open_acceptance_gate(store, renamed)
    store.answer_gate(acceptance_gate_name(renamed), GateStatus.APPROVED)

    with pytest.raises(PlanAcceptanceError, match="cannot replace the persisted declaration"):
        install_accepted_plan(store, renamed)


# --------------------------------------------------------------------------
# Operator-facing CLI entry point
# --------------------------------------------------------------------------


def launcher_main(argv: list[str]) -> int:
    from tl_loop import __main__ as launcher

    return launcher.main(argv)


def _run_cli(project_root: Path, *argv: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = launcher_main(["plan-authoring", "--project-root", str(project_root), *argv])
    return code, cast("dict[str, object]", json.loads(output.getvalue()))


def test_cli_reports_a_recorded_proposal_without_installing_it(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _project_store(tmp_path)
    open_acceptance_gate(store, plan)

    code, summary = _run_cli(tmp_path)

    assert code == 0
    assert summary["plan_digest"] == plan.digest
    assert summary["gate_name"] == acceptance_gate_name(plan)
    assert summary["status"] == "pending"
    assert summary["slices"] == ["api"]
    assert summary["audit"] == plan.audit
    installed = store.load().plan_manifest
    assert installed is not None
    assert installed.digest != plan.digest


def test_cli_install_refuses_while_the_gate_is_pending(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _project_store(tmp_path)
    open_acceptance_gate(store, plan)

    assert launcher_main(["plan-authoring", "--project-root", str(tmp_path), "--install"]) == 2
    assert "api" not in store.load().slices


def test_cli_install_publishes_the_accepted_plan(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _project_store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.APPROVED)

    code, summary = _run_cli(tmp_path, "--install")

    assert code == 0
    assert summary["installed"] is True
    assert summary["manifest_digest"] == plan.digest
    assert summary["status"] == ACCEPTANCE_APPROVED
    state = store.load()
    assert state.plan_manifest is not None
    assert state.plan_manifest.digest == plan.digest
    assert sorted(state.slices) == ["api"]


def test_cli_refuses_when_no_proposal_was_recorded(tmp_path: Path) -> None:
    _store(tmp_path)

    assert launcher_main(["plan-authoring", "--project-root", str(tmp_path)]) == 2


# --------------------------------------------------------------------------
# Durable plan identity and bounded judgment audit records
# --------------------------------------------------------------------------


def test_opening_the_gate_persists_the_validated_identity_and_audit(
    tmp_path: Path,
) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)

    open_acceptance_gate(store, plan)

    record = store.plan_authoring_record()
    assert record is not None
    assert record["plan_digest"] == plan.digest
    assert record["gate_name"] == acceptance_gate_name(plan)
    assert record["status"] == ACCEPTANCE_PENDING
    assert record["run_id"] == "root"
    assert record["audit"] == plan.audit
    assert set(record) == {
        "schema_version",
        "run_id",
        "plan_digest",
        "gate_name",
        "status",
        "owned_branch",
        "document",
        "audit",
    }


def test_recorded_plan_round_trips_and_keeps_the_accepted_digest(
    tmp_path: Path,
) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)

    # A second process reads the durable record rather than re-running the
    # judgment, and recovers exactly the same proposal identity.
    restarted = RunStore("root", tmp_path)
    recovered = recorded_plan(restarted, "root")

    assert recovered is not None
    assert recovered.digest == plan.digest
    assert recovered.manifest == plan.manifest
    assert recovered.plan_mapping() == plan.plan_mapping()
    assert recovered.audit == plan.audit


def test_install_after_approval_uses_the_durable_record(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    store.answer_gate(acceptance_gate_name(plan), GateStatus.APPROVED)

    restarted = RunStore("root", tmp_path)
    state = install_accepted_plan(restarted, run_id="root")

    assert state.plan_manifest is not None
    assert state.plan_manifest.digest == plan.digest
    assert sorted(state.slices) == ["api"]
    record = restarted.plan_authoring_record()
    assert record is not None
    assert record["status"] == ACCEPTANCE_APPROVED


def test_install_without_a_recorded_proposal_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert recorded_plan(store, "root") is None
    with pytest.raises(PlanAuthoringRecordError, match="no plan-authoring proposal"):
        install_accepted_plan(store, run_id="root")


def test_a_record_for_another_run_is_refused(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)

    with pytest.raises(PlanAuthoringRecordError, match="belongs to run 'root'"):
        recorded_plan(store, "other")


def test_a_hand_edited_record_cannot_present_another_plan(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)

    edited = dict(store.plan_authoring_record() or {})
    document = dict(cast(Mapping[str, object], edited["document"]))
    plan_object = dict(cast(Mapping[str, object], document["plan"]))
    leaves = [dict(leaf) for leaf in cast("list[dict[str, object]]", plan_object["leaves"])]
    leaves[0]["boundary"] = ["src/elsewhere.py"]
    plan_object["leaves"] = leaves
    document["plan"] = plan_object
    edited["document"] = document
    store.record_plan_authoring(edited)

    # The digest is what the gate is named after, so an edited body cannot
    # borrow an approval.
    with pytest.raises(PlanAuthoringRecordError, match="does not hash to its recorded digest"):
        recorded_plan(store, "root")


def test_malformed_records_are_refused(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)
    open_acceptance_gate(store, plan)
    good = dict(store.plan_authoring_record() or {})

    def refuse(mutate: Callable[[dict[str, object]], None], match: str) -> None:
        edited = dict(good)
        mutate(edited)
        store.record_plan_authoring(edited)
        with pytest.raises(PlanAuthoringRecordError, match=match):
            plan_from_record(edited)

    refuse(lambda record: record.update({"extra": 1}), "unknown keys")
    refuse(lambda record: record.update({"schema_version": 2}), "schema version")
    refuse(lambda record: record.update({"plan_digest": "abc"}), "sha256 hex")
    refuse(lambda record: record.update({"status": "guessed"}), "valid status")
    refuse(lambda record: record.update({"gate_name": "plan-acceptance-0000"}), "gate")
    refuse(lambda record: record.update({"audit": {"model": ""}}), "audit model")
    refuse(lambda record: record.update({"audit": {"attempts": "2"}}), "audit attempts")
    refuse(
        lambda record: record.update({"audit": {"attempts": -1}}),
        "non-negative",
    )
    refuse(
        lambda record: record.update(
            {"audit": {"violations": ["x" * (MAX_AUDIT_VIOLATION_CHARS + 1)]}}
        ),
        "violations must be strings",
    )


def test_record_status_must_be_known(tmp_path: Path) -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"))])
    store = _store(tmp_path)

    with pytest.raises(PlanAuthoringRecordError, match="status 'approved' is not recognized"):
        record_authored_plan(store, plan, status="approved")


def test_proposal_summary_is_bounded_and_does_not_carry_the_plan_body() -> None:
    plan, _ = _authored([_output(_slice("api", "src/api.py"), _slice("docs", "docs/api.md"))])

    summary = proposal_summary(plan, status=ACCEPTANCE_PENDING)

    assert summary["plan_digest"] == plan.digest
    assert summary["slices"] == ["api", "docs"]
    assert summary["requires_acceptance"] is True
    assert "plan" not in summary
    assert "src/api.py" not in str(summary)


def test_judgment_audit_is_persisted_with_the_identity(tmp_path: Path) -> None:
    backend = FakeBackend(
        [
            {"slices": [{"id": "api"}]},
            _output(_slice("api", "src/api.py")),
        ]
    )
    choice = _choice(backend)
    plan = author_plan(_request(), model_choice=choice, run_id="root")
    store = _store(tmp_path)

    record = record_authored_plan(store, plan, status=ACCEPTANCE_PENDING)
    audit = cast(Mapping[str, object], record["audit"])

    assert audit["attempts"] == 2
    assert audit["failures"] == 1
    assert audit["violations"] == plan.audit["violations"]
    assert judgment_audit(choice) == plan.audit


# --------------------------------------------------------------------------
# Bounded judgment audit records
# --------------------------------------------------------------------------


def test_judgment_audit_of_a_clean_judgment_has_no_violations() -> None:
    backend = FakeBackend([_output(_slice("api", "src/api.py"))])
    choice = _choice(backend)
    author_plan(_request(), model_choice=choice, run_id="root")

    audit = judgment_audit(choice)

    assert audit["attempts"] == 1
    assert audit["failures"] == 0
    assert audit["violations"] == []
