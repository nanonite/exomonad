"""Crash-window and stable-key coverage for lifecycle effect journaling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import EffectFailed, TLLoopError, _invoke
from tl_loop.loop.journal import EffectJournal, stable_action_key


def _intent(operation: str = "merge_pr") -> SimpleNamespace:
    return SimpleNamespace(
        operation=operation,
        target="slice-a",
        arguments={"head_sha": "head-a", "pr_number": 42},
        active=True,
    )


def test_stable_action_key_is_order_independent() -> None:
    first = stable_action_key("run-a", "merge_pr", "slice-a", {"a": 1, "b": 2})
    second = stable_action_key("run-a", "merge_pr", "slice-a", {"b": 2, "a": 1})

    assert first == second


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
