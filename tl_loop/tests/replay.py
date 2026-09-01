"""Deterministic replay harness for recorded TL event streams."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader
from tl_loop.events.replay import ReplayEventSource
from tl_loop.loop.driver import SubTLTask, TLLoopConfig, TLLoopError, WorkPlan, tl_run
from tl_loop.loop.escalate import park
from tl_loop.loop.journal import EffectJournal
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import load_policy
from tl_loop.shadow.diff import normalize_arguments
from tl_loop.state.schema import ParkCause, RepositoryIdentity
from tl_loop.state.store import RunStore

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "replay"
POLICY_ROOT = Path(__file__).parent / "fixtures"
REPLAY_REVIEW_NOW = datetime(2026, 8, 11, 17, 20, tzinfo=UTC)


RecordedEventSource = ReplayEventSource


@dataclass
class RecordingTransport:
    """Effect transport double; no FSM, selector, or store behavior is substituted."""

    current_head: str = "head-a"
    issue_id: int = 900
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, copy.deepcopy(arguments)))
        if tool_name == "watcher_pr_state":
            return {
                "success": True,
                "result": {
                    "found": True,
                    "head_sha": self.current_head,
                },
            }
        if tool_name == "chainlink_issue_create":
            return {
                "success": True,
                "result": {"issue_id": self.issue_id},
            }
        if tool_name == "chainlink_issue_close":
            issue_id = arguments.get("issue_id")
            return {
                "success": True,
                "result": {"issue_id": issue_id, "receipt_id": f"issue-close:{issue_id}"},
            }
        if tool_name == "merge_pr":
            return {
                "success": True,
                "result": {"merged": True, "pr_number": arguments.get("pr_number")},
            }
        if tool_name == "post_merge_parent_sync":
            merged_head = arguments.get("merged_head_sha")
            parent_commit = "parent-after-merge"
            return {
                "success": True,
                "result": {
                    **arguments,
                    "repository": "org/repo",
                    "parent_commit_sha": parent_commit,
                    "remote_head_sha": parent_commit,
                    "ancestry_proof": f"ancestor:{merged_head}->{parent_commit}",
                },
            }
        if tool_name == "post_merge_issue_close":
            return {
                "success": True,
                "result": {
                    **arguments,
                    "receipt_id": f"issue-close:{arguments.get('issue_id')}",
                },
            }
        if tool_name == "post_merge_changelog":
            return {
                "success": True,
                "result": {**arguments, "commit_sha": "changelog-commit"},
            }
        if tool_name == "post_merge_push":
            pushed = arguments.get("pushed_commit")
            return {
                "success": True,
                "result": {
                    **arguments,
                    "push_receipt_id": "push-receipt",
                    "observed_remote_head": pushed,
                    "ancestry_proof": f"ancestor:{pushed}->{pushed}",
                },
            }
        if tool_name == "root_branch_finalize":
            branch = arguments.get("branch")
            head = "root-head"
            return {
                "success": True,
                "result": {
                    "branch": branch,
                    "local_head_sha": head,
                    "remote_head_sha": head,
                    "ancestry_proof": f"ancestor:{head}->{head}",
                    "fast_forward": True,
                },
            }
        if tool_name == "resume_pr":
            return {"success": True, "result": {"resumed": True}}
        return {"success": True, "result": None}


@dataclass(frozen=True)
class ReplayJudgments:
    """Deterministic terminal decisions used by the parking scenario."""

    park_cause: ParkCause = ParkCause.RETRIES_EXHAUSTED

    def cause_for_terminal_error(self, error: TLLoopError) -> ParkCause:
        del error
        return self.park_cause


@dataclass(frozen=True)
class ReplayResult:
    """Normalized ordered effects and final durable state from one stream."""

    actions: tuple[dict[str, object], ...]
    state: dict[str, object]
    durable_state: dict[str, object] = field(default_factory=dict)
    cursor: int = 0
    reducer_version: int = 1
    transitions: tuple[dict[str, object], ...] = ()
    journal_entries: tuple[Mapping[str, object], ...] = ()
    acknowledged: tuple[int, ...] = ()


def replay_fixture(
    fixture: str | Path,
    root_dir: str | Path,
    *,
    event_transform: Callable[[list[EventEnvelope]], Sequence[EventEnvelope]] | None = None,
    max_events: int = 32,
    journal: bool = False,
    production_clock: bool = False,
    session_mode: str | None = None,
    crash_after: str | None = None,
    live_ledger: bool = False,
) -> ReplayResult:
    """Run one committed event stream and return normalized observable output."""
    spec = _load_fixture(fixture)
    run_id = _string(spec.get("run_id"), "run_id")
    plan = _plan_with_replay_sources(
        _mapping(spec["plan"], "plan"),
        spec.get("child_events"),
    )
    events = _events(spec["events"])
    if event_transform is not None:
        events = list(event_transform(events))
    transport = RecordingTransport(
        current_head=_string(spec.get("current_head", "head-a"), "current_head")
    )
    queues: list[LedgerQueue] = []
    if live_ledger:
        segments = Path(root_dir) / ".exo" / "ledger" / "segments"
        _write_ledger_segment(segments, cast(list[Mapping[str, object]], spec["events"]))
        source = LedgerQueue(
            LedgerReader(
                segments,
                run_id=run_id,
                state_root=root_dir,
                ledger_run_id=run_id,
            ),
            poll_interval=0.001,
        ).start()
        queues.append(source)
        plan = _attach_live_child_sources(
            plan,
            spec.get("child_events"),
            Path(root_dir) / run_id,
            Path(root_dir) / ".exo" / "child-ledger",
            queues,
        )
    else:
        source = RecordedEventSource(events)
    policy = load_policy(POLICY_ROOT / _string(spec["policy"], "policy"))
    capability = CapabilityMap({"codex/gpt-luna": Difficulty.STANDARD})
    effects = EffectClient(transport)
    active = spec.get("active", True) is True
    client = effects if active else ReadOnlyEffectClient(effects)
    config = TLLoopConfig(
        active=active,
        source=source,
        effects=client,
        root_dir=root_dir,
        run_id=run_id,
        policy=policy,
        capabilities=capability,
        max_workers=2,
        max_leaves=1,
        max_events=max_events,
        poll_interval=0.001,
        review_policy_path=FIXTURE_ROOT / "review-policy.toml",
        review_clock=None if production_clock else lambda: REPLAY_REVIEW_NOW,
        ledger_run_id=run_id if journal else None,
        chainlink_issue_id=_optional_positive_int(spec.get("chainlink_issue_id")),
        repository_identity=_repository_identity(spec.get("repository_identity")),
        session_mode=session_mode,
    )
    run_result = None
    original_checkpoint = RunStore.checkpoint
    crash_journal = (
        EffectJournal(run_id, Path(root_dir) / run_id / "action-journal.json")
        if crash_after is not None
        else None
    )
    crashed = False

    def crash_checkpoint(store: RunStore, *args: object, **kwargs: object) -> object:
        nonlocal crashed
        if (
            crash_journal is not None
            and not crashed
            and store.run_id == run_id
            and any(
                entry.get("operation") == crash_after and entry.get("status") == "confirmed"
                for entry in crash_journal.snapshot()
            )
        ):
            crashed = True
            raise RuntimeError(f"simulated process death after {crash_after}")
        return original_checkpoint(store, *args, **kwargs)

    if crash_after is not None:
        RunStore.checkpoint = crash_checkpoint
    try:
        try:
            run_result = tl_run(
                {"run_id": run_id, "plan": plan},
                config,
                {"tokens": 0, "wall_seconds": 0},
            )
        except TLLoopError as error:
            if not _bool(spec.get("expect_terminal_park"), "expect_terminal_park"):
                raise
            store = RunStore(run_id, root_dir=Path(root_dir))
            state = store.load()
            target_id = _string(spec["park_slice"], "park_slice")
            target = state.slices.get(target_id)
            if target is None:
                raise TypeError(f"terminal replay slice {target_id!r} is missing")
            judgment = ReplayJudgments()
            park(
                target,
                judgment.cause_for_terminal_error(error),
                store=store,
                issue_creator=effects,
                ledger=state.budgets,
            )
    finally:
        if crash_after is not None:
            RunStore.checkpoint = original_checkpoint
        for queue in queues:
            queue.close(timeout=2)
    if crash_after is not None and not crashed:
        raise AssertionError(f"replay never reached crash boundary {crash_after!r}")
    store = RunStore(run_id, root_dir=Path(root_dir))
    state_path = store.path
    document = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"replay state {state_path} is not an object")
    actions = tuple(
        cast(
            dict[str, object],
            {
                "operation": tool_name,
                "arguments": normalize_arguments(arguments),
            },
        )
        for tool_name, arguments in transport.calls
    )
    normalized = normalize_state(cast(dict[str, object], document))
    durable = normalize_durable_state(cast(dict[str, object], document))
    if run_result is None:
        events_document = cast(dict[str, object], document["events"])
        return ReplayResult(
            actions,
            normalized,
            durable_state=durable,
            cursor=cast(int, events_document["last_consumed_offset"]),
            acknowledged=tuple(getattr(source, "acknowledged", ())),
        )
    transitions = tuple(
        {
            "event_seq": transition.event_seq,
            "event_type": transition.event_type,
            "before": transition.before.value,
            "after": transition.after.value,
        }
        for transition in run_result.transitions
    )
    return ReplayResult(
        actions,
        normalized,
        durable_state=durable,
        cursor=run_result.cursor,
        reducer_version=run_result.reducer_version,
        transitions=transitions,
        journal_entries=run_result.journal_entries,
        acknowledged=tuple(getattr(source, "acknowledged", ())),
    )


def expected_actions(fixture: str | Path) -> tuple[dict[str, object], ...]:
    """Read the exact normalized action sequence committed for a stream."""
    spec = _load_fixture(fixture)
    raw = spec.get("expected_actions")
    if not isinstance(raw, list):
        raise TypeError("fixture.expected_actions must be an array")
    return tuple(cast(dict[str, object], item) for item in raw)


def expected_state(fixture: str | Path) -> dict[str, object]:
    """Read the normalized golden state committed for a stream."""
    spec = _load_fixture(fixture)
    golden = spec.get("golden")
    if not isinstance(golden, str):
        raise TypeError("fixture.golden must be a string")
    value = json.loads((FIXTURE_ROOT / golden).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("golden state must be an object")
    return cast(dict[str, object], value)


def normalize_state(document: Mapping[str, object]) -> dict[str, object]:
    """Normalize only run IDs and timestamp fields for byte-stable comparison."""
    return cast(dict[str, object], _normalize_value("", document))


def normalize_durable_state(document: Mapping[str, object]) -> dict[str, object]:
    """Normalize volatile values while retaining every durable replay field."""
    return cast(dict[str, object], _normalize_value("", document, strip_runtime=False))


def _normalize_value(key: str, value: object, *, strip_runtime: bool = True) -> object:
    if isinstance(value, Mapping):
        return {
            name: _normalize_value(name, item, strip_runtime=strip_runtime)
            for name, item in sorted(value.items())
            if not strip_runtime
            or (
                name
                not in {
                    "plan_manifest",
                    "manifest_node_id",
                    "manifest_revision",
                    "reducer_version",
                }
                and not (key == "fsm" and name in {"kind", "payload"})
            )
        }
    if isinstance(value, list):
        return [_normalize_value(key, item, strip_runtime=strip_runtime) for item in value]
    if key in {"dispatch_intent_id", "intent_id"}:
        return "<intent-id>"
    if key == "dispatch_started_at":
        return "<timestamp>"
    if key in {"controller_started_at", "last_progress_at"} and isinstance(value, (int, float)):
        return "<timestamp>"
    if key == "run_id":
        return "<run-id>"
    if key.endswith("_at") and isinstance(value, str):
        return "<timestamp>"
    return value


def _load_fixture(fixture: str | Path) -> dict[str, object]:
    path = Path(fixture)
    if not path.is_absolute():
        path = FIXTURE_ROOT / path
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"replay fixture {path} must be an object")
    return cast(dict[str, object], value)


def _events(value: object) -> list[EventEnvelope]:
    if not isinstance(value, list):
        raise TypeError("fixture.events must be an array")
    return [project(cast(dict[str, object], item)) for item in value]


def _plan_with_replay_sources(value: Mapping[str, object], child_events: object) -> WorkPlan:
    """Attach independently replayable event streams to recursive child scopes."""
    plan = WorkPlan.from_mapping(value)
    if child_events is None:
        return plan
    if not isinstance(child_events, Mapping):
        raise TypeError("fixture.child_events must be an object")

    def bind(current: WorkPlan) -> WorkPlan:
        children: list[SubTLTask] = []
        for task in current.sub_tls:
            child_plan = task.plan
            if isinstance(child_plan, Mapping):
                child_plan = WorkPlan.from_mapping(child_plan)
            if not isinstance(child_plan, WorkPlan):
                raise TypeError(f"sub-TL {task.name!r} has no WorkPlan")
            source_value = child_events.get(task.name)
            source = task.source
            if source_value is not None:
                source = RecordedEventSource(_events(source_value))
            children.append(replace(task, plan=bind(child_plan), source=source))
        return replace(current, sub_tls=tuple(children))

    return bind(plan)


def _write_ledger_segment(segments: Path, rows: list[Mapping[str, object]]) -> None:
    segments.mkdir(parents=True, exist_ok=True)
    segment = segments / "segment-000000000001.jsonl"
    segment.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _attach_live_child_sources(
    plan: WorkPlan,
    child_events: object,
    parent_state_dir: Path,
    ledger_root: Path,
    queues: list[LedgerQueue],
) -> WorkPlan:
    """Give every recursive child an independent live or empty source."""
    event_map = child_events if isinstance(child_events, Mapping) else {}
    children: list[SubTLTask] = []
    for task in plan.sub_tls:
        child_plan = task.plan
        if not isinstance(child_plan, WorkPlan):
            raise TypeError(f"sub-TL {task.name!r} has no WorkPlan")
        raw_events = event_map.get(task.name)
        child_state_dir = parent_state_dir / task.name
        child_ledger_dir = ledger_root / task.name
        if isinstance(raw_events, list):
            _write_ledger_segment(child_ledger_dir, cast(list[Mapping[str, object]], raw_events))
            source = LedgerQueue(
                LedgerReader(child_ledger_dir, run_dir=child_state_dir),
                poll_interval=0.001,
            ).start()
            queues.append(source)
        else:
            source = ReplayEventSource([])
        nested_events = raw_events if isinstance(raw_events, Mapping) else None
        children.append(
            replace(
                task,
                source=source,
                plan=_attach_live_child_sources(
                    child_plan,
                    nested_events,
                    child_state_dir,
                    child_ledger_dir,
                    queues,
                ),
            )
        )
    return replace(plan, sub_tls=tuple(children))


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise TypeError("fixture.chainlink_issue_id must be a positive integer or null")
    return value


def _repository_identity(value: object) -> RepositoryIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("fixture.repository_identity must be an object or null")
    fields = {name: value.get(name) for name in ("owner", "repo", "base_branch")}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        raise TypeError("fixture.repository_identity requires owner, repo, and base_branch")
    return RepositoryIdentity(
        fields["owner"],
        fields["repo"],
        fields["base_branch"],
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


__all__ = [
    "FIXTURE_ROOT",
    "RecordedEventSource",
    "RecordingTransport",
    "ReplayJudgments",
    "ReplayResult",
    "expected_actions",
    "expected_state",
    "normalize_durable_state",
    "normalize_state",
    "replay_fixture",
]
