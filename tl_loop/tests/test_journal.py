"""Crash-window and stable-key coverage for lifecycle effect journaling."""

from __future__ import annotations

import json
from types import MappingProxyType
from types import SimpleNamespace

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.fsm.recovery import (
    RecoveryIdentity,
    RecoveryIntent,
    assert_recovery_identity,
)
from tl_loop.loop.driver import EffectFailed, TLLoopError, _invoke
from tl_loop.loop.journal import EffectJournal, stable_action_key


def _intent(operation: str = "merge_pr") -> SimpleNamespace:
    return SimpleNamespace(
        operation=operation,
        target="slice-a",
        arguments={"head_sha": "head-a", "pr_number": 42},
        active=True,
    )


def _recovery_intent(action: str = "resume_same_owner") -> RecoveryIntent:
    return RecoveryIntent(
        intent_id="recovery-intent-1",
        recovery_identity=RecoveryIdentity(
            run_id="run-a",
            slice_id="slice-a",
            owner_agent_id="agent-a",
            invocation_id="inv-1",
            invocation_generation=3,
            recovery_round=1,
            branch="task/slice-a",
            worktree=".worktrees/slice-a",
        ),
        action=action,
        expected_worktree_fingerprint="sha256:worktree-1",
    )


def test_stable_action_key_is_order_independent() -> None:
    first = stable_action_key("run-a", "merge_pr", "slice-a", {"a": 1, "b": 2})
    second = stable_action_key("run-a", "merge_pr", "slice-a", {"b": 2, "a": 1})

    assert first == second


def test_nested_mappingproxy_arguments_are_durable(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    intent = SimpleNamespace(
        operation="spawn_worker",
        target="slice-a",
        arguments={
            "payload": MappingProxyType({"nested": MappingProxyType({"value": "preserved"})})
        },
    )

    EffectJournal("run-a", path).append(intent)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document[0]["arguments"]["payload"]["nested"]["value"] == "preserved"


def test_confirmed_action_replays_after_journal_reload(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    intent = _intent()
    journal = EffectJournal("run-a", path)
    journal.append(intent)
    result = ToolResult.from_raw({"success": True, "result": {"merge_id": "m-1"}})
    journal.mark_result(intent, result)

    reloaded = EffectJournal("run-a", path)
    entry = reloaded.existing(intent)
    assert entry is not None
    assert entry["status"] == "confirmed"
    assert reloaded.replay(entry).result == {"merge_id": "m-1"}


def test_resume_action_is_journaled_and_replayed_after_reload(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    intent = _intent(operation="resume_pr")
    journal = EffectJournal("run-a", path)
    journal.append(intent)
    result = ToolResult.from_raw(
        {
            "success": True,
            "result": {"invocation": {"invocation_id": "resume-1", "fresh": True}},
        }
    )
    journal.mark_result(intent, result)

    reloaded = EffectJournal("run-a", path)
    entry = reloaded.existing(intent)
    assert entry is not None
    assert entry["operation"] == "resume_pr"
    assert reloaded.replay(entry).result == {
        "invocation": {"invocation_id": "resume-1", "fresh": True}
    }


def test_unknown_action_cannot_be_retried_blindly(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _intent()
    journal.append(intent)
    journal.mark_unknown(intent, RuntimeError("connection lost"))

    with pytest.raises(TLLoopError, match="requires reconciliation"):
        _invoke(
            intent.operation,
            intent.target,
            intent.arguments,
            True,
            SimpleNamespace(),
            lambda _: ToolResult.from_raw({"success": True}),
            journal,
        )


def test_pending_entries_lists_only_intended_and_unknown(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)

    intended = _intent(operation="file_pr")
    journal.append(intended)

    unknown = _intent(operation="merge_pr")
    journal.append(unknown)
    journal.mark_unknown(unknown, RuntimeError("connection lost"))

    confirmed = _intent(operation="cleanup_leaf")
    journal.append(confirmed)
    journal.mark_result(confirmed, ToolResult.from_raw({"success": True}))

    pending_keys = {entry["key"] for entry in journal.pending_entries()}
    assert pending_keys == {journal.key_for(intended), journal.key_for(unknown)}


def test_resolve_by_key_compensated_clears_the_block_for_retry(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _intent()
    journal.append(intent)
    journal.mark_unknown(intent, RuntimeError("connection lost"))

    journal.resolve_by_key(journal.key_for(intent), status="compensated")

    dispatched = []
    result = _invoke(
        intent.operation,
        intent.target,
        intent.arguments,
        True,
        SimpleNamespace(),
        lambda _: dispatched.append(1) or ToolResult.from_raw({"success": True}),
        journal,
    )
    assert dispatched == [1]
    assert result is not None and result.success is True


def test_resolve_by_key_rejected_replays_as_a_clean_failure(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _intent()
    journal.append(intent)
    journal.mark_unknown(intent, RuntimeError("connection lost"))

    journal.resolve_by_key(
        journal.key_for(intent),
        status="rejected",
        result={"success": False, "error": "operator rejected retry"},
    )

    with pytest.raises(EffectFailed, match="operator rejected retry"):
        _invoke(
            intent.operation,
            intent.target,
            intent.arguments,
            True,
            SimpleNamespace(),
            lambda _: pytest.fail("rejected action must not be dispatched again"),
            journal,
        )


def test_replayed_rejection_preserves_failure_semantics(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _intent()
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult.from_raw({"success": False, "error": "CAS mismatch"}),
    )

    with pytest.raises(EffectFailed, match="CAS mismatch"):
        _invoke(
            intent.operation,
            intent.target,
            intent.arguments,
            True,
            SimpleNamespace(),
            lambda _: pytest.fail("confirmed rejection must not be dispatched again"),
            journal,
        )


def test_recovery_intent_is_durable_and_idempotent(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _recovery_intent()

    journal.append_recovery_intent(intent)
    journal.append_recovery_intent(intent)

    assert len(journal.pending_recovery_entries()) == 1
    entry = journal.pending_recovery_entries()[0]
    assert journal.load_recovery_intent(entry) == intent


def test_recovery_intent_unknown_outcome_is_reconciled_without_replay(tmp_path) -> None:
    path = tmp_path / "action-journal.json"
    journal = EffectJournal("run-a", path)
    intent = _recovery_intent("probe")
    journal.append(intent)
    journal.mark_recovery_intent(intent, "unknown", error="server restarted")

    assert len(journal.pending_recovery_entries()) == 1
    journal.mark_recovery_intent(intent, "reconciled", result={"healthy": False})

    assert journal.pending_recovery_entries() == []
    restored = journal.existing_recovery(intent)
    assert restored is not None
    assert restored["state"] == "reconciled"
    assert restored["result"] == {"healthy": False}


def test_recovery_identity_compare_and_set_fails_closed() -> None:
    expected = _recovery_intent().recovery_identity
    observed = RecoveryIdentity(
        **{**expected.__dict__, "invocation_generation": expected.invocation_generation + 1}
    )

    with pytest.raises(ValueError, match="compare-and-set failed"):
        assert_recovery_identity(expected, observed)
