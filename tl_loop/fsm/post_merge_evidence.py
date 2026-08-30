"""Durable evidence records for post-merge recovery."""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import require_positive as _require_positive
from .evidence import require_text as _require_text


@dataclass(frozen=True)
class PushReceipt:
    """Authoritative proof for one bookkeeping push into one repository lane."""

    repository: str
    parent_branch: str
    child_id: str
    lane_epoch: int
    push_intent_id: str
    push_journal_id: str
    push_receipt_id: str
    expected_base_sha: str
    pushed_commit: str
    observed_remote_head: str
    ancestry_proof: str

    def __post_init__(self) -> None:
        _require_text(self.repository, "push receipt repository")
        _require_text(self.parent_branch, "push receipt parent branch")
        _require_text(self.child_id, "push receipt child ID")
        _require_positive(self.lane_epoch, "push receipt lane epoch")
        for name in (
            "push_intent_id",
            "push_journal_id",
            "push_receipt_id",
            "expected_base_sha",
            "pushed_commit",
            "observed_remote_head",
            "ancestry_proof",
        ):
            _require_text(getattr(self, name), f"push receipt {name}")


__all__ = ["PushReceipt"]
