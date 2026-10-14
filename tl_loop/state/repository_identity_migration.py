"""Pure records and diagnostics for audited repository identity migration."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .repository_identity import canonical_repository_url

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RepositoryIdentityMigrationError(RuntimeError):
    """A repository identity could not be migrated without guessing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChildIdentityEvidence:
    """Fingerprint of one declared subTL checkpoint that corroborates a URL."""

    run_id: str
    checkpoint_sha256: str
    identity_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("child identity evidence requires a run id")
        for name in ("checkpoint_sha256", "identity_sha256"):
            digest = getattr(self, name)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError(f"child identity evidence {name} must be a SHA-256 digest")

    def to_document(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "identity_sha256": self.identity_sha256,
        }


def identity_url_from_document(value: object) -> tuple[str | None, str]:
    """Return a canonical URL and source-shape tag without trusting legacy fields."""
    if value is None:
        return None, "missing"
    if not isinstance(value, Mapping):
        raise RepositoryIdentityMigrationError("malformed", "repository identity is not an object")
    keys = frozenset(value)
    if keys == {"remote_url"}:
        shape = "canonical"
    elif (
        keys <= {"owner", "repo", "base_branch", "forge_host", "remote_url"}
        and {
            "owner",
            "repo",
            "base_branch",
        }
        <= keys
    ):
        shape = "legacy"
    else:
        raise RepositoryIdentityMigrationError(
            "ambiguous", "repository identity has an unsupported field shape"
        )
    raw_url = value.get("remote_url")
    if raw_url is None or raw_url == "":
        if shape == "canonical":
            raise RepositoryIdentityMigrationError(
                "malformed", "canonical repository identity has no remote_url"
            )
        return None, shape
    if not isinstance(raw_url, str):
        raise RepositoryIdentityMigrationError(
            "malformed", "repository identity remote_url is not text"
        )
    try:
        canonical_url = canonical_repository_url(raw_url)
        if shape == "canonical" and canonical_url != raw_url:
            shape = "legacy"
        return canonical_url, shape
    except ValueError as error:
        message = str(error)
        code = (
            "ambiguous" if "ambiguous" in message or "credential marker" in message else "malformed"
        )
        raise RepositoryIdentityMigrationError(
            code, f"repository identity remote_url is {code}"
        ) from error


def digest_bytes(value: bytes) -> str:
    """Fingerprint exact durable bytes without retaining their contents."""
    return hashlib.sha256(value).hexdigest()


def digest_url(value: str) -> str:
    """Fingerprint a canonical URL for the audit record."""
    return digest_bytes(value.encode("utf-8"))


def transition_id(
    *,
    run_id: str,
    source_checkpoint_sha256: str,
    target_identity_sha256: str,
    child_evidence: Sequence[ChildIdentityEvidence],
) -> str:
    """Return a stable ID for one source snapshot and its identity proof."""
    payload = "\n".join(
        (
            run_id,
            source_checkpoint_sha256,
            target_identity_sha256,
            *(
                f"{item.run_id}:{item.checkpoint_sha256}:{item.identity_sha256}"
                for item in sorted(child_evidence, key=lambda evidence: evidence.run_id)
            ),
        )
    )
    return digest_bytes(payload.encode("utf-8"))


def make_audit_record(
    *,
    run_id: str,
    source_checkpoint_sha256: str,
    source_shape: str,
    target_identity_sha256: str,
    evidence: Sequence[ChildIdentityEvidence],
    created_at: str,
    actor: str,
) -> dict[str, object]:
    """Build the credential-free, immutable in-checkpoint audit projection."""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "transition_id": transition_id(
            run_id=run_id,
            source_checkpoint_sha256=source_checkpoint_sha256,
            target_identity_sha256=target_identity_sha256,
            child_evidence=evidence,
        ),
        "source_checkpoint_sha256": source_checkpoint_sha256,
        "source_shape": source_shape,
        "target_identity_sha256": target_identity_sha256,
        "corroborating_children": [
            item.to_document() for item in sorted(evidence, key=lambda item: item.run_id)
        ],
        "created_at": created_at,
        "actor": actor,
        "reason_code": (
            "bootstrap_missing_identity"
            if source_shape == "missing"
            else "canonicalize_legacy_identity"
        ),
    }


def validate_audit_record(value: object) -> dict[str, object]:
    """Validate and normalize one credential-free migration audit record."""
    if not isinstance(value, Mapping):
        raise RepositoryIdentityMigrationError(
            "malformed", "repository identity migration audit is not an object"
        )
    required = {
        "schema_version",
        "run_id",
        "transition_id",
        "source_checkpoint_sha256",
        "source_shape",
        "target_identity_sha256",
        "corroborating_children",
        "created_at",
        "actor",
        "reason_code",
    }
    if frozenset(value) != required:
        raise RepositoryIdentityMigrationError(
            "malformed", "repository identity migration audit has an unsupported shape"
        )
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise RepositoryIdentityMigrationError(
            "malformed", "repository identity migration audit version is unsupported"
        )
    run_id = value.get("run_id")
    source_sha = value.get("source_checkpoint_sha256")
    target_sha = value.get("target_identity_sha256")
    shape = value.get("source_shape")
    if not isinstance(run_id, str) or not run_id:
        raise RepositoryIdentityMigrationError("malformed", "migration audit run id is invalid")
    for name, digest in (
        ("source checkpoint", source_sha),
        ("target identity", target_sha),
        ("transition", value.get("transition_id")),
    ):
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RepositoryIdentityMigrationError(
                "malformed", f"migration audit {name} digest is invalid"
            )
    if not isinstance(shape, str) or shape not in {"missing", "legacy"}:
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit source shape is invalid"
        )
    expected_reason = (
        "bootstrap_missing_identity" if shape == "missing" else "canonicalize_legacy_identity"
    )
    if value.get("reason_code") != expected_reason:
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit reason does not match its source shape"
        )
    if any(
        not isinstance(value.get(key), str) or not value.get(key) for key in ("created_at", "actor")
    ):
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit timestamp or actor is invalid"
        )
    raw_children = value.get("corroborating_children")
    if not isinstance(raw_children, list):
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit child evidence is not an array"
        )
    children: list[ChildIdentityEvidence] = []
    try:
        for child in raw_children:
            if not isinstance(child, Mapping) or frozenset(child) != {
                "run_id",
                "checkpoint_sha256",
                "identity_sha256",
            }:
                raise ValueError("child evidence has an unsupported shape")
            child_run_id = child["run_id"]
            checkpoint_sha = child["checkpoint_sha256"]
            identity_sha = child["identity_sha256"]
            if not all(
                isinstance(item, str) for item in (child_run_id, checkpoint_sha, identity_sha)
            ):
                raise ValueError("child evidence fields must be text")
            children.append(ChildIdentityEvidence(child_run_id, checkpoint_sha, identity_sha))
    except (TypeError, ValueError) as error:
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit child evidence is invalid"
        ) from error
    if [child.run_id for child in children] != sorted({child.run_id for child in children}):
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit child evidence must be sorted and unique"
        )
    if shape == "missing" and not children:
        raise RepositoryIdentityMigrationError(
            "malformed", "missing-identity audit has no child corroboration"
        )
    expected_transition = transition_id(
        run_id=run_id,
        source_checkpoint_sha256=source_sha,
        target_identity_sha256=target_sha,
        child_evidence=children,
    )
    if value.get("transition_id") != expected_transition:
        raise RepositoryIdentityMigrationError(
            "malformed", "migration audit transition id does not match its evidence"
        )
    return {
        **dict(value),
        "corroborating_children": [child.to_document() for child in children],
    }
