"""Deterministic replay harness for recorded TL event streams."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.loop.driver import TLLoopConfig, TLLoopError, tl_run
from tl_loop.loop.escalate import park
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import load_policy
from tl_loop.shadow.diff import normalize_arguments
from tl_loop.state.schema import ParkCause
from tl_loop.state.store import RunStore

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "replay"
POLICY_ROOT = Path(__file__).parent / "fixtures"
REPLAY_REVIEW_NOW = datetime(2026, 8, 11, 17, 20, tzinfo=UTC)


@dataclass
class RecordedEventSource:
    """FIFO source backed only by a committed JSON event stream."""

    events: list[EventEnvelope]
    acknowledged: list[int] = field(default_factory=list)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        if not self.events:
            raise TypeError("recorded replay stream ended before TL completion")
        return self.events.pop(0)

    def acknowledge(self, event: EventEnvelope) -> int:
        if event.run_seq is None:
            raise TypeError("recorded event has no run_seq")
        self.acknowledged.append(event.run_seq)
        return event.run_seq


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


def replay_fixture(fixture: str | Path, root_dir: str | Path) -> ReplayResult:
    """Run one committed event stream and return normalized observable output."""
    spec = _load_fixture(fixture)
    run_id = _string(spec.get("run_id"), "run_id")
    plan = _mapping(spec["plan"], "plan")
    events = _events(spec["events"])
    transport = RecordingTransport(
        current_head=_string(spec.get("current_head", "head-a"), "current_head")
    )
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
        max_events=32,
        poll_interval=0.001,
        review_policy_path=FIXTURE_ROOT / "review-policy.toml",
        review_clock=lambda: REPLAY_REVIEW_NOW,
    )
    try:
        tl_run({"run_id": run_id, "plan": plan}, config, {"tokens": 0, "wall_seconds": 0})
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
    return ReplayResult(actions, normalize_state(cast(dict[str, object], document)))


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


def _normalize_value(key: str, value: object) -> object:
    if isinstance(value, Mapping):
        return {
            name: _normalize_value(name, item)
            for name, item in sorted(value.items())
            if name not in {"plan_manifest", "manifest_node_id", "manifest_revision"}
            and not (key == "fsm" and name in {"kind", "payload"})
        }
    if isinstance(value, list):
        return [_normalize_value(key, item) for item in value]
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
    "normalize_state",
    "replay_fixture",
]
