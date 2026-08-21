"""Run the programmatic TL controller from an ExoMonad project."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import DEFAULT_TIMEOUT_SECONDS, TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.queue import DEFAULT_ACTIVE_TAIL_TIMEOUT_SECONDS, LedgerQueue
from tl_loop.events.reader import LedgerReader, SequenceStatus
from tl_loop.loop.driver import TLLoopConfig, TLRunResult, WorkPlan, tl_run
from tl_loop.loop.heartbeat import HeartbeatConfig
from tl_loop.loop.observability import emit_controller_event
from tl_loop.plan_validation import (
    PlanValidationError,
    validate_plan_document,
    validate_plan_proposal,
)
from tl_loop.preflight import PreflightError, run_preflight
from tl_loop.select.capability import load_capability
from tl_loop.select.model import load_model_catalog
from tl_loop.select.policy import load_policy
from tl_loop.state.read_model import project_read_model
from tl_loop.state.schema import GateStatus, RunState
from tl_loop.state.store import CorruptCheckpoint, RunStore

LOGGER = logging.getLogger("tl_loop")
DEFAULT_RUN_ID = "root"
DEFAULT_PLAN = Path(".exo/tl-loop/plan.json")
DEFAULT_IDLE_TIMEOUT = 30.0
DEFAULT_DISPATCH_TIMEOUT = 5.0
DEFAULT_CONTROLLER_STALL_TIMEOUT = 300.0
DEFAULT_MAX_EVENTS = 256
PLAN_REJECTION_REASON_LIMIT = 160


class LauncherError(RuntimeError):
    """The controller cannot start without an explicit, valid input."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the controller or one of its operator-facing read/write commands."""
    parser = _parser()
    parsed_argv = list(argv) if argv is not None else sys.argv[1:]
    if not parsed_argv or parsed_argv[0] not in {
        "run",
        "status",
        "gate",
        "plan-proposal",
        "preflight",
        "-h",
        "--help",
    }:
        parsed_argv.insert(0, "run")
    args = parser.parse_args(parsed_argv)
    _configure_logging(
        args.verbose if hasattr(args, "verbose") else False,
        project_root=getattr(args, "project_root", None),
        run_id=getattr(args, "run_id", None),
    )
    try:
        if args.command == "run":
            result = _run(args)
            _print_result(result)
        elif args.command == "status":
            _print_status(args)
        elif args.command == "plan-proposal":
            _print_plan_proposal(args)
        elif args.command == "preflight":
            run_preflight(args.project_root)
            print(
                "TL preflight passed: config.toml, harness_policy.toml, review-policy.toml, harness_capability.toml"
            )
        else:
            _set_gate(args)
    except (
        LauncherError,
        OSError,
        RuntimeError,
        ValueError,
        PlanValidationError,
        PreflightError,
    ) as error:
        LOGGER.error("[TL loop] %s", error)
        _record_run_failure(args, error)
        return 2
    return 0


def _record_run_failure(args: argparse.Namespace, error: Exception) -> None:
    """Keep a failed run discoverable even when startup fails before _run."""
    if getattr(args, "command", None) != "run":
        return
    try:
        project_root = args.project_root.expanduser().resolve()
        run_id = getattr(args, "run_id", DEFAULT_RUN_ID)
        RunStore(run_id, project_root / ".exo" / "tl-loop").record_exit_reason(
            str(error), error=error
        )
    except (OSError, ValueError, TypeError) as record_error:
        LOGGER.error("[TL loop] failed to persist controller exit reason: %s", record_error)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tl_loop")
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="run the programmatic TL controller")
    _add_project_options(run)
    run.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    run.add_argument("--run-id", default=os.environ.get("EXOMONAD_TL_LOOP_RUN_ID", DEFAULT_RUN_ID))
    run.add_argument("--max-events", type=_positive_int, default=DEFAULT_MAX_EVENTS)
    run.add_argument(
        "--idle-timeout",
        type=_positive_float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="legacy compatibility option; lifecycle progress has no idle deadline",
    )
    run.add_argument("--transport-timeout", type=_positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument(
        "--active-tail-timeout",
        type=_positive_float,
        default=DEFAULT_ACTIVE_TAIL_TIMEOUT_SECONDS,
    )
    run.add_argument("--dispatch-timeout", type=_positive_float, default=DEFAULT_DISPATCH_TIMEOUT)
    run.add_argument(
        "--controller-stall-timeout",
        type=_positive_float,
        default=DEFAULT_CONTROLLER_STALL_TIMEOUT,
    )
    run.add_argument("--poll-interval", type=_positive_float, default=0.25)
    run.add_argument("--wait-for-plan", action="store_true")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(command="run")

    status = subcommands.add_parser("status", help="show a durable TL run state")
    _add_project_options(status)
    status.add_argument(
        "--run-id", default=os.environ.get("EXOMONAD_TL_LOOP_RUN_ID", DEFAULT_RUN_ID)
    )
    status.add_argument("--watch", action="store_true")
    status.add_argument("--interval", type=_positive_float, default=2.0)
    status.set_defaults(command="status")

    gate = subcommands.add_parser("gate", help="answer a durable human gate")
    _add_project_options(gate)
    gate.add_argument("--run-id", default=os.environ.get("EXOMONAD_TL_LOOP_RUN_ID", DEFAULT_RUN_ID))
    gate.add_argument("--name", required=True)
    decision = gate.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    gate.add_argument(
        "--source",
        choices=("cli", "control"),
        default="cli",
        help=argparse.SUPPRESS,
    )
    gate.set_defaults(command="gate")

    proposal = subcommands.add_parser(
        "plan-proposal", help="validate an inert control-plane plan proposal"
    )
    _add_project_options(proposal)
    proposal.add_argument(
        "--run-id", default=os.environ.get("EXOMONAD_TL_LOOP_RUN_ID", DEFAULT_RUN_ID)
    )
    proposal.set_defaults(command="plan-proposal")

    preflight = subcommands.add_parser("preflight", help="validate required TL controller files")
    _add_project_options(preflight)
    preflight.set_defaults(command="preflight")

    return parser


def _add_project_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(os.environ.get("EXOMONAD_TL_LOOP_PROJECT_ROOT", Path.cwd())),
    )


def _run(args: argparse.Namespace) -> TLRunResult:
    project_root = args.project_root.expanduser().resolve()
    plan_path = _resolve_under_project(project_root, args.plan)
    plan_document = _load_plan(plan_path, args.wait_for_plan)
    plan = _plan_from_document(plan_document)
    run_id = _run_id(plan_document, args.run_id)
    ledger_run_id = _authoritative_ledger_run_id(project_root)
    state_root = project_root / ".exo" / "tl-loop"
    reader = LedgerReader(
        project_root / ".exo" / "ledger" / "segments",
        run_id=run_id,
        state_root=state_root,
        ledger_run_id=ledger_run_id,
    )
    source = LedgerQueue(
        reader,
        poll_interval=args.poll_interval,
        active_tail_timeout=args.active_tail_timeout,
    ).start()
    effects = EffectClient(
        TransportClient(project_root=project_root, timeout=args.transport_timeout),
        role="tl",
        name="root",
    )
    try:
        policy = load_policy(project_root / ".exo" / "harness_policy.toml")
        capabilities = load_capability(
            project_root / ".exo" / "harness_capability.toml",
            policy_path=project_root / ".exo" / "harness_policy.toml",
        )
        catalog = load_model_catalog(project_root / ".exo" / "model-catalog.json")
    except Exception as error:
        RunStore(run_id, state_root).record_exit_reason(str(error), error=error)
        raise
    config = TLLoopConfig(
        active=True,
        max_events=args.max_events,
        idle_timeout=args.idle_timeout,
        dispatch_timeout=args.dispatch_timeout,
        controller_stall_timeout=args.controller_stall_timeout,
        keep_alive_on_waiting=True,
        heartbeat=HeartbeatConfig(),
        poll_interval=args.poll_interval,
        source=source,
        effects=effects,
        root_dir=state_root,
        project_root=project_root,
        run_id=run_id,
        ledger_run_id=ledger_run_id,
        role="worker",
        review_policy_path=project_root / ".exo" / "review-policy.toml",
        policy=policy,
        capabilities=capabilities,
        catalog=catalog,
    )
    budgets = plan_document.get("budgets", {"tokens": 0, "wall_seconds": 0})
    if not isinstance(budgets, Mapping):
        raise LauncherError("plan.budgets must be an object when provided")
    LOGGER.info(
        "[TL loop] starting run_id=%s plan=%s role=tl name=root",
        run_id,
        plan_path,
    )
    try:
        return tl_run(
            {"run_id": run_id, "plan": plan},
            config,
            cast(Mapping[str, object], budgets),
        )
    except Exception as error:
        RunStore(run_id, state_root).record_exit_reason(str(error), error=error)
        raise
    finally:
        source.close(timeout=1.0)


def _load_plan(path: Path, wait_for_plan: bool) -> dict[str, object]:
    if wait_for_plan:
        announced = False
        while not path.exists():
            if not announced:
                LOGGER.info(
                    "[TL loop] waiting for plan at %s; the TL window is ready for operator input",
                    path,
                )
                LOGGER.info(
                    "[TL loop] write a JSON WorkPlan there, then the controller will resume"
                )
                announced = True
            time.sleep(0.5)
    if not path.exists():
        raise LauncherError(
            f"plan is missing at {path}; write a JSON WorkPlan or start with --wait-for-plan"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LauncherError(f"plan {path} is not valid JSON: {error}") from error
    try:
        return validate_plan_document(value)
    except PlanValidationError as error:
        raise LauncherError(f"plan {path} is invalid: {error}") from error


def _plan_from_document(document: Mapping[str, object]) -> WorkPlan:
    value: object = document.get("plan")
    if value is None and any(key in document for key in ("workers", "leaves", "sub_tls")):
        value = {key: document[key] for key in ("workers", "leaves", "sub_tls") if key in document}
    if not isinstance(value, Mapping):
        raise LauncherError("plan must contain a WorkPlan object under `plan`")
    try:
        return WorkPlan.from_mapping(value)
    except (TypeError, ValueError) as error:
        raise LauncherError(f"invalid WorkPlan: {error}") from error


def _print_plan_proposal(args: argparse.Namespace) -> None:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        _emit_plan_proposal(args, False, _bounded_reason(error))
        raise LauncherError(f"plan proposal is not valid JSON: {error}") from error
    try:
        proposal = validate_plan_proposal(value)
    except PlanValidationError as error:
        _emit_plan_proposal(args, False, _bounded_reason(error))
        raise
    plan = proposal["plan"]
    if not isinstance(plan, Mapping):
        raise LauncherError("validated plan proposal is not an object")
    _emit_plan_proposal(args, True)
    print(
        json.dumps(
            {"run_id": args.run_id, "plan": dict(plan), "inert": True, "status": "proposed"},
            sort_keys=True,
        )
    )


def _emit_plan_proposal(
    args: argparse.Namespace,
    accepted: bool,
    rejection_reason: str | None = None,
) -> None:
    payload: dict[str, object] = {"run_id": args.run_id, "accepted": accepted}
    if rejection_reason is not None:
        payload["rejection_reason"] = rejection_reason
    project_root = args.project_root.expanduser().resolve()
    effects = EffectClient(
        TransportClient(project_root=project_root),
        role="tl",
        name="root",
    )
    emit_controller_event(effects, "tl.plan_proposed", payload)


def _bounded_reason(error: Exception) -> str:
    reason = " ".join(str(error).split()) or "proposal rejected"
    if len(reason) <= PLAN_REJECTION_REASON_LIMIT:
        return reason
    return reason[: PLAN_REJECTION_REASON_LIMIT - 3] + "..."


def _run_id(document: Mapping[str, object], configured: str) -> str:
    value = document.get("run_id", configured)
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise LauncherError("run_id must be a non-empty single path component")
    return value


def _authoritative_ledger_run_id(project_root: Path) -> str | None:
    """Read the server-owned swarm ID without changing the local checkpoint key."""
    path = project_root / ".exo" / "run_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _print_result(result: TLRunResult) -> None:
    state = result.final_state
    LOGGER.info(
        "[TL loop] finished phase=%s consumed_offset=%d effects=%d",
        state.fsm.phase.value,
        state.events.last_consumed_offset,
        len(result.effects),
    )
    if result.diagnostics:
        LOGGER.info("[TL loop] event diagnostics=%s", dict(result.diagnostics))
    LOGGER.info(
        "[TL loop] ordered current_order=%d integration=%s next_transition=%s",
        state.current_order,
        state.integration.lifecycle.value,
        _next_state_transition(state),
    )
    for gate in state.gates:
        LOGGER.info("[TL loop] gate name=%s status=%s", gate.name, gate.status.value)
        if gate.status is GateStatus.PENDING:
            LOGGER.warning(
                "[TL loop] human gate pending: run `python3 -m tl_loop gate --run-id %s --name %s --approve|--reject`",
                state.run_id,
                gate.name,
            )
    for slice_state in state.slices.values():
        LOGGER.info(
            "[TL loop] slice id=%s status=%s pr=%s",
            slice_state.id,
            slice_state.status.value,
            slice_state.pr_number,
        )


def _next_state_transition(state: RunState) -> str:
    """Describe the next legal operator transition for the terminal summary."""
    pending_gate = next(
        (gate.name for gate in state.gates if gate.status is GateStatus.PENDING),
        None,
    )
    if pending_gate is not None:
        return f"answer_gate:{pending_gate}"
    if state.integration.lifecycle.value == "INTEGRATION_CONFLICT":
        return f"resume_pr:{state.integration.integration_owner_id or 'aggregate'}"
    if state.integration.lifecycle.value == "NEEDS_BASE_REVALIDATION":
        return "revalidate_base_and_integration_ci"
    if state.integration.lifecycle.value == "READY_FOR_INTEGRATION":
        return "validate_integration_evidence"
    return "await_controller"


def _print_status(args: argparse.Namespace) -> None:
    project_root = args.project_root.expanduser().resolve()
    root = project_root / ".exo" / "tl-loop"
    plan_path = project_root / ".exo" / "tl-loop" / "plan.json"
    while True:
        if args.watch:
            print("\033[2J\033[H", end="")
        store = RunStore(args.run_id, root)
        print(_status_snapshot(store, project_root, plan_path, args.run_id))
        if not args.watch:
            return
        time.sleep(args.interval)


def _status_snapshot(
    store: RunStore,
    project_root: Path,
    plan_path: Path,
    run_id: str,
) -> str:
    """Render one race-tolerant, read-only status snapshot."""
    reason = store.exit_reason()
    if reason:
        return f"controller exited: {reason}"
    if not store.path.exists():
        return f"no run yet; controller is waiting for .exo/tl-loop/plan.json (path: {plan_path})"
    try:
        state = store.load()
        reader = LedgerReader(
            project_root / ".exo" / "ledger" / "segments",
            run_id=run_id,
            state_root=store.root_dir,
            ledger_run_id=_authoritative_ledger_run_id(project_root),
        )
        replay = reader.read_from(0)
    except (CorruptCheckpoint, OSError, ValueError) as error:
        return f"checkpoint is currently unavailable; retrying: {error}"
    document = _state_document(state, replay.events, replay.sequence_status)
    summary = store.terminal_summary()
    if summary is not None:
        document["terminal_summary"] = dict(summary)
    return json.dumps(document, indent=2, sort_keys=True)


def _set_gate(args: argparse.Namespace) -> None:
    project_root = args.project_root.expanduser().resolve()
    root = project_root / ".exo" / "tl-loop"
    store = RunStore(args.run_id, root)
    status = GateStatus.APPROVED.value if args.approve else GateStatus.REJECTED.value
    try:
        store.answer_gate(args.name, GateStatus(status))
    except ValueError as error:
        raise LauncherError(str(error)) from error
    effects = EffectClient(
        TransportClient(project_root=project_root),
        role="tl",
        name="root",
    )
    emit_controller_event(
        effects,
        "tl.gate_answered",
        {
            "gate_name": args.name,
            "decision": status,
            "source": args.source,
        },
    )
    LOGGER.info("[TL loop] gate name=%s status=%s", args.name, status)


def _state_document(
    state: RunState,
    events: Iterable[EventEnvelope] = (),
    sequence_status: SequenceStatus | None = None,
) -> dict[str, object]:
    """Serialize the cursor-carrying read model for the status client."""
    document = project_read_model(
        state,
        events,
        sequence_status=sequence_status,
    ).to_document()
    document["last_consumed_offset"] = state.events.last_consumed_offset
    return document


def _resolve_under_project(project_root: Path, value: Path) -> Path:
    candidate = value.expanduser()
    return candidate if candidate.is_absolute() else project_root / candidate


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    # `not parsed > 0` also rejects NaN, which `parsed <= 0` would let through.
    if not parsed > 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _configure_logging(
    verbose: bool,
    *,
    project_root: Path | None = None,
    run_id: str | None = None,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for handler in list(LOGGER.handlers):
        if getattr(handler, "_tl_controller_output", False):
            LOGGER.removeHandler(handler)
            handler.close()
    if project_root is not None and run_id:
        output_path = project_root.expanduser().resolve() / ".exo" / "tl-loop" / run_id
        output_path.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(output_path / "controller-output.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        handler._tl_controller_output = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
