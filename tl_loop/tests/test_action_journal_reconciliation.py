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
    _reconcile_action_journal,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import GateStatus
from tl_loop.state.store import RunStore, create


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
