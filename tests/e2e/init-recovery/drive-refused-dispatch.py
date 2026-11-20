"""Execute the retained old dispatch through the production host path.

The historical prelaunch configuration refusal must be durable host evidence,
not a caller assertion. This helper replays the exact rejected action arguments
against the running standalone server whose accepted configuration is newer, so
the server itself writes the `tool.called` refusal row into the ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED = (
    "Invalid input: role route configuration is stale or was not accepted by this server"
)
EXACT_ERROR = ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED
TOOL = "resume_blocked_leaf"


def _ledger_rows(repository: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((repository / ".exo/ledger/segments").glob("segment-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _refusal_rows(repository: Path) -> list[dict[str, Any]]:
    return [
        row
        for row in _ledger_rows(repository)
        if row.get("type") == "tool.called"
        and row.get("data", {}).get("tool_name") == TOOL
    ]


def _launch_rows(repository: Path, intent: str) -> list[dict[str, Any]]:
    return [
        row
        for row in _ledger_rows(repository)
        if row.get("type") == "agent.spawned"
        and row.get("data", {}).get("intent_id") == intent
    ]


def _declaration_scope(repository: Path, intent: str) -> str:
    for row in _ledger_rows(repository):
        data = row.get("data")
        if (
            row.get("type") == "tl.dispatch_intended"
            and isinstance(data, Mapping)
            and data.get("intent_id") == intent
            and isinstance(data.get("slice_id"), str)
        ):
            return str(data["slice_id"])
    raise SystemExit("the retained dispatch has no durable declaration scope")


def _route(arguments: Mapping[str, Any], repository: Path) -> Mapping[str, Any]:
    """Read the route receipt from a journal action or a recorded request.

    Journal actions keep the route fields at the top level; the host records
    the request's nested `role_route`. Both describe the same authorization.
    """
    route = arguments.get("role_route")
    if isinstance(route, Mapping):
        return route
    intent = arguments.get("dispatch_intent_id")
    if not isinstance(intent, str) or not intent:
        raise SystemExit("the retained action has no durable dispatch intent")
    return {
        "role": arguments.get("role") or arguments.get("configured_role"),
        "harness": arguments.get("harness"),
        "agent_type": arguments.get("agent_type") or arguments.get("desired_agent_type"),
        "model": arguments.get("model") or arguments.get("desired_model"),
        "effort": arguments.get("effort") or arguments.get("desired_effort"),
        "configuration_fingerprint": arguments.get("route_configuration_fingerprint"),
        "policy_fingerprint": arguments.get("route_policy_fingerprint"),
        "manifest_digest": arguments.get("route_manifest_digest"),
        "route_fingerprint": arguments.get("route_fingerprint"),
        "dispatch_intent_id": intent,
        "scope_id": arguments.get("route_scope_id")
        or arguments.get("scope_id")
        or _declaration_scope(repository, intent),
    }


def _call(arguments: Mapping[str, Any], repository: Path, run_id: str) -> Any:
    from tl_loop.client.effects import EffectClient
    from tl_loop.client.transport import TransportClient

    route = _route(arguments, repository)
    client = EffectClient(
        TransportClient(project_root=repository, timeout=60), role="tl", name=run_id
    )
    return client.resume_blocked_leaf(
        chainlink_issue_id=int(arguments["chainlink_issue_id"]),
        expected_invocation_id=str(arguments["expected_invocation_id"]),
        expected_branch=str(arguments["expected_branch"]),
        expected_worktree_fingerprint=str(arguments["expected_worktree_fingerprint"]),
        task=str(arguments["task"]),
        human_approved=True,
        agent_type=str(route["agent_type"]),
        model=str(route["model"]),
        role=str(route["role"]),
        harness=str(route["harness"]),
        effort=str(route["effort"]),
        route_configuration_fingerprint=str(route["configuration_fingerprint"]),
        route_policy_fingerprint=str(route["policy_fingerprint"]),
        route_manifest_digest=str(route["manifest_digest"]),
        route_fingerprint=str(route["route_fingerprint"]),
        route_dispatch_intent_id=str(route["dispatch_intent_id"]),
        route_scope_id=str(route["scope_id"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--refused-action", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    repository = args.repo.resolve()
    refused = json.loads(args.refused_action.read_text(encoding="utf-8"))
    entries = refused.get("journal_entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("the retained refusal has no journal entries")
    arguments = entries[-1].get("arguments")
    if not isinstance(arguments, Mapping):
        raise SystemExit("the retained refusal has no action arguments")
    route = _route(arguments, repository)
    intent = str(route["dispatch_intent_id"])
    configuration = str(route["configuration_fingerprint"])
    route_fingerprint = str(route["route_fingerprint"])

    before = _refusal_rows(repository)
    result = _call(arguments, repository, args.run_id)
    if result.success is not False:
        raise SystemExit(f"the old dispatch was not refused: {result.raw!r}")
    error = str(result.raw.get("error") or "")
    if error != EXACT_ERROR:
        raise SystemExit(f"the old dispatch produced an unexpected error: {error!r}")

    after = _refusal_rows(repository)
    added = [row for row in after if row not in before]
    if len(added) != 1:
        raise SystemExit(f"expected one durable host refusal row, found {len(added)}")
    refusal = added[0]
    data = refusal.get("data")
    if not isinstance(data, Mapping):
        raise SystemExit("the durable host refusal row has no data")
    refusal_arguments = data.get("arguments")
    if not isinstance(refusal_arguments, Mapping):
        raise SystemExit("the durable host refusal row has no arguments")
    refusal_route = _route(refusal_arguments, repository)
    checks = {
        "tool_name": data.get("tool_name"),
        "success": data.get("success"),
        "error": data.get("error"),
        "intent_id": refusal_route.get("dispatch_intent_id"),
        "configuration_fingerprint": refusal_route.get("configuration_fingerprint"),
        "route_fingerprint": refusal_route.get("route_fingerprint"),
        "expected_invocation_id": refusal_arguments.get("expected_invocation_id"),
        "expected_branch": refusal_arguments.get("expected_branch"),
        "expected_worktree_fingerprint": refusal_arguments.get(
            "expected_worktree_fingerprint"
        ),
        "human_approved": refusal_arguments.get("human_approved"),
    }
    expected = {
        "tool_name": TOOL,
        "success": False,
        "error": EXACT_ERROR,
        "intent_id": intent,
        "configuration_fingerprint": configuration,
        "route_fingerprint": route_fingerprint,
        "expected_invocation_id": arguments.get("expected_invocation_id"),
        "expected_branch": arguments.get("expected_branch"),
        "expected_worktree_fingerprint": arguments.get("expected_worktree_fingerprint"),
        "human_approved": True,
    }
    mismatched = [key for key, value in expected.items() if checks.get(key) != value]
    if mismatched:
        raise SystemExit(
            "the durable host refusal does not reproduce the refused authorization: "
            + ", ".join(mismatched)
        )
    if _launch_rows(repository, intent):
        raise SystemExit("the refused old dispatch has a launch row")
    print(
        json.dumps(
            {
                "refusal_run_seq": refusal.get("run_seq"),
                "intent_id": intent,
                "configuration_fingerprint": configuration,
                "route_fingerprint": route_fingerprint,
                "error": error,
            }
        )
    )


if __name__ == "__main__":
    main()
