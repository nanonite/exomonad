"""Startup reconciliation recovers a missing PR identity and executes it.

Covers the #904 verification finding: reconciliation used to query watcher
state only when pr_number was already persisted, so a slice whose pr.filed
was acknowledged but never associated with the checkpoint (the motivating
PR #42 shape) could never recover and its reviewer would never spawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import TLLoopConfig, _reconcile_nonterminal_slices
from tl_loop.state.schema import SliceState, SliceStatus
from tl_loop.state.store import RunStore, _encode_slice, create


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
    resolved_pr_number: int | None = 99

    def list_agents(self, *, filter_type: str | None = None) -> ToolResult:
        return ToolResult(
            raw={"success": True},
            success=True,
            result={
                "agents": [
                    {"agent_id": "agent-a", "intent_id": "intent-a", "is_alive": True}
                ]
            },
            error=None,
        )

    def watcher_pr_state(
        self, *, pr_number: int | None = None, slice_id: str | None = None
    ) -> ToolResult:
        self.watcher_calls.append({"pr_number": pr_number, "slice_id": slice_id})
        if slice_id == "slice-a" and self.resolved_pr_number is not None:
            return ToolResult(
                raw={"success": True},
                success=True,
                result={
                    "found": True,
                    "pr_number": self.resolved_pr_number,
                    "head_sha": "head-a",
                    "review_state": "approved",
                    "ci_status": "success",
                    "merged": False,
                },
                error=None,
            )
        return ToolResult(
            raw={"success": False},
            success=False,
            result=None,
            error=f"no published PR found for slice_id '{slice_id}'",
        )


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
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1")

    new_state = _reconcile_nonterminal_slices(state, config, client, store, [])

    assert [call["slice_id"] for call in client.watcher_calls] == ["slice-a"]
    slice_state = new_state.slices["slice-a"]
    assert slice_state.pr_number == 99
    assert slice_state.reconciliation is not None
    assert "pr_number" not in slice_state.reconciliation["missing_evidence"]
    assert "published_pr" in slice_state.reconciliation["authoritative_evidence"]
    assert slice_state.reconciliation["next_action"] != "await_authoritative_evidence"

    # The recovered pr_number is durable, not just an in-memory observation.
    assert store.load().slices["slice-a"].pr_number == 99


def test_reconciliation_leaves_pr_number_missing_when_nothing_was_ever_published(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient(resolved_pr_number=None)
    config = TLLoopConfig(active=True, ledger_run_id="run-1")

    new_state = _reconcile_nonterminal_slices(state, config, client, store, [])

    slice_state = new_state.slices["slice-a"]
    assert slice_state.pr_number is None
    assert slice_state.reconciliation["missing_evidence"] == ["pr_number"]
    assert slice_state.reconciliation["next_action"] == "await_authoritative_evidence"


def test_reconciliation_skips_slice_id_lookup_when_ledger_run_id_unset(tmp_path) -> None:
    store, state = _load_state(tmp_path)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id=None)

    _reconcile_nonterminal_slices(state, config, client, store, [])

    assert client.watcher_calls == []
