"""Disposable terminal-checkpoint test for the Beast-shaped tl_failed recovery.

The Beast workspace reached a terminal root/child ``tl_failed`` state with
Chainlink escalation #816 and PR #45 (see Chainlink #1112). This test copies
that shape into a disposable checkpoint under the test's tmp dir -- it never
reads or writes the live Beast checkpoint -- and exercises the supported
recovery path to record precisely whether ``init --continue`` alone suffices
or a named ordered-recovery gate (human gate) is required.

Answer encoded below: ``init --continue`` alone does NOT suffice. The child
carries PR #45 publication evidence, so the ordered-recovery decision is
non-recoverable and opens the named ``tl-ordered-child-recovery-stage-a``
gate for the operator.
"""

from __future__ import annotations

import json
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.events.replay import ReplayEventSource
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import TLLoopConfig, tl_run
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.schema import SliceStatus
from tl_loop.state.store import RunStore
from tl_loop.tests.replay import RecordingTransport

RUN_ID = "beast-replay"
CHILD = "stage-a"
LEAF = "child-leaf"
FAILURE_REASON = "chainlink issue result has no positive issue ID: {'cicoIssueId': 816}"
GATE_NAME = "tl-ordered-child-recovery-stage-a"
RECOVERY_REASON = "parent child has publication evidence requiring integration reconciliation"
PUBLICATION = {
    "pr_number": 45,
    "head_sha": "head-45",
    "head_branch": "main.stage-a",
    "base_branch": "main",
    "attempt": 1,
    "invocation_id": "inv-current",
}
CHILD_PUBLICATION = {**PUBLICATION, "head_branch": "main.stage-a.child-leaf"}


def _failed_fsm(reason: str, scope_path: list[str]) -> dict:
    return {
        "kind": "recursive",
        "phase": "tl_failed",
        "waiting": [],
        "payload": {
            "kind": "tl_failed",
            "reason": reason,
            "scope_path": scope_path,
            "last_evidence": {},
            "next_transition": "operator_recovery",
        },
    }


def _empty_integration() -> dict:
    return {
        "aggregate_head_sha": None,
        "aggregate_original_base_sha": None,
        "aggregate_patch_digest": None,
        "aggregate_pr_number": None,
        "base_revalidation_count": 0,
        "candidates": {},
        "ci_status": "unknown",
        "head_sha": None,
        "integration_owner_branch": None,
        "integration_owner_id": None,
        "integration_owner_run_id": None,
        "integration_owner_worktree": None,
        "lanes": {},
        "lifecycle": "RUNNING",
        "merge_attempts": 0,
        "merge_tree_sha": None,
        "patch_digest": None,
        "stage_verification": "pending",
        "sub_tl_recovery": {},
        "sub_tl_states": {CHILD: "RUNNING"},
        "validated_base_sha": None,
    }


def _repository_identity() -> dict:
    return {
        "owner": "org",
        "repo": "repo",
        "base_branch": "main",
        "forge_host": None,
        "remote_url": None,
    }


def _write_run(directory: Path, document: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _synthesize_beast_checkpoint(root: Path) -> None:
    """Write the copied Beast root/child terminal checkpoint under tmp."""
    root_manifest = build_plan_manifest(
        {
            "sub_tls": [
                {
                    "name": CHILD,
                    "order": 1,
                    "plan": {"leaves": [{"name": LEAF, "task": "implement"}]},
                }
            ]
        },
        scope_id=RUN_ID,
        owned_branch="main",
    )
    stage_node = next(node for node in root_manifest.nodes if node.name == CHILD)
    child_manifest = root_manifest.child_manifests[stage_node.node_id]

    child_dir = root / RUN_ID / CHILD
    child_revision = 3
    _write_run(
        child_dir,
        {
            "version": 1,
            "revision": child_revision,
            "run_id": CHILD,
            "fsm": _failed_fsm(FAILURE_REASON, [f"{RUN_ID}/sub_tl/{CHILD}"]),
            "slices": {
                LEAF: {
                    "id": LEAF,
                    "status": SliceStatus.FAILED.value,
                    "paths": ["src/child.py"],
                    "depends_on": [],
                    "base_ref": "main",
                    "test_plan": ["just tl-loop-test"],
                    "agent_type": "codex/gpt-luna",
                    "model": None,
                    "branch": "main.stage-a.child-leaf",
                    "worktree": str(child_dir / LEAF),
                    "pr_number": 45,
                    "publication": CHILD_PUBLICATION,
                    "review_findings": {},
                    "reviewer_attempt": {},
                    "reviewed_head": "head-45",
                    "attempts": 1,
                    "verdict": None,
                    "ci_state": {},
                    "repair_attempts": 0,
                    "task_timeout_seconds": 3600.0,
                    "task_timeout_source": "built_in",
                    "dispatch_intent_id": "child-intent",
                    "dispatch_invocation_id": "inv-current",
                    "dispatch_agent_id": LEAF,
                    "dispatch_authoritative_event_seq": 2,
                    "dispatch_last_boundary": "agent.spawned",
                    "manifest_node_id": f"{RUN_ID}/sub_tl/{CHILD}/leaf/{LEAF}",
                    "manifest_revision": 1,
                }
            },
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
            "gates": [],
            "events": {"last_consumed_offset": 6},
            "reducer_version": 1,
            "current_order": 1,
            "ordered_stages": [],
            "integration": _empty_integration(),
            "repository_identity": _repository_identity(),
            "owner_branch": "main.stage-a",
            "owner_worktree": str(child_dir),
            "parent_agent_id": RUN_ID,
            "parent_branch": "main",
            "parent_run_id": RUN_ID,
            "depth": 1,
            "state_version": 1,
            "plan_manifest": json.loads(json.dumps(child_manifest.to_document())),
        },
    )
    (child_dir / "controller-exit.json").write_text(
        json.dumps(
            {
                "reason": FAILURE_REASON,
                "checkpoint_revision": child_revision,
                "checkpoint_failure_reason": FAILURE_REASON,
            }
        ),
        encoding="utf-8",
    )

    _write_run(
        root / RUN_ID,
        {
            "version": 1,
            "revision": 5,
            "run_id": RUN_ID,
            "fsm": _failed_fsm("recursive child failed", [RUN_ID]),
            "slices": {
                CHILD: {
                    "id": CHILD,
                    "status": SliceStatus.FAILED.value,
                    "paths": ["tl-loop/stage-a"],
                    "depends_on": [],
                    "base_ref": "main",
                    "test_plan": ["controller"],
                    "agent_type": "codex",
                    "model": None,
                    "branch": "main.stage-a",
                    "worktree": str(root / RUN_ID / CHILD),
                    "pr_number": 45,
                    "publication": PUBLICATION,
                    "review_findings": {},
                    "reviewer_attempt": {},
                    "reviewed_head": None,
                    "attempts": 1,
                    "verdict": None,
                    "ci_state": {},
                    "repair_attempts": 0,
                    "task_timeout_seconds": 3600.0,
                    "task_timeout_source": "built_in",
                    "dispatch_intent_id": "stage-a-intent",
                    "dispatch_invocation_id": "inv-current",
                    "dispatch_agent_id": CHILD,
                    "dispatch_authoritative_event_seq": 1,
                    "dispatch_last_boundary": "sub_tl_started",
                    "manifest_node_id": stage_node.node_id,
                    "manifest_revision": 1,
                }
            },
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
            "gates": [],
            "events": {"last_consumed_offset": 6},
            "reducer_version": 1,
            "current_order": 1,
            "ordered_stages": [{"order": 1, "sub_tls": [CHILD]}],
            "integration": _empty_integration(),
            "repository_identity": _repository_identity(),
            "owner_branch": "main",
            "owner_worktree": str(root / RUN_ID),
            "parent_agent_id": None,
            "parent_branch": None,
            "parent_run_id": None,
            "depth": 0,
            "state_version": 1,
            "plan_manifest": json.loads(json.dumps(root_manifest.to_document())),
        },
    )


def _continue(root: Path):
    transport = RecordingTransport()
    config = TLLoopConfig(
        active=True,
        session_mode="continue",
        root_dir=root,
        run_id=RUN_ID,
        max_events=8,
        poll_interval=0.001,
        source=ReplayEventSource([]),
        effects=EffectClient(transport),
    )
    result = tl_run(
        {"run_id": RUN_ID, "plan": None},
        config,
        {"tokens": 0, "wall_seconds": 0},
    )
    return result, transport


def test_beast_terminal_checkpoint_records_escalation_and_publication(
    tmp_path: Path,
) -> None:
    """The copied checkpoint durably records escalation #816 and PR #45."""
    _synthesize_beast_checkpoint(tmp_path)
    child = RunStore(CHILD, tmp_path / RUN_ID).load()
    assert child.recursive_fsm is not None
    assert child.recursive_fsm.reason == FAILURE_REASON
    assert "816" in child.recursive_fsm.reason
    leaf = child.slices[LEAF]
    assert leaf.publication is not None
    assert leaf.publication.pr_number == 45
    assert leaf.publication.head_sha == "head-45"
    root = RunStore(RUN_ID, tmp_path).load()
    assert root.recursive_fsm is not None
    assert root.recursive_fsm.reason == "recursive child failed"
    assert root.slices[CHILD].publication is not None
    assert root.slices[CHILD].publication.pr_number == 45


def test_init_continue_alone_does_not_recover_beast_checkpoint(
    tmp_path: Path,
) -> None:
    """`init --continue` semantics alone do NOT suffice for the Beast shape.

    The child carries PR #45 publication evidence, so the ordered-recovery
    decision is non-recoverable: the run stays at tl_failed and a named
    ordered-recovery gate opens for the operator. A human gate is required.
    """
    _synthesize_beast_checkpoint(tmp_path)
    result, transport = _continue(tmp_path)
    final = result.final_state
    # The run is NOT recovered: it remains at the terminal failed phase.
    assert final.fsm.phase is TLPhase.TLFailed
    assert final.recursive_fsm is not None
    assert final.recursive_fsm.reason == "recursive child failed"
    # A named ordered-recovery gate opened for the operator.
    gate_names = {gate.name for gate in final.gates}
    assert GATE_NAME in gate_names
    # The refusal reason is the publication evidence, not a retryable crash.
    diagnostics = result.diagnostics
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("ordered_recovery") == RECOVERY_REASON
    assert diagnostics.get("recovery_gate") == GATE_NAME
    # The child is not relaunched: no spawn effect was issued.
    operations = {call[0] for call in transport.calls}
    assert "spawn_leaf" not in operations
    assert "spawn_worker" not in operations
