"""Golden parity checks against the Haskell TLPhase source of truth."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.fsm import (
    AllChildrenDone,
    ChildCompleted,
    ChildFailed,
    ChildHandle,
    ChildSpawned,
    IllegalTransition,
    OwnPRFiled,
    PhaseValue,
    PRMerged,
    TLAllMerged,
    TLDispatching,
    TLDone,
    TLEvent,
    TLFailed,
    TLMerging,
    TLPlanning,
    TLPRFiled,
    TLWaiting,
    transition,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "tl_phase_golden.json"
TL_PHASE_SOURCE = REPOSITORY_ROOT / ".exo" / "roles" / "devswarm" / "TLPhase.hs"


def test_haskell_golden_fixture_is_fresh_and_complete() -> None:
    fixture = _load_fixture()
    source_hash = fixture.get("source_blob_hash")
    rows = fixture.get("rows")
    assert source_hash == _git_blob_hash(TL_PHASE_SOURCE)
    assert isinstance(rows, list)
    assert len(rows) == 48


def test_staleness_guard_changes_for_a_tlphase_edit() -> None:
    fixture = _load_fixture()
    source_hash = fixture.get("source_blob_hash")
    assert isinstance(source_hash, str)
    edited_source = TL_PHASE_SOURCE.read_bytes() + b"\n-- edited\n"
    assert _git_blob_hash_bytes(edited_source) != source_hash


def test_python_fsm_matches_every_haskell_golden_row() -> None:
    fixture = _load_fixture()
    rows = fixture.get("rows")
    assert isinstance(rows, list)
    for row_value in rows:
        assert isinstance(row_value, dict)
        row = cast(JsonObject, row_value)
        phase_data = row.get("phase")
        event_data = row.get("event")
        expected = row.get("result")
        assert isinstance(phase_data, dict)
        assert isinstance(event_data, dict)
        assert expected is not None
        phase = _parse_phase(cast(JsonObject, phase_data))
        event = _parse_event(cast(JsonObject, event_data))
        actual = _apply_transition(phase, event)
        assert actual == expected, f"{phase_data!r} + {event_data!r}"


def _load_fixture() -> JsonObject:
    return cast(JsonObject, json.loads(FIXTURE.read_text(encoding="utf-8")))


def _git_blob_hash(path: Path) -> str:
    return _git_blob_hash_bytes(path.read_bytes())


def _git_blob_hash_bytes(contents: bytes) -> str:
    header = f"blob {len(contents)}\0".encode("ascii")
    return hashlib.sha1(header + contents).hexdigest()


def _parse_phase(data: JsonObject) -> PhaseValue:
    tag = data.get("phase")
    assert isinstance(tag, str)
    if tag == "tl_planning":
        return TLPlanning()
    if tag == "tl_dispatching":
        return TLDispatching()
    if tag == "tl_waiting":
        return TLWaiting(_parse_children(data))
    if tag == "tl_merging":
        pr_number = data.get("pr_number")
        assert isinstance(pr_number, int)
        return TLMerging(pr_number, _parse_children(data))
    if tag == "tl_all_merged":
        return TLAllMerged()
    if tag == "tl_pr_filed":
        pr_number = data.get("pr_number")
        url = data.get("url")
        assert isinstance(pr_number, int)
        assert isinstance(url, str)
        return TLPRFiled(pr_number, url)
    if tag == "tl_done":
        return TLDone()
    if tag == "tl_failed":
        message = data.get("message")
        assert isinstance(message, str)
        return TLFailed(message)
    raise AssertionError(f"Unknown phase in golden fixture: {tag!r}")


def _parse_children(data: JsonObject) -> dict[str, ChildHandle]:
    children = data.get("children")
    assert isinstance(children, dict)
    parsed: dict[str, ChildHandle] = {}
    for slug, value in children.items():
        assert isinstance(value, dict)
        parsed[slug] = _parse_handle(cast(JsonObject, value))
    return parsed


def _parse_handle(data: JsonObject) -> ChildHandle:
    slug = data.get("slug")
    branch = data.get("branch")
    agent_type = data.get("agent_type")
    assert isinstance(slug, str)
    assert isinstance(branch, str)
    assert isinstance(agent_type, str)
    return ChildHandle(slug, branch, agent_type)


def _parse_event(data: JsonObject) -> TLEvent:
    tag = data.get("event")
    assert isinstance(tag, str)
    if tag == "child_spawned":
        handle = data.get("handle")
        assert isinstance(handle, dict)
        return ChildSpawned(_parse_handle(cast(JsonObject, handle)))
    if tag == "child_completed":
        slug = data.get("slug")
        assert isinstance(slug, str)
        return ChildCompleted(slug)
    if tag == "child_failed":
        slug = data.get("slug")
        reason = data.get("reason")
        assert isinstance(slug, str)
        assert isinstance(reason, str)
        return ChildFailed(slug, reason)
    if tag == "pr_merged":
        pr_number = data.get("pr_number")
        slug = data.get("slug")
        assert isinstance(pr_number, int)
        assert isinstance(slug, str)
        return PRMerged(pr_number, slug)
    if tag == "all_children_done":
        return AllChildrenDone()
    if tag == "own_pr_filed":
        pr_number = data.get("pr_number")
        url = data.get("url")
        branch = data.get("branch")
        assert isinstance(pr_number, int)
        assert isinstance(url, str)
        assert isinstance(branch, str)
        return OwnPRFiled(pr_number, url, branch)
    raise AssertionError(f"Unknown event in golden fixture: {tag!r}")


def _apply_transition(phase: PhaseValue, event: TLEvent) -> JsonValue:
    try:
        result = transition(phase, event)
    except IllegalTransition:
        return "illegal"
    return _phase_json(result)

def _phase_json(phase: PhaseValue) -> JsonObject:
    if isinstance(phase, TLPlanning):
        return {"phase": "tl_planning"}
    if isinstance(phase, TLDispatching):
        return {"phase": "tl_dispatching"}
    if isinstance(phase, TLWaiting):
        return {"phase": "tl_waiting", "children": _children_json(phase.children)}
    if isinstance(phase, TLMerging):
        return {
            "phase": "tl_merging",
            "pr_number": phase.pr_number,
            "children": _children_json(phase.children),
        }
    if isinstance(phase, TLAllMerged):
        return {"phase": "tl_all_merged"}
    if isinstance(phase, TLPRFiled):
        return {"phase": "tl_pr_filed", "pr_number": phase.pr_number, "url": phase.url}
    if isinstance(phase, TLDone):
        return {"phase": "tl_done"}
    return {"phase": "tl_failed", "message": cast(TLFailed, phase).message}


def _children_json(children: Mapping[str, ChildHandle]) -> JsonObject:
    return {slug: _handle_json(handle) for slug, handle in children.items()}


def _handle_json(handle: ChildHandle) -> JsonObject:
    return {"slug": handle.slug, "branch": handle.branch, "agent_type": handle.agent_type}
