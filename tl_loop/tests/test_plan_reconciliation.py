"""Safety tests for audited recovery from unjournaled plan identity drift."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLAllMerged, TLRunning
from tl_loop.loop.driver import (
    EffectIntent,
    TLLoopConfig,
    WorkPlan,
    _manifest_plan,
    _verify_supplied_plan_before_identity_recovery,
    _work_plan_from_manifest,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.lock import LockTimeout, RunLock
from tl_loop.state.plan_manifest import ManifestNode, PlanManifest, build_plan_manifest
from tl_loop.state.plan_reconciliation import PlanRevisionError, analyze_plan_reconciliation
from tl_loop.state.plan_reconciliation_apply import apply_plan_reconciliation
from tl_loop.state.plan_reconciliation_report import _safe_text
from tl_loop.state.plan_revision_transaction import recover_plan_revision
from tl_loop.state.schema import (
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create


def _plan_document(
    *,
    future_issue_id: int | None,
    completed_task: str = "accepted completed task",
    completed_issue_id: int | None = 811,
    controller_alias: str = "agent-architecture",
    five_ordered_stages: bool = False,
):
    plan = {
        "sub_tls": [
            {
                "name": "architecture",
                "agent_id": controller_alias,
                "order": 1,
                "plan": {
                    "leaves": [
                        {
                            "name": "completed",
                            "task": completed_task,
                            "chainlink_issue_id": completed_issue_id,
                        },
                        {
                            "name": "future",
                            "task": "future work",
                            "chainlink_issue_id": future_issue_id,
                        },
                    ]
                },
            }
        ]
    }
    if five_ordered_stages:
        plan["sub_tls"].extend(
            {"name": name, "order": order, "plan": {}}
            for order, name in enumerate(
                ("kani", "stage-three", "stage-four", "stage-five"),
                start=2,
            )
        )
    return {
        "run_id": "root",
        "plan": plan,
    }


def _manifest(document: dict[str, object]) -> PlanManifest:
    plan = WorkPlan.from_mapping(document["plan"])
    return build_plan_manifest(_manifest_plan(plan), scope_id="root", owned_branch="main")


def _manifest_compatibility_version(manifest: PlanManifest, version: int) -> PlanManifest:
    children = {
        node_id: _manifest_compatibility_version(child, version)
        for node_id, child in manifest.child_manifests.items()
    }
    nodes = tuple(
        replace(
            node,
            child_manifest_digest=(
                children[node.node_id].digest if node.node_id in children else None
            ),
        )
        for node in manifest.nodes
    )
    return replace(
        manifest,
        nodes=nodes,
        child_manifest_digests={node_id: child.digest for node_id, child in children.items()},
        child_manifests=children,
        plan_compatibility_version=version,
        digest=None,
    )


def _legacy_root_defaults(
    manifest: PlanManifest,
    *,
    leaf_review_owner: str | None = None,
) -> PlanManifest:
    owner_fields = (
        "leaf_review_owner",
        "aggregate_review_owner",
        "aggregate_repair_owner",
    )
    nodes = []
    for node in manifest.nodes:
        if node.name != "architecture":
            nodes.append(node)
            continue
        declaration = dict(node.declaration)
        declaration.pop("order_explicit", None)
        contract = dict(node.integration_contract)
        for name in owner_fields:
            contract.pop(name, None)
        if leaf_review_owner is not None:
            contract["leaf_review_owner"] = leaf_review_owner
        nodes.append(replace(node, declaration=declaration, integration_contract=contract))
    return replace(manifest, nodes=tuple(nodes), digest=None)


def _legacy_aggregate_review_defaults(manifest: PlanManifest) -> PlanManifest:
    """Materialize the former aggregate-review default throughout a plan tree."""
    children = {
        node_id: _legacy_aggregate_review_defaults(child)
        for node_id, child in manifest.child_manifests.items()
    }
    nodes = tuple(
        replace(
            node,
            integration_contract={
                **node.integration_contract,
                "aggregate_review_owner": "aggregate",
            },
            child_manifest_digest=(
                children[node.node_id].digest if node.node_id in children else None
            ),
        )
        for node in manifest.nodes
    )
    return replace(
        manifest,
        nodes=nodes,
        child_manifest_digests={node_id: child.digest for node_id, child in children.items()},
        child_manifests=children,
        plan_compatibility_version=1,
        digest=None,
    )


def _legacy_null_completed_issue_binding(manifest: PlanManifest) -> PlanManifest:
    """Retain the historical null leaf binding while receipts name issue 811."""
    children = {
        node_id: _legacy_null_completed_issue_binding(child)
        for node_id, child in manifest.child_manifests.items()
    }
    nodes = tuple(
        replace(
            node,
            chainlink_issue_id=None
            if node.kind == "leaf" and node.name == "completed"
            else node.chainlink_issue_id,
            child_manifest_digest=(
                children[node.node_id].digest if node.node_id in children else None
            ),
        )
        for node in manifest.nodes
    )
    return replace(
        manifest,
        nodes=nodes,
        child_manifest_digests={node_id: child.digest for node_id, child in children.items()},
        child_manifests=children,
        digest=None,
    )


def _post_merge_receipt() -> PostMergeState:
    evidence = {
        "child_id": "completed",
        "repository": "repo",
        "parent_branch": "main",
        "pr_number": "45",
        "head_sha": "a" * 40,
        "merge_journal_id": "merge-45",
        "lane_epoch": "epoch-1",
        "parent_commit_sha": "b" * 40,
        "issue_id": "811",
        "issue_close_intent_id": "close-811",
        "issue_close_journal_id": "chainlink-close:811",
        "changelog_intent_id": "changelog-811",
        "changelog_generation": "1",
        "changelog_commit_sha": "c" * 40,
        "parent_push_intent_id": "push-811",
        "push_journal_id": "push-journal-811",
        "expected_base_sha": "d" * 40,
        "push_receipt_id": "receipt-811",
        "pushed_commit": "e" * 40,
        "bookkeeping_commit": "f" * 40,
        "observed_remote_head": "f" * 40,
        "ancestry_proof": "push-ancestry-811",
    }
    return PostMergeState(PostMergePhase.COMPLETE, evidence)


def _child_record(
    node: ManifestNode,
    kind: ChildKind,
    *,
    dispatch_intent_id: str | None = None,
    invocation_id: str | None = None,
    **evidence: str,
) -> ChildRecord:
    return ChildRecord(
        child_id=node.name,
        kind=kind,
        dispatch_intent_id=dispatch_intent_id,
        invocation_id=invocation_id,
        evidence={
            "manifest_node_id": node.node_id,
            "manifest_revision": "1",
            **evidence,
        },
        manifest_node_id=node.node_id,
        manifest_revision=1,
    )


def _project(
    tmp_path: Path,
    *,
    candidate_issue_id: int | None = 812,
    candidate_completed_task: str = "accepted completed task",
    stale_runtime_digest: bool = False,
    legacy_root_defaults: bool = False,
    legacy_leaf_review_owner: str | None = None,
    accepted_completed_issue_id: int | None = 811,
    candidate_completed_issue_id: int | None = 811,
    include_completed_spawn_journal: bool = False,
    spawn_task_override: str | None = None,
    legacy_completed_dispatch: bool = False,
    controller_alias: str = "agent-architecture",
    parent_attempts: int = 1,
    legacy_aggregate_review_default: bool = False,
    legacy_completed_issue_binding: bool = False,
    persisted_compatible_plan_version: int | None = None,
    five_ordered_stages: bool = False,
    legacy_integration_coordinator_binding: bool = False,
) -> tuple[Path, RunStore, RunStore, bytes, bytes]:
    accepted_document = _plan_document(
        future_issue_id=None,
        completed_issue_id=accepted_completed_issue_id,
        controller_alias=controller_alias,
        five_ordered_stages=five_ordered_stages,
    )
    candidate_document = _plan_document(
        future_issue_id=candidate_issue_id,
        completed_task=candidate_completed_task,
        completed_issue_id=candidate_completed_issue_id,
        controller_alias=controller_alias,
        five_ordered_stages=five_ordered_stages,
    )
    accepted_bytes = json.dumps(accepted_document, sort_keys=True).encode()
    candidate_bytes = json.dumps(candidate_document, sort_keys=True).encode()
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "plan.snapshot").write_bytes(accepted_bytes)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(accepted_bytes).hexdigest() + "\n", encoding="ascii"
    )
    (state_root / "plan.json").write_bytes(candidate_bytes)

    accepted_root = _manifest(accepted_document)
    current_root = _manifest(candidate_document)
    if legacy_aggregate_review_default:
        current_root = _legacy_aggregate_review_defaults(accepted_root)
        if legacy_completed_issue_binding:
            current_root = _legacy_null_completed_issue_binding(current_root)
    elif persisted_compatible_plan_version is not None:
        current_root = _manifest_compatibility_version(
            accepted_root,
            persisted_compatible_plan_version,
        )
    if legacy_root_defaults:
        current_root = _legacy_root_defaults(
            current_root,
            leaf_review_owner=legacy_leaf_review_owner,
        )
    accepted_child = accepted_root.child_manifests["root/sub_tl/architecture"]
    current_child = current_root.child_manifests["root/sub_tl/architecture"]
    runtime_digest = "0" * 64 if stale_runtime_digest else None
    root_runtime_digest = (
        runtime_digest or current_root.digest
        if legacy_aggregate_review_default or persisted_compatible_plan_version is not None
        else runtime_digest
    )
    child_runtime_digest = (
        runtime_digest or current_child.digest
        if legacy_aggregate_review_default or persisted_compatible_plan_version is not None
        else runtime_digest
    )
    root_store = _create_root(
        state_root,
        current_root,
        accepted_root,
        root_runtime_digest,
        controller_alias=controller_alias,
        attempts=parent_attempts,
    )
    child_store = _create_child(
        state_root,
        current_child,
        accepted_child,
        child_runtime_digest,
        include_spawn_journal=include_completed_spawn_journal,
        spawn_task_override=spawn_task_override,
        legacy_completed_dispatch=legacy_completed_dispatch,
        controller_alias=controller_alias,
    )
    if legacy_completed_dispatch:
        _record_authoritative_spawn_event(
            tmp_path,
            child_store,
            emitting_controller_id=controller_alias,
        )
        invocation_dir = tmp_path / ".exo" / "agents" / controller_alias
        invocation_dir.mkdir(parents=True)
        (invocation_dir / "invocation.json").write_text(
            json.dumps(
                {
                    "invocation_id": "invocation-architecture",
                    "runtime_agent_id": controller_alias,
                    "trigger": "spawn",
                    "slice_id": "architecture",
                    "branch": f"main.{controller_alias}",
                    "worktree": f"/tmp/architecture/{controller_alias}",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    if legacy_integration_coordinator_binding:
        root_state = root_store.load()
        owner_id = "root:architecture:integration"
        current_slice = root_state.slices["architecture"]
        candidate = IntegrationCandidateState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            integration_owner_id=owner_id,
            integration_owner_run_id="architecture",
            integration_owner_branch=current_slice.branch,
            integration_owner_worktree=current_slice.worktree,
        )
        root_store.checkpoint(
            root_state.recursive_fsm,
            {
                **root_state.slices,
                "architecture": replace(current_slice, dispatch_agent_id=owner_id),
            },
            root_state.budgets,
            root_state.events.last_consumed_offset,
            current_order=root_state.current_order,
            ordered_stages=root_state.ordered_stages,
            integration=replace(root_state.integration, candidates={"architecture": candidate}),
        )
    return state_root, root_store, child_store, accepted_bytes, candidate_bytes


def _create_root(
    state_root: Path,
    current: PlanManifest,
    accepted: PlanManifest,
    runtime_digest: str | None = None,
    *,
    controller_alias: str = "agent-architecture",
    attempts: int = 1,
) -> RunStore:
    create(
        "root",
        {
            "plan_manifest": current.to_document(),
            "owner_branch": "main",
            "owner_worktree": "/tmp/root",
        },
        root_dir=state_root,
    )
    store = RunStore("root", state_root)
    state = store.load()
    stage = current.node("root/sub_tl/architecture")
    slices = dict(state.slices)
    slices["architecture"] = replace(
        slices["architecture"],
        status=SliceStatus.PARKED,
        attempts=attempts,
        branch=f"main.{controller_alias}",
        worktree=f"/tmp/architecture/{controller_alias}",
        base_ref="main",
        dispatch_intent_id="dispatch-architecture",
        dispatch_agent_id=controller_alias,
        dispatch_invocation_id="invocation-architecture",
        dispatch_authoritative_event_seq=1,
        park_cause=ParkCause.PUBLICATION_BLOCKED,
        park_issue_id=46,
        park_audit={
            "error_kind": "missing_invocation_registration",
            "missing_evidence": ["invocation_id", "slice_id", "head_branch"],
        },
    )
    record = _child_record(
        stage,
        ChildKind.SUB_TL,
        dispatch_intent_id="dispatch-architecture",
        invocation_id="invocation-architecture",
    )
    phase = TLRunning(
        current_order=1,
        pending_by_order={1: (record,)},
        scope_path=("root",),
        plan_digest=runtime_digest or accepted.digest or "",
    )
    store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=tuple(
            OrderedStageState(order, tuple(current.node(node_id).name for node_id in node_ids))
            for order, node_ids in current.ordered_stages
        ),
        integration=IntegrationRuntimeState(
            sub_tl_states={"architecture": IntegrationLifecycle.RUNNING}
        ),
        plan_manifest=current,
    )
    return store


def _create_child(
    state_root: Path,
    current: PlanManifest,
    accepted: PlanManifest,
    runtime_digest: str | None = None,
    *,
    include_spawn_journal: bool = False,
    spawn_task_override: str | None = None,
    legacy_completed_dispatch: bool = False,
    controller_alias: str = "agent-architecture",
) -> RunStore:
    child_spec = {
        "parent_run_id": "root",
        "parent_agent_id": "root",
        "parent_branch": "main",
        "owner_branch": f"main.{controller_alias}",
        "owner_worktree": f"/tmp/architecture/{controller_alias}",
        "depth": 1,
        "plan_manifest": current.to_document(),
    }
    if legacy_completed_dispatch:
        child_spec.update(
            ledger_run_id="root-ledger",
        )
    create("architecture", child_spec, root_dir=state_root / "root")
    store = RunStore("architecture", state_root / "root")
    state = store.load()
    receipt = _post_merge_receipt()
    if legacy_completed_dispatch:
        receipt = PostMergeState(
            PostMergePhase.COMPLETE,
            {
                **receipt.evidence,
                "parent_branch": f"main.{controller_alias}",
                "forgejo_head_sha": "a" * 40,
                "forgejo_merged": "true",
                "forgejo_pr_number": "45",
            },
        )
    slices = dict(state.slices)
    completed_node = current.node("root/sub_tl/architecture/leaf/completed")
    slices["completed"] = replace(
        slices["completed"],
        status=SliceStatus.MERGED,
        attempts=1,
        pr_number=45,
        dispatch_intent_id="dispatch-completed",
        dispatch_started_at=1.0,
        dispatch_agent_id="agent-completed",
        dispatch_invocation_id=None if legacy_completed_dispatch else "invocation-completed",
        dispatch_authoritative_event_seq=1,
        branch=f"main.{controller_alias}.agent-completed" if legacy_completed_dispatch else None,
        worktree="/tmp/architecture/completed" if legacy_completed_dispatch else None,
        base_ref=f"main.{controller_alias}"
        if legacy_completed_dispatch
        else slices["completed"].base_ref,
        manifest_node_id=completed_node.node_id,
        manifest_revision=current.manifest_revision,
        chainlink_issue_id=completed_node.chainlink_issue_id,
        post_merge=receipt,
    )
    completed = replace(
        _child_record(completed_node, ChildKind.LEAF),
        dispatch_intent_id="dispatch-completed",
        invocation_id="invocation-completed",
    )
    future = _child_record(
        current.node("root/sub_tl/architecture/leaf/future"),
        ChildKind.LEAF,
    )
    phase = (
        TLAllMerged(
            scope_path=(current.scope_id,),
            plan_digest=runtime_digest or accepted.digest or "",
            completed_children={"completed": receipt},
        )
        if legacy_completed_dispatch
        else TLRunning(
            current_order=0,
            pending_by_order={},
            scope_path=(current.scope_id,),
            plan_digest=runtime_digest or accepted.digest or "",
            parallel_pending=(future,),
            completed_children={"completed": completed},
            post_merge={"completed": receipt},
        )
    )
    store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=current,
    )
    if include_spawn_journal:
        _record_completed_spawn(
            store,
            accepted.node("root/sub_tl/architecture/leaf/completed").task,
            spawn_task_override,
            legacy_completed_dispatch=legacy_completed_dispatch,
            branch_name=f"main.{controller_alias}.agent-completed",
        )
    return store


def _record_completed_spawn(
    store: RunStore,
    accepted_task: str,
    task_override: str | None,
    *,
    legacy_completed_dispatch: bool = False,
    branch_name: str = "main.agent-completed",
    intent_id: str = "dispatch-completed",
) -> None:
    intent = EffectIntent(
        "spawn_leaf",
        "completed",
        {
            "name": "completed",
            "task": task_override if task_override is not None else accepted_task,
            "intent_id": intent_id,
            "boundary": [],
        },
        True,
    )
    journal = EffectJournal(store.run_id, store.run_dir / "action-journal.json")
    journal.append(intent)
    result = (
        {
            "success": True,
            "result": {
                "invocation_id": None,
                "branch_name": branch_name,
                "worktree_path": "/tmp/architecture/agent-completed",
            },
        }
        if legacy_completed_dispatch
        else {
            "success": True,
            "result": {"agent_id": "agent-completed", "invocation_id": "invocation-completed"},
        }
    )
    journal.mark_result(
        intent,
        ToolResult.from_raw(result),
    )


def _record_authoritative_spawn_event(
    project: Path,
    store: RunStore,
    *,
    emitting_controller_id: str,
) -> None:
    state = store.load()
    slice_state = state.slices["completed"]
    ledger = project / ".exo" / "ledger" / "segments"
    ledger.mkdir(parents=True)
    event = {
        "agent_id": emitting_controller_id,
        "data": {
            "branch": slice_state.branch,
            "child_agent": slice_state.dispatch_agent_id,
            "intent_id": slice_state.dispatch_intent_id,
        },
        "invocation_id": None,
        "lifecycle_state": "observed",
        "observed_at": "2026-10-13T00:00:00Z",
        "run_id": state.ledger_run_id,
        "run_seq": slice_state.dispatch_authoritative_event_seq,
        "type": "agent.spawned",
    }
    (ledger / "segment-000000000001.jsonl").write_text(
        json.dumps(event, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _direct_root_legacy_project(
    project: Path,
    *,
    emitting_controller_id: str,
) -> tuple[Path, RunStore]:
    state_root = project / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    accepted_document = {
        "run_id": "root",
        "plan": {
            "leaves": [
                {
                    "name": "completed",
                    "task": "accepted root task",
                    "chainlink_issue_id": 811,
                }
            ]
        },
    }
    candidate_document = {
        "run_id": "root",
        "plan": {
            "leaves": [
                {
                    "name": "completed",
                    "task": "rewritten root task",
                    "chainlink_issue_id": 811,
                }
            ]
        },
    }
    accepted_bytes = json.dumps(accepted_document, sort_keys=True).encode()
    candidate_bytes = json.dumps(candidate_document, sort_keys=True).encode()
    (state_root / "plan.snapshot").write_bytes(accepted_bytes)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(accepted_bytes).hexdigest() + "\n",
        encoding="ascii",
    )
    (state_root / "plan.json").write_bytes(candidate_bytes)
    accepted = _manifest(accepted_document)
    current = _manifest(candidate_document)
    create(
        "root",
        {
            "ledger_run_id": "root-ledger",
            "owner_branch": "main",
            "owner_worktree": "/tmp/root",
            "plan_manifest": current.to_document(),
        },
        root_dir=state_root,
    )
    store = RunStore("root", state_root)
    state = store.load()
    receipt = PostMergeState(
        PostMergePhase.COMPLETE,
        {
            **_post_merge_receipt().evidence,
            "forgejo_head_sha": "a" * 40,
            "forgejo_merged": "true",
            "forgejo_pr_number": "45",
        },
    )
    completed_node = current.node("root/leaf/completed")
    completed_slice = replace(
        state.slices["completed"],
        status=SliceStatus.MERGED,
        attempts=1,
        pr_number=45,
        dispatch_intent_id="dispatch-root-leaf",
        dispatch_agent_id="agent-completed",
        dispatch_invocation_id=None,
        dispatch_authoritative_event_seq=1,
        branch="main.agent-completed",
        worktree="/tmp/root/completed",
        base_ref="main",
        manifest_node_id=completed_node.node_id,
        manifest_revision=current.manifest_revision,
        chainlink_issue_id=811,
        post_merge=receipt,
    )
    store.checkpoint(
        TLAllMerged(
            scope_path=("root",),
            plan_digest=accepted.digest or "",
            completed_children={"completed": receipt},
        ),
        {**state.slices, "completed": completed_slice},
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=current,
    )
    _record_completed_spawn(
        store,
        "accepted root task",
        None,
        legacy_completed_dispatch=True,
        branch_name="main.agent-completed",
        intent_id="dispatch-root-leaf",
    )
    _record_authoritative_spawn_event(
        project,
        store,
        emitting_controller_id=emitting_controller_id,
    )
    return state_root, store


def _mutate_completed_spawn_result(
    store: RunStore,
    field: str,
    value: object,
) -> None:
    journal_path = store.run_dir / "action-journal.json"
    entries = json.loads(journal_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in entries
        if item.get("operation") == "spawn_leaf" and item.get("target") == "completed"
    )
    entry["result"]["result"][field] = value
    journal_path.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }


def test_dry_run_reports_snapshot_manifest_fsm_and_protected_leaf_drift(
    tmp_path: Path,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task=(
            "rewritten completed task\npassword=should-not-appear "
            "https://user:token@forge.invalid/path"
        ),
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert report["evidence_fingerprint"]
    assert {scope["scope_id"] for scope in report["scopes"]} == {
        "root",
        "root/sub_tl/architecture",
    }
    assert all(scope["fsm_matches_accepted"] for scope in report["scopes"])
    assert any(
        blocker["code"] == "protected_declaration_evidence_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture/leaf/completed"
        for blocker in report["blockers"]
    )
    changed = report["manifest_changes_to_accepted_plan"]
    assert any("task" in change["fields"] for change in changed)
    assert any("chainlink_issue_id" in change["fields"] for change in changed)
    completed = next(
        change
        for change in report["scopes"][1]["declaration_changes"]
        if change["node_name"] == "completed"
    )
    assert completed["protected"]
    assert completed["disposition"] == "blocked"
    assert all("observed" in field and "proposed" in field for field in completed["fields"])
    candidate_future = next(
        change
        for change in report["candidate_declaration_changes_not_applied"]
        if change["node_name"] == "future"
    )
    assert candidate_future["disposition"] == "not_applied"
    assert candidate_future["fields"][0]["proposed"] is not None
    assert report["scopes"][1]["runtime_records"][0]["manifest_revision"] == 1
    rendered = json.dumps(report, sort_keys=True)
    assert "rewritten completed task" not in rendered
    assert "should-not-appear" not in rendered
    assert "user:token" not in rendered
    escaped = _safe_text("branch\npassword=hidden https://user:token@forge.invalid/path")
    assert "\\u000a" in escaped
    assert "hidden" not in escaped
    assert "user:token" not in escaped
    root_ownership = report["scopes"][0]["slice_ownership"][0]
    assert root_ownership["dispatch_intent_id"] == "dispatch-architecture"
    assert root_ownership["publication"] is None
    assert root_ownership["park_cause"] == "publication_blocked"
    assert root_ownership["park_issue_id"] == 46
    assert _tree_bytes(state_root) == before


def test_stale_fsm_digests_do_not_hide_manifest_and_ownership_drift(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        stale_runtime_digest=True,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert all(not scope["fsm_matches_current"] for scope in report["scopes"])
    assert all(not scope["fsm_matches_accepted"] for scope in report["scopes"])
    assert all(scope["runtime_records"] for scope in report["scopes"])
    root_scope = report["scopes"][0]
    assert root_scope["slice_ownership"][0]["park_cause"] == "publication_blocked"
    assert (
        root_scope["slice_ownership"][0]["park_audit"]["error_kind"]
        == "missing_invocation_registration"
    )
    completed = next(
        item for item in report["scopes"][1]["slice_ownership"] if item["slice_id"] == "completed"
    )
    assert completed["pr_number"] == 45
    assert completed["post_merge_phase"] == "complete"
    assert completed["post_merge_evidence_sha256"]
    assert {blocker["code"] for blocker in report["blockers"]} >= {
        "runtime_digest_unproven",
        "protected_declaration_evidence_unproven",
    }
    assert _tree_bytes(state_root) == before


def test_apply_restores_only_proven_future_drift_and_keeps_completion_receipt(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, accepted_bytes, candidate_bytes = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    assert report.can_apply, report.blockers
    root_report = report.to_document()["scopes"][0]
    architecture_change = next(
        change
        for change in root_report["declaration_changes"]
        if change["node_name"] == "architecture"
    )
    assert architecture_change["protected"]
    assert architecture_change["disposition"] == "restore"
    before_receipt = child_store.load().slices["completed"].post_merge

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore accepted manifests before revising future metadata",
        report.evidence_fingerprint,
        actor="operator",
    )

    root_state = root_store.load()
    child_state = child_store.load()
    assert result.applied
    assert root_state.plan_manifest is not None
    assert root_state.plan_manifest.digest == _manifest(_plan_document(future_issue_id=None)).digest
    assert child_state.plan_manifest is not None
    assert (
        child_state.plan_manifest.digest
        == _manifest(_plan_document(future_issue_id=None))
        .child_manifests["root/sub_tl/architecture"]
        .digest
    )
    assert child_state.slices["future"].chainlink_issue_id is None
    assert child_state.slices["completed"].post_merge == before_receipt
    assert root_state.slices["architecture"].dispatch_invocation_id == "invocation-architecture"
    assert (state_root / "plan.snapshot").read_bytes() == accepted_bytes
    assert (state_root / "plan.json").read_bytes() == candidate_bytes
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1
    assert not (state_root / "plan-revision-transition.json").exists()
    assert not analyze_plan_reconciliation(tmp_path).has_drift
    repeat = analyze_plan_reconciliation(tmp_path)
    second = apply_plan_reconciliation(
        tmp_path,
        "repeat already completed reconciliation",
        repeat.evidence_fingerprint,
        actor="operator",
    )
    assert not second.applied
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1


def test_apply_accepts_exact_legacy_parent_defaults_with_fsm_and_dispatch_proof(
    tmp_path: Path,
) -> None:
    state_root, root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
    )

    report = analyze_plan_reconciliation(tmp_path)
    root_report = report.to_document()["scopes"][0]

    assert report.can_apply, report.blockers
    equivalence = root_report["legacy_default_equivalences"][0]
    assert equivalence["node_id"] == "root/sub_tl/architecture"
    assert equivalence["fields"] == [
        "declaration.order_explicit",
        "integration_contract.aggregate_repair_owner",
        "integration_contract.aggregate_review_owner",
        "integration_contract.leaf_review_owner",
    ]
    assert equivalence["evidence_source"] == (
        "verified_snapshot_and_matching_fsm_dispatch_identity"
    )

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the accepted manifest after proving legacy defaults",
        report.evidence_fingerprint,
        actor="operator",
    )

    assert result.applied
    assert root_store.load().plan_manifest == _manifest(_plan_document(future_issue_id=None))
    assert (state_root / "plan-reconciliations.jsonl").is_file()


def test_apply_migrates_unchanged_legacy_aggregate_defaults_and_keeps_receipts(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, accepted_bytes, candidate_bytes = _project(
        tmp_path,
        candidate_issue_id=None,
        legacy_aggregate_review_default=True,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        legacy_integration_coordinator_binding=True,
        legacy_completed_issue_binding=True,
        controller_alias="architecture",
        five_ordered_stages=True,
    )
    assert accepted_bytes == candidate_bytes
    before_child = child_store.load()
    before_receipt = before_child.slices["completed"].post_merge
    journal_path = child_store.run_dir / "action-journal.json"
    journal_bytes = journal_path.read_bytes()
    ledger_path = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    ledger_bytes = ledger_path.read_bytes()

    analysis = analyze_plan_reconciliation(tmp_path)

    assert analysis.can_apply, analysis.to_document()["blockers"]
    transition = analysis.compatibility_transition
    assert transition is not None
    assert transition["persisted_plan_compatibility_version"] == 1
    assert transition["target_plan_compatibility_version"] == 2
    assert transition["from"] == "aggregate_review_owner=aggregate"
    assert transition["to"] == "aggregate_review_owner=none"
    allowed_fields = transition["allowed_fields"]
    assert isinstance(allowed_fields, list)
    assert "integration_contract.aggregate_review_owner" in allowed_fields
    assert "child_manifest_digest" in allowed_fields
    affected_nodes = transition["affected_nodes"]
    assert isinstance(affected_nodes, list)
    assert any(
        isinstance(node, dict)
        and isinstance(node.get("node_id"), str)
        and node["node_id"].endswith("/leaf/completed")
        and isinstance(node.get("fields"), list)
        and "integration_contract.aggregate_review_owner" in node["fields"]
        for node in affected_nodes
    )

    def interrupt_after_first_target(_index: int, _target: str) -> None:
        raise RuntimeError("simulated migration process interruption")

    result = apply_plan_reconciliation(
        tmp_path,
        "Apply the audited legacy aggregate-review default migration",
        analysis.evidence_fingerprint,
        actor="operator",
        after_target=interrupt_after_first_target,
    )

    assert result.applied and result.recovered_during_apply
    root_after = root_store.load()
    child_after = child_store.load()
    assert result.applied
    assert root_after.plan_manifest == _manifest(
        _plan_document(future_issue_id=None, five_ordered_stages=True)
    )
    assert (
        child_after.plan_manifest
        == root_after.plan_manifest.child_manifests["root/sub_tl/architecture"]
    )
    assert root_after.recursive_fsm.plan_digest == root_after.plan_manifest.digest
    assert child_after.recursive_fsm.plan_digest == child_after.plan_manifest.digest
    assert (
        root_after.plan_manifest.node("root/sub_tl/architecture").integration_contract[
            "aggregate_review_owner"
        ]
        == "none"
    )
    assert child_after.slices["completed"].post_merge == before_receipt
    assert child_after.slices["completed"].status is SliceStatus.MERGED
    assert child_after.slices["completed"].chainlink_issue_id == 811
    assert [stage.order for stage in root_after.ordered_stages] == [1, 2, 3, 4, 5]
    assert [stage.sub_tls for stage in root_after.ordered_stages] == [
        ("architecture",),
        ("kani",),
        ("stage-three",),
        ("stage-four",),
        ("stage-five",),
    ]
    assert journal_path.read_bytes() == journal_bytes
    assert ledger_path.read_bytes() == ledger_bytes
    assert (state_root / "plan.snapshot").read_bytes() == accepted_bytes
    assert (state_root / "plan.json").read_bytes() == candidate_bytes
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    assert audit["compatibility_transition"]["from"] == "aggregate_review_owner=aggregate"
    assert audit["compatibility_transition"]["to"] == "aggregate_review_owner=none"
    replay = analyze_plan_reconciliation(tmp_path)
    assert not replay.has_drift
    assert replay.compatibility_transition is None
    assert not apply_plan_reconciliation(
        tmp_path,
        "Replay the already applied compatibility migration",
        replay.evidence_fingerprint,
        actor="operator",
    ).applied

    # Prove the migrated checkpoint is accepted by the same driver guard that
    # checks supplied plan identity before recovery and ordered continuation.
    resumed_plan = _work_plan_from_manifest(root_after.plan_manifest)
    _verify_supplied_plan_before_identity_recovery(
        "root",
        resumed_plan,
        root_after,
        TLLoopConfig(
            root_dir=tmp_path,
            project_root=tmp_path,
            run_id="root",
            branch="main",
            session_mode="continue",
        ),
        state_root,
    )
    assert len(resumed_plan.sub_tls) == 5
    assert root_after.current_order == 1
    assert root_after.slices["architecture"].status is SliceStatus.PARKED


def test_compatible_legacy_manifest_version_does_not_trigger_migration(tmp_path: Path) -> None:
    _state_root, root_store, _child_store, accepted_bytes, candidate_bytes = _project(
        tmp_path,
        candidate_issue_id=None,
        persisted_compatible_plan_version=1,
    )

    analysis = analyze_plan_reconciliation(tmp_path)

    assert accepted_bytes == candidate_bytes
    assert analysis.compatibility_transition is None
    assert not analysis.has_drift
    assert not analysis.can_apply
    assert root_store.load().plan_manifest.plan_compatibility_version == 1


def test_candidate_plan_drift_does_not_authorize_default_migration(tmp_path: Path) -> None:
    _state_root, _root_store, _child_store, accepted_bytes, candidate_bytes = _project(
        tmp_path,
        candidate_issue_id=812,
        legacy_aggregate_review_default=True,
    )

    analysis = analyze_plan_reconciliation(tmp_path)

    assert accepted_bytes != candidate_bytes
    assert analysis.compatibility_transition is None
    assert not analysis.can_apply


def test_explicit_aggregate_review_policy_is_not_reinterpreted(tmp_path: Path) -> None:
    _state_root, _root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_issue_id=None,
    )
    snapshot_path = tmp_path / ".exo" / "tl-loop" / "plan.snapshot"
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    architecture = document["plan"]["sub_tls"][0]
    architecture["integration"] = {"aggregate_review_owner": "aggregate"}
    encoded = json.dumps(document, sort_keys=True).encode()
    snapshot_path.write_bytes(encoded)
    (tmp_path / ".exo" / "tl-loop" / "plan.json").write_bytes(encoded)
    (tmp_path / ".exo" / "tl-loop" / "plan.snapshot.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(PlanRevisionError) as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "unsupported_aggregate_review_policy"


def test_legacy_default_equivalence_does_not_hide_explicit_owner_changes(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
        legacy_leaf_review_owner="aggregate",
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = report["scopes"][0]

    assert not report["can_apply"]
    equivalent_fields = root["legacy_default_equivalences"][0]["fields"]
    assert "integration_contract.leaf_review_owner" not in equivalent_fields
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in report["blockers"]
    )


def test_legacy_default_equivalence_requires_accepted_fsm_digest(tmp_path: Path) -> None:
    _state_root, root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
    )
    state = root_store.load()
    phase = replace(state.recursive_fsm, plan_digest=state.plan_manifest.digest)
    root_store.checkpoint(
        phase,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = report["scopes"][0]

    assert not report["can_apply"]
    assert root["legacy_default_equivalences"] == []
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in report["blockers"]
    )


def test_apply_restores_completed_task_and_issue_binding_from_dispatch_evidence(
    tmp_path: Path,
) -> None:
    _state_root, root_store, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
    )
    before_child = child_store.load()
    before_slice = before_child.slices["completed"]
    before_receipt = before_slice.post_merge
    action_journal = child_store.run_dir / "action-journal.json"
    action_journal_bytes = action_journal.read_bytes()

    report = analyze_plan_reconciliation(tmp_path)
    document = report.to_document()
    child_report = next(
        item for item in document["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child_report["protected_declaration_proofs"][0]

    assert report.can_apply, report.blockers
    assert proof["fields"] == ["chainlink_issue_id", "task"]
    assert proof["disposition"] == "proven"
    assert "confirmed_spawn_action_with_accepted_task" in proof["evidence"]
    assert "completed_issue_closure_receipt" in proof["evidence"]

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the completed declaration from its dispatch and closure evidence",
        report.evidence_fingerprint,
        actor="operator",
    )

    after_root = root_store.load()
    after_child = child_store.load()
    restored = after_child.slices["completed"]
    accepted_manifest = _manifest(_plan_document(future_issue_id=None, completed_issue_id=None))
    assert result.applied
    assert after_root.plan_manifest == accepted_manifest
    assert (
        after_child.plan_manifest == accepted_manifest.child_manifests["root/sub_tl/architecture"]
    )
    assert restored.status is SliceStatus.MERGED
    assert restored.pr_number == before_slice.pr_number == 45
    assert restored.chainlink_issue_id is None
    assert restored.dispatch_intent_id == before_slice.dispatch_intent_id
    assert restored.dispatch_agent_id == before_slice.dispatch_agent_id
    assert restored.dispatch_invocation_id == before_slice.dispatch_invocation_id
    assert restored.post_merge == before_receipt
    assert action_journal.read_bytes() == action_journal_bytes


def test_apply_restores_historical_dispatch_without_inventing_invocation_identity(
    tmp_path: Path,
) -> None:
    state_root, _root_store, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    before = child_store.load().slices["completed"]
    before_tree = _tree_bytes(tmp_path)

    report = analyze_plan_reconciliation(tmp_path)
    assert _tree_bytes(tmp_path) == before_tree
    child_report = next(
        item
        for item in report.to_document()["scopes"]
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child_report["protected_declaration_proofs"][0]

    assert report.can_apply, report.blockers
    assert proof["disposition"] == "proven"
    assert "missing_dispatch_invocation_preserved_as_unknown" in proof["evidence"]
    dispatch_event = json.loads(
        (tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl").read_text(
            encoding="utf-8"
        )
    )
    controller_invocation = (
        tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    ).read_bytes()
    assert proof["correlation"] == {
        "accepted_boundary_sha256": proof["correlation"]["accepted_boundary_sha256"],
        "accepted_task_sha256": proof["correlation"]["accepted_task_sha256"],
        "base_branch": "main.agent-architecture",
        "branch": "main.agent-architecture.agent-completed",
        "dispatch_event_seq": 1,
        "dispatch_event_sha256": hashlib.sha256(
            json.dumps(dispatch_event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "dispatch_event_emitter_agent_id": "agent-architecture",
        "dispatch_event_expected_emitter_agent_id": "agent-architecture",
        "dispatch_event_observed_emitter_agent_id": "agent-architecture",
        "dispatch_intent_id": "dispatch-completed",
        "dispatch_invocation_id": None,
        "dispatch_invocation_status": "unavailable_in_legacy_records",
        "dispatch_owner_agent_id": "agent-completed",
        "pr_base_branch": "main.agent-architecture",
        "pr_head_sha": "a" * 40,
        "pr_number": 45,
        "completion_receipt_sha256": hashlib.sha256(
            json.dumps(
                dict(before.post_merge.evidence), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "ledger_run_id": "root-ledger",
        "scope_id": "root/sub_tl/architecture",
        "scope_run_id": "architecture",
        "scope_controller_agent_id": "agent-architecture",
        "scope_controller_invocation_id": "invocation-architecture",
        "scope_controller_invocation_sha256": hashlib.sha256(controller_invocation).hexdigest(),
        "scope_controller_invocation_trigger": "spawn",
        "scope_controller_branch_sha256": hashlib.sha256(b"main.agent-architecture").hexdigest(),
        "scope_controller_worktree_sha256": hashlib.sha256(
            b"/tmp/architecture/agent-architecture"
        ).hexdigest(),
        "parent_scope_run_id": "root",
        "parent_scope_controller_agent_id": "root",
        "parent_dispatch_intent_id": "dispatch-architecture",
        "parent_dispatch_invocation_id": "invocation-architecture",
        "parent_dispatch_event_seq": 1,
        "parent_dispatch_agent_id": "agent-architecture",
        "parent_dispatch_attempts": 1,
        "parent_dispatch_attempt_condition": "positive_with_complete_dispatch_proof",
        "parent_dispatch_owner_record": "dispatch_owner",
        "slice_id": "completed",
        "spawn_action_key": proof["correlation"]["spawn_action_key"],
        "spawn_result_agent_id": None,
        "spawn_result_invocation_id": None,
        "slice_worktree_sha256": hashlib.sha256(b"/tmp/architecture/completed").hexdigest(),
        "spawn_result_worktree_sha256": hashlib.sha256(
            b"/tmp/architecture/agent-completed"
        ).hexdigest(),
        "worktree_paths_match": False,
    }

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the completed declaration from legacy dispatch evidence",
        report.evidence_fingerprint,
        actor="operator",
    )

    restored = child_store.load().slices["completed"]
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    assert result.applied
    assert restored.status is SliceStatus.MERGED
    assert restored.dispatch_invocation_id is None
    assert restored.dispatch_agent_id == before.dispatch_agent_id
    assert restored.dispatch_intent_id == before.dispatch_intent_id
    assert restored.dispatch_authoritative_event_seq == before.dispatch_authoritative_event_seq
    assert restored.pr_number == before.pr_number == 45
    assert restored.post_merge == before.post_merge
    assert audit["protected_declaration_proofs"][0]["correlation"] == proof["correlation"]


def test_zero_parent_attempts_reconcile_only_with_complete_proof_and_preserve_audit(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    root_before = root_store.load().slices["architecture"]
    child_before = child_store.load().slices["completed"]
    report = analyze_plan_reconciliation(tmp_path)
    document = report.to_document()
    child_report = next(
        item for item in document["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    emitter_proof = child_report["scope_emitter_proof"]
    declaration_proof = child_report["protected_declaration_proofs"][0]

    assert report.can_apply, report.blockers
    assert emitter_proof["correlation"]["parent_dispatch_attempts"] == 0
    assert emitter_proof["correlation"]["parent_dispatch_attempt_condition"] == (
        "zero_with_complete_dispatch_proof"
    )
    assert "zero_attempt_counter_with_complete_dispatch_proof" in emitter_proof["evidence"]
    assert declaration_proof["correlation"]["parent_dispatch_attempts"] == 0
    assert declaration_proof["correlation"]["parent_dispatch_attempt_condition"] == (
        "zero_with_complete_dispatch_proof"
    )

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore accepted declarations from complete legacy dispatch proof",
        report.evidence_fingerprint,
        actor="operator",
    )

    root_after = root_store.load().slices["architecture"]
    child_after = child_store.load().slices["completed"]
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    audit_child = next(
        item for item in audit["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    assert result.applied
    assert root_after.attempts == root_before.attempts == 0
    assert root_after.dispatch_intent_id == root_before.dispatch_intent_id
    assert root_after.dispatch_invocation_id == root_before.dispatch_invocation_id
    assert (
        root_after.dispatch_authoritative_event_seq == root_before.dispatch_authoritative_event_seq
    )
    assert child_after.attempts == child_before.attempts
    assert child_after.status is SliceStatus.MERGED
    assert child_after.post_merge == child_before.post_merge
    assert child_after.pr_number == child_before.pr_number
    assert audit_child["scope_emitter_proof"]["correlation"]["parent_dispatch_attempts"] == 0
    assert (
        audit_child["scope_emitter_proof"]["correlation"]["parent_dispatch_attempt_condition"]
        == "zero_with_complete_dispatch_proof"
    )
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1


def test_legacy_dispatch_event_change_invalidates_reconciliation_fingerprint(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    ledger = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    event = json.loads(ledger.read_text(encoding="utf-8"))
    event["data"]["branch"] = "main.changed-after-dry-run"
    ledger.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject stale legacy dispatch evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"


def test_parent_attempt_counter_change_invalidates_reconciliation_fingerprint(
    tmp_path: Path,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    report = analyze_plan_reconciliation(tmp_path)
    document = json.loads(root_store.path.read_text(encoding="utf-8"))
    document["slices"]["architecture"]["attempts"] = 1
    root_store.path.write_text(json.dumps(document), encoding="utf-8")
    before_apply = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject a changed dispatch counter after dry run",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before_apply


def test_scope_controller_registration_change_invalidates_fingerprint(tmp_path: Path) -> None:
    _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    recorded = dict(report.input_hashes)
    assert recorded[".exo/agents/agent-architecture/invocation.json"] is not None
    registration.write_text(
        registration.read_text(encoding="utf-8").replace(
            "main.agent-architecture", "main.changed-after-dry-run"
        ),
        encoding="utf-8",
    )

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject stale scope controller ownership evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"


@pytest.mark.parametrize(
    ("registration_state", "reason_code"),
    [
        ("missing", "scope_controller_registration_missing"),
        ("malformed", "scope_controller_registration_malformed"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_requires_a_valid_scope_controller_registration(
    tmp_path: Path,
    registration_state: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, _root, _child, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    if registration_state == "missing":
        registration.unlink()
    else:
        registration.write_text('{"invocation_id":', encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child["protected_declaration_proofs"][0]

    assert not report["can_apply"]
    assert proof["reason_code"] == reason_code
    assert proof["correlation"]["dispatch_event_observed_emitter_agent_id"] == (
        "agent-architecture"
    )
    expected_counter = "zero" if parent_attempts == 0 else "positive"
    assert (
        child["scope_emitter_proof"]["correlation"]["parent_dispatch_attempt_condition"]
        == f"{expected_counter}_dispatch_proof_unproven"
    )
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("corruption", "reason_code"),
    [
        ("wrong_owner", "scope_controller_invocation_owner_mismatch"),
        ("wrong_lineage", "scope_controller_invocation_lineage_mismatch"),
        ("wrong_branch", "scope_controller_invocation_branch_mismatch"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_rejects_conflicting_scope_controller_registration(
    tmp_path: Path,
    corruption: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    record = json.loads(registration.read_text(encoding="utf-8"))
    if corruption == "wrong_owner":
        record["runtime_agent_id"] = "unrelated-controller"
    elif corruption == "wrong_lineage":
        record["invocation_id"] = "unrelated-invocation"
    elif corruption == "wrong_branch":
        record["branch"] = "main.unrelated"
    registration.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child["protected_declaration_proofs"][0]

    assert not report["can_apply"]
    assert proof["reason_code"] == reason_code
    assert proof["correlation"]["expected"] == "agent-architecture"
    assert proof["correlation"]["dispatch_event_observed_emitter_agent_id"] == (
        "agent-architecture"
    )
    assert _tree_bytes(state_root) == before


def test_legacy_dispatch_requires_proven_parent_scope_identity(tmp_path: Path) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    root_document = json.loads(root_store.path.read_text(encoding="utf-8"))
    root_document["parent_agent_id"] = "unexpected-parent"
    root_store.path.write_text(json.dumps(root_document), encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = next(scope for scope in report["scopes"] if scope["scope_id"] == "root")
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert root["scope_emitter_proof"]["reason_code"] == ("scope_controller_root_identity_unproven")
    assert child["protected_declaration_proofs"][0]["reason_code"] == (
        "scope_controller_parent_identity_unproven"
    )
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("corruption", "reason_code"),
    [
        ("missing_event", "legacy_dispatch_event_missing"),
        ("malformed_event", "legacy_dispatch_event_ledger_invalid"),
        ("duplicate_event", "legacy_dispatch_event_ambiguous"),
        ("conflicting_invocation", "legacy_dispatch_result_identity_conflict"),
        ("wrong_branch", "legacy_dispatch_coordinates_mismatch"),
        ("missing_worktree", "legacy_spawn_result_worktree_missing"),
        ("wrong_owner", "legacy_dispatch_coordinates_mismatch"),
        ("wrong_emitter_parent", "legacy_scope_emitter_mismatch"),
        ("wrong_emitter_child", "legacy_scope_emitter_mismatch"),
        ("wrong_emitter_unrelated", "legacy_scope_emitter_mismatch"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_recovery_fails_closed_on_conflicting_or_missing_evidence(
    tmp_path: Path,
    corruption: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, _root, child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    ledger = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    if corruption == "missing_event":
        ledger.unlink()
    elif corruption == "malformed_event":
        ledger.write_text("not-json\n", encoding="utf-8")
    elif corruption == "duplicate_event":
        ledger.write_text(ledger.read_text(encoding="utf-8") * 2, encoding="utf-8")
    elif corruption == "conflicting_invocation":
        _mutate_completed_spawn_result(child_store, "invocation_id", "unrelated-invocation")
    elif corruption == "wrong_branch":
        _mutate_completed_spawn_result(child_store, "branch_name", "main.other")
    elif corruption == "missing_worktree":
        _mutate_completed_spawn_result(child_store, "worktree_path", None)
    elif corruption == "wrong_owner":
        event = json.loads(ledger.read_text(encoding="utf-8"))
        event["data"]["child_agent"] = "unrelated-agent"
        ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    elif corruption.startswith("wrong_emitter_"):
        emitter = {
            "wrong_emitter_parent": "root",
            "wrong_emitter_child": "agent-completed",
            "wrong_emitter_unrelated": "unrelated-controller",
        }[corruption]
        event = json.loads(ledger.read_text(encoding="utf-8"))
        event["agent_id"] = emitter
        ledger.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item for item in report["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert report["can_apply"] is False
    assert child_report["protected_declaration_proofs"][0]["reason_code"] == reason_code
    if corruption.startswith("wrong_emitter_"):
        proof = child_report["protected_declaration_proofs"][0]
        assert proof["correlation"]["dispatch_event_expected_emitter_agent_id"] == (
            "agent-architecture"
        )
        assert proof["correlation"]["dispatch_event_observed_emitter_agent_id"] == emitter
    assert _tree_bytes(state_root) == before


def test_legacy_dispatch_resolves_sub_tl_controller_alias(tmp_path: Path) -> None:
    alias = "architecture-controller-alias"
    _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        controller_alias=alias,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child["protected_declaration_proofs"][0]

    assert report["can_apply"]
    assert child["scope_emitter_proof"]["agent_id"] == alias
    assert proof["disposition"] == "proven"
    assert proof["correlation"]["dispatch_event_expected_emitter_agent_id"] == alias
    assert proof["correlation"]["dispatch_event_observed_emitter_agent_id"] == alias


@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_scope_controller_registration_recovers_original_owner_after_aggregate_handoff(
    tmp_path: Path,
    parent_attempts: int,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    root = root_store.load()
    controller_branch = "main.agent-architecture"
    controller_worktree = "/tmp/architecture/agent-architecture"
    owner_id = "root:architecture:integration"
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        integration_owner_id=owner_id,
        integration_owner_run_id="architecture",
        integration_owner_branch=controller_branch,
        integration_owner_worktree=controller_worktree,
    )
    slices = {
        **root.slices,
        "architecture": replace(
            root.slices["architecture"],
            dispatch_agent_id=owner_id,
        ),
    }
    root_store.checkpoint(
        root.recursive_fsm,
        slices,
        root.budgets,
        root.events.last_consumed_offset,
        current_order=root.current_order,
        ordered_stages=root.ordered_stages,
        integration=replace(root.integration, candidates={"architecture": candidate}),
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child["protected_declaration_proofs"][0]

    assert report["can_apply"]
    assert proof["disposition"] == "proven"
    assert proof["correlation"]["scope_controller_agent_id"] == "agent-architecture"
    assert proof["correlation"]["parent_dispatch_agent_id"] == owner_id
    assert proof["correlation"]["parent_dispatch_owner_record"] == (
        "integration_owner_with_verified_registration"
    )
    assert proof["correlation"]["parent_dispatch_attempts"] == parent_attempts
    assert state_root.is_dir()


@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_integration_owner_id_alone_does_not_prove_scope_controller(
    tmp_path: Path,
    parent_attempts: int,
) -> None:
    _state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    root = root_store.load()
    owner_id = "root:architecture:integration"
    slices = {
        **root.slices,
        "architecture": replace(root.slices["architecture"], dispatch_agent_id=owner_id),
    }
    root_store.checkpoint(
        root.recursive_fsm,
        slices,
        root.budgets,
        root.events.last_consumed_offset,
        current_order=root.current_order,
        ordered_stages=root.ordered_stages,
        integration=root.integration,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope for scope in report["scopes"] if scope["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert child["scope_emitter_proof"]["reason_code"] == ("scope_controller_parent_owner_conflict")


@pytest.mark.parametrize("parent_attempts", [0, 1])
@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("intent", "scope_controller_parent_dispatch_unproven"),
        ("never_dispatched", "scope_controller_parent_dispatch_unproven"),
        ("invocation", "scope_controller_parent_dispatch_unproven"),
        ("sequence", "scope_controller_parent_dispatch_unproven"),
        ("branch", "scope_controller_parent_dispatch_unproven"),
        ("worktree", "scope_controller_parent_dispatch_unproven"),
        ("base", "scope_controller_parent_dispatch_unproven"),
        ("record", "scope_controller_parent_dispatch_conflict"),
        ("owner", "scope_controller_parent_owner_conflict"),
    ],
)
def test_parent_dispatch_proof_requires_complete_coordinates_at_any_attempt_count(
    tmp_path: Path,
    parent_attempts: int,
    corruption: str,
    expected_reason: str,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    state = root_store.load()
    parent_slice = state.slices["architecture"]
    changes = {
        "intent": {"dispatch_intent_id": None},
        "never_dispatched": {
            "dispatch_intent_id": None,
            "dispatch_agent_id": None,
            "dispatch_invocation_id": None,
            "dispatch_authoritative_event_seq": None,
            "branch": None,
            "worktree": None,
            "base_ref": None,
        },
        "invocation": {"dispatch_invocation_id": None},
        "sequence": {"dispatch_authoritative_event_seq": 0},
        "branch": {"branch": "main.unrelated"},
        "worktree": {"worktree": "/tmp/unrelated"},
        "base": {"base_ref": "unrelated"},
        "owner": {"dispatch_agent_id": "unrelated-owner"},
    }
    phase = state.recursive_fsm
    slices = {
        **state.slices,
        "architecture": replace(parent_slice, **changes.get(corruption, {})),
    }
    if corruption == "record":
        records = phase.pending_by_order[1]
        phase = replace(
            phase,
            pending_by_order={
                **phase.pending_by_order,
                1: (replace(records[0], dispatch_intent_id="conflicting-intent"),),
            },
        )
    root_store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        item for item in report["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert child["scope_emitter_proof"]["reason_code"] == expected_reason
    assert child["scope_emitter_proof"]["correlation"]["parent_dispatch_attempts"] == (
        parent_attempts
    )
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize("attempts", [-1, True, "0"])
def test_invalid_parent_attempt_counter_fails_runstore_validation_without_writes(
    tmp_path: Path,
    attempts: object,
) -> None:
    state_root, root_store, _child_store, *_ = _project(tmp_path)
    document = json.loads(root_store.path.read_text(encoding="utf-8"))
    document["slices"]["architecture"]["attempts"] = attempts
    root_store.path.write_text(json.dumps(document), encoding="utf-8")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "runstore_invalid"
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("emitter", "expected_can_apply", "expected_disposition", "expected_reason"),
    [
        ("root", True, "proven", "proven"),
        ("unrelated-controller", False, "blocked", "legacy_scope_emitter_mismatch"),
    ],
)
def test_root_scope_requires_its_proven_controller_emitter(
    tmp_path: Path,
    emitter: str,
    expected_can_apply: bool,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    _direct_root_legacy_project(tmp_path, emitting_controller_id=emitter)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = report["scopes"][0]
    proof = root["protected_declaration_proofs"][0]

    assert root["scope_emitter_proof"]["agent_id"] == "root"
    assert proof["disposition"] == expected_disposition
    assert proof["reason_code"] == expected_reason
    assert proof["correlation"]["dispatch_event_expected_emitter_agent_id"] == "root"
    assert proof["correlation"]["dispatch_event_observed_emitter_agent_id"] == emitter
    assert report["can_apply"] is expected_can_apply


def test_completed_declaration_restore_blocks_without_spawn_journal(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item for item in report["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child_report["protected_declaration_proofs"][0]

    assert report["can_apply"] is False
    assert proof["disposition"] == "blocked"
    assert proof["reason_code"] == "action_journal_missing"
    assert any(
        item["code"] == "protected_declaration_evidence_unproven" for item in report["blockers"]
    )
    assert _tree_bytes(state_root) == before


def test_completed_declaration_restore_blocks_when_spawn_task_conflicts(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        spawn_task_override="different dispatch task",
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item for item in report["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = child_report["protected_declaration_proofs"][0]

    assert report["can_apply"] is False
    assert proof["reason_code"] == "spawn_declaration_mismatch"
    assert _tree_bytes(state_root) == before


def test_malformed_spawn_journal_is_reported_as_unproven_evidence(tmp_path: Path) -> None:
    state_root, _root, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
    )
    journal = child_store.run_dir / "action-journal.json"
    journal.write_bytes(b"not-json")

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item for item in report["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert report["can_apply"] is False
    assert child_report["action_journal"]["status"] == "invalid"
    assert child_report["protected_declaration_proofs"][0]["reason_code"] == (
        "action_journal_invalid"
    )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


def test_action_journal_change_invalidates_completed_declaration_proof(tmp_path: Path) -> None:
    state_root, _root, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    journal = child_store.run_dir / "action-journal.json"
    journal.write_bytes(journal.read_bytes() + b" ")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "reject stale declaration evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


def test_apply_requires_the_exact_dry_run_fingerprint(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    before = _tree_bytes(state_root)
    with pytest.raises(PlanRevisionError, match="inputs changed") as error:
        apply_plan_reconciliation(tmp_path, "restore", "0" * 64, actor="operator")
    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    "relative_path",
    ("plan.json", "root/run.json", "root/architecture/run.json"),
)
def test_apply_rejects_changed_inputs_after_dry_run(
    tmp_path: Path,
    relative_path: str,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    changed_path = state_root / relative_path
    changed_path.write_bytes(changed_path.read_bytes() + b" ")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "reject a stale candidate",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


def test_apply_refuses_pending_plan_revision_journal_without_replaying_it(
    tmp_path: Path,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    journal = state_root / "plan-revision-transition.json"
    journal.write_text("pending recovery", encoding="utf-8")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "do not mix transaction replay with reconciliation",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "transition_pending"
    assert _tree_bytes(state_root) == before


def test_apply_rejects_controller_lifecycle_lock_timeout(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    report = analyze_plan_reconciliation(tmp_path)
    assert report.can_apply
    with RunLock(state_root / "controller-lifecycle.lock"), pytest.raises(LockTimeout):
        apply_plan_reconciliation(
            tmp_path,
            "restore",
            report.evidence_fingerprint,
            actor="operator",
            lock_timeout=0.02,
        )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


@pytest.mark.parametrize("lock_path", ["plan-revision.lock", "run.lock", "root/run.lock"])
def test_apply_rejects_revision_and_scope_lock_timeouts(
    tmp_path: Path,
    lock_path: str,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    with RunLock(state_root / lock_path), pytest.raises(LockTimeout):
        apply_plan_reconciliation(
            tmp_path,
            "wait for the scope lock",
            report.evidence_fingerprint,
            actor="operator",
            lock_timeout=0.02,
        )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


@pytest.mark.parametrize("interrupt_index", [0, 1, 2])
def test_interrupted_apply_recovers_without_duplicate_audit(
    tmp_path: Path,
    interrupt_index: int,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)

    def interrupt_after_target(index: int, _target: str) -> None:
        if index == interrupt_index:
            raise RuntimeError("simulated interruption")

    result = apply_plan_reconciliation(
        tmp_path,
        "recover an interrupted reconciliation",
        report.evidence_fingerprint,
        actor="operator",
        after_target=interrupt_after_target,
    )

    assert result.applied and result.recovered_during_apply
    records = (state_root / "plan-reconciliations.jsonl").read_text().splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["evidence_fingerprint"] == report.evidence_fingerprint
    assert not (state_root / "plan-revision-transition.json").exists()


def test_pending_reconciliation_journal_replays_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root, root_store, _child, _accepted, _candidate = _project(
        tmp_path,
        parent_attempts=0,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    report = analyze_plan_reconciliation(tmp_path)

    def stop_recovery(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated process loss")

    import tl_loop.state.plan_reconciliation_apply as apply_module

    monkeypatch.setattr(apply_module, "_recover_locked", stop_recovery)

    def interrupt_after_first(_index: int, _target: str) -> None:
        raise RuntimeError("simulated process loss")

    with pytest.raises(PlanRevisionError, match="remains journaled"):
        apply_plan_reconciliation(
            tmp_path,
            "recover after restart",
            report.evidence_fingerprint,
            actor="operator",
            after_target=interrupt_after_first,
        )
    assert (state_root / "plan-revision-transition.json").exists()
    monkeypatch.undo()

    assert recover_plan_revision(tmp_path)
    assert not (state_root / "plan-revision-transition.json").exists()
    records = (state_root / "plan-reconciliations.jsonl").read_text().splitlines()
    assert len(records) == 1
    audit = json.loads(records[0])
    root_attempts = root_store.load().slices["architecture"].attempts
    assert root_attempts == 0
    child_scope = next(
        item for item in audit["scopes"] if item["scope_id"] == "root/sub_tl/architecture"
    )
    assert child_scope["scope_emitter_proof"]["correlation"]["parent_dispatch_attempts"] == 0
    assert not analyze_plan_reconciliation(tmp_path).has_drift


def test_symlinked_snapshot_and_bad_digest_fail_closed(tmp_path: Path) -> None:
    state_root, _root, _child, accepted, _candidate = _project(tmp_path)
    snapshot = state_root / "plan.snapshot"
    snapshot.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(accepted)
    snapshot.symlink_to(outside)
    with pytest.raises(PlanRevisionError, match="symlink"):
        analyze_plan_reconciliation(tmp_path)

    snapshot.unlink()
    snapshot.write_bytes(accepted)
    (state_root / "plan.snapshot.sha256").write_text("bad\n", encoding="ascii")
    with pytest.raises(PlanRevisionError, match="SHA-256"):
        analyze_plan_reconciliation(tmp_path)

    malformed = b"{"
    snapshot.write_bytes(malformed)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(malformed).hexdigest() + "\n", encoding="ascii"
    )
    with pytest.raises(
        PlanRevisionError, match="accepted snapshot plan failed validation"
    ) as error:
        analyze_plan_reconciliation(tmp_path)
    assert error.value.code == "plan_invalid"


def test_missing_child_runstore_evidence_blocks_restoration(tmp_path: Path) -> None:
    _state_root, _root_store, child_store, _accepted, _candidate = _project(tmp_path)
    child_store.path.unlink()

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert any(item["code"] == "materialized_scope_missing" for item in report["blockers"])


def test_malformed_materialized_runstore_fails_closed(tmp_path: Path) -> None:
    _state_root, _root_store, child_store, _accepted, _candidate = _project(tmp_path)
    child_store.path.write_bytes(b"{")

    with pytest.raises(PlanRevisionError, match="failed validation") as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "runstore_invalid"


def test_operator_cli_emits_structured_dry_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _project(tmp_path)
    from tl_loop.__main__ import main

    exit_code = main(
        [
            "plan-reconciliation",
            "--project-root",
            str(tmp_path),
            "--dry-run",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["operation"] == "plan_reconciliation"
    assert report["evidence_fingerprint"]
