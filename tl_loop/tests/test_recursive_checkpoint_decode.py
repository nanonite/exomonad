"""Recursive checkpoint decoding keeps the typed post-merge invariant."""

from __future__ import annotations

import pytest

from tl_loop.state.store import _decode_recursive_fsm


def test_recursive_running_rejects_a_malformed_post_merge_state() -> None:
    payload = {
        "kind": "tl_running",
        "current_order": 0,
        "pending_by_order": {},
        "scope_path": ["root"],
        "plan_digest": "digest",
        "parallel_pending": [{"child_id": "child-a", "kind": "leaf", "evidence": {}}],
        "completed_children": {},
        "post_merge": {"child-a": "not-an-object"},
        "dispatch_intents": {},
        "evidence": {},
        "lane_bindings": {},
    }

    with pytest.raises(TypeError):
        _decode_recursive_fsm({"payload": payload})
