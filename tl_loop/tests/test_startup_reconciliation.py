"""Startup reconciliation recovers a missing PR identity and executes it.

Covers the #904 verification finding: reconciliation used to query watcher
state only when pr_number was already persisted, so a slice whose pr.filed
was acknowledged but never associated with the checkpoint (the motivating
PR #42 shape) could never recover and its reviewer would never spawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import (
    EffectIntent,
    LeafTask,
    TLLoopConfig,
    WorkPlan,
    _reconcile_nonterminal_slices,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import SliceState, SliceStatus
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
    resolved_pr_number: int | None = 99
    review_state: str = "pending"
    pr_state: str = "open"
    merged: bool = False
    head_reachable: bool = True
    publication_ownership_verified: bool | None = None

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
                "review_state": self.review_state,
                "ci_status": "success",
                "pr_state": self.pr_state,
                "merged": self.merged,
                "head_reachable": self.head_reachable,
            }
            if self.publication_ownership_verified is not None:
                result["publication_ownership_verified"] = (
                    self.publication_ownership_verified
                )
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
    client = FakeClient()
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

    # Recovering the PR identity must also perform the action the crash
    # interrupted: spawning the reviewer for the filed-but-unreviewed head
    # (chainlink #904 follow-up finding).
    assert client.spawn_reviewer_calls == [
        {
            "pr_number": 99,
            "head_sha": "head-a",
            "acceptance_criteria": client.spawn_reviewer_calls[0]["acceptance_criteria"],
            "force": False,
        }
    ]
    assert new_state.slices["slice-a"].reviewer_attempt == {"head-a": 1}
    assert store.load().slices["slice-a"].reviewer_attempt == {"head-a": 1}


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
    assert store.load().slices["slice-a"].handoff == recovered.handoff


def test_reconciliation_does_not_respawn_reviewer_when_head_already_claimed(
    tmp_path,
) -> None:
    store, state = _load_state(tmp_path)
    already_claimed = replace(
        state.slices["slice-a"], reviewer_attempt={"head-a": 1}
    )
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


def _claim_reviewer_attempt(store, state):
    already_claimed = replace(state.slices["slice-a"], reviewer_attempt={"head-a": 1})
    return store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": already_claimed},
        state.budgets,
        state.events.last_consumed_offset,
    )


def test_reconciliation_respawns_reviewer_when_claim_was_checkpointed_but_spawn_never_journaled(
    tmp_path,
) -> None:
    """The exact crash shape: the reviewer_attempt claim was durably checkpointed
    but the process died before spawn_reviewer was ever dispatched. An empty
    action journal has no confirmed entry for it, so reconciliation must
    distinguish this from a completed spawn and retry -- retrying is
    journal-safe because _invoke would replay a confirmed entry instead of
    double-spawning."""
    store, state = _load_state(tmp_path)
    state = _claim_reviewer_attempt(store, state)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)
    journal = EffectJournal("reconcile", tmp_path / "action-journal.json")

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    assert client.spawn_reviewer_calls == [
        {
            "pr_number": 99,
            "head_sha": "head-a",
            "acceptance_criteria": client.spawn_reviewer_calls[0]["acceptance_criteria"],
            "force": False,
        }
    ]


def test_reconciliation_does_not_respawn_when_journal_confirms_the_spawn(tmp_path) -> None:
    """A claimed head whose spawn_reviewer effect is journal-confirmed must not
    be re-spawned -- the claim and the completed spawn are the same event."""
    store, state = _load_state(tmp_path)
    state = _claim_reviewer_attempt(store, state)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)
    journal = EffectJournal("reconcile", tmp_path / "action-journal.json")
    confirmed_intent = EffectIntent(
        operation="spawn_reviewer",
        target="slice-a",
        arguments={
            "pr_number": 99,
            "head_sha": "head-a",
            "acceptance_criteria": [],
            "force": False,
        },
        executed=True,
    )
    journal.append(confirmed_intent)
    journal.mark_result(
        confirmed_intent, ToolResult(raw={"success": True}, success=True, result={}, error=None)
    )

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    assert client.spawn_reviewer_calls == []


def test_reconciliation_keeps_assuming_claimed_means_spawned_without_a_journal(tmp_path) -> None:
    """Without a durable action journal (e.g. a plain-list effects_log, as in
    unit tests that don't wire one up), reconciliation has no evidence to
    distinguish a checkpointed claim from a completed spawn and must stay
    conservative: assume claimed means spawned rather than risk a duplicate."""
    store, state = _load_state(tmp_path)
    state = _claim_reviewer_attempt(store, state)
    client = FakeClient()
    config = TLLoopConfig(active=True, ledger_run_id="run-1", enable_reviewer_spawn=True)

    _reconcile_nonterminal_slices(_PLAN, state, config, client, store, [])

    assert client.spawn_reviewer_calls == []


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
