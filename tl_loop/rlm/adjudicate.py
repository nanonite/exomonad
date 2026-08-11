"""Bounded review adjudication with policy gates outside the model."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.state.schema import Verdict

from .call import rlm
from .review_contract import (
    ADJUDICATION_SCHEMA,
    AdjudicationError,
    AdjudicationInputError,
    AdjudicationResult,
    AdjudicationValidationError,
    NitRecordingError,
    ReviewedHeadMismatch,
    ReviewPolicy,
)
from .review_input import (
    DEFAULT_REVIEW_POLICY,
    diff_context,
    load_review_policy,
    policy_gates,
    require_json,
    resolve_policy,
)

ADJUDICATE_PROMPT = """Return one closed structured review judgment.

ANTI-PATTERNS (FIRST)
- Do not invent a fourth verdict or return prose outside the requested schema.
- Do not approve a change without echoing reviewed_head exactly.
- Do not adjudicate a diff that is absent; the diff section is authoritative.
- Do not choose a harness, model, budget, or policy gate outcome.

READ FIRST
- Read the complete diff, review comments, and acceptance criteria supplied.
- Treat the reviewed head as the exact commit being judged.

STEPS
- Classify findings as blocking, nit, or info with file, line, and claim.
- Use GO only when no blocking finding remains.
- Use GO-WITH-NITS only for mergeable non-blocking follow-up findings.
- Use NO-GO when a blocking finding remains.

VERIFY
- blocking_count equals the number of blocking reasons.
- reviewed_head is copied byte-for-byte from the supplied head.

DONE CRITERIA
- Return only verdict, reviewed_head, reasons, and blocking_count.
"""


def adjudicate_review(
    pr_diff: Mapping[str, object] | str,
    comments: JsonValue,
    criteria: JsonValue,
    reviewed_head: str,
    *,
    model_choice: object | None = None,
    policy: ReviewPolicy | Mapping[str, object] | None = None,
    policy_path: str = str(DEFAULT_REVIEW_POLICY),
    chainlink_issue_id: int | None = None,
    issue_commenter: object | Callable[[int, str], object] | None = None,
) -> AdjudicationResult:
    """Return a head-bound verdict after Python policy gates are evaluated."""
    if model_choice is None:
        raise AdjudicationInputError(
            "adjudicate_review requires an injected resolved model choice"
        )
    if not isinstance(reviewed_head, str) or not reviewed_head:
        raise AdjudicationInputError("reviewed_head must be a non-empty string")

    diff = diff_context(pr_diff, criteria)
    require_json(comments, "comments")
    require_json(criteria, "criteria")
    selected_policy, policy_source = resolve_policy(policy, policy_path)

    inputs = _adjudication_inputs(diff.payload, comments, criteria, reviewed_head)
    output = rlm("adjudicate_review", inputs, ADJUDICATION_SCHEMA, model_choice)
    result = _result(output, reviewed_head)
    gates = policy_gates(diff, selected_policy)
    if gates:
        result = _apply_policy_gates(result, gates, policy_source)
    if result.verdict is Verdict.GO_WITH_NITS:
        _record_nits(result, chainlink_issue_id, issue_commenter)
    return result


def _adjudication_inputs(
    diff: JsonValue,
    comments: JsonValue,
    criteria: JsonValue,
    reviewed_head: str,
) -> JsonObject:
    sections: list[JsonObject] = [
        {
            "name": "diff",
            "content": copy.deepcopy(diff),
            "priority": 110,
            "required": True,
        },
        {
            "name": "instructions",
            "content": ADJUDICATE_PROMPT,
            "priority": 100,
            "required": True,
        },
        {
            "name": "criteria",
            "content": copy.deepcopy(criteria),
            "priority": 90,
            "required": True,
        },
        {
            "name": "comments",
            "content": copy.deepcopy(comments),
            "priority": 80,
            "required": False,
        },
        {
            "name": "reviewed_head",
            "content": reviewed_head,
            "priority": 120,
            "required": True,
        },
    ]
    return {"sections": cast(JsonValue, sections)}


def _result(output: JsonObject, expected_head: str) -> AdjudicationResult:
    raw_verdict = output["verdict"]
    raw_head = output["reviewed_head"]
    raw_reasons = output["reasons"]
    raw_blocking = output["blocking_count"]
    if not isinstance(raw_verdict, str):
        raise AdjudicationValidationError("verdict is outside the closed enum")
    if not isinstance(raw_head, str):
        raise AdjudicationValidationError("reviewed_head must be a string")
    if raw_head != expected_head:
        raise ReviewedHeadMismatch(
            f"adjudicator reviewed {raw_head!r}, expected {expected_head!r}"
        )
    if not isinstance(raw_reasons, list):
        raise AdjudicationValidationError("reasons must be an array")
    reasons = tuple(_reason(item, index) for index, item in enumerate(raw_reasons))
    if type(raw_blocking) is not int or raw_blocking < 0:
        raise AdjudicationValidationError(
            "blocking_count must be a non-negative integer"
        )
    blocking_count = sum(
        reason["severity"] == "blocking" for reason in reasons
    )
    if raw_blocking != blocking_count:
        raise AdjudicationValidationError(
            "blocking_count does not match blocking reasons"
        )
    try:
        verdict = Verdict(raw_verdict)
    except ValueError as error:
        raise AdjudicationValidationError(
            "verdict is outside the closed enum"
        ) from error
    if verdict in {Verdict.GO, Verdict.GO_WITH_NITS} and blocking_count:
        raise AdjudicationValidationError(
            f"{verdict.value} cannot contain blocking reasons"
        )
    return AdjudicationResult(
        verdict=verdict,
        reviewed_head=raw_head,
        reasons=reasons,
        blocking_count=blocking_count,
        second_review_required=False,
        mergeable=verdict in {Verdict.GO, Verdict.GO_WITH_NITS},
    )


def _reason(value: object, index: int) -> JsonObject:
    if not isinstance(value, Mapping):
        raise AdjudicationValidationError(f"reasons[{index}] must be an object")
    required = {"severity", "file", "line", "claim"}
    if set(value) != required:
        raise AdjudicationValidationError(
            f"reasons[{index}] must contain exactly {sorted(required)}"
        )
    severity = value["severity"]
    file_name = value["file"]
    line = value["line"]
    claim = value["claim"]
    if not isinstance(severity, str) or severity not in {"blocking", "nit", "info"}:
        raise AdjudicationValidationError(f"reasons[{index}].severity is invalid")
    if not isinstance(file_name, str) or not file_name:
        raise AdjudicationValidationError(f"reasons[{index}].file is invalid")
    if type(line) is not int or line < 0:
        raise AdjudicationValidationError(f"reasons[{index}].line is invalid")
    if not isinstance(claim, str) or not claim:
        raise AdjudicationValidationError(f"reasons[{index}].claim is invalid")
    return {
        "severity": severity,
        "file": file_name,
        "line": line,
        "claim": claim,
    }


def _apply_policy_gates(
    result: AdjudicationResult,
    gates: Sequence[str],
    policy_source: str,
) -> AdjudicationResult:
    policy_reasons: tuple[JsonObject, ...] = tuple(
        {
            "severity": "info",
            "file": policy_source,
            "line": 0,
            "claim": f"Second review required: {gate}",
        }
        for gate in gates
    )
    reasons = (*result.reasons, *policy_reasons)
    return AdjudicationResult(
        verdict=result.verdict,
        reviewed_head=result.reviewed_head,
        reasons=reasons,
        blocking_count=result.blocking_count,
        second_review_required=True,
        mergeable=False if result.verdict is Verdict.GO else result.mergeable,
    )


def _record_nits(
    result: AdjudicationResult,
    issue_id: int | None,
    commenter: object | Callable[[int, str], object] | None,
) -> None:
    nits = [reason for reason in result.reasons if reason.get("severity") == "nit"]
    if not nits:
        raise NitRecordingError(
            "GO-WITH-NITS must include at least one nit reason"
        )
    if issue_id is None or issue_id <= 0 or commenter is None:
        raise NitRecordingError(
            "GO-WITH-NITS requires a positive Chainlink issue and commenter"
        )
    message = _nit_message(result.reviewed_head, nits)
    if callable(commenter) and not hasattr(commenter, "chainlink_issue_comment"):
        outcome = commenter(issue_id, message)
    else:
        method = getattr(commenter, "chainlink_issue_comment", None)
        if not callable(method):
            raise NitRecordingError("issue commenter has no comment capability")
        outcome = method(issue_id=issue_id, message=message)
    if isinstance(outcome, ToolResult) and outcome.success is not True:
        raise NitRecordingError(outcome.error or "Chainlink nit comment failed")
    if outcome is False:
        raise NitRecordingError("Chainlink nit comment failed")


def _nit_message(head: str, nits: Sequence[JsonObject]) -> str:
    lines = [f"GO-WITH-NITS follow-up for reviewed head {head}:"]
    lines.extend(
        f"- {reason['file']}:{reason['line']}: {reason['claim']}"
        for reason in nits
    )
    return chr(10).join(lines)


__all__ = [
    "ADJUDICATE_PROMPT",
    "ADJUDICATION_SCHEMA",
    "DEFAULT_REVIEW_POLICY",
    "AdjudicationError",
    "AdjudicationInputError",
    "AdjudicationResult",
    "AdjudicationValidationError",
    "NitRecordingError",
    "ReviewPolicy",
    "ReviewedHeadMismatch",
    "adjudicate_review",
    "load_review_policy",
]
