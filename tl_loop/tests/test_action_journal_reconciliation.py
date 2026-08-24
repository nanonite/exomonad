"""Startup reconciliation for stuck action-journal entries (chainlink #905).

Covers the verification finding that intended/unknown journal entries used
to raise permanently with no path back to a working controller: every
restart hit the exact same TLLoopError forever. _reconcile_action_journal
now converts that into the project's normal named-gate pattern instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tl_loop.loop.driver import (
    _action_journal_gate_name,
    _record_controller_event,
    _reconcile_action_journal,
    TLLoopConfig,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.client.effects import ToolResult
from tl_loop.state.schema import GateStatus, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_reconcile import _slice


@dataclass
class _Intent:
    operation: str = "merge_pr"
    target: str = "slice-a"
    arguments: dict[str, Any] = field(
        default_factory=lambda: {"head_sha": "head-a", "pr_number": 42}
    )
    active: bool = True


def _journal_with_unknown_entry(tmp_path) -> tuple[EffectJournal, _Intent, str]:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("reconcile-journal", path)
    intent = _Intent()
    journal.append(intent)
    journal.mark_unknown(intent, RuntimeError("connection lost"))
    return journal, intent, journal.key_for(intent)


def _store_and_state(tmp_path):
    create("reconcile-journal", {"slices": {}}, root_dir=tmp_path)
    store = RunStore("reconcile-journal", tmp_path)
    return store, store.load()


def test_first_reconciliation_opens_a_named_gate_and_leaves_entry_blocked(tmp_path) -> None:
    journal, intent, key = _journal_with_unknown_entry(tmp_path)
    store, state = _store_and_state(tmp_path)

    new_state = _reconcile_action_journal(state, store, journal)

    gate_name = _action_journal_gate_name(key)
    gate = next((g for g in new_state.gates if g.name == gate_name), None)
    assert gate is not None
    assert gate.status is GateStatus.PENDING
    # The entry itself is untouched until the operator answers the gate.
    assert journal.existing(intent)["status"] == "unknown"


def test_approved_gate_compensates_the_entry_and_clears_the_block(tmp_path) -> None:
    journal, intent, key = _journal_with_unknown_entry(tmp_path)
    store, state = _store_and_state(tmp_path)
    gate_name = _action_journal_gate_name(key)
    state = store.set_gate(gate_name, GateStatus.APPROVED)

    _reconcile_action_journal(state, store, journal)

    assert journal.pending_entries() == []
    assert journal.existing(intent)["status"] == "compensated"


def test_rejected_gate_records_a_durable_rejected_outcome(tmp_path) -> None:
    journal, intent, key = _journal_with_unknown_entry(tmp_path)
    store, state = _store_and_state(tmp_path)
    gate_name = _action_journal_gate_name(key)
    state = store.set_gate(gate_name, GateStatus.REJECTED)

    _reconcile_action_journal(state, store, journal)

    assert journal.pending_entries() == []
    resolved = journal.existing(intent)
    assert resolved["status"] == "rejected"
    assert resolved["result"]["success"] is False


def test_reconciliation_is_a_noop_without_an_action_journal(tmp_path) -> None:
    store, state = _store_and_state(tmp_path)
    plain_effects_log: list[object] = []

    new_state = _reconcile_action_journal(state, store, plain_effects_log)

    assert new_state.gates == state.gates
    assert plain_effects_log == []


def test_controller_event_journal_records_confirmed_and_unknown_outcomes(tmp_path) -> None:
    class Client:
        def __init__(self, result: ToolResult | None = None) -> None:
            self.result = result

        def emit_controller_event(
            self, *, event_type: str, payload: dict[str, object]
        ) -> ToolResult:
            del event_type, payload
            if self.result is not None:
                return self.result
            raise RuntimeError("connection lost")

    journal = EffectJournal("controller-events", tmp_path / "action-journal.json")
    config = TLLoopConfig(active=True)
    payload = {"slice_id": "slice-a", "to_status": "spawned"}
    _record_controller_event(
        "slice-a",
        "tl.slice_status_changed",
        payload,
        config,
        Client(ToolResult.from_raw({"success": True, "result": {}})),
        journal,
    )
    confirmed = journal._read()[0]
    assert confirmed["status"] == "confirmed"

    _record_controller_event(
        "slice-a",
        "tl.slice_status_changed",
        {"slice_id": "slice-a", "to_status": "merged"},
        config,
        Client(),
        journal,
    )
    unknown = journal._read()[-1]
    assert unknown["status"] == "unknown"


def test_a_second_unknown_outcome_for_the_same_key_does_not_reuse_the_earlier_approval(
    tmp_path,
) -> None:
    """chainlink #905 follow-up: one approval must not silently authorize a
    retried effect (spawn/merge/PR/cleanup) to run again with no further
    human check. The retried dispatch itself lands in another ambiguous
    ("unknown") outcome here, exactly as an interrupted merge_pr retry
    would; the operator must be asked again before it is compensated a
    second time.
    """
    journal, intent, key = _journal_with_unknown_entry(tmp_path)
    store, state = _store_and_state(tmp_path)
    first_gate_name = _action_journal_gate_name(key)
    state = store.set_gate(first_gate_name, GateStatus.APPROVED)

    state = _reconcile_action_journal(state, store, journal)

    assert journal.existing(intent)["status"] == "compensated"
    assert journal.existing(intent)["compensation_attempt"] == 1

    # The retried dispatch re-appends the same key (a fresh "intended"
    # attempt), then itself becomes ambiguous again.
    journal.append(intent)
    journal.mark_unknown(intent, RuntimeError("connection lost again"))

    state = _reconcile_action_journal(state, store, journal)

    second_gate_name = _action_journal_gate_name(key, attempt=1)
    assert second_gate_name != first_gate_name
    second_gate = next((g for g in state.gates if g.name == second_gate_name), None)
    assert second_gate is not None
    assert second_gate.status is GateStatus.PENDING

    # The first, already-spent approval remains as a durable audit record
    # but must not have resolved the new occurrence.
    first_gate = next((g for g in state.gates if g.name == first_gate_name), None)
    assert first_gate.status is GateStatus.APPROVED
    assert journal.existing(intent)["status"] == "unknown"


def test_invoke_error_points_to_the_attempt_scoped_gate(tmp_path) -> None:
    import pytest

    from tl_loop.loop.driver import TLLoopError, _invoke

    journal, intent, key = _journal_with_unknown_entry(tmp_path)
    journal.resolve_by_key(key, compensation_attempt=1)

    def call(_client: object) -> ToolResult:
        raise AssertionError("must not dispatch while the entry is still blocked")

    with pytest.raises(TLLoopError, match=_action_journal_gate_name(key, attempt=1)):
        _invoke(
            intent.operation,
            intent.target,
            intent.arguments,
            intent.active,
            None,
            call,  # type: ignore[arg-type]
            journal,
        )


def test_restart_adopts_an_authoritative_merge_for_a_pending_intent(tmp_path) -> None:
    journal, intent, _ = _journal_with_unknown_entry(tmp_path)
    store, state = _store_and_state(tmp_path)
    state = store.checkpoint(
        state.fsm,
        {"slice-a": _slice(SliceStatus.IN_REVIEW)},
        state.budgets,
        state.events.last_consumed_offset,
    )

    class Watcher:
        def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
            assert pr_number == 42
            return ToolResult.from_raw(
                {"success": True, "result": {"merged": True, "pr_state": "closed"}}
            )

    reconciled = _reconcile_action_journal(
        state,
        store,
        journal,
        effects=Watcher(),
    )

    assert journal.pending_entries() == []
    assert journal.existing(intent)["status"] == "confirmed"
    assert reconciled.slices["slice-a"].status is SliceStatus.MERGED
    assert reconciled.slices["slice-a"].action is None
