#!/usr/bin/env python3
"""Run the M3 read-only shadow loop beside a live interactive TL."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader
from tl_loop.loop.shadow import ShadowLoop, ShadowLoopError
from tl_loop.shadow.actual import ActualActionReader
from tl_loop.shadow.diff import generate_report
from tl_loop.state.store import create

RELEVANT_EVENT_TYPES = frozenset(
    {
        "agent.spawned",
        "agent.completed",
        "agent.notify_parent",
        "agent.stuck",
        "pr.merged",
    }
)
CONCRETE_SPAWN_TYPES = frozenset({"worker", "subtree", "leaf_subtree"})


class LifecycleSource:
    """Project only lifecycle events while acknowledging other mapped rows."""

    def __init__(self, queue: LedgerQueue) -> None:
        self.queue = queue

    def get(self, timeout: float | None = None):
        while True:
            event = self.queue.get(timeout=timeout)
            if event.event_type == "agent.spawned" and not _is_concrete_spawn(event):
                self.queue.acknowledge(event)
                continue
            if event.event_type in RELEVANT_EVENT_TYPES:
                return event
            self.queue.acknowledge(event)

    def acknowledge(self, event) -> int:
        return self.queue.acknowledge(event)


def _is_concrete_spawn(event: EventEnvelope) -> bool:
    """Keep lifecycle rows separate from spawn-request telemetry rows."""
    if event.data.get("spawn_type") not in CONCRETE_SPAWN_TYPES:
        return False
    return all(
        isinstance(event.data.get(key), str) and bool(event.data[key])
        for key in ("child_agent", "branch", "agent_type")
    )


def main() -> int:
    arguments = _arguments()
    repo = arguments.repo.resolve()
    artifacts = arguments.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    run_id = _wait_for_run_id(repo / ".exo" / "run_id", arguments.timeout)
    shadow_root = repo / ".exo" / "tl-loop" / "shadow"
    segments = repo / ".exo" / "ledger" / "segments"
    checkpoint = shadow_root / run_id / "run.json"
    if not checkpoint.exists():
        create(run_id, {}, root_dir=shadow_root)
    source_queue = LedgerQueue(
        LedgerReader(segments, run_id=run_id, state_root=shadow_root),
        maxsize=64,
        poll_interval=0.2,
    ).start()
    source = LifecycleSource(source_queue)
    readonly = ReadOnlyEffectClient(
        EffectClient(TransportClient(project_root=repo, timeout=2), role="tl", name="root")
    )
    shadow = ShadowLoop.for_run(
        source,
        run_id,
        readonly_client=readonly,
        root_dir=shadow_root,
    )
    shadow_error: str | None = None
    deadline = time.monotonic() + arguments.timeout
    last_sequence = -1
    last_activity = time.monotonic()
    try:
        while time.monotonic() < deadline:
            if shadow_error is None:
                try:
                    result = shadow.run(timeout=1, max_events=1)
                    if result.actions:
                        print(
                            f"[shadow] consumed event_seq={result.actions[-1].event_seq}",
                            flush=True,
                        )
                except ShadowLoopError as error:
                    shadow_error = str(error)
                    print(f"[shadow] trajectory divergence: {shadow_error}", flush=True)
            current_sequence, merged = _ledger_progress(segments, run_id)
            if current_sequence != last_sequence:
                last_sequence = current_sequence
                last_activity = time.monotonic()
            if merged >= 2 and time.monotonic() - last_activity >= arguments.quiet_seconds:
                break
            time.sleep(0.2)
        else:
            shadow_error = shadow_error or "live trajectory timed out"
    finally:
        source_queue.close(timeout=2)

    _archive(run_id, shadow_root, segments, artifacts, shadow_error)
    subprocess.run(
        ["tmux", "kill-session", "-t", arguments.session],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


def _archive(
    run_id: str,
    shadow_root: Path,
    segments: Path,
    artifacts: Path,
    shadow_error: str | None,
) -> None:
    intended_source = shadow_root / run_id / "intended.jsonl"
    intended_target = artifacts / "intended.jsonl"
    if intended_source.exists():
        shutil.copyfile(intended_source, intended_target)
    else:
        intended_target.write_text("", encoding="utf-8")

    actual_actions = ActualActionReader(segments).read(run_id)
    actual_target = artifacts / "actual.jsonl"
    with actual_target.open("w", encoding="utf-8") as stream:
        for action in actual_actions:
            json.dump(
                {
                    "kind": action.kind,
                    "target": action.target,
                    "arguments": dict(action.arguments),
                    "event_seq": action.event_seq,
                    "rationale": action.rationale,
                    "agent_id": action.agent_id,
                },
                stream,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")

    report = generate_report(
        run_id,
        shadow_root=shadow_root,
        segments_dir=segments,
        docs_dir=artifacts / "docs" / "observability",
    )
    (artifacts / "report.path").write_text(str(report), encoding="utf-8")
    metadata = {
        "run_id": run_id,
        "shadow_error": shadow_error,
        "actual_actions": len(actual_actions),
        "shadow_mutation_calls": 0,
        "source": "immutable ledger segments",
    }
    (artifacts / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _ledger_progress(segments: Path, run_id: str) -> tuple[int, int]:
    highest = -1
    merged = 0
    for segment in sorted(segments.glob("segment-*.jsonl")):
        for line in _lines(segment):
            if line.get("run_id") != run_id:
                continue
            if line.get("type") not in RELEVANT_EVENT_TYPES:
                continue
            sequence = line.get("run_seq")
            if type(sequence) is int:
                highest = max(highest, sequence)
            if line.get("type") == "pr.merged":
                merged += 1
    return highest, merged


def _lines(path: Path) -> Iterable[dict[str, object]]:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    return (value for value in (_decode(line) for line in rows) if value is not None)


def _decode(line: str) -> dict[str, object] | None:
    if not line.strip():
        return None
    value = json.loads(line)
    return value if isinstance(value, dict) else None


def _wait_for_run_id(path: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            run_id = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            run_id = ""
        if run_id and Path(run_id).name == run_id:
            return run_id
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {path}")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("session")
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--timeout", type=float, default=480)
    parser.add_argument("--quiet-seconds", type=float, default=10)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
