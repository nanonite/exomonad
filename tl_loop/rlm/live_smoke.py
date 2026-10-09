"""Opt-in live smoke checks for every installed subprocess RLM harness.

Claude and Codex require explicit model IDs. OpenCode cycles through its
published free Zen and Go model IDs, trying fallbacks until one works per
provider. Each successful check makes one tiny judgment.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Mapping, Sequence

from .call import RlmCallError, RlmConfigurationError
from .store import RlmRequest
from .subprocess_backend import SUPPORTED_BACKEND_HARNESSES, SubprocessRlmBackend

_EXPECTED_OUTPUT = {"sum": 5}
_LIVE_SMOKE_TIMEOUT_SECONDS = 120.0
# Free IDs from the OpenCode Zen and Go pricing catalogs. Go's two preview
# models are explicitly time-limited; keep paid Go IDs out of this allowlist.
_OPENCODE_FREE_MODEL_GROUPS = (
    (
        "Zen",
        "opencode/",
        (
            "opencode/big-pickle",
            "opencode/space-bunny-free",
            "opencode/longcat-2.5-preview-free",
            "opencode/step-5-preview-free",
            "opencode/mimo-v2.6-flash-free",
            "opencode/mimo-v2.5-free",
            "opencode/ling-3.1-flash-free",
            "opencode/ling-3.0-flash-fin-free",
            "opencode/nemotron-3-ultra-free",
            "opencode/nemotron-3.5-lightning-free",
            "opencode/muse-spark-1.3-contributor-free",
        ),
    ),
    (
        "Go",
        "opencode-go/",
        (
            "opencode-go/longcat-2.5-preview-free",
            "opencode-go/step-5-preview-free",
        ),
    ),
)
_OPENCODE_FREE_MODELS = frozenset(
    model_id
    for _provider, _prefix, model_ids in _OPENCODE_FREE_MODEL_GROUPS
    for model_id in model_ids
)


def installed_harnesses() -> tuple[str, ...]:
    """Return supported harnesses whose CLIs are available on PATH."""
    return tuple(harness for harness in SUPPORTED_BACKEND_HARNESSES if shutil.which(harness))


def model_environment_name(harness: str) -> str:
    """Return the environment variable that selects one harness's live models."""
    suffix = "MODELS" if harness == "opencode" else "MODEL"
    return f"EXOMONAD_TL_SMOKE_{harness.upper()}_{suffix}"


def _require_explicit_models(harnesses: Sequence[str], source: Mapping[str, str]) -> None:
    missing = [
        model_environment_name(harness)
        for harness in harnesses
        if harness != "opencode" and not source.get(model_environment_name(harness), "").strip()
    ]
    if missing:
        raise ValueError(
            "Set a model ID for every installed Claude/Codex harness before "
            "running the live smoke: " + ", ".join(missing)
        )


def _configured_opencode_models(source: Mapping[str, str]) -> tuple[str, ...]:
    variable = model_environment_name("opencode")
    configured = source.get(variable, "").strip()
    if not configured:
        return tuple(model_id for _, _, group in _OPENCODE_FREE_MODEL_GROUPS for model_id in group)

    selected = tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))
    unsupported = [model_id for model_id in selected if model_id not in _OPENCODE_FREE_MODELS]
    if unsupported:
        raise ValueError(
            f"{variable} only accepts published free OpenCode Zen/Go model IDs; "
            "rejected: " + ", ".join(unsupported)
        )
    if not selected:
        raise ValueError(f"{variable} must contain at least one free model ID")
    return selected


def configured_models(
    harnesses: Sequence[str], environment: Mapping[str, str] | None = None
) -> dict[str, tuple[str, ...]]:
    """Validate all model choices before making any live calls."""
    source = os.environ if environment is None else environment
    _require_explicit_models(harnesses, source)
    return {
        harness: _configured_opencode_models(source)
        if harness == "opencode"
        else (source[model_environment_name(harness)].strip(),)
        for harness in harnesses
    }


def opencode_model_groups(
    model_ids: Sequence[str],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Group the selected free model IDs by OpenCode provider."""
    return tuple(
        (provider, tuple(model_id for model_id in model_ids if model_id.startswith(prefix)))
        for provider, prefix, _catalog in _OPENCODE_FREE_MODEL_GROUPS
        if any(model_id.startswith(prefix) for model_id in model_ids)
    )


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
        raise RlmCallError(f"{harness} returned {response.output!r}; expected {_EXPECTED_OUTPUT!r}")
    if response.input_tokens < 1 or response.output_tokens < 1:
        raise RlmCallError(f"{harness} returned zero token usage")
    return response.input_tokens, response.output_tokens, response.latency_ms


def _parse_harness_arguments(argv: Sequence[str] | None) -> Sequence[str] | None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--harness",
        action="append",
        choices=SUPPORTED_BACKEND_HARNESSES,
        help="test only this harness (repeat to select more than one)",
    )
    return parser.parse_args(argv).harness


def _select_harnesses(requested: Sequence[str] | None, installed: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(requested or installed))
    unavailable = [harness for harness in selected if harness not in installed]
    if unavailable:
        raise ValueError("Requested harness CLI(s) not found on PATH: " + ", ".join(unavailable))
    return selected


def _run_opencode_provider(provider: str, model_ids: Sequence[str]) -> bool:
    for model_id in model_ids:
        try:
            input_tokens, output_tokens, latency_ms = run_smoke("opencode", model_id)
        except (RlmCallError, RlmConfigurationError) as error:
            print(f"RETRY opencode provider={provider} model={model_id}: {error}", file=sys.stderr)
            continue
        print(
            f"PASS opencode provider={provider} model={model_id} "
            f"input_tokens={input_tokens} output_tokens={output_tokens} "
            f"latency_ms={latency_ms}"
        )
        return True

    attempted = ", ".join(model_ids)
    print(f"FAIL opencode provider={provider}: no free model passed ({attempted})", file=sys.stderr)
    return False


def _run_harness(harness: str, model_ids: Sequence[str]) -> tuple[str, ...]:
    if harness == "opencode":
        return tuple(
            f"{harness}/{provider}"
            for provider, candidates in opencode_model_groups(model_ids)
            if not _run_opencode_provider(provider, candidates)
        )

    model_id = model_ids[0]
    try:
        input_tokens, output_tokens, latency_ms = run_smoke(harness, model_id)
    except (RlmCallError, RlmConfigurationError) as error:
        print(f"FAIL {harness}: {error}", file=sys.stderr)
        return (harness,)
    print(
        f"PASS {harness} model={model_id} input_tokens={input_tokens} "
        f"output_tokens={output_tokens} latency_ms={latency_ms}"
    )
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    """Run live judgments for installed harnesses, with optional filtering."""
    try:
        requested = _parse_harness_arguments(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        harnesses = _select_harnesses(requested, installed_harnesses())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not harnesses:
        print("No supported harness CLIs found on PATH.", file=sys.stderr)
        return 2
    try:
        models = configured_models(harnesses)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    failures = tuple(
        failure for harness in harnesses for failure in _run_harness(harness, models[harness])
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
