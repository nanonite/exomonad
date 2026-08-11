"""Closed-key validation for structured RLM responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from tl_loop.client.transport import JsonObject


class OutputSchemaError(ValueError):
    """A model response does not satisfy its closed output schema."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = tuple(errors)
        message = "; ".join(f"{path}: {reason}" for path, reason in errors)
        super().__init__(message)


def validate_output(value: object, schema: Mapping[str, object]) -> JsonObject:
    """Validate and return a JSON object using fail-closed key semantics."""
    errors: list[tuple[str, str]] = []
    _validate_schema(value, schema, "output", errors)
    if errors:
        raise OutputSchemaError(errors)
    if not isinstance(value, dict):
        raise OutputSchemaError([("output", "must be an object")])
    return cast(JsonObject, value)


def _validate_schema(
    value: object,
    schema: Mapping[str, object],
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    schema_type = schema.get("type")
    if not isinstance(schema_type, str):
        errors.append((path, "schema.type must be a string"))
        return
    if not _matches_type(value, schema_type):
        errors.append((path, f"must be a {schema_type}"))
        return

    enum_values = schema.get("enum")
    if enum_values is not None:
        if not isinstance(enum_values, list):
            errors.append((f"{path}.enum", "must be an array"))
        elif value not in enum_values:
            errors.append((path, "must match one of schema.enum"))

    if schema_type == "object":
        _validate_object(value, schema, path, errors)
    elif schema_type == "array":
        _validate_array(value, schema, path, errors)
    elif schema_type == "string":
        _validate_string(value, schema, path, errors)


def _validate_object(
    value: object,
    schema: Mapping[str, object],
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if not isinstance(value, dict):
        return
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        errors.append((f"{path}.properties", "must be an object"))
        return
    property_schemas: dict[str, Mapping[str, object]] = {}
    for key, child_schema in properties.items():
        if not isinstance(key, str) or not isinstance(child_schema, Mapping):
            errors.append((f"{path}.properties", "keys and schemas must be objects"))
            continue
        property_schemas[key] = cast(Mapping[str, object], child_schema)

    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
        errors.append((f"{path}.required", "must be an array of strings"))
    else:
        for key in required:
            if key not in value:
                errors.append((f"{path}.{key}", "required key is missing"))

    additional = schema.get("additionalProperties", False)
    if not isinstance(additional, (bool, Mapping)):
        errors.append((f"{path}.additionalProperties", "must be false, true, or an object"))
        additional = False

    for key, child in value.items():
        if not isinstance(key, str):
            errors.append((path, "object keys must be strings"))
            continue
        child_schema = property_schemas.get(key)
        if child_schema is None:
            if additional is False:
                errors.append((f"{path}.{key}", "unknown key"))
            elif isinstance(additional, Mapping):
                _validate_schema(child, additional, f"{path}.{key}", errors)
            continue
        _validate_schema(child, child_schema, f"{path}.{key}", errors)


def _validate_array(
    value: object,
    schema: Mapping[str, object],
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if not isinstance(value, list):
        return
    items = schema.get("items")
    if not isinstance(items, Mapping):
        errors.append((f"{path}.items", "must be an object"))
        return
    for index, item in enumerate(value):
        _validate_schema(item, items, f"{path}[{index}]", errors)


def _validate_string(
    value: object,
    schema: Mapping[str, object],
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if not isinstance(value, str):
        return
    minimum = schema.get("minLength")
    if type(minimum) is int and len(value) < minimum:
        errors.append((path, f"must contain at least {minimum} characters"))
    maximum = schema.get("maxLength")
    if type(maximum) is int and len(value) > maximum:
        errors.append((path, f"must contain at most {maximum} characters"))


def _matches_type(value: object, schema_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": (type(value) is int or type(value) is float),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(schema_type, False)


__all__ = ["OutputSchemaError", "validate_output"]
