"""Regression coverage for #1061: direct-leaf-only plans reaching TLDone.

test_legacy_manifest_convergence.py documents (and deliberately works around)
a gap in run_tl_loop's outer event loop: a plan built only from direct
``leaves`` (no ``sub_tls``) never returns ``TLDone`` from a single
``run_tl_loop()`` call once its last leaf's post-merge recovery (parent sync,
Chainlink issue close, changelog commit, parent push) finishes and there are
no further incoming ledger events to nudge it. ``_run_sub_tls`` drains that
same recovery to completion within one call, but it is gated on
``plan.sub_tls`` and is untouched here.

This module reuses that file's sanitized "captured Beast" fixtures (a single
already-merged leaf with durable action-journal evidence, reconstructed
through the same dataclasses and journal API production code uses -- not the
real captured checkpoint, which #1060 forbids editing) and drives them
through the real outer ``run_tl_loop()`` loop with an empty event source,
instead of calling the inner reconciliation/convergence pipeline directly.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.fsm.scope import TLDone as RecursiveTLDone
from tl_loop.loop.driver import LoopCancelled, TLLoopConfig, run_tl_loop
from tl_loop.state.schema import SliceStatus
from tl_loop.tests.test_driver import SyntheticQueue
from tl_loop.tests.test_legacy_manifest_convergence import (
    CHAINLINK_ISSUE_ID,
    RUN_ID,
    SLICE_ID,
    _config,
    _merged_watcher_transport,
    _seed_and_migrate,
)


def _run_bounded(config: TLLoopConfig, effects: EffectClient, tmp_path: Path):
    """Run run_tl_loop with a hard wall-clock cancellation safety net.

    A regression of #1061 is exactly "blocks/spins forever waiting on ledger
    events" -- if the fix regresses, this must fail fast with LoopCancelled
    instead of hanging the suite.
    """
    cancel_event = threading.Event()
    timer = threading.Timer(10.0, cancel_event.set)
    timer.start()
    try:
        return run_tl_loop(
            RUN_ID,
            None,
            SyntheticQueue([]),
            effects,
            config=replace(
                config,
                cancel_event=cancel_event,
                keep_alive_on_waiting=False,
                poll_interval=0.01,
            ),
            root_dir=tmp_path,
        )
    finally:
        timer.cancel()


def test_direct_leaf_plan_reaches_tl_done_in_one_run_tl_loop_call(tmp_path: Path) -> None:
    _seed_and_migrate(tmp_path)
    config = _config()
    transport = _merged_watcher_transport()
    effects = EffectClient(transport)

    try:
        result = _run_bounded(config, effects, tmp_path)
    except LoopCancelled:
        raise AssertionError(
            "run_tl_loop did not converge to TLDone and was cancelled by the "
            "test's wall-clock safety net -- #1061 regressed"
        ) from None

    final = result.final_state
    assert isinstance(final.recursive_fsm, RecursiveTLDone)
    assert final.slices[SLICE_ID].status is SliceStatus.MERGED
    assert final.slices[SLICE_ID].post_merge is not None
    assert final.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE

    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    chainlink_close_calls = [
        arguments for name, arguments in transport.calls if name == "chainlink_issue_close"
    ]
    assert len(chainlink_close_calls) == 1
    assert chainlink_close_calls[0]["issue_id"] == CHAINLINK_ISSUE_ID
    assert [name for name, _ in transport.calls if name == "post_merge_changelog"] == [
        "post_merge_changelog"
    ]
    assert [name for name, _ in transport.calls if name == "post_merge_push"] == ["post_merge_push"]


def test_direct_leaf_plan_repeated_continuation_after_tl_done_is_a_no_op(tmp_path: Path) -> None:
    _seed_and_migrate(tmp_path)
    config = _config()
    transport = _merged_watcher_transport()
    effects = EffectClient(transport)

    first = _run_bounded(config, effects, tmp_path)
    assert isinstance(first.final_state.recursive_fsm, RecursiveTLDone)
    calls_after_first = list(transport.calls)

    second = _run_bounded(config, effects, tmp_path)

    assert isinstance(second.final_state.recursive_fsm, RecursiveTLDone)
    assert transport.calls == calls_after_first
