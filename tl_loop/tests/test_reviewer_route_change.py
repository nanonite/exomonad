"""A reviewer route change is configuration only; run state never sees it.

The reviewer route is operator configuration (``config.toml`` ``[reviewer]``
and ``.exo/harness_policy.toml`` ``roles.reviewer.allow``), not a per-slice or
per-call request. ``derive_next_action`` and ``slice_transition`` therefore have
no reviewer-route input at all. These tests pin that the same run state makes
the same decision whatever the configured reviewer route is, so a route change
cannot invalidate a verdict, reset or double-count a counter, or duplicate an
in-flight reviewer. They do not exercise the Rust spawn/resolver path; the
Rust tests in ``exomonad-core`` do that.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.loop.reconcile import (
    ExternalIntent,
    InternalTransition,
    Quiescent,
    derive_next_action,
)
from tl_loop.state.review_validation import review_validation_is_fresh
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    HandoffEvidence,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.slice_transition import (
    HeadChanged,
    ReviewerIdentityObserved,
    ReviewVerdictObserved,
    slice_transition,
)

HEAD = "head-a"
NEXT_HEAD = "head-b"
PR = 43


def _slice(
    *,
    head: str = HEAD,
    verdict: Verdict | None = None,
    reviewed_head: str | None = None,
    review_rounds: int = 0,
    intervention_attempts: int = 0,
    reviewer_agent_id: str | None = "review-pr-43-codex",
    action: ActionState | None = None,
    reviewer_attempt: dict[str, int] | None = None,
) -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-6.1-sol",
        branch="main.slice-a",
        worktree=".worktrees/slice-a",
        pr_number=PR,
        reviewed_head=reviewed_head,
        attempts=1,
        verdict=verdict,
        review_rounds=review_rounds,
        tl_intervention_attempts=intervention_attempts,
        reviewer_attempt={head: 1} if reviewer_attempt is None else reviewer_attempt,
        reviewer_agent_id=reviewer_agent_id,
        dispatch_agent_id="worker-invocation",
        handoff=HandoffEvidence(
            pr_number=PR,
            head_sha=head,
            attempt=1,
            invocation_id="worker-invocation",
            agent_id="worker-agent",
            observed_at="2026-08-27T00:00:00Z",
        ),
        action=action,
    )


def _approved_state(verdict: Verdict = Verdict.GO) -> SliceState:
    return replace(
        _slice(
            verdict=verdict,
            reviewed_head=HEAD,
            reviewer_agent_id="review-pr-43-codex",
            reviewer_attempt={HEAD: 1},
        ),
        ci_state={HEAD: "success"},
    )


def _verdict_event(
    *,
    head: str = HEAD,
    verdict: Verdict = Verdict.GO,
    reviewer_agent_id: str = "review-pr-43-codex",
    review_id: int = 17,
) -> ReviewVerdictObserved:
    return ReviewVerdictObserved(
        head_sha=head,
        verdict=verdict,
        reviewer_agent_id=reviewer_agent_id,
        review_id=review_id,
        findings=(),
        source="ledger",
        pr_number=PR,
        reviewer_account_authenticated=True,
        reviewer_identity_unresolved=False,
        self_approval=False,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at="2026-08-27T00:00:00Z",
        observed_at="2026-08-27T00:00:00Z",
    )


@pytest.mark.parametrize(
    ("verdict", "expected_operation"),
    [
        (Verdict.GO, "merge"),
        (Verdict.GO_WITH_NITS, "merge"),
        (Verdict.NO_GO, "repair"),
    ],
)
def test_persisted_verdict_is_configuration_independent_and_not_re_requested(
    verdict: Verdict, expected_operation: str
) -> None:
    """Run state alone decides; no reviewer-route input exists to invalidate it."""
    state = _approved_state(verdict)

    decision = derive_next_action(state, reviewer_max_rounds=5)

    assert isinstance(decision, ExternalIntent)
    assert decision.operation == expected_operation
    assert decision.operation != "spawn_reviewer"
    assert state.verdict is verdict
    assert state.reviewed_head == HEAD
    assert state.reviewer_attempt == {HEAD: 1}


def test_no_reviewer_route_input_changes_the_same_state_decision() -> None:
    """Calling the pure decision twice pins that configuration is not an input."""
    state = _approved_state(Verdict.GO)

    # A route change is operator configuration: no run-state event, no verdict
    # invalidation, and no new reviewer action.
    first = derive_next_action(state, reviewer_max_rounds=5)
    second = derive_next_action(state, reviewer_max_rounds=5)

    assert first == second
    assert isinstance(first, ExternalIntent)
    assert first.operation == "merge"
    assert first.arguments["head_sha"] == HEAD


def test_head_change_invalidates_the_verdict_and_queues_one_reviewer_for_the_new_head() -> None:
    state = _approved_state(Verdict.GO)
    changed = slice_transition(state, HeadChanged(NEXT_HEAD))
    changed = replace(
        changed,
        handoff=HandoffEvidence(
            pr_number=PR,
            head_sha=NEXT_HEAD,
            attempt=1,
            invocation_id="worker-invocation",
            agent_id="worker-agent",
            observed_at="2026-08-27T00:00:00Z",
        ),
        reviewer_attempt={HEAD: 1},
    )

    decision = derive_next_action(changed, reviewer_max_rounds=5)

    assert isinstance(decision, ExternalIntent)
    assert decision.operation == "spawn_reviewer"
    assert decision.arguments["head_sha"] == NEXT_HEAD
    assert changed.verdict is None
    assert changed.reviewed_head == NEXT_HEAD


def test_review_rounds_and_interventions_count_once_per_verdict_not_per_configuration() -> None:
    """A configuration change is not an event: one verdict is one round."""
    state = _slice(review_rounds=2, intervention_attempts=1)

    changed_head = slice_transition(state, HeadChanged(NEXT_HEAD))
    changed_head = replace(
        changed_head,
        handoff=HandoffEvidence(
            pr_number=PR,
            head_sha=NEXT_HEAD,
            attempt=1,
            invocation_id="worker-invocation",
            agent_id="worker-agent",
            observed_at="2026-08-27T00:00:00Z",
        ),
    )
    replacement_identity = slice_transition(
        changed_head, ReviewerIdentityObserved("review-pr-43-opencode")
    )
    assert replacement_identity.review_rounds == 2
    assert replacement_identity.tl_intervention_attempts == 1
    assert replacement_identity.reviewer_agent_id == "review-pr-43-opencode"

    reviewed = slice_transition(
        replacement_identity,
        _verdict_event(
            head=NEXT_HEAD,
            reviewer_agent_id="review-pr-43-opencode",
            review_id=18,
        ),
    )
    assert reviewed.review_rounds == 3, "one verdict after the route change is one round"
    assert reviewed.tl_intervention_attempts == 1
    assert reviewed.verdict is Verdict.GO


def test_review_stuck_still_fires_at_the_ceiling_independent_of_configuration() -> None:
    exhausted = replace(
        _approved_state(Verdict.NO_GO),
        review_rounds=2,
        reviewed_head=HEAD,
        reviewer_agent_id="review-pr-43-opencode",
    )

    decision = derive_next_action(exhausted, reviewer_max_rounds=2)

    assert decision == InternalTransition("parked", "review_rounds_exhausted")
    assert exhausted.review_rounds == 2


def test_verdict_from_a_replacement_reviewer_identity_is_accepted_for_the_same_head() -> None:
    """Pure ``slice_transition`` state test, not route resolution.

    It pins that the slice state machine accepts a verdict bound to a new
    reviewer identity for the same exact head. It does not read configuration,
    spawn a reviewer, or exercise the Rust reviewer resolver/author attribution.
    """
    state = _slice(reviewer_agent_id="review-pr-43-codex")

    observed = slice_transition(
        state, ReviewerIdentityObserved("review-pr-43-opencode")
    )
    reviewed = slice_transition(
        observed,
        _verdict_event(reviewer_agent_id="review-pr-43-opencode"),
    )

    assert reviewed.verdict is Verdict.GO
    assert reviewed.reviewer_agent_id == "review-pr-43-opencode"
    assert reviewed.review_evidence is not None
    assert reviewed.review_evidence.reviewer_agent_id == "review-pr-43-opencode"
    assert review_validation_is_fresh(
        reviewed.review_evidence,
        now=None,
        freshness_window_secs=None,
        expected_pr_number=PR,
        expected_head_sha=HEAD,
        expected_verdict=Verdict.GO,
        expected_reviewer_agent_id="review-pr-43-opencode",
    )


def test_inflight_reviewer_action_adds_no_route_dependent_decision() -> None:
    """The in-flight action gate is state-only; configuration cannot duplicate it."""
    inflight = _slice(
        action=ActionState(
            ActionKind.REVIEWER_SPAWN,
            ActionPhase.IN_FLIGHT,
            intent_id="review-intent",
            head_sha=HEAD,
            attempt=1,
        ),
        reviewer_attempt={HEAD: 1},
    )

    decision = derive_next_action(inflight, reviewer_max_rounds=5)

    assert isinstance(decision, Quiescent)
    assert decision.reason == "await_reviewer_spawn_reconciliation"
    assert inflight.reviewer_attempt == {HEAD: 1}
    assert inflight.action is not None
    assert inflight.action.phase is ActionPhase.IN_FLIGHT


def test_no_reviewer_harness_escalation_path_exists() -> None:
    """The selector's failure escalation is worker-only.

    A repeated NO-GO removes a *worker* harness from rotation after
    ``roles.<worker>.escalate_after_attempts``. No code path calls
    ``select_agent_type`` for the reviewer role, so there is no reviewer
    "luna to sol" escalation to compose with a configured route change.
    """
    source = Path(__file__).resolve().parents[1].joinpath("loop/driver.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    roles: list[str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else None
        if name != "select_agent_type":
            continue
        role: str | None = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            role = str(node.args[1].value)
        for keyword in node.keywords:
            if keyword.arg == "role" and isinstance(keyword.value, ast.Constant):
                role = str(keyword.value.value)
        roles.append(role)

    assert roles, "driver.py calls the shared selector"
    assert "reviewer" not in roles, (
        "the reviewer route is configured, not selected/escalated: "
        f"selector roles were {roles}"
    )


def test_reviewer_route_is_not_an_input_of_the_run_state_machine() -> None:
    """A reviewer-route field must never become a run-state input.

    ``derive_next_action`` takes run state plus the review policy ceiling and
    ``slice_transition`` takes ``SliceState`` plus a ``SliceEvent``. If a route,
    harness, effort or reviewer-model field is ever added to either signature or
    to a concrete ``SliceEvent``, the route change would become run state and
    could invalidate a verdict or reset counters. This test fails then, so the
    configuration-only pin above keeps its teeth.
    """
    root = Path(__file__).resolve().parents[1]
    forbidden_tokens = ("harness", "route", "effort")

    def is_route_field(name: str) -> bool:
        lowered = name.lower()
        return any(token in lowered for token in forbidden_tokens) or (
            "reviewer" in lowered and "model" in lowered
        )

    def function_parameters(path: Path, name: str) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                arguments = [*node.args.args, *node.args.kwonlyargs]
                return [argument.arg for argument in arguments if argument.arg != "self"]
        raise AssertionError(f"{name} is declared in {path}")

    for path, name in (
        (root / "loop/reconcile.py", "derive_next_action"),
        (root / "state/slice_transition.py", "slice_transition"),
    ):
        offending = [parameter for parameter in function_parameters(path, name) if is_route_field(parameter)]
        assert not offending, f"{name} must not take reviewer-route input: {offending}"

    tree = ast.parse((root / "state/slice_transition.py").read_text(encoding="utf-8"))
    events = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "SliceEvent" for base in node.bases
        )
    ]
    assert events, "SliceEvent subclasses are declared in slice_transition.py"
    for event in events:
        fields = [
            statement.target.id
            for statement in event.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        ]
        offending = [field for field in fields if is_route_field(field)]
        assert not offending, f"{event.name} must not carry reviewer-route input: {offending}"
