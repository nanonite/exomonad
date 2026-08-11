"""Bounded, structured, budgeted, and replayable RLM judgments."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections.abc import Mapping
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue

from .schema import OutputSchemaError, validate_output
from .store import (
    RlmCallStore,
    RlmModelChoice,
    RlmRequest,
    RlmResponse,
    normalize_response,
)

MAX_ATTEMPTS = 3
_SENSITIVE_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")


class RlmError(RuntimeError):
    """Base class for structured RLM failures."""


class RlmConfigurationError(RlmError):
    """The supplied model choice does not expose a safe RLM backend."""


class RlmCallError(RlmError):
    """The tool-free backend failed to return a response."""


class JudgmentFailed(RlmError):
    """The model did not produce a schema-valid judgment within the bound."""

    def __init__(self, name: str, errors: tuple[str, ...]) -> None:
        self.name = name
        self.errors = errors
        super().__init__(
            f"RLM judgment {name!r} failed after {len(errors)} attempt(s): "
            + "; ".join(errors)
        )


def rlm(
    name: str,
    inputs: Mapping[str, object],
    output_schema: Mapping[str, object],
    model_choice: object,
) -> JsonObject:
    """Perform one bounded, stateless, structured, tool-free LM judgment."""
    _validate_call_arguments(name, inputs, output_schema)
    choice = _coerce_model_choice(model_choice)
    request_inputs = copy.deepcopy(cast(JsonObject, dict(inputs)))
    request_schema = copy.deepcopy(dict(output_schema))
    input_hash = judgment_hash(name, request_inputs, request_schema, choice.model_id)

    replayed = choice.replay.get(input_hash)
    if replayed is not None:
        response = normalize_response(replayed)
        try:
            result = validate_output(response.output, request_schema)
        except OutputSchemaError as error:
            _record_failure(
                choice,
                name,
                input_hash,
                attempt=1,
                response=response,
                replayed=True,
                validation_error=str(error),
            )
            raise JudgmentFailed(name, (str(error),)) from error
        _record_success(choice, name, input_hash, 1, response, result, replayed=True)
        return result

    errors: list[str] = []
    for attempt in range(1, choice.max_attempts + 1):
        request = RlmRequest(
            name=name,
            model_id=choice.model_id,
            inputs=copy.deepcopy(request_inputs),
            output_schema=copy.deepcopy(request_schema),
            attempt=attempt,
            retry_error=errors[-1] if errors else None,
        )
        try:
            response = _invoke(choice.backend, request)
        except Exception as error:
            _record_failure(
                choice,
                name,
                input_hash,
                attempt,
                response=None,
                replayed=False,
                validation_error=None,
                call_error=type(error).__name__,
            )
            if isinstance(error, RlmError):
                raise
            raise RlmCallError(f"RLM backend failed for {name!r}: {type(error).__name__}") from error

        try:
            result = validate_output(response.output, request_schema)
        except OutputSchemaError as error:
            message = str(error)
            errors.append(message)
            _record_failure(
                choice,
                name,
                input_hash,
                attempt,
                response,
                replayed=False,
                validation_error=message,
            )
            if attempt == choice.max_attempts:
                raise JudgmentFailed(name, tuple(errors)) from error
            continue

        _record_success(choice, name, input_hash, attempt, response, result, replayed=False)
        choice.replay[input_hash] = response
        return result

    raise JudgmentFailed(name, tuple(errors))


def judgment_hash(
    name: str,
    inputs: Mapping[str, object],
    output_schema: Mapping[str, object],
    model_id: str,
) -> str:
    """Return the stable hash used to identify a replayable judgment."""
    payload = {
        "name": name,
        "inputs": inputs,
        "model": model_id,
        "output_schema": output_schema,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RlmConfigurationError(f"RLM inputs are not canonical JSON: {error}") from error
    return hashlib.sha256(encoded).hexdigest()


def _validate_call_arguments(
    name: str,
    inputs: Mapping[str, object],
    output_schema: Mapping[str, object],
) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be an object")
    if not isinstance(output_schema, Mapping):
        raise TypeError("output_schema must be an object")


def _coerce_model_choice(model_choice: object) -> RlmModelChoice:
    if isinstance(model_choice, RlmModelChoice):
        return model_choice

    model_id = _member(model_choice, "model_id") or _member(model_choice, "model")
    if not isinstance(model_id, str) or not model_id:
        raise RlmConfigurationError("model_choice must provide a non-empty model_id")

    backend = _member(model_choice, "backend")
    if backend is None:
        for key in ("complete", "invoke", "call"):
            backend = _member(model_choice, key)
            if backend is not None:
                break
    if backend is None and callable(model_choice):
        backend = model_choice
    if backend is None:
        raise RlmConfigurationError(
            "model_choice must provide a tool-free backend callable"
        )

    store = _member(model_choice, "store")
    if not isinstance(store, RlmCallStore):
        store = RlmCallStore()
    replay = _member(model_choice, "replay")
    if not isinstance(replay, dict):
        replay = {}
    role = _member(model_choice, "role") or "worker"
    max_attempts = _member(model_choice, "max_attempts") or MAX_ATTEMPTS
    if not isinstance(role, str) or not isinstance(max_attempts, int):
        raise RlmConfigurationError("model_choice runtime metadata is invalid")
    return RlmModelChoice(
        model_id=model_id,
        backend=backend,
        role=role,
        store=store,
        replay=replay,
        max_attempts=max_attempts,
    )


def _member(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _invoke(backend: object, request: RlmRequest) -> RlmResponse:
    complete = getattr(backend, "complete", None)
    if callable(complete):
        raw = complete(request)
    elif callable(backend):
        raw = backend(request)
    else:
        raise RlmConfigurationError("RLM backend is not callable")
    if inspect.isawaitable(raw):
        raise RlmCallError("RLM backend must be synchronous")
    return normalize_response(raw)


def _record_success(
    choice: RlmModelChoice,
    name: str,
    input_hash: str,
    attempt: int,
    response: RlmResponse,
    result: JsonObject,
    *,
    replayed: bool,
) -> None:
    event = _event(
        name,
        choice.model_id,
        input_hash,
        attempt,
        response,
        replayed=replayed,
        result=result,
    )
    choice.store.commit(choice.role, response.total_tokens, event)


def _record_failure(
    choice: RlmModelChoice,
    name: str,
    input_hash: str,
    attempt: int,
    response: RlmResponse | None,
    *,
    replayed: bool,
    validation_error: str | None,
    call_error: str | None = None,
) -> None:
    event = _event(
        name,
        choice.model_id,
        input_hash,
        attempt,
        response,
        replayed=replayed,
        result=None,
        validation_error=validation_error,
        call_error=call_error,
    )
    tokens = response.total_tokens if response is not None else 0
    choice.store.commit(choice.role, tokens, event)


def _event(
    name: str,
    model_id: str,
    input_hash: str,
    attempt: int,
    response: RlmResponse | None,
    *,
    replayed: bool,
    result: JsonObject | None,
    validation_error: str | None = None,
    call_error: str | None = None,
) -> JsonObject:
    event: JsonObject = {
        "type": "rlm.judgment",
        "name": name,
        "model": model_id,
        "input_hash": input_hash,
        "attempt": attempt,
        "input_tokens": response.input_tokens if response else 0,
        "output_tokens": response.output_tokens if response else 0,
        "total_tokens": response.total_tokens if response else 0,
        "latency_ms": response.latency_ms if response else 0,
        "replayed": replayed,
        "result": _redact(result) if result is not None else None,
    }
    if validation_error is not None:
        event["validation_error"] = validation_error
    if call_error is not None:
        event["call_error"] = call_error
    return event


def _redact(value: JsonValue, key: str | None = None) -> JsonValue:
    if key is not None and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: _redact(child, name) for name, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, key) for child in value]
    return value


__all__ = [
    "MAX_ATTEMPTS",
    "JudgmentFailed",
    "RlmCallError",
    "RlmConfigurationError",
    "RlmError",
    "judgment_hash",
    "rlm",
]
