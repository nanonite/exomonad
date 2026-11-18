"""Fixture builders for plan-reconciliation safety tests.

The builders are importable without a test runner so Rust integration tests can
materialize the same historical workspaces the Python suite inspects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import (
    PhaseValue as ScopePhase,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLFinalizing,
    TLPlanning,
    TLParked,
    TLPRFiled,
    TLRunning,
)
from tl_loop.fsm.scope_events import ScopeRole
from tl_loop.loop.driver import EffectIntent, WorkPlan, _manifest_plan
from tl_loop.loop.journal import EffectJournal
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.plan_manifest import ManifestNode, PlanManifest, build_plan_manifest
from tl_loop.state.schema import (
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    SliceStatus,
    SliceState,
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
) -> dict[str, object]:
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


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _records(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, Mapping) for item in value)
    return [item for item in value if isinstance(item, Mapping)]


def _strings(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return [item for item in value if isinstance(item, str)]


def _manifest(document: Mapping[str, object]) -> PlanManifest:
    plan = WorkPlan.from_mapping(_mapping(document["plan"]))
    return build_plan_manifest(_manifest_plan(plan), scope_id="root", owned_branch="main")


def _manifest_digest(manifest: PlanManifest | None) -> str:
    assert manifest is not None
    assert manifest.digest is not None
    return manifest.digest


def _required_manifest(manifest: PlanManifest | None) -> PlanManifest:
    assert manifest is not None
    return manifest


def _scope_phase(value: object) -> ScopePhase:
    if isinstance(
        value,
        (TLPlanning, TLRunning, TLAllMerged, TLFinalizing, TLDone, TLPRFiled, TLFailed, TLParked),
    ):
        return value
    raise AssertionError(f"expected recursive phase, got {type(value).__name__}")


def _pr_filed(value: object) -> TLPRFiled:
    phase = _scope_phase(value)
    assert isinstance(phase, TLPRFiled)
    return phase


def _replace_pr_filed_field(phase: TLPRFiled, field: str, value: object) -> TLPRFiled:
    if field == "scope_path":
        assert isinstance(value, tuple)
        assert all(isinstance(part, str) for part in value)
        return replace(phase, scope_path=tuple(part for part in value if isinstance(part, str)))
    assert isinstance(value, str)
    if field == "aggregate_pr":
        return replace(phase, aggregate_pr=value)
    if field == "head_sha":
        return replace(phase, head_sha=value)
    if field == "base_sha":
        return replace(phase, base_sha=value)
    if field == "parent_branch":
        return replace(phase, parent_branch=value)
    if field == "handoff":
        return replace(phase, handoff=value)
    raise AssertionError(f"unsupported TLPRFiled field: {field}")


def _required_post_merge(value: PostMergeState | None) -> PostMergeState:
    assert value is not None
    return value


def _running_phase(value: object) -> TLRunning:
    phase = _scope_phase(value)
    assert isinstance(phase, TLRunning)
    return phase


def _plan_digest(value: object) -> str:
    if isinstance(value, (TLPlanning, TLRunning, TLAllMerged, TLFinalizing, TLDone, TLPRFiled)):
        return value.plan_digest
    raise AssertionError(f"expected phase with plan digest, got {type(value).__name__}")


def _corrupt_parent_slice(parent_slice: SliceState, corruption: str) -> SliceState:
    if corruption == "intent":
        return replace(parent_slice, dispatch_intent_id=None)
    if corruption == "never_dispatched":
        return replace(
            parent_slice,
            dispatch_intent_id=None,
            dispatch_agent_id=None,
            dispatch_invocation_id=None,
            dispatch_authoritative_event_seq=None,
            branch=None,
            worktree=None,
            base_ref=None,
        )
    if corruption == "invocation":
        return replace(parent_slice, dispatch_invocation_id=None)
    if corruption == "sequence":
        return replace(parent_slice, dispatch_authoritative_event_seq=0)
    if corruption == "branch":
        return replace(parent_slice, branch="main.unrelated")
    if corruption == "worktree":
        return replace(parent_slice, worktree="/tmp/unrelated")
    if corruption == "base":
        return replace(parent_slice, base_ref="unrelated")
    if corruption == "owner":
        return replace(parent_slice, dispatch_agent_id="unrelated-owner")
    if corruption == "record":
        return parent_slice
    raise AssertionError(f"unsupported corruption: {corruption}")


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
        child_manifest_digests={
            node_id: _manifest_digest(child) for node_id, child in children.items()
        },
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
        child_manifest_digests={
            node_id: _manifest_digest(child) for node_id, child in children.items()
        },
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
        child_manifest_digests={
            node_id: _manifest_digest(child) for node_id, child in children.items()
        },
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
    terminal_aggregate_handoff: bool = False,
    terminal_handoff_phase: str = "filed",
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
        terminal_aggregate_handoff=terminal_aggregate_handoff,
        terminal_handoff_phase=terminal_handoff_phase,
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
            _scope_phase(root_state.recursive_fsm),
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
    terminal_aggregate_handoff: bool = False,
    terminal_handoff_phase: str = "filed",
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
    aggregate_pr = 46
    aggregate_head = "1" * 40
    aggregate_base = "2" * 40
    integration_owner = "root:architecture:integration"
    handoff = {
        "aggregate_pr": str(aggregate_pr),
        "head_sha": aggregate_head,
        "base_sha": aggregate_base,
        "parent_branch": "main",
        "handoff": integration_owner,
    }
    phase = (
        TLFinalizing(
            role=ScopeRole.NON_ROOT,
            scope_path=(current.scope_id,),
            plan_digest=runtime_digest or accepted.digest or "",
            evidence=handoff,
        )
        if terminal_aggregate_handoff and terminal_handoff_phase == "finalizing"
        else TLPRFiled(
            **handoff,
            scope_path=(current.scope_id,),
            plan_digest=runtime_digest or accepted.digest or "",
        )
        if terminal_aggregate_handoff
        else TLAllMerged(
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
    integration = (
        IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            aggregate_pr_number=aggregate_pr,
            aggregate_head_sha=aggregate_head,
            aggregate_patch_digest=hashlib.sha256(b"architecture aggregate patch").hexdigest(),
            aggregate_original_base_sha=aggregate_base,
            integration_owner_id=integration_owner,
            integration_owner_run_id="architecture",
            integration_owner_branch=f"main.{controller_alias}",
            integration_owner_worktree=f"/tmp/architecture/{controller_alias}",
            head_sha=aggregate_head,
            patch_digest=hashlib.sha256(b"architecture aggregate patch").hexdigest(),
        )
        if terminal_aggregate_handoff
        else state.integration
    )
    store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=current,
        integration=integration,
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


def _legacy_tlprfiled_project(tmp_path: Path) -> tuple[Path, RunStore, RunStore, bytes, bytes]:
    return _project(
        tmp_path,
        candidate_issue_id=None,
        legacy_aggregate_review_default=True,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        legacy_integration_coordinator_binding=True,
        legacy_completed_issue_binding=True,
        controller_alias="architecture",
        five_ordered_stages=True,
        terminal_aggregate_handoff=True,
    )


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
    result: JsonObject = (
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
    # The Rust ledger writer creates this file on its first append; a segment
    # without it is a state the reader treats as uncertain process history.
    (ledger / ".append.lock").touch()
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
