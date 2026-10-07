"""Contracts for optional, slice-scoped Chainlink issue bindings."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from dataclasses import replace

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import ServerTimeout
from tl_loop.fsm.lane import LanePhase, LaneState
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.loop.driver import (
    CHAINLINK_BINDING_RECONCILIATION_GATE_NAME,
    ChainlinkIssueBindingError,
    EffectIntent,
    EffectJournal,
    TLLoopConfig,
    TLLoopError,
    WorkPlan,
    _advance_post_merge_boundary,
    _child_config,
    run_tl_loop,
    tl_run,
)
from tl_loop.rlm.plan_authoring import slices_to_plan_document
from tl_loop.rlm.slice_spec import SliceSpec
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.schema import GateStatus, RepositoryIdentity, RunState, SliceStatus
from tl_loop.state.store import RunStore, create


def test_binding_round_trips_through_work_plan_and_recursive_manifest() -> None:
    document = {
        "sub_tls": [
            {
                "name": "stage",
                "order": 1,
                "plan": {
                    "leaves": [
                        {
                            "name": "leaf",
                            "task": "implement work",
                            "chainlink_issue_id": 1167,
                        }
                    ]
                },
            }
        ]
    }

    plan = WorkPlan.from_mapping(document)
    manifest = build_plan_manifest(document, scope_id="root")
    restored = PlanManifest.from_document(manifest.to_document())
    reconstructed = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "stage",
                    "order": 1,
                    "plan": {"leaves": [{"name": "leaf", "task": "implement work"}]},
                }
            ]
        }
    )

    assert plan.sub_tls[0].plan.leaves[0].chainlink_issue_id == 1167
    assert restored.child_manifests["root/sub_tl/stage"].node(
        "root/sub_tl/stage/leaf/leaf"
    ).chainlink_issue_id == 1167
    assert reconstructed.sub_tls[0].plan.leaves[0].chainlink_issue_id is None


@pytest.mark.parametrize("value", [0, -1, True, 1167.0, "1167"])
def test_binding_rejects_non_positive_or_non_integer_values(value: object) -> None:
    with pytest.raises(ChainlinkIssueBindingError, match="chainlink_issue_id"):
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf", "task": "work", "chainlink_issue_id": value}]}
        )


def test_binding_rejects_duplicate_ids_across_recursive_slices() -> None:
    with pytest.raises(ChainlinkIssueBindingError, match="bind each issue to one slice"):
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {"name": "one", "task": "one", "chainlink_issue_id": 91}
                ],
                "sub_tls": [
                    {
                        "name": "stage",
                        "order": 1,
                        "plan": {
                            "leaves": [
                                {"name": "two", "task": "two", "chainlink_issue_id": 91}
                            ]
                        },
                    }
                ],
            }
        )


def test_malformed_binding_opens_reconciliation_gate(tmp_path: Path) -> None:
    config = TLLoopConfig(root_dir=tmp_path, run_id="invalid-binding")

    with pytest.raises(TLLoopError, match=CHAINLINK_BINDING_RECONCILIATION_GATE_NAME):
        tl_run(
            {
                "run_id": "invalid-binding",
                "plan": {
                    "leaves": [
                        {"name": "leaf", "task": "work", "chainlink_issue_id": 0}
                    ]
                },
            },
            config,
            {"tokens": 0, "wall_seconds": 0},
        )

    state = RunStore("invalid-binding", tmp_path).load()
    assert any(
        gate.name == CHAINLINK_BINDING_RECONCILIATION_GATE_NAME
        for gate in state.gates
    )


def test_direct_loop_entrypoint_gates_malformed_binding(tmp_path: Path) -> None:
    class Transport:
        def call_tool(self, **arguments: object) -> dict[str, object]:
            del arguments
            raise AssertionError("invalid plans must be rejected before effects")

    config = TLLoopConfig(root_dir=tmp_path, run_id="direct-invalid-binding")
    with pytest.raises(TLLoopError, match=CHAINLINK_BINDING_RECONCILIATION_GATE_NAME):
        run_tl_loop(
            "direct-invalid-binding",
            {"leaves": [{"name": "leaf", "task": "work", "chainlink_issue_id": True}]},
            object(),
            EffectClient(Transport()),
            config=config,
            root_dir=tmp_path,
        )

    state = RunStore("direct-invalid-binding", tmp_path).load()
    assert any(
        gate.name == CHAINLINK_BINDING_RECONCILIATION_GATE_NAME
        for gate in state.gates
    )


def test_old_manifest_and_checkpoint_decode_as_unlinked(tmp_path: Path) -> None:
    manifest = build_plan_manifest(
        {"leaves": [{"name": "leaf", "task": "work"}]},
        scope_id="old-run",
    )
    node = manifest.node("old-run/leaf/leaf")
    create("old-run", {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)

    restored = RunStore("old-run", tmp_path).load()

    assert "chainlink_issue_id" not in node.to_document()
    assert restored.slices["leaf"].chainlink_issue_id is None
    assert restored.plan_manifest == manifest


def test_unlinked_post_merge_persists_not_applicable_without_effects(tmp_path: Path) -> None:
    class Transport:
        calls: list[tuple[str, dict[str, object]]]

        def __init__(self) -> None:
            self.calls = []

        def call_tool(self, role: str, name: str, tool_name: str, arguments: dict[str, object]):
            del role, name
            self.calls.append((tool_name, arguments))
            raise AssertionError("unlinked completion must make no effect calls")

    plan = {"leaves": [{"name": "leaf", "task": "work"}]}
    manifest = build_plan_manifest(plan, scope_id="unlinked")
    identity = RepositoryIdentity("org", "repo", "main")
    create(
        "unlinked",
        {
            "plan_manifest": manifest.to_document(),
            "repository_identity": {
                "owner": identity.owner,
                "repo": identity.repo,
                "base_branch": identity.base_branch,
            },
        },
        root_dir=tmp_path,
    )
    store = RunStore("unlinked", tmp_path)
    state = store.load()
    current = replace(
        state.slices["leaf"],
        status=SliceStatus.MERGED,
        base_ref="main",
        pr_number=42,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                "child_id": "leaf",
                "repository": "org/repo",
                "parent_branch": "main",
                "pr_number": "42",
                "head_sha": "head-a",
                "merge_journal_id": "merge-journal",
                "lane_epoch": "1",
                "parent_commit_sha": "parent-commit",
            },
        ),
    )
    integration = replace(
        state.integration,
        lanes={
            "org/repo:main": LaneState(
                "org/repo",
                "main",
                phase=LanePhase.INTEGRATING,
                child_id="leaf",
                lane_epoch=1,
                head_sha="head-a",
            )
        },
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=integration,
        plan_manifest=manifest,
    )
    transport = Transport()

    completed = _advance_post_merge_boundary(
        state,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )

    final = completed.slices["leaf"].post_merge
    assert final is not None and final.phase is PostMergePhase.COMPLETE
    assert final.evidence["issue_close_outcome"] == "not_applicable"
    assert final.evidence["changelog_outcome"] == "not_applicable"
    assert completed.integration.lanes["org/repo:main"].phase is LanePhase.IDLE
    assert transport.calls == []


class IssueCloseTransport:
    def __init__(
        self,
        issue_id: int,
        *,
        issue_status: str = "open",
        close_mode: str = "success",
    ) -> None:
        self.issue_id = issue_id
        self.issue_status = issue_status
        self.close_mode = close_mode
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "chainlink_issue_show":
            return {
                "success": True,
                "result": {
                    "id": self.issue_id,
                    "title": "linked work",
                    "status": self.issue_status,
                },
            }
        if tool_name == "chainlink_issue_close":
            return self._close_result()
        if tool_name == "emit_controller_event":
            return {"success": True, "result": {}}
        raise AssertionError(f"unexpected effect {tool_name}")

    def _close_result(self) -> dict[str, object]:
        close_count = len([name for name, _ in self.calls if name == "chainlink_issue_close"])
        if self.close_mode == "timeout":
            raise ServerTimeout("issue close timed out")
        if self.close_mode == "unavailable":
            return {
                "success": False,
                "error": "close tool unavailable",
                "error_kind": "tool_unavailable",
            }
        if self.close_mode == "rejected":
            return {"success": False, "error": "tracker rejected close"}
        if self.close_mode == "reject_once" and close_count == 1:
            return {"success": False, "error": "tracker temporarily rejected close"}
        return {
            "success": True,
            "result": {
                "issue_id": self.issue_id,
                "receipt_id": f"chainlink-close:{self.issue_id}",
            },
        }


def _linked_parent_sync_state(
    root_dir: Path,
    *,
    run_id: str = "linked",
    issue_id: int = 702,
) -> tuple[RunStore, RunState, RepositoryIdentity]:
    identity = RepositoryIdentity("org", "repo", "main")
    plan = {
        "leaves": [
            {"name": "leaf", "task": "work", "chainlink_issue_id": issue_id}
        ]
    }
    manifest = build_plan_manifest(plan, scope_id=run_id)
    create(
        run_id,
        {
            "plan_manifest": manifest.to_document(),
            "repository_identity": {
                "owner": identity.owner,
                "repo": identity.repo,
                "base_branch": identity.base_branch,
            },
        },
        root_dir=root_dir,
    )
    store = RunStore(run_id, root_dir)
    state = store.load()
    current = replace(
        state.slices["leaf"],
        status=SliceStatus.MERGED,
        base_ref="main",
        pr_number=42,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                "child_id": "leaf",
                "repository": "org/repo",
                "parent_branch": "main",
                "pr_number": "42",
                "head_sha": "head-a",
                "merge_journal_id": "merge-journal",
                "lane_epoch": "1",
                "parent_commit_sha": "parent-commit",
            },
        ),
    )
    integration = replace(
        state.integration,
        lanes={
            "org/repo:main": LaneState(
                "org/repo",
                "main",
                phase=LanePhase.INTEGRATING,
                child_id="leaf",
                lane_epoch=1,
                head_sha="head-a",
            )
        },
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=integration,
        plan_manifest=manifest,
    )
    return store, state, identity


def _pending_issue_close(
    root_dir: Path,
    transport: IssueCloseTransport,
    *,
    run_id: str = "linked",
    issue_id: int = 702,
) -> tuple[RunStore, RunState, RepositoryIdentity]:
    store, state, identity = _linked_parent_sync_state(
        root_dir, run_id=run_id, issue_id=issue_id
    )
    pending = _advance_post_merge_boundary(
        state,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )
    return store, pending, identity


def test_linked_issue_close_uses_exact_binding_and_receipt(tmp_path: Path) -> None:
    issue_id = 702
    transport = IssueCloseTransport(issue_id)
    store, pending, identity = _pending_issue_close(tmp_path, transport)
    pending_close = pending.slices["leaf"].post_merge
    assert pending_close is not None
    assert pending_close.phase is PostMergePhase.ISSUE_CLOSE_PENDING
    assert pending_close.evidence["issue_id"] == str(issue_id)

    confirmed = _advance_post_merge_boundary(
        pending,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )

    close_state = confirmed.slices["leaf"].post_merge
    assert close_state is not None
    assert close_state.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert close_state.evidence["issue_close_outcome"] == "closed"
    assert close_state.evidence["issue_close_journal_id"] == f"chainlink-close:{issue_id}"
    close_calls = [arguments for name, arguments in transport.calls if name == "chainlink_issue_close"]
    assert close_calls == [{"issue_id": issue_id, "force": True, "commit_changelog": False, "summary": "Closed after successful merge of PR #42"}]


def test_linked_already_closed_issue_is_adopted_without_receipt(tmp_path: Path) -> None:
    issue_id = 703
    transport = IssueCloseTransport(issue_id, issue_status="closed")
    store, pending, identity = _pending_issue_close(
        tmp_path, transport, run_id="already-closed", issue_id=issue_id
    )

    confirmed = _advance_post_merge_boundary(
        pending,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )

    close_state = confirmed.slices["leaf"].post_merge
    assert close_state is not None
    assert close_state.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert close_state.evidence["issue_close_outcome"] == "already_closed"
    assert close_state.evidence["issue_close_observed_issue_id"] == str(issue_id)
    assert close_state.evidence["issue_close_observed_status"] == "closed"
    assert "issue_close_journal_id" not in close_state.evidence
    assert not any(name == "chainlink_issue_close" for name, _ in transport.calls)


@pytest.mark.parametrize(
    ("close_mode", "expected_kind"),
    [
        ("unavailable", "unavailable"),
        ("timeout", "timeout"),
        ("rejected", "rejected"),
    ],
)
def test_linked_issue_close_failures_are_typed_and_recoverable(
    tmp_path: Path, close_mode: str, expected_kind: str
) -> None:
    issue_id = 704
    transport = IssueCloseTransport(issue_id, close_mode=close_mode)
    run_id = f"close-{close_mode}"
    store, pending, identity = _pending_issue_close(
        tmp_path, transport, run_id=run_id, issue_id=issue_id
    )

    failed = _advance_post_merge_boundary(
        pending,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )

    close_state = failed.slices["leaf"].post_merge
    assert close_state is not None
    assert close_state.phase is PostMergePhase.ISSUE_CLOSE_PENDING
    assert close_state.evidence["issue_id"] == str(issue_id)
    assert close_state.evidence["issue_close_failure_generation"] == "1"
    assert close_state.evidence["issue_close_failure_1_kind"] == expected_kind
    assert close_state.evidence["issue_close_failure_1_observed_status"] == "open"
    assert "issue_close_journal_id" not in close_state.evidence
    assert any(
        gate.name == "tl-post-merge-issue-close-leaf-1"
        and gate.status is GateStatus.PENDING
        for gate in failed.gates
    )


def test_confirmed_close_receipt_replays_without_second_close(tmp_path: Path) -> None:
    issue_id = 705
    transport = IssueCloseTransport(issue_id, issue_status="closed")
    store, pending, identity = _pending_issue_close(
        tmp_path, transport, run_id="confirmed-replay", issue_id=issue_id
    )
    close_state = pending.slices["leaf"].post_merge
    assert close_state is not None
    intent = EffectIntent(
        "post_merge_issue_close",
        "leaf",
        {
            "issue_id": issue_id,
            "force": True,
            "commit_changelog": False,
            "intent_id": close_state.evidence["issue_close_intent_id"],
        },
        True,
    )
    journal = EffectJournal("confirmed-replay", store.run_dir / "action-journal.json")
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    "issue_id": issue_id,
                    "receipt_id": f"chainlink-close:{issue_id}",
                },
            }
        ),
    )

    confirmed = _advance_post_merge_boundary(
        pending,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        journal,
        store,
    )

    final = confirmed.slices["leaf"].post_merge
    assert final is not None and final.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert final.evidence["issue_close_journal_id"] == f"chainlink-close:{issue_id}"
    assert not any(name in {"chainlink_issue_close", "chainlink_issue_show"} for name, _ in transport.calls)


def test_approved_issue_close_retry_requires_open_status_and_new_intent(
    tmp_path: Path,
) -> None:
    issue_id = 706
    transport = IssueCloseTransport(issue_id, close_mode="reject_once")
    run_id = "close-retry"
    store, pending, identity = _pending_issue_close(
        tmp_path, transport, run_id=run_id, issue_id=issue_id
    )
    journal = EffectJournal(run_id, store.run_dir / "action-journal.json")

    failed = _advance_post_merge_boundary(
        pending,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        journal,
        store,
    )
    store.answer_gate("tl-post-merge-issue-close-leaf-1", GateStatus.APPROVED)

    confirmed = _advance_post_merge_boundary(
        store.load(),
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        journal,
        store,
    )

    final = confirmed.slices["leaf"].post_merge
    assert final is not None and final.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert final.evidence["issue_close_attempt"] == "2"
    close_entries = journal.confirmed_entries("post_merge_issue_close", "leaf")
    assert len(close_entries) == 1
    assert close_entries[0]["arguments"]["retry_attempt"] == 2
    assert len([name for name, _ in transport.calls if name == "chainlink_issue_close"]) == 2


def test_child_controller_config_has_no_parent_slice_issue_binding(tmp_path: Path) -> None:
    parent_plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "child",
                    "order": 1,
                    "chainlink_issue_id": 318,
                    "plan": {"leaves": [{"name": "leaf", "task": "child work"}]},
                }
            ]
        }
    )
    create("parent", {}, root_dir=tmp_path)
    store = RunStore("parent", tmp_path)
    child = _child_config(
        TLLoopConfig(root_dir=tmp_path),
        parent_plan.sub_tls[0],
        object(),
        object(),
        store,
        "main.child",
        str(tmp_path / "child"),
    )

    assert parent_plan.sub_tls[0].chainlink_issue_id == 318
    assert "chainlink_issue_id" not in {field.name for field in fields(TLLoopConfig)}
    assert not hasattr(child, "chainlink_issue_id")


def test_plan_authoring_preserves_explicit_slice_binding() -> None:
    authored = SliceSpec(
        id="leaf",
        title="implement work",
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        steps=("implement",),
        verify=("just tl-loop-test",),
        boundary=("src/leaf.py",),
        done_criteria=("work is complete",),
        chainlink_issue_id=702,
    )

    document = slices_to_plan_document([authored], run_id="authored")

    assert document["plan"]["leaves"][0]["chainlink_issue_id"] == 702
