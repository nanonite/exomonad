"""Bounded RLM composition and resume dispatch for PR repairs."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.state.schema import Verdict
from tl_loop.state.store import RunStore

from .call import rlm
from .repair_contract import (
    REPAIR_HANDOFF_FIELDS,
    REPAIR_HANDOFF_SCHEMA,
    RepairBoundaryError,
    RepairDispatchError,
    RepairError,
    RepairHandoff,
    RepairHandoffRejected,
    RepairInputError,
    RepairPRStateError,
)
from .repair_input import (
    increment_attempts,
    member,
    pr_identity,
    repair_max_attempts,
    review_inputs,
    validate_owned_paths,
    watch_existing_pr,
)
from .review_contract import AdjudicationResult

REPAIR_PROMPT = """Compose one complete repair handoff for the existing PR.

ANTI-PATTERNS (FIRST)
- Do not create a new branch, leaf name, agent type, or PR.
- Do not propose edits outside the supplied slice paths.
- Do not omit any required handoff section or return prose outside the schema.
- Do not replace the review's NO-GO reasons with a new scope or silent fallback.

READ FIRST
- Read the NO-GO reasons as the primary diagnosis.
- Read only the supplied slice-owned paths and the exact PR context.

STEPS
- State the root cause precisely.
- Propose the smallest complete solution within the owned paths.
- List exact files to read, implementation steps, verification commands,
  boundary constraints, and done criteria.

VERIFY
- Every section is non-empty.
- Every path reference belongs to the supplied slice boundary.
- The existing PR owner is resumed through resume_pr.

DONE CRITERIA
- Return only the seven-section RepairHandoff schema.
"""


def compose_repair(
    pr: Mapping[str, object] | object,
    verdict: Verdict | str,
    review: Mapping[str, object] | AdjudicationResult | object,
    *,
    client: EffectClient | object | None = None,
    model_choice: object | None = None,
    store: RunStore | None = None,
    slice_id: str | None = None,
    model: str | None = None,
    dispatch: Callable[[JsonObject], object] | None = None,
) -> RepairHandoff:
    """Compose, validate, dispatch, and account for one existing-PR repair."""
    number, paths = pr_identity(pr)
    selected_client = client if client is not None else member(pr, "client")
    selected_model = (
        model_choice if model_choice is not None else member(pr, "model_choice")
    )
    if selected_client is None or selected_model is None:
        raise RepairInputError(
            "compose_repair requires an effect client and resolved model choice"
        )

    watcher = watch_existing_pr(selected_client, number)
    reasons = review_inputs(verdict, review)
    attempts = repair_max_attempts(selected_model)
    feedback: tuple[str, ...] = ()

    for attempt in range(1, attempts + 1):
        inputs = _repair_inputs(number, paths, watcher, reasons, feedback)
        output = rlm("compose_repair", inputs, REPAIR_HANDOFF_SCHEMA, selected_model)
        try:
            handoff = RepairHandoff.from_mapping(output)
            validate_owned_paths(handoff, paths)
        except (RepairBoundaryError, RepairInputError) as error:
            feedback = (str(error),)
            if attempt == attempts:
                raise RepairHandoffRejected(attempts, feedback) from error
            continue
        dispatch_resume(selected_client, number, handoff, model, dispatch=dispatch)
        increment_attempts(pr, number, store, slice_id)
        return handoff

    raise RepairHandoffRejected(attempts, feedback)


def forward_review_findings(
    pr: Mapping[str, object] | object,
    verdict: Verdict | str,
    review: Mapping[str, object] | AdjudicationResult | object,
    *,
    client: EffectClient | object,
    test_plan: Sequence[str],
    store: RunStore | None = None,
    slice_id: str | None = None,
    model: str | None = None,
    dispatch: Callable[[JsonObject], object] | None = None,
) -> RepairHandoff:
    """Forward a NO-GO's findings verbatim to the existing PR owner, with no model.

    This is the inner worker<->reviewer loop's repair: the reviewer's reasons
    become the handoff's ROOT CAUSE unchanged, the slice's own boundary and test
    plan supply the remaining sections, and ``resume_pr`` resumes the same owner.
    """
    number, paths = pr_identity(pr)
    watch_existing_pr(client, number)
    reasons = review_inputs(verdict, review)
    handoff = findings_handoff(reasons, paths, test_plan)
    dispatch_resume(client, number, handoff, model, dispatch=dispatch)
    increment_attempts(pr, number, store, slice_id)
    return handoff


def dispatch_guide_handoff(
    pr: Mapping[str, object] | object,
    handoff: RepairHandoff,
    *,
    client: EffectClient | object,
    store: RunStore | None = None,
    slice_id: str | None = None,
    model: str | None = None,
    dispatch: Callable[[JsonObject], object] | None = None,
) -> RepairHandoff:
    """Dispatch a TL-authored handoff to the existing PR owner after boundary validation."""
    number, paths = pr_identity(pr)
    watch_existing_pr(client, number)
    validate_owned_paths(handoff, paths)
    dispatch_resume(client, number, handoff, model, dispatch=dispatch)
    increment_attempts(pr, number, store, slice_id)
    return handoff


def findings_handoff(
    reasons: Sequence[Mapping[str, object]],
    paths: Sequence[str],
    test_plan: Sequence[str],
) -> RepairHandoff:
    """Build the seven-section handoff whose ROOT CAUSE is the reviewer's findings."""
    if not test_plan:
        raise RepairInputError("a findings handoff requires the slice test plan")
    findings = [_finding_line(reason) for reason in reasons]
    return RepairHandoff(
        root_cause="Reviewer findings, verbatim:\n" + "\n".join(findings),
        proposed_solution=(
            "Resolve every reviewer finding listed under ROOT CAUSE on the existing PR branch."
        ),
        read_first=tuple(paths),
        steps=tuple(f"Resolve: {line.removeprefix('- ')}" for line in findings),
        verify=tuple(test_plan),
        boundary=("Only edit files under the slice-owned paths: " + ", ".join(paths),),
        done_criteria=(
            "Every finding listed under ROOT CAUSE is resolved on the PR head",
            "The verify commands pass",
        ),
    )


def _finding_line(reason: Mapping[str, object]) -> str:
    location = str(reason.get("file", "review"))
    line = reason.get("line")
    if type(line) is int and line > 0:
        location = f"{location}:{line}"
    return f"- [{reason.get('severity', 'blocking')}] {location}: {reason.get('claim', '')}"


def _repair_inputs(
    number: int,
    paths: Sequence[str],
    watcher: Mapping[str, object],
    reasons: Sequence[JsonObject],
    feedback: Sequence[str],
) -> JsonObject:
    sections: list[JsonObject] = [
        {
            "name": "no_go_reasons",
            "content": cast(JsonValue, copy.deepcopy(list(reasons))),
            "priority": 130,
            "required": True,
        },
        {
            "name": "slice_boundary",
            "content": list(paths),
            "priority": 125,
            "required": True,
        },
        {
            "name": "instructions",
            "content": REPAIR_PROMPT,
            "priority": 120,
            "required": True,
        },
        {
            "name": "pr_context",
            "content": cast(
                JsonValue,
                {
                    "pr_number": number,
                    "head_branch": watcher["head_branch"],
                    "head_sha": watcher["head_sha"],
                },
            ),
            "priority": 110,
            "required": True,
        },
    ]
    if feedback:
        sections.insert(
            0,
            {
                "name": "validation_feedback",
                "content": chr(10).join(feedback),
                "priority": 140,
                "required": True,
            },
        )
    return {"sections": cast(JsonValue, sections)}


def dispatch_resume(
    client: object,
    number: int,
    handoff: RepairHandoff,
    model: str | None,
    *,
    dispatch: Callable[[JsonObject], object] | None = None,
) -> None:
    resume = getattr(client, "resume_pr", None)
    if not callable(resume):
        raise RepairInputError("client has no resume_pr capability")
    kwargs: JsonObject = {
        "pr_number": number,
        "task": handoff.proposed_solution,
        "context": (
            f"ROOT CAUSE: {handoff.root_cause}\n"
            f"PROPOSED SOLUTION: {handoff.proposed_solution}"
        ),
        "read_first": list(handoff.read_first),
        "steps": list(handoff.steps),
        "verify": list(handoff.verify),
        "boundary": list(handoff.boundary),
        "done_criteria": list(handoff.done_criteria),
    }
    if model is not None:
        kwargs["model"] = model
    if dispatch is not None:
        outcome = dispatch(kwargs)
        if isinstance(outcome, ToolResult) and outcome.success is False:
            raise RepairDispatchError(outcome.error or "resume_pr failed")
        if isinstance(outcome, Mapping) and outcome.get("success") is False:
            raise RepairDispatchError(
                cast(str, outcome.get("error") or "resume_pr failed")
            )
        return
    parameters = inspect.signature(resume).parameters
    if "handoff" in parameters and "task" not in parameters:
        outcome = resume(number, handoff.to_mapping())
    else:
        outcome = resume(**kwargs)
    if isinstance(outcome, ToolResult) and outcome.success is False:
        raise RepairDispatchError(outcome.error or "resume_pr failed")
    if isinstance(outcome, Mapping) and outcome.get("success") is False:
        raise RepairDispatchError(
            cast(str, outcome.get("error") or "resume_pr failed")
        )


__all__ = [
    "REPAIR_HANDOFF_FIELDS",
    "REPAIR_HANDOFF_SCHEMA",
    "REPAIR_PROMPT",
    "RepairBoundaryError",
    "RepairDispatchError",
    "RepairError",
    "RepairHandoff",
    "RepairHandoffRejected",
    "RepairInputError",
    "RepairPRStateError",
    "compose_repair",
    "dispatch_guide_handoff",
    "dispatch_resume",
    "findings_handoff",
    "forward_review_findings",
]
