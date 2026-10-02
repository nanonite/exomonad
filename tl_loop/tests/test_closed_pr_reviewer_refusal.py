"""Regression coverage for #1141: a closed PR does not fail the run.

The child leg's last item drives a real escalation by closing the leaf's pull
request on the forge. Closing it while that leaf's reviewer was being dispatched
made the server refuse the spawn -- ``ensure_open_unmerged_pr`` in
``rust/exomonad-core/src/handlers/agent.rs`` answers a closed or merged PR with
"PR #N is not open and unmerged; use replace_close_pr for a closed PR" -- and
``_execute_direct_reviewer_intent`` re-raised that refusal as a run failure.

A closed, unmerged PR is an authoritative observation the product already knows
how to classify: ``pr_terminal_cause`` in ``tl_loop/loop/heartbeat.py`` returns
``ParkCause.PR_CLOSED_UNMERGED`` for it, and the heartbeat parks the slice and
escalates exactly once. The refusal only needs to stop killing the run so that
path can run. In a nested child the failure was worse than useless: the child
exited ``tl_failed``, the parent recorded "recursive child failed", and both the
park and its single escalation were lost.

The refusal is still journaled as ``REJECTED``, so the attempt stays auditable,
and every other spawn refusal still fails the run unchanged.
"""

from __future__ import annotations

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import _rejected_because_pr_closed

#: The message the shipped server sends for a closed or merged PR.
SERVER_REFUSAL = (
    "spawn_reviewer for 'out': Invalid input: PR #2 is not open and unmerged; "
    "use replace_close_pr for a closed PR"
)


def test_a_closed_pr_refusal_is_recognized_as_an_observation() -> None:
    """The exact refusal the server sends is the one case that is recognized."""
    assert _rejected_because_pr_closed(Exception(SERVER_REFUSAL)) is True


def test_a_closed_pr_refusal_is_recognized_in_any_casing() -> None:
    """Recognition does not depend on how the transport cased the message."""
    assert _rejected_because_pr_closed(Exception(SERVER_REFUSAL.upper())) is True


def test_an_unrelated_refusal_still_fails_the_run() -> None:
    """Only the closed-unmerged refusal is an observation to wait for.

    Every other reviewer refusal must keep raising, or a genuine spawn failure
    would be silently converted into a run that waits forever.
    """
    assert _rejected_because_pr_closed(Exception("spawn_reviewer for 'out': HTTP 500")) is False
    assert _rejected_because_pr_closed(Exception("spawn_reviewer for 'out': no such agent")) is False
    assert _rejected_because_pr_closed(Exception("review contract changed for 'out'")) is False


def test_a_phrase_without_a_pull_request_is_not_recognized() -> None:
    """The marker must name a PR, so unrelated prose cannot match it."""
    assert _rejected_because_pr_closed(Exception("tool is not open and unmerged")) is False


def test_an_unrelated_tool_result_is_not_recognized() -> None:
    """The classifier reads the failure text, not a result envelope.

    A successful result carries no error, so a spawn that went through can never
    be mistaken for the closed-PR refusal.
    """
    result = ToolResult.from_raw({"success": True, "result": {"reviewer_name": "r"}})
    assert result.error is None
    assert _rejected_because_pr_closed(Exception(result.error or "")) is False
