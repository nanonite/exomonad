"""Reconciliation of a timed-out blocked-leaf resume from durable host evidence.

Every test drives the production driver hook or the production classifier. The
fixtures are redacted copies: a real Git worktree plus a real ledger segment,
and only the host rows the server writes for the same launch.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from tl_loop.loop import launch_evidence, prelaunch_refusal
from tl_loop.loop.driver import EffectIntent, _reconcile_unknown_launch_actions
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.launch_evidence import HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS
from tl_loop.loop.launch_reconciliation import reconcile_unknown_blocked_resume
from tl_loop.loop.prelaunch_refusal import (
    PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS,
    is_proven_prelaunch_role_route_refusal,
)
from tl_loop.state.schema import RunState
from tl_loop.state.store import RunStore

RUN_ID = "kani"
SLICE_ID = "issue-814-substitution-model-kani"
OWNER = f"{SLICE_ID}-opencode"
PARENT_BRANCH = "main.kani"
BRANCH = f"{PARENT_BRANCH}.{OWNER}"
WORKTREE_FINGERPRINT = "sha256:" + "b" * 64
PRIOR_INVOCATION = "opencode-prior-invocation"
SUCCESSOR_INVOCATION = "opencode-successor-invocation"
INTENT = "blocked-resume:" + "a" * 64
DISPATCH_STARTED_AT = 1000.0
LEDGER_FLOOR = 10
ROUTE = {
    "role": "worker",
    "harness": "opencode/opencode-go/deepseek-v4.1-flash",
    "agent_type": "opencode",
    "model": "opencode-go/deepseek-v4.1-flash",
    "effort": "high",
    "configuration_fingerprint": "sha256:v1:" + "4" * 64,
    "policy_fingerprint": None,
    "manifest_digest": "sha256:v1:" + "5" * 64,
    "route_fingerprint": "sha256:v1:" + "6" * 64,
    "dispatch_intent_id": INTENT,
    "scope_id": SLICE_ID,
}


def _journal_arguments(worktree: Path) -> dict[str, object]:
    return {
        "chainlink_issue_id": 814,
        "expected_invocation_id": PRIOR_INVOCATION,
        "expected_branch": BRANCH,
        "expected_worktree_fingerprint": WORKTREE_FINGERPRINT,
        "task": "Complete the Kani stage recovery fixture for issue 814",
        "human_approved": True,
        "owner_agent_name": OWNER,
        "prior_generation": 1,
        "expected_worktree": str(worktree),
        "expected_worktree_recorded": str(worktree),
        "dispatch_intent_id": INTENT,
        "dispatch_attempt": 2,
        "dispatch_started_at": DISPATCH_STARTED_AT,
        "dispatch_generation": 2,
        "dispatch_ledger_floor": LEDGER_FLOOR,
        "controller_epoch": "epoch-1",
        "route_reconciliation_source": "proven_preexecution_retry",
        "harness": ROUTE["harness"],
        "agent_type": ROUTE["agent_type"],
        "model": ROUTE["model"],
        "role": ROUTE["role"],
        "effort": ROUTE["effort"],
        "route_configuration_fingerprint": ROUTE["configuration_fingerprint"],
        "route_policy_fingerprint": ROUTE["policy_fingerprint"],
        "route_manifest_digest": ROUTE["manifest_digest"],
        "route_fingerprint": ROUTE["route_fingerprint"],
        "role_route": dict(ROUTE),
    }


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "repo"
    worktree = project / ".exo" / "worktrees" / OWNER
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", BRANCH, str(worktree)],
        check=True,
        capture_output=True,
    )
    owner_dir = project / ".exo" / "agents" / OWNER
    owner_dir.mkdir(parents=True)
    (owner_dir / "active_issue").write_text("814\n", encoding="utf-8")
    (owner_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": OWNER,
                "slug": SLICE_ID,
                "slice_id": SLICE_ID,
                "agent_type": "opencode",
                "model": "opencode-go/deepseek-v4.1-flash",
                "effort": "high",
                "birth_branch": BRANCH,
                "parent_branch": PARENT_BRANCH,
                "working_dir": str(worktree),
                "topology": "worktree_per_agent",
                "ledger_owned": False,
            }
        ),
        encoding="utf-8",
    )
    (owner_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": PRIOR_INVOCATION,
                "generation": 1,
                "status": "failed",
                "runtime": "opencode",
                "model": "opencode-go/deepseek-v4.1-flash",
                "effort": "high",
                "trigger": "spawn",
                "branch": BRANCH,
                "worktree": str(worktree),
                "pr_number": None,
                "head_sha": None,
                "prior_invocation_id": None,
            }
        ),
        encoding="utf-8",
    )
    return project, owner_dir


def _mark_successor(owner_dir: Path, worktree: Path) -> None:
    (owner_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": SUCCESSOR_INVOCATION,
                "generation": 2,
                "status": "running",
                "runtime": "opencode",
                "model": "opencode-go/deepseek-v4.1-flash",
                "effort": "high",
                "trigger": "resume_blocked",
                "branch": BRANCH,
                "worktree": str(worktree),
                "prior_invocation_id": PRIOR_INVOCATION,
                "pr_number": None,
                "head_sha": None,
                "role_route": dict(ROUTE),
            }
        ),
        encoding="utf-8",
    )
    (owner_dir / "dispatch_intent").write_text(INTENT + "\n", encoding="utf-8")
    (owner_dir / "routing.json").write_text(
        json.dumps(
            {
                "owner_agent_id": OWNER,
                "owner_invocation_id": SUCCESSOR_INVOCATION,
                "owner_generation": 2,
            }
        ),
        encoding="utf-8",
    )


def _ledger_event(
    event_type: str,
    data: dict[str, object],
    *,
    run_seq: int,
    caller: str = "kani",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": f"{event_type}-{run_seq}",
        "id": f"{event_type}-{run_seq}",
        "event_time": "2026-11-23T14:52:13Z",
        "observed_at": "2026-11-23T14:52:13Z",
        "run_seq": run_seq,
        "type": event_type,
        "agent_id": caller,
        "run_id": RUN_ID,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": data,
    }


def _launch_row(run_seq: int = 11) -> dict[str, object]:
    return _ledger_event(
        "agent.spawned",
        {
            "child_agent": OWNER,
            "agent_type": "opencode",
            "spawn_type": "resume_blocked",
            "branch": BRANCH,
            "model": "opencode-go/deepseek-v4.1-flash",
            "effort": "high",
            "topology": "worktree_per_agent",
            "intent_id": INTENT,
            "invocation_id": SUCCESSOR_INVOCATION,
        },
        run_seq=run_seq,
    )


def _refusal_row(
    *,
    run_seq: int = 12,
    error: str = next(iter(PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS)),
    arguments: dict[str, object] | None = None,
    caller: str = "kani",
) -> dict[str, object]:
    worktree = Path(str(arguments["expected_worktree"])) if arguments else Path("/unused")
    request = arguments or _journal_arguments(worktree)
    return _ledger_event(
        "tool.called",
        {
            "tool_name": "resume_blocked_leaf",
            "role": "tl",
            "arguments": request,
            "duration_ms": 24000,
            "success": False,
            "error": error,
            "error_kind": "invalid_input",
            "error_context": None,
            "cancelled": False,
            "dispatch_intent_id": INTENT,
        },
        run_seq=run_seq,
        caller=caller,
    )


def _write_ledger(project: Path, *rows: dict[str, object]) -> None:
    segments = project / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    segment = segments / "segment-000000000001.jsonl"
    segment.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _journal(project: Path) -> tuple[EffectJournal, EffectIntent]:
    run_dir = project / ".exo" / "tl-loop" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = EffectJournal(RUN_ID, run_dir / "action-journal.json")
    intent = EffectIntent(
        "resume_blocked_leaf", SLICE_ID, _journal_arguments(_worktree(project)), True
    )
    journal.append(intent)
    journal.mark_unknown(intent, TimeoutError("client gave up before the host replied"))
    return journal, intent


def _worktree(project: Path) -> Path:
    return project / ".exo" / "worktrees" / OWNER


def _run_hook(
    project: Path,
    journal: EffectJournal,
    *,
    now: float,
) -> bool:
    store = RunStore(RUN_ID, project / ".exo" / "tl-loop")
    _, resolved = _reconcile_unknown_launch_actions(
        cast(RunState, object()),
        store,
        journal,
        project_root=project,
        ledger_run_id=RUN_ID,
        now=now,
    )
    return resolved


def _entry(journal: EffectJournal) -> dict[str, object]:
    entries = journal.entries_for("resume_blocked_leaf", SLICE_ID)
    assert len(entries) == 1
    return entries[0]


def test_host_completed_launch_is_adopted_without_a_second_request(tmp_path: Path) -> None:
    """A host launch row adopts exactly one invocation and retires the request."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    _write_ledger(project, _launch_row())
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "adopt"
    assert decision.invocation_id == SUCCESSOR_INVOCATION
    assert decision.result is not None
    assert decision.result["result"]["invocation"]["invocation_id"] == SUCCESSOR_INVOCATION  # type: ignore[index]
    resolved = _run_hook(project, journal, now=DISPATCH_STARTED_AT + 31)
    assert resolved is True
    entry = _entry(journal)
    assert entry["status"] == "confirmed"
    assert entry["result"]["result"]["invocation"]["invocation_id"] == SUCCESSOR_INVOCATION  # type: ignore[index]
    reconciliation = entry["launch_reconciliation"]
    assert isinstance(reconciliation, dict)
    assert reconciliation["outcome"] == "adopt"
    # Re-evaluating the same journal never creates a second adoption.
    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 32) is False
    assert _entry(journal)["result"] == entry["result"]


def test_host_prelaunch_refusal_resolves_rejected_with_the_shared_contract(
    tmp_path: Path,
) -> None:
    """A host refusal row rejects the entry and unlocks the #1590 retry path."""
    project, _owner_dir = _seed_project(tmp_path)
    arguments = _journal_arguments(_worktree(project))
    refusal = _refusal_row(arguments=arguments)
    _write_ledger(project, refusal)
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "reject"
    assert decision.error in PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS
    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 31) is True
    entry = _entry(journal)
    assert entry["status"] == "rejected"
    # The shared prelaunch-refusal definition recognizes the resolved entry, so
    # the #1590 classifier permits a retry on the newly accepted route.
    assert is_proven_prelaunch_role_route_refusal(entry)


def test_no_host_evidence_holds_then_is_reevaluated_and_adopts(tmp_path: Path) -> None:
    """Absence of evidence holds, is re-evaluated on later ticks, then adopts."""
    project, owner_dir = _seed_project(tmp_path)
    _write_ledger(project)
    journal, _intent = _journal(project)

    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 5) is False
    first = _entry(journal)
    assert first["status"] == "unknown"
    diagnostic = first["launch_reconciliation"]["diagnostic"]  # type: ignore[index]
    assert INTENT in diagnostic
    assert "no host evidence yet" in diagnostic
    assert first["launch_reconciliation"]["evaluations"] == 1  # type: ignore[index]

    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 20) is False
    second = _entry(journal)
    assert second["status"] == "unknown"
    assert second["launch_reconciliation"]["evaluations"] == 2  # type: ignore[index]

    _mark_successor(owner_dir, _worktree(project))
    _write_ledger(project, _launch_row())
    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 40) is True
    assert _entry(journal)["status"] == "confirmed"


def test_no_evidence_after_the_bounded_window_stays_held(tmp_path: Path) -> None:
    """The bounded window changes the diagnostic, never the outcome."""
    project, _owner_dir = _seed_project(tmp_path)
    _write_ledger(project)
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project,
        _entry(journal),
        ledger_run_id=RUN_ID,
        now=DISPATCH_STARTED_AT + HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS + 1,
    )

    assert decision.outcome == "hold"
    assert INTENT in decision.diagnostic
    assert "bounded window" in decision.diagnostic
    assert "never relaunched on absence of evidence" in decision.diagnostic
    assert _entry(journal)["status"] == "unknown"


@pytest.mark.parametrize("conflict", ["launch_and_refusal", "duplicate_launch", "duplicate_refusal"])
def test_conflicting_or_duplicate_evidence_stays_held(
    tmp_path: Path, conflict: str
) -> None:
    """Ambiguous host evidence never resolves to a launch."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    arguments = _journal_arguments(_worktree(project))
    if conflict == "launch_and_refusal":
        rows = (_launch_row(11), _refusal_row(arguments=arguments, run_seq=12))
    elif conflict == "duplicate_launch":
        rows = (_launch_row(11), _launch_row(12))
    else:
        rows = (
            _refusal_row(arguments=arguments, run_seq=11),
            _refusal_row(arguments=arguments, run_seq=12),
        )
    _write_ledger(project, *rows)
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "hold"
    assert "conflicting host evidence" in decision.diagnostic
    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 31) is False
    assert _entry(journal)["status"] == "unknown"


@pytest.mark.parametrize(
    "case", ["wrong_owner", "wrong_predecessor", "forged_intent", "wrong_invocation"]
)
def test_wrong_owner_predecessor_or_forged_intent_stays_held(
    tmp_path: Path, case: str
) -> None:
    """A launch row must bind to the exact journaled owner and predecessor."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    launch = _launch_row()
    data = cast(dict[str, object], launch["data"])
    if case == "wrong_owner":
        data["child_agent"] = "someone-else"
    elif case == "wrong_predecessor":
        invocation = json.loads((owner_dir / "invocation.json").read_text(encoding="utf-8"))
        invocation["prior_invocation_id"] = "a-different-predecessor"
        (owner_dir / "invocation.json").write_text(json.dumps(invocation), encoding="utf-8")
    elif case == "forged_intent":
        data["intent_id"] = "blocked-resume:" + "f" * 64
    else:
        data["invocation_id"] = "a-different-successor"
    _write_ledger(project, launch)
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "hold"
    assert _run_hook(project, journal, now=DISPATCH_STARTED_AT + 31) is False
    assert _entry(journal)["status"] == "unknown"


def test_refusal_with_a_wrong_owner_binding_stays_held(tmp_path: Path) -> None:
    """A refusal row that names another predecessor cannot reject this request."""
    project, _owner_dir = _seed_project(tmp_path)
    arguments = _journal_arguments(_worktree(project))
    arguments["expected_invocation_id"] = "a-different-predecessor"
    _write_ledger(project, _refusal_row(arguments=arguments))
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "hold"
    assert "does not bind" in decision.diagnostic
    assert _entry(journal)["status"] == "unknown"


def test_evidence_before_the_dispatch_floor_is_ignored(tmp_path: Path) -> None:
    """A row older than the journaled dispatch boundary cannot adopt the request."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    _write_ledger(project, _launch_row(run_seq=LEDGER_FLOOR - 1))
    journal, _intent = _journal(project)

    decision = reconcile_unknown_blocked_resume(
        project, _entry(journal), ledger_run_id=RUN_ID, now=DISPATCH_STARTED_AT + 30
    )

    assert decision.outcome == "hold"
    assert "no host evidence yet" in decision.diagnostic
    assert _entry(journal)["status"] == "unknown"


def test_restart_while_unknown_adopts_exactly_once(tmp_path: Path) -> None:
    """A fresh journal/process reads the same evidence and adopts once."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    _write_ledger(project, _launch_row())
    journal, _intent = _journal(project)

    # Simulate init --continue: a brand-new journal instance over the same file.
    restarted = EffectJournal(RUN_ID, project / ".exo" / "tl-loop" / RUN_ID / "action-journal.json")
    assert _run_hook(project, restarted, now=DISPATCH_STARTED_AT + 30) is True
    entry = _entry(restarted)
    assert entry["status"] == "confirmed"
    assert entry["result"]["result"]["invocation"]["invocation_id"] == SUCCESSOR_INVOCATION  # type: ignore[index]

    assert _run_hook(project, restarted, now=DISPATCH_STARTED_AT + 31) is False
    assert len(journal.entries_for("resume_blocked_leaf", SLICE_ID)) == 1
    assert _entry(restarted)["result"] == entry["result"]


def test_controller_evidence_definition_is_shared_not_copied() -> None:
    """The reconciler imports the one #1590 refusal set; it does not copy it."""
    assert (
        launch_evidence.PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS
        is prelaunch_refusal.PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS
    )


def test_docs_describe_the_timeout_and_reconciliation_contract() -> None:
    """Both CLAUDE.md files and the contract doc describe the shipped rules."""
    repository = Path(__file__).resolve().parents[2]
    required = {
        "LAUNCH_CAPABLE_TIMEOUT_SECONDS": "timeout contract",
        "dispatch_intent_id": "reconciliation identity",
        "PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS": "shared refusal definition",
    }
    for relative in (
        "CLAUDE.md",
        "tl_loop/CLAUDE.md",
        "docs/launch-capable-effect-contract.md",
    ):
        text = (repository / relative).read_text(encoding="utf-8")
        for needle, label in required.items():
            assert needle in text, f"{relative} does not describe the {label}"


def test_concurrent_reconciliation_ticks_adopt_exactly_once(tmp_path: Path) -> None:
    """Two concurrent ticks serialize on the run lock and adopt one invocation."""
    project, owner_dir = _seed_project(tmp_path)
    _mark_successor(owner_dir, _worktree(project))
    _write_ledger(project, _launch_row())
    journal, _intent = _journal(project)
    store = RunStore(RUN_ID, project / ".exo" / "tl-loop")

    def tick(now: float) -> bool:
        tick_journal = EffectJournal(
            RUN_ID, project / ".exo" / "tl-loop" / RUN_ID / "action-journal.json"
        )
        _, resolved = _reconcile_unknown_launch_actions(
            cast(RunState, object()),
            store,
            tick_journal,
            project_root=project,
            ledger_run_id=RUN_ID,
            now=now,
        )
        return resolved

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(tick, (DISPATCH_STARTED_AT + 30, DISPATCH_STARTED_AT + 30)))

    assert sum(outcomes) == 1
    entries = journal.entries_for("resume_blocked_leaf", SLICE_ID)
    assert len(entries) == 1
    assert entries[0]["status"] == "confirmed"
    assert entries[0]["result"]["result"]["invocation"]["invocation_id"] == SUCCESSOR_INVOCATION  # type: ignore[index]
