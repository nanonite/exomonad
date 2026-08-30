"""Startup reconciliation recovers facts without executing reviewer effects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from tl_loop.client.effects import ToolResult
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.convergence import ConvergenceTracker
from tl_loop.loop.driver import (
    EffectIntent,
    LeafTask,
    TLLoopConfig,
    WorkPlan,
    _action_journal_gate_name,
    _apply_convergence,
    _persist_review_validation,
    _reconcile_action_journal,
    _reconcile_nonterminal_slices,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.state.schema import (
    DurableReviewEvidence,
    FSMState,
    GateStatus,
    ReviewValidationDisposition,
    ReviewValidationObservation,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.slice_transition import (
    RevalidateReview,
    ReviewValidationFailed,
    slice_transition,
)
from tl_loop.state.store import RunStore, _encode_slice, create

_PLAN = WorkPlan(
    leaves=(
        LeafTask(
            name="slice-a",
            task="implement slice a",
            verify=("just test",),
        ),
    )
)


def _spawned_slice_without_pr() -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.SPAWNED,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-5",
        branch="task/slice-a",
        worktree=".worktrees/slice-a",
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_intent_id="intent-a",
        dispatch_agent_id="agent-a",
        dispatch_authoritative_event_seq=7,
    )


@dataclass
class FakeClient:
    watcher_calls: list[dict[str, object]] = field(default_factory=list)
    spawn_reviewer_calls: list[dict[str, object]] = field(default_factory=list)
    merge_calls: list[dict[str, object]] = field(default_factory=list)
    resolved_pr_number: int | None = 99
    review_state: str = "pending"
    pr_state: str = "open"
    merged: bool = False
    head_reachable: bool = True
    publication_ownership_verified: bool = True
    publication_ownership_error: str = ""
    review_id: int | None = None
    review_verdict: str | None = None
    review_head_sha: str | None = None
    review_submitted_at: str | None = None
    review_body: str | None = None
    reviewer_agent_id: str | None = None
    reviewer_identity_error: str | None = None
    resume_pr_calls: list[dict[str, object]] = field(default_factory=list)
    chainlink_issue_close_calls: list[dict[str, object]] = field(default_factory=list)
    chainlink_issue_close_result: ToolResult | None = None

    def list_agents(self, *, filter_type: str | None = None) -> ToolResult:
        return ToolResult(
            raw={"success": True},
            success=True,
            result={"agents": [{"agent_id": "agent-a", "intent_id": "intent-a", "is_alive": True}]},
            error=None,
        )

    def resolve_live_pr_for_slice(self, *, slice_id: str) -> ToolResult:
        if slice_id == "slice-a" and self.resolved_pr_number is not None:
            return ToolResult(
                raw={"success": True},
                success=True,
                result={
                    "slice_id": slice_id,
                    "resolution": "live",
                    "pr_number": self.resolved_pr_number,
                },
                error=None,
            )
        return ToolResult(
            raw={"success": True},
            success=True,
            result={
                "slice_id": slice_id,
                "resolution": "never_published",
                "pr_number": 0,
            },
            error=None,
        )

    def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
        self.watcher_calls.append({"pr_number": pr_number})
        if pr_number == self.resolved_pr_number and self.resolved_pr_number is not None:
            result = {
                "found": True,
                "pr_number": self.resolved_pr_number,
                "head_sha": "head-a",
                "head_branch": "main.slice-a",
                "base_branch": "main",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "review_state": self.review_state,
                "ci_status": "success",
                "state": self.pr_state,
                "pr_state": self.pr_state,
                "merged": self.merged,
                "head_reachable": self.head_reachable,
            }
            result["publication_ownership_verified"] = self.publication_ownership_verified
            result["publication_ownership_error"] = self.publication_ownership_error
            if self.review_id is not None:
                result["review_id"] = self.review_id
            if self.review_verdict is not None:
                result["review_verdict"] = self.review_verdict
            if self.review_head_sha is not None:
                result["review_head_sha"] = self.review_head_sha
            if self.review_submitted_at is not None:
                result["review_submitted_at"] = self.review_submitted_at
            if self.review_body is not None:
                result["review_body"] = self.review_body
            if self.reviewer_agent_id is not None:
                result["reviewer_agent_id"] = self.reviewer_agent_id
            if self.reviewer_identity_error is not None:
                result["reviewer_identity_error"] = self.reviewer_identity_error
            return ToolResult(
                raw={"success": True},
                success=True,
                result=result,
                error=None,
            )
        return ToolResult(
            raw={"success": False},
            success=False,
            result=None,
            error=f"no live PR found for pr_number {pr_number}",
        )

    def spawn_reviewer(
        self,
        *,
        pr_number: int,
        head_sha: str,
        acceptance_criteria: tuple[str, ...],
        force: bool,
    ) -> ToolResult:
        self.spawn_reviewer_calls.append(
            {
                "pr_number": pr_number,
                "head_sha": head_sha,
                "acceptance_criteria": acceptance_criteria,
                "force": force,
            }
        )
        return ToolResult(raw={"success": True}, success=True, result={}, error=None)

    def merge_pr(self, **arguments: object) -> ToolResult:
        self.merge_calls.append(arguments)
        self.merged = True
        return ToolResult(
            raw={"success": True},
            success=True,
            result={"merged": True},
            error=None,
        )

    def resume_pr(self, **arguments: object) -> ToolResult:
        self.resume_pr_calls.append(arguments)
        return ToolResult(
            raw={"success": True},
            success=True,
            result={"resumed": True},
            error=None,
        )

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: tuple[str, ...] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        del title, description, labels, priority
        return ToolResult(
            raw={"success": True},
            success=True,
            result={"issue_id": 932},
            error=None,
        )

    def chainlink_issue_close(
        self,
        *,
        issue_id: int,
        force: bool,
        summary: str | None = None,
        commit_changelog: bool = False,
    ) -> ToolResult:
        self.chainlink_issue_close_calls.append(
            {
                "issue_id": issue_id,
                "force": force,
                "summary": summary,
                "commit_changelog": commit_changelog,
            }
        )
        if self.chainlink_issue_close_result is not None:
            return self.chainlink_issue_close_result
        return ToolResult(raw={"success": True}, success=True, result={}, error=None)

    def emit_controller_event(self, *, event_type: str, payload: dict[str, object]) -> ToolResult:
        del event_type, payload
        return ToolResult(raw={"success": True}, success=True, result={}, error=None)


def _load_state(tmp_path):
    create(
        "reconcile",
        {"slices": {"slice-a": _encode_slice("slice-a", _spawned_slice_without_pr())}},
        root_dir=tmp_path,
    )
    store = RunStore("reconcile", tmp_path)
    return store, store.load()


def test_reconciliation_recovers_and_persists_missing_pr_number(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    state = store.checkpoint(
        state.fsm,
        {"slice-a": replace(state.slices["slice-a"], dispatch_invocation_id="invocation-a")},
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient(publication_ownership_verified=True)
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    new_state = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    assert [call["pr_number"] for call in client.watcher_calls] == [99]
    slice_state = new_state.slices["slice-a"]
    assert slice_state.pr_number == 99
    assert slice_state.reconciliation is not None
    assert "pr_number" not in slice_state.reconciliation["missing_evidence"]
    assert "published_pr" in slice_state.reconciliation["authoritative_evidence"]
    assert slice_state.reconciliation["next_action"] != "await_authoritative_evidence"

    # The recovered pr_number is durable, not just an in-memory observation.
    assert store.load().slices["slice-a"].pr_number == 99

    # Startup reconciliation records facts only; the reducer owns the effect.
    assert client.spawn_reviewer_calls == []
    assert new_state.slices["slice-a"].reviewer_attempt == {}
    converged = _apply_convergence(
        new_state,
        ConvergenceTracker(),
        store,
        config,
        client,
        [],
    )
    assert client.spawn_reviewer_calls == [
        {
            "pr_number": 99,
            "head_sha": "head-a",
            "acceptance_criteria": client.spawn_reviewer_calls[0]["acceptance_criteria"],
            "force": False,
        }
    ]
    assert converged.slices["slice-a"].reviewer_attempt == {"head-a": 1}
    assert store.load().slices["slice-a"].reviewer_attempt == {"head-a": 1}


def test_reconciliation_preserves_plan_json_bytes_across_restart(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    plan_path = tmp_path / ".exo" / "tl-loop" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"leaves":[{"name":"slice-a"}]}\n'
    plan_path.write_bytes(original)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=False)

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])
    after_retry = plan_path.read_bytes()
    _reconcile_nonterminal_slices(_PLAN, store.load(), config, client, store, [])
    after_restart = plan_path.read_bytes()

    assert after_retry == original
    assert after_restart == original


def test_reconciliation_adopts_authoritative_publication_handoff_after_restart(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    restarted_slice = replace(
        state.slices["slice-a"],
        pr_number=99,
        dispatch_invocation_id="inv-new",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": restarted_slice},
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient(publication_ownership_verified=True)
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=False)

    new_state = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    recovered = new_state.slices["slice-a"]
    assert recovered.status is SliceStatus.IN_REVIEW
    assert recovered.handoff is not None
    assert recovered.handoff.pr_number == 99
    assert recovered.handoff.head_sha == "head-a"
    assert recovered.handoff.invocation_id == "inv-new"
    assert recovered.handoff.agent_id == "agent-a"
    assert recovered.publication is not None
    assert recovered.publication.head_sha == "head-a"
    assert recovered.publication.head_branch == "main.slice-a"
    assert store.load().slices["slice-a"].handoff == recovered.handoff


def test_reconciliation_rebuilds_publication_and_handoff_when_pr_number_was_lost(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    restarted_slice = replace(
        state.slices["slice-a"],
        dispatch_invocation_id="inv-old",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": restarted_slice},
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient(publication_ownership_verified=True)
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=False)

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])
    slice_state = recovered.slices["slice-a"]

    assert slice_state.pr_number == 99
    assert slice_state.publication is not None
    assert slice_state.publication.pr_number == 99
    assert slice_state.handoff is not None
    assert slice_state.handoff.pr_number == 99
    assert slice_state.handoff.head_sha == "head-a"


def test_reconciliation_does_not_respawn_reviewer_when_head_already_claimed(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    already_claimed = replace(state.slices["slice-a"], reviewer_attempt={"head-a": 1})
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": already_claimed},
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    assert client.spawn_reviewer_calls == []


def test_reconciliation_parks_closed_unmerged_pr_without_resurrecting_slice(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient(pr_state="closed")
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    new_state = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    parked = new_state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause.value == "pr_closed_unmerged"
    assert parked.reconciliation["next_action"] == "park_closed_unmerged_pr"
    assert client.spawn_reviewer_calls == []
    assert store.load().slices["slice-a"].status is SliceStatus.PARKED


def test_reconciliation_does_not_spawn_reviewer_when_disabled(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=False)

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    assert client.spawn_reviewer_calls == []


def test_reconciliation_leaves_pr_number_missing_when_nothing_was_ever_published(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient(resolved_pr_number=None)
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    new_state = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    slice_state = new_state.slices["slice-a"]
    assert slice_state.pr_number is None
    assert slice_state.reconciliation["missing_evidence"] == ["pr_number"]
    assert slice_state.reconciliation["next_action"] == "await_authoritative_evidence"
    assert client.spawn_reviewer_calls == []


def test_reconciliation_skips_slice_id_lookup_when_ledger_run_id_unset(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id=None, enable_reviewer_spawn=True)

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    assert client.watcher_calls == []
    assert client.spawn_reviewer_calls == []


def _review_recovery_state(store: RunStore):
    state = store.load()
    return store.checkpoint(
        state.fsm,
        {
            "slice-a": replace(
                state.slices["slice-a"],
                status=SliceStatus.IN_REVIEW,
                pr_number=99,
                reviewed_head="head-a",
                dispatch_invocation_id="invocation-a",
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-99-codex",
            )
        },
        state.budgets,
        25446,
    )


def test_reconciliation_replays_exact_head_review_when_verdict_was_lost(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    client = FakeClient(
        review_id=7,
        review_verdict="APPROVED",
        review_head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
    )
    config = TLLoopConfig(
        active=True,
        ledger_run_id="run-1",
        enable_reviewer_spawn=True,
        chainlink_issue_id=1039,
    )
    journal = EffectJournal("run-1", tmp_path / "action-journal.json")

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    slice_state = recovered.slices["slice-a"]
    assert slice_state.reviewed_head == "head-a"
    assert slice_state.verdict is not None
    assert slice_state.verdict.value == "GO"
    assert slice_state.reconciliation["next_action"] == "queue_merge"
    assert client.spawn_reviewer_calls == []
    assert recovered.events.last_consumed_offset == 25446

    merged = _apply_convergence(
        recovered,
        ConvergenceTracker(),
        store,
        config,
        client,
        journal,
    )
    assert len(client.merge_calls) == 1
    assert client.merge_calls[0]["expected_head_sha"] == "head-a"
    assert len(client.chainlink_issue_close_calls) == 1
    assert client.chainlink_issue_close_calls[0]["commit_changelog"] is True
    assert merged.slices["slice-a"].status is SliceStatus.MERGED

    repeated = _reconcile_nonterminal_slices(
        _PLAN,
        store.load(),
        config,
        client,
        store,
        journal,
    )
    _apply_convergence(repeated, ConvergenceTracker(), store, config, client, journal)
    assert len(client.merge_calls) == 1
    assert len(client.chainlink_issue_close_calls) == 1


def test_confirmed_merge_is_adopted_atomically_before_review_revalidation(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    state = store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("slice-a",)),
        {
            "slice-a": replace(
                state.slices["slice-a"],
                status=SliceStatus.IN_REVIEW,
                pr_number=99,
                dispatch_invocation_id="invocation-a",
                reviewed_head="head-a",
            )
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    journal = EffectJournal("run-1", tmp_path / "action-journal.json")
    intent = EffectIntent("merge_pr", "slice-a", {"pr_number": 99}, True)
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult(
            raw={"success": True, "result": {"merged": True}},
            success=True,
            result={"merged": True},
            error=None,
        ),
    )
    client = FakeClient(merged=True, pr_state="closed")
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    assert recovered.slices["slice-a"].status is SliceStatus.MERGED
    assert recovered.slices["slice-a"].action is None
    assert recovered.fsm.phase is TLPhase.TLAllMerged
    assert recovered.fsm.waiting == ()
    assert client.spawn_reviewer_calls == []
    assert client.merge_calls == []
    assert journal.pending_entries() == []


def test_review_validation_failure_reason_round_trips_through_checkpoint(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    failed = slice_transition(
        state.slices["slice-a"],
        ReviewValidationFailed(
            disposition=ReviewValidationDisposition.INVALIDATED,
            reason="authoritative_review_observation_incomplete",
        ),
    )
    store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": failed},
        state.budgets,
        state.events.last_consumed_offset,
    )

    restored = store.load().slices["slice-a"]

    assert restored.review_validation_failure_reason == (
        "authoritative_review_observation_incomplete"
    )
    assert restored.review_validation_disposition is ReviewValidationDisposition.INVALIDATED
    assert restored.stall_classification is None


def test_stale_exact_head_review_refreshes_validation_once_before_merge(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    current = state.slices["slice-a"]
    evidence = DurableReviewEvidence(
        review_id=7,
        pr_number=99,
        head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        verdict=Verdict.GO,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at="2026-08-27T00:00:00Z",
    )
    state = store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "slice-a": replace(
                current,
                verdict=Verdict.GO,
                verdict_at=evidence.submitted_at,
                review_evidence=evidence,
                review_validation_required=False,
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    policy_path = tmp_path / "review-policy.toml"
    policy_path.write_text("review_freshness_window_secs = 1200\n", encoding="utf-8")
    client = FakeClient(
        review_id=7,
        review_verdict="APPROVED",
        review_head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
    )
    config = TLLoopConfig(
        active=True,
        ledger_run_id="run-1",
        enable_reviewer_spawn=True,
        review_policy_path=policy_path,
        chainlink_issue_id=1039,
    )
    journal = EffectJournal("run-1", tmp_path / "action-journal.json")

    refreshed = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)
    refreshed_slice = refreshed.slices["slice-a"]

    assert refreshed.state_version == 1
    assert refreshed_slice.review_evidence is not None
    assert refreshed_slice.review_evidence.submitted_at == evidence.submitted_at
    assert refreshed_slice.review_evidence.validated_at != evidence.validated_at
    assert refreshed_slice.review_rounds == 0
    assert refreshed_slice.review_validation_required is False

    _apply_convergence(refreshed, ConvergenceTracker(), store, config, client, journal)
    assert len(client.merge_calls) == 1

    repeated = _reconcile_nonterminal_slices(
        _PLAN,
        store.load(),
        config,
        client,
        store,
        journal,
    )
    _apply_convergence(repeated, ConvergenceTracker(), store, config, client, journal)
    _apply_convergence(repeated, ConvergenceTracker(), store, config, client, journal)
    assert len(client.merge_calls) == 1


def test_derived_revalidation_and_success_share_one_version_advance(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    evidence = DurableReviewEvidence(
        review_id=7,
        pr_number=99,
        head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        verdict=Verdict.GO,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at="2026-08-27T00:00:00Z",
    )
    current = replace(
        state.slices["slice-a"],
        verdict=Verdict.GO,
        verdict_at=evidence.submitted_at,
        review_evidence=evidence,
        review_validation_required=False,
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": current},
        state.budgets,
        state.events.last_consumed_offset,
    )
    requested = slice_transition(current, RevalidateReview())
    requested_state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": requested},
        state.budgets,
        state.events.last_consumed_offset,
        state_version=1,
    )
    observation = ReviewValidationObservation(
        review_id=7,
        pr_number=99,
        head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        verdict=Verdict.GO,
        observed_at="2026-08-29T00:00:00Z",
        submitted_at=evidence.submitted_at,
    )

    refreshed = _persist_review_validation(
        requested_state,
        requested,
        observation,
        store,
        submitted_at=observation.submitted_at,
    )

    assert refreshed.state_version == 1
    assert refreshed.slices["slice-a"].review_validation_required is False


def test_out_of_order_snapshot_is_quiescent_while_revalidation_is_pending(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    evidence = DurableReviewEvidence(
        review_id=7,
        pr_number=99,
        head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        verdict=Verdict.GO,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at="2026-08-29T00:00:00Z",
    )
    state = store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "slice-a": replace(
                state.slices["slice-a"],
                verdict=Verdict.GO,
                verdict_at=evidence.submitted_at,
                review_evidence=evidence,
                review_validation_required=True,
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient(
        review_id=6,
        review_verdict="APPROVED",
        review_head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
    )
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])
    current = recovered.slices["slice-a"]

    assert current.review_evidence is not None
    assert current.review_evidence.review_id == 7
    assert current.review_validation_required is True
    assert current.review_rounds == 0


def test_newer_same_head_snapshot_supersedes_fresh_verdict(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    evidence = DurableReviewEvidence(
        review_id=7,
        pr_number=99,
        head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        verdict=Verdict.GO,
        submitted_at="2026-08-29T00:00:00Z",
        validated_at="2026-08-29T00:00:00Z",
    )
    state = store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "slice-a": replace(
                state.slices["slice-a"],
                verdict=Verdict.GO,
                verdict_at=evidence.submitted_at,
                review_evidence=evidence,
                review_validation_required=False,
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    client = FakeClient(
        review_id=8,
        review_verdict="CHANGES_REQUESTED",
        review_head_sha="head-a",
        review_body="Please fix the regression",
        reviewer_agent_id="review-pr-99-codex",
    )
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])
    current = recovered.slices["slice-a"]

    assert current.verdict is Verdict.NO_GO
    assert current.review_evidence is not None
    assert current.review_evidence.review_id == 8
    assert current.review_rounds == 1


def test_remote_merge_stays_nonterminal_when_bookkeeping_fails(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    client = FakeClient(
        review_id=7,
        review_verdict="APPROVED",
        review_head_sha="head-a",
        reviewer_agent_id="review-pr-99-codex",
        chainlink_issue_close_result=ToolResult(
            raw={"success": False, "error": "changelog commit failed"},
            success=False,
            result=None,
            error="changelog commit failed",
        ),
    )
    config = TLLoopConfig(
        active=True,
        ledger_run_id="run-1",
        enable_reviewer_spawn=True,
        chainlink_issue_id=1039,
    )
    journal = EffectJournal("run-1", tmp_path / "action-journal.json")

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)
    after_merge = _apply_convergence(
        recovered,
        ConvergenceTracker(),
        store,
        config,
        client,
        journal,
    )

    assert len(client.merge_calls) == 1
    assert len(client.chainlink_issue_close_calls) == 1
    assert after_merge.slices["slice-a"].status is SliceStatus.MERGED
    assert after_merge.slices["slice-a"].action is None
    assert "changelog commit failed" in (after_merge.slices["slice-a"].dispatch_error or "")
    pending = journal.pending_entries()
    assert len(pending) == 1
    assert pending[0]["operation"] == "merge_bookkeeping"
    gate = next(gate for gate in after_merge.gates if gate.name.startswith("tl-action-journal-"))
    assert gate.status is GateStatus.PENDING


@dataclass
class RecordingRepairBackend:
    response: RlmResponse
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        return self.response


def test_reconciliation_replays_actionable_findings_through_resume_pr_once(tmp_path) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    client = FakeClient(
        review_id=8,
        review_verdict="CHANGES_REQUESTED",
        review_head_sha="head-a",
        review_body="Handle the missing error branch in src/a.py before retrying.",
        reviewer_agent_id="review-pr-99-codex",
    )
    backend = RecordingRepairBackend(
        RlmResponse(
            {
                "root_cause": "The error branch is not handled",
                "proposed_solution": "Handle the error branch in src/a.py",
                "read_first": ["src/a.py"],
                "steps": ["Update src/a.py"],
                "verify": ["just test"],
                "boundary": ["Only edit src/a.py"],
                "done_criteria": ["The error branch is covered"],
            }
        )
    )
    config = TLLoopConfig(
        active=True,
        ledger_run_id="run-1",
        enable_reviewer_spawn=True,
        review_model_choice=RlmModelChoice(
            model_id="test-model",
            backend=backend,
            store=RlmCallStore(),
            context_length=10_000,
        ),
    )

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    slice_state = recovered.slices["slice-a"]
    assert slice_state.review_findings["head-a"][0]["rationale"] == (
        "Handle the missing error branch in src/a.py before retrying."
    )
    assert len(backend.requests) == 1
    no_go_sections = [
        section
        for section in backend.requests[0].inputs["sections"]
        if section["name"] == "no_go_reasons"
    ]
    assert len(no_go_sections) == 1
    assert "Handle the missing error branch" in str(no_go_sections[0]["content"])
    assert len(client.resume_pr_calls) == 1
    assert client.resume_pr_calls[0]["pr_number"] == 99

    repeated = _reconcile_nonterminal_slices(
        _PLAN,
        store.load(),
        config,
        client,
        store,
        [],
    )

    assert repeated.slices["slice-a"].verdict is not None
    assert len(backend.requests) == 1
    assert len(client.resume_pr_calls) == 1


def test_repair_effect_journal_recovers_unknown_resume_without_duplicate_dispatch(tmp_path) -> None:
    import pytest

    class UnknownResumeClient(FakeClient):
        def resume_pr(self, **arguments: object) -> ToolResult:
            self.resume_pr_calls.append(arguments)
            raise RuntimeError("resume transport connection lost")

    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    client = UnknownResumeClient(
        review_id=8,
        review_verdict="CHANGES_REQUESTED",
        review_head_sha="head-a",
        review_body="Handle the missing error branch in src/a.py before retrying.",
        reviewer_agent_id="review-pr-99-codex",
    )
    backend = RecordingRepairBackend(
        RlmResponse(
            {
                "root_cause": "The error branch is not handled",
                "proposed_solution": "Handle the error branch in src/a.py",
                "read_first": ["src/a.py"],
                "steps": ["Update src/a.py"],
                "verify": ["just test"],
                "boundary": ["Only edit src/a.py"],
                "done_criteria": ["The error branch is covered"],
            }
        )
    )
    config = TLLoopConfig(
        active=True,
        ledger_run_id="run-1",
        enable_reviewer_spawn=True,
        review_model_choice=RlmModelChoice(
            model_id="test-model",
            backend=backend,
            store=RlmCallStore(),
            context_length=10_000,
        ),
    )
    journal_path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-1", journal_path)

    with pytest.raises(RuntimeError, match="resume transport connection lost"):
        _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    pending = journal.pending_entries()
    assert len(pending) == 1
    assert pending[0]["operation"] == "resume_pr"
    assert pending[0]["status"] == "unknown"
    assert len(client.resume_pr_calls) == 1

    # A fresh journal object models a restarted controller recovering the
    # durable unknown outcome. Recovery opens a gate and must not dispatch the
    # repair again.
    restarted_journal = EffectJournal("run-1", journal_path)
    restarted_store = RunStore("reconcile", tmp_path)
    restarted_state = _reconcile_action_journal(
        restarted_store.load(),
        restarted_store,
        restarted_journal,
    )
    key = pending[0]["key"]
    gate_name = _action_journal_gate_name(key)
    gate = next(gate for gate in restarted_state.gates if gate.name == gate_name)
    assert gate.status is GateStatus.PENDING
    assert len(client.resume_pr_calls) == 1
    assert restarted_journal.pending_entries()[0]["status"] == "unknown"

    # Replaying startup reconciliation with the same unresolved journal is
    # idempotent: it reuses the pending gate and still does not duplicate the
    # external repair effect.
    repeated = _reconcile_action_journal(
        restarted_store.load(),
        restarted_store,
        restarted_journal,
    )
    assert (
        next(gate for gate in repeated.gates if gate.name == gate_name).status is GateStatus.PENDING
    )
    assert len(client.resume_pr_calls) == 1


def test_reconciliation_rejects_stale_or_unresolved_review_evidence(tmp_path) -> None:
    cases = (
        {
            "review_id": 7,
            "review_verdict": "approved",
            "review_head_sha": "old-head",
            "reviewer_agent_id": "review-pr-99-codex",
        },
        {
            "review_id": 7,
            "review_verdict": "approved",
            "review_head_sha": "head-a",
            "reviewer_agent_id": "review-pr-99-codex",
            "reviewer_identity_error": "unresolved",
        },
        {
            "review_id": 7,
            "review_verdict": "approved",
            "review_head_sha": "head-a",
            "reviewer_agent_id": "agent-a",
        },
        {
            "review_id": 0,
            "review_verdict": "approved",
            "review_head_sha": "head-a",
            "reviewer_agent_id": "review-pr-99-codex",
        },
    )
    for evidence in cases:
        case_dir = tmp_path / str(len(list(tmp_path.iterdir())))
        case_dir.mkdir()
        store, _ = _load_state(case_dir)
        state = _review_recovery_state(store)
        client = FakeClient(**evidence)
        config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

        recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

        assert recovered.slices["slice-a"].verdict is None
        assert client.merge_calls == []
