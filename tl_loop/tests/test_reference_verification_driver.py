"""Accepted reference requirements gate only their declared production dispatch."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.loop import driver as driver_module
from tl_loop.loop.driver import (
    LoopCancelled,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _dispatch_children,
    run_tl_loop,
)
from tl_loop.loop.reference_verification import observe_reference_project
from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import LedgerSnapshot, SequenceStatus
from tl_loop.state.reference_verification import (
    ReferenceSourceState,
    ReferenceVerificationEvidence,
    ReferenceVerificationRequirement,
    VerificationKind,
    VerificationOutcome,
    VerificationProvenance,
)
from tl_loop.state.schema import RepositoryIdentity, RunState
from tl_loop.state.store import RunStore, create

REMOTE = "https://forge.example.invalid/org/llmlift.git"
REPOSITORY = RepositoryIdentity("https://forge.example.invalid/org/workspace.git")
DECLARED_COMMAND = "touch MUST_NOT_BE_EXECUTED"


class RecordingEffects:
    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: JsonObject
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == "spawn_leaf":
            return {
                "success": True,
                "result": {"agent_id": "leaf-codex", "invocation_id": "leaf-invocation"},
            }
        if tool_name == "emit_controller_event":
            return {"success": True, "result": {"event_id": "controller-event"}}
        return {"success": True, "result": {}}


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _workspace_with_reference(root: Path) -> str:
    reference_source = root.parent / "reference-source"
    reference_source.mkdir()
    _git(reference_source, "init", "-q", "-b", "main")
    _git(reference_source, "config", "user.name", "Reference fixture")
    _git(reference_source, "config", "user.email", "reference@example.invalid")
    (reference_source / "source.txt").write_text("reference source\n", encoding="utf-8")
    _git(reference_source, "add", "source.txt")
    _git(reference_source, "commit", "-m", "reference source")
    commit = _git(reference_source, "rev-parse", "HEAD")

    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Workspace fixture")
    _git(root, "config", "user.email", "workspace@example.invalid")
    reference = root / "tools" / "llmlift"
    reference.parent.mkdir()
    _git(root, "clone", "-q", "--local", str(reference_source), str(reference))
    _git(reference, "remote", "set-url", "origin", REMOTE)
    (root / ".gitmodules").write_text(
        f'[submodule "llmlift"]\n\tpath = tools/llmlift\n\turl = {REMOTE}\n',
        encoding="utf-8",
    )
    _git(root, "add", ".gitmodules")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},tools/llmlift")
    _git(root, "commit", "-m", "record reference")
    return commit


def _plan(*, required: bool = True) -> WorkPlan:
    requirement = ReferenceVerificationRequirement(
        "tools/llmlift", REMOTE, VerificationKind.TEST, DECLARED_COMMAND, required
    )
    return _plan_for_requirements((requirement,))


def _plan_for_requirements(
    requirements: tuple[ReferenceVerificationRequirement, ...],
) -> WorkPlan:
    return WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf",
                    "task": "work on the declared assignment",
                    "chainlink_issue_id": None,
                    "reference_verification_requirements": [
                        requirement.to_document() for requirement in requirements
                    ],
                }
            ]
        }
    )


def _evidence(
    store: RunStore,
    requirement: ReferenceVerificationRequirement,
    commit: str,
    *,
    outcome: VerificationOutcome = VerificationOutcome.PASSED,
    source_state: ReferenceSourceState = ReferenceSourceState.CLEAN,
) -> ReferenceVerificationEvidence:
    manifest = store.load().plan_manifest
    assert manifest is not None
    return ReferenceVerificationEvidence.create(
        run_id=store.run_id,
        scope_id=manifest.scope_id,
        path=requirement.path,
        remote_url=requirement.remote_url,
        commit_sha=commit,
        source_state=source_state,
        kind=requirement.kind,
        command=requirement.command,
        outcome=outcome,
        provenance=VerificationProvenance.controller("verification-invocation"),
        recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
    )


def _store_for_plan(plan: WorkPlan, root: Path, state_root: Path, run_id: str) -> RunStore:
    config = TLLoopConfig(
        active=True,
        project_root=root,
        worktree=root,
        repository_identity=REPOSITORY,
        root_dir=state_root,
        run_id=run_id,
    )
    manifest = driver_module._manifest_for_plan(plan, run_id, config)
    slices = driver_module._initial_slices(plan, config, state_root, run_id)
    create(
        run_id,
        {
            "repository_identity": REPOSITORY.to_document(),
            "slices": slices,
            "plan_manifest": manifest.to_document(),
        },
        root_dir=state_root,
    )
    return RunStore(run_id, state_root)


def _dispatch_once(
    plan: WorkPlan, store: RunStore, root: Path, effects: RecordingEffects
) -> RunState:
    config = TLLoopConfig(
        active=True,
        project_root=root,
        worktree=root,
        repository_identity=REPOSITORY,
        root_dir=store.root_dir,
        run_id=store.run_id,
    )
    return _dispatch_children(
        plan,
        store.load(),
        config,
        EffectClient(effects),
        [],
        store,
    )


def test_production_dispatch_rechecks_required_reference_evidence_without_running_commands(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    commit = _workspace_with_reference(workspace)
    plan = _plan()
    store = _store_for_plan(plan, workspace, tmp_path / "state", "reference-required")
    effects = RecordingEffects()

    missing = _dispatch_once(plan, store, workspace, effects)
    assert missing.slices["leaf"].status.value == "pending"
    assert not [call for call in effects.calls if call[0] == "spawn_leaf"]

    evidence = ReferenceVerificationEvidence.create(
        run_id=store.run_id,
        scope_id=store.load().plan_manifest.scope_id,  # type: ignore[union-attr]
        path="tools/llmlift",
        remote_url=REMOTE,
        commit_sha=commit,
        source_state=ReferenceSourceState.CLEAN,
        kind=VerificationKind.TEST,
        command=DECLARED_COMMAND,
        outcome=VerificationOutcome.PASSED,
        provenance=VerificationProvenance.controller("verification-invocation"),
        recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    assert store.record_reference_verification(evidence)

    reference_source = workspace / "tools" / "llmlift" / "source.txt"
    reference_source.write_text("dirty after verification\n", encoding="utf-8")
    dirty = _dispatch_once(plan, store, workspace, effects)
    assert dirty.slices["leaf"].status.value == "pending"
    assert not [call for call in effects.calls if call[0] == "spawn_leaf"]

    reference_source.write_text("reference revision two\n", encoding="utf-8")
    _git(workspace / "tools" / "llmlift", "add", "source.txt")
    _git(workspace / "tools" / "llmlift", "commit", "-m", "advance reference")
    advanced_commit = _git(workspace / "tools" / "llmlift", "rev-parse", "HEAD")
    stale_pointer = _dispatch_once(plan, store, workspace, effects)
    assert stale_pointer.slices["leaf"].status.value == "pending"
    assert not [call for call in effects.calls if call[0] == "spawn_leaf"]

    _git(
        workspace,
        "update-index",
        "--cacheinfo",
        f"160000,{advanced_commit},tools/llmlift",
    )
    _git(workspace, "commit", "-m", "accept reference revision")
    stale_evidence = _dispatch_once(plan, store, workspace, effects)
    assert stale_evidence.slices["leaf"].status.value == "pending"
    assert not [call for call in effects.calls if call[0] == "spawn_leaf"]

    assert store.record_reference_verification(
        ReferenceVerificationEvidence.create(
            run_id=store.run_id,
            scope_id=store.load().plan_manifest.scope_id,  # type: ignore[union-attr]
            path="tools/llmlift",
            remote_url=REMOTE,
            commit_sha=advanced_commit,
            source_state=ReferenceSourceState.CLEAN,
            kind=VerificationKind.TEST,
            command=DECLARED_COMMAND,
            outcome=VerificationOutcome.PASSED,
            provenance=VerificationProvenance.controller("verification-invocation-2"),
            recorded_at=datetime(2026, 11, 2, 0, 1, tzinfo=UTC),
        )
    )
    ready = _dispatch_once(plan, store, workspace, effects)
    assert ready.slices["leaf"].status.value == "dispatch_unconfirmed"
    assert len([call for call in effects.calls if call[0] == "spawn_leaf"]) == 1
    assert not (workspace / "tools" / "llmlift" / "MUST_NOT_BE_EXECUTED").exists()

    manifest = store.load().plan_manifest
    assert manifest is not None
    accepted = driver_module._work_plan_from_manifest(manifest)
    assert accepted == plan


def test_optional_reference_verification_does_not_gate_unrelated_work(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _workspace_with_reference(workspace)
    plan = _plan(required=False)
    store = _store_for_plan(plan, workspace, tmp_path / "state", "reference-optional")
    effects = RecordingEffects()

    result = _dispatch_once(plan, store, workspace, effects)

    assert result.slices["leaf"].status.value == "dispatch_unconfirmed"
    assert len([call for call in effects.calls if call[0] == "spawn_leaf"]) == 1


def test_required_build_and_test_share_one_reference_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    commit = _workspace_with_reference(workspace)
    requirements = (
        ReferenceVerificationRequirement(
            "tools/llmlift", REMOTE, VerificationKind.BUILD, "cargo build", True
        ),
        ReferenceVerificationRequirement(
            "tools/llmlift", REMOTE, VerificationKind.TEST, "cargo test", True
        ),
    )
    plan = _plan_for_requirements(requirements)
    store = _store_for_plan(plan, workspace, tmp_path / "state", "reference-build-test")
    for requirement in requirements:
        assert store.record_reference_verification(_evidence(store, requirement, commit))

    effects = RecordingEffects()
    result = _dispatch_once(plan, store, workspace, effects)

    assert result.slices["leaf"].status.value == "dispatch_unconfirmed"
    assert len([call for call in effects.calls if call[0] == "spawn_leaf"]) == 1


@pytest.mark.parametrize(
    "scenario", ("missing_one", "failed", "stale", "dirty", "conflicting_identity")
)
def test_required_build_and_test_need_independent_current_evidence(
    tmp_path: Path,
    scenario: str,
) -> None:
    workspace = tmp_path / "workspace"
    commit = _workspace_with_reference(workspace)
    test_remote = "https://forge.example.invalid/org/other-llmlift.git"
    requirements = (
        ReferenceVerificationRequirement(
            "tools/llmlift", REMOTE, VerificationKind.BUILD, "cargo build", True
        ),
        ReferenceVerificationRequirement(
            "tools/llmlift",
            test_remote if scenario == "conflicting_identity" else REMOTE,
            VerificationKind.TEST,
            "cargo test",
            True,
        ),
    )
    plan = _plan_for_requirements(requirements)
    store = _store_for_plan(plan, workspace, tmp_path / "state", f"reference-{scenario}")
    assert store.record_reference_verification(_evidence(store, requirements[0], commit))
    if scenario != "missing_one":
        test_commit = "0" * 40 if scenario == "stale" else commit
        test_outcome = (
            VerificationOutcome.FAILED if scenario == "failed" else VerificationOutcome.PASSED
        )
        assert store.record_reference_verification(
            _evidence(
                store,
                requirements[1],
                test_commit,
                outcome=test_outcome,
            )
        )
    if scenario == "dirty":
        source = workspace / "tools" / "llmlift" / "source.txt"
        source.write_text("modified after verification\n", encoding="utf-8")

    effects = RecordingEffects()
    result = _dispatch_once(plan, store, workspace, effects)

    assert result.slices["leaf"].status.value == "pending"
    assert not [call for call in effects.calls if call[0] == "spawn_leaf"]


def test_reference_observation_does_not_refresh_project_index(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _workspace_with_reference(workspace)
    requirement = ReferenceVerificationRequirement(
        "tools/llmlift", REMOTE, VerificationKind.TEST, DECLARED_COMMAND, True
    )
    project = workspace / "tools" / "llmlift"
    index = project / ".git" / "index"
    indexed_before = index.read_bytes()
    source = project / "source.txt"
    stat = source.stat()
    os.utime(source, (stat.st_atime, stat.st_mtime + 10))

    observation = observe_reference_project(workspace, requirement)

    assert observation.source_state is ReferenceSourceState.CLEAN
    assert index.read_bytes() == indexed_before


def test_required_reference_verification_holds_ordered_subtl_before_handoff_or_provision(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _workspace_with_reference(workspace)
    requirement = ReferenceVerificationRequirement(
        "tools/llmlift", REMOTE, VerificationKind.BUILD, "cargo build --release", True
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                name="reference-consumer",
                plan=WorkPlan(),
                order=1,
                reference_verification_requirements=(requirement,),
            ),
        )
    )
    cancel = threading.Event()
    cancel.set()
    effects = RecordingEffects()

    class EmptySource:
        def get(self, timeout: float | None = None) -> EventEnvelope:
            del timeout
            raise queue.Empty

        def acknowledge(self, event: EventEnvelope) -> int:
            raise AssertionError(f"unexpected event {event!r}")

        def capture_snapshot(self, cursor: int) -> LedgerSnapshot:
            del cursor
            return LedgerSnapshot((), 0, SequenceStatus.COMPLETE, ())

    state_root = tmp_path / "state"
    with pytest.raises(LoopCancelled):
        run_tl_loop(
            "reference-subtl",
            plan,
            EmptySource(),
            EffectClient(effects),
            config=TLLoopConfig(
                active=True,
                project_root=workspace,
                worktree=workspace,
                repository_identity=REPOSITORY,
                root_dir=state_root,
                run_id="reference-subtl",
                cancel_event=cancel,
                poll_interval=0.001,
            ),
            root_dir=state_root,
        )

    final_state = RunStore("reference-subtl", state_root).load()
    operations = [name for name, _ in effects.calls]
    assert final_state.slices["reference-consumer"].status.value == "pending"
    assert "prepare_scope_handoff" not in operations
    assert "inspect_scope_handoff" not in operations
    assert "provision_ordered_sub_tl" not in operations
    assert "spawn_leaf" not in operations
