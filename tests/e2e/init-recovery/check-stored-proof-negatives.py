"""Prove the stored v1 proof's negative guards using the shipped controller.

Run with the built ``tl_loop.pyz`` on ``PYTHONPATH`` so the same production code
the compiled ``exomonad`` controller executes decides each case. Every case
mutates the real seeded state, runs the production proof, asserts the hold, and
restores the exact bytes it changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))


def _config(
    repository: Path,
    *,
    fingerprint: str | None,
    effort: str,
    requested_model: str | None = None,
) -> Any:
    from tl_loop.__main__ import _authoritative_ledger_run_id
    from tl_loop.loop.driver import TLLoopConfig
    from tl_loop.select.capability import load_capability
    from tl_loop.select.model import ModelCatalog, load_model_catalog
    from tl_loop.select.policy import load_policy
    from tl_loop.state.store import RunStore

    state = RunStore("kani", repository / ".exo/tl-loop/root").load()
    policy_path = repository / ".exo/harness_policy.toml"
    return TLLoopConfig(
        active=True,
        root_dir=repository / ".exo/tl-loop",
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
        policy=load_policy(policy_path),
        capabilities=load_capability(
            repository / ".exo/harness_capability.toml", policy_path=policy_path
        ),
        catalog=(
            ModelCatalog.from_payload(
                {
                    "models": [
                        {
                            "harness": "opencode",
                            "model_id": "opencode-go/deepseek-v4.1-flash",
                        },
                        {"harness": "opencode", "model_id": "opencode-go/glm-5.3-flash"},
                    ]
                }
            )
            if requested_model is not None
            else load_model_catalog(repository / ".exo/model-catalog.json")
        ),
        requested_model=requested_model,
        worker_effort_level=effort,
        role_route_configuration_fingerprint=fingerprint,
    )


class Harness:
    def __init__(self, repository: Path, identity: str) -> None:
        from tl_loop.loop.driver import WorkPlan
        from tl_loop.state.store import RunStore

        self.repository = repository
        self.child_store = RunStore("kani", repository / ".exo/tl-loop/root")
        snapshot = json.loads(
            (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
        )
        declaration = next(
            row for row in snapshot["plan"]["sub_tls"] if row["name"] == "kani"
        )
        self.plan = WorkPlan.from_mapping(declaration["plan"])
        self.identity = identity
        self.accepted = _config(repository, fingerprint=identity, effort="high")
        self.slice_id = "issue-814-substitution-model-kani"
        self.owner_dir = next(
            path
            for path in (repository / ".exo" / "agents").iterdir()
            if path.name.endswith("-opencode") and (path / "invocation.json").is_file()
        )
        self.journal_path = self.child_store.run_dir / "action-journal.json"

    def proof(self, config: Any | None = None) -> tuple[Any, str | None]:
        from tl_loop.loop.driver import _terminal_owner_recovery_proof

        return _terminal_owner_recovery_proof(
            self.plan, self.child_store.load(), config or self.accepted, self.child_store
        )

    def mutate_authorization(self, mutate: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        from dataclasses import replace

        state = self.child_store.load()
        current = state.slices[self.slice_id]
        assert current.recovery is not None
        original_evidence = dict(current.recovery.evidence)
        authorization = dict(original_evidence["configured_route_authorization"])
        mutate(authorization)
        self.child_store.checkpoint(
            state.fsm,
            {
                **state.slices,
                self.slice_id: replace(
                    current,
                    recovery=replace(
                        current.recovery,
                        evidence={
                            **original_evidence,
                            "configured_route_authorization": authorization,
                        },
                    ),
                ),
            },
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
            plan_manifest=state.plan_manifest,
        )

        def restore() -> None:
            latest = self.child_store.load()
            target = latest.slices[self.slice_id]
            assert target.recovery is not None
            self.child_store.checkpoint(
                latest.fsm,
                {
                    **latest.slices,
                    self.slice_id: replace(
                        target,
                        recovery=replace(target.recovery, evidence=original_evidence),
                    ),
                },
                latest.budgets,
                latest.events.last_consumed_offset,
                current_order=latest.current_order,
                ordered_stages=latest.ordered_stages,
                integration=latest.integration,
                plan_manifest=latest.plan_manifest,
            )

        return restore

    def mutate_journal(self, status: str) -> Callable[[], None]:
        original = self.journal_path.read_text(encoding="utf-8")
        entries = json.loads(original)
        resume_indexes = [
            index
            for index, entry in enumerate(entries)
            if entry.get("operation") == "resume_blocked_leaf"
        ]
        entries[resume_indexes[-1]]["status"] = status
        self.journal_path.write_text(json.dumps(entries, sort_keys=True, indent=2) + "\n")

        def restore() -> None:
            self.journal_path.write_text(original, encoding="utf-8")

        return restore

    def mutate_invocation(self, status: str) -> Callable[[], None]:
        path = self.owner_dir / "invocation.json"
        original = path.read_text(encoding="utf-8")
        invocation = json.loads(original)
        invocation.update({"status": status, "ended_at": None})
        path.write_text(json.dumps(invocation, sort_keys=True, indent=2) + "\n")

        def restore() -> None:
            path.write_text(original, encoding="utf-8")

        return restore

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--v1-digest", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    from tl_loop.state.schema import GateStatus

    harness = Harness(args.repo, args.identity)
    results: list[dict[str, Any]] = []
    proof, reason = harness.proof()
    if proof is None:
        raise SystemExit(f"the corrected host must accept the stored proof first: {reason}")
    results.append({"case": "positive-control", "held": False, "reason": None})

    unsupported = _config(args.repo, fingerprint=None, effort="high")
    changed_route = _config(
        args.repo,
        fingerprint=args.identity,
        effort="high",
        requested_model="opencode-go/glm-5.3-flash",
    )
    changed_effort = _config(args.repo, fingerprint=args.identity, effort="xhigh")

    cases: list[tuple[str, Callable[[], Callable[[], None]], Callable[[], tuple[Any, str | None]]]] = []

    def forged() -> Callable[[], None]:
        def mutate(record: dict[str, Any]) -> None:
            record["digest"] = "1" * 64

        return harness.mutate_authorization(mutate)

    def malformed() -> Callable[[], None]:
        def mutate(record: dict[str, Any]) -> None:
            record["digest"] = "not-a-digest"

        return harness.mutate_authorization(mutate)

    def wrong_owner() -> Callable[[], None]:
        def mutate(record: dict[str, Any]) -> None:
            record["owner_agent_id"] = "some-other-owner"

        return harness.mutate_authorization(mutate)

    def wrong_scope() -> Callable[[], None]:
        def mutate(record: dict[str, Any]) -> None:
            record["manifest_digest"] = "0" * 64

        return harness.mutate_authorization(mutate)

    def wrong_predecessor() -> Callable[[], None]:
        def mutate(record: dict[str, Any]) -> None:
            record["prior_invocation_id"] = "00000000-0000-0000-0000-000000000000"

        return harness.mutate_authorization(mutate)

    cases.extend(
        [
            ("forged-legacy-digest", forged, harness.proof),
            ("malformed-legacy-digest", malformed, harness.proof),
            ("wrong-owner", wrong_owner, harness.proof),
            ("wrong-scope-manifest", wrong_scope, harness.proof),
            ("wrong-predecessor", wrong_predecessor, harness.proof),
            ("unaccepted-current-identity", lambda: lambda: None, lambda: harness.proof(unsupported)),
            ("unknown-prior-effect", lambda: harness.mutate_journal("unknown"), harness.proof),
            ("live-worker", lambda: harness.mutate_invocation("running"), harness.proof),
        ]
    )

    def pending_gate() -> Callable[[], None]:
        gate_name = "tl-terminal-owner-recovery-issue-814-substitution-model-kani"
        harness.child_store.set_gate(gate_name, GateStatus.PENDING)

        def restore() -> None:
            harness.child_store.set_gate(gate_name, GateStatus.APPROVED)

        return restore

    cases.append(
        ("route-change-with-pending-gate", pending_gate, lambda: harness.proof(changed_route))
    )

    for name, mutate, run in cases:
        restore = mutate()
        try:
            held, held_reason = run()
        finally:
            restore()
        if held is not None:
            raise SystemExit(f"{name} was not held: the proof still accepted the state")
        results.append({"case": name, "held": True, "reason": held_reason})

    # A proven prelaunch refusal is the supported continue path for model,
    # harness, runtime and effort changes. These are accepted, not held.
    for name, config in (
        ("genuine-model-change", changed_route),
        ("genuine-effort-change", changed_effort),
    ):
        accepted, accepted_reason = harness.proof(config)
        if accepted is None:
            raise SystemExit(f"{name} was held: {accepted_reason}")
        results.append({"case": name, "held": False, "reason": accepted_reason})

    gate_name = "tl-terminal-owner-recovery-issue-814-substitution-model-kani"
    harness.child_store.answer_gate(gate_name, GateStatus.REJECTED)
    try:
        held, held_reason = harness.proof()
    finally:
        harness.child_store.answer_gate(gate_name, GateStatus.APPROVED)
    if held is not None:
        raise SystemExit("an independent gate rejection was not held")
    results.append({"case": "independent-gate-rejection", "held": True, "reason": held_reason})

    after, reason = harness.proof()
    if after is None:
        raise SystemExit(f"the restored state no longer accepts the stored proof: {reason}")
    args.output.write_text(
        json.dumps({"cases": results, "restored": True}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cases": len(results), "restored": True}))


if __name__ == "__main__":
    main()
