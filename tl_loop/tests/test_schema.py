"""Closed-key and cross-field validation coverage for TL run state."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import SCHEMA_VERSION, SchemaError, validate


def test_valid_run_state_document_is_accepted() -> None:
    validate(_valid_document())


def test_per_head_review_state_is_valid_and_closed() -> None:
    document = _valid_document()
    slice_state = _slice(document, "slice-a")
    slice_state["review_findings"] = {
        "head-a": [
            {
                "severity": "blocking",
                "path": "src/a.py",
                "rationale": "The error path is unhandled",
            }
        ]
    }
    slice_state["ci_state"] = {"head-a": "success"}
    slice_state["reviewer_attempt"] = {"head-a": 2}
    slice_state["repair_attempts"] = 1
    validate(document)

    unknown = _valid_document()
    unknown_slice = _slice(unknown, "slice-a")
    unknown_slice["review_findings"] = {}
    findings = cast(dict[str, object], unknown_slice["review_findings"])
    findings["head-a"] = [{"severity": "info", "path": "src/a.py", "rationale": "covered"}]
    cast(list[dict[str, object]], findings["head-a"])[0]["extra"] = True
    _assert_rejected(unknown, "run.slices['slice-a'].review_findings['head-a'][0]")


def test_per_head_review_state_rejects_invalid_values() -> None:
    invalid_ci = _valid_document()
    _slice(invalid_ci, "slice-a")["ci_state"] = {"head-a": "passed"}
    _assert_rejected(invalid_ci, "run.slices['slice-a'].ci_state['head-a']")

    invalid_attempt = _valid_document()
    _slice(invalid_attempt, "slice-a")["reviewer_attempt"] = {"head-a": -1}
    _assert_rejected(invalid_attempt, "run.slices['slice-a'].reviewer_attempt['head-a']")

    invalid_repair = _valid_document()
    _slice(invalid_repair, "slice-a")["repair_attempts"] = -1
    _assert_rejected(invalid_repair, "run.slices['slice-a'].repair_attempts")


def test_unknown_keys_are_rejected_at_every_nesting_level() -> None:
    cases: list[tuple[str, dict[str, object]]] = []

    root = _valid_document()
    root["unknown"] = True
    cases.append(("run", root))

    fsm = _valid_document()
    cast(dict[str, object], fsm["fsm"])["unknown"] = True
    cases.append(("run.fsm", fsm))

    slice_state = _valid_document()
    _slice(slice_state, "slice-a")["unknown"] = True
    cases.append(("run.slices['slice-a']", slice_state))

    budgets = _valid_document()
    cast(dict[str, object], budgets["budgets"])["unknown"] = True
    cases.append(("run.budgets", budgets))

    ledger = _valid_document()
    cast(dict[str, object], cast(dict[str, object], ledger["budgets"])["ledger"])["unknown"] = True
    cases.append(("run.budgets.ledger", ledger))

    gates = _valid_document()
    cast(list[dict[str, object]], gates["gates"])[0]["unknown"] = True
    cases.append(("run.gates[0]", gates))

    events = _valid_document()
    cast(dict[str, object], events["events"])["unknown"] = True
    cases.append(("run.events", events))

    for path, document in cases:
        _assert_rejected(document, path)


def test_enum_values_are_closed() -> None:
    phase = _valid_document()
    cast(dict[str, object], phase["fsm"])["phase"] = "unknown-phase"
    _assert_rejected(phase, "run.fsm.phase")

    status = _valid_document()
    _slice(status, "slice-a")["status"] = "unknown-status"
    _assert_rejected(status, "run.slices['slice-a'].status")

    verdict = _valid_document()
    _slice(verdict, "slice-a")["verdict"] = "maybe"
    _assert_rejected(verdict, "run.slices['slice-a'].verdict")

    gate = _valid_document()
    cast(list[dict[str, object]], gate["gates"])[0]["status"] = "unknown-gate"
    _assert_rejected(gate, "run.gates[0].status")


def test_wrong_types_are_rejected_with_qualified_paths() -> None:
    document = _valid_document()
    document["revision"] = False
    cast(dict[str, object], document["events"])["last_consumed_offset"] = "zero"
    errors = _rejection(document)
    assert "run.revision" in errors
    assert "run.events.last_consumed_offset" in errors


def test_spawned_slice_requires_authoritative_dispatch_evidence() -> None:
    document = _valid_document()
    slice_state = _slice(document, "slice-a")
    slice_state.update(
        {
            "status": "spawned",
            "dispatch_intent_id": "intent-1",
            "dispatch_agent_id": "agent-1",
            "dispatch_authoritative_event_seq": 7,
        }
    )
    validate(document)

    missing_evidence = deepcopy(document)
    missing_slice = _slice(missing_evidence, "slice-a")
    missing_slice["dispatch_intent_id"] = None
    missing_slice["dispatch_authoritative_event_seq"] = None
    _assert_rejected(missing_evidence, "dispatch_intent_id")


def test_merge_evidence_is_typed_and_exact_head_bound() -> None:
    document = _valid_document()
    document["state_version"] = 3
    document["repository_identity"] = {
        "owner": "acme",
        "repo": "exomonad",
        "base_branch": "main",
        "remote_url": "https://forgejo.local/acme/exomonad",
    }
    _slice(document, "slice-a").update(
        {
            "publication": {
                "pr_number": 42,
                "head_sha": "head-a",
                "head_branch": "task/a",
                "base_branch": "main",
                "attempt": 1,
                "invocation_id": "inv-1",
            },
            "handoff": {
                "pr_number": 42,
                "head_sha": "head-a",
                "attempt": 1,
                "invocation_id": "inv-1",
                "agent_id": "agent-a",
                "observed_at": "2026-08-24T00:00:00Z",
            },
            "observation_provenance": {
                "source": "watcher",
                "observed_at": "2026-08-24T00:00:01Z",
                "event_seq": 7,
                "snapshot_id": "snap-7",
            },
            "action": {
                "kind": "merge",
                "phase": "intended",
                "state_version": 2,
                "intent_id": "merge-intent",
                "head_sha": "head-a",
                "attempt": 1,
            },
        }
    )
    validate(document)

    stale_handoff = deepcopy(document)
    cast(dict[str, object], cast(dict[str, object], stale_handoff["slices"])["slice-a"])[
        "handoff"
    ] = {"pr_number": 42, "head_sha": "head-a"}
    _assert_rejected(stale_handoff, "run.slices['slice-a'].handoff")


def test_unknown_version_is_rejected_without_migration() -> None:
    document = _valid_document()
    document["version"] = 99
    _assert_rejected(document, "run.version")


def test_ordered_runtime_state_is_closed_and_validated() -> None:
    document = _valid_document()
    document.update(
        {
            "current_order": 2,
            "ordered_stages": [
                {"order": 1, "sub_tls": ["auth"]},
                {"order": 2, "sub_tls": ["docs"]},
            ],
            "integration": {
                "lifecycle": IntegrationLifecycle.READY_FOR_INTEGRATION.value,
                "sub_tl_states": {"auth": IntegrationLifecycle.MERGED.value},
                "aggregate_pr_number": 42,
                "aggregate_head_sha": "head-42",
                "aggregate_patch_digest": "patch-42",
                "aggregate_original_base_sha": "base-1",
                "integration_owner_id": "tl/root",
                "head_sha": "head-42",
                "patch_digest": "patch-42",
                "validated_base_sha": "base-1",
                "merge_tree_sha": "tree-42",
                "ci_status": "success",
                "merge_attempts": 0,
                "base_revalidation_count": 1,
                "stage_verification": "passed",
            },
        }
    )
    validate(document)

    candidates = {
        "auth": {
            "lifecycle": IntegrationLifecycle.READY_FOR_INTEGRATION.value,
            "aggregate_pr_number": 42,
            "aggregate_head_sha": "head-auth",
            "aggregate_patch_digest": "patch-auth",
            "aggregate_original_base_sha": "base-1",
            "integration_owner_id": "tl/auth",
            "head_sha": "head-auth",
            "patch_digest": "patch-auth",
            "validated_base_sha": "base-1",
            "merge_tree_sha": "tree-auth",
            "integration_evidence_at": "2026-01-01T00:00:00Z",
            "ci_status": "success",
            "merge_attempts": 1,
            "base_revalidation_count": 0,
            "stage_verification": "passed",
        },
        "docs": {
            "lifecycle": IntegrationLifecycle.READY_FOR_INTEGRATION.value,
            "aggregate_pr_number": 43,
            "aggregate_head_sha": "head-docs",
            "aggregate_patch_digest": "patch-docs",
            "aggregate_original_base_sha": "base-1",
            "integration_owner_id": "tl/docs",
            "head_sha": "head-docs",
            "patch_digest": "patch-docs",
            "validated_base_sha": "base-1",
            "merge_tree_sha": "tree-docs",
            "integration_evidence_at": "2026-01-01T00:00:00Z",
            "ci_status": "success",
            "merge_attempts": 1,
            "base_revalidation_count": 0,
            "stage_verification": "passed",
        },
    }
    cast(dict[str, object], document["integration"])["candidates"] = candidates
    validate(document)

    invalid_candidate = deepcopy(document)
    candidate_map = cast(
        dict[str, object], cast(dict[str, object], invalid_candidate["integration"])["candidates"]
    )
    cast(dict[str, object], candidate_map["auth"])["lifecycle"] = IntegrationLifecycle.MERGED.value
    cast(dict[str, object], candidate_map["auth"]).pop("integration_owner_id")
    _assert_rejected(invalid_candidate, "run.integration.candidates['auth'].integration_owner_id")

    invalid = deepcopy(document)
    cast(dict[str, object], invalid["integration"])["ci_status"] = "green"
    _assert_rejected(invalid, "run.integration.ci_status")


def test_overlapping_non_terminal_paths_are_rejected() -> None:
    document = _valid_document()
    _slice(document, "slice-a")["paths"] = ["src/shared/main.py"]
    slices = cast(dict[str, object], document["slices"])
    slices["slice-b"] = _slice_record(
        "slice-b",
        paths=["src/shared/*.py"],
    )
    _assert_rejected(document, "run.slices['slice-b'].paths")


def test_terminal_slice_paths_do_not_conflict_with_active_ownership() -> None:
    document = _valid_document()
    slices = cast(dict[str, object], document["slices"])
    slices["slice-b"] = _slice_record(
        "slice-b",
        status="merged",
        paths=["src/a.py"],
    )
    validate(document)


def test_dependencies_must_exist_and_be_acyclic() -> None:
    document = _valid_document()
    _slice(document, "slice-a")["depends_on"] = ["slice-b"]
    _assert_rejected(document, "unknown slice 'slice-b'")

    cyclic = _valid_document()
    slices = cast(dict[str, object], cyclic["slices"])
    _slice(cyclic, "slice-a")["depends_on"] = ["slice-b"]
    slices["slice-b"] = _slice_record("slice-b", depends_on=["slice-a"], paths=["src/b.py"])
    _assert_rejected(cyclic, "depends_on cycle")


def test_waiting_ids_must_reference_slices_without_duplicates() -> None:
    unknown = _valid_document()
    cast(dict[str, object], unknown["fsm"])["waiting"] = ["missing"]
    _assert_rejected(unknown, "run.fsm.waiting[0]")

    duplicate = _valid_document()
    cast(dict[str, object], duplicate["fsm"])["waiting"] = ["slice-a", "slice-a"]
    _assert_rejected(duplicate, "run.fsm.waiting")


def test_review_policy_snapshot_accepts_only_producer_values() -> None:
    for ceiling, source in (
        (3, "environment"),
        (3, "policy_file"),
        (None, "disabled"),
    ):
        document = _valid_document()
        document["reviewer_max_rounds"] = ceiling
        document["reviewer_max_rounds_source"] = source
        validate(document)

    # Both keys absent is the only supported pre-policy legacy shape.
    validate(_valid_document())

    invalid_pairs = (
        (3, None),
        (None, "environment"),
        (None, "policy_file"),
        (None, None),
        (3, "disabled"),
        (0, "environment"),
        (-1, "policy_file"),
        (True, "environment"),
        ("3", "environment"),
        (1.5, "environment"),
        (3, "banana"),
    )
    for ceiling, source in invalid_pairs:
        document = _valid_document()
        if ceiling is not None or source is None:
            document["reviewer_max_rounds"] = ceiling
        if source is not None or ceiling is None:
            document["reviewer_max_rounds_source"] = source
        message = _rejection(document)
        assert "reviewer_max_rounds" in message
        assert "reviewer_max_rounds_source" in message


def _valid_document() -> dict[str, object]:
    return {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "run-1",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {"slice-a": _slice_record("slice-a")},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [{"name": "plan", "status": "pending"}],
        "events": {"last_consumed_offset": 0},
    }


def _slice_record(
    slice_id: str,
    *,
    status: str = "pending",
    paths: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": slice_id,
        "status": status,
        "paths": paths or [f"src/{slice_id}.py"],
        "depends_on": depends_on or [],
        "base_ref": None,
        "test_plan": ["just tl-loop-test"],
        "agent_type": None,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }


def _slice(document: dict[str, object], slice_id: str) -> dict[str, object]:
    slices = cast(dict[str, object], document["slices"])
    return cast(dict[str, object], slices[slice_id])


def _assert_rejected(document: dict[str, object], expected: str) -> None:
    assert expected in _rejection(document)


def _rejection(document: dict[str, object]) -> str:
    try:
        validate(deepcopy(document))
    except SchemaError as error:
        return str(error)
    raise AssertionError("expected SchemaError")
