"""Pure readiness assessment for reference-project verification evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .reference_verification import (
    ProvenanceKind,
    ReferenceProjectObservation,
    ReferenceSourceState,
    ReferenceVerificationError,
    ReferenceVerificationEvidence,
    ReferenceVerificationRequirement,
    VerificationOutcome,
    _validate_text,
)


@dataclass(frozen=True)
class ReferenceVerificationDecision:
    """Readiness result for only the explicitly required checks."""

    ready: bool
    blockers: tuple[str, ...] = ()


def assess_reference_verification(
    requirements: Sequence[ReferenceVerificationRequirement],
    evidence: Sequence[ReferenceVerificationEvidence],
    observations: Sequence[ReferenceProjectObservation],
    *,
    run_id: str,
    scope_id: str,
) -> ReferenceVerificationDecision:
    """Evaluate required evidence without executing any declared command."""
    _validate_text(run_id, "run_id")
    _validate_text(scope_id, "scope_id")
    required = tuple(item for item in requirements if item.required)
    _reject_duplicate_requirements(required)
    blockers: list[str] = []
    for requirement in required:
        _assess_requirement(requirement, evidence, observations, run_id, scope_id, blockers)
    return ReferenceVerificationDecision(not blockers, tuple(blockers))


def _assess_requirement(
    requirement: ReferenceVerificationRequirement,
    evidence: Sequence[ReferenceVerificationEvidence],
    observations: Sequence[ReferenceProjectObservation],
    run_id: str,
    scope_id: str,
    blockers: list[str],
) -> None:
    candidates = [
        item
        for item in evidence
        if item.run_id == run_id
        and item.scope_id == scope_id
        and item.path == requirement.path
        and item.kind is requirement.kind
        and item.command == requirement.command
    ]
    if not candidates:
        blockers.append(
            f"no matching evidence for required {requirement.kind.value} at {requirement.path}"
        )
        return
    observed = [item for item in observations if item.path == requirement.path]
    if len(observed) != 1:
        blockers.append(
            f"current reference observation is missing or ambiguous for {requirement.path}"
        )
        return
    current, candidate = observed[0], candidates[-1]
    if current.remote_url is None:
        blockers.append(f"current reference identity is unavailable at {requirement.path}")
        return
    if current.remote_url != requirement.remote_url:
        blockers.append(
            f"current reference identity differs from the requirement at {requirement.path}"
        )
        return
    if candidate.remote_url != requirement.remote_url:
        blockers.append(f"evidence identity differs from the requirement at {requirement.path}")
        return
    if current.source_state is ReferenceSourceState.UNINITIALIZED:
        blockers.append(
            f"reference is not initialized for required verification at {requirement.path}"
        )
        return
    if current.source_state is not ReferenceSourceState.CLEAN:
        blockers.append(f"current source is not clean or unavailable at {requirement.path}")
        return
    if current.commit_sha is None or candidate.commit_sha != current.commit_sha:
        blockers.append(f"evidence commit does not match current commit at {requirement.path}")
        return
    if candidate.source_state is not ReferenceSourceState.CLEAN:
        blockers.append(f"evidence source was not clean at {requirement.path}")
        return
    if candidate.outcome is not VerificationOutcome.PASSED:
        blockers.append(
            f"required {requirement.kind.value} outcome is {candidate.outcome.value} at {requirement.path}"
        )
        return
    if candidate.provenance.kind is ProvenanceKind.OPERATOR_CLAIM:
        blockers.append(f"operator claim is not executed evidence at {requirement.path}")


def _reject_duplicate_requirements(
    requirements: Sequence[ReferenceVerificationRequirement],
) -> None:
    keys = [(item.path, item.kind, item.command) for item in requirements]
    if len(set(keys)) != len(keys):
        raise ReferenceVerificationError("verification requirements contain duplicate checks")
