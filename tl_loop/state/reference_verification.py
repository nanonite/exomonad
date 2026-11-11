"""Typed, inert evidence for optional reference-project build and test checks.

This module records or evaluates evidence only. It never starts a build, test,
or Git mutation. Callers must provide an explicit requirement and a fresh
read-only observation before evidence can satisfy a gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import PurePosixPath
from typing import TypeVar
from urllib.parse import urlsplit

from .repository_identity import canonical_repository_url

_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EnumT = TypeVar("_EnumT", bound=Enum)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "run_id",
        "scope_id",
        "path",
        "remote_url",
        "commit_sha",
        "source_state",
        "kind",
        "command",
        "outcome",
        "provenance",
        "recorded_at",
    }
)
_REQUIREMENT_KEYS = frozenset({"path", "remote_url", "kind", "command", "required"})
_PROVENANCE_KEYS = frozenset({"kind", "invocation_id", "artifact_ref", "artifact_sha256", "actor"})


class ReferenceVerificationError(ValueError):
    """A reference verification declaration or evidence record is malformed."""


class VerificationKind(str, Enum):
    BUILD = "build"
    TEST = "test"


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


class ReferenceSourceState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"
    UNINITIALIZED = "uninitialized"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProvenanceKind(str, Enum):
    CONTROLLER = "controller"
    IMPORTED = "imported"
    OPERATOR_CLAIM = "operator_claim"


@dataclass(frozen=True)
class ReferenceVerificationRequirement:
    """One explicitly declared command that may gate reference readiness."""

    path: str
    remote_url: str
    kind: VerificationKind
    command: str
    required: bool = False

    def __post_init__(self) -> None:
        _validate_path(self.path)
        _validate_canonical_url(self.remote_url)
        _enum(self.kind, VerificationKind, "verification kind")
        _validate_command(self.command)
        if type(self.required) is not bool:
            raise ReferenceVerificationError("required must be a boolean")

    def to_document(self) -> dict[str, object]:
        return {
            "path": self.path,
            "remote_url": self.remote_url,
            "kind": self.kind.value,
            "command": self.command,
            "required": self.required,
        }

    @classmethod
    def from_document(cls, value: object) -> ReferenceVerificationRequirement:
        if not isinstance(value, Mapping) or frozenset(value) != _REQUIREMENT_KEYS:
            raise ReferenceVerificationError(
                "reference verification requirement has an unsupported shape"
            )
        required = value.get("required")
        if type(required) is not bool:
            raise ReferenceVerificationError("required must be a boolean")
        return cls(
            path=_required_text(value, "path"),
            remote_url=_required_text(value, "remote_url"),
            kind=_enum_value(value.get("kind"), VerificationKind, "verification kind"),
            command=_required_text(value, "command"),
            required=required,
        )


@dataclass(frozen=True)
class VerificationProvenance:
    """Origin of a result; operator claims are retained but never execution proof."""

    kind: ProvenanceKind
    invocation_id: str | None = None
    artifact_ref: str | None = None
    artifact_sha256: str | None = None
    actor: str | None = None

    def __post_init__(self) -> None:
        _enum(self.kind, ProvenanceKind, "provenance kind")
        if self.invocation_id is not None:
            _validate_text(self.invocation_id, "invocation_id")
        if self.actor is not None:
            _validate_text(self.actor, "actor")
        if self.artifact_ref is not None:
            _validate_artifact_ref(self.artifact_ref)
        if self.artifact_sha256 is not None and not _SHA256.fullmatch(self.artifact_sha256):
            raise ReferenceVerificationError("artifact_sha256 must be a lowercase SHA-256 digest")
        if (self.artifact_ref is None) != (self.artifact_sha256 is None):
            raise ReferenceVerificationError(
                "artifact reference and SHA-256 must be provided together"
            )
        if self.kind is ProvenanceKind.CONTROLLER and self.invocation_id is None:
            raise ReferenceVerificationError("controller provenance requires invocation_id")
        if self.kind is ProvenanceKind.IMPORTED and self.artifact_ref is None:
            raise ReferenceVerificationError(
                "imported provenance requires artifact reference and SHA-256"
            )
        if self.kind is ProvenanceKind.OPERATOR_CLAIM and self.actor is None:
            raise ReferenceVerificationError("operator claim provenance requires actor")

    @classmethod
    def controller(cls, invocation_id: str) -> VerificationProvenance:
        return cls(ProvenanceKind.CONTROLLER, invocation_id=invocation_id)

    @classmethod
    def imported(
        cls, artifact_ref: str, artifact_sha256: str | None = None
    ) -> VerificationProvenance:
        return cls(
            ProvenanceKind.IMPORTED,
            artifact_ref=artifact_ref,
            artifact_sha256=artifact_sha256,
        )

    @classmethod
    def operator_claim(cls, actor: str) -> VerificationProvenance:
        return cls(ProvenanceKind.OPERATOR_CLAIM, actor=actor)

    def to_document(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "invocation_id": self.invocation_id,
            "artifact_ref": self.artifact_ref,
            "artifact_sha256": self.artifact_sha256,
            "actor": self.actor,
        }

    @classmethod
    def from_document(cls, value: object) -> VerificationProvenance:
        if not isinstance(value, Mapping) or frozenset(value) != _PROVENANCE_KEYS:
            raise ReferenceVerificationError("provenance has an unsupported shape")
        return cls(
            kind=_enum_value(value.get("kind"), ProvenanceKind, "provenance kind"),
            invocation_id=_optional_text(value.get("invocation_id"), "invocation_id"),
            artifact_ref=_optional_text(value.get("artifact_ref"), "artifact_ref"),
            artifact_sha256=_optional_text(value.get("artifact_sha256"), "artifact_sha256"),
            actor=_optional_text(value.get("actor"), "actor"),
        )


@dataclass(frozen=True)
class ReferenceVerificationEvidence:
    """One immutable result bound to a run scope and exact reference revision."""

    run_id: str
    scope_id: str
    path: str
    remote_url: str
    commit_sha: str | None
    source_state: ReferenceSourceState
    kind: VerificationKind
    command: str
    outcome: VerificationOutcome
    provenance: VerificationProvenance
    recorded_at: str
    evidence_id: str

    def __post_init__(self) -> None:
        _validate_text(self.run_id, "run_id")
        _validate_text(self.scope_id, "scope_id")
        _validate_path(self.path)
        _validate_canonical_url(self.remote_url)
        if self.commit_sha is not None and not _OID.fullmatch(self.commit_sha):
            raise ReferenceVerificationError("commit_sha must be a lowercase Git object ID")
        _enum(self.source_state, ReferenceSourceState, "source state")
        _enum(self.kind, VerificationKind, "verification kind")
        _enum(self.outcome, VerificationOutcome, "verification outcome")
        if not isinstance(self.provenance, VerificationProvenance):
            raise ReferenceVerificationError("provenance must be a VerificationProvenance")
        _validate_command(self.command)
        _validate_timestamp(self.recorded_at)
        expected_id = _evidence_id(self.identity_document())
        if self.evidence_id != expected_id:
            raise ReferenceVerificationError("evidence_id does not match its immutable evidence")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        scope_id: str,
        path: str,
        remote_url: str,
        commit_sha: str | None,
        source_state: ReferenceSourceState,
        kind: VerificationKind,
        command: str,
        outcome: VerificationOutcome,
        provenance: VerificationProvenance,
        recorded_at: datetime,
    ) -> ReferenceVerificationEvidence:
        stamp = _utc_timestamp(recorded_at)
        identity = {
            "run_id": run_id,
            "scope_id": scope_id,
            "path": path,
            "remote_url": remote_url,
            "commit_sha": commit_sha,
            "source_state": source_state.value,
            "kind": kind.value,
            "command": command,
            "outcome": outcome.value,
            "provenance": provenance.to_document(),
        }
        return cls(
            run_id=run_id,
            scope_id=scope_id,
            path=path,
            remote_url=remote_url,
            commit_sha=commit_sha,
            source_state=source_state,
            kind=kind,
            command=command,
            outcome=outcome,
            provenance=provenance,
            recorded_at=stamp,
            evidence_id=_evidence_id(identity),
        )

    def identity_document(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scope_id": self.scope_id,
            "path": self.path,
            "remote_url": self.remote_url,
            "commit_sha": self.commit_sha,
            "source_state": self.source_state.value,
            "kind": self.kind.value,
            "command": self.command,
            "outcome": self.outcome.value,
            "provenance": self.provenance.to_document(),
        }

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "evidence_id": self.evidence_id,
            **self.identity_document(),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_document(cls, value: object) -> ReferenceVerificationEvidence:
        if not isinstance(value, Mapping) or frozenset(value) != _EVIDENCE_KEYS:
            raise ReferenceVerificationError(
                "reference verification evidence has an unsupported shape"
            )
        if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            raise ReferenceVerificationError(
                "reference verification evidence schema is unsupported"
            )
        return cls(
            run_id=_required_text(value, "run_id"),
            scope_id=_required_text(value, "scope_id"),
            path=_required_text(value, "path"),
            remote_url=_required_text(value, "remote_url"),
            commit_sha=_optional_text(value.get("commit_sha"), "commit_sha"),
            source_state=_enum_value(
                value.get("source_state"), ReferenceSourceState, "source state"
            ),
            kind=_enum_value(value.get("kind"), VerificationKind, "verification kind"),
            command=_required_text(value, "command"),
            outcome=_enum_value(value.get("outcome"), VerificationOutcome, "verification outcome"),
            provenance=VerificationProvenance.from_document(value.get("provenance")),
            recorded_at=_required_text(value, "recorded_at"),
            evidence_id=_required_text(value, "evidence_id"),
        )


@dataclass(frozen=True)
class ReferenceProjectObservation:
    """Fresh read-only identity, commit and source-state observation."""

    path: str
    remote_url: str | None
    commit_sha: str | None
    source_state: ReferenceSourceState

    def __post_init__(self) -> None:
        _validate_path(self.path)
        if self.remote_url is not None:
            _validate_canonical_url(self.remote_url)
        if self.commit_sha is not None and not _OID.fullmatch(self.commit_sha):
            raise ReferenceVerificationError(
                "observed commit_sha must be a lowercase Git object ID"
            )
        _enum(self.source_state, ReferenceSourceState, "observed source state")


def validate_reference_verification_document(
    value: object,
) -> tuple[ReferenceVerificationEvidence, ...]:
    """Validate the optional RunStore evidence list and reject duplicate IDs."""
    if value is None:
        return ()
    if not isinstance(value, Mapping) or frozenset(value) != {"schema_version", "records"}:
        raise ReferenceVerificationError("run.reference_verification has an unsupported shape")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise ReferenceVerificationError("run.reference_verification schema is unsupported")
    records = value.get("records")
    if not isinstance(records, list):
        raise ReferenceVerificationError("run.reference_verification.records must be an array")
    parsed = tuple(ReferenceVerificationEvidence.from_document(item) for item in records)
    identifiers = [item.evidence_id for item in parsed]
    if len(set(identifiers)) != len(identifiers):
        raise ReferenceVerificationError("run.reference_verification repeats an evidence_id")
    return parsed


def parse_reference_verification_requirements(
    value: object,
) -> tuple[ReferenceVerificationRequirement, ...]:
    """Parse the closed, task-local reference-check declaration shape."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ReferenceVerificationError("reference_verification_requirements must be an array")
    requirements = tuple(ReferenceVerificationRequirement.from_document(item) for item in value)
    keys = [(item.path, item.kind, item.command) for item in requirements]
    if len(set(keys)) != len(keys):
        raise ReferenceVerificationError("reference verification requirements contain duplicates")
    return requirements


def _evidence_id(identity: Mapping[str, object]) -> str:
    canonical = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_path(value: str) -> None:
    _validate_text(value, "reference path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReferenceVerificationError("reference path must be normalized and workspace-relative")


def _validate_canonical_url(value: str) -> None:
    try:
        canonical = canonical_repository_url(value)
    except ValueError as error:
        raise ReferenceVerificationError("reference remote URL is invalid") from error
    if canonical != value:
        raise ReferenceVerificationError(
            "reference remote URL must be canonical and credential-free"
        )


def _validate_command(value: str) -> None:
    _validate_text(value, "verification command")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReferenceVerificationError("verification command must be a single safe text value")


def _validate_artifact_ref(value: str) -> None:
    _validate_text(value, "artifact_ref")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReferenceVerificationError("artifact_ref contains control characters")
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReferenceVerificationError("artifact_ref must not contain credentials")
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ReferenceVerificationError("artifact_ref uses an unsupported location")
        return
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
        raise ReferenceVerificationError("artifact_ref must be a safe relative path or public URL")


def _validate_timestamp(value: str) -> None:
    _validate_text(value, "recorded_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReferenceVerificationError("recorded_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReferenceVerificationError("recorded_at must be expressed in UTC")


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReferenceVerificationError("recorded_at must be a timezone-aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceVerificationError(f"{field} must be non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReferenceVerificationError(f"{field} contains a control character")


def _required_text(value: Mapping[str, object], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str):
        raise ReferenceVerificationError(f"{field} must be text")
    return raw


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReferenceVerificationError(f"{field} must be text or null")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ReferenceVerificationError(f"{field} is not a recognized value")


def _enum_value(value: object, expected: type[_EnumT], field: str) -> _EnumT:
    try:
        return expected(value)
    except (TypeError, ValueError) as error:
        raise ReferenceVerificationError(f"{field} is not a recognized value") from error
