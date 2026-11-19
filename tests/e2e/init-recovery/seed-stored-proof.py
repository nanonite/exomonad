"""Seed the retained v1 terminal-owner proof and its rejected prelaunch action.

The fixture copies the shape of the live #1433 finding on 7f0cced2:

* f446 (OpenCode/DeepSeek/high) was the accepted initial spawn and failed.
* The operator approved the named terminal-owner recovery gate.
* The next attempt was authorized under the historical ``sha256:v1:`` role
  configuration identity and anchored to the confirmed f285 (Codex/Sol/xhigh)
  generation-2 successor.
* That attempt was refused by the host before launch because init and the
  running server had not agreed on the configuration snapshot.
* The current init/server snapshot uses the canonical ``sha256:v2:`` identity
  with the same effective route.

Everything here uses production helpers (`_terminal_owner_recovery_proof`,
`_persist_terminal_route_authorization`, `EffectJournal`), so the seeded state
is exactly what the shipped controller would have written. Only the historical
identity scheme is substituted: the retained v1 fingerprint is recorded, and
its digest is recomputed by the production v1 digest function for this
fixture's own policy and manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

HISTORICAL_CONFIG_FINGERPRINT_V1 = (
    "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
)
CONFIRMED_TERMINAL_SUCCESSOR_TRANSITION = (
    "confirmed_terminal_successor_reconciled_to_current_route"
)
DIRECTED_WORKER_MODEL = "opencode-go/deepseek-v4.1-flash"
DIRECTED_WORKER_EFFORT = "high"
OWNER_SLICE = "issue-814-substitution-model-kani"
BLOCKED_ISSUE_ID = 814


def _config(
    repository: Path,
    *,
    fingerprint: str | None,
    worker_effort: str,
) -> Any:
    from tl_loop.__main__ import _authoritative_ledger_run_id
    from tl_loop.loop.driver import TLLoopConfig
    from tl_loop.select.capability import load_capability
    from tl_loop.select.model import load_model_catalog
    from tl_loop.select.policy import load_policy
    from tl_loop.state.store import RunStore

    root = repository / ".exo/tl-loop"
    state = RunStore("kani", root / "root").load()
    policy_path = repository / ".exo/harness_policy.toml"
    policy = load_policy(policy_path)
    return TLLoopConfig(
        active=True,
        root_dir=root,
        project_root=repository,
        run_id="kani",
        ledger_run_id=_authoritative_ledger_run_id(repository),
        branch=state.owner_branch,
        role="worker",
        worktree=Path(state.owner_worktree) if state.owner_worktree else None,
        parent_run_id=state.parent_run_id,
        parent_branch=state.parent_branch,
        parent_agent_id=state.parent_agent_id,
        depth=state.depth,
        declared_manifest=state.plan_manifest,
        policy=policy,
        capabilities=load_capability(
            repository / ".exo/harness_capability.toml", policy_path=policy_path
        ),
        catalog=load_model_catalog(repository / ".exo/model-catalog.json"),
        worker_effort_level=worker_effort,
        role_route_configuration_fingerprint=fingerprint,
    )


def _sibling_module(name: str) -> Any:
    import importlib.util

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_legacy_successor_lifecycle(
    repository: Path,
    *,
    owner_name: str,
    controller_run_id: str,
    successor_id: str,
    predecessor_id: str,
    branch: str,
    worktree: str,
    ended_at: int,
) -> bool:
    module = _sibling_module("configured-owner-resume")
    module._seed_legacy_successor_lifecycle(
        repository,
        owner_name=owner_name,
        controller_run_id=controller_run_id,
        successor_id=successor_id,
        predecessor_id=predecessor_id,
        branch=branch,
        worktree=worktree,
        ended_at=ended_at,
    )
    return True


def _plan(repository: Path) -> Any:
    from tl_loop.loop.driver import WorkPlan

    snapshot = json.loads(
        (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
    )
    declaration = next(
        row for row in snapshot["plan"]["sub_tls"] if row["name"] == "kani"
    )
    return WorkPlan.from_mapping(declaration["plan"])


def _ensure_human_recovery(repository: Path, child_store: Any) -> None:
    """Retain the human-gate recovery the shipped controller recorded for f446.

    The seeded ledger cursor is already past the blocker event, so the
    controller's startup reconciliation must find this durable human-gate
    recovery instead of replaying the event. The shape mirrors the retained
    live checkpoint and is only written when the fixture has no recovery yet.
    """
    from dataclasses import replace

    from tl_loop.fsm.recovery import RecoveryPhase, RecoveryState
    from tl_loop.state.store import RunStore

    store: RunStore = child_store
    state = store.load()
    current = state.slices[OWNER_SLICE]
    if current.recovery is not None:
        return
    if current.attempts < 1 or current.dispatch_invocation_id is None:
        raise SystemExit("the seeded terminal-owner checkpoint has no accepted invocation")
    blocker_seq = 0
    for segment in sorted(
        (repository / ".exo" / "ledger" / "segments").glob("segment-*.jsonl")
    ):
        for line in segment.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            data = row.get("data") or {}
            if (
                row.get("type") == "agent.task_blocked"
                and data.get("slice_id") == OWNER_SLICE
                and type(row.get("run_seq")) is int
            ):
                blocker_seq = max(blocker_seq, int(row["run_seq"]))
    if blocker_seq == 0:
        raise SystemExit("the seeded ledger has no human-blocked row for the owner")
    recovery = RecoveryState(
        cause="human_decision_required",
        phase=RecoveryPhase.HUMAN_GATE,
        recovery_round=1,
        next_action="open_human_gate",
        owner_run_id=state.run_id,
        entered_at=time.time(),
        slice_attempt=current.attempts,
        owner_agent_id=current.dispatch_agent_id,
        invocation_generation=1,
        plan_revision=1,
        evidence={
            "slice_id": OWNER_SLICE,
            "attempt": current.attempts,
            "invocation_id": current.dispatch_invocation_id,
            "scope_attribution": "agent_lifecycle",
            "matched_difficulty_rule": "missing_handoff:nonzero_exit",
            "outcome": "blocked",
            "needs_human": True,
            "retryable": True,
            "generation": 1,
        },
    )
    store.checkpoint(
        state.fsm,
        {**state.slices, OWNER_SLICE: replace(current, recovery=recovery)},
        state.budgets,
        max(state.events.last_consumed_offset, blocker_seq),
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )


def seed(repository: Path, baseline_path: Path, output: Path) -> None:
    """Record the retained v1 authorization for the unanswered f285 predecessor."""
    from tl_loop.client.effects import ToolResult
    from tl_loop.loop.driver import (
        EffectIntent,
        _blocked_leaf_resume_action_arguments,
        _persist_terminal_route_authorization,
        _terminal_owner_recovery_proof,
    )
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.state.store import RunStore

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        raise SystemExit("stored-proof seed requires the accepted terminal-owner fixture")
    if baseline["prior_invocation"]["invocation_id"] != (
        "f4460477-40bf-4824-941b-dcc3d0ebc9a1"
    ):
        raise SystemExit("the seeded predecessor is not the retained f446 invocation")
    plan = _plan(repository)
    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    _ensure_human_recovery(repository, child_store)
    historical_config = _config(
        repository,
        fingerprint=HISTORICAL_CONFIG_FINGERPRINT_V1,
        worker_effort=DIRECTED_WORKER_EFFORT,
    )
    initial, reason = _terminal_owner_recovery_proof(
        plan, child_store.load(), historical_config, child_store
    )
    if initial is None:
        raise SystemExit(f"could not authorize the historical f446 route: {reason}")
    if (
        initial.resume.desired_model != DIRECTED_WORKER_MODEL
        or initial.resume.desired_effort != DIRECTED_WORKER_EFFORT
    ):
        raise SystemExit("production route selection did not resolve OpenCode DeepSeek/high")
    state = _persist_terminal_route_authorization(initial, child_store.load(), child_store)
    initial_recovery = state.slices[OWNER_SLICE].recovery
    assert initial_recovery is not None
    initial_authorization = initial_recovery.evidence["configured_route_authorization"]

    owner_name = baseline["owner_agent_name"]
    owner_dir = Path(baseline["owner_dir"])
    branch = baseline["branch"]
    worktree = Path(baseline["worktree"]).resolve(strict=True)
    predecessor_id = baseline["prior_invocation"]["invocation_id"]
    successor_id = "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
    journal = EffectJournal("kani", child_store.run_dir / "action-journal.json")
    arguments = dict(_blocked_leaf_resume_action_arguments(initial.resume, state))
    arguments.pop("route_fingerprint", None)
    intent = EffectIntent("resume_blocked_leaf", OWNER_SLICE, arguments, True)
    journal.append(intent)
    legacy_agent = {
        "agent_type": "codex",
        "branch_name": branch,
        "worktree_path": str(worktree),
        "invocation_id": successor_id,
        "invocation_trigger": "resume_blocked",
        "invocation_runtime": "codex",
        "invocation_fresh": True,
        "invocation_ready": True,
        "invocation_outcome": "started",
        "routing_target_id": "@43",
        "routing_target_type": "window",
    }
    legacy_invocation = {
        "invocation_id": successor_id,
        "trigger": "resume_blocked",
        "runtime": "codex",
        "fresh": True,
        "ready": True,
        "outcome": "started",
        "target_id": "@43",
        "target_type": "window",
    }
    journal.mark_result(
        intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    "success": True,
                    "chainlink_issue_id": BLOCKED_ISSUE_ID,
                    "agent": legacy_agent,
                    "invocation": legacy_invocation,
                },
            }
        ),
    )
    identity_path = owner_dir / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity.update({"agent_type": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"})
    identity_path.write_text(json.dumps(identity, sort_keys=True, indent=2) + "\n")
    ended_at = int(time.time()) - 1
    (owner_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": successor_id,
                "generation": 2,
                "status": "exited",
                "runtime": "codex",
                "model": "gpt-6.1-sol",
                "effort": "xhigh",
                "trigger": "resume_blocked",
                "branch": branch,
                "worktree": str(worktree),
                "runtime_agent_id": owner_name,
                "slice_id": OWNER_SLICE,
                "prior_invocation_id": predecessor_id,
                "pr_number": None,
                "head_sha": None,
                "started_at": ended_at - 1,
                "ended_at": ended_at,
                "exit_code": 0,
                "exit_reason": "one_shot_complete",
                "exit_classification": "one_shot_complete",
                "one_shot_complete": True,
                "routing": {"window_id": "@43"},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    lifecycle_seeded = _seed_legacy_successor_lifecycle(
        repository,
        owner_name=owner_name,
        controller_run_id="kani",
        successor_id=successor_id,
        predecessor_id=predecessor_id,
        branch=branch,
        worktree=str(worktree),
        ended_at=ended_at,
    )
    if not lifecycle_seeded:
        raise SystemExit("the retained f285 lifecycle rows could not be seeded")
    (owner_dir / "dispatch_intent").write_text(
        str(arguments["dispatch_intent_id"]) + "\n", encoding="utf-8"
    )
    (owner_dir / "routing.json").write_text(
        json.dumps(
            {
                "owner_agent_id": owner_name,
                "owner_invocation_id": successor_id,
                "owner_generation": 2,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    current, reason = _terminal_owner_recovery_proof(
        plan, child_store.load(), historical_config, child_store
    )
    if current is None:
        raise SystemExit(f"could not reconcile the confirmed f285 predecessor: {reason}")
    authorization = current.route_authorization
    if authorization.get("transition") != CONFIRMED_TERMINAL_SUCCESSOR_TRANSITION:
        raise SystemExit("the retained v1 proof is not the confirmed-successor reconciliation")
    if authorization.get("supersedes_route_authorization_digest") != (
        initial_authorization["digest"]
    ):
        raise SystemExit("the retained v1 proof does not supersede the f446 authorization")
    if authorization.get("prior_invocation_id") != successor_id:
        raise SystemExit("the retained v1 proof is not anchored to f285")
    if authorization.get("configuration_fingerprint") != HISTORICAL_CONFIG_FINGERPRINT_V1:
        raise SystemExit("the retained proof does not carry the historical identity")
    state = _persist_terminal_route_authorization(current, child_store.load(), child_store)
    stored_recovery = state.slices[OWNER_SLICE].recovery
    assert stored_recovery is not None
    stored = stored_recovery.evidence["configured_route_authorization"]
    output.write_text(
        json.dumps(
            {
                "v1_configuration_fingerprint": stored["configuration_fingerprint"],
                "v1_digest": stored["digest"],
                "v1_supersedes": stored["supersedes_route_authorization_digest"],
                "f446_authorization_digest": initial_authorization["digest"],
                "successor_invocation_id": successor_id,
                "successor_generation": 2,
                "gate_name": baseline["terminal_recovery_gate"],
                "owner_agent_name": owner_name,
                "branch": branch,
                "worktree": str(worktree),
                "stored_proof": stored,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"v1_digest": stored["digest"], "prior_invocation_id": successor_id}))


def expected(repository: Path, accepted: Path, output: Path) -> None:
    """Compute, without persisting, the v2 proof the controller must create."""
    from tl_loop.loop.driver import _terminal_owner_recovery_proof
    from tl_loop.state.store import RunStore

    accepted_document = json.loads(accepted.read_text(encoding="utf-8"))
    config = _config(
        repository,
        fingerprint=accepted_document["identity"],
        worker_effort=DIRECTED_WORKER_EFFORT,
    )
    plan = _plan(repository)
    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    proof, reason = _terminal_owner_recovery_proof(
        plan, child_store.load(), config, child_store
    )
    if proof is None:
        raise SystemExit(f"the corrected controller still holds: {reason}")
    authorization = proof.route_authorization
    output.write_text(
        json.dumps(
            {
                "configuration_fingerprint": authorization["configuration_fingerprint"],
                "digest": authorization["digest"],
                "transition": authorization["transition"],
                "supersedes_route_authorization_digest": authorization.get(
                    "supersedes_route_authorization_digest"
                ),
                "prior_invocation_id": authorization["prior_invocation_id"],
                "harness": authorization["harness"],
                "agent_type": authorization["agent_type"],
                "model": authorization["model"],
                "effort": authorization["effort"],
                "terminal_invocation_digest": authorization["terminal_invocation_digest"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"expected_digest": authorization["digest"]}))


def refuse(repository: Path, baseline_path: Path, output: Path) -> None:
    """Record the exact prelaunch configuration refusal the live run retained."""
    from tl_loop.client.effects import ToolResult
    from tl_loop.loop.driver import (
        EffectIntent,
        ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED,
        _blocked_leaf_resume_action_arguments,
        _terminal_owner_recovery_proof,
    )
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.state.store import RunStore

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    plan = _plan(repository)
    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    historical_config = _config(
        repository,
        fingerprint=HISTORICAL_CONFIG_FINGERPRINT_V1,
        worker_effort=DIRECTED_WORKER_EFFORT,
    )
    proof, reason = _terminal_owner_recovery_proof(
        plan, child_store.load(), historical_config, child_store
    )
    if proof is None:
        raise SystemExit(f"could not rebuild the retained v1 action: {reason}")
    arguments = dict(_blocked_leaf_resume_action_arguments(proof.resume, child_store.load()))
    journal = EffectJournal("kani", child_store.run_dir / "action-journal.json")
    intent = EffectIntent("resume_blocked_leaf", OWNER_SLICE, arguments, True)
    if journal.existing(intent) is not None:
        raise SystemExit("the prelaunch refusal is already recorded")
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult.from_raw(
            {
                "success": False,
                "result": None,
                "error": ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED,
            }
        ),
    )
    entries = journal.entries_for("resume_blocked_leaf", OWNER_SLICE)
    if len(entries) != 2 or entries[-1].get("status") != "rejected":
        raise SystemExit("the retained prelaunch refusal was not recorded exactly")
    output.write_text(
        json.dumps(
            {
                "route_configuration_fingerprint": arguments[
                    "route_configuration_fingerprint"
                ],
                "route_authorization_digest": arguments["route_authorization_digest"],
                "desired_model": arguments["desired_model"],
                "desired_effort": arguments["desired_effort"],
                "expected_invocation_id": arguments["expected_invocation_id"],
                "status": entries[-1]["status"],
                "error": entries[-1]["error"],
                "journal_entries": [json.loads(json.dumps(entry)) for entry in entries],
                "baseline_owner": baseline["owner_agent_name"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"refused_digest": arguments["route_authorization_digest"]}))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    seed_parser = sub.add_parser("seed")
    seed_parser.add_argument("--repo", required=True, type=Path)
    seed_parser.add_argument("--baseline", required=True, type=Path)
    seed_parser.add_argument("--output", required=True, type=Path)
    expected_parser = sub.add_parser("expected")
    expected_parser.add_argument("--repo", required=True, type=Path)
    expected_parser.add_argument("--accepted", required=True, type=Path)
    expected_parser.add_argument("--output", required=True, type=Path)
    refuse_parser = sub.add_parser("refuse")
    refuse_parser.add_argument("--repo", required=True, type=Path)
    refuse_parser.add_argument("--baseline", required=True, type=Path)
    refuse_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.repo, args.baseline, args.output)
    elif args.command == "expected":
        expected(args.repo, args.accepted, args.output)
    else:
        refuse(args.repo, args.baseline, args.output)


if __name__ == "__main__":
    main()
