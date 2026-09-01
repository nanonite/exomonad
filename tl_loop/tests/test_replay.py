"""Golden replay tests for complete active-loop trajectories."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.events.envelope import project
from tl_loop.events.replay import ReplayEventSource, ReplayTruncated
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import ChildKind, ChildRecord, TLRunning
from tl_loop.loop.driver import EffectIntent, TLLoopConfig, tl_run
from tl_loop.loop.journal import MUTATING_OPERATIONS, EffectJournal
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmResponse
from tl_loop.select.policy import load_policy
from tl_loop.state.schema import (
    DurableReviewEvidence,
    HandoffEvidence,
    PublicationBinding,
    RepositoryIdentity,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.replay import (
    FIXTURE_ROOT,
    POLICY_ROOT,
    RecordingTransport,
    expected_actions,
    expected_state,
    normalize_durable_state,
    replay_fixture,
)

REPLAYS = (
    "clean-two-slice.json",
    "no-go-repair.json",
    "retry-exhausted.json",
)


@pytest.mark.parametrize("fixture_name", REPLAYS)
def test_recorded_stream_replays_exact_actions_and_state(fixture_name: str, tmp_path: Path) -> None:
    first = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "first")
    second = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "second")

    assert first.actions == expected_actions(FIXTURE_ROOT / fixture_name)
    assert _canonical(first.state) == _canonical(expected_state(FIXTURE_ROOT / fixture_name))
    assert first.actions == second.actions
    assert _canonical(first.state) == _canonical(second.state)


def test_permuted_ledger_rows_replay_to_the_same_durable_position(tmp_path: Path) -> None:
    baseline = replay_fixture(FIXTURE_ROOT / "no-go-repair.json", tmp_path / "baseline")
    permuted = replay_fixture(
        FIXTURE_ROOT / "no-go-repair.json",
        tmp_path / "permuted",
        event_transform=lambda events: list(reversed(events)),
    )

    assert permuted.actions == baseline.actions
    assert permuted.durable_state == baseline.durable_state
    assert permuted.cursor == baseline.cursor
    assert permuted.reducer_version == baseline.reducer_version
    assert permuted.transitions == baseline.transitions


def test_replay_source_preserves_event_identity_and_resumes_after_cursor() -> None:
    raw = json.loads((FIXTURE_ROOT / "clean-two-slice.json").read_text(encoding="utf-8"))
    events = [project(value) for value in raw["events"]]
    source = ReplayEventSource([events[2], events[1], events[1]], start_cursor=1)

    first = source.get()
    assert first.run_seq == 2
    assert first.event_id == "replay-2"
    assert first.identity[2] == first.event_id
    source.acknowledge(first)
    assert source.cursor == 2
    assert source.get().run_seq == 2


def test_duplicate_ledger_rows_are_acknowledged_without_reducing_twice(
    tmp_path: Path,
) -> None:
    baseline = replay_fixture(FIXTURE_ROOT / "clean-two-slice.json", tmp_path / "baseline")
    duplicate = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        tmp_path / "duplicate",
        event_transform=lambda rows: [*rows, rows[2]],
    )

    assert duplicate.actions == baseline.actions
    assert duplicate.durable_state == baseline.durable_state
    assert duplicate.cursor == baseline.cursor
    assert duplicate.acknowledged.count(3) == 2
    assert duplicate.transitions == baseline.transitions


def test_truncated_ledger_prefix_fails_closed_with_cursor_context(tmp_path: Path) -> None:
    with pytest.raises(ReplayTruncated) as error:
        replay_fixture(
            FIXTURE_ROOT / "clean-two-slice.json",
            tmp_path / "truncated",
            event_transform=lambda rows: rows[:-2],
        )

    assert error.value.cursor == 3
    assert error.value.consumed == 3


def test_journal_probe_and_repeated_continuation_are_noops(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    first = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        root,
        journal=True,
        production_clock=True,
        session_mode="continue",
    )
    checkpoint = (root / "replay-clean.controller-epoch").parent / "replay-clean" / "run.json"
    before = checkpoint.read_bytes()
    (root / "replay-clean.controller-epoch").write_text("continued-controller-epoch\n")
    second = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        root,
        journal=True,
        production_clock=True,
        session_mode="continue",
    )

    assert first.journal_entries
    assert all(action["operation"] not in MUTATING_OPERATIONS for action in second.actions)
    assert second.cursor == first.cursor
    assert second.durable_state == first.durable_state
    assert second.journal_entries == first.journal_entries
    assert checkpoint.read_bytes() == before


def test_recursive_replay_preserves_position_and_ownership_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    live = replay_fixture(
        FIXTURE_ROOT / "recursive-position.json",
        tmp_path / "recursive-live",
        journal=True,
        live_ledger=True,
    )
    replay_root = tmp_path / "recursive-replay"
    first = replay_fixture(FIXTURE_ROOT / "recursive-position.json", replay_root, journal=True)
    second = replay_fixture(FIXTURE_ROOT / "recursive-position.json", replay_root, journal=True)

    assert _normalize_recovery_state(
        normalize_durable_state(live.durable_state), tmp_path / "recursive-live"
    ) == _normalize_recovery_state(normalize_durable_state(first.durable_state), replay_root)
    assert live.actions == first.actions
    assert first.durable_state == second.durable_state
    assert second.actions == ()
    assert second.cursor == first.cursor == 0
    assert second.journal_entries == first.journal_entries

    manifest = first.durable_state["plan_manifest"]
    assert isinstance(manifest, dict)
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    node_names = {node["name"] for node in nodes}
    assert node_names == {"root-leaf", "stage-a", "stage-b"}

    slices = first.durable_state["slices"]
    assert isinstance(slices, dict)
    assert {"root-leaf", "stage-a", "stage-b"}.issubset(slices)
    assert slices["stage-a"]["dispatch_agent_id"] == "stage-a"
    assert slices["stage-a"]["dispatch_intent_id"]
    assert first.durable_state["ordered_stages"] == [
        {"order": 1, "sub_tls": ["stage-a"]},
        {"order": 2, "sub_tls": ["stage-b"]},
    ]

    child = json.loads(
        (replay_root / "replay-recursive" / "stage-a" / "run.json").read_text(encoding="utf-8")
    )
    child_manifest = child["plan_manifest"]
    child_names = {node["name"] for node in child_manifest["nodes"]}
    assert child_names == {"child-leaf", "nested-a", "nested-b"}
    assert child["slices"]["child-leaf"]["manifest_node_id"]
    assert child["ordered_stages"] == [
        {"order": 1, "sub_tls": ["nested-a", "nested-b"]},
    ]


def test_recursive_replay_replays_review_checkpoint_and_journal_once(tmp_path: Path) -> None:
    live = replay_fixture(
        FIXTURE_ROOT / "recursive-recovery.json",
        tmp_path / "recursive-recovery-live",
        journal=True,
        live_ledger=True,
    )
    root = tmp_path / "recursive-recovery-replay"
    first = replay_fixture(FIXTURE_ROOT / "recursive-recovery.json", root, journal=True)
    second = replay_fixture(FIXTURE_ROOT / "recursive-recovery.json", root, journal=True)

    assert _normalize_recovery_state(
        normalize_durable_state(live.durable_state), tmp_path / "recursive-recovery-live"
    ) == _normalize_recovery_state(normalize_durable_state(first.durable_state), root)
    assert live.actions == first.actions
    assert first.cursor == second.cursor == 0
    assert second.actions == ()
    assert second.durable_state == first.durable_state
    assert second.journal_entries == first.journal_entries

    child = json.loads(
        (root / "replay-recovery" / "stage-a" / "run.json").read_text(encoding="utf-8")
    )
    assert child["slices"]["child-leaf"]["reviewed_head"] == "head-a"
    assert child["slices"]["child-leaf"]["pr_number"] == 42
    assert child["events"]["last_consumed_offset"] == 6
    assert child["slices"]["child-leaf"]["manifest_node_id"]


@pytest.mark.parametrize(
    "operation",
    [
        "resume_pr",
        "post_merge_parent_sync",
        "post_merge_issue_close",
        "post_merge_changelog",
        "post_merge_push",
    ],
)
def test_replay_probes_each_recovery_boundary_without_redispatch(
    tmp_path: Path, operation: str
) -> None:
    journal_path = tmp_path / "action-journal.json"
    intent = EffectIntent(operation, "recursive-child", {"head_sha": "head-a"}, True)
    result = ToolResult.from_raw(
        {"success": True, "result": {"operation": operation, "head_sha": "head-a"}}
    )

    first = EffectJournal("replay-recovery", journal_path)
    first.append(intent)
    first.mark_result(intent, result)

    restarted = EffectJournal("replay-recovery", journal_path)
    probe = restarted.probe(intent)
    assert probe.status == "confirmed"
    assert probe.is_terminal
    assert probe.result is not None
    assert probe.result.success is True
    assert restarted.probe(intent) == probe


@pytest.mark.parametrize("crash_after", ["spawn_worker", "spawn_leaf"])
def test_tl_run_restart_after_confirmed_effect_matches_live_replay(
    tmp_path: Path, crash_after: str
) -> None:
    crashed_root = tmp_path / "crashed"
    with pytest.raises(RuntimeError, match=f"after {crash_after}"):
        replay_fixture(
            FIXTURE_ROOT / "clean-two-slice.json",
            crashed_root,
            journal=True,
            crash_after=crash_after,
        )

    resumed = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        crashed_root,
        journal=True,
    )
    baseline = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        tmp_path / "baseline",
        journal=True,
    )

    assert resumed.durable_state == baseline.durable_state
    assert resumed.cursor == baseline.cursor
    assert resumed.reducer_version == baseline.reducer_version
    assert not any(action["operation"] == crash_after for action in resumed.actions)


RECOVERY_BOUNDARIES = (
    "resume_pr",
    "post_merge_parent_sync",
    "post_merge_issue_close",
    "post_merge_changelog",
    "post_merge_push",
)

RECOVERY_EFFECT_TO_TOOL = {
    "post_merge_issue_close": "chainlink_issue_close",
}


class _RepairBackend:
    def complete(self, request: object) -> RlmResponse:
        del request
        return RlmResponse(
            {
                "root_cause": "the recorded review finding is unresolved",
                "proposed_solution": "apply the review correction",
                "read_first": ["src/leaf.py"],
                "steps": ["Apply the correction"],
                "verify": ["just tl-loop-test"],
                "boundary": ["src/leaf.py"],
                "done_criteria": ["The review finding is resolved"],
            }
        )


class _RecoveryTransport(RecordingTransport):
    def __init__(self, *, repair_mode: bool = False) -> None:
        super().__init__()
        self.repair_mode = repair_mode

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if tool_name == "watcher_pr_state":
            self.calls.append((tool_name, dict(arguments)))
            return {
                "success": True,
                "result": {
                    "found": True,
                    "pr_number": 42,
                    "head_sha": "head-a",
                    "head_branch": "main.leaf-a",
                    "base_branch": "main",
                    "base_sha": "base-a",
                    "patch_digest": "patch-a",
                    "merge_tree_sha": "tree-a",
                    "review_state": "changes_requested",
                    "review_id": 8,
                    "review_verdict": "CHANGES_REQUESTED",
                    "review_head_sha": "head-a",
                    "review_submitted_at": "2026-08-11T17:20:00Z",
                    "review_body": "Apply the review correction.",
                    "reviewer_agent_id": "review-pr-42-codex",
                    "ci_status": "success",
                    "state": "open",
                    "pr_state": "open",
                    "merged": False,
                    "head_reachable": True,
                    "publication_ownership_verified": True,
                    "publication_ownership_error": "",
                    "evidence_error": "",
                    "reviewer_identity_error": "",
                },
            }
        if tool_name == "resolve_live_pr_for_slice":
            self.calls.append((tool_name, dict(arguments)))
            if self.repair_mode:
                return {
                    "success": True,
                    "result": {
                        "slice_id": arguments["slice_id"],
                        "resolution": "never_published",
                        "pr_number": 0,
                    },
                }
            return {
                "success": True,
                "result": {
                    "slice_id": arguments["slice_id"],
                    "resolution": "live",
                    "pr_number": 42,
                    "publication": {
                        "repository": "org/repo",
                        "parent_branch": "main",
                        "head_sha": "head-a",
                    },
                },
            }
        return super().call_tool(role, name, tool_name, arguments)


def _seed_recovery_checkpoint(root: Path, operation: str) -> tuple[Path, str, str]:
    """Create a production-shaped nonterminal checkpoint for one real tl_run effect."""
    fixture = "clean-two-slice.json" if operation == "resume_pr" else "recursive-position.json"
    replay_fixture(FIXTURE_ROOT / fixture, root, journal=True)
    if operation == "resume_pr":
        run_root = root
        run_id = "replay-clean"
        store = RunStore(run_id, root_dir=run_root)
        state = store.load()
        current = state.slices["leaf-a"]
        observed_at = "2026-08-11T17:20:00Z"
        current = replace(
            current,
            status=SliceStatus.REPAIRING,
            pr_number=42,
            reviewed_head="head-a",
            verdict=Verdict.NO_GO,
            reviewer_agent_id="review-pr-42-codex",
            review_evidence=DurableReviewEvidence(
                8,
                42,
                "head-a",
                "review-pr-42-codex",
                Verdict.NO_GO,
                observed_at,
                observed_at,
            ),
            review_findings={
                "head-a": (
                    {
                        "path": "src/leaf.py",
                        "severity": "blocking",
                        "rationale": "Apply the review correction.",
                    },
                )
            },
            reviewer_attempt={"head-a": 1},
            dispatch_invocation_id="invocation-a",
            handoff=HandoffEvidence(42, "head-a", 1, "invocation-a", "leaf-a", observed_at),
            publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1, "invocation-a"),
        )
        slices = dict(state.slices)
        slices["leaf-a"] = current
        records = tuple(
            ChildRecord(
                child_id,
                ChildKind.LEAF if child_id == "leaf-a" else ChildKind.WORKER,
                manifest_node_id=slice_state.manifest_node_id,
                manifest_revision=slice_state.manifest_revision,
            )
            for child_id, slice_state in slices.items()
        )
        fsm = TLRunning(
            0,
            {},
            scope_path=(run_id,),
            plan_digest=state.plan_manifest.digest,
            parallel_pending=records,
        )
        store.checkpoint(
            fsm,
            slices,
            state.budgets,
            state.events.last_consumed_offset,
            plan_manifest=state.plan_manifest,
        )
        return run_root, run_id, run_id

    run_root = root / "replay-recursive"
    run_id = "stage-a"
    store = RunStore(run_id, root_dir=run_root)
    state = store.load()
    current = state.slices["nested-a"]
    evidence = {
        "child_id": "nested-a",
        "repository": "org/repo",
        "parent_branch": "main",
        "pr_number": "42",
        "head_sha": "head-a",
        "merge_journal_id": "merge-journal",
        "lane_epoch": "1",
    }
    phase_by_operation = {
        "post_merge_parent_sync": PostMergePhase.REMOTE_MERGE_ADOPTED,
        "post_merge_issue_close": PostMergePhase.ISSUE_CLOSE_PENDING,
        "post_merge_changelog": PostMergePhase.CHANGELOG_PENDING,
        "post_merge_push": PostMergePhase.PARENT_PUSH_PENDING,
    }
    phase = phase_by_operation[operation]
    if phase is not PostMergePhase.REMOTE_MERGE_ADOPTED:
        evidence.update(
            {
                "parent_commit_sha": "parent-after-merge",
                "issue_id": "1053",
                "issue_close_intent_id": "issue-close-intent",
            }
        )
    if phase in {PostMergePhase.CHANGELOG_PENDING, PostMergePhase.PARENT_PUSH_PENDING}:
        evidence.update(
            {
                "issue_close_journal_id": "issue-close-journal",
                "changelog_intent_id": "changelog-intent",
                "changelog_generation": "0",
            }
        )
    if phase is PostMergePhase.PARENT_PUSH_PENDING:
        evidence.update(
            {
                "changelog_commit_sha": "changelog-commit",
                "parent_push_intent_id": "parent-push-intent",
                "push_journal_id": "push-journal",
                "expected_base_sha": "parent-after-merge",
            }
        )
    reconciliation = {
        "confirmed_stage": "merge",
        "authoritative_evidence": ["published_pr", "pr_state", "merged"],
        "missing_evidence": [],
        "conflicts": [],
        "next_action": "adopt_merged",
        "merge_journal_id": "merge-journal",
        "remote_head_sha": (
            "base-a" if phase is PostMergePhase.REMOTE_MERGE_ADOPTED else "parent-after-merge"
        ),
    }
    current = replace(
        current,
        status=SliceStatus.MERGED,
        pr_number=42,
        reviewed_head="head-a",
        base_ref="main",
        reconciliation=reconciliation,
        post_merge=PostMergeState(phase, evidence),
    )
    slices = dict(state.slices)
    slices["nested-a"] = current
    recursive_fsm = replace(
        state.recursive_fsm,
        post_merge={**state.recursive_fsm.post_merge, "nested-a": current.post_merge},
    )
    store.checkpoint(
        recursive_fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=state.state_version,
        plan_manifest=state.plan_manifest,
    )
    journal = EffectJournal(run_id, store.run_dir / "action-journal.json")
    merge_intent = EffectIntent(
        "merge_pr", "nested-a", {"pr_number": 42, "expected_head_sha": "head-a"}, True
    )
    journal.append(merge_intent)
    journal.mark_result(
        merge_intent,
        ToolResult.from_raw({"success": True, "result": {"merged": True}}),
    )
    return run_root, run_id, state.ledger_run_id or run_id


def _run_recovery_tl(
    root: Path,
    run_id: str,
    ledger_run_id: str,
    operation: str,
    *,
    crash_after: str | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[_RecoveryTransport, object]:
    """Run the real driver, optionally dying after a confirmed journaled effect."""
    transport = _RecoveryTransport(repair_mode=operation == "resume_pr")
    effects = EffectClient(transport)
    config = TLLoopConfig(
        active=True,
        source=ReplayEventSource([]),
        effects=effects,
        root_dir=root,
        run_id=run_id,
        policy=load_policy(POLICY_ROOT / "selector_policy_cheap_only.toml"),
        review_policy_path=FIXTURE_ROOT / "review-policy.toml",
        ledger_run_id=ledger_run_id,
        chainlink_issue_id=1053,
        repository_identity=RepositoryIdentity("org", "repo", "main"),
        session_mode="continue",
        max_events=32,
        poll_interval=0.001,
        review_clock=lambda: datetime(2026, 8, 11, 17, 30, tzinfo=UTC),
        review_model_choice=(
            RlmModelChoice(
                "test-model",
                _RepairBackend(),
                store=RlmCallStore(),
                context_length=10_000,
            )
            if operation == "resume_pr"
            else None
        ),
    )
    original_checkpoint = RunStore.checkpoint
    journal = EffectJournal(run_id, root / run_id / "action-journal.json")
    crashed = False

    def crash_checkpoint(store: RunStore, *args: object, **kwargs: object) -> object:
        nonlocal crashed
        if (
            not crashed
            and crash_after is not None
            and store.run_id == run_id
            and any(
                entry.get("operation") == crash_after and entry.get("status") == "confirmed"
                for entry in journal.snapshot()
            )
        ):
            crashed = True
            raise RuntimeError(f"simulated process death after {crash_after}")
        return original_checkpoint(store, *args, **kwargs)

    if monkeypatch is not None and crash_after is not None:
        monkeypatch.setattr(RunStore, "checkpoint", crash_checkpoint)
    try:
        try:
            tl_run(
                {"run_id": run_id, "plan": None},
                config,
                {"tokens": 0, "wall_seconds": 0},
            )
        except ReplayTruncated:
            pass
    finally:
        if monkeypatch is not None and crash_after is not None:
            monkeypatch.setattr(RunStore, "checkpoint", original_checkpoint)
    if crash_after is not None:
        assert crashed is True
    return transport, RunStore(run_id, root_dir=root).load()


def _normalize_recovery_state(document: object, root: Path) -> object:
    """Normalize path-scoped worktree evidence for cross-run comparison."""
    if isinstance(document, dict):
        return {key: _normalize_recovery_state(value, root) for key, value in document.items()}
    if isinstance(document, list):
        return [_normalize_recovery_state(value, root) for value in document]
    if isinstance(document, str) and document.startswith(str(root)):
        return "<recovery-root>" + document[len(str(root)) :]
    return document


def _semantic_recovery_state(document: object, root: Path) -> object:
    """Compare durable FSM state without physical checkpoint-write counts."""
    normalized = _normalize_recovery_state(document, root)
    if isinstance(normalized, dict):
        return {key: value for key, value in normalized.items() if key != "revision"}
    return normalized


@pytest.mark.parametrize("operation", RECOVERY_BOUNDARIES)
def test_tl_run_crash_resume_matches_live_and_probes_each_recovery_effect(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_root = tmp_path / "live"
    live_run_root, live_run_id, live_ledger_id = _seed_recovery_checkpoint(live_root, operation)
    live_transport, live_state = _run_recovery_tl(
        live_run_root,
        live_run_id,
        live_ledger_id,
        operation,
    )

    replay_root = tmp_path / "replay"
    replay_run_root, replay_run_id, replay_ledger_id = _seed_recovery_checkpoint(
        replay_root, operation
    )
    with pytest.raises(RuntimeError, match=f"after {operation}"):
        _run_recovery_tl(
            replay_run_root,
            replay_run_id,
            replay_ledger_id,
            operation,
            crash_after=operation,
            monkeypatch=monkeypatch,
        )
    resumed_transport, resumed_state = _run_recovery_tl(
        replay_run_root,
        replay_run_id,
        replay_ledger_id,
        operation,
    )

    live_document = json.loads((live_run_root / live_run_id / "run.json").read_text())
    resumed_document = json.loads((replay_run_root / replay_run_id / "run.json").read_text())
    assert _semantic_recovery_state(
        normalize_durable_state(live_document), live_run_root
    ) == _semantic_recovery_state(normalize_durable_state(resumed_document), replay_run_root)
    assert live_state.state_version == resumed_state.state_version
    assert live_state.reducer_version == resumed_state.reducer_version
    effect_tool = RECOVERY_EFFECT_TO_TOOL.get(operation, operation)
    assert sum(action[0] == effect_tool for action in live_transport.calls) == 1
    assert sum(action[0] == effect_tool for action in resumed_transport.calls) == 0


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
