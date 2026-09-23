"""End-to-end escalation, atomic parking, and harness-switch coverage."""

from __future__ import annotations

import fcntl
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.loop.escalate import (
    EscalationError,
    HarnessSwitchDecision,
    IssueCreationError,
    IssueLookupUnavailable,
    ParkResult,
    _create_issue,
    _escalation_intent_path,
    _escalation_key,
    _issue_id,
    _legacy_escalation_intent_path,
    _run_token,
    _write_intent,
    authorize_harness_switch,
    bind_legacy_escalation,
    blocked_gate_name,
    park,
    switch_harness,
)
from tl_loop.state.schema import (
    BudgetLedger,
    ParkCause,
    PublicationBinding,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import RunStore, create

CAUSES = tuple(ParkCause)


def test_each_closed_cause_produces_a_parked_state() -> None:
    for cause in CAUSES:
        result = park(_slice(), cause)

        assert isinstance(result, SliceState)
        assert result.status is SliceStatus.PARKED
        assert result.park_cause is cause
        assert result.park_issue_id is None
        assert result.park_audit is not None
        assert result.park_audit["attempts"] == 2
        assert result.park_audit["verdict"] == Verdict.NO_GO.value
        assert result.park_audit["harness"] == "codex"
        assert result.park_audit["model"] == "gpt-test"


@pytest.mark.parametrize("cause", CAUSES)
def test_each_cause_creates_issue_and_blocks_transitive_dependents(
    tmp_path: Path, cause: ParkCause
) -> None:
    store = _store(tmp_path)
    issues: list[tuple[str, str]] = []

    def create_issue(title: str, description: str) -> int:
        issues.append((title, description))
        return 700

    result = park(
        _slice(),
        cause,
        store=store,
        issue_creator=create_issue,
        ledger=BudgetLedger(tokens=42, wall_seconds=3),
    )

    assert result == ParkResult(700, "root", ("child", "grandchild"))
    state = store.load()
    assert state.slices["root"].status is SliceStatus.PARKED
    assert state.slices["root"].park_cause is cause
    assert state.slices["root"].park_issue_id == 700
    assert state.slices["root"].park_audit is not None
    assert state.slices["root"].park_audit is not None
    ledger_audit = cast(Mapping[str, object], state.slices["root"].park_audit["ledger"])
    assert ledger_audit["tokens"] == 42
    assert state.slices["child"].status is SliceStatus.BLOCKED
    assert state.slices["child"].blocked_by == "root"
    assert state.slices["child"].park_issue_id == 700
    assert state.slices["grandchild"].status is SliceStatus.BLOCKED
    assert state.slices["grandchild"].blocked_by == "root"
    assert issues[0][0].startswith(f"Escalate slice root: {cause.value}")
    assert cause.value in issues[0][1]
    assert '"needs-human"' not in issues[0][1]


def test_effect_issue_creation_has_needs_human_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    creator = RecordingCreator()

    result = park(_slice(), ParkCause.REVIEW_STUCK, store=store, issue_creator=creator)

    assert isinstance(result, ParkResult)
    assert creator.labels == ("needs-human",)
    assert creator.priority == "high"


def test_externally_blocked_parking_is_gate_and_issue_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created: list[tuple[str, str]] = []

    def create_issue(title: str, description: str) -> int:
        created.append((title, description))
        return 703

    first = park(
        _slice(),
        ParkCause.BASE_CI_UNSTABLE,
        store=store,
        issue_creator=create_issue,
        audit={"attempt": 2, "recovery_action": "repair base CI", "needs_human": True},
    )
    second = park(
        _slice(),
        ParkCause.BASE_CI_UNSTABLE,
        store=store,
        issue_creator=create_issue,
        audit={"attempt": 2, "recovery_action": "repair base CI", "needs_human": True},
    )

    assert first == ParkResult(703, "root", ("child", "grandchild"))
    assert second == ParkResult(703, "root", ())
    assert len(created) == 1
    assert store.load().fsm.phase.value != "tl_failed"
    assert any(
        gate.name == blocked_gate_name("escalate-test", "root", 2, "base_ci_unstable")
        for gate in store.load().gates
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"issue_id": 5}, 5),
        ({"cicoIssueId": 6}, 6),
        ({"id": 7}, 7),
        ({"number": 8}, 8),
        (11, 11),
        ({"issue_id": 0}, None),
        ({"issue_id": -1}, None),
        ({"issue_id": "9"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_issue_id_accepts_canonical_and_legacy_shapes(
    payload: object, expected: int | None
) -> None:
    assert _issue_id(payload) == expected


def test_create_issue_parses_legacy_cico_issue_id_result() -> None:
    class LegacyCreator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del title, description, labels, priority
            return _tool_result({"cicoIssueId": 816})

    assert _create_issue(LegacyCreator(), _slice(), ParkCause.REVIEW_STUCK, {}) == 816


def test_create_issue_reuses_run_scoped_issue_with_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 900})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            issues = [{"issue_id": 900, "title": created[0]}] if created else []
            return _tool_result({"issues": issues})

    creator = Creator()
    first = _create_issue(
        creator, _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
    )
    assert first == 900

    # Simulate a crash after remote creation but before the intent recorded the
    # issue id: only the pre-failure request (with PR/head) survives.
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 1) [run {token}]"
    )
    intent_path = _escalation_intent_path(
        store, _escalation_key(store.run_id, "root", 1, ParkCause.REVIEW_STUCK)
    )
    _write_intent(
        intent_path,
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 1,
            "cause": ParkCause.REVIEW_STUCK.value,
            "title": title,
            "state": "requested",
            "pr_number": None,
            "head_sha": None,
        },
    )

    second = _create_issue(
        creator, _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
    )

    assert second == 900
    assert len(created) == 1, "a proven run-scoped issue must be reused"


def test_create_issue_reuses_bound_legacy_issue() -> None:
    class NoopCreator:
        def chainlink_issue_create(
            self, **kwargs: object
        ) -> ToolResult:
            raise AssertionError("a verified legacy binding must be reused")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    issue_id = _create_issue(
        NoopCreator(), _slice(), ParkCause.REVIEW_STUCK, {}, legacy_binding_id=816
    )

    assert issue_id == 816


def test_create_issue_fails_closed_when_lookup_is_unavailable(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class BrokenLookupCreator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("must not create when reconciliation is unavailable")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"unexpected": []})

    with pytest.raises(IssueLookupUnavailable):
        _create_issue(
            BrokenLookupCreator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
        )


def test_park_reconciles_bound_legacy_816_on_stored_first_attempt(tmp_path: Path) -> None:
    slice_id = "issue-811-substitution-model-architecture"
    target = replace(
        _slice(),
        id=slice_id,
        attempts=1,
        pr_number=44,
        publication=PublicationBinding(
            pr_number=44,
            head_sha="25ec8d08ca984b56497518dd05a572a116d1f883",
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )
    create(
        "escalate-test",
        {"slices": {slice_id: _record(slice_id, caller=target)}},
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)
    bind_legacy_escalation(
        store,
        slice_id=slice_id,
        cause=ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        attempt=1,
        issue_id=816,
        pr_number=44,
        head_sha="25ec8d08ca984b56497518dd05a572a116d1f883",
    )
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 999})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            return _tool_result({"issues": []})

    result = park(
        target,
        ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        store=store,
        issue_creator=Creator(),
    )

    assert isinstance(result, ParkResult)
    assert result.issue_id == 816
    assert created == [], "a verified legacy binding must be reused without creating"


def test_legacy_binding_requires_matching_publication(tmp_path: Path) -> None:
    slice_id = "issue-811-substitution-model-architecture"
    target = replace(
        _slice(),
        id=slice_id,
        attempts=1,
        pr_number=45,
        publication=PublicationBinding(
            pr_number=45,
            head_sha="25ec8d08ca984b56497518dd05a572a116d1f883",
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )
    create(
        "escalate-test",
        {"slices": {slice_id: _record(slice_id, caller=target)}},
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)
    # Bound to PR #44; this slice is PR #45, so the binding must not apply.
    bind_legacy_escalation(
        store,
        slice_id=slice_id,
        cause=ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        attempt=1,
        issue_id=816,
        pr_number=44,
        head_sha="25ec8d08ca984b56497518dd05a572a116d1f883",
    )
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 999})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            return _tool_result({"issues": []})

    result = park(
        target,
        ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        store=store,
        issue_creator=Creator(),
    )

    assert isinstance(result, ParkResult)
    assert result.issue_id == 999
    assert len(created) == 1


def test_run_token_is_rejected_when_empty_or_malformed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token_path = tmp_path / "escalate-test" / "escalations" / "run.token"
    token_path.parent.mkdir(parents=True, exist_ok=True)

    token_path.write_text("", encoding="utf-8")
    with pytest.raises(EscalationError):
        _create_issue(_NoopIssueCreator(), _slice(), ParkCause.REVIEW_STUCK, {}, store=store)

    token_path.write_text("not-a-token", encoding="utf-8")
    with pytest.raises(EscalationError):
        _create_issue(_NoopIssueCreator(), _slice(), ParkCause.REVIEW_STUCK, {}, store=store)


class _NoopIssueCreator:
    def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
        raise AssertionError("must not create with an unusable run token")

    def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
        del kwargs
        return _tool_result({"issues": []})


def test_legacy_binding_is_scoped_to_attempt(tmp_path: Path) -> None:
    slice_id = "issue-811-substitution-model-architecture"
    head_sha = "25ec8d08ca984b56497518dd05a572a116d1f883"
    target = replace(
        _slice(),
        id=slice_id,
        attempts=2,
        pr_number=44,
        publication=PublicationBinding(
            pr_number=44,
            head_sha=head_sha,
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=2,
        ),
    )
    create(
        "escalate-test",
        {"slices": {slice_id: _record(slice_id, caller=target)}},
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)
    # Binding is for attempt 1; this park is attempt 2.
    bind_legacy_escalation(
        store,
        slice_id=slice_id,
        cause=ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        attempt=1,
        issue_id=816,
        pr_number=44,
        head_sha=head_sha,
    )
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 999})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            return _tool_result({"issues": []})

    result = park(
        target,
        ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        store=store,
        issue_creator=Creator(),
        audit={"attempt": 2},
    )

    assert isinstance(result, ParkResult)
    assert result.issue_id == 999
    assert len(created) == 1


def test_park_fails_closed_on_unverified_recorded_issue(tmp_path: Path) -> None:
    from tl_loop.fsm.scope import TLFailed as RecursiveTLFailed

    slice_id = "issue-811-substitution-model-architecture"
    head_sha = "25ec8d08ca984b56497518dd05a572a116d1f883"
    target = replace(
        _slice(),
        id=slice_id,
        attempts=1,
        pr_number=44,
        publication=PublicationBinding(
            pr_number=44,
            head_sha=head_sha,
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )
    create(
        "escalate-test",
        {
            "slices": {slice_id: _record(slice_id, caller=target)},
            "fsm": RecursiveTLFailed(
                "chainlink issue result has no positive issue ID: {'cicoIssueId': 816}",
                ("root",),
            ),
        },
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("an unverified recorded issue must not be reused or duplicated")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="cannot be proven"):
        park(
            target,
            ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
            store=store,
            issue_creator=Creator(),
        )


def test_park_fails_closed_on_recorded_issue_from_other_publication(
    tmp_path: Path,
) -> None:
    from tl_loop.fsm.scope import TLFailed as RecursiveTLFailed

    slice_id = "issue-811-substitution-model-architecture"
    # The recorded issue predates this attempt; the current publication is a
    # different PR and head, so it must not be adopted.
    target = replace(
        _slice(),
        id=slice_id,
        attempts=1,
        pr_number=45,
        publication=PublicationBinding(
            pr_number=45,
            head_sha="different-head-sha",
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )
    create(
        "escalate-test",
        {
            "slices": {slice_id: _record(slice_id, caller=target)},
            "fsm": RecursiveTLFailed(
                "chainlink issue result has no positive issue ID: {'cicoIssueId': 816}",
                ("root",),
            ),
        },
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("a stale recorded issue must not be reused")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="cannot be proven"):
        park(
            target,
            ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
            store=store,
            issue_creator=Creator(),
        )


def test_create_issue_fails_closed_when_intent_lacks_provenance_keys(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 1) [run {token}]"
    )
    intent_path = _escalation_intent_path(
        store, _escalation_key(store.run_id, "root", 1, ParkCause.REVIEW_STUCK)
    )
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    # Identity is present but provenance keys are entirely absent.
    _write_intent(
        intent_path,
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 1,
            "cause": ParkCause.REVIEW_STUCK.value,
            "issue_id": 900,
            "title": title,
            "state": "created",
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("missing provenance must fail closed")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="does not match the current publication"):
        _create_issue(
            Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
        )


def test_park_refuses_stale_caller_slice(tmp_path: Path) -> None:
    persisted = replace(
        _slice(),
        attempts=1,
        pr_number=45,
        publication=PublicationBinding(
            pr_number=45,
            head_sha="newer-head",
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )
    create(
        "escalate-test",
        {"slices": {"root": _record("root", caller=persisted)}},
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)
    stale = replace(
        _slice(),
        attempts=1,
        pr_number=44,
        publication=PublicationBinding(
            pr_number=44,
            head_sha="older-head",
            head_branch="main.stage.issue",
            base_branch="main.stage",
            attempt=1,
        ),
    )

    with pytest.raises(EscalationError, match="stale or inconsistent"):
        park(stale, ParkCause.REVIEW_STUCK, store=store, issue_creator=_NoopIssueCreator())


def test_pre_389_intent_is_detected_and_blocks_creation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 2) [run {token}]"
    )
    # The actual pre-389 filename and (identity-less) intent format. The title
    # matches so the identity/provenance proof is what fails closed.
    legacy_path = _legacy_escalation_intent_path(
        store, store.run_id, "root", 2, ParkCause.REVIEW_STUCK
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent(
        legacy_path,
        {
            "title": title,
            "state": "created",
            "pr_number": None,
            "head_sha": None,
            "issue_id": 900,
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("must not create while an old intent is unresolved")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            # The remote lookup returns nothing; the old file alone must stop us.
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="cannot be proven"):
        _create_issue(
            Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=2, store=store
        )


def test_stale_audit_attempt_cannot_select_older_issue(tmp_path: Path) -> None:
    persisted = replace(_slice(), attempts=2)
    create(
        "escalate-test",
        {"slices": {"root": _record("root", caller=persisted)}},
        root_dir=tmp_path,
    )
    store = RunStore("escalate-test", root_dir=tmp_path)
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 1) [run {token}]"
    )
    _write_intent(
        _escalation_intent_path(
            store, _escalation_key(store.run_id, "root", 1, ParkCause.REVIEW_STUCK)
        ),
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 1,
            "cause": ParkCause.REVIEW_STUCK.value,
            "issue_id": 900,
            "title": title,
            "state": "created",
            "pr_number": None,
            "head_sha": None,
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("a stale audit attempt must not reuse or create")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": [{"issue_id": 900, "title": title}]})

    with pytest.raises(EscalationError, match="does not match the persisted slice attempt"):
        park(
            replace(_slice(), attempts=2),
            ParkCause.REVIEW_STUCK,
            store=store,
            issue_creator=Creator(),
            audit={"attempt": 1},
        )


def test_intent_paths_do_not_collide_for_lookalike_slice_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key_slash = _escalation_key(store.run_id, "a/b", 1, ParkCause.REVIEW_STUCK)
    key_underscore = _escalation_key(store.run_id, "a_b", 1, ParkCause.REVIEW_STUCK)

    assert key_slash != key_underscore
    assert _escalation_intent_path(store, key_slash) != _escalation_intent_path(
        store, key_underscore
    )


def test_mixed_version_intents_cannot_create_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 2) [run {token}]"
    )
    # Pre-389 intent records issue 900 but carries no identity/provenance.
    legacy_path = _legacy_escalation_intent_path(
        store, store.run_id, "root", 2, ParkCause.REVIEW_STUCK
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent(
        legacy_path,
        {
            "title": title,
            "state": "requested",
            "pr_number": None,
            "head_sha": None,
            "issue_id": 900,
        },
    )
    # The newer digest intent is only "requested" and records no issue id.
    digest_path = _escalation_intent_path(
        store, _escalation_key(store.run_id, "root", 2, ParkCause.REVIEW_STUCK)
    )
    _write_intent(
        digest_path,
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 2,
            "cause": ParkCause.REVIEW_STUCK.value,
            "title": title,
            "state": "requested",
            "pr_number": None,
            "head_sha": None,
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("a mixed-version state must never create issue 901")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            # Remote lookup is empty: the old file alone must stop creation.
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="cannot be proven"):
        _create_issue(
            Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=2, store=store
        )


@pytest.mark.parametrize("bad_attempt", [True, 1.0, 1, 0, -1, "2", None])
def test_park_rejects_invalid_audit_attempt(tmp_path: Path, bad_attempt: object) -> None:
    store = _store(tmp_path)

    with pytest.raises(EscalationError):
        park(
            _slice(),
            ParkCause.REVIEW_STUCK,
            store=store,
            issue_creator=_NoopIssueCreator(),
            audit={"attempt": bad_attempt},
        )


def test_park_accepts_matching_integer_audit_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 900})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            return _tool_result({"issues": []})

    result = park(
        _slice(),
        ParkCause.REVIEW_STUCK,
        store=store,
        issue_creator=Creator(),
        audit={"attempt": 2},
    )

    assert isinstance(result, ParkResult)
    assert result.issue_id == 900
    assert len(created) == 1


def test_intent_with_mismatched_title_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stale_title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 2) [run stale-token]"
    )
    intent_path = _escalation_intent_path(
        store, _escalation_key(store.run_id, "root", 2, ParkCause.REVIEW_STUCK)
    )
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent(
        intent_path,
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 2,
            "cause": ParkCause.REVIEW_STUCK.value,
            "issue_id": 900,
            "title": stale_title,
            "state": "created",
            "pr_number": None,
            "head_sha": None,
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("a mismatched-title intent must not authorize creation")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result(
                {"issues": [{"issue_id": 900, "title": stale_title}]}
            )

    # The crash window where an old issue exists remotely but the ID was not
    # written under the current title: the title mismatch fails closed.
    with pytest.raises(EscalationError, match="title"):
        _create_issue(
            Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=2, store=store
        )


def test_concurrent_pre_389_writer_serializes_new_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cause = ParkCause.REVIEW_STUCK
    attempt = 2
    legacy_path = _legacy_escalation_intent_path(
        store, store.run_id, "root", attempt, cause
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = legacy_path.with_suffix(".lock")
    holdings = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def pre_389_writer() -> None:
        with open(lock_path, "w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            holdings.set()
            release.wait(timeout=10)
            # A pre-389 writer records the issue without identity or provenance.
            _write_intent(
                legacy_path,
                {
                    "title": f"Escalate slice root: {cause.value} (attempt {attempt})",
                    "state": "created",
                    "pr_number": None,
                    "head_sha": None,
                    "issue_id": 900,
                },
            )
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            outcome["created"] = True
            return _tool_result({"issue_id": 901})

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    def new_writer() -> None:
        try:
            _create_issue(Creator(), _slice(), cause, {}, attempt=attempt, store=store)
            outcome["result"] = "created"
        except EscalationError as error:
            outcome["result"] = "failed"
            outcome["error"] = str(error)

    old_thread = threading.Thread(target=pre_389_writer)
    old_thread.start()
    assert holdings.wait(timeout=5)

    new_thread = threading.Thread(target=new_writer)
    new_thread.start()
    # The new invocation must block on the historical lock.
    time.sleep(0.2)
    assert new_thread.is_alive()
    assert "result" not in outcome

    release.set()
    new_thread.join(timeout=10)
    old_thread.join(timeout=10)

    assert outcome.get("result") == "failed"
    assert "created" not in outcome, "a concurrent pre-389 writer must not be duplicated"


def test_reconciliation_does_not_cross_runs(tmp_path: Path) -> None:
    store_a = _store(tmp_path / "a")
    store_b = _store(tmp_path / "b")
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 700 + len(created)})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            issues = [{"issue_id": 701, "title": created[0]}] if created else []
            return _tool_result({"issues": issues})

    first = _create_issue(
        Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store_a
    )
    second = _create_issue(
        Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store_b
    )

    assert first == 701
    assert second == 702
    assert len(created) == 2, "a run must not attach to another run's escalation issue"


def test_create_issue_fails_closed_on_intent_provenance_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = _run_token(store)
    title = (
        f"Escalate slice root: {ParkCause.REVIEW_STUCK.value} (attempt 1) [run {token}]"
    )
    intent_path = _escalation_intent_path(
        store, _escalation_key(store.run_id, "root", 1, ParkCause.REVIEW_STUCK)
    )
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent(
        intent_path,
        {
            "run_id": store.run_id,
            "slice_id": "root",
            "attempt": 1,
            "cause": ParkCause.REVIEW_STUCK.value,
            "issue_id": 900,
            "title": title,
            "state": "created",
            "pr_number": 44,
            "head_sha": "head-a",
        },
    )

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("must not create when intent provenance mismatches")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError, match="does not match the current publication"):
        _create_issue(
            Creator(),
            _slice(),
            ParkCause.REVIEW_STUCK,
            {},
            attempt=1,
            pr_number=45,
            head_sha="head-b",
            store=store,
        )


def test_escalation_intent_is_scoped_per_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created: list[str] = []

    class Creator:
        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            return _tool_result({"issue_id": 900 + len(created)})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            issues = [{"issue_id": 900, "title": created[0]}] if created else []
            return _tool_result({"issues": issues})

    first = _create_issue(
        Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
    )
    second = _create_issue(
        Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=2, store=store
    )

    assert first == 901
    assert second == 902
    assert first != second
    assert len(created) == 2, "a later attempt must not reuse the earlier attempt's issue"


def test_escalation_fails_closed_on_malformed_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = _escalation_key(store.run_id, "root", 1, ParkCause.REVIEW_STUCK)
    intent_path = _escalation_intent_path(store, key)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text("{ not valid json", encoding="utf-8")

    class Creator:
        def chainlink_issue_create(self, **kwargs: object) -> ToolResult:
            raise AssertionError("must not create with an unusable intent")

        def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
            del kwargs
            return _tool_result({"issues": []})

    with pytest.raises(EscalationError):
        _create_issue(
            Creator(), _slice(), ParkCause.REVIEW_STUCK, {}, attempt=1, store=store
        )


def test_park_reuses_durable_intent_for_non_gated_cause(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created: list[str] = []

    class Creator:
        marker_title: str | None = None

        def chainlink_issue_create(
            self,
            *,
            title: str,
            description: str | None = None,
            labels: Sequence[str] | None = None,
            priority: str | None = None,
        ) -> ToolResult:
            del description, labels, priority
            created.append(title)
            Creator.marker_title = title
            return _tool_result({"issue_id": 555})

        def chainlink_issue_list(
            self,
            *,
            labels: Sequence[str] | None = None,
            milestone: str | None = None,
            priority: str | None = None,
            status: str | None = None,
        ) -> ToolResult:
            del labels, milestone, priority, status
            return _tool_result(
                {"issues": [{"issue_id": 555, "title": Creator.marker_title}]}
            )

    creator = Creator()
    first = park(_slice(), ParkCause.REVIEW_STUCK, store=store, issue_creator=creator)
    second = park(_slice(), ParkCause.REVIEW_STUCK, store=store, issue_creator=creator)

    assert first.issue_id == 555
    assert second.issue_id == 555
    assert len(created) == 1, "non-gated causes must also deduplicate"


def test_effect_client_parses_legacy_cico_issue_id_at_the_boundary(tmp_path: Path) -> None:
    transport = ParkingTransport()
    transport.chainlink_result = {"cicoIssueId": 816}
    store = _store(tmp_path)
    result = park(
        _slice(),
        ParkCause.REVIEW_STUCK,
        store=store,
        issue_creator=EffectClient(transport),
    )

    assert isinstance(result, ParkResult)
    assert result.issue_id == 816
    assert store.load().slices["root"].park_issue_id == 816


def test_failed_issue_creation_does_not_mutate_state(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(IssueCreationError, match="positive issue ID"):
        park(_slice(), ParkCause.RETRIES_EXHAUSTED, store=store, issue_creator=lambda *_: 0)

    assert store.load().slices["root"].status is SliceStatus.PENDING


def test_nested_mappingproxy_audit_is_durable() -> None:
    descriptions: list[str] = []

    def create_issue(title: str, description: str) -> int:
        del title
        descriptions.append(description)
        return 702

    result = _create_issue(
        create_issue,
        _slice(),
        ParkCause.REVIEW_STUCK,
        MappingProxyType(
            {"nested": MappingProxyType({"classification": MappingProxyType({"value": "stalled"})})}
        ),
    )

    assert result == 702
    assert '"classification": {"value": "stalled"}' in descriptions[0]


def test_declared_harness_switch_is_allowed_and_audited() -> None:
    decision = authorize_harness_switch(
        "codex", "claude", "review repair", "model-a", "high", ["claude"], env={}
    )

    assert isinstance(decision, HarnessSwitchDecision)
    assert decision.allowed is True
    assert decision.cause is None
    assert decision.audit["from_harness"] == "codex"
    assert decision.audit["to_harness"] == "claude"


def test_ungated_harness_switch_parks_with_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    creator = RecordingCreator()

    result = switch_harness(
        _slice(),
        "codex",
        "claude",
        "review repair",
        "model-a",
        "high",
        [],
        env={},
        store=store,
        issue_creator=creator,
    )

    assert isinstance(result, ParkResult)
    state = store.load()
    audit = state.slices["root"].park_audit
    assert state.slices["root"].park_cause is ParkCause.HARNESS_SWITCH_REQUESTED
    assert audit is not None
    assert audit["from_harness"] == "codex"
    assert audit["to_harness"] == "claude"
    assert audit["reason"] == "review repair"
    assert audit["model"] == "model-a"
    assert audit["effort"] == "high"


def test_operator_flag_allows_ungated_harness_switch() -> None:
    decision = authorize_harness_switch(
        "codex",
        "claude",
        "review repair",
        "model-a",
        "high",
        [],
        env={"EXOMONAD_ALLOW_HARNESS_SWITCH": "1"},
    )

    assert decision.allowed is True
    assert decision.cause is None


def _store(tmp_path: Path) -> RunStore:
    create(
        "escalate-test",
        {
            "slices": {
                "root": _record("root", caller=_slice()),
                "child": _record("child", depends_on=["root"]),
                "grandchild": _record("grandchild", depends_on=["child"]),
            }
        },
        root_dir=tmp_path,
    )
    return RunStore("escalate-test", root_dir=tmp_path)


def _slice() -> SliceState:
    return SliceState(
        id="root",
        status=SliceStatus.PENDING,
        paths=("src/root.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-test",
        branch="task/root",
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=2,
        verdict=Verdict.NO_GO,
    )


def _record(
    slice_id: str,
    *,
    depends_on: list[str] | None = None,
    caller: SliceState | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": slice_id,
        "status": "pending",
        "paths": [f"src/{slice_id}.py"],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "agent_type": "codex",
        "model": "gpt-test",
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }
    if caller is not None:
        record["attempts"] = caller.attempts
        record["pr_number"] = caller.pr_number
        publication = getattr(caller, "publication", None)
        if publication is not None:
            record["publication"] = {
                "pr_number": publication.pr_number,
                "head_sha": publication.head_sha,
                "head_branch": publication.head_branch,
                "base_branch": publication.base_branch,
                "attempt": publication.attempt,
                "invocation_id": publication.invocation_id,
            }
    return record


class RecordingCreator:
    """Effect-shaped issue creator used to verify the boundary payload."""

    labels: tuple[str, ...] | None = None
    priority: str | None = None

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: Sequence[str] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        del title, description
        self.labels = tuple(labels) if labels is not None else None
        self.priority = priority
        return _tool_result({"issue_id": 701})


class ParkingTransport:
    """Effect transport that records the durable parking observations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.chainlink_result: dict[str, object] = {"issue_id": 701}

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "chainlink_issue_create":
            return {"success": True, "result": self.chainlink_result}
        return {"success": True, "result": None}


def test_durable_parking_emits_bounded_observations(tmp_path: Path) -> None:
    transport = ParkingTransport()
    result = park(
        _slice(),
        ParkCause.REVIEW_STUCK,
        store=_store(tmp_path),
        issue_creator=EffectClient(transport),
    )

    assert isinstance(result, ParkResult)
    events = [
        arguments["payload"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]
    assert events == [
        {"slice_id": "root", "from_status": "pending", "to_status": "parked"},
        {"slice_id": "child", "from_status": "pending", "to_status": "blocked"},
        {"slice_id": "grandchild", "from_status": "pending", "to_status": "blocked"},
        {"slice_id": "root", "park_cause": "review_stuck", "attempts": 2},
    ]


def _tool_result(value: dict[str, object]) -> ToolResult:
    raw = cast(JsonObject, {"success": True, "result": value})
    return ToolResult(
        raw=raw,
        success=True,
        result=cast(JsonValue, value),
        error=None,
    )
