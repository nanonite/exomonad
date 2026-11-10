"""Fail-closed decoding of synchronization effect results."""

from __future__ import annotations

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import _sync_decision_payload


def test_failed_sync_without_a_diagnostic_stays_unresolved() -> None:
    result = ToolResult(raw={}, success=False, result=None, error=None)

    payload = _sync_decision_payload(result, "sync-1", 2)

    assert payload is not None
    assert payload["sync_outcome"] == "unresolved"
    assert payload["diagnostic"] == "synchronization failed without a diagnostic"
