"""Reviewed-head and freshness gate coverage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.fsm.event import ChildCompleted
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import (
    EffectIntent,
    TLLoopConfig,
    _merge_completed_leaf,
)
from tl_loop.loop.review import (
    CIStatusNotApproved,
    IntegrationEvidenceMismatch,
    MissingCIStatus,
    MissingPatchDigest,
    OptionalPolicyRejected,
    PatchDigestMismatch,
    ReviewHeadMismatch,
    StaleVerdict,
    integration_needs_revalidation,
    invalidate_integration_evidence,
    verify_integration,
    verify_review,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
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

    with pytest.raises(IntegrationEvidenceMismatch):
        verify_integration(state, **live)


def test_base_movement_only_requires_integration_revalidation() -> None:
    state = IntegrationRuntimeState(head_sha="head-a", patch_digest="patch-a", validated_base_sha="base-a")

    assert integration_needs_revalidation(
        state, base_sha="base-b", head_sha="head-a", patch_digest="patch-a"
    ) == "base_invalidated"
    assert integration_needs_revalidation(
        state, base_sha="base-a", head_sha="head-a", patch_digest="patch-b"
    ) == "head_invalidated"


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


def test_pre_merge_watcher_recheck_blocks_a_fix_pushed_after_verdict(
    tmp_path: Path,
) -> None:
    state, store = _state(tmp_path, "abc123", "2026-08-11T16:55:00Z")
    transport = RecordingTransport(current_head="def456")
    effects_log: list[EffectIntent] = []

    allowed = _merge_completed_leaf(
        _event(),
        _completion(),
        {"leaf"},
        set(),
        EffectClient(transport),
        TLLoopConfig(poll_interval=0.001),
        effects_log,
        state,
    )

    assert allowed is False
    assert [name for name, _ in transport.calls if name != "emit_controller_event"] == [
        "watcher_pr_state"
    ]
    assert effects_log[0].operation == "watcher_pr_state"
    assert store.load().slices["leaf"].verdict is not None


def test_matching_head_within_window_allows_merge(tmp_path: Path) -> None:
    state, _ = _state(tmp_path, "abc123", _fresh_verdict_at())
    transport = RecordingTransport(current_head="abc123")
    effects_log: list[EffectIntent] = []

    allowed = _merge_completed_leaf(
        _event(),
        _completion(),
        {"leaf"},
        set(),
        EffectClient(transport),
        TLLoopConfig(
            poll_interval=0.001,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        effects_log,
        state,
    )

    assert allowed is True
    assert [name for name, _ in transport.calls if name != "emit_controller_event"] == [
        "watcher_pr_state",
        "merge_pr",
    ]


def test_expired_matching_head_is_refused_before_merge(tmp_path: Path) -> None:
    state, _ = _state(tmp_path, "abc123", "2026-08-11T00:00:00Z")
    transport = RecordingTransport(current_head="abc123")
    effects_log: list[EffectIntent] = []

    allowed = _merge_completed_leaf(
        _event(),
        _completion(),
        {"leaf"},
        set(),
        EffectClient(transport),
        TLLoopConfig(
            poll_interval=0.001,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        effects_log,
        state,
    )

    assert allowed is False
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
    return {
        "version": 1,
        "revision": 0,
        "run_id": "review-test",
        "fsm": {"phase": TLPhase.TLWaiting.value, "waiting": ["leaf"]},
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


def _state(tmp_path: Path, head: str, verdict_at: str) -> tuple[RunState, RunStore]:
    document = _document()
    record = cast(dict[str, object], cast(dict[str, object], document["slices"])["leaf"])
    record["reviewed_head"] = head
    record["verdict"] = Verdict.GO.value
    record["ci_state"] = {head: "success"}
    record["verdict_at"] = verdict_at
    root_spec = {
        key: document[key]
        for key in ("fsm", "slices", "budgets", "gates", "events")
    }
    create("review-test", root_spec, root_dir=tmp_path)
    store = RunStore("review-test", root_dir=tmp_path)
    return store.load(), store


def _event() -> EventEnvelope:
    raw = {
        "schema_version": 1,
        "event_id": "review-1",
        "id": "review-1",
        "event_time": "2026-08-11T17:00:00Z",
        "observed_at": "2026-08-11T17:00:00Z",
        "run_seq": 1,
        "type": "agent.notify_parent",
        "agent_id": "leaf",
        "run_id": "review-test",
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": {"pr_number": 42},
    }
    return project(cast(dict[str, object], raw))


def _completion() -> ChildCompleted:
    return ChildCompleted("leaf")


@dataclass
class RecordingTransport:
    current_head: str
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
            return {
                "success": True,
                "result": {
                    "found": True,
                    "head_sha": self.current_head,
                },
            }
        return {"success": True, "result": None}
