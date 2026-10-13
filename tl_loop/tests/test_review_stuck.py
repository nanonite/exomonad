"""The deterministic review loop and the roles.tl review-stuck judgment (#1158)."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tl_loop import __main__ as launcher
from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope
from tl_loop.fsm.phase import TLPlanning
from tl_loop.loop.convergence import ConvergenceTracker
from tl_loop.loop.driver import (
    TLLoopConfig,
    WorkPlan,
    _apply_convergence,
    _judge_review_stuck,
    _route_ci_event,
    _route_review_event,
)
from tl_loop.loop.escalate import blocked_gate_name
from tl_loop.loop.journal import EffectJournal
from tl_loop.rlm.call import RlmCallError, RlmConfigurationError
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.rlm.subprocess_backend import SubprocessRlmBackend
from tl_loop.rlm.tl_judgment import TLModelResolutionError, build_tl_model_choice
from tl_loop.select.model import ModelCatalog
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import (
    BudgetLedger,
    GateStatus,
    ParkCause,
    PublicationBinding,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import (
    RecordingTransport,
    ReviewBackend,
    ReviewRepairTransport,
    _ci_failure_event,
    _direct_reviewer_event,
    _effect_names,
    _review_store,
    _selector_policy,
)

ROOT = Path(__file__).parents[2]
GUIDE = {
    "root_cause": "The failure path in src/leaf.py is never exercised",
    "proposed_solution": "Cover the failure path in src/leaf.py with a test",
    "read_first": ["src/leaf.py"],
    "steps": ["Add a test for the failure path in src/leaf.py"],
    "verify": ["just tl-loop-test"],
    "boundary": ["Only edit src/leaf.py"],
    "done_criteria": ["The failure path is covered"],
}


def _project(tmp_path: Path, *, tl_allow: str = "codex/gpt-test", catalog: bool = False) -> Path:
    """A project directory laid out the way the controller reads it."""
    exo = tmp_path / ".exo"
    (exo / "tl-loop").mkdir(parents=True)
    policy = (ROOT / ".exo" / "review-policy.toml").read_text(encoding="utf-8")
    (exo / "review-policy.toml").write_text(
        policy.replace("reviewer_max_rounds = 5", "reviewer_max_rounds = 2"),
        encoding="utf-8",
    )
    role = (
        "allow = [{allow}]\ncost_rank = {{ {allow} = 1 }}\n"
        "token_budget = 100000\nescalate_after_attempts = 1\n"
    )
    (exo / "harness_policy.toml").write_text(
        "".join(
            f"[roles.{name}]\n"
            + role.format(allow=f'"{tl_allow if name == "tl" else "codex/gpt-test"}"')
            for name in ("tl", "worker", "reviewer")
        ),
        encoding="utf-8",
    )
    if catalog:
        (exo / "model-catalog.json").write_text(
            (Path(__file__).parent / "fixtures" / "model_catalog.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return tmp_path


def _main_args(project: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=project,
        plan=Path("plan.json"),
        wait_for_plan=False,
        run_id="root",
        poll_interval=0.25,
        max_events=256,
        transport_timeout=45.5,
        active_tail_timeout=60.0,
        task_timeout=90.0,
        dispatch_retry_limit=3,
        dispatch_retry_base_delay=5.0,
        dispatch_retry_max_delay=60.0,
    )


class _EmptySource:
    def start(self) -> _EmptySource:
        return self

    def close(self, timeout: float) -> None:
        del timeout


def _patch_launcher(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Patch only the I/O the launcher opens; the TLLoopConfig it builds stays real."""
    monkeypatch.setattr(launcher, "TransportClient", lambda **kwargs: object())
    monkeypatch.setattr(launcher, "LedgerQueue", lambda *a, **kw: _EmptySource())
    monkeypatch.setattr(launcher, "EffectClient", lambda *a, **kw: object())
    monkeypatch.setattr(launcher, "LedgerReader", lambda *a, **kw: object())
    monkeypatch.setattr(launcher, "_load_plan", lambda path, wait: ({"plan": {}}, b'{"plan":{}}'))
    monkeypatch.setattr(launcher, "_record_plan_snapshot", lambda root, plan, data: None)
    monkeypatch.setattr(launcher, "_plan_from_document", lambda document: object())
    monkeypatch.setattr(launcher, "_authoritative_ledger_run_id", lambda root: None)
    monkeypatch.setattr(launcher, "load_capability", lambda path, policy_path: object())
    monkeypatch.setattr(
        launcher,
        "tl_run",
        lambda initial, config, budgets: captured.setdefault("config", config),
    )
    monkeypatch.setattr(
        "tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/usr/bin/{name}"
    )


def _config_built_by_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: object
) -> TLLoopConfig:
    """The TLLoopConfig ``python -m tl_loop`` builds, with only the backend stubbed."""
    project = _project(tmp_path / "project")
    captured: dict[str, object] = {}
    _patch_launcher(monkeypatch, captured)
    monkeypatch.setattr(
        "tl_loop.rlm.tl_judgment.SubprocessRlmBackend", lambda harness, model: backend
    )
    launcher._run(_main_args(project))
    config = captured["config"]
    assert isinstance(config, TLLoopConfig)
    return config


def _plan() -> WorkPlan:
    return WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                }
            ]
        }
    )


def _owned_store(tmp_path: Path, **slice_changes: object) -> RunStore:
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
                publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1, "inv-a"),
                **slice_changes,
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    return store


def _no_go() -> EventEnvelope:
    return _direct_reviewer_event(verdict="NO-GO", reviewer_agent_id="review-pr-42-codex")


def test_main_config_no_go_below_ceiling_forwards_findings_with_no_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ReviewBackend([])
    config = _config_built_by_main(tmp_path, monkeypatch, backend)
    # The shipped runner never sets review_model_choice; the inner loop needs none.
    assert config.review_model_choice is None
    transport = ReviewRepairTransport()
    store = _owned_store(tmp_path / "run")
    config = replace(
        config, review_policy_path=_project(tmp_path / "policy") / ".exo" / "review-policy.toml"
    )

    _route_review_event(
        _plan(),
        store,
        store.load(),
        TLPlanning(),
        _no_go(),
        1,
        config,
        EffectClient(transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.status is SliceStatus.REPAIRING
    assert restored.review_rounds == 1
    assert backend.requests == []
    assert _effect_names(transport) == ["watcher_pr_state", "resume_pr"]
    resume = next(arguments for name, arguments in transport.calls if name == "resume_pr")
    assert resume["pr_number"] == 42
    task = str(resume["task"])
    assert "The failure path is unhandled" in task
    assert "VERIFY" in task and "just tl-loop-test" in task


def test_ci_failure_before_merge_forwards_failure_with_no_model_call_and_counts_a_round(
    tmp_path: Path,
) -> None:
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path, verdict=Verdict.GO)
    store.set_review_policy(3, "policy_file")

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.status is SliceStatus.REPAIRING
    assert restored.verdict is Verdict.NO_GO
    assert restored.review_rounds == 1
    assert _effect_names(transport) == ["watcher_pr_state", "resume_pr"]
    resume = next(arguments for name, arguments in transport.calls if name == "resume_pr")
    assert "tests failed" in str(resume["task"])


def test_ci_failure_at_the_round_ceiling_is_left_to_the_review_stuck_judgment(
    tmp_path: Path,
) -> None:
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path, verdict=Verdict.GO)
    store.set_review_policy(1, "policy_file")

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True),
        EffectClient(transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.review_rounds == 1
    assert restored.verdict is Verdict.NO_GO
    assert "resume_pr" not in _effect_names(transport)


@dataclass
class _IssueTransport(ReviewRepairTransport):
    def call_tool(self, role: str, name: str, tool_name: str, arguments: JsonObject) -> JsonObject:
        if tool_name == "chainlink_issue_create":
            self.calls.append((tool_name, arguments))
            return {"success": True, "result": {"issue_id": 1010}}
        if tool_name == "watcher_pr_state":
            return ReviewRepairTransport.call_tool(self, role, name, tool_name, arguments)
        return RecordingTransport.call_tool(self, role, name, tool_name, arguments)


def _stuck_store(tmp_path: Path, **changes: object) -> RunStore:
    issue_id = changes.get("chainlink_issue_id")
    store = _review_store(
        tmp_path,
        chainlink_issue_id=issue_id if type(issue_id) is int else None,
    )
    current = replace(
        store.load().slices["leaf-a"],
        verdict=Verdict.NO_GO,
        review_rounds=2,
        reviewer_attempt={"head-a": 1},
        **changes,
    )
    store.checkpoint(TLPlanning(), {"leaf-a": current}, BudgetLedger(0, 0), offset=0)
    store.set_review_policy(2, "policy_file")
    return store


def _stuck_config(
    backend: ReviewBackend,
    *,
    escalate_after: int = 1,
    token_budget: int = 120_000,
) -> TLLoopConfig:
    policy = _selector_policy(escalate_after)
    policy["roles"]["tl"]["token_budget"] = token_budget  # type: ignore[index]
    return TLLoopConfig(
        active=True,
        policy=validate_policy(policy),
        tl_model_choice=RlmModelChoice(
            model_id="tl-model",
            backend=backend,
            role="tl",
            store=RlmCallStore(),
            context_length=10_000,
        ),
    )


def _converge(store: RunStore, config: TLLoopConfig, transport: RecordingTransport):
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")
    return _apply_convergence(
        store.load(),
        ConvergenceTracker(reviewer_max_rounds=2),
        store,
        config,
        EffectClient(transport),
        journal,
    )


def _judged_events(transport: RecordingTransport) -> list[JsonObject]:
    return [
        arguments["payload"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.review_stuck_judged"
    ]


def test_rounds_at_ceiling_make_one_tl_judgment_and_guide_resumes_with_fresh_budget(
    tmp_path: Path,
) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {"action": "guide", "reason": "a concrete repair exists", "handoff": GUIDE},
                input_tokens=10,
                output_tokens=5,
            )
        ]
    )
    transport = _IssueTransport()
    store = _stuck_store(tmp_path)

    _converge(store, _stuck_config(backend), transport)

    restored = store.load().slices["leaf-a"]
    assert [request.name for request in backend.requests] == ["review_stuck"]
    assert restored.status is SliceStatus.REPAIRING
    assert restored.review_rounds == 0
    assert restored.tl_intervention_attempts == 1
    assert restored.park_cause is None
    resume = next(arguments for name, arguments in transport.calls if name == "resume_pr")
    task = str(resume["task"])
    assert GUIDE["proposed_solution"] in task
    assert "The failure path in src/leaf.py is never exercised" in task
    assert resume["expected_head_sha"] == "head-a"
    assert set(resume) == {"pr_number", "expected_head_sha", "task"}
    judged = _judged_events(transport)
    assert judged == [
        {
            "slice_id": "leaf-a",
            "pr_number": 42,
            "head_sha": "head-a",
            "action": "guide",
            "intervention": 1,
            "ceiling": 1,
            "model": "tl-model",
            "tokens": 15,
        }
    ]


def test_review_stuck_budget_is_restored_and_blocks_calls_after_continue(
    tmp_path: Path,
) -> None:
    first_backend = ReviewBackend(
        [RlmResponse({"action": "guide", "reason": "a repair fits", "handoff": GUIDE}, 10, 5)]
    )
    transport = _IssueTransport()
    store = _stuck_store(tmp_path)
    state = store.load()
    config = _stuck_config(first_backend, escalate_after=2, token_budget=15)

    _judge_review_stuck(
        state,
        state.slices["leaf-a"],
        store,
        config,
        EffectClient(transport),
        [],
        2,
    )

    assert store.load().budgets.role_spent["tl"] == 15
    resumed_store = RunStore(store.run_id, store.root_dir)
    resumed = resumed_store.load()
    next_review = replace(
        resumed.slices["leaf-a"],
        status=SliceStatus.IN_REVIEW,
        verdict=Verdict.NO_GO,
        review_rounds=2,
        park_cause=None,
        dispatch_error=None,
    )
    resumed_store.checkpoint(
        resumed.fsm,
        {**resumed.slices, "leaf-a": next_review},
        resumed.budgets,
        resumed.events.last_consumed_offset,
    )
    second_backend = ReviewBackend([])
    second_config = _stuck_config(second_backend, escalate_after=2, token_budget=15)
    second_config = replace(
        second_config,
        tl_model_choice=build_tl_model_choice(
            second_config.policy,
            None,
            backend=second_backend,
            run_store=resumed_store,
        ),
    )
    assert second_config.tl_model_choice.store.ledger.spent["tl"] == 15
    result = _judge_review_stuck(
        resumed_store.load(),
        next_review,
        resumed_store,
        second_config,
        EffectClient(transport),
        [],
        2,
    )

    assert second_backend.requests == []
    assert result.budgets.role_spent["tl"] == 15
    assert result.slices["leaf-a"].park_cause is ParkCause.REVIEW_STUCK


def test_tl_escalate_opens_the_named_human_gate(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [RlmResponse({"action": "escalate", "reason": "the reviewer contradicts the plan"})]
    )
    transport = _IssueTransport()
    store = _stuck_store(tmp_path)

    result = _converge(store, _stuck_config(backend), transport)

    parked = result.slices["leaf-a"]
    assert [request.name for request in backend.requests] == ["review_stuck"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.REVIEW_ROUNDS_EXHAUSTED
    assert any(
        gate.name == blocked_gate_name("review-run", "leaf-a", 1, "review_rounds_exhausted")
        and gate.status is GateStatus.PENDING
        for gate in result.gates
    )
    assert "resume_pr" not in _effect_names(transport)
    assert [event["action"] for event in _judged_events(transport)] == ["escalate"]


def test_review_stuck_keeps_slice_issue_binding_without_tracker_or_plan_effects(
    tmp_path: Path,
) -> None:
    backend = ReviewBackend(
        [RlmResponse({"action": "escalate", "reason": "the findings need human input"})]
    )
    transport = _IssueTransport()
    store = _stuck_store(tmp_path / "run", chainlink_issue_id=1169)
    project_root = tmp_path / "project"
    plan_path = project_root / ".exo" / "tl-loop" / "plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_bytes = (
        b'{"plan":{"leaves":[{"name":"leaf-a","task":"implement",'
        b'"chainlink_issue_id":1169}]}}\n'
    )
    plan_path.write_bytes(plan_bytes)

    result = _converge(
        store,
        replace(_stuck_config(backend), project_root=project_root),
        transport,
    )

    assert result.slices["leaf-a"].chainlink_issue_id == 1169
    assert plan_path.read_bytes() == plan_bytes
    assert not any(name.startswith("chainlink_issue_") for name, _ in transport.calls), (
        transport.calls
    )
    assert [request.name for request in backend.requests] == ["review_stuck"]


def test_tl_intervention_ceiling_opens_the_gate_without_a_model_call(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = _IssueTransport()
    store = _stuck_store(tmp_path, tl_intervention_attempts=1)

    result = _converge(store, _stuck_config(backend, escalate_after=1), transport)

    parked = result.slices["leaf-a"]
    assert backend.requests == []
    assert parked.park_cause is ParkCause.REVIEW_ROUNDS_EXHAUSTED
    assert any(gate.status is GateStatus.PENDING for gate in result.gates)
    assert "resume_pr" not in _effect_names(transport)
    assert _judged_events(transport) == []


@pytest.mark.parametrize(
    "response",
    [
        RlmResponse(
            {
                "action": "guide",
                "reason": "repair",
                "handoff": {**GUIDE, "steps": ["Edit src/other.py instead"]},
            }
        ),
        RlmResponse({"action": "guide", "reason": "repair"}),
    ],
    ids=["unowned-path", "missing-handoff"],
)
def test_unusable_guide_parks_review_stuck_with_the_cause_recorded(
    tmp_path: Path, response: RlmResponse
) -> None:
    backend = ReviewBackend([response])
    transport = _IssueTransport()
    store = _stuck_store(tmp_path)

    _converge(store, _stuck_config(backend), transport)

    parked = store.load().slices["leaf-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.REVIEW_STUCK
    assert parked.dispatch_error
    assert "resume_pr" not in _effect_names(transport)


def test_backend_failure_at_intervention_time_parks_with_an_auditable_cause(
    tmp_path: Path,
) -> None:
    class FailingBackend:
        def complete(self, request: RlmRequest) -> object:
            raise RlmCallError("codex exited with code 1")

    config = _stuck_config(ReviewBackend([]))
    config = replace(
        config,
        tl_model_choice=RlmModelChoice(
            model_id="tl-model",
            backend=FailingBackend(),
            role="tl",
            store=RlmCallStore(),
            context_length=10_000,
        ),
    )
    store = _stuck_store(tmp_path)

    _converge(store, config, _IssueTransport())

    parked = store.load().slices["leaf-a"]
    assert parked.park_cause is ParkCause.REVIEW_STUCK
    assert "codex exited with code 1" in (parked.dispatch_error or "")
    assert parked.tl_intervention_attempts == 1


def test_active_controller_without_a_resolved_tl_model_refuses_to_judge(tmp_path: Path) -> None:
    store = _stuck_store(tmp_path)

    with pytest.raises(Exception, match="tl_model_choice"):
        _converge(store, TLLoopConfig(active=True), _IssueTransport())


# ---- startup resolution ----------------------------------------------------------


def test_startup_refuses_a_bare_harness_with_no_catalog_and_names_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "project", tl_allow="claude")
    captured: dict[str, object] = {}
    _patch_launcher(monkeypatch, captured)

    with pytest.raises(TLModelResolutionError) as error:
        launcher._run(_main_args(project))

    assert ".exo/model-catalog.json" in str(error.value)
    assert ".exo/harness_policy.toml" in str(error.value)
    assert "config" not in captured
    exit_reason = RunStore("root", project / ".exo" / "tl-loop").exit_reason()
    assert exit_reason is not None and ".exo/model-catalog.json" in exit_reason


def test_redispatch_refuses_an_unresolved_tl_model_and_records_the_exit_reason(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", tl_allow="claude")
    state_root = project / ".exo" / "tl-loop"
    (state_root / "plan.json").write_text(
        '{"plan":{"workers":[],"leaves":[],"sub_tls":[]}}', encoding="utf-8"
    )
    store = RunStore("root", state_root)
    create("root", {}, root_dir=state_root)

    with pytest.raises(TLModelResolutionError, match="model-catalog.json"):
        launcher._redispatch(
            argparse.Namespace(
                project_root=project,
                plan=Path(".exo/tl-loop/plan.json"),
                run_id="root",
                slice_id="leaf-a",
                confirm=True,
                max_attempts=1,
                transport_timeout=10.0,
            )
        )

    exit_reason = store.exit_reason()
    assert exit_reason is not None and "model-catalog.json" in exit_reason


def test_pinned_model_and_catalog_both_resolve_a_tl_model() -> None:
    pinned = validate_policy(_selector_policy())
    choice = build_tl_model_choice(pinned, None, backend=object())
    assert choice.model_id == "gpt-luna"
    assert choice.role == "tl"

    bare = _selector_policy()
    bare["roles"]["tl"]["allow"] = ["codex"]  # type: ignore[index]
    bare["roles"]["tl"]["cost_rank"] = {"codex": 1}  # type: ignore[index]
    bare["roles"]["tl"]["per_harness_budget"] = {"codex": 1000}  # type: ignore[index]
    catalog = ModelCatalog.from_fixture(Path(__file__).parent / "fixtures" / "model_catalog.json")
    resolved = build_tl_model_choice(validate_policy(bare), catalog, backend=object())
    assert resolved.model_id != "codex"


def test_startup_refuses_when_the_tl_harness_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tl_loop.rlm.subprocess_backend.shutil.which", lambda name: None)
    with pytest.raises(TLModelResolutionError, match="not found on PATH"):
        build_tl_model_choice(validate_policy(_selector_policy()), None)


def test_startup_refuses_a_harness_without_a_tool_free_backend() -> None:
    policy = _selector_policy()
    policy["roles"]["tl"]["allow"] = ["shoal/custom"]  # type: ignore[index]
    policy["roles"]["tl"]["cost_rank"] = {"shoal/custom": 1}  # type: ignore[index]
    policy["roles"]["tl"]["per_harness_budget"] = {"shoal/custom": 1000}  # type: ignore[index]
    with pytest.raises(TLModelResolutionError, match="no tool-free RLM backend"):
        build_tl_model_choice(validate_policy(policy), None)


# ---- subprocess backend ------------------------------------------------------------


@dataclass
class _Runner:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    last_message: str | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        if self.last_message is not None:
            Path(command[command.index("--output-last-message") + 1]).write_text(self.last_message)
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def _request() -> RlmRequest:
    return RlmRequest(
        name="review_stuck",
        model_id="m",
        inputs={"sections": []},
        output_schema={"type": "object"},
        attempt=1,
    )


def _backend(
    harness: str, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> SubprocessRlmBackend:
    monkeypatch.setattr(
        "tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    return SubprocessRlmBackend(harness, "model-x", runner=runner)


def test_claude_backend_runs_tool_free_and_reads_authoritative_usage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = _Runner(
        stdout=json.dumps(
            {
                "is_error": False,
                "result": '```json\n{"action": "escalate", "reason": "r"}\n```',
                "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "output_tokens": 4},
            }
        )
    )
    caplog.set_level("INFO")

    response = _backend("claude", runner, monkeypatch).complete(_request())

    command = runner.calls[0]["command"]
    assert command[:5] == ["/usr/bin/claude", "-p", "--output-format", "json", "--model"]
    assert command[-2:] == ["--tools", ""]
    assert "model-x" in command
    assert "OUTPUT SCHEMA" in str(runner.calls[0]["input"])
    assert response.output == {"action": "escalate", "reason": "r"}
    assert (response.input_tokens, response.output_tokens) == (10, 4)
    assert "exit_code=0" in caplog.text
    assert "command=/usr/bin/claude" in caplog.text


def test_codex_backend_reads_the_last_message_file_and_charges_an_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _Runner(last_message='{"action": "escalate", "reason": "r"}')

    response = _backend("codex", runner, monkeypatch).complete(_request())

    command = runner.calls[0]["command"]
    assert command[1] == "exec"
    assert "--sandbox" in command and "read-only" in command
    assert "--ephemeral" in command
    assert response.output == {"action": "escalate", "reason": "r"}
    assert response.input_tokens > 0 and response.output_tokens > 0


def test_opencode_backend_runs_standalone_with_the_model_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _Runner(stdout='{"action": "escalate", "reason": "r"}')

    response = _backend("opencode", runner, monkeypatch).complete(_request())

    command = runner.calls[0]["command"]
    assert command[1:5] == ["run", "--standalone", "-m", "model-x"]
    assert "--auto" not in command
    assert response.output == {"action": "escalate", "reason": "r"}


def test_backend_failure_modes_are_explicit_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failed = _Runner(returncode=3, stderr="auth expired")
    with pytest.raises(RlmCallError, match="exited with code 3"):
        _backend("codex", failed, monkeypatch).complete(_request())
    assert "FAILED stderr=auth expired" in caplog.text

    with pytest.raises(RlmCallError, match="not valid JSON"):
        _backend("opencode", _Runner(stdout="no json here"), monkeypatch).complete(_request())

    def timeout(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(RlmCallError, match="timed out"):
        _backend("codex", timeout, monkeypatch).complete(_request())  # type: ignore[arg-type]

    with pytest.raises(RlmConfigurationError, match="no tool-free RLM backend"):
        SubprocessRlmBackend("shoal", "m")
