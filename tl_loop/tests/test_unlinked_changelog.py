"""An unlinked merged slice gets one roles.tl CHANGELOG-coverage judgment (#1170).

Below every path: the slice always completes with a typed changelog outcome,
the decision is checkpointed before any write, and the judgment fails hard
only when the controller has no resolved roles.tl model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.lane import LanePhase, LaneState
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.post_merge_events import PostMergeNotApplicable, UnlinkedChangelogJudged
from tl_loop.fsm.post_merge_transition import advance_post_merge
from tl_loop.loop.driver import TLLoopConfig, _drain_post_merge_recovery
from tl_loop.rlm.call import RlmCallError
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.select.policy import validate_policy
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import RepositoryIdentity, RunState, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import ReviewBackend, _selector_policy

UNRELEASED = "### Fixed\n- Fix an older thing (#1)"

SYNCED_EVIDENCE = {
    "child_id": "leaf",
    "repository": "https://forge.example.com/org/repo.git",
    "parent_branch": "main",
    "pr_number": "42",
    "head_sha": "head-a",
    "merge_journal_id": "merge-journal",
    "lane_epoch": "1",
    "parent_commit_sha": "parent-commit",
    "forgejo_merge_commit_sha": "merge-commit",
}


@dataclass
class ChangelogTransport:
    """Records effect calls and answers the two changelog tools."""

    context: dict[str, object] | None = None
    context_error: str | None = None
    entry_outcome: str = "entry_added"
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "post_merge_changelog_context":
            if self.context_error is not None:
                return {"success": False, "error": self.context_error}
            return {"success": True, "result": self.context or _context()}
        if tool_name == "post_merge_changelog_entry":
            added = self.entry_outcome == "entry_added"
            return {
                "success": True,
                "result": {
                    "child_id": arguments["child_id"],
                    "repository": arguments["repository"],
                    "parent_branch": arguments["parent_branch"],
                    "expected_base_sha": arguments["expected_base_sha"],
                    "intent_id": arguments["intent_id"],
                    "outcome": self.entry_outcome,
                    "detail": "" if added else "stale info",
                    "commit_sha": "entry-commit" if added else None,
                    "observed_remote_head": "entry-commit" if added else None,
                },
            }
        if tool_name == "emit_controller_event":
            return {"success": True, "result": {}}
        raise AssertionError(f"unexpected effect {tool_name}")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls if name != "emit_controller_event"]

    def events(self) -> list[dict[str, object]]:
        return [args for name, args in self.calls if name == "emit_controller_event"]


def _context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "child_id": "leaf",
        "expected_base_sha": "parent-commit",
        "changelog_present": True,
        "unreleased_present": True,
        "unreleased_section": UNRELEASED,
        "changed_paths": ["src/feature.py"],
        "changed_paths_error": None,
        "pr_edited_changelog": False,
        "pr_title": "Add the feature",
        "pr_body": "Adds the feature.",
        "pr_lookup_error": None,
    }
    context.update(overrides)
    return context


def _config(backend: object, *, token_budget: int = 120_000) -> TLLoopConfig:
    policy = _selector_policy()
    policy["roles"]["tl"]["token_budget"] = token_budget  # type: ignore[index]
    return TLLoopConfig(
        active=True,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        policy=validate_policy(policy),
        tl_model_choice=RlmModelChoice(
            model_id="tl-model",
            backend=backend,
            role="tl",
            store=RlmCallStore(),
            context_length=20_000,
        ),
    )


def _synced_store(root_dir: Path, evidence: dict[str, str] | None = None) -> tuple[RunStore, RunState]:
    plan = {"leaves": [{"name": "leaf", "task": "work"}]}
    manifest = build_plan_manifest(plan, scope_id="unlinked")
    create(
        "unlinked",
        {
            "plan_manifest": manifest.to_document(),
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=root_dir,
    )
    store = RunStore("unlinked", root_dir)
    state = store.load()
    current = replace(
        state.slices["leaf"],
        status=SliceStatus.MERGED,
        base_ref="main",
        pr_number=42,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED, evidence or dict(SYNCED_EVIDENCE)
        ),
    )
    integration = replace(
        state.integration,
        lanes={
            "https://forge.example.com/org/repo.git:main": LaneState(
                "https://forge.example.com/org/repo.git",
                "main",
                phase=LanePhase.INTEGRATING,
                child_id="leaf",
                lane_epoch=1,
                head_sha="head-a",
            )
        },
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=integration,
        plan_manifest=manifest,
    )
    return store, state


def _drain(
    store: RunStore, state: RunState, config: TLLoopConfig, transport: ChangelogTransport
) -> RunState:
    return _drain_post_merge_recovery(
        state, "leaf", 42, config, EffectClient(transport), [], store
    )


def _final(state: RunState) -> PostMergeState:
    post_merge = state.slices["leaf"].post_merge
    assert post_merge is not None
    return post_merge


def test_captured_work_completes_without_writing(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [RlmResponse({"action": "captured", "reason": "entry (#1) covers it"}, 30, 7)]
    )
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    final = _final(done)
    assert final.phase is PostMergePhase.COMPLETE
    assert final.evidence["issue_close_outcome"] == "not_applicable"
    assert final.evidence["changelog_decision"] == "captured"
    assert final.evidence["changelog_outcome"] == "captured"
    assert transport.names() == ["post_merge_changelog_context"]
    assert done.integration.lanes["https://forge.example.com/org/repo.git:main"].phase is LanePhase.IDLE
    assert store.load().budgets.role_spent["tl"] == 37
    (event,) = transport.events()
    assert event["event_type"] == "tl.unlinked_changelog_resolved"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["outcome"] == "captured"
    assert payload["tokens"] == 37
    assert payload["model"] == "tl-model"


def test_judgment_sees_the_merged_slice_evidence(tmp_path: Path) -> None:
    backend = ReviewBackend([RlmResponse({"action": "captured", "reason": "covered"})])
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    _drain(store, state, _config(backend), transport)

    (request,) = backend.requests
    prompt = json.dumps(request.inputs)
    for text in ("Add the feature", "src/feature.py", "Fix an older thing (#1)"):
        assert text in prompt
    (context_call,) = [args for name, args in transport.calls if name == "post_merge_changelog_context"]
    assert context_call["merge_commit_sha"] == "merge-commit"
    assert context_call["expected_base_sha"] == "parent-commit"


def test_missing_work_is_written_committed_and_pushed(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "action": "add_entry",
                    "reason": "no entry describes the feature",
                    "section": "Added",
                    "entry": "Add the feature",
                }
            )
        ]
    )
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    final = _final(done)
    assert final.phase is PostMergePhase.COMPLETE
    assert final.evidence["changelog_outcome"] == "entry_added"
    assert final.evidence["changelog_commit_sha"] == "entry-commit"
    assert final.evidence["observed_remote_head"] == "entry-commit"
    assert transport.names() == ["post_merge_changelog_context", "post_merge_changelog_entry"]
    (_, entry_call) = transport.calls[1]
    assert entry_call["section"] == "Added"
    assert entry_call["entry"] == "Add the feature"
    assert entry_call["expected_base_sha"] == "parent-commit"
    assert entry_call["intent_id"] == final.evidence["changelog_judgment_intent_id"]


@pytest.mark.parametrize("outcome", ["entry_push_failed", "entry_write_failed"])
def test_a_failed_write_is_a_typed_outcome_and_the_slice_completes(
    tmp_path: Path, outcome: str
) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {"action": "add_entry", "reason": "missing", "section": "Fixed", "entry": "Fix it"}
            )
        ]
    )
    transport = ChangelogTransport(entry_outcome=outcome)
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    final = _final(done)
    assert final.phase is PostMergePhase.COMPLETE
    assert final.evidence["changelog_outcome"] == outcome
    assert final.evidence["changelog_outcome_detail"] == "stale info"
    assert "changelog_commit_sha" not in final.evidence
    assert done.integration.lanes["https://forge.example.com/org/repo.git:main"].phase is LanePhase.IDLE


def test_a_parent_without_changelog_makes_no_model_call(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = ChangelogTransport(context=_context(changelog_present=False, unreleased_present=False, unreleased_section=None))
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    final = _final(done)
    assert final.evidence["changelog_outcome"] == "no_changelog"
    assert backend.requests == []
    assert transport.names() == ["post_merge_changelog_context"]


def test_a_changelog_without_unreleased_makes_no_model_call(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = ChangelogTransport(context=_context(unreleased_present=False, unreleased_section=None))
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    assert _final(done).evidence["changelog_outcome"] == "no_changelog"
    assert backend.requests == []


def test_a_backend_failure_completes_as_judgment_failed(tmp_path: Path) -> None:
    class FailingBackend:
        def complete(self, request: RlmRequest) -> object:
            raise RlmCallError("claude exited with code 1")

    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(FailingBackend()), transport)

    final = _final(done)
    assert final.phase is PostMergePhase.COMPLETE
    assert final.evidence["changelog_outcome"] == "judgment_failed"
    assert "claude exited with code 1" in final.evidence["changelog_outcome_detail"]
    assert done.slices["leaf"].dispatch_error is None


def test_an_exhausted_tl_budget_completes_as_judgment_failed(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [RlmResponse({"action": "captured", "reason": "covered"}, 50, 50)] * 3
    )
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend, token_budget=10), transport)

    assert _final(done).evidence["changelog_outcome"] == "judgment_failed"


def test_an_unreadable_context_completes_as_judgment_failed(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = ChangelogTransport(context_error="parent branch moved after synchronization")
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    final = _final(done)
    assert final.evidence["changelog_outcome"] == "judgment_failed"
    assert "parent branch moved" in final.evidence["changelog_outcome_detail"]
    assert backend.requests == []


def test_a_malformed_judgment_completes_as_judgment_failed(tmp_path: Path) -> None:
    bad = RlmResponse({"action": "add_entry", "reason": "missing", "section": "Added", "entry": "- marked"})
    backend = ReviewBackend([bad, bad, bad])
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    done = _drain(store, state, _config(backend), transport)

    assert _final(done).evidence["changelog_outcome"] == "judgment_failed"
    assert "post_merge_changelog_entry" not in transport.names()


def test_a_restart_after_the_decision_writes_without_judging_again(tmp_path: Path) -> None:
    evidence = {
        **SYNCED_EVIDENCE,
        "changelog_judgment_intent_id": "intent-1",
        "changelog_decision": "add_entry",
        "changelog_decision_reason": "missing",
        "changelog_entry_section": "Fixed",
        "changelog_entry": "Fix it",
        "changelog_judgment_tokens": "12",
        "changelog_judgment_model": "tl-model",
    }
    backend = ReviewBackend([])
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path, evidence)

    done = _drain(store, state, _config(backend), transport)

    assert _final(done).evidence["changelog_outcome"] == "entry_added"
    assert backend.requests == []
    assert transport.names() == ["post_merge_changelog_entry"]
    (_, entry_call) = transport.calls[0]
    assert entry_call["intent_id"] == "intent-1"


def test_an_active_controller_without_a_tl_model_fails_hard(tmp_path: Path) -> None:
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)
    config = replace(_config(ReviewBackend([])), tl_model_choice=None)

    blocked = _drain(store, state, config, transport)

    assert _final(blocked).phase is PostMergePhase.PARENT_BRANCH_SYNCED
    assert "tl_model_choice" in (blocked.slices["leaf"].dispatch_error or "")
    assert any(gate.name == "tl-post-merge-leaf" for gate in blocked.gates)
    assert transport.calls == []


def test_a_shadow_controller_neither_judges_nor_writes(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = ChangelogTransport()
    store, state = _synced_store(tmp_path)

    unchanged = _drain(store, state, replace(_config(backend), active=False), transport)

    assert _final(unchanged).phase is PostMergePhase.PARENT_BRANCH_SYNCED
    assert transport.calls == []
    assert backend.requests == []


def test_completion_must_agree_with_the_recorded_decision() -> None:
    synced = PostMergeState(PostMergePhase.PARENT_BRANCH_SYNCED, dict(SYNCED_EVIDENCE))
    with pytest.raises(ValueError, match="requires a recorded changelog decision"):
        advance_post_merge(synced, PostMergeNotApplicable("leaf", "captured"))
    judged = advance_post_merge(
        synced, UnlinkedChangelogJudged("leaf", "intent-1", "captured", "covered")
    )
    with pytest.raises(ValueError, match="contradicts decision"):
        advance_post_merge(judged, PostMergeNotApplicable("leaf", "entry_added", "", "c", "c"))
    with pytest.raises(ValueError, match="unknown unlinked changelog outcome"):
        advance_post_merge(judged, PostMergeNotApplicable("leaf", "not_applicable"))


def test_entry_added_requires_its_commit_evidence() -> None:
    synced = PostMergeState(PostMergePhase.PARENT_BRANCH_SYNCED, dict(SYNCED_EVIDENCE))
    judged = advance_post_merge(
        synced,
        UnlinkedChangelogJudged("leaf", "intent-1", "add_entry", "missing", "Added", "Add it"),
    )
    with pytest.raises(ValueError, match="changelog entry commit SHA"):
        advance_post_merge(judged, PostMergeNotApplicable("leaf", "entry_added"))
    done = advance_post_merge(
        judged, PostMergeNotApplicable("leaf", "entry_added", "", "commit", "commit")
    )
    assert done.phase is PostMergePhase.COMPLETE
    assert advance_post_merge(
        done, PostMergeNotApplicable("leaf", "entry_added", "", "commit", "commit")
    ) == done


def test_a_judgment_replays_only_with_the_same_decision() -> None:
    synced = PostMergeState(PostMergePhase.PARENT_BRANCH_SYNCED, dict(SYNCED_EVIDENCE))
    event = UnlinkedChangelogJudged("leaf", "intent-1", "captured", "covered")
    judged = advance_post_merge(synced, event)
    assert advance_post_merge(judged, event) == judged
    with pytest.raises(ValueError, match="evidence mismatch"):
        advance_post_merge(
            judged, UnlinkedChangelogJudged("leaf", "intent-1", "add_entry", "x", "Added", "y")
        )
    with pytest.raises(ValueError, match="only an add_entry decision carries"):
        advance_post_merge(
            synced, UnlinkedChangelogJudged("leaf", "intent-1", "captured", "c", "Added", "y")
        )


def test_a_legacy_unlinked_completion_still_decodes() -> None:
    legacy = PostMergeState(
        PostMergePhase.COMPLETE,
        {
            **SYNCED_EVIDENCE,
            "issue_close_outcome": "not_applicable",
            "changelog_outcome": "not_applicable",
        },
    )
    assert legacy.phase is PostMergePhase.COMPLETE
