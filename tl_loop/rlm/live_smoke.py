"""Opt-in live smoke checks for every installed subprocess RLM harness.

Set EXOMONAD_TL_SMOKE_{CLAUDE,CODEX,OPENCODE}_MODEL for each installed CLI,
then run ``just tl-loop-live-smoke``. Each harness receives one tiny judgment.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence

from .call import RlmCallError, RlmConfigurationError
from .store import RlmRequest
from .subprocess_backend import SUPPORTED_BACKEND_HARNESSES, SubprocessRlmBackend

_EXPECTED_OUTPUT = {"sum": 5}
_LIVE_SMOKE_TIMEOUT_SECONDS = 120.0


def installed_harnesses() -> tuple[str, ...]:
    """Return supported harnesses whose CLIs are available on PATH."""
    return tuple(
        harness for harness in SUPPORTED_BACKEND_HARNESSES if shutil.which(harness)
    )


def model_environment_name(harness: str) -> str:
    """Return the environment variable that selects one harness's live model."""
    return f"EXOMONAD_TL_SMOKE_{harness.upper()}_MODEL"


def configured_models(
    harnesses: Sequence[str], environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Require explicit model IDs for all installed harnesses before making calls."""
    source = os.environ if environment is None else environment
    missing = [
        model_environment_name(harness)
        for harness in harnesses
        if not source.get(model_environment_name(harness), "").strip()
    ]
    if missing:
        raise ValueError(
            "Set model IDs for every installed harness before running the live smoke: "
            + ", ".join(missing)
        )
    return {
        harness: source[model_environment_name(harness)].strip()
        for harness in harnesses
    }


def smoke_request(model_id: str) -> RlmRequest:
    """Build a tiny deterministic addition judgment with an exact output schema."""
    return RlmRequest(
        name="live_cli_smoke",
        model_id=model_id,
        inputs={"left": 2, "right": 3},
        output_schema={
            "type": "object",
            "properties": {"sum": {"type": "integer"}},
            "required": ["sum"],
            "additionalProperties": False,
        },
        attempt=1,
    )


def run_smoke(harness: str, model_id: str) -> tuple[int, int, int]:
    """Run one live request and return its token counts and latency."""
    response = SubprocessRlmBackend(
        harness, model_id, timeout_seconds=_LIVE_SMOKE_TIMEOUT_SECONDS
    ).complete(smoke_request(model_id))
    if response.output != _EXPECTED_OUTPUT:
        raise RlmCallError(
            f"{harness} returned {response.output!r}; expected {_EXPECTED_OUTPUT!r}"
        )
    if response.input_tokens < 1 or response.output_tokens < 1:
        raise RlmCallError(f"{harness} returned zero token usage")
    return response.input_tokens, response.output_tokens, response.latency_ms


def main() -> int:
    """Run one live judgment for every installed supported harness."""
    harnesses = installed_harnesses()
    if not harnesses:
        print("No supported harness CLIs found on PATH.", file=sys.stderr)
        return 2
    try:
        models = configured_models(harnesses)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    failures: list[str] = []
    for harness in harnesses:
        try:
            input_tokens, output_tokens, latency_ms = run_smoke(
                harness, models[harness]
            )
        except (RlmCallError, RlmConfigurationError) as error:
            print(f"FAIL {harness}: {error}", file=sys.stderr)
            failures.append(harness)
            continue
        print(
            f"PASS {harness} model={models[harness]} input_tokens={input_tokens} "
            f"output_tokens={output_tokens} latency_ms={latency_ms}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
