"""Reference verification evidence stays optional and revision-bound."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tl_loop.state.reference_verification import (
    ReferenceProjectObservation,
    ReferenceSourceState,
    ReferenceVerificationError,
    ReferenceVerificationEvidence,
    ReferenceVerificationRequirement,
    VerificationKind,
    VerificationOutcome,
    VerificationProvenance,
)
from tl_loop.state.reference_verification_assessment import assess_reference_verification

REMOTE = "https://example.invalid/tools/llmlift.git"
COMMIT = "a" * 40


def _requirement(*, required: bool = True) -> ReferenceVerificationRequirement:
    return ReferenceVerificationRequirement(
        path="tools/llmlift",
        remote_url=REMOTE,
        kind=VerificationKind.TEST,
        command="cargo test --workspace",
        required=required,
    )


def _evidence(
    *,
    outcome: VerificationOutcome = VerificationOutcome.PASSED,
    source_state: ReferenceSourceState = ReferenceSourceState.CLEAN,
    provenance: VerificationProvenance | None = None,
    commit: str = COMMIT,
    run_id: str = "root",
    scope_id: str = "root",
    path: str = "tools/llmlift",
    remote_url: str = REMOTE,
    command: str = "cargo test --workspace",
) -> ReferenceVerificationEvidence:
    return ReferenceVerificationEvidence.create(
        run_id=run_id,
        scope_id=scope_id,
        path=path,
        remote_url=remote_url,
        commit_sha=commit,
        source_state=source_state,
        kind=VerificationKind.TEST,
        command=command,
        outcome=outcome,
        provenance=provenance or VerificationProvenance.controller("invoke-1"),
        recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
    )


def _observation(
    *,
    source_state: ReferenceSourceState = ReferenceSourceState.CLEAN,
    commit: str | None = COMMIT,
) -> ReferenceProjectObservation:
    return ReferenceProjectObservation(
        path="tools/llmlift",
        remote_url=REMOTE,
        commit_sha=commit,
        source_state=source_state,
    )


def test_passed_evidence_is_bound_to_declared_project_revision_and_command() -> None:
    evidence = _evidence()
    decision = assess_reference_verification(
        (_requirement(),),
        (evidence,),
        (_observation(),),
        run_id="root",
        scope_id="root",
    )

    assert decision.ready
    assert evidence.to_document()["outcome"] == "passed"
    assert ReferenceVerificationEvidence.from_document(evidence.to_document()) == evidence


def test_missing_or_nonpassing_evidence_gates_only_explicit_required_checks() -> None:
    for outcome in (
        VerificationOutcome.FAILED,
        VerificationOutcome.NOT_RUN,
        VerificationOutcome.UNAVAILABLE,
        VerificationOutcome.TIMED_OUT,
    ):
        decision = assess_reference_verification(
            (_requirement(),),
            (_evidence(outcome=outcome),),
            (_observation(),),
            run_id="root",
            scope_id="root",
        )
        assert not decision.ready
        assert outcome.value in decision.blockers[0]

    optional = assess_reference_verification(
        (_requirement(required=False),),
        (_evidence(outcome=VerificationOutcome.FAILED),),
        (),
        run_id="root",
        scope_id="root",
    )
    missing = assess_reference_verification(
        (),
        (),
        (),
        run_id="root",
        scope_id="root",
    )
    assert optional.ready
    assert missing.ready


@pytest.mark.parametrize(
    ("evidence", "observation", "message"),
    [
        (_evidence(commit="b" * 40), _observation(), "does not match current commit"),
        (_evidence(source_state=ReferenceSourceState.DIRTY), _observation(), "was not clean"),
        (
            _evidence(),
            _observation(source_state=ReferenceSourceState.DIRTY),
            "current source is not clean",
        ),
        (
            _evidence(),
            ReferenceProjectObservation(
                path="tools/llmlift",
                remote_url="https://example.invalid/other.git",
                commit_sha=COMMIT,
                source_state=ReferenceSourceState.CLEAN,
            ),
            "identity differs",
        ),
        (
            _evidence(),
            ReferenceProjectObservation(
                path="tools/llmlift",
                remote_url=REMOTE,
                commit_sha=None,
                source_state=ReferenceSourceState.UNINITIALIZED,
            ),
            "not initialized",
        ),
    ],
)
def test_revision_and_source_mismatches_hold(
    evidence: ReferenceVerificationEvidence,
    observation: ReferenceProjectObservation,
    message: str,
) -> None:
    decision = assess_reference_verification(
        (_requirement(),),
        (evidence,),
        (observation,),
        run_id="root",
        scope_id="root",
    )
    assert not decision.ready
    assert message in decision.blockers[0]


def test_undeclared_command_and_wrong_scope_cannot_satisfy_requirement() -> None:
    wrong_command = _evidence(command="cargo test --all")
    wrong_scope = _evidence(scope_id="root/other")
    for evidence in (wrong_command, wrong_scope):
        decision = assess_reference_verification(
            (_requirement(),),
            (evidence,),
            (_observation(),),
            run_id="root",
            scope_id="root",
        )
        assert not decision.ready
        assert "no matching evidence" in decision.blockers[0]


def test_operator_claim_is_retained_but_not_mistaken_for_executed_test() -> None:
    claim = _evidence(provenance=VerificationProvenance.operator_claim("operator-7"))
    decision = assess_reference_verification(
        (_requirement(),),
        (claim,),
        (_observation(),),
        run_id="root",
        scope_id="root",
    )

    assert not decision.ready
    assert "operator claim is not executed evidence" in decision.blockers[0]
    provenance = claim.to_document()["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["kind"] == "operator_claim"


def test_imported_evidence_requires_an_artifact_reference_and_digest() -> None:
    with pytest.raises(ValueError, match="artifact reference and SHA-256"):
        VerificationProvenance.imported("build-result")

    evidence = _evidence(
        provenance=VerificationProvenance.imported("evidence/llmlift-test.json", "c" * 64)
    )
    decision = assess_reference_verification(
        (_requirement(),),
        (evidence,),
        (_observation(),),
        run_id="root",
        scope_id="root",
    )
    assert decision.ready

    with pytest.raises(ReferenceVerificationError, match="must not contain credentials"):
        VerificationProvenance.imported(
            "https://example.invalid/result.json?token=secret", "c" * 64
        )


def test_build_and_test_evidence_keep_separate_kinds_and_outcome_vocabulary() -> None:
    build = ReferenceVerificationRequirement(
        "tools/llmlift", REMOTE, VerificationKind.BUILD, "cargo build", required=True
    )
    build_evidence = ReferenceVerificationEvidence.create(
        run_id="root",
        scope_id="root",
        path="tools/llmlift",
        remote_url=REMOTE,
        commit_sha=COMMIT,
        source_state=ReferenceSourceState.CLEAN,
        kind=VerificationKind.BUILD,
        command="cargo build",
        outcome=VerificationOutcome.PASSED,
        provenance=VerificationProvenance.controller("invoke-build"),
        recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    decision = assess_reference_verification(
        (build,),
        (build_evidence,),
        (_observation(),),
        run_id="root",
        scope_id="root",
    )

    assert decision.ready
    assert {item.value for item in VerificationOutcome} == {
        "passed",
        "failed",
        "not_run",
        "unavailable",
        "timed_out",
    }


@pytest.mark.parametrize(
    ("path", "remote_url", "commit", "message"),
    [
        ("../outside", REMOTE, COMMIT, "workspace-relative"),
        (
            "tools/llmlift",
            "https://user:token@example.invalid/tools/llmlift.git",
            COMMIT,
            "canonical and credential-free",
        ),
    ],
)
def test_requirement_and_observation_identity_inputs_fail_closed(
    path: str, remote_url: str, commit: str, message: str
) -> None:
    with pytest.raises(ReferenceVerificationError, match=message):
        ReferenceVerificationRequirement(
            path, remote_url, VerificationKind.TEST, "cargo test", required=True
        )
    with pytest.raises(ReferenceVerificationError, match=message):
        ReferenceProjectObservation(path, remote_url, commit, ReferenceSourceState.CLEAN)


def test_observation_rejects_non_git_commit_identity() -> None:
    with pytest.raises(ReferenceVerificationError, match="lowercase Git object ID"):
        ReferenceProjectObservation(
            "tools/llmlift", REMOTE, "not-a-commit", ReferenceSourceState.CLEAN
        )


def test_evidence_identity_digest_detects_record_tampering() -> None:
    document = _evidence().to_document()
    document["outcome"] = "failed"

    with pytest.raises(ReferenceVerificationError, match="evidence_id"):
        ReferenceVerificationEvidence.from_document(document)


def test_idempotency_identity_ignores_only_recorded_at() -> None:
    first = _evidence()
    second = ReferenceVerificationEvidence.create(
        run_id="root",
        scope_id="root",
        path="tools/llmlift",
        remote_url=REMOTE,
        commit_sha=COMMIT,
        source_state=ReferenceSourceState.CLEAN,
        kind=VerificationKind.TEST,
        command="cargo test --workspace",
        outcome=VerificationOutcome.PASSED,
        provenance=VerificationProvenance.controller("invoke-1"),
        recorded_at=datetime(2026, 11, 3, tzinfo=UTC),
    )

    assert first.evidence_id == second.evidence_id
    assert first.identity_document() == second.identity_document()
