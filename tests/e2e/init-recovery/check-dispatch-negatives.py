"""Compiled host negative controls for reauthorized blocked-leaf resumes.

Every case runs against the standalone `exomonad serve` through the production
`EffectClient`, with a disposable ledger that carries the same stable logical
resume intent as the acceptance fixture. A reauthorized declaration pair is
seeded for the case and removed again byte-for-byte, so a refusal cannot have
launched anything and the later real init still sees only the historical pair.

The cases pin: missing authorization, a fully populated foreign intent with a
durable declaration, conflicting duplicate rows, wrong
caller/scope/predecessor/worktree, a stale accepted configuration, a forged
route fingerprint, missing/unrelated/wrong-declaration/contradictory prelaunch
refusal proof, two distinct historical observations sharing one refusal,
missing/foreign/rejected durable approval, a rejected controller gate, an
unknown live outcome, a prior launched effect, and untouched independent
workers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

OWNER_SLICE = "issue-814-substitution-model-kani"
OWNER_AGENT = "issue-814-substitution-model-kani-opencode"
# A fully populated authorization for a different logical resume. It carries a
# durable declaration pair below so only the host-derived identity binding can
# distinguish it from the request that owns the fixture.
FOREIGN_INTENT = (
    "blocked-resume:0000000000000000000000000000000000000000000000000000000000000000"
)
V1_CONFIGURATION = (
    "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
)
V1_ROUTE = "sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724"
WORKER_MODEL = "opencode-go/deepseek-v4.1-flash"
GATE_NAME = "tl-terminal-owner-recovery-issue-814-substitution-model-kani"
EXACT_CONFIGURATION_REFUSAL = (
    "Invalid input: role route configuration is stale or was not accepted by this server"
)


def _fixture_helpers() -> Any:
    path = Path(__file__).with_name("configured-owner-resume.py")
    spec = importlib.util.spec_from_file_location("configured_owner_resume", path)
    if spec is None or spec.loader is None:
        raise SystemExit("the production ledger fixture helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(repository: Path, identity: str, run_id: str) -> Any:
    from tl_loop.__main__ import _authoritative_ledger_run_id
    from tl_loop.loop.driver import TLLoopConfig
    from tl_loop.select.capability import load_capability
    from tl_loop.select.model import load_model_catalog
    from tl_loop.select.policy import load_policy
    from tl_loop.state.store import RunStore

    root = repository / ".exo/tl-loop"
    state = RunStore(run_id, root / "root").load()
    policy_path = repository / ".exo/harness_policy.toml"
    return TLLoopConfig(
        active=True,
        root_dir=root,
        project_root=repository,
        run_id=run_id,
        ledger_run_id=_authoritative_ledger_run_id(repository),
        branch=state.owner_branch,
        role="worker",
        worktree=Path(state.owner_worktree) if state.owner_worktree else None,
        parent_run_id=state.parent_run_id,
        parent_branch=state.parent_branch,
        parent_agent_id=state.parent_agent_id,
        depth=state.depth,
        declared_manifest=state.plan_manifest,
        policy=load_policy(policy_path),
        capabilities=load_capability(
            repository / ".exo/harness_capability.toml", policy_path=policy_path
        ),
        catalog=load_model_catalog(repository / ".exo/model-catalog.json"),
        worker_effort_level="high",
        role_route_configuration_fingerprint=identity,
    )


class Snapshot:
    def __init__(self, repository: Path, owner_dir: Path, journal: Path) -> None:
        self.repository = repository
        self.owner_dir = owner_dir
        self.journal = journal
        self.segments = {
            path: path.read_bytes()
            for path in sorted((repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
        }
        self.run_seq = (repository / ".exo/ledger/run_seq").read_bytes()
        self.owner_files = {
            path: path.read_bytes() for path in sorted(owner_dir.glob("*.json"))
        }
        self.journal_bytes = journal.read_bytes()
        self.run_state_path = journal.parent / "run.json"
        self.run_state_bytes = self.run_state_path.read_bytes()

    def restore(self) -> None:
        segment_dir = self.repository / ".exo/ledger/segments"
        for path in sorted(segment_dir.glob("segment-*.jsonl")):
            if path not in self.segments:
                path.unlink()
        for path, payload in self.segments.items():
            path.write_bytes(payload)
        (self.repository / ".exo/ledger/run_seq").write_bytes(self.run_seq)
        for path in sorted(self.owner_dir.glob("*.json")):
            if path.name not in {item.name for item in self.owner_files}:
                path.unlink()
        for path, payload in self.owner_files.items():
            path.write_bytes(payload)
        self.journal.write_bytes(self.journal_bytes)
        self.run_state_path.write_bytes(self.run_state_bytes)


class Harness:
    def __init__(self, repository: Path, identity: str, run_id: str) -> None:
        from tl_loop.loop.driver import WorkPlan
        from tl_loop.state.store import RunStore

        self.repository = repository
        self.identity = identity
        self.run_id = run_id
        self.config = _config(repository, identity, run_id)
        self.store = RunStore(run_id, repository / ".exo/tl-loop/root")
        snapshot = json.loads(
            (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
        )
        stage = next(
            row for row in snapshot["plan"]["sub_tls"] if row["name"] == run_id
        )
        self.plan = WorkPlan.from_mapping(stage["plan"])
        self.owner_dir = repository / ".exo/agents" / OWNER_AGENT
        self.journal = self.store.run_dir / "action-journal.json"
        self.helpers = _fixture_helpers()
        self.snapshot = Snapshot(repository, self.owner_dir, self.journal)
        self.frozen_arguments: Mapping[str, object] = {}

    def proof(self, config: Any | None = None) -> tuple[Any, str | None]:
        from tl_loop.loop.driver import _terminal_owner_recovery_proof

        return _terminal_owner_recovery_proof(
            self.plan, self.store.load(), config or self.config, self.store
        )

    def arguments(self) -> dict[str, object]:
        from tl_loop.loop.driver import _blocked_leaf_resume_action_arguments

        proof, reason = self.proof()
        if proof is None:
            raise RuntimeError(f"the accepted controller proof is unavailable: {reason}")
        return dict(_blocked_leaf_resume_action_arguments(proof.resume, self.store.load()))

    def seed_declaration_pair(
        self,
        arguments: Mapping[str, object],
        *,
        caller: str | None = None,
        scope: str = OWNER_SLICE,
        configuration: str | None = None,
        route: str | None = None,
        started_at: float | None = None,
        duplicate_offset: float = 0.0,
    ) -> list[int]:
        from datetime import UTC, datetime

        from tl_loop.loop.driver import DispatchAttempt, _dispatch_payload

        attempt = DispatchAttempt(
            intent_id=str(arguments["dispatch_intent_id"]),
            started_at=(
                float(started_at if started_at is not None else arguments["dispatch_started_at"])
                + duplicate_offset
            ),
            harness=str(arguments["harness"]),
            agent_type=str(arguments["agent_type"]),
            model=str(arguments["model"]),
            attempt=int(arguments["dispatch_attempt"]),
            role=str(arguments["role"]),
            effort=str(arguments["effort"]),
            route_configuration_fingerprint=configuration
            or str(arguments["route_configuration_fingerprint"]),
            route_policy_fingerprint=str(arguments["route_policy_fingerprint"]),
            route_manifest_digest=str(arguments["route_manifest_digest"]),
            route_fingerprint=route or str(arguments["route_fingerprint"]),
            controller_epoch=str(arguments["controller_epoch"]),
            dispatch_generation=int(arguments["dispatch_generation"]),
        )
        base = self._highest_sequence()
        event_time = (
            datetime.fromtimestamp(attempt.started_at + duplicate_offset, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        rows = []
        for offset, boundary in ((1, "dispatch_intended"), (2, "spawn_requested")):
            payload = _dispatch_payload(scope, attempt, boundary)
            rows.append(
                self.helpers.ledger_event_row(
                    event_id=f"1511-negative-{boundary}-{base + offset}-{duplicate_offset}",
                    event_time=event_time,
                    run_seq=base + offset,
                    event_type=f"tl.{boundary}",
                    agent_id=caller or self.run_id,
                    run_id=self.run_id,
                    invocation_id=None,
                    generation=None,
                    source="rust",
                    session_id=None,
                    data=payload,
                )
            )
        self.helpers.append_seeded_ledger_rows(self.repository, rows)
        return [row["run_seq"] for row in rows]

    def seed_refusal(
        self,
        arguments: Mapping[str, object],
        *,
        caller: str | None = None,
        scope: str = OWNER_SLICE,
        configuration: str | None = None,
        route: str | None = None,
        intent: str | None = None,
        success: bool = False,
        error: str = EXACT_CONFIGURATION_REFUSAL,
    ) -> int:
        """Append one host-recorded `tool.called` refusal row.

        The standalone server writes this shape after the handler returns. The
        row carries the exact request arguments and error envelope, so the host
        can bind a superseded declaration to a durable prelaunch refusal.
        """
        base = self._highest_sequence()
        run_seq = base + 1
        role_route = {
            "dispatch_intent_id": intent or str(arguments["dispatch_intent_id"]),
            "scope_id": scope,
            "role": str(arguments["role"]),
            "harness": str(arguments["harness"]),
            "agent_type": str(arguments["agent_type"]),
            "model": str(arguments["model"]),
            "effort": str(arguments["effort"]),
            "configuration_fingerprint": configuration
            or str(arguments["route_configuration_fingerprint"]),
            "policy_fingerprint": str(arguments["route_policy_fingerprint"]),
            "manifest_digest": str(arguments["route_manifest_digest"]),
            "route_fingerprint": route or str(arguments["route_fingerprint"]),
        }
        refusal_arguments = {
            "chainlink_issue_id": int(arguments["chainlink_issue_id"]),
            "expected_invocation_id": str(arguments["expected_invocation_id"]),
            "expected_branch": str(arguments["expected_branch"]),
            "expected_worktree_fingerprint": str(
                arguments["expected_worktree_fingerprint"]
            ),
            "task": str(arguments["task"]),
            "human_approved": True,
            "role_route": role_route,
        }
        row = self.helpers.ledger_event_row(
            event_id=f"1511-negative-refusal-{run_seq}",
            event_time="2026-11-20T01:12:00.000Z",
            run_seq=run_seq,
            event_type="tool.called",
            agent_id=caller or self.run_id,
            run_id=self.run_id,
            invocation_id=None,
            generation=None,
            source="rust",
            session_id=None,
            data={
                "tool_name": "resume_blocked_leaf",
                "role": "tl",
                "arguments": refusal_arguments,
                "duration_ms": 4,
                "success": success,
                "error": error,
            },
        )
        self.helpers.append_seeded_ledger_rows(self.repository, [row])
        return run_seq

    def seed_gate_answer(self, gate_name: str, decision: str) -> int:
        """Append one durable `tl.gate_answered` row for the recovery gate."""
        base = self._highest_sequence()
        row = self.helpers.ledger_event_row(
            event_id=f"1511-negative-gate-{base + 1}",
            event_time="2026-11-20T01:12:30.000Z",
            run_seq=base + 1,
            event_type="tl.gate_answered",
            agent_id="controller",
            run_id=self.run_id,
            invocation_id=None,
            generation=None,
            source="rust",
            session_id=None,
            data={
                "gate_name": gate_name,
                "decision": decision,
                "source": "cli",
            },
        )
        self.helpers.append_seeded_ledger_rows(self.repository, [row])
        return row["run_seq"]

    def remove_gate_answers(self) -> None:
        """Temporarily remove durable recovery-gate decisions; restored by the snapshot."""
        for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl")):
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                data = event.get("data") or {}
                if (
                    event.get("type") == "tl.gate_answered"
                    and data.get("gate_name") in {
                        GATE_NAME,
                        f"tl-dispatch-failed-{OWNER_SLICE}",
                        f"tl-dispatch-ownership-conflict-{OWNER_SLICE}",
                    }
                ):
                    continue
                kept.append(line)
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def seed_launch_row(self, arguments: Mapping[str, object], invocation_id: str) -> int:
        base = self._highest_sequence()
        row = self.helpers.ledger_event_row(
            event_id=f"1511-negative-launch-{base + 1}",
            event_time="2026-11-20T01:13:00.000Z",
            run_seq=base + 1,
            event_type="agent.spawned",
            agent_id=self.run_id,
            run_id=self.run_id,
            invocation_id=invocation_id,
            generation=3,
            source="rust",
            session_id=None,
            data={
                "intent_id": str(arguments["dispatch_intent_id"]),
                "slice_id": OWNER_SLICE,
                "invocation_id": invocation_id,
                "child_agent": OWNER_AGENT,
            },
        )
        self.helpers.append_seeded_ledger_rows(self.repository, [row])
        return row["run_seq"]

    def _highest_sequence(self) -> int:
        highest = 0
        for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                sequence = json.loads(line).get("run_seq")
                if type(sequence) is int and sequence > highest:
                    highest = sequence
        return highest

    def event_count(self) -> int:
        return sum(
            1
            for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def dispatch_effect_count(self) -> int:
        effect_types = {
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
            "tl.dispatch_authorization_resolved",
            "agent.spawned",
            "agent.resumed",
            "agent.harness_switch",
        }
        return sum(
            1
            for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("type") in effect_types
        )

    def remove_dispatch_history(self, intent: str) -> None:
        """Temporarily remove dispatch rows for one intent; the case snapshot restores them."""
        for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl")):
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if (
                    event.get("type")
                    in {"tl.dispatch_intended", "tl.spawn_requested", "agent.spawned"}
                    and event.get("data", {}).get("intent_id") == intent
                ):
                    continue
                kept.append(line)
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def remove_refusal_history(self, intent: str) -> None:
        """Temporarily remove host refusal rows for one intent; the snapshot restores them."""
        for path in sorted((self.repository / ".exo/ledger/segments").glob("segment-*.jsonl")):
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                data = event.get("data") or {}
                route = (data.get("arguments") or {}).get("role_route") or {}
                if (
                    event.get("type") == "tool.called"
                    and data.get("tool_name") == "resume_blocked_leaf"
                    and route.get("dispatch_intent_id") == intent
                ):
                    continue
                kept.append(line)
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def host_intent(self, *, predecessor: str, branch: str, fingerprint: str) -> str:
        identity = json.loads(
            (self.owner_dir / "identity.json").read_text(encoding="utf-8")
        )
        issue = int((self.owner_dir / "active_issue").read_text(encoding="utf-8").strip())
        payload = (
            f"blocked-resume-v1\0{identity['agent_name']}\0{issue}\0"
            f"{predecessor}\0{branch}\0{identity['working_dir']}\0{fingerprint}"
        )
        return "blocked-resume:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def call(self, overrides: Mapping[str, object]) -> Any:
        from tl_loop.client.effects import EffectClient
        from tl_loop.client.transport import TransportClient

        arguments = dict(self.frozen_arguments)
        arguments.update(overrides)
        client = EffectClient(
            TransportClient(project_root=self.repository, timeout=60),
            role="tl",
            name=self.run_id,
        )
        return client.resume_blocked_leaf(
            chainlink_issue_id=int(arguments["chainlink_issue_id"]),
            expected_invocation_id=str(arguments["expected_invocation_id"]),
            expected_branch=str(arguments["expected_branch"]),
            expected_worktree_fingerprint=str(arguments["expected_worktree_fingerprint"]),
            task=str(arguments["task"]),
            human_approved=True,
            agent_type=str(arguments["agent_type"]),
            model=str(arguments["model"]),
            role=str(arguments["role"]),
            harness=str(arguments["harness"]),
            effort=str(arguments["effort"]),
            route_configuration_fingerprint=str(arguments["route_configuration_fingerprint"]),
            route_policy_fingerprint=str(arguments["route_policy_fingerprint"]),
            route_manifest_digest=str(arguments["route_manifest_digest"]),
            route_fingerprint=str(arguments["route_fingerprint"]),
            route_dispatch_intent_id=str(arguments["dispatch_intent_id"]),
            route_scope_id=str(arguments.get("route_scope_id", OWNER_SLICE)),
        )

    def invocation_id(self) -> str:
        invocation = json.loads(
            (self.owner_dir / "invocation.json").read_text(encoding="utf-8")
        )
        return str(invocation["invocation_id"])

    def replace_invocation(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        path = self.owner_dir / "invocation.json"
        invocation = json.loads(path.read_text(encoding="utf-8"))
        mutate(invocation)
        path.write_text(json.dumps(invocation) + "\n", encoding="utf-8")

    def replace_gate(self, status: str) -> None:
        path = self.journal.parent / "run.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        gates = [
            {**gate, "status": status} if gate.get("name") == GATE_NAME else gate
            for gate in document["gates"]
        ]
        document["gates"] = gates
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def replace_journal_error(self, error: str) -> None:
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        for row in journal:
            if row.get("operation") == "resume_blocked_leaf" and row.get("status") == "rejected":
                row["error"] = error
                row["result"] = {
                    "success": False,
                    "result": None,
                    "error": error,
                }
        self.journal.write_text(json.dumps(journal) + "\n", encoding="utf-8")


def _agent_digest(repository: Path) -> str:
    digest = hashlib.sha256()
    for directory in sorted((repository / ".exo/agents").iterdir()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            digest.update(path.relative_to(repository).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _expect_refusal(
    name: str,
    result: Any,
    expected: str | tuple[str, ...],
    harness: Harness,
    invocation_before: str,
) -> None:
    if result.success is not False:
        raise SystemExit(f"{name}: the host did not refuse: {result.raw!r}")
    error = str(result.raw.get("error") or "")
    accepted = (expected,) if isinstance(expected, str) else expected
    if not any(candidate in error for candidate in accepted):
        raise SystemExit(f"{name}: expected one of {accepted!r} in {error!r}")
    if harness.invocation_id() != invocation_before:
        raise SystemExit(f"{name}: the refusal changed the owner invocation")
    print(f"NEGATIVE {name}: {error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()

    repository = args.repo.resolve()
    harness = Harness(repository, args.identity, args.run_id)
    agents_before = _agent_digest(repository)
    invocation_before = harness.invocation_id()
    arguments = harness.arguments()
    intent = str(arguments["dispatch_intent_id"])
    harness.frozen_arguments = arguments

    def case(name: str, expected: str | tuple[str, ...], run: Callable[[], Any]) -> None:
        harness.snapshot.restore()
        result = run()
        _expect_refusal(name, result, expected, harness, invocation_before)
        harness.snapshot.restore()

    case(
        "unbound-foreign-intent",
        "role route dispatch intent does not match the launch intent",
        lambda: harness.call({"dispatch_intent_id": f"{intent}:absent"}),
    )

    def missing_authorization() -> Any:
        harness.remove_dispatch_history(intent)
        return harness.call({"dispatch_intent_id": intent})

    case(
        "missing-authorization",
        "role route dispatch authorization is missing or ambiguous",
        missing_authorization,
    )

    def populated_foreign_intent() -> Any:
        foreign = dict(arguments)
        foreign["dispatch_intent_id"] = FOREIGN_INTENT
        harness.seed_declaration_pair(foreign)
        before = harness.dispatch_effect_count()
        result = harness.call({"dispatch_intent_id": FOREIGN_INTENT})
        if harness.dispatch_effect_count() != before:
            raise SystemExit(
                "populated-foreign-intent: the refusal appended dispatch effects"
            )
        return result

    case(
        "populated-foreign-intent",
        "role route dispatch intent does not match the launch intent",
        populated_foreign_intent,
    )
    case(
        "conflicting-duplicate-rows",
        ("competing declarations", "missing or ambiguous"),
        lambda: (
            harness.seed_declaration_pair(arguments),
            harness.seed_declaration_pair(arguments, duplicate_offset=1.0),
            harness.call({}),
        )[-1],
    )
    case(
        "wrong-caller",
        (
            "does not match any durable declaration",
            "missing or ambiguous",
            "conflicts with its durable dispatch authorization",
        ),
        lambda: (
            harness.seed_declaration_pair(arguments, caller="foreign-stage"),
            harness.call({}),
        )[-1],
    )
    case(
        "wrong-scope",
        (
            "does not match any durable declaration",
            "missing or ambiguous",
            "conflicts with its durable dispatch authorization",
        ),
        lambda: (
            harness.seed_declaration_pair(arguments, scope="other-slice"),
            harness.call({}),
        )[-1],
    )
    case(
        "stale-configuration",
        "role route configuration is stale or was not accepted by this server",
        lambda: (
            harness.seed_declaration_pair(arguments),
            harness.call(
                {
                    "route_configuration_fingerprint": (
                        "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
                    ),
                    "route_fingerprint": (
                        "sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724"
                    ),
                }
            ),
        )[-1],
    )
    case(
        "forged-route",
        (
            "does not match any durable declaration",
            "missing or ambiguous",
            "conflicts with its durable dispatch authorization",
        ),
        lambda: (
            harness.seed_declaration_pair(arguments),
            harness.call({"route_fingerprint": "sha256:v1:forged"}),
        )[-1],
    )
    def missing_refusal_proof() -> Any:
        harness.remove_refusal_history(intent)
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "missing-refusal-proof",
        "has no durable verified prelaunch configuration refusal",
        missing_refusal_proof,
    )

    def unrelated_refusal_proof() -> Any:
        harness.remove_refusal_history(intent)
        harness.seed_refusal(
            arguments,
            intent=f"{FOREIGN_INTENT}:unrelated",
            configuration=V1_CONFIGURATION,
            route=V1_ROUTE,
        )
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "unrelated-refusal-proof",
        "has no durable verified prelaunch configuration refusal",
        unrelated_refusal_proof,
    )

    def wrong_declaration_refusal() -> Any:
        harness.remove_refusal_history(intent)
        harness.seed_refusal(
            arguments,
            scope="other-slice",
            configuration=V1_CONFIGURATION,
            route=V1_ROUTE,
        )
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "wrong-declaration-refusal",
        "not bound to any durable dispatch declaration",
        wrong_declaration_refusal,
    )

    def contradictory_refusal() -> Any:
        harness.remove_refusal_history(intent)
        harness.seed_refusal(
            arguments,
            configuration=V1_CONFIGURATION,
            route=V1_ROUTE,
            success=True,
            error="Invalid input: unrelated host refusal",
        )
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "contradictory-refusal",
        "not the exact permitted prelaunch configuration refusal",
        contradictory_refusal,
    )

    def two_distinct_observations_one_refusal() -> Any:
        # The retained v1 declaration already precedes the host refusal. Append a
        # second, distinct v1 observation after the refusal, then the current v2
        # declaration. One refusal between the two old observations can bind
        # only the first; the second has no refusal in its own window.
        harness.seed_declaration_pair(
            arguments, configuration=V1_CONFIGURATION, route=V1_ROUTE
        )
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "two-observations-one-refusal",
        "bound to that exact observation",
        two_distinct_observations_one_refusal,
    )

    def missing_approval() -> Any:
        harness.remove_gate_answers()
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "missing-durable-approval",
        "requires a durable approved operator decision",
        missing_approval,
    )

    def foreign_approval() -> Any:
        harness.remove_gate_answers()
        harness.seed_gate_answer("tl-terminal-owner-recovery-other-slice", "approved")
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "foreign-durable-approval",
        "requires a durable approved operator decision",
        foreign_approval,
    )

    def rejected_approval() -> Any:
        harness.seed_gate_answer(GATE_NAME, "rejected")
        harness.seed_declaration_pair(arguments)
        return harness.call({})

    case(
        "rejected-durable-approval",
        "approved current supersession is required",
        rejected_approval,
    )

    def wrong_predecessor() -> Any:
        predecessor = "foreign-predecessor"
        foreign_intent = harness.host_intent(
            predecessor=predecessor,
            branch=str(arguments["expected_branch"]),
            fingerprint=str(arguments["expected_worktree_fingerprint"]),
        )
        foreign = {**arguments, "dispatch_intent_id": foreign_intent}
        # Keep one older, non-matching declaration under the same intent so the
        # host reaches its durable no-effect/owner evidence check instead of
        # stopping at the earlier intent-binding guard.
        harness.seed_declaration_pair(
            foreign, configuration=V1_CONFIGURATION, route=V1_ROUTE
        )
        harness.seed_refusal(
            foreign, configuration=V1_CONFIGURATION, route=V1_ROUTE
        )
        harness.seed_declaration_pair(foreign)
        return harness.call(
            {
                "expected_invocation_id": predecessor,
                "dispatch_intent_id": foreign_intent,
            }
        )

    case(
        "wrong-predecessor",
        ("launch outcome is unresolved", "missing or ambiguous"),
        wrong_predecessor,
    )

    def wrong_worktree() -> Any:
        fingerprint = "sha256:foreign"
        foreign_intent = harness.host_intent(
            predecessor=str(arguments["expected_invocation_id"]),
            branch=str(arguments["expected_branch"]),
            fingerprint=fingerprint,
        )
        harness.seed_declaration_pair(
            {**arguments, "dispatch_intent_id": foreign_intent}
        )
        return harness.call(
            {
                "expected_worktree_fingerprint": fingerprint,
                "dispatch_intent_id": foreign_intent,
            }
        )

    case(
        "wrong-worktree",
        ("worktree fingerprint changed", "missing or ambiguous"),
        wrong_worktree,
    )
    case(
        "prior-launched-effect",
        ("has a launch row", "missing or ambiguous"),
        lambda: (
            harness.seed_declaration_pair(arguments),
            harness.seed_launch_row(arguments, "foreign-successor"),
            harness.call({}),
        )[-1],
    )
    case(
        "unknown-live-outcome",
        ("still live", "missing or ambiguous"),
        lambda: (
            harness.seed_declaration_pair(arguments),
            harness.replace_invocation(
                lambda invocation: invocation.update({"status": "running", "ended_at": None})
            ),
            harness.call({}),
        )[-1],
    )

    # Controller-level refusals: an unrelated journal error and a rejected gate
    # must hold before any host call.
    def proof_holds(expected: str, mutate: Callable[[], None]) -> None:
        harness.snapshot.restore()
        mutate()
        proof, reason = harness.proof()
        harness.snapshot.restore()
        if proof is not None:
            raise SystemExit(f"the controller released an invalid proof: {expected}")
        if not reason or expected not in reason:
            raise SystemExit(f"expected {expected!r} in {reason!r}")
        print(f"NEGATIVE {expected}: {reason}")

    proof_holds(
        "rejected",
        lambda: harness.replace_journal_error("Invalid input: unrelated host refusal"),
    )
    proof_holds(
        "operator rejected terminal-owner recovery",
        lambda: harness.replace_gate("rejected"),
    )

    if _agent_digest(repository) != agents_before:
        raise SystemExit("a negative case changed an independent agent record")
    print("NEGATIVE independent-workers: unchanged")
    print("DISPATCH-5 PASS")


if __name__ == "__main__":
    main()
