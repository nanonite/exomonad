"""Reference verification records share RunStore's atomic, immutable path."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.state.reference_verification import (
    ProvenanceKind,
    ReferenceSourceState,
    ReferenceVerificationEvidence,
    VerificationKind,
    VerificationOutcome,
    VerificationProvenance,
)
from tl_loop.state.schema import BudgetLedger, FSMState, SchemaError, validate
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create

_RUN_ID = "reference-evidence-run"
_REMOTE = "https://forge.example.test/tools/llmlift.git"
_COMMIT = "a" * 40
_STAMP = datetime(2026, 11, 2, 12, tzinfo=UTC)


def _new_store(root: Path) -> RunStore:
    create(_RUN_ID, {}, root_dir=root)
    return RunStore(_RUN_ID, root)


def _evidence(
    store: RunStore,
    *,
    run_id: str | None = None,
    scope_id: str | None = None,
    recorded_at: datetime = _STAMP,
) -> ReferenceVerificationEvidence:
    manifest = store.load().plan_manifest
    assert manifest is not None
    return ReferenceVerificationEvidence.create(
        run_id=run_id or store.run_id,
        scope_id=scope_id or manifest.scope_id,
        path="tools/llmlift",
        remote_url=_REMOTE,
        commit_sha=_COMMIT,
        source_state=ReferenceSourceState.CLEAN,
        kind=VerificationKind.TEST,
        command="cargo test --workspace",
        outcome=VerificationOutcome.PASSED,
        provenance=VerificationProvenance(ProvenanceKind.CONTROLLER, invocation_id="inv-1"),
        recorded_at=recorded_at,
    )


def test_evidence_append_is_immutable_idempotent_and_survives_resume(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    evidence = _evidence(store)

    assert store.record_reference_verification(evidence) is True
    first_revision = store.load().revision
    assert (
        store.record_reference_verification(
            _evidence(store, recorded_at=datetime(2026, 11, 3, 12, tzinfo=UTC))
        )
        is False
    )
    assert store.load().revision == first_revision

    store.checkpoint(FSMState(TLPhase.TLPlanning, ()), {}, BudgetLedger(0, 0), 0)

    assert store.load().reference_verification == (evidence,)
    assert store.resume().reference_verification == (evidence,)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["reference_verification"] == {
        "schema_version": 1,
        "records": [evidence.to_document()],
    }


@pytest.mark.parametrize(
    ("run_id", "scope_id", "message"),
    [
        ("other-run", None, "run identity"),
        (None, "other-scope", "scope identity"),
    ],
)
def test_cross_run_or_scope_append_is_rejected_without_revision(
    tmp_path: Path, run_id: str | None, scope_id: str | None, message: str
) -> None:
    store = _new_store(tmp_path)
    before = store.load()

    with pytest.raises(ValueError, match=message):
        store.record_reference_verification(_evidence(store, run_id=run_id, scope_id=scope_id))

    assert store.load() == before


def test_concurrent_retries_append_one_immutable_record(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    evidence = _evidence(store)
    writers = (store, RunStore(store.run_id, tmp_path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(lambda writer: writer.record_reference_verification(evidence), writers)
        )

    assert sorted(results) == [False, True]
    assert store.load().reference_verification == (evidence,)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", "different-run", "checkpoint run_id"),
        ("scope_id", "different-scope", "manifest scope"),
    ],
)
def test_schema_rejects_seeded_cross_scope_evidence(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    store = _new_store(tmp_path)
    evidence = _evidence(
        store,
        run_id=value if field == "run_id" else None,
        scope_id=value if field == "scope_id" else None,
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["reference_verification"] = {
        "schema_version": 1,
        "records": [evidence.to_document()],
    }

    with pytest.raises(SchemaError, match=message):
        validate(document)


def test_schema_rejects_malformed_and_duplicate_evidence_ids(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    evidence = _evidence(store).to_document()
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["reference_verification"] = {
        "schema_version": 1,
        "records": [evidence, evidence],
    }

    with pytest.raises(SchemaError, match="repeats an evidence_id"):
        validate(document)

    conflicting = dict(evidence)
    conflicting["command"] = "cargo test --different"
    document["reference_verification"]["records"] = [conflicting]
    with pytest.raises(SchemaError, match="evidence_id does not match"):
        validate(document)


def test_append_refuses_conflicting_durable_content_without_rewrite(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    evidence = _evidence(store)
    store.record_reference_verification(evidence)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["reference_verification"]["records"][0]["command"] = "cargo test --different"
    store.path.write_text(json.dumps(document), encoding="utf-8")
    conflicted = store.path.read_bytes()

    with pytest.raises(SchemaError, match="evidence_id does not match"):
        store.record_reference_verification(evidence)

    assert store.path.read_bytes() == conflicted
