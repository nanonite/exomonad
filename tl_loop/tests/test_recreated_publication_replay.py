"""Replay acceptance for recreated publication correlation (#1112, #1117).

The committed fixture replays a synthetic recorded ledger shape. The
predecessor controller generation filed PR #101 at ledger seq 200. The recreated
generation re-confirmed the leaf dispatch, consumed the still-present #101 row
under its own owner, and filed PR #102 at seq 300 under the new invocation and
controller epoch.

The acceptance proves two properties of the production binder through the real
reducer:

* the historical #101 row is permanent audit evidence and never mutates state;
* #102 binds only with exact invocation, PR number, SHA, head branch, base
  branch, owner, slice, dispatch generation, and controller epoch identity, so
  every single-field mismatch refuses binding.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tl_loop.state.store import RunStore
from tl_loop.tests.replay import (
    FIXTURE_ROOT,
    normalize_durable_state,
    replay_fixture,
)

FIXTURE = FIXTURE_ROOT / "recreated-publication-correlation.json"
CHILD_SCOPE = "recreate-stage"
LEAF = "recreated-leaf"
DISPATCH_CONFIRMATION = "synthetic-dispatch-confirmed-100"
HISTORICAL_PUBLICATION = "synthetic-pr-101-filed-200"
CURRENT_PUBLICATION = "synthetic-pr-102-filed-300"
COMPLETION_ROWS = ("synthetic-child-completed-330", "synthetic-all-children-done-340")
BASELINE_ROWS = (
    DISPATCH_CONFIRMATION,
    HISTORICAL_PUBLICATION,
    CURRENT_PUBLICATION,
    *COMPLETION_ROWS,
)
CONTROL_ROWS = (DISPATCH_CONFIRMATION, *COMPLETION_ROWS)
WITHOUT_HISTORICAL_ROWS = (DISPATCH_CONFIRMATION, CURRENT_PUBLICATION, *COMPLETION_ROWS)

RUN_ID = "replay-recreate"
LEAF_AGENT_ID = "recreated-leaf-opencode"
RECREATED_INVOCATION = "inv-recreated-1"
RECREATED_EPOCH = "fd510ebdd318dec7c78d56df90ea9627"
PREDECESSOR_EPOCH = "1111aaaa2222bbbb3333cccc4444dddd"
HEAD_BRANCH = "main.recreate-stage.recreated-leaf"
BASE_BRANCH = "main"
HEAD_101 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
HEAD_102 = "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5"

HISTORICAL_REFUSAL = (
    "publication invocation does not match the current dispatch invocation or a "
    "recorded recovery succession"
)
HISTORICAL_RUN_SEQ = 200
CURRENT_RUN_SEQ = 300


def _spec() -> dict[str, Any]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _rows(names: Sequence[str]) -> Any:
    """Deliver only the named committed ledger rows to the child scope."""

    def transform(rows: list[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
        return [row for row in rows if row.get("event_id") in names]

    return transform


def _patched(row_name: str, fields: Mapping[str, Any]) -> Any:
    """Deliver the baseline rows with one single-field change to one row."""

    def transform(rows: list[Mapping[str, object]]) -> Sequence[Mapping[str, object]]:
        selected: list[Mapping[str, object]] = []
        for row in rows:
            if row.get("event_id") not in BASELINE_ROWS:
                continue
            if row.get("event_id") != row_name:
                selected.append(row)
                continue
            patched = copy.deepcopy(dict(row))
            for key, value in fields.items():
                if isinstance(value, Mapping):
                    nested = dict(patched.get(key, {}))
                    nested.update(value)
                    patched[key] = nested
                else:
                    patched[key] = value
            selected.append(patched)
        return selected

    return transform


def _replay(root: Path, transform: Any) -> None:
    replay_fixture(
        FIXTURE,
        root,
        journal=True,
        production_clock=True,
        child_event_transform=transform,
    )


def _child_document(root: Path) -> dict[str, Any]:
    path = root / RUN_ID / CHILD_SCOPE / "run.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _child_store(root: Path) -> RunStore:
    return RunStore(CHILD_SCOPE, root / RUN_ID)


def _mask_paths(value: object, root: Path) -> object:
    if isinstance(value, dict):
        return {key: _mask_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_paths(item, root) for item in value]
    if isinstance(value, str):
        prefix = str(root)
        if value.startswith(prefix):
            return "<replay-root>" + value[len(prefix) :]
    return value


def _leaf_state(root: Path) -> dict[str, Any]:
    """Normalized durable state of the published leaf, without volatile roots."""
    durable = normalize_durable_state(_child_document(root))
    slices = durable["slices"]
    assert isinstance(slices, dict)
    state = slices[LEAF]
    assert isinstance(state, dict)
    masked = _mask_paths(state, root)
    assert isinstance(masked, dict)
    return masked


def _audit_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _child_store(root).audited_events():
        data = row.get("data")
        assert isinstance(data, dict)
        rows.append(
            {
                "run_seq": row.get("run_seq"),
                "event_type": row.get("type"),
                "correlation": row.get("correlation"),
                "correlation_reason": row.get("correlation_reason"),
                "agent_id": row.get("agent_id"),
                "invocation_id": row.get("invocation_id"),
                "pr_number": data.get("pr_number"),
                "head_sha": data.get("head_sha"),
                "controller_epoch": data.get("controller_epoch"),
            }
        )
    return rows


def _pending_seqs(root: Path) -> list[Any]:
    return [row.get("run_seq") for row in _child_store(root).quarantined_events()]


def test_recreated_generation_is_the_only_owner_of_the_leaf(tmp_path: Path) -> None:
    """The recreated dispatch carries the exact owner provenance of #102."""
    root = tmp_path / "recreated"
    _replay(root, _rows(BASELINE_ROWS))
    document = _child_document(root)
    state = document["slices"][LEAF]

    # The committed epoch is the recreated controller generation, not a
    # predecessor epoch and not an absent epoch.
    assert _spec()["controller_epoch"] == RECREATED_EPOCH
    assert _spec()["predecessor_controller_epoch"] == PREDECESSOR_EPOCH
    assert document["controller_epoch"] == RECREATED_EPOCH
    assert state["branch"] == HEAD_BRANCH
    assert state["base_ref"] == BASE_BRANCH
    assert state["dispatch_agent_id"] == LEAF_AGENT_ID
    assert state["dispatch_invocation_id"] == RECREATED_INVOCATION
    # The leaf owns dispatch generation 0 of the recreated generation; the
    # persisted checkpoint omits the zero rather than inventing a predecessor.
    assert state.get("dispatch_generation", 0) == 0


def test_current_publication_binds_only_with_exact_identity(tmp_path: Path) -> None:
    """#102 binds with all nine identity fields; #101 never reaches the state."""
    root = tmp_path / "baseline"
    _replay(root, _rows(BASELINE_ROWS))
    document = _child_document(root)
    state = document["slices"][LEAF]

    publication = state.get("publication")
    assert publication == {
        "pr_number": 102,
        "head_sha": HEAD_102,
        "head_branch": HEAD_BRANCH,
        "base_branch": BASE_BRANCH,
        "attempt": 1,
        "invocation_id": RECREATED_INVOCATION,
    }
    assert state.get("pr_number") == 102
    assert state.get("reviewed_head") == HEAD_102
    assert state.get("handoff") == {
        "pr_number": 102,
        "head_sha": HEAD_102,
        "attempt": 1,
        "invocation_id": RECREATED_INVOCATION,
        "agent_id": LEAF_AGENT_ID,
        "observed_at": "2026-09-22T20:14:00Z",
    }
    # No field of the bound state may carry the historical publication.
    bound = json.dumps(state)
    assert HEAD_101 not in bound
    assert '"pr_number": 101' not in bound


def test_historical_publication_is_permanent_audit_evidence(tmp_path: Path) -> None:
    """#101 is retained for audit, never as replayable pending work."""
    root = tmp_path / "audit"
    _replay(root, _rows(BASELINE_ROWS))

    assert _audit_rows(root) == [
        {
            "run_seq": HISTORICAL_RUN_SEQ,
            "event_type": "pr.filed",
            "correlation": "publication_history_audit",
            "correlation_reason": HISTORICAL_REFUSAL,
            "agent_id": LEAF_AGENT_ID,
            "invocation_id": "inv-predecessor-1",
            "pr_number": 101,
            "head_sha": HEAD_101,
            "controller_epoch": PREDECESSOR_EPOCH,
        }
    ]
    assert _pending_seqs(root) == []


def test_historical_publication_never_mutates_state(tmp_path: Path) -> None:
    """Removing the #101 row changes nothing but the audit evidence."""
    with_historical = tmp_path / "with-historical"
    without_historical = tmp_path / "without-historical"
    _replay(with_historical, _rows(BASELINE_ROWS))
    _replay(without_historical, _rows(WITHOUT_HISTORICAL_ROWS))

    assert _leaf_state(with_historical) == _leaf_state(without_historical)
    assert _audit_rows(with_historical) != _audit_rows(without_historical)
    assert _audit_rows(without_historical) == []
    assert _pending_seqs(with_historical) == _pending_seqs(without_historical) == []


def _case(case_name: str) -> Mapping[str, Any]:
    for entry in _spec()["binding_cases"]:
        if entry["case"] == case_name:
            return entry
    raise AssertionError(f"the fixture does not commit the {case_name!r} case")


MUTATION_CASES = tuple(entry["case"] for entry in _spec()["binding_cases"] if "row" in entry)
CONFLICT_CASES = tuple(entry["case"] for entry in _spec()["binding_cases"] if "deliver" in entry)


@pytest.mark.parametrize("case_name", MUTATION_CASES)
def test_single_field_publication_mismatch_refuses_binding(case_name: str, tmp_path: Path) -> None:
    """Every single-field mismatch is indistinguishable from no publication."""
    case = _case(case_name)
    root = tmp_path / case_name
    _replay(root, _patched(case["row"], case["patch"]))
    control = tmp_path / f"{case_name}-control"
    _replay(control, _rows(CONTROL_ROWS))

    state = _child_document(root)["slices"][LEAF]
    assert state.get("publication") is None
    assert state.get("handoff") is None
    assert state.get("pr_number") is None
    assert state.get("reviewed_head") is None
    # The refused row never reduced, so the durable state equals the run in
    # which the row was never observed at all.
    assert _leaf_state(root) == _leaf_state(control)


@pytest.mark.parametrize("case_name", MUTATION_CASES)
def test_generation_mismatch_is_retained_as_audit_evidence(case_name: str, tmp_path: Path) -> None:
    """Generation-class refusals are permanent audit rows with their reason."""
    case = _case(case_name)
    root = tmp_path / case_name
    _replay(root, _patched(case["row"], case["patch"]))
    rows = _audit_rows(root)

    historical = [row for row in rows if row["run_seq"] == CURRENT_RUN_SEQ]
    if "audit_reason" in case:
        assert len(historical) == 1
        row = historical[0]
        assert row["correlation_reason"] == case["audit_reason"]
        assert row["event_type"] == "pr.filed"
        assert row["correlation"] == "publication_history_audit"
        assert row["pr_number"] == 102
        assert row["head_sha"] == HEAD_102
    else:
        # Owner, branch, and slice-identity refusals are not publication
        # history: they are acknowledged without durable audit evidence.
        assert historical == []
    assert [row["run_seq"] for row in rows if row["run_seq"] == HISTORICAL_RUN_SEQ] == [
        HISTORICAL_RUN_SEQ
    ]
    if case_name != "slice-identity":
        assert _pending_seqs(root) == []


@pytest.mark.parametrize("case_name", CONFLICT_CASES)
def test_conflicting_publication_after_binding_is_refused(case_name: str, tmp_path: Path) -> None:
    """A second publication row cannot rewrite the bound PR number or head."""
    case = _case(case_name)
    root = tmp_path / case_name
    _replay(root, _rows((*BASELINE_ROWS, case["deliver"])))
    baseline = tmp_path / f"{case_name}-baseline"
    _replay(baseline, _rows(BASELINE_ROWS))

    publication = _child_document(root)["slices"][LEAF].get("publication")
    assert publication is not None
    assert publication["pr_number"] == 102
    assert publication["head_sha"] == HEAD_102
    assert _leaf_state(root) == _leaf_state(baseline)
    # An ordinary conflict is not publication history and never becomes work.
    assert [row["run_seq"] for row in _audit_rows(root)] == [HISTORICAL_RUN_SEQ]
    assert _pending_seqs(root) == []


def test_slice_identity_mismatch_is_retained_as_unresolved_pending_work(
    tmp_path: Path,
) -> None:
    """A publication naming another slice stays pending and binds nothing."""
    case = _case("slice-identity")
    root = tmp_path / "slice-identity"
    _replay(root, _patched(case["row"], case["patch"]))

    assert _pending_seqs(root) == [CURRENT_RUN_SEQ]
    assert [row["run_seq"] for row in _audit_rows(root)] == [HISTORICAL_RUN_SEQ]
    assert _child_document(root)["slices"][LEAF].get("publication") is None
