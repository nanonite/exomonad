"""Replay acceptance for the #1117 recreated-publication correlation fixture.

The committed fixture replays the Beast-shaped recreate: a historical pr.filed
#44 at ledger seq 39829 filed under the predecessor invocation, then the current
pr.filed #45 at seq 42329 filed under the recreated invocation, into one
recreated controller generation (a nested run with a fresh dispatch epoch).

The replay proves #44 stays permanent audit evidence and never mutates state,
while #45 binds only when every identity field matches the recreated dispatch.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tl_loop.state.store import RunStore
from tl_loop.tests.replay import (
    FIXTURE_ROOT,
    expected_actions,
    expected_state,
    replay_fixture,
)

FIXTURE = FIXTURE_ROOT / "recreated-publication.json"
RUN_ID = "replay-recreated"
HISTORICAL_SEQ = 39829
CURRENT_SEQ = 42329


def _normalize(document: object, root: Path) -> object:
    """Normalize tmp-scoped worktree evidence for byte-stable comparison."""
    if isinstance(document, dict):
        return {key: _normalize(value, root) for key, value in document.items()}
    if isinstance(document, list):
        return [_normalize(value, root) for value in document]
    if isinstance(document, str) and document.startswith(str(root)):
        return "<state-root>" + document[len(str(root)) :]
    return document


def _replay(root: Path):
    return replay_fixture(FIXTURE, root, production_clock=True)


def _publication(state: dict) -> dict:
    return state["slices"]["leaf-a"]["publication"]


def test_historical_publication_is_permanent_audit(tmp_path: Path) -> None:
    """#44 is refused before reduction and retained permanently for audit."""
    _replay(tmp_path)
    store = RunStore(RUN_ID, tmp_path)
    audited = store.audited_events()
    historical = [entry for entry in audited if entry.get("run_seq") == HISTORICAL_SEQ]
    assert len(historical) == 1
    assert historical[0].get("correlation") == "publication_history_audit"
    # Permanent audit, never pending replay work.
    assert store.quarantined_events() == ()
    # A restart reloads the audit row but never replays or releases it.
    reloaded = RunStore(RUN_ID, tmp_path)
    assert reloaded.quarantined_events() == ()
    assert any(entry.get("run_seq") == HISTORICAL_SEQ for entry in reloaded.audited_events())


def test_historical_publication_never_mutates_slice_state(tmp_path: Path) -> None:
    """The refused #44 left no publication, PR number, or head evidence."""
    result = _replay(tmp_path / "first")
    leaf = result.state["slices"]["leaf-a"]
    # The bound publication is #45, never the historical #44.
    assert _publication(result.state)["pr_number"] == 45
    assert _publication(result.state)["head_sha"] == "head-45"
    assert leaf["pr_number"] == 45
    assert leaf["reviewed_head"] == "head-45"
    assert leaf["status"] == "in_review"


def test_current_publication_binds_with_exact_identity(tmp_path: Path) -> None:
    """#45 binds only with the exact recreated dispatch and publication identity."""
    result = _replay(tmp_path / "first")
    publication = _publication(result.state)
    assert publication == {
        "pr_number": 45,
        "head_sha": "head-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "attempt": 1,
        "invocation_id": "inv-current",
    }
    leaf = result.state["slices"]["leaf-a"]
    assert leaf["dispatch_invocation_id"] == "inv-current"
    assert leaf["dispatch_agent_id"] == "leaf-a"
    assert leaf["handoff"]["head_sha"] == "head-45"
    assert leaf["handoff"]["invocation_id"] == "inv-current"
    assert result.state["fsm"]["phase"] == "tl_done"


def test_recorded_stream_replays_exact_actions_and_state(tmp_path: Path) -> None:
    first = _replay(tmp_path / "first")
    second = _replay(tmp_path / "second")
    assert first.actions == expected_actions(FIXTURE)
    assert _canonical(_normalize(first.state, tmp_path / "first")) == _canonical(
        expected_state(FIXTURE)
    )
    # A restart reproduces the same durable position.
    assert _canonical(_normalize(first.state, tmp_path / "first")) == _canonical(
        _normalize(second.state, tmp_path / "second")
    )


def _mutated_publication_event(spec: dict, field: str) -> dict:
    """Copy the #45 event with one identity field mutated."""
    event = copy.deepcopy(
        next(item for item in spec["events"] if item.get("run_seq") == CURRENT_SEQ)
    )
    event["run_seq"] = CURRENT_SEQ + 2
    event["event_id"] = f"negative-{field}"
    event["id"] = f"negative-{field}"
    if field == "invocation":
        event["invocation_id"] = "inv-wrong"
    elif field == "owner":
        event["agent_id"] = "someone-else"
    elif field == "pr_number":
        event["data"]["pr_number"] = 46
    elif field == "head_sha":
        event["data"]["head_sha"] = "head-46"
    elif field == "head_branch":
        event["data"]["head_branch"] = "main.other"
    elif field == "base_branch":
        event["data"]["base_branch"] = "develop"
    elif field == "slice":
        event["data"]["slice_id"] = "leaf-b"
    elif field == "generation":
        event["generation"] = 1
    elif field == "epoch":
        event["data"]["controller_epoch"] = "epoch-wrong"
    else:
        raise ValueError(f"unknown publication identity field {field!r}")
    return event


_SINGLE_FIELD_MISMATCHES = (
    "invocation",
    "owner",
    "pr_number",
    "head_sha",
    "head_branch",
    "base_branch",
    "slice",
    "generation",
    "epoch",
)


@pytest.mark.parametrize("field", _SINGLE_FIELD_MISMATCHES)
def test_single_field_mismatch_refuses_binding(tmp_path: Path, field: str) -> None:
    """A second publication event with one mutated field must refuse binding.

    The first #45 binds with the exact identity; a follow-up publication event
    that deviates in exactly one field must refuse and leave the bound #45
    publication untouched.
    """
    spec = json.loads(FIXTURE.read_text())
    spec["events"].append(_mutated_publication_event(spec, field))
    root = tmp_path / field
    variant = root / "variant.json"
    variant.parent.mkdir(parents=True, exist_ok=True)
    variant.write_text(json.dumps(spec), encoding="utf-8")
    result = replay_fixture(variant, root, production_clock=True)
    leaf = result.state["slices"]["leaf-a"]
    # The bound #45 publication survives the refused mismatch.
    assert _publication(result.state)["pr_number"] == 45
    assert _publication(result.state)["head_sha"] == "head-45"
    assert _publication(result.state)["invocation_id"] == "inv-current"
    assert leaf["pr_number"] == 45
    assert leaf["reviewed_head"] == "head-45"
    # The refused event never became silently bound. A wrong slice identity is
    # retained for reconciliation (fail-closed quarantine); every other
    # single-field mismatch is refused without pending replay work.
    store = RunStore(RUN_ID, root)
    if field == "slice":
        assert [entry.get("run_seq") for entry in store.quarantined_events()] == [
            CURRENT_SEQ + 2
        ]
    else:
        assert store.quarantined_events() == ()
    # The historical #44 audit row is joined only by refusals classified as
    # generation evidence (invocation, generation, epoch).
    audited = [entry.get("run_seq") for entry in store.audited_events()]
    assert HISTORICAL_SEQ in audited
    if field in {"invocation", "generation", "epoch"}:
        assert CURRENT_SEQ + 2 in audited
    else:
        assert CURRENT_SEQ + 2 not in audited


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
