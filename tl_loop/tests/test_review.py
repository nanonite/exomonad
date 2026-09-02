"""Reviewed-head and freshness gate coverage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.fsm.lane import (
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneReserved,
)
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.convergence import ConvergenceTracker
from tl_loop.loop.driver import (
    EffectIntent,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _apply_convergence,
    _execute_direct_merge_intent,
    _merge_recovery_gate_name,
    _open_integration_gate,
    _reconcile_action_journal,
    _reconcile_legacy_parked_lanes,
    _reconcile_nonterminal_slices,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.reconcile import ExternalIntent
from tl_loop.loop.review import (
    CIStatusNotApproved,
    IntegrationEvidenceMismatch,
    MissingCIStatus,
    MissingPatchDigest,
    OptionalPolicyRejected,
    PatchDigestMismatch,
    ReviewContract,
    ReviewGateError,
    ReviewHeadMismatch,
    StaleVerdict,
    integration_needs_revalidation,
    invalidate_integration_evidence,
    load_reviewer_max_rounds,
    verdict_is_stale,
    verify_integration,
    verify_review,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import (
    SCHEMA_VERSION,
    ActionKind,
    ActionPhase,
    ActionState,
    GateStatus,
    IntegrationRuntimeState,
    RunState,
    SchemaError,
    SliceState,
    SliceStatus,
    Verdict,
    validate,
)
from tl_loop.state.store import RunStore, create

NOW = datetime(2026, 8, 11, 17, 0, tzinfo=UTC)


def test_reviewer_max_rounds_honors_session_override(monkeypatch, tmp_path: Path) -> None:
    policy = tmp_path / "review-policy.toml"
    policy.write_text("reviewer_max_rounds = 7\n", encoding="utf-8")

    assert load_reviewer_max_rounds(policy) == 7

    monkeypatch.setenv("EXOMONAD_REVIEWER_MAX_ROUNDS", "2")
    assert load_reviewer_max_rounds(policy) == 2

    monkeypatch.setenv("EXOMONAD_REVIEWER_MAX_ROUNDS", "0")
    with pytest.raises(ReviewGateError, match="must be at least 1"):
        load_reviewer_max_rounds(policy)


def test_review_contract_normalizes_criteria_and_binds_digest() -> None:
    contract = ReviewContract.from_criteria((" verify ", "verify", "boundary"))

    assert contract.acceptance_criteria == ("verify", "boundary")
    assert ReviewContract.from_mapping(contract.as_mapping()) == contract
    with pytest.raises(ValueError, match="digest"):
        ReviewContract.from_mapping(
            {"acceptance_criteria": ["verify", "boundary"], "digest": "stale"}
        )


def test_verdict_without_reviewed_head_fails_schema_validation() -> None:
    document = _document()
    record = cast(dict[str, object], cast(dict[str, object], document["slices"])["leaf"])
    record["verdict"] = Verdict.GO.value

    with pytest.raises(SchemaError, match="reviewed_head"):
        validate(document)


def test_matching_head_within_freshness_window_is_accepted() -> None:
    evidence = verify_review(
        _slice(verdict_at="2026-08-11T16:55:00Z"),
        "abc123",
        now=NOW,
        freshness_window_secs=600,
    )

    assert evidence.reviewed_head == "abc123"
    assert evidence.age_seconds == 300


def test_canonical_rule_does_not_require_review_timestamp() -> None:
    evidence = verify_review(_slice(verdict_at=None), "abc123")

    assert evidence.reviewed_head == "abc123"
    assert evidence.age_seconds == 0.0


def test_missing_ci_status_rejects_the_reviewed_head() -> None:
    with pytest.raises(MissingCIStatus, match="no CI status"):
        verify_review(_slice(verdict_at=None, ci_status=None), "abc123")


def test_failed_ci_status_rejects_the_reviewed_head() -> None:
    with pytest.raises(CIStatusNotApproved, match="failure"):
        verify_review(_slice(verdict_at=None, ci_status="failure"), "abc123")


def test_neutral_ci_status_satisfies_the_canonical_ci_gate() -> None:
    evidence = verify_review(
        _slice(verdict_at=None, ci_status="neutral"),
        "abc123",
    )

    assert evidence.reviewed_head == "abc123"


def test_optional_policy_predicate_is_not_an_implicit_gate() -> None:
    with pytest.raises(OptionalPolicyRejected, match="optional review policy"):
        verify_review(
            _slice(verdict_at=None),
            "abc123",
            policy_predicate=lambda _slice: False,
        )


def test_changed_head_rejects_the_verdict() -> None:
    with pytest.raises(ReviewHeadMismatch, match="current head"):
        verify_review(
            _slice(verdict_at="2026-08-11T16:55:00Z"),
            "def456",
            now=NOW,
            freshness_window_secs=600,
        )


def test_changed_patch_digest_rejects_reused_head() -> None:
    reviewed = replace(_slice(verdict_at=None), review_patch_digests={"abc123": "patch-a"})

    with pytest.raises(PatchDigestMismatch, match="current patch"):
        verify_review(reviewed, "abc123", current_patch_digest="patch-b")


def test_missing_live_patch_digest_rejects_bound_review() -> None:
    reviewed = replace(_slice(verdict_at=None), review_patch_digests={"abc123": "patch-a"})

    with pytest.raises(MissingPatchDigest, match="no live patch digest"):
        verify_review(reviewed, "abc123")


def test_matching_patch_digest_accepts_bound_review() -> None:
    reviewed = replace(_slice(verdict_at=None), review_patch_digests={"abc123": "patch-a"})

    evidence = verify_review(reviewed, "abc123", current_patch_digest="patch-a")

    assert evidence.patch_digest == "patch-a"


def test_integration_evidence_requires_exact_base_head_tree_and_ci() -> None:
    state = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
        stage_verification="passed",
        integration_evidence_at="2026-08-11T17:00:00Z",
    )

    evidence = verify_integration(
        state,
        base_sha="base-a",
        head_sha="head-a",
        patch_digest="patch-a",
        merge_tree_sha="tree-a",
        ci_status="success",
    )

    assert evidence.merge_tree_sha == "tree-a"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("base_sha", "base-b"),
        ("head_sha", "head-b"),
        ("patch_digest", "patch-b"),
        ("merge_tree_sha", "tree-b"),
        ("ci_status", "failure"),
    ),
)
def test_integration_evidence_rejects_each_changed_dimension(field: str, value: str) -> None:
    state = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
        stage_verification="passed",
        integration_evidence_at="2026-08-11T17:00:00Z",
    )
    live = {
        "base_sha": "base-a",
        "head_sha": "head-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": "tree-a",
        "ci_status": "success",
    }
    live[field] = value

    with pytest.raises(IntegrationEvidenceMismatch) as error_info:
        verify_integration(state, **live)
    expected_field = "validated_base_sha" if field == "base_sha" else field
    assert error_info.value.field == expected_field


def test_base_movement_only_requires_integration_revalidation() -> None:
    state = IntegrationRuntimeState(
        head_sha="head-a", patch_digest="patch-a", validated_base_sha="base-a"
    )

    assert (
        integration_needs_revalidation(
            state, base_sha="base-b", head_sha="head-a", patch_digest="patch-a"
        )
        == "base_invalidated"
    )
    assert (
        integration_needs_revalidation(
            state, base_sha="base-a", head_sha="head-a", patch_digest="patch-b"
        )
        == "head_invalidated"
    )


def test_base_movement_clears_only_integration_authority() -> None:
    state = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
        stage_verification="passed",
        integration_evidence_at="2026-08-11T17:00:00Z",
    )

    invalidated = invalidate_integration_evidence(
        state,
        base_sha="base-b",
        head_sha="head-a",
        patch_digest="patch-a",
    )

    assert invalidated.lifecycle is IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    assert invalidated.head_sha == "head-a"
    assert invalidated.patch_digest == "patch-a"
    assert invalidated.merge_tree_sha is None
    assert invalidated.ci_status == "unknown"


def test_head_or_patch_movement_clears_review_and_integration_authority() -> None:
    state = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
        stage_verification="passed",
        integration_evidence_at="2026-08-11T17:00:00Z",
    )

    invalidated = invalidate_integration_evidence(
        state,
        base_sha="base-a",
        head_sha="head-b",
        patch_digest="patch-b",
    )

    assert invalidated.lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE
    assert invalidated.head_sha is None
    assert invalidated.patch_digest is None


def test_expired_matching_verdict_requires_review() -> None:
    with pytest.raises(StaleVerdict, match="exceeds"):
        verify_review(
            _slice(verdict_at="2026-08-11T15:00:00Z"),
            "abc123",
            now=NOW,
            freshness_window_secs=600,
        )


def test_verdict_is_stale_reports_true_only_past_the_freshness_window() -> None:
    fresh = _slice(verdict_at="2026-08-11T16:55:00Z")
    expired = _slice(verdict_at="2026-08-11T15:00:00Z")

    assert verdict_is_stale(fresh, now=NOW, freshness_window_secs=600) is False
    assert verdict_is_stale(expired, now=NOW, freshness_window_secs=600) is True


def test_verdict_is_stale_is_false_without_a_verdict_or_disabled_window() -> None:
    no_verdict = replace(_slice(verdict_at=None), verdict=None)
    unbounded = _slice(verdict_at="2026-08-11T15:00:00Z")

    assert verdict_is_stale(no_verdict, now=NOW, freshness_window_secs=600) is False
    assert verdict_is_stale(unbounded, now=NOW, freshness_window_secs=None) is False


def test_pre_merge_watcher_recheck_blocks_a_fix_pushed_after_verdict(
    tmp_path: Path,
) -> None:
    state, store = _state(tmp_path, "abc123", "2026-08-11T16:55:00Z")
    transport = DirectMergeTransport(snapshots=[_snapshot(head_sha="def456")])
    effects_log: list[EffectIntent] = []

    result = _run_direct_merge(
        state,
        store,
        transport,
        TLLoopConfig(poll_interval=0.001),
        effects_log,
    )

    assert result.slices["leaf"].status is SliceStatus.IN_REVIEW
    assert [name for name, _ in transport.calls if name != "emit_controller_event"] == [
        "watcher_pr_state"
    ]
    assert effects_log[0].operation == "watcher_pr_state"
    assert store.load().slices["leaf"].verdict is not None


def test_matching_head_within_window_allows_merge(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    transport = DirectMergeTransport(
        snapshots=[
            _snapshot(head_sha="abc123"),
            {
                "merged": True,
                "head_sha": "abc123",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
                "pr_state": "closed",
            },
        ]
    )
    effects_log: list[EffectIntent] = []

    result = _run_direct_merge(
        state,
        store,
        transport,
        TLLoopConfig(
            poll_interval=0.001,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        effects_log,
    )

    assert result.slices["leaf"].status is SliceStatus.MERGED
    lane = store.load().integration.lanes["org/repo:main"]
    assert lane.child_id is None
    assert lane.phase is LanePhase.IDLE
    assert [name for name, _ in transport.calls if name != "emit_controller_event"] == [
        "watcher_pr_state",
        "merge_pr",
        "watcher_pr_state",
        "post_merge_parent_sync",
    ]
    merge_arguments = next(arguments for name, arguments in transport.calls if name == "merge_pr")
    assert merge_arguments == {
        "pr_number": 42,
        "expected_base_sha": "base-a",
        "expected_head_sha": "abc123",
        "expected_patch_digest": "patch-a",
        "expected_merge_tree_sha": "tree-a",
    }


def test_unknown_merge_restart_resolves_lane_and_finishes_without_remerge(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    state = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "abc123"))
    journal = EffectJournal("review-test", store.run_dir / "action-journal.json")

    class LostMergeTransport(DirectMergeTransport):
        watcher_count: int = 0
        merge_count: int = 0

        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "watcher_pr_state":
                self.calls.append((tool_name, arguments))
                self.watcher_count += 1
                if self.watcher_count == 1:
                    return {"success": True, "result": _snapshot(head_sha="abc123")}
                return {"success": False, "error": "watcher unavailable"}
            if tool_name == "merge_pr":
                self.calls.append((tool_name, arguments))
                self.merge_count += 1
                raise RuntimeError("merge response lost")
            return super().call_tool(role, name, tool_name, arguments)

    lost = LostMergeTransport()
    first = _run_direct_merge(
        state,
        store,
        lost,
        TLLoopConfig(active=True, chainlink_issue_id=599),
        journal,
    )

    assert first.slices["leaf"].action is not None
    assert first.slices["leaf"].action.phase.value == "unknown"
    assert first.integration.lanes["org/repo:main"].phase is LanePhase.RECOVERY
    assert lost.merge_count == 1

    class RecoveryTransport(DirectMergeTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            del role, name
            self.calls.append((tool_name, arguments))
            if tool_name == "watcher_pr_state":
                return {
                    "success": True,
                    "result": {
                        "merged": True,
                        "pr_number": 42,
                        "head_sha": "abc123",
                        "base_sha": "base-a",
                        "pr_state": "closed",
                    },
                }
            if tool_name == "post_merge_parent_sync":
                return {
                    "success": True,
                    "result": {
                        **arguments,
                        "parent_commit_sha": "parent-a",
                        "remote_head_sha": "parent-a",
                        "ancestry_proof": "ancestor:abc123->parent-a",
                    },
                }
            if tool_name == "chainlink_issue_close":
                return {
                    "success": True,
                    "result": {"issue_id": arguments["issue_id"], "receipt_id": "close-599"},
                }
            if tool_name == "post_merge_changelog":
                return {
                    "success": True,
                    "result": {**arguments, "commit_sha": "changelog-a"},
                }
            if tool_name == "post_merge_push":
                return {
                    "success": True,
                    "result": {
                        **arguments,
                        "push_receipt_id": "receipt-a",
                        "observed_remote_head": arguments["pushed_commit"],
                        "ancestry_proof": (
                            f"ancestor:{arguments['pushed_commit']}->{arguments['pushed_commit']}"
                        ),
                    },
                }
            return {"success": True, "result": None}

    recovery = RecoveryTransport()
    restarted = _reconcile_action_journal(
        store.load(),
        store,
        EffectJournal("review-test", store.run_dir / "action-journal.json"),
        effects=EffectClient(recovery),
    )
    assert restarted.integration.lanes["org/repo:main"].phase is LanePhase.INTEGRATING

    config = TLLoopConfig(active=True, chainlink_issue_id=599)
    for _ in range(8):
        restarted = _apply_convergence(
            store.load(),
            ConvergenceTracker(),
            store,
            config,
            EffectClient(recovery),
            EffectJournal("review-test", store.run_dir / "action-journal.json"),
        )
        if restarted.slices["leaf"].post_merge is not None and (
            restarted.slices["leaf"].post_merge.phase.value == "complete"
        ):
            break

    assert restarted.slices["leaf"].post_merge is not None
    assert restarted.slices["leaf"].post_merge.phase.value == "complete"
    assert restarted.integration.lanes["org/repo:main"].phase is LanePhase.IDLE
    assert lost.merge_count == 1
    assert [name for name, _ in recovery.calls].count("merge_pr") == 0


def test_authoritative_nonmerge_clears_pending_action_and_releases_lane(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    state = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "abc123"))
    current = replace(
        state.slices["leaf"],
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="merge-intent",
            head_sha="abc123",
            attempt=1,
        ),
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=state.integration,
    )
    journal = EffectJournal("review-test", store.run_dir / "action-journal.json")
    journal.append(EffectIntent("merge_pr", "leaf", {"pr_number": 42}, True))

    transport = DirectMergeTransport(
        snapshots=[{"merged": False, "pr_number": 42, "pr_state": "open"}]
    )
    reconciled = _reconcile_action_journal(
        state,
        store,
        journal,
        effects=EffectClient(transport),
    )

    assert reconciled.slices["leaf"].action is None
    assert reconciled.integration.lanes["org/repo:main"].phase is LanePhase.IDLE
    assert journal.pending_entries() == []
    assert journal.snapshot()[0]["status"] == "compensated"


def test_direct_merge_waits_for_an_existing_parent_lane(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("other", 1, "base-a"))
    transport = DirectMergeTransport(snapshots=[_snapshot(head_sha="abc123")])
    effects_log: list[EffectIntent] = []

    result = _run_direct_merge(
        state,
        store,
        transport,
        TLLoopConfig(poll_interval=0.001),
        effects_log,
    )

    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    lane = result.integration.lanes["org/repo:main"]
    assert lane.child_id == "other"
    assert lane.phase is LanePhase.RESERVED


def test_integration_gate_releases_the_owned_parent_lane(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    transport = DirectMergeTransport()

    result = _open_integration_gate(
        SubTLTask("leaf", WorkPlan()),
        state,
        TLLoopConfig(poll_interval=0.001),
        EffectClient(transport),
        store,
        [],
        gate_name="tl-integration-conflict",
        lifecycle=IntegrationLifecycle.INTEGRATION_CONFLICT,
        reason="conflicting parent update",
    )

    lane = result.integration.lanes["org/repo:main"]
    assert lane.child_id is None
    assert lane.phase is LanePhase.IDLE
    assert store.load().integration.lanes["org/repo:main"].phase is LanePhase.IDLE


def test_continuation_releases_legacy_parked_deterministic_lane(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    state = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "abc123"))
    state = store.transition_lane(
        "org/repo",
        "main",
        LaneParkRequested("legacy_gate", "persisted before lane recovery migration"),
    )

    migrated = _reconcile_legacy_parked_lanes(state, store)

    assert migrated.integration.lanes["org/repo:main"].phase is LanePhase.IDLE
    assert store.load().integration.lanes["org/repo:main"].phase is LanePhase.IDLE


def test_continuation_keeps_legacy_parked_unknown_merge_in_recovery(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    state = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "abc123"))
    state = store.transition_lane(
        "org/repo",
        "main",
        LaneParkRequested("legacy_unknown", "merge response was lost before migration"),
    )
    current = replace(
        state.slices["leaf"],
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="merge-intent",
            head_sha="abc123",
            attempt=1,
        ),
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=state.integration,
    )

    migrated = _reconcile_legacy_parked_lanes(state, store)

    assert migrated.integration.lanes["org/repo:main"].phase is LanePhase.RECOVERY
    assert store.load().integration.lanes["org/repo:main"].phase is LanePhase.RECOVERY


def test_journal_less_legacy_unknown_merge_reaches_operator_terminal_gate(
    tmp_path: Path,
) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    state = store.transition_lane("org/repo", "main", LaneReserved("leaf", 1, "base-a"))
    state = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "abc123"))
    state = store.transition_lane(
        "org/repo",
        "main",
        LaneParkRequested("legacy_unknown", "merge response was lost before migration"),
    )
    current = replace(
        state.slices["leaf"],
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="legacy-merge-intent",
            head_sha="abc123",
            attempt=1,
        ),
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=state.integration,
    )
    migrated = _reconcile_legacy_parked_lanes(state, store)
    config = TLLoopConfig(active=False)

    waiting = _reconcile_nonterminal_slices(
        WorkPlan(),
        migrated,
        config,
        EffectClient(DirectMergeTransport()),
        store,
        [],
    )
    gate_name = _merge_recovery_gate_name(migrated, migrated.slices["leaf"])
    assert (
        next(gate for gate in waiting.gates if gate.name == gate_name).status is GateStatus.PENDING
    )
    assert waiting.slices["leaf"].action is not None
    assert waiting.integration.lanes["org/repo:main"].phase is LanePhase.RECOVERY

    store.answer_gate(gate_name, GateStatus.APPROVED)
    approved = _reconcile_nonterminal_slices(
        WorkPlan(),
        store.load(),
        config,
        EffectClient(DirectMergeTransport()),
        store,
        [],
    )
    assert approved.slices["leaf"].action is None
    assert approved.integration.lanes["org/repo:main"].phase is LanePhase.IDLE

    approved = store.transition_lane(
        "org/repo",
        "main",
        LaneReserved("leaf", 2, "base-b"),
    )
    approved = store.transition_lane("org/repo", "main", LaneIntegrationStarted("leaf", "def456"))
    second_current = replace(
        approved.slices["leaf"],
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="legacy-merge-intent-2",
            head_sha="def456",
            attempt=2,
        ),
    )
    approved = store.checkpoint(
        approved.fsm,
        {**approved.slices, "leaf": second_current},
        approved.budgets,
        approved.events.last_consumed_offset,
        integration=approved.integration,
    )
    second = _reconcile_nonterminal_slices(
        WorkPlan(),
        approved,
        config,
        EffectClient(DirectMergeTransport()),
        store,
        [],
    )
    second_gate_name = _merge_recovery_gate_name(second, second.slices["leaf"])
    assert second_gate_name != gate_name
    assert (
        next(gate for gate in second.gates if gate.name == gate_name).status is GateStatus.APPROVED
    )
    assert (
        next(gate for gate in second.gates if gate.name == second_gate_name).status
        is GateStatus.PENDING
    )
    assert second.slices["leaf"].action is not None

    store.answer_gate(second_gate_name, GateStatus.REJECTED)
    terminal = _reconcile_nonterminal_slices(
        WorkPlan(),
        store.load(),
        config,
        EffectClient(DirectMergeTransport()),
        store,
        [],
    )

    assert terminal.slices["leaf"].action is None
    assert terminal.slices["leaf"].status is SliceStatus.PARKED
    assert terminal.integration.lanes["org/repo:main"].phase is LanePhase.IDLE


def test_merge_recovery_gate_hash_separates_hyphenated_slice_and_intent_ids(
    tmp_path: Path,
) -> None:
    state, _ = _state(tmp_path, "abc123", _fresh_verdict_at())
    first = replace(
        state.slices["leaf"],
        id="a",
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="b-c",
            head_sha="abc123",
            attempt=1,
        ),
    )
    second = replace(
        state.slices["leaf"],
        id="a-b",
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="c",
            head_sha="abc123",
            attempt=1,
        ),
    )

    first_gate = _merge_recovery_gate_name(state, first)
    second_gate = _merge_recovery_gate_name(state, second)

    assert first_gate != second_gate
    assert first_gate.startswith("tl-merge-recovery-")
    assert second_gate.startswith("tl-merge-recovery-")


def test_missing_direct_compare_evidence_opens_integrity_gate(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", _fresh_verdict_at())
    transport = DirectMergeTransport(snapshots=[_snapshot(head_sha="abc123", merge_tree_sha=None)])
    effects_log: list[EffectIntent] = []

    result = _run_direct_merge(
        state,
        store,
        transport,
        TLLoopConfig(poll_interval=0.001),
        effects_log,
    )

    assert result.slices["leaf"].status is SliceStatus.IN_REVIEW
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert any(gate.name == "tl-integrity-reconciliation" for gate in store.load().gates)


def test_expired_matching_head_is_refused_before_merge(tmp_path: Path) -> None:
    state, store = _state(tmp_path, "abc123", "2026-08-11T00:00:00Z")
    transport = DirectMergeTransport(snapshots=[_snapshot(head_sha="abc123")])
    effects_log: list[EffectIntent] = []

    result = _run_direct_merge(
        state,
        store,
        transport,
        TLLoopConfig(
            poll_interval=0.001,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        effects_log,
    )

    assert result.slices["leaf"].status is SliceStatus.IN_REVIEW
    assert [name for name, _ in transport.calls if name != "emit_controller_event"] == [
        "watcher_pr_state"
    ]


def _fresh_verdict_at() -> str:
    return (datetime.now(UTC) - timedelta(seconds=30)).isoformat()


def _slice(
    *,
    verdict_at: str | None,
    ci_status: str | None = "success",
) -> SliceState:
    return SliceState(
        id="leaf",
        status=SliceStatus.IN_REVIEW,
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-test",
        branch="task/leaf",
        worktree=None,
        pr_number=42,
        reviewed_head="abc123",
        attempts=1,
        verdict=Verdict.GO,
        ci_state={} if ci_status is None else {"abc123": ci_status},
        verdict_at=verdict_at,
    )


def _document() -> dict[str, object]:
    document = {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "review-test",
        "fsm": {"phase": TLPhase.TLPlanning.value, "waiting": []},
        "slices": {
            "leaf": {
                "id": "leaf",
                "status": SliceStatus.IN_REVIEW.value,
                "paths": ["src/leaf.py"],
                "depends_on": [],
                "base_ref": "main",
                "test_plan": ["just tl-loop-test"],
                "agent_type": "codex",
                "model": "gpt-test",
                "branch": "task/leaf",
                "worktree": None,
                "pr_number": 42,
                "reviewed_head": None,
                "attempts": 1,
                "verdict": None,
            }
        },
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }
    manifest = build_plan_manifest(
        {"workers": [{"name": "leaf", "task": "legacy"}]},
        scope_id="review-test",
        owned_branch="legacy",
    )
    document["plan_manifest"] = manifest.to_document()
    cast(dict[str, object], document["slices"])["leaf"].update(
        {"manifest_node_id": manifest.nodes[0].node_id, "manifest_revision": 1}
    )
    return document


def _state(tmp_path: Path, head: str, verdict_at: str) -> tuple[RunState, RunStore]:
    document = _document()
    record = cast(dict[str, object], cast(dict[str, object], document["slices"])["leaf"])
    record["reviewed_head"] = head
    record["verdict"] = Verdict.GO.value
    record["ci_state"] = {head: "success"}
    record["verdict_at"] = verdict_at
    root_spec = {key: document[key] for key in ("fsm", "slices", "budgets", "gates", "events")}
    root_spec["repository_identity"] = {
        "owner": "org",
        "repo": "repo",
        "base_branch": "main",
    }
    create("review-test", root_spec, root_dir=tmp_path)
    store = RunStore("review-test", root_dir=tmp_path)
    return store.load(), store


@dataclass
class DirectMergeTransport:
    snapshots: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "watcher_pr_state":
            if not self.snapshots:
                raise AssertionError("unexpected watcher_pr_state call")
            return {"success": True, "result": self.snapshots.pop(0)}
        if tool_name == "merge_pr":
            return {"success": True, "result": {"merged": True}}
        return {"success": True, "result": None}


def _snapshot(
    *,
    head_sha: str,
    merge_tree_sha: str | None = "tree-a",
) -> dict[str, object]:
    return {
        "found": True,
        "head_sha": head_sha,
        "base_sha": "base-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": merge_tree_sha,
        "ci_status": "success",
    }


def _run_direct_merge(
    state: RunState,
    store: RunStore,
    transport: DirectMergeTransport,
    config: TLLoopConfig,
    effects_log: list[EffectIntent],
) -> RunState:
    intent = ExternalIntent(
        "merge",
        "leaf",
        {"pr_number": 42, "head_sha": state.slices["leaf"].reviewed_head},
    )
    return _execute_direct_merge_intent(
        state,
        intent,
        ConvergenceTracker(),
        store,
        config,
        EffectClient(transport),
        effects_log,
    )
