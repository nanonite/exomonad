"""Ownership normalization and quarantine contract tests."""

from __future__ import annotations

from types import SimpleNamespace

from tl_loop.events.envelope import project
from tl_loop.events.identity import envelope_document, resolve_event_slice
from tl_loop.state.store import RunStore, create


def _event(
    event_type: str,
    *,
    agent_id: str,
    pr_number: int | None = 42,
    branch: str | None = "main.tunable-operator-body-opencode",
) -> object:
    data: dict[str, object] = {}
    if pr_number is not None:
        data["pr_number"] = pr_number
    if branch is not None:
        data["branch"] = branch
    return project(
        {
            "type": event_type,
            "run_seq": 7,
            "run_id": "swarm-uuid",
            "agent_id": agent_id,
            "lifecycle_state": "observed",
            "observed_at": "2026-08-18T00:00:00Z",
            "data": data,
        }
    )


def _state(*, duplicate_pr: bool = False) -> SimpleNamespace:
    first = SimpleNamespace(
        pr_number=42,
        dispatch_intent_id="intent-42",
        dispatch_agent_id="tunable-operator-body-opencode",
        branch="main.tunable-operator-body-opencode",
    )
    slices = {"tunable-operator-body": first}
    if duplicate_pr:
        slices["other"] = SimpleNamespace(
            pr_number=42,
            dispatch_intent_id="intent-other",
            dispatch_agent_id="other-agent",
            branch="main.other-agent",
        )
    return SimpleNamespace(slices=slices)


def test_branch_and_dispatch_agent_aliases_resolve_same_slice() -> None:
    event = _event(
        "ci.status_changed",
        agent_id="tunable-operator-body-opencode",
    )

    result = resolve_event_slice(event, _state())

    assert result.resolved
    assert result.slice_id == "tunable-operator-body"
    assert result.reason == "resolved"


def test_ambiguous_pr_is_rejected_without_first_match() -> None:
    event = _event("ci.status_changed", agent_id="unknown-owner", branch=None)

    result = resolve_event_slice(event, _state(duplicate_pr=True))

    assert not result.resolved
    assert result.slice_id is None
    assert result.reason == "ambiguous"
    assert result.candidates == ("other", "tunable-operator-body")


def test_quarantine_round_trip_preserves_observation_for_replay(tmp_path) -> None:
    create("root", {}, root_dir=tmp_path)
    store = RunStore("root", tmp_path)
    event = _event("ci.status_changed", agent_id="unknown-owner", branch=None)

    store.quarantine_event(envelope_document(event))

    entries = store.quarantined_events()
    assert len(entries) == 1
    assert entries[0]["run_seq"] == 7
    assert entries[0]["data"]["pr_number"] == 42

    store.release_quarantined_event(7)

    assert store.quarantined_events() == ()
