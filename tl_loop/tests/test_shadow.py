"""End-to-end synthetic coverage for the read-only shadow loop."""

from __future__ import annotations

import hashlib
import json
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.fsm.event import ChildBlocked, ChildSpawned, PRFiled
from tl_loop.fsm.phase import ChildHandle
from tl_loop.loop.shadow import ShadowLoop, TLEventDecoder, _update_slices
from tl_loop.state.schema import SliceState, SliceStatus
from tl_loop.state.store import create


@dataclass
class SyntheticQueue:
    events: list[EventEnvelope]
    acknowledged: list[int] = field(default_factory=list)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        if not self.events:
            raise queue.Empty
        return self.events.pop(0)

    def acknowledge(self, event: EventEnvelope) -> int:
        assert event.run_seq is not None
        self.acknowledged.append(event.run_seq)
        return event.run_seq


def test_shadow_decoder_accepts_canonical_spawn_payload() -> None:
    raw = {
        "schema_version": 1,
        "event_id": "spawn-event",
        "id": "spawn-event",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": 1,
        "type": "agent.spawned",
        "agent_id": "root",
        "run_id": "run-1",
        "session_id": "session-1",
        "lifecycle_state": "emitted",
        "data": {
            "child_agent": "child-a",
            "agent_type": "codex",
            "branch": "main.child-a",
            "intent_id": _dispatch_intent("run-1", "child-a"),
        },
    }

    decoded = TLEventDecoder().decode(project(raw))

    assert decoded == ChildSpawned(ChildHandle("child-a", "main.child-a", "codex"))


def test_shadow_decoder_accepts_typed_task_blocked_payload() -> None:
    raw = {
        "schema_version": 1,
        "event_id": "blocked-event",
        "id": "blocked-event",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": 2,
        "type": "agent.task_blocked",
        "agent_id": "worker-a",
        "run_id": "run-1",
        "session_id": "session-1",
        "invocation_id": "inv-1",
        "generation": 1,
        "harness": "codex",
        "role": "worker",
        "lifecycle_state": "authoritative",
        "data": {
            "outcome": "blocked",
            "slice_id": "slice-a",
            "cause": "base_ci_unstable",
            "scope_attribution": "base",
            "needs_human": True,
            "retryable": True,
            "recovery_action": "repair base CI",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "standard_slice",
            "attempt": 2,
        },
    }

    decoded = TLEventDecoder().decode(project(raw))

    assert decoded == ChildBlocked("slice-a", "base_ci_unstable", True, "repair base CI", 2)


def test_shadow_recovery_stays_nonterminal_until_pr_is_filed() -> None:
    current = SliceState(
        id="slice-a",
        status=SliceStatus.SPAWNED,
        paths=("src",),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type="codex",
        model="gpt-5",
        branch="task/slice-a",
        worktree=".worktrees/slice-a",
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
    )

    recovering = _update_slices(
        {"slice-a": current},
        ChildBlocked("slice-a", "base_ci_unstable", True, "repair base CI", 1),
        run_id="run-1",
    )["slice-a"]
    assert recovering.status is SliceStatus.SPAWNED
    assert recovering.recovery is not None

    reviewed = _update_slices(
        {"slice-a": recovering},
        PRFiled(42, "head-1", "slice-a"),
    )["slice-a"]
    assert reviewed.status is SliceStatus.IN_REVIEW
    assert reviewed.recovery is None


def test_shadow_loop_reaches_terminal_phase_and_records_intended_sequence(tmp_path: Path) -> None:
    create(
        "synthetic-shadow",
        {
            "slices": {
                "child-a": {
                    "id": "child-a",
                    "status": "dispatch_unconfirmed",
                    "paths": ["src"],
                    "depends_on": [],
                    "base_ref": "main",
                    "test_plan": ["just test"],
                    "agent_type": "codex",
                    "model": None,
                    "branch": "main.child-a",
                    "worktree": "/tmp/child-a",
                    "pr_number": None,
                    "reviewed_head": None,
                    "attempts": 1,
                    "verdict": None,
                    "dispatch_intent_id": _dispatch_intent("synthetic-shadow", "child-a"),
                    "dispatch_started_at": 0.0,
                    "dispatch_last_boundary": "dispatch_intended",
                }
            }
        },
        root_dir=tmp_path / "shadow",
    )
    source = SyntheticQueue(
        [
            _event(1, "child_spawned"),
            _event(2, "child_completed"),
            _event(3, "all_children_done"),
        ]
    )

    readonly = ReadOnlyEffectClient(EffectClient(_RecordingTransport()))
    result = ShadowLoop.for_run(
        source,
        "synthetic-shadow",
        readonly_client=readonly,
        root_dir=tmp_path / "shadow",
    ).run()

    assert [action.kind for action in result.actions] == ["dispatch", "dispatch", "dispatch"]
    assert [action.event_seq for action in result.actions] == [1, 2, 3]
    assert [action.phase_before.value for action in result.actions] == [
        "tl_planning",
        "tl_waiting",
        "tl_all_merged",
    ]
    assert result.actions[-1].phase_after.value == "tl_done"
    assert result.final_state.fsm.phase.value == "tl_done"
    assert result.final_state.events.last_consumed_offset == 3
    assert source.acknowledged == [1, 2, 3]

    checkpoint = json.loads((tmp_path / "shadow" / "synthetic-shadow" / "run.json").read_text())
    assert checkpoint["events"]["last_consumed_offset"] == 3


def test_shadow_loop_accepts_two_distinct_live_spawns(tmp_path: Path) -> None:
    create(
        "two-spawns",
        {
            "slices": {
                slug: {
                    "id": slug,
                    "status": "dispatch_unconfirmed",
                    "paths": [f"shadow:{slug}"],
                    "depends_on": [],
                    "base_ref": "main",
                    "test_plan": ["just test"],
                    "agent_type": "codex",
                    "model": None,
                    "branch": f"main.{slug}",
                    "worktree": None,
                    "pr_number": None,
                    "reviewed_head": None,
                    "attempts": 1,
                    "verdict": None,
                    "dispatch_intent_id": _dispatch_intent("synthetic-shadow", slug),
                    "dispatch_started_at": 0.0,
                    "dispatch_last_boundary": "dispatch_intended",
                }
                for slug in ("child-a", "child-b")
            }
        },
        root_dir=tmp_path / "shadow",
    )
    source = SyntheticQueue(
        [
            _event(1, "child_spawned", "child-a"),
            _event(2, "child_spawned", "child-b"),
        ]
    )

    result = ShadowLoop.for_run(
        source,
        "two-spawns",
        readonly_client=ReadOnlyEffectClient(EffectClient(_RecordingTransport())),
        root_dir=tmp_path / "shadow",
    ).run()

    assert result.final_state.slices["child-a"].paths == ("shadow:child-a",)
    assert result.final_state.slices["child-b"].paths == ("shadow:child-b",)


@dataclass
class _RecordingTransport:
    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name, tool_name, arguments
        return {"success": True, "result": None}


def _event(run_seq: int, shadow_kind: str, slug: str = "child-a") -> EventEnvelope:
    raw = {
        "schema_version": 1,
        "event_id": f"event-{run_seq}",
        "id": f"event-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": "agent.notify_parent",
        "agent_id": "child-a",
        "run_id": "synthetic-shadow",
        "session_id": "session-1",
        "invocation_id": None,
        "generation": 1,
        "provider": "openai",
        "runtime": "codex",
        "harness": "codex",
        "role": "worker",
        "source": "synthetic",
        "lifecycle_state": "observed",
        "data": {
            "shadow_event": {
                "kind": shadow_kind,
                "slug": slug,
                "branch": "main.child-a",
                "agent_type": "codex",
                "intent_id": _dispatch_intent("synthetic-shadow", slug),
                "reason": "synthetic reason",
            }
        },
    }
    return project(cast(dict[str, object], raw))


def _dispatch_intent(run_id: str, slug: str) -> str:
    return hashlib.sha256(f"{run_id}:{slug}:1".encode()).hexdigest()[:32]
